#!/usr/bin/env python3
"""Discover printers and print PDF/JPEG files. Results are JSON; queries never print."""

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import discovery
from adapters import queue_identity
from ipp import PRINTER_ATTRIBUTES, PrinterError, page_ranges, positive, request, values
from store import Store, endpoint_key, normal_uuid

TERMINAL = {"completed", "canceled", "aborted"}


def call(endpoint, operation, args, **kwargs):
    return request(operation, endpoint["uri"], args.timeout, socket_path=endpoint.get("socket_path"),
                   ca_file=args.ca_file, connect_address=endpoint.get("connect_address"), **kwargs)


def refreshed(endpoint, args, mime=None):
    if endpoint["backend"] == "windows":
        raise PrinterError("Windows system queues are inventory-only in v1; use an IPP/IPPS endpoint",
                           code="backend_not_supported")
    requested = PRINTER_ATTRIBUTES.split(",")
    if endpoint["backend"] == "cups":
        requested.append("device-uri")
    info = call(endpoint, "Get-Printer-Attributes", args, requested=requested,
                variables={"mime": mime} if mime else None)["attributes"]
    identity = normal_uuid(info.get("printer-uuid"))
    expected = endpoint.get("queue_uuid") if endpoint["backend"] == "cups" else endpoint.get("physical_uuid")
    if expected and identity != expected:
        raise PrinterError("Printer identity changed; rediscover and select the intended device",
                           code="identity_changed", uri=endpoint["uri"])
    old_info = endpoint.get("info", {})
    if not expected and any(old_info.get(k) and old_info[k] != info.get(k)
                            for k in ("printer-name", "printer-make-and-model")):
        raise PrinterError("Printer identity cannot be confirmed", code="identity_changed")
    if endpoint["backend"] == "cups" and endpoint.get("physical_uuid"):
        if mime and endpoint.get("device_uri") and not info.get("device-uri"):
            raise PrinterError("Cannot confirm the system queue's physical target", code="identity_changed")
        physical = queue_identity(info.get("device-uri"))
        if physical and physical != endpoint["physical_uuid"]:
            raise PrinterError("System queue now targets a different printer", code="identity_changed")
        if endpoint.get("device_uri") and info.get("device-uri") and info["device-uri"] != endpoint["device_uri"]:
            raise PrinterError("System queue target changed; rediscover before printing", code="identity_changed")
    result = dict(endpoint, info=info, verified=True)
    if endpoint["backend"] == "ipp":
        result["physical_uuid"] = identity
    return result


def summarize(device, live_keys=None):
    result = dict(device)
    endpoints = []
    for endpoint in device["endpoints"]:
        ep = dict(endpoint)
        ep["observation"] = "live" if live_keys is not None and endpoint_key(ep) in live_keys else "cached"
        ep["availability"] = ("printable" if discovery.usable(ep) else "communicating" if ep.get("verified") else "candidate")
        if ep["observation"] == "cached":
            ep["availability"] = "cached_unknown"
        endpoints.append(ep)
    result["endpoints"] = endpoints
    return result


def discover_into(store, args):
    endpoints, warnings = discovery.discover(args.scan, args.interface, args.subnet, args.ca_file)
    live = set()
    for endpoint in endpoints:
        store.upsert(endpoint)
        live.add(endpoint_key(endpoint))
    return {"devices": [summarize(d, live) for d in store.devices()], "warnings": warnings}


def select_device(store, args):
    selected = args.device or store.default()
    if selected:
        return store.device(selected)
    devices = store.devices()
    if not devices:
        discover_into(store, args)
        devices = store.devices()
    if len(devices) == 1:
        return devices[0]
    raise PrinterError("Select a printer with --device, then save it with default", code="selection_required",
                       devices=[{"device_id": d["device_id"], "name": d["name"]} for d in devices])


def fresh_endpoints(store, device, args, mime=None):
    endpoints, errors = [], []
    # A previously verified secure route must not be silently downgraded.
    secure = any(urlsplit(e["uri"]).scheme == "ipps" for e in device["endpoints"])
    for endpoint in sorted(device["endpoints"], key=lambda e: (e["backend"] != "ipp", urlsplit(e["uri"]).scheme != "ipps")):
        if secure and endpoint["backend"] == "ipp" and urlsplit(endpoint["uri"]).scheme == "ipp":
            continue
        try:
            current = refreshed(endpoint, args, mime)
            store.upsert(current)
            endpoints.append(current)
        except PrinterError as exc:
            errors.append(dict(exc.result, uri=endpoint["uri"], backend=endpoint["backend"]))
    if not endpoints and device.get("physical_uuid"):
        # Rediscovery may update a DHCP address, but can only recover this UUID.
        discovered, _ = discovery.discover("auto", ca_file=args.ca_file)
        for endpoint in discovered:
            if secure and endpoint["backend"] == "ipp" and urlsplit(endpoint["uri"]).scheme == "ipp":
                continue
            if normal_uuid(endpoint.get("physical_uuid")) == device["physical_uuid"] and endpoint.get("verified"):
                store.upsert(endpoint)
                try:
                    endpoints.append(refreshed(endpoint, args, mime))
                except PrinterError as exc:
                    errors.append(exc.result)
    return endpoints, errors


def inspect_file(path):
    path = path.expanduser().resolve()
    if not path.is_file():
        raise PrinterError("File does not exist", code="invalid_file", file=str(path))
    with path.open("rb") as stream:
        header = stream.read(8)
    if path.suffix.lower() == ".pdf" and header.startswith(b"%PDF-"):
        return path, "application/pdf"
    if path.suffix.lower() in (".jpg", ".jpeg") and header.startswith(b"\xff\xd8\xff"):
        return path, "image/jpeg"
    raise PrinterError("Only PDF/JPEG are supported; convert other formats to PDF first", code="unsupported_file")


def job_options(info, args, mime):
    if mime not in values(info.get("document-format-supported", [])):
        raise PrinterError("Printer does not advertise this file format", code="unsupported_format", mime=mime)
    if info.get("printer-is-accepting-jobs") is not True or info.get("printer-state") == 5:
        raise PrinterError("Printer or queue is stopped or not accepting jobs", code="printer_unavailable",
                           printer_state_reasons=info.get("printer-state-reasons", []))
    operations = values(info.get("operations-supported", []))
    if operations and (2 not in operations or 4 not in operations):
        raise PrinterError("Print-Job and Validate-Job are required by this client", code="operation_not_supported")
    copies = info.get("copies-supported")
    if isinstance(copies, dict) and not copies["lower"] <= args.copies <= copies["upper"]:
        raise PrinterError("Requested copies exceed printer capability", code="unsupported_option", supported=copies)
    if args.copies != 1 and not isinstance(copies, dict):
        raise PrinterError("Printer does not advertise copy-count support", code="unsupported_option")
    lines = [(0x21, "copies", args.copies)]
    if args.pages:
        if info.get("page-ranges-supported") is not True:
            raise PrinterError("Printer does not advertise page-range support", code="unsupported_option")
        lines.append((0x33, "page-ranges", [tuple(map(int, part.split("-"))) for part in args.pages.split(",")]))
    for value, attr, supported in ((args.media, "media", "media-supported"),
                                   (args.sides, "sides", "sides-supported"),
                                   (None if args.color == "auto" else args.color, "print-color-mode", "print-color-mode-supported")):
        if value:
            if value not in values(info.get(supported, [])):
                raise PrinterError(f"Printer does not advertise {attr}={value}", code="unsupported_option",
                                   supported=info.get(supported, []))
            lines.append((0x44, attr, value))
    return lines


def prepare(store, args):
    # Reject disguised files before discovery or any other network request.
    path, mime = inspect_file(args.file)
    device = select_device(store, args)
    endpoints, errors = fresh_endpoints(store, device, args, mime)
    for endpoint in endpoints:
        try:
            lines = job_options(endpoint["info"], args, mime)
        except PrinterError as exc:
            errors.append(dict(exc.result, backend=endpoint["backend"]))
            continue
        variables = {"mime": mime, "job_name": path.name}
        validation = call(endpoint, "Validate-Job", args, variables=variables, job_lines=lines)
        validation.update(device_id=device["device_id"], backend=endpoint["backend"], file=str(path),
                          printer_state_reasons=endpoint["info"].get("printer-state-reasons", []))
        return path, device, endpoint, variables, lines, validation
    raise PrinterError("No confirmed backend can satisfy this print request", code="no_usable_backend", attempts=errors)


def check_job_identity(job, result):
    attrs = result.get("attributes", {})
    if job.get("remote_job_uuid") and attrs.get("job-uuid") != job["remote_job_uuid"]:
        raise PrinterError("Remote job UUID changed", code="job_identity_changed", job_ref=job["job_ref"])
    if job.get("job_name") and attrs.get("job-name") != job["job_name"]:
        raise PrinterError("Remote job identity changed; refusing to act on a reused job-id", code="job_identity_changed",
                           job_ref=job["job_ref"])


def job_status(store, job, args):
    endpoint = refreshed(job["endpoint"], args)
    if not job.get("remote_job_id"):
        matches = [j for j in call(endpoint, "Get-Jobs", args)["jobs"] if j.get("job-name") == job.get("job_name")]
        if len(matches) != 1:
            raise PrinterError("Submission is still unknown; absence from the queue does not prove it was not printed",
                               code="submission_unknown", submission_unknown=True, job_ref=job["job_ref"])
        job["remote_job_id"] = matches[0]["job-id"]
    result = call(endpoint, "Get-Job-Attributes", args, variables={"job_id": job["remote_job_id"]})
    check_job_identity(job, result)
    job.update(attributes=result["attributes"], state=result.get("state", "unknown"), submitted=True,
               submission_unknown=False)
    store.save_job(job)
    return dict(result, job_ref=job["job_ref"], device_id=job["device_id"], backend=job["backend"],
                submitted=True, submission_unknown=False)


def wait_job(store, job, args):
    deadline = time.monotonic() + args.wait
    while args.wait and time.monotonic() < deadline:
        try:
            # Each poll has two reads; reserve half the remaining budget for each.
            polling_args = argparse.Namespace(**vars(args))
            polling_args.timeout = min(args.timeout, max(0.01, (deadline - time.monotonic()) / 2))
            result = job_status(store, job, polling_args)
        except PrinterError as exc:
            return {"tracking_error": exc.result}
        if result["state"] in TERMINAL:
            return result
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))
    return {"state": job["state"], "wait_expired": bool(args.wait)}


def submit(store, args):
    path, device, endpoint, variables, lines, validation = prepare(store, args)
    job_ref = str(uuid.uuid4())
    variables["job_name"] = f"{path.name} [{job_ref}]"
    job = store.new_job(device["device_id"], endpoint, path.name, job_ref, variables["job_name"])
    try:
        result = call(endpoint, "Print-Job", args, variables=variables, job_lines=lines, filename=path)
    except PrinterError as exc:
        job.update(submission_unknown=exc.result.get("submission_unknown", False),
                   submitted=exc.result.get("submitted", False), error=exc.result)
        if exc.result.get("attributes", {}).get("job-id"):
            job["remote_job_id"] = exc.result["attributes"]["job-id"]
        if not job["submission_unknown"]:
            job["state"] = "submitted" if job["submitted"] else "rejected"
        store.save_job(job)
        raise PrinterError(exc.result["error"], **{k: v for k, v in exc.result.items() if k not in ("ok", "error")},
                           job_ref=job_ref, backend=endpoint["backend"], device_id=device["device_id"]) from exc
    job.update(remote_job_id=result["attributes"]["job-id"], submitted=True, submission_unknown=False,
               state=result.get("state", "pending"), remote_job_uuid=result["attributes"].get("job-uuid"))
    store.save_job(job)
    if not store.default() and len(store.devices()) == 1:
        store.set_default(device["device_id"])
    tracking = wait_job(store, job, args)
    result.update(job_ref=job_ref, device_id=device["device_id"], backend=endpoint["backend"],
                  printer_state_reasons=validation["printer_state_reasons"], tracking=tracking,
                  state=tracking.get("state", job["state"]))
    return result


def list_jobs(store, device, args):
    receipts = store.jobs(device["device_id"])
    endpoints = {endpoint_key(e): e for e in device["endpoints"] if e["backend"] != "windows"}
    endpoints.update({endpoint_key(j["endpoint"]): j["endpoint"] for j in receipts})
    remote, warnings = [], []
    for key, endpoint in endpoints.items():
        try:
            current = refreshed(endpoint, args)
            for entry in call(current, "Get-Jobs", args)["jobs"]:
                receipt = next((j for j in receipts if endpoint_key(j["endpoint"]) == key
                                and j.get("job_name") == entry.get("job-name")
                                and (not j.get("remote_job_id") or j["remote_job_id"] == entry["job-id"])), None)
                if receipt is None:
                    receipt = store.new_job(device["device_id"], current, entry.get("job-name", "external job"))
                receipt.update(remote_job_id=entry["job-id"], job_name=entry.get("job-name"),
                               submitted=True, submission_unknown=False, state=entry.get("state", "unknown"),
                               remote_job_uuid=entry.get("job-uuid"))
                store.save_job(receipt)
                remote.append(dict(entry, job_ref=receipt["job_ref"], backend=endpoint["backend"]))
        except PrinterError as exc:
            warnings.append(exc.result)
    return {"device_id": device["device_id"], "jobs": remote,
            "receipts": store.jobs(device["device_id"]), "warnings": warnings}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PrinterError(message, code="invalid_arguments")


def nonnegative(value):
    number = float(value)
    if not 0 <= number <= 3600:
        raise argparse.ArgumentTypeError("Expected 0..3600 seconds")
    return number


def parser():
    root = Parser(description=__doc__)
    root.add_argument("--data-dir", type=Path, help="Override user data directory (also useful for isolated tests)")
    root.add_argument("--timeout", type=positive, default=10)
    root.add_argument("--ca-file", help="CA certificate file for IPPS")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("discover", "list", "add", "default", "info", "doctor", "validate", "print", "jobs", "status", "cancel"):
        sub = commands.add_parser(command)
        if command in ("info", "doctor", "validate", "print", "jobs"):
            sub.add_argument("--device")
        if command == "discover":
            sub.add_argument("--scan", choices=("auto", "never", "always"), default="auto")
            sub.add_argument("--interface")
            sub.add_argument("--subnet")
        if command == "add":
            sub.add_argument("address")
            sub.add_argument("--name")
        if command == "default":
            sub.add_argument("device_id")
        if command in ("validate", "print"):
            sub.add_argument("file", type=Path)
            sub.add_argument("--copies", type=positive, default=1)
            sub.add_argument("--pages", type=page_ranges)
            sub.add_argument("--media", help="Advertised PWG name, e.g. iso_a4_210x297mm")
            sub.add_argument("--sides", choices=("one-sided", "two-sided-long-edge", "two-sided-short-edge"))
            sub.add_argument("--color", choices=("auto", "monochrome", "color"), default="auto")
        if command in ("status", "cancel"):
            sub.add_argument("job_ref")
        if command in ("print", "status"):
            sub.add_argument("--wait", type=nonnegative, default=60 if command == "print" else 0)
    root.set_defaults(scan="auto", interface=None, subnet=None)
    return root


def execute(store, args):
    if args.command == "discover":
        return discover_into(store, args)
    if args.command == "list":
        return {"devices": [summarize(d) for d in store.devices()]}
    if args.command == "add":
        endpoint = discovery.manual_endpoints(args.address, args.timeout, args.ca_file)
        return {"device": summarize(store.upsert(endpoint, args.name), {endpoint_key(endpoint)})}
    if args.command == "default":
        store.set_default(args.device_id)
        return {"device_id": args.device_id}
    if args.command == "print":
        return submit(store, args)
    if args.command == "validate":
        return prepare(store, args)[-1]
    if args.command in ("status", "cancel"):
        job = store.job(args.job_ref)
        result = job_status(store, job, args)
        if args.command == "cancel":
            if job["state"] in TERMINAL:
                raise PrinterError("Job is already terminal; it cannot be canceled", code="job_terminal", state=job["state"])
            result = call(job["endpoint"], "Cancel-Job", args, variables={"job_id": job["remote_job_id"]})
            result.update(job_ref=job["job_ref"], backend=job["backend"], cancellation_requested=True)
            # Cancellation acceptance is not a confirmed canceled job state.
        elif args.wait and job["state"] not in TERMINAL:
            result["tracking"] = wait_job(store, job, args)
        return result
    if args.command == "doctor":
        dependencies = {name: importlib.util.find_spec(name) is not None for name in ("zeroconf", "psutil", "platformdirs")}
        if not args.device and not store.devices():
            return {"dependencies": dependencies, "data_directory": str(store.directory),
                    "warnings": ["No saved printers. Run discover; check LAN/Wi-Fi, guest-network isolation and multicast permissions."]}
    device = select_device(store, args)
    if args.command == "jobs":
        return list_jobs(store, device, args)
    endpoints, errors = fresh_endpoints(store, device, args)
    result = {"device_id": device["device_id"], "endpoints": endpoints, "warnings": errors}
    if args.command == "doctor":
        result.update(dependencies=dependencies, data_directory=str(store.directory),
                      suggestions=["Check the same LAN, guest Wi-Fi isolation, firewall/multicast, TLS CA and system queue state."])
    if not endpoints:
        raise PrinterError("Saved printer is unavailable; no other printer was selected", code="device_unreachable", **result)
    return result


def main(argv=None):
    store = None
    try:
        args = parser().parse_args(argv)
        store = Store(args.data_dir)
        output = {"ok": True, "command": args.command, **execute(store, args)}
        code = 0
    except PrinterError as exc:
        output = exc.result
        code = 2 if output.get("code") == "invalid_arguments" else 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        output = {"ok": False, "code": "local_error", "error": str(exc)}
        code = 1
    finally:
        if store:
            store.close()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

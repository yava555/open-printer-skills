#!/usr/bin/env python3
"""Small IPP client. No redirects and no retries of job creation requests."""

import argparse
import getpass
import http.client
import re
import secrets
import struct
import socket
import ssl
from pathlib import Path
from urllib.parse import unquote, urlsplit

PRINTER_ATTRIBUTES = (
    "printer-name,printer-uuid,printer-uri-supported,printer-make-and-model,printer-state,printer-state-reasons,"
    "printer-is-accepting-jobs,document-format-supported,operations-supported,uri-authentication-supported,uri-security-supported,"
    "media-default,media-supported,copies-supported,page-ranges-supported,sides-supported,print-color-mode-supported,"
    "color-supported,printer-resolution-supported"
)
JOB_ATTRIBUTES = (
    "job-id,job-uri,job-name,job-uuid,job-state,job-state-reasons,"
    "job-impressions-completed,job-media-sheets-completed"
)
JOB_STATES = {3: "pending", 4: "pending-held", 5: "processing",
              6: "processing-stopped", 7: "canceled", 8: "aborted", 9: "completed"}
OPERATIONS = {"Print-Job": 2, "Validate-Job": 4, "Cancel-Job": 8,
              "Get-Job-Attributes": 9, "Get-Jobs": 10, "Get-Printer-Attributes": 11,
              "CUPS-Get-Printers": 0x4002}
STATUS_NAMES = {0: "successful-ok", 1: "successful-ok-ignored-or-substituted-attributes",
                2: "successful-ok-conflicting-attributes", 0x0400: "client-error-bad-request",
                0x0401: "client-error-forbidden", 0x0402: "client-error-not-authenticated",
                0x0403: "client-error-not-authorized", 0x0404: "client-error-not-possible",
                0x0406: "client-error-not-found", 0x040A: "client-error-document-format-not-supported",
                0x040B: "client-error-attributes-or-values-not-supported",
                0x0500: "server-error-internal-error", 0x0501: "server-error-operation-not-supported",
                0x0502: "server-error-service-unavailable", 0x0506: "server-error-not-accepting-jobs",
                0x0507: "server-error-busy"}


class PrinterError(Exception):
    def __init__(self, message, **details):
        self.result = {"ok": False, "error": message, "code": "printer_error", **details}
        super().__init__(message)


def values(value):
    return value if isinstance(value, list) else [value]


def page_ranges(value):
    ranges = []
    for part in value.split(","):
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", part):
            raise argparse.ArgumentTypeError("Use page numbers such as 1 or 1-3,5")
        bounds = [int(x) for x in part.split("-")]
        lo, hi = bounds[0], bounds[-1]
        if not 1 <= lo <= hi <= 2147483647:
            raise argparse.ArgumentTypeError("Page ranges must be positive and ascending")
        ranges.append(f"{lo}-{hi}")
    return ",".join(ranges)


def positive(value):
    number = int(value)
    if not 1 <= number <= 2147483647:
        raise argparse.ArgumentTypeError("Expected a positive 32-bit integer")
    return number


def check_uri(uri):
    parsed = urlsplit(uri)
    if (parsed.scheme not in ("ipp", "ipps") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise PrinterError("Use an ipp:// or ipps:// printer URI without credentials/query/fragment")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
    except ValueError as exc:
        raise PrinterError(str(exc)) from exc


def encode_attribute(tag, name, value):
    result = b""
    for item in values(value):
        if tag in (0x21, 0x23):
            raw = struct.pack(">i", item)
        elif tag == 0x22:
            raw = bytes([bool(item)])
        elif tag == 0x33:
            raw = struct.pack(">ii", *item)
        else:
            raw = str(item).encode("utf-8")
        key = name.encode("ascii")
        result += bytes([tag]) + struct.pack(">H", len(key)) + key + struct.pack(">H", len(raw)) + raw
        name = ""  # Additional values repeat the tag with a zero-length name.
    return result


def decode_response(data, request_id):
    if len(data) < 9 or struct.unpack(">I", data[4:8])[0] != request_id:
        raise ValueError("Missing or mismatched IPP response header")
    code = struct.unpack(">H", data[2:4])[0]
    position, groups, group, last_name = 8, [], None, ""

    def take(length):
        nonlocal position
        if position + length > len(data):
            raise ValueError("Truncated IPP response")
        raw = data[position:position + length]
        position += length
        return raw

    while position < len(data):
        tag = take(1)[0]
        if tag == 3:
            return code, groups
        if tag < 16:
            group, last_name = {}, ""
            groups.append(group)
            continue
        name = take(struct.unpack(">H", take(2))[0]).decode("ascii") or last_name
        raw = take(struct.unpack(">H", take(2))[0])
        if group is None or not name:
            raise ValueError("IPP attribute has no group or name")
        if tag in (0x21, 0x23):
            value = struct.unpack(">i", raw)[0]
        elif tag == 0x22:
            value = bool(raw[0])
        elif tag == 0x33:
            lo, hi = struct.unpack(">ii", raw)
            value = {"lower": lo, "upper": hi}
        elif tag == 0x32:
            xres, yres, units = struct.unpack(">iiB", raw)
            value = {"xres": xres, "yres": yres, "units": "dpi" if units == 3 else "dpcm"}
        elif tag in (0x35, 0x36):
            language_length = struct.unpack(">H", raw[:2])[0]
            text_offset = 2 + language_length
            text_length = struct.unpack(">H", raw[text_offset:text_offset + 2])[0]
            value = raw[text_offset + 2:text_offset + 2 + text_length].decode("utf-8", errors="replace")
        elif tag == 0x34:
            # Collections are not needed by our flat options, but devices may
            # return them unsolicited. Consume nesting without losing alignment.
            depth = 1
            while depth:
                nested_tag = take(1)[0]
                take(struct.unpack(">H", take(2))[0])
                take(struct.unpack(">H", take(2))[0])
                depth += (nested_tag == 0x34) - (nested_tag == 0x37)
            value = {"collection": "not interpreted"}
        elif tag < 0x20:
            value = None
        else:
            value = raw.decode("utf-8", errors="replace") if tag >= 0x40 else raw.hex()
        if name in group:
            group[name] = values(group[name]) + [value]
        else:
            group[name] = value
        last_name = name
    raise ValueError("IPP response has no end-of-attributes tag")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def request(operation, uri, timeout=10, variables=None, job_lines=(), filename=None,
            socket_path=None, ca_file=None, requested=None, connect_address=None):
    """One HTTP POST, without redirects or automatic retries."""
    check_uri(uri)
    variables = variables or {}
    attrs = [(0x47, "attributes-charset", "utf-8"),
             (0x48, "attributes-natural-language", "en"),
             (0x45, "printer-uri", uri),
             (0x42, "requesting-user-name", getpass.getuser())]
    if operation in ("Get-Printer-Attributes", "CUPS-Get-Printers"):
        attrs.append((0x44, "requested-attributes", requested or PRINTER_ATTRIBUTES.split(",")))
        if variables.get("mime"):
            attrs.append((0x49, "document-format", variables["mime"]))
    elif operation in ("Validate-Job", "Print-Job"):
        attrs += [(0x42, "job-name", variables["job_name"]),
                  (0x49, "document-format", variables["mime"]),
                  (0x22, "ipp-attribute-fidelity", True)]
    elif operation == "Get-Jobs":
        attrs += [(0x44, "which-jobs", "all"), (0x22, "my-jobs", False),
                  (0x44, "requested-attributes", JOB_ATTRIBUTES.split(","))]
    else:
        attrs.append((0x21, "job-id", variables["job_id"]))
        if operation == "Get-Job-Attributes":
            attrs.append((0x44, "requested-attributes", JOB_ATTRIBUTES.split(",")))
    request_id = secrets.randbelow(2147483646) + 1
    packet = struct.pack(">BBHI", 1, 1, OPERATIONS[operation], request_id) + b"\x01"
    packet += b"".join(encode_attribute(*attr) for attr in attrs)
    if job_lines:
        packet += b"\x02" + b"".join(encode_attribute(*attr) for attr in job_lines)
    packet += b"\x03"
    parsed = urlsplit(uri)
    host = unquote(parsed.hostname)
    if socket_path:
        connection = UnixHTTPConnection(socket_path, timeout)
    elif parsed.scheme == "ipps":
        connection = http.client.HTTPSConnection(host, parsed.port or 631, timeout=timeout,
                                                context=ssl.create_default_context(cafile=ca_file))
    else:
        connection = http.client.HTTPConnection(host, parsed.port or 631, timeout=timeout)
    if connect_address and not socket_path:
        # DNS-SD has resolved the address even on systems without .local DNS.
        # Keep the original hostname for HTTP Host and TLS SNI/verification.
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
            (connect_address, address[1]), timeout, source_address)
    uncertain = {"submission_unknown": True,
                 "next_step": "Query jobs before resubmitting; the printer may have accepted this job."}
    try:
        size = Path(filename).stat().st_size if filename is not None else 0
        connection.putrequest("POST", parsed.path or "/")
        connection.putheader("Content-Type", "application/ipp")
        connection.putheader("Content-Length", str(len(packet) + size))
        connection.putheader("Connection", "close")
        connection.endheaders(packet)
        if filename is not None:
            with Path(filename).open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    connection.send(chunk)
        response = connection.getresponse()
        if response.status != 200:
            raise PrinterError(f"HTTP {response.status}: {response.reason}",
                               code="authentication_required" if response.status in (401, 403) else "http_error",
                               **(uncertain if operation == "Print-Job" else {}))
        payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("IPP response exceeds 4 MiB")
        code, groups = decode_response(payload, request_id)
    except ssl.SSLCertVerificationError as exc:
        raise PrinterError("TLS certificate verification failed; configure a CA file or use a system queue",
                           code="tls_certificate_error", diagnostic=str(exc)) from exc
    except (OSError, http.client.HTTPException, ValueError, struct.error, IndexError) as exc:
        raise PrinterError("No readable IPP response", operation=operation,
                           code="transport_error", diagnostic=str(exc),
                           **(uncertain if operation == "Print-Job" else {})) from exc
    finally:
        connection.close()
    attributes = {key: value for group in groups for key, value in group.items()}
    status = STATUS_NAMES.get(code, f"{'successful' if code < 256 else 'error'}-0x{code:04x}")
    output = {"ok": code < 256 and (operation != "Validate-Job" or code == 0),
              "operation": operation, "printer_uri": uri, "status": status, "groups": groups}
    if operation == "Get-Jobs":
        output["jobs"] = [dict(group, state=JOB_STATES.get(group.get("job-state"), "unknown"))
                          for group in groups if "job-id" in group]
    else:
        output["attributes"] = attributes
        if "job-state" in attributes:
            output["state"] = JOB_STATES.get(attributes["job-state"], "unknown")
    if operation == "Print-Job":
        output["submitted"] = code < 256
        if output["submitted"]:
            output["next_step"] = "Query status using job-id; submission does not confirm paper output."
            if not isinstance(attributes.get("job-id"), int) or attributes["job-id"] < 1:
                output.update(uncertain, ok=False)
            elif code != 0:
                output.update(ok=False, code="attributes_substituted")
    if not output["ok"]:
        output.setdefault("code", "authentication_required" if code in (0x0401, 0x0402, 0x0403) else "ipp_rejected")
        raise PrinterError("IPP operation failed", **output)
    return output

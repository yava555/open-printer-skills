"""Bounded, read-only DNS-SD and IPv4 IPP discovery."""

import concurrent.futures
import ipaddress
import socket
import threading
import time
from urllib.parse import quote

from adapters import installed_queues
from ipp import PrinterError, check_uri, request, values
from store import normal_uuid

PATHS = ("/ipp/print", "/ipp/printer", "/ipp", "/")


def probe(endpoint, timeout=3, ca_file=None):
    endpoint = dict(endpoint)
    result = request("Get-Printer-Attributes", endpoint["uri"], timeout,
                     socket_path=endpoint.get("socket_path"), ca_file=ca_file,
                     connect_address=endpoint.get("connect_address"))
    info = result["attributes"]
    if "printer-state" not in info or "document-format-supported" not in info:
        raise PrinterError("Endpoint did not return printer capabilities", code="not_ipp_printer")
    endpoint.update(info=info, verified=True)
    if endpoint["backend"] == "ipp":
        endpoint["physical_uuid"] = normal_uuid(info.get("printer-uuid"))
    endpoint.setdefault("name", info.get("printer-name") or info.get("printer-make-and-model") or endpoint["uri"])
    return endpoint


def service_endpoint(info, service_type):
    props = {k.decode("utf-8", "replace").lower(): v.decode("utf-8", "replace") if v else ""
             for k, v in info.properties.items()}
    host = (info.server or "").rstrip(".")
    if not host:
        raise ValueError("DNS-SD service has no target host")
    scheme = "ipps" if service_type.startswith("_ipps.") else "ipp"
    path = "/" + props.get("rp", "").lstrip("/")
    # Preserve valid %-escapes in advertised resource paths.
    resource = quote(path, safe="/%:@!$&'()*+,;=-._~")
    uri = f"{scheme}://{host}:{info.port}{resource}"
    addresses = info.parsed_scoped_addresses()
    return {"backend": "ipp", "uri": uri, "name": info.name.removesuffix("." + service_type),
            "source": "mdns", "physical_uuid": normal_uuid(props.get("uuid")),
            "service": info.name, "verified": False,
            "addresses": addresses, "connect_address": addresses[0] if addresses else None}


def mdns_endpoints(timeout=5):
    try:
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf
    except ImportError:
        return [], [{"code": "missing_dependency", "message": "Install zeroconf for DNS-SD discovery"}]
    found, lock = {}, threading.Lock()

    def changed(zeroconf, service_type, name, state_change):
        # Callbacks only enqueue names: resolving on the browser thread deadlocks.
        with lock:
            found[(service_type, name)] = True

    zc = None
    browser = None
    try:
        zc = Zeroconf(ip_version=IPVersion.All)
        browser = ServiceBrowser(zc, ["_ipp._tcp.local.", "_ipps._tcp.local."], handlers=[changed])
        deadline = time.monotonic() + timeout
        endpoints = []
        while time.monotonic() < deadline:
            with lock:
                names = list(found)
                found.clear()
            for service_type, name in names:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                info = zc.get_service_info(service_type, name, timeout=min(500, int(remaining * 1000)))
                if info:
                    try:
                        endpoints.append(service_endpoint(info, service_type))
                    except ValueError:
                        continue
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return endpoints, []
    except OSError as exc:
        return [], [{"code": "mdns_unavailable", "message": str(exc)}]
    finally:
        if browser:
            browser.cancel()
        if zc:
            zc.close()


def networks(interface=None, subnet=None):
    try:
        import psutil
    except ImportError as exc:
        raise PrinterError("Install psutil to select a local network", code="missing_dependency") from exc
    choices = []
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if interface and name != interface:
            continue
        lowered = name.lower()
        if not interface and (lowered.startswith(("utun", "tun", "tap", "veth", "docker", "br-", "vmnet", "vbox", "virbr", "tailscale", "wg", "awdl", "llw", "bridge"))
                              or any(word in lowered for word in ("vpn", "virtual", "hyper-v", "wsl", "loopback"))):
            continue
        if name not in stats or not stats[name].isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET or not address.netmask:
                continue
            ip = ipaddress.ip_address(address.address)
            if ip.is_loopback or ip.is_link_local or not ip.is_private:
                continue
            network = ipaddress.ip_network(f"{ip}/{address.netmask}", strict=False)
            choices.append((name, network))
    if subnet:
        selected = ipaddress.ip_network(subnet, strict=False)
        if selected.version != 4 or not any(selected.subnet_of(net) for _, net in choices):
            raise PrinterError("Scan subnet must belong to a selected active local IPv4 network", code="invalid_subnet")
        choices = [(name, selected) for name, net in choices if selected.subnet_of(net)]
    unique = list(dict.fromkeys(choices))
    if not unique:
        raise PrinterError("No active LAN interface; check Wi-Fi or specify --interface", code="no_lan")
    if len(unique) != 1:
        raise PrinterError("Several LAN interfaces found; select --interface or --subnet", code="ambiguous_interface",
                           networks=[{"interface": n, "subnet": str(net)} for n, net in unique])
    network = unique[0][1]
    if network.num_addresses > 1024:
        raise PrinterError("Network exceeds 1,024 addresses; specify a smaller --subnet", code="scan_range_too_large")
    return network


def scan_network(network, timeout=30, ca_file=None, ports=(631, 443)):
    deadline = time.monotonic() + timeout

    def scan_host(host):
        results = []
        for port in ports:
            if time.monotonic() >= deadline:
                break
            try:
                with socket.create_connection((str(host), port), timeout=min(0.5, max(0.01, deadline - time.monotonic()))):
                    pass
            except OSError:
                continue
            # An open port alone is not a printer. No document data is sent.
            for scheme in (("ipps", "ipp") if port == 631 else ("ipps",)):
                for path in PATHS:
                    if time.monotonic() >= deadline:
                        break
                    ep = {"backend": "ipp", "uri": f"{scheme}://{host}:{port}{path}",
                          "source": "scan", "verified": False}
                    try:
                        results.append(probe(ep, min(2, max(0.01, deadline - time.monotonic())), ca_file))
                        break
                    except PrinterError as exc:
                        if exc.result.get("code") in ("tls_certificate_error", "authentication_required"):
                            results.append(dict(ep, diagnostic=exc.result))
                            # A TLS endpoint must not silently become plaintext.
                            return results
                if results:
                    break
        return results

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=32)
    pending = {executor.submit(scan_host, host) for host in network.hosts()}
    results = []
    try:
        while pending and time.monotonic() < deadline:
            done, pending = concurrent.futures.wait(pending, timeout=max(0, deadline - time.monotonic()),
                                                    return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                results.extend(future.result())
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return results, ([{"code": "scan_timeout", "message": "Scan reached its deadline; results are partial"}] if pending else [])


def manual_endpoints(address, timeout=3, ca_file=None):
    if "://" in address:
        check_uri(address)
        candidates = [address]
    else:
        # A bare IPv6 address must be bracketed in a URI.
        try:
            host = "[" + address.replace("%", "%25") + "]" if ipaddress.ip_address(address.split("%")[0]).version == 6 else address
        except ValueError:
            host = address
        candidates = [f"{scheme}://{host}:631{path}" for scheme in ("ipps", "ipp") for path in PATHS]
    errors = []
    for uri in candidates:
        try:
            return probe({"backend": "ipp", "uri": uri, "source": "manual", "verified": False}, timeout, ca_file)
        except PrinterError as exc:
            errors.append(exc.result)
            if exc.result.get("code") in ("tls_certificate_error", "authentication_required"):
                return {"backend": "ipp", "uri": uri, "source": "manual", "verified": False,
                        "diagnostic": exc.result}
    raise PrinterError("No usable IPP endpoint; specify the full printer URI", code="device_unreachable", attempts=errors)


def usable(endpoint):
    info = endpoint.get("info", {})
    return (endpoint.get("verified") and endpoint["backend"] != "windows"
            and info.get("printer-is-accepting-jobs") is True and info.get("printer-state") != 5
            and bool(set(values(info.get("document-format-supported", []))) & {"application/pdf", "image/jpeg"}))


def discover(scan="auto", interface=None, subnet=None, ca_file=None):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        queues_future = pool.submit(installed_queues, ca_file=ca_file)
        mdns_future = pool.submit(mdns_endpoints)
        queues, warnings = queues_future.result()
        services, mdns_warnings = mdns_future.result()
    warnings += mdns_warnings
    endpoints = list(queues)
    # Preserve candidates if authentication or TLS prevents a capability query.
    for endpoint in services:
        try:
            endpoints.append(probe(endpoint, ca_file=ca_file))
        except PrinterError as exc:
            endpoints.append(dict(endpoint, diagnostic=exc.result))
    if scan == "always" or (scan == "auto" and not any(usable(e) for e in endpoints)):
        try:
            scanned, scan_warnings = scan_network(networks(interface, subnet), ca_file=ca_file)
            endpoints += scanned
            warnings += scan_warnings
        except (PrinterError, ValueError) as exc:
            warnings.append(exc.result if isinstance(exc, PrinterError) else {"code": "invalid_subnet", "message": str(exc)})
    # An exact IPP device URI is also evidence linking a queue to that endpoint.
    physical_by_uri = {e["uri"]: e.get("physical_uuid") for e in endpoints if e["backend"] == "ipp" and e.get("physical_uuid")}
    for endpoint in endpoints:
        if endpoint["backend"] == "cups" and not endpoint.get("physical_uuid"):
            endpoint["physical_uuid"] = physical_by_uri.get(endpoint.get("device_uri"))
    return endpoints, warnings

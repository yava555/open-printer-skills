"""Read installed queues. Printing uses IPP, not localized command output."""

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ipp import PrinterError, request, values
from store import normal_uuid


def queue_identity(device_uri):
    return normal_uuid(parse_qs(urlsplit(device_uri or "").query).get("uuid", [None])[0])


def cups_queues(timeout=3, ca_file=None):
    server = os.environ.get("CUPS_SERVER")
    if not server and shutil.which("lpstat"):
        result = subprocess.run(["lpstat", "-H"], capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            server = result.stdout.strip()
    if not server:
        server = next((p for p in ("/run/cups/cups.sock", "/var/run/cups/cups.sock",
                                  "/private/var/run/cupsd") if Path(p).exists()), "localhost:631")
    socket_path = server if server.startswith("/") else None
    uri = "ipp://localhost:631/" if socket_path else f"ipp://{server}/"
    result = request("CUPS-Get-Printers", uri, timeout, socket_path=socket_path, ca_file=ca_file,
                     requested=["printer-name", "printer-uri-supported", "printer-uuid", "device-uri",
                                "printer-state", "printer-state-reasons", "printer-is-accepting-jobs",
                                "document-format-supported", "printer-make-and-model"])
    endpoints = []
    for group in result["groups"]:
        if "printer-name" not in group:
            continue
        uris = values(group.get("printer-uri-supported", []))
        if not uris:
            continue
        endpoint = {"backend": "cups", "uri": uris[0], "name": group["printer-name"],
                    "queue_uuid": normal_uuid(group.get("printer-uuid")),
                    "physical_uuid": queue_identity(group.get("device-uri")),
                    "device_uri": group.get("device-uri"), "info": group,
                    "verified": True, "source": "system-queue"}
        if socket_path:
            endpoint["socket_path"] = socket_path
        endpoints.append(endpoint)
    return endpoints


def windows_queues(timeout=5):
    # No input interpolation. UTF-8 is explicit for Windows PowerShell 5.1.
    script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
              "$ErrorActionPreference='Stop'; @(Get-Printer | "
              "Select-Object Name,PortName,PrinterStatus) | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8-sig", timeout=timeout)
    if result.returncode:
        raise PrinterError("Windows PrintManagement enumeration is unavailable", code="adapter_unavailable",
                           diagnostic=result.stderr.strip())
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    return [{"backend": "windows", "uri": "windows:" + row["Name"], "name": row["Name"],
             "info": row, "verified": False, "source": "system-queue", "inventory_only": True}
            for row in rows or []]


def installed_queues(timeout=3, ca_file=None):
    try:
        return (windows_queues(timeout) if platform.system() == "Windows" else cups_queues(timeout, ca_file)), []
    except (PrinterError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return [], [{"code": "adapter_unavailable", "message": str(exc)}]

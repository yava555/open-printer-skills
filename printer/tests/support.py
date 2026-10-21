"""Loopback-only fixtures: nothing here connects to a physical printer."""

import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ipp

PHYSICAL_UUID = "00000001-0000-1000-8000-000000000001"
QUEUE_UUID = "00000002-0000-1000-8000-000000000002"


def attribute(tag, name, value):
    name = name.encode()
    if isinstance(value, str):
        value = value.encode()
    elif isinstance(value, bool):
        value = bytes([value])
    elif isinstance(value, int):
        value = struct.pack(">i", value)
    return bytes([tag]) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value


def decode_request(data):
    operation = struct.unpack(">H", data[2:4])[0]
    position, attributes, last_name = 8, {}, ""
    while data[position] != 3:
        tag = data[position]
        position += 1
        if tag <= 15:
            continue
        length = struct.unpack(">H", data[position:position + 2])[0]
        position += 2
        name = data[position:position + length].decode() or last_name
        position += length
        length = struct.unpack(">H", data[position:position + 2])[0]
        position += 2
        value = data[position:position + length]
        position += length
        attributes.setdefault(name, []).append(value)
        last_name = name
    return operation, attributes, data[position + 1:]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        operation, attributes, document = decode_request(raw)
        self.server.requests.append((operation, attributes, document))
        if self.path != self.server.path:
            self.send_error(404)
            return
        if self.server.http_status != 200:
            self.send_error(self.server.http_status)
            return
        if operation == 2:
            self.server.job_name = attributes["job-name"][0].decode()
            if self.server.drop_print_response:
                self.close_connection = True
                return
        code = self.server.validate_code if operation == 4 else self.server.print_code if operation == 2 else 0
        response = b"\x01\x01" + struct.pack(">H", code) + raw[4:8] + b"\x01"
        response += attribute(0x47, "attributes-charset", "utf-8")
        response += attribute(0x48, "attributes-natural-language", "en")
        if operation in (11, 0x4002):
            response += b"\x04"
            response += attribute(0x42, "printer-name", "测试打印机")
            response += attribute(0x42, "printer-make-and-model", self.server.model)
            response += attribute(0x45, "printer-uuid", "urn:uuid:" + self.server.uuid)
            response += attribute(0x45, "printer-uri-supported", self.server.uri)
            response += attribute(0x45, "device-uri", "dnssd://Fixture._ipps._tcp.local./?uuid=" + PHYSICAL_UUID)
            response += attribute(0x49, "document-format-supported", self.server.formats[0])
            for mime in self.server.formats[1:]:
                response += attribute(0x49, "", mime)
            response += attribute(0x22, "printer-is-accepting-jobs", self.server.accepting)
            response += attribute(0x23, "printer-state", self.server.printer_state)
            response += attribute(0x33, "copies-supported", struct.pack(">ii", 1, 9))
            response += attribute(0x22, "page-ranges-supported", True)
            response += attribute(0x44, "printer-state-reasons", "toner-empty-warning")
            for key, options in (("media-supported", ["iso_a4_210x297mm"]),
                                 ("sides-supported", ["one-sided", "two-sided-long-edge"]),
                                 ("print-color-mode-supported", ["monochrome", "color"])):
                for index, option in enumerate(options):
                    response += attribute(0x44, key if index == 0 else "", option)
        elif operation in (2, 9, 10) and (operation != 10 or self.server.jobs_visible):
            response += b"\x02"
            if not (operation == 2 and self.server.omit_job_id):
                response += attribute(0x21, "job-id", 42)
            response += attribute(0x45, "job-uri", "ipp://localhost/jobs/42")
            response += attribute(0x42, "job-name", self.server.job_name)
            if self.server.job_uuid:
                response += attribute(0x45, "job-uuid", self.server.job_uuid)
            response += attribute(0x23, "job-state", 3 if operation == 2 else self.server.job_state)
            response += attribute(0x44, "job-state-reasons", "none")
        response += b"\x03"
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def configure(server):
    server.requests = []
    server.formats = ["application/pdf", "image/jpeg"]
    server.drop_print_response = False
    server.validate_code = server.print_code = 0
    server.accepting = True
    server.printer_state = 3
    server.job_state = 9
    server.model = "Fixture PDF Printer"
    server.uuid = PHYSICAL_UUID
    server.omit_job_id = False
    server.jobs_visible = True
    server.job_name = "external job.pdf"
    server.job_uuid = None
    server.http_status = 200
    server.path = "/ipp/print"


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    configure(server)
    server.uri = f"ipp://127.0.0.1:{server.server_port}/ipp/print"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread

import contextlib
import io
import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from support import Handler, PHYSICAL_UUID, QUEUE_UUID, configure, serve
import adapters
import ipp
import printer
from store import Store


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.servers = []
        self.pdf = self.root / "fixture.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nmock PDF bytes\n")

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join()
        self.folder.cleanup()

    def server(self):
        server, thread = serve()
        self.servers.append((server, thread))
        return server

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = printer.main(["--data-dir", str(self.root / "state"), *map(str, args)])
        return code, json.loads(output.getvalue())

    def setup_backends(self):
        direct, queue = self.server(), self.server()
        self.call("add", direct.uri)
        queue.uuid = QUEUE_UUID
        store = Store(self.root / "state")
        try:
            device = store.upsert({"backend": "cups", "uri": queue.uri, "physical_uuid": PHYSICAL_UUID,
                                   "queue_uuid": QUEUE_UUID, "verified": True, "info": {}})
        finally:
            store.close()
        return direct, queue, device

    def test_queue_formats_can_satisfy_request_before_submission(self):
        direct, queue, device = self.setup_backends()
        direct.formats = ["image/jpeg"]
        code, result = self.call("print", self.pdf, "--wait", "0")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["backend"], "cups")
        self.assertFalse(any(r[0] == 2 for r in direct.requests))
        self.assertEqual(sum(r[0] == 2 for r in queue.requests), 1)
        # Query receipts after a new CLI invocation: use the scheduler's job-id.
        code, status = self.call("status", result["job_ref"])
        self.assertEqual(code, 0, status)
        self.assertEqual(status["backend"], "cups")

    def test_ambiguous_direct_submission_never_falls_back_to_queue(self):
        direct, queue, device = self.setup_backends()
        direct.drop_print_response = True
        code, result = self.call("print", self.pdf, "--wait", "0")
        self.assertEqual(code, 1, result)
        self.assertTrue(result["submission_unknown"])
        self.assertEqual(sum(r[0] == 2 for r in direct.requests), 1)
        self.assertFalse(any(r[0] == 2 for r in queue.requests))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name != "nt", "Unix CUPS sockets are macOS/Linux only")
    def test_real_unix_socket_uses_same_ipp_transport(self):
        path = str(self.root / "cups.sock")
        server = socketserver.ThreadingUnixStreamServer(path, Handler)
        configure(server)
        server.uri = "ipp://localhost/printers/fixture"
        server.path = "/printers/fixture"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        result = ipp.request("Get-Printer-Attributes", server.uri, socket_path=path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attributes"]["printer-state"], 3)
        self.assertEqual(server.requests[0][2], b"")

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for temporary loopback TLS certificate")
    def test_ipps_rejects_untrusted_certificate_and_accepts_explicit_ca(self):
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-addext", "basicConstraints=critical,CA:TRUE", "-keyout", str(key), "-out", str(cert)],
                       capture_output=True, check=True)
        server = self.server()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        uri = server.uri.replace("ipp://127.0.0.1", "ipps://localhost")
        with self.assertRaises(ipp.PrinterError) as caught:
            ipp.request("Get-Printer-Attributes", uri)
        self.assertEqual(caught.exception.result["code"], "tls_certificate_error")
        result = ipp.request("Get-Printer-Attributes", uri, ca_file=str(cert))
        self.assertTrue(result["ok"])
        self.assertFalse(any(r[0] == 2 for r in server.requests))


if __name__ == "__main__":
    unittest.main()

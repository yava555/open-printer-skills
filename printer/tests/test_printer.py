"""Original eleven regression scenarios plus durable end-to-end CLI behavior."""

import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import PHYSICAL_UUID, serve
import ipp
import printer
from store import Store


class PrinterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = Path(self.folder.name) / "state"
        self.pdf = Path(self.folder.name) / '打印 test "$file".pdf'
        self.pdf.write_bytes(b"%PDF-1.4\nloopback test document\n%%EOF\n")
        self.server, self.thread = serve()
        code, result = self.call("add", self.server.uri)
        self.assertEqual(code, 0, result)
        self.device_id = result["device"]["device_id"]
        self.server.requests.clear()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.folder.cleanup()

    def call(self, *args):
        output = io.StringIO()
        if args[0] == "print":
            args = (*args, "--wait", "0")
        with contextlib.redirect_stdout(output):
            code = printer.main(["--data-dir", str(self.state), *map(str, args)])
        return code, json.loads(output.getvalue())

    def test_validate_never_transmits_document_or_creates_job(self):
        code, result = self.call("validate", self.pdf, "--pages", "1-3,5", "--copies", "2")
        self.assertEqual(code, 0, result)
        self.assertEqual([r[0] for r in self.server.requests], [11, 4])
        self.assertTrue(all(not r[2] for r in self.server.requests))
        attrs = self.server.requests[-1][1]
        self.assertEqual(attrs["page-ranges"], [struct.pack(">ii", 1, 3), struct.pack(">ii", 5, 5)])
        self.assertEqual(attrs["copies"], [struct.pack(">i", 2)])

    def test_print_sends_exact_file_once_and_keeps_job_receipt(self):
        code, result = self.call("print", self.pdf)
        self.assertEqual(code, 0, result)
        self.assertEqual([r[0] for r in self.server.requests], [11, 4, 2])
        self.assertEqual(self.server.requests[-1][2], self.pdf.read_bytes())
        self.assertIn(self.pdf.name.encode(), self.server.requests[-1][1]["job-name"][0])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["attributes"]["job-id"], 42)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["printer_state_reasons"], "toner-empty-warning")
        code, status = self.call("status", result["job_ref"])
        self.assertEqual(code, 0, status)
        self.assertEqual(status["state"], "completed")

    def test_status_jobs_and_cancel_use_the_requested_job(self):
        self.server.job_state = 3
        _, result = self.call("print", self.pdf)
        for command, operation in (("status", 9), ("jobs", 10), ("cancel", 8)):
            args = (command,) if command == "jobs" else (command, result["job_ref"])
            code, output = self.call(*args)
            self.assertEqual(code, 0, output)
            self.assertEqual(self.server.requests[-1][0], operation)
            if command != "jobs":
                self.assertEqual(self.server.requests[-1][1]["job-id"], [struct.pack(">i", 42)])

    def test_unsupported_format_never_submits(self):
        self.server.formats = ["image/jpeg"]
        code, result = self.call("print", self.pdf)
        self.assertEqual(code, 1)
        self.assertEqual(result["attempts"][0]["code"], "unsupported_format")
        self.assertEqual([r[0] for r in self.server.requests], [11])

    def test_disguised_file_rejected_before_network_access(self):
        self.pdf.write_bytes(b"This is not a PDF")
        code, result = self.call("print", self.pdf)
        self.assertEqual(code, 1)
        self.assertEqual(self.server.requests, [])

    def test_validation_rejection_or_substitution_never_prints(self):
        for status in (0x040B, 1):
            self.server.requests.clear()
            self.server.validate_code = status
            code, result = self.call("print", self.pdf, "--pages", "1")
            self.assertEqual(code, 1, result)
            self.assertEqual([r[0] for r in self.server.requests], [11, 4])

    def test_jpeg_is_sent_with_its_explicit_mime_type(self):
        jpeg = self.pdf.with_suffix(".jpeg")
        jpeg.write_bytes(b"\xff\xd8\xfftest jpeg")
        code, result = self.call("print", jpeg)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.server.requests[-1][1]["document-format"], [b"image/jpeg"])
        self.assertEqual(self.server.requests[-1][2], jpeg.read_bytes())

    def test_truncated_or_unrelated_response_is_rejected(self):
        response = b"\x01\x01\x00\x00" + struct.pack(">I", 42) + b"\x01\x03"
        with self.assertRaises(ValueError):
            ipp.decode_response(response, 43)
        with self.assertRaises(ValueError):
            ipp.decode_response(response[:-1], 42)

    def test_lost_print_response_is_not_retried(self):
        self.server.drop_print_response = True
        code, result = self.call("print", self.pdf)
        self.assertEqual(code, 1)
        self.assertTrue(result["submission_unknown"], result)
        self.assertEqual([r[0] for r in self.server.requests], [11, 4, 2])
        self.server.drop_print_response = False
        code, status = self.call("status", result["job_ref"])
        self.assertEqual(code, 0, status)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(sum(r[0] == 2 for r in self.server.requests), 1)

    def test_submission_timeout_reports_uncertainty(self):
        with patch.object(ipp.http.client.HTTPConnection, "endheaders", side_effect=TimeoutError("timed out")):
            with self.assertRaises(ipp.PrinterError) as caught:
                ipp.request("Print-Job", self.server.uri,
                            variables={"mime": "application/pdf", "job_name": "test.pdf"})
        self.assertTrue(caught.exception.result["submission_unknown"])

    def test_invalid_ranges_and_copy_counts(self):
        for invalid in ("0", "4-2", "1,0", "-1", "1;x", "2147483648"):
            with self.assertRaises(ipp.argparse.ArgumentTypeError):
                ipp.page_ranges(invalid)
        with self.assertRaises(ipp.argparse.ArgumentTypeError):
            ipp.positive("0")

    def test_all_supported_options_are_encoded(self):
        code, result = self.call("validate", self.pdf, "--media", "iso_a4_210x297mm", "--color", "color",
                                 "--sides", "two-sided-long-edge")
        self.assertEqual(code, 0, result)
        attrs = self.server.requests[-1][1]
        self.assertEqual(attrs["print-color-mode"], [b"color"])
        self.assertEqual(attrs["sides"], [b"two-sided-long-edge"])

    def test_stopped_queue_and_unsupported_options_never_submit(self):
        for option in (("--copies", "10"), ("--media", "unknown"), ("--sides", "two-sided-short-edge")):
            code, result = self.call("print", self.pdf, *option)
            self.assertEqual(code, 1, result)
        self.server.printer_state = 5
        code, result = self.call("print", self.pdf)
        self.assertEqual(code, 1, result)
        self.assertFalse(any(r[0] in (2, 4) for r in self.server.requests))

    def test_missing_job_id_remains_unknown(self):
        self.server.omit_job_id = True
        code, result = self.call("print", self.pdf)
        self.assertEqual(code, 1, result)
        self.assertTrue(result["submission_unknown"])
        self.assertTrue(result["submitted"])
        self.assertEqual(sum(r[0] == 2 for r in self.server.requests), 1)

    def test_absent_unknown_job_does_not_imply_success(self):
        self.server.drop_print_response = True
        _, result = self.call("print", self.pdf)
        self.server.jobs_visible = False
        code, status = self.call("status", result["job_ref"])
        self.assertEqual(code, 1, status)
        self.assertTrue(status["submission_unknown"])
        self.assertEqual(sum(r[0] == 2 for r in self.server.requests), 1)

    def test_reused_remote_id_is_not_canceled(self):
        _, result = self.call("print", self.pdf)
        self.server.job_name = "somebody else's document"
        code, status = self.call("cancel", result["job_ref"])
        self.assertEqual(code, 1, status)
        self.assertEqual(status["code"], "job_identity_changed")
        self.assertFalse(any(r[0] == 8 for r in self.server.requests))

    def test_reused_id_and_name_with_different_job_uuid_is_not_canceled(self):
        self.server.job_uuid = "urn:uuid:" + PHYSICAL_UUID
        self.server.job_state = 3
        _, result = self.call("print", self.pdf)
        self.server.job_uuid = "urn:uuid:00000003-0000-1000-8000-000000000003"
        code, status = self.call("cancel", result["job_ref"])
        self.assertEqual(code, 1, status)
        self.assertEqual(status["code"], "job_identity_changed")
        self.assertFalse(any(r[0] == 8 for r in self.server.requests))

    def test_identity_change_never_prints_on_new_device(self):
        self.server.uuid = "00000003-0000-1000-8000-000000000003"
        with patch.object(printer.discovery, "discover", return_value=([], [])):
            code, result = self.call("print", self.pdf)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["attempts"][0]["code"], "identity_changed")
        self.assertFalse(any(r[0] == 2 for r in self.server.requests))

    def test_interrupt_leaves_durable_unknown_intent(self):
        original = printer.call
        def interrupted(endpoint, operation, args, **kwargs):
            if operation == "Print-Job":
                raise KeyboardInterrupt()
            return original(endpoint, operation, args, **kwargs)
        with patch.object(printer, "call", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.call("print", self.pdf)
        store = Store(self.state)
        try:
            jobs = store.jobs(self.device_id)
            self.assertEqual(len(jobs), 1)
            self.assertTrue(jobs[0]["submission_unknown"])
            self.assertIn("job_name", jobs[0])
        finally:
            store.close()

    def test_argument_errors_are_json_with_exit_two(self):
        code, output = self.call("validate", self.pdf, "--copies", "0")
        self.assertEqual(code, 2, output)
        self.assertEqual(output["code"], "invalid_arguments")

    def test_http_authentication_is_reported(self):
        self.server.http_status = 401
        with patch.object(printer.discovery, "discover", return_value=([], [])):
            code, result = self.call("info")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["warnings"][0]["code"], "authentication_required")

    def test_jobs_returns_refs_for_explicit_external_job_selection(self):
        code, result = self.call("jobs")
        self.assertEqual(code, 0, result)
        self.assertEqual(len(result["jobs"]), 1)
        self.assertTrue(result["jobs"][0]["job_ref"])

    def test_receipt_backend_is_not_changed_by_later_discovery(self):
        _, result = self.call("print", self.pdf)
        with patch.object(printer.discovery, "discover", side_effect=AssertionError("No discovery for job tracking")):
            code, status = self.call("status", result["job_ref"])
        self.assertEqual(code, 0, status)
        self.assertEqual(status["backend"], "ipp")

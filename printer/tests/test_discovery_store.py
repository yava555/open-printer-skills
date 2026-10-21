import concurrent.futures
import contextlib
import ipaddress
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import PHYSICAL_UUID, QUEUE_UUID, attribute
import adapters
import discovery
import ipp
import printer
from store import Store


def endpoint(uri="ipp://printer.local:631/ipp/print", backend="ipp", physical=PHYSICAL_UUID):
    return {"uri": uri, "backend": backend, "physical_uuid": physical, "verified": True,
            "info": {"printer-state": 3, "printer-is-accepting-jobs": True,
                     "document-format-supported": ["application/pdf"]}, "name": "Fixture"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(self.folder.name)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_queue_uuid_is_not_physical_uuid(self):
        physical = self.store.upsert(endpoint())
        queue = endpoint("ipp://localhost/printers/Fixture", "cups")
        queue["queue_uuid"] = QUEUE_UUID
        stored = self.store.upsert(queue)
        self.assertEqual(stored["device_id"], physical["device_id"])
        self.assertEqual(len(stored["endpoints"]), 2)
        unrelated = endpoint("ipp://localhost/printers/Unknown", "cups", physical=None)
        unrelated["queue_uuid"] = PHYSICAL_UUID
        self.assertNotEqual(self.store.upsert(unrelated)["device_id"], physical["device_id"])

    def test_dhcp_updates_keep_default_and_user_label(self):
        old = self.store.upsert(endpoint(), "书房")
        self.store.set_default(old["device_id"])
        new = self.store.upsert(endpoint("ipp://10.1.1.9:631/custom"))
        self.assertEqual(new["device_id"], old["device_id"])
        self.assertEqual(new["name"], "书房")
        self.assertTrue(new["default"])
        self.store.close()
        self.store = Store(self.folder.name)
        self.assertEqual(self.store.default(), old["device_id"])
        cached = printer.summarize(self.store.device(old["device_id"]))
        self.assertTrue(all(e["availability"] == "cached_unknown" for e in cached["endpoints"]))

    def test_ip_reassignment_does_not_reassign_saved_default(self):
        old = self.store.upsert(endpoint())
        self.store.set_default(old["device_id"])
        changed = endpoint(physical=QUEUE_UUID)
        new = self.store.upsert(changed)
        self.assertNotEqual(old["device_id"], new["device_id"])
        self.assertEqual(self.store.default(), old["device_id"])
        self.assertEqual(self.store.device(old["device_id"])["endpoints"], [])

    def test_candidate_can_be_verified_without_changing_id(self):
        candidate = self.store.upsert(dict(endpoint(physical=None), verified=False))
        verified = self.store.upsert(endpoint())
        self.assertEqual(candidate["device_id"], verified["device_id"])

    def test_offline_observation_keeps_verified_identity(self):
        known = self.store.upsert(endpoint())
        saved = self.store.upsert(dict(endpoint(physical=None), verified=False, info={}))
        saved = self.store.upsert(dict(endpoint(physical=None), verified=False, info={}))
        self.assertEqual(known["device_id"], saved["device_id"])
        self.assertEqual(saved["physical_uuid"], PHYSICAL_UUID)

    def test_concurrent_discovery_deduplicates_by_uuid(self):
        def save(index):
            connection = Store(self.folder.name)
            try:
                return connection.upsert(endpoint(f"ipp://host{index}/ipp/print"))["device_id"]
            finally:
                connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(save, range(12)))
        self.assertEqual(len(set(ids)), 1)

    def test_default_offline_does_not_switch_to_other_printer(self):
        first = self.store.upsert(endpoint())
        self.store.set_default(first["device_id"])
        other = endpoint("ipp://other/ipp/print", physical=QUEUE_UUID)
        self.store.upsert(other)
        args = printer.parser().parse_args(["info"])
        with patch.object(printer, "refreshed", side_effect=ipp.PrinterError("offline")), \
             patch.object(printer.discovery, "discover", return_value=([other], [])):
            selected = printer.select_device(self.store, args)
            current, errors = printer.fresh_endpoints(self.store, selected, args)
        self.assertEqual(selected["device_id"], first["device_id"])
        self.assertEqual(current, [])

    def test_failed_tls_never_uses_plaintext_even_after_rediscovery(self):
        secure = endpoint("ipps://printer.local/ipp/print")
        saved = self.store.upsert(secure)
        clear = endpoint()
        self.store.upsert(clear)
        calls = []
        def refresh(ep, args, mime=None):
            calls.append(ep["uri"])
            raise ipp.PrinterError("certificate", code="tls_certificate_error")
        args = printer.parser().parse_args(["info"])
        with patch.object(printer, "refreshed", side_effect=refresh), \
             patch.object(printer.discovery, "discover", return_value=([clear], [])):
            current, _ = printer.fresh_endpoints(self.store, self.store.device(saved["device_id"]), args)
        self.assertEqual(current, [])
        self.assertEqual(calls, [secure["uri"]])


class DiscoveryTests(unittest.TestCase):
    def test_mdns_uses_advertised_host_port_resource_and_ipv6_scope(self):
        info = SimpleNamespace(server="printer.local.", port=8631,
                               name="书房._ipps._tcp.local.", properties={b"rp": b"custom/queue", b"UUID": PHYSICAL_UUID.encode()},
                               parsed_scoped_addresses=lambda: ["fe80::123%en0"])
        found = discovery.service_endpoint(info, "_ipps._tcp.local.")
        self.assertEqual(found["uri"], "ipps://printer.local:8631/custom/queue")
        self.assertEqual(found["physical_uuid"], PHYSICAL_UUID)
        self.assertEqual(found["connect_address"], "fe80::123%en0")

    def fake_interfaces(self, mask="255.255.254.0"):
        address = SimpleNamespace(family=socket.AF_INET, address="10.2.0.5", netmask=mask)
        vpn = SimpleNamespace(family=socket.AF_INET, address="10.9.0.1", netmask="255.255.255.0")
        return {"en0": [address], "utun0": [vpn]}, {"en0": SimpleNamespace(isup=True), "utun0": SimpleNamespace(isup=True)}

    def test_real_mask_is_used_and_vpn_excluded(self):
        addresses, stats = self.fake_interfaces()
        with patch("psutil.net_if_addrs", return_value=addresses), patch("psutil.net_if_stats", return_value=stats):
            self.assertEqual(str(discovery.networks()), "10.2.0.0/23")

    def test_windows_local_area_connection_is_not_mistaken_for_loopback(self):
        addresses, stats = self.fake_interfaces()
        addresses["Local Area Connection"] = addresses.pop("en0")
        stats["Local Area Connection"] = stats.pop("en0")
        with patch("psutil.net_if_addrs", return_value=addresses), patch("psutil.net_if_stats", return_value=stats):
            self.assertEqual(str(discovery.networks()), "10.2.0.0/23")

    def test_large_range_requires_explicit_smaller_local_subnet(self):
        addresses, stats = self.fake_interfaces("255.255.0.0")
        with patch("psutil.net_if_addrs", return_value=addresses), patch("psutil.net_if_stats", return_value=stats):
            with self.assertRaises(ipp.PrinterError) as caught:
                discovery.networks()
            self.assertEqual(caught.exception.result["code"], "scan_range_too_large")
            self.assertEqual(str(discovery.networks(subnet="10.2.1.0/24")), "10.2.1.0/24")
            with self.assertRaises(ipp.PrinterError):
                discovery.networks(subnet="192.168.0.0/24")

    def test_multiple_lans_are_explicit(self):
        addresses, stats = self.fake_interfaces()
        addresses["eth0"] = addresses.pop("utun0")
        stats["eth0"] = stats.pop("utun0")
        with patch("psutil.net_if_addrs", return_value=addresses), patch("psutil.net_if_stats", return_value=stats):
            with self.assertRaises(ipp.PrinterError) as caught:
                discovery.networks()
            self.assertEqual(caught.exception.result["code"], "ambiguous_interface")
            self.assertEqual(str(discovery.networks(interface="en0")), "10.2.0.0/23")

    def test_open_web_port_is_not_reported_as_printer(self):
        with patch.object(discovery.socket, "create_connection", return_value=contextlib.nullcontext()), \
             patch.object(discovery, "probe", side_effect=ipp.PrinterError("not IPP")):
            found, warnings = discovery.scan_network(ipaddress.ip_network("127.0.0.0/30"), ports=(443,))
        self.assertEqual(found, [])

    def test_single_format_value_is_printable_without_extra_scan(self):
        ep = endpoint()
        ep["info"]["document-format-supported"] = "application/pdf"
        self.assertTrue(discovery.usable(ep))

    def test_scanning_deadline_returns_partial_results(self):
        def slow_probe(ep, timeout, ca_file):
            time.sleep(0.05)
            raise ipp.PrinterError("unresponsive")
        before = time.monotonic()
        with patch.object(discovery.socket, "create_connection", return_value=contextlib.nullcontext()), \
             patch.object(discovery, "probe", side_effect=slow_probe):
            found, warnings = discovery.scan_network(ipaddress.ip_network("127.0.0.0/30"), timeout=0.01)
            time.sleep(0.06)  # Drain the two in-flight mock requests before patches end.
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(warnings[0]["code"], "scan_timeout")

    def test_manual_uri_preserves_path_and_port(self):
        with patch.object(discovery, "probe", side_effect=lambda e, *a: e) as probe:
            result = discovery.manual_endpoints("ipp://host:9876/queue/special")
        self.assertEqual(result["uri"], "ipp://host:9876/queue/special")
        self.assertEqual(probe.call_count, 1)

    def test_manual_ipv6_is_bracketed(self):
        with patch.object(discovery, "probe", side_effect=lambda e, *a: e):
            result = discovery.manual_endpoints("fe80::1%en0")
        self.assertIn("[fe80::1%25en0]:631", result["uri"])

    def test_missing_system_adapter_does_not_disable_mdns(self):
        with patch.object(discovery, "installed_queues", return_value=([], [{"code": "adapter_unavailable"}])), \
             patch.object(discovery, "mdns_endpoints", return_value=([endpoint()], [])), \
             patch.object(discovery, "probe", side_effect=lambda e, **kw: e):
            found, warnings = discovery.discover(scan="never")
        self.assertEqual(len(found), 1)
        self.assertEqual(warnings[0]["code"], "adapter_unavailable")

    def test_windows_enumeration_handles_chinese_names_and_inventory_only(self):
        completed = SimpleNamespace(returncode=0, stdout='[{"Name":"书房 打印机","PortName":"USB001"}]', stderr="")
        with patch.object(adapters.subprocess, "run", return_value=completed) as run:
            result = adapters.windows_queues()
        self.assertEqual(result[0]["name"], "书房 打印机")
        self.assertTrue(result[0]["inventory_only"])
        self.assertIn("-NonInteractive", run.call_args.args[0])

    def test_tls_validation_is_not_disabled(self):
        with patch.object(ipp.ssl, "create_default_context", wraps=ipp.ssl.create_default_context) as context, \
             patch.object(ipp.http.client.HTTPSConnection, "endheaders", side_effect=ipp.ssl.SSLCertVerificationError("untrusted")):
            with self.assertRaises(ipp.PrinterError) as caught:
                ipp.request("Get-Printer-Attributes", "ipps://fixture/ipp/print")
        context.assert_called_once_with(cafile=None)
        self.assertEqual(caught.exception.result["code"], "tls_certificate_error")

    def test_ipp_collections_do_not_break_following_attributes(self):
        packet = b"\x01\x01\x00\x00" + struct.pack(">I", 1) + b"\x04"
        packet += attribute(0x34, "media-col", b"")
        packet += attribute(0x4A, "", "media-size") + attribute(0x34, "", b"")
        packet += attribute(0x4A, "", "x-dimension") + attribute(0x21, "", 21000)
        packet += attribute(0x37, "", b"") + attribute(0x37, "", b"")
        packet += attribute(0x23, "printer-state", 3) + b"\x03"
        _, groups = ipp.decode_response(packet, 1)
        self.assertEqual(groups[0]["printer-state"], 3)


if __name__ == "__main__":
    unittest.main()

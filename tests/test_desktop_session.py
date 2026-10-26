"""Desktop protocol and acquisition contracts; no USB devices or network IO."""
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hu_lab import client as client_module
from hu_lab.client import HUClient
from hu_lab.audio import MEDIA_INIT, MEDIA_DATA
from hu_lab.desktop_session import (AccessoryTransportFactory, DesktopConfig,
    DesktopConnectionError, DesktopV2Session, ExplicitAOATransportFactory,
    HARD_KEYS, create_client, list_usb_devices)
from hu_lab.honda_reference import (MD_FEATURE_REQUEST, CARLIFE_SUBSCRIBE_DONE, list_body)
from hu_lab.protocol import (CMD, TOUCH, HARD_KEY, MEDIA, VIDEO, VIDEO_DATA,
    VIDEO_INIT, VIDEO_INIT_DONE, VERSION_MATCH, HUSession, Packet, ProtocolError, fields, protobuf)
from hu_lab.usb import USBIdentity, RecoveryBlocked, select_bulk_pair
from hu_lab.public import prepare_usb


def descriptor(identity):
    return {"identity": asdict(identity), "physical_key": identity.physical_key,
        "configuration": 1, "interfaces": [
        {"number": 0, "alternate": 0, "class": 255, "subclass": 255, "protocol": 0,
         "endpoints": [{"address": 0x81, "attributes": 2, "max_packet": 512},
                       {"address": 2, "attributes": 2, "max_packet": 512}]}]}


class FakeUSB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.closed = False
        self.handle = None
        self.opened_descriptor = None

    def discover(self):
        self.calls.append("discover")
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def open_exact(self, identity):
        self.calls.append(("open_exact", identity))
        self.handle = FakeHandle(self, identity, self.opened_descriptor or self.rows[0])
        return self.handle

    def close(self):
        self.calls.append("close")
        self.closed = True


class FakeHandle:
    def __init__(self, usb, identity, row):
        self.usb, self.identity, self.descriptor = usb, identity, row
        self.cancel = threading.Event()
        self.closed = False
        self.pair = None

    def claim_accessory(self, gate):
        gate.require(self.identity)
        self.pair = select_bulk_pair(self.descriptor)
        self.usb.calls.append("claim_accessory")

    def close(self):
        self.closed = True
        self.cancel.set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def serial_sha256(self):
        self.usb.calls.append("read_serial")
        return "a" * 64


class DesktopUSBTests(unittest.TestCase):
    def setUp(self):
        self.identity = USBIdentity(230, (4, 7), 0x18D1, 0x2D01, 18)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def factory(self, usb, identity=None):
        return AccessoryTransportFactory(identity or self.identity,
            usb_factory=lambda: usb, lock_directory=self.tmp.name)

    def test_inventory_is_descriptor_only_and_keeps_unsupported_devices(self):
        normal = replace(self.identity, pid=0x4EE1)
        usb = FakeUSB([descriptor(normal), descriptor(self.identity)])
        rows = list_usb_devices(lambda: usb)
        self.assertEqual(usb.calls, ["discover", "close"])
        self.assertTrue(rows[0]["desktop_supported"])
        self.assertFalse(rows[1]["desktop_supported"])
        self.assertIn("does not switch USB", rows[1]["desktop_reason"])

    def test_exact_accessory_claim_is_exclusive_and_released(self):
        usb = FakeUSB([descriptor(self.identity)])
        stream = self.factory(usb)()
        try:
            self.assertEqual(usb.calls, ["discover", ("open_exact", self.identity), "claim_accessory"])
            self.assertFalse(stream.evidence["mode_switch_requested"])
            other = FakeUSB([descriptor(self.identity)])
            with self.assertRaisesRegex(DesktopConnectionError, "already owns"):
                self.factory(other)()
            self.assertEqual(other.calls, [])
        finally:
            stream.close()
        self.assertTrue(usb.handle.closed)
        self.assertTrue(usb.closed)
        second = self.factory(FakeUSB([descriptor(self.identity)]))()
        second.close()

    def test_non_accessory_or_missing_path_rejected_before_any_open(self):
        for identity in (replace(self.identity, pid=0x4EE1), replace(self.identity, ports=())):
            usb = FakeUSB([descriptor(identity)])
            with self.assertRaises(DesktopConnectionError):
                self.factory(usb, identity)
            self.assertEqual(usb.calls, [])

    def test_changed_address_or_usb_configuration_never_claimed(self):
        row = descriptor(replace(self.identity, address=19))
        usb = FakeUSB([row])
        with self.assertRaisesRegex(DesktopConnectionError, "changed"):
            self.factory(usb)()
        self.assertEqual(usb.calls, ["discover", "close"])
        row = descriptor(self.identity)
        row["configuration"] = 2
        usb = FakeUSB([row])
        with self.assertRaisesRegex(DesktopConnectionError, "configuration 1"):
            self.factory(usb)()
        self.assertNotIn("claim_accessory", usb.calls)

    def test_accessory_returned_to_normal_requires_refresh_without_implicit_aoa(self):
        normal = replace(self.identity, pid=0x4EE1, address=19)
        usb = FakeUSB([descriptor(normal)])
        with patch("hu_lab.public.prepare_usb") as prepare:
            with self.assertRaisesRegex(DesktopConnectionError, "refresh the USB device list"):
                self.factory(usb)()
            prepare.assert_not_called()
        self.assertEqual(usb.calls, ["discover", "close"])

    def test_second_enumeration_race_closes_open_handle_without_claim(self):
        usb = FakeUSB([descriptor(self.identity)])
        usb.opened_descriptor = descriptor(replace(self.identity, address=19))
        with self.assertRaisesRegex(DesktopConnectionError, "changed"):
            self.factory(usb)()
        self.assertTrue(usb.handle.closed)
        self.assertTrue(usb.closed)
        self.assertNotIn("claim_accessory", usb.calls)

    def test_discovery_failure_closes_libusb(self):
        usb = FakeUSB([])
        with patch.object(usb, "discover", side_effect=RuntimeError("descriptor failure")):
            with self.assertRaisesRegex(RuntimeError, "descriptor failure"):
                list_usb_devices(lambda: usb)
        self.assertTrue(usb.closed)

    def test_explicit_aoa_factory_prepares_only_on_connect_and_reuses_admission(self):
        normal = replace(self.identity, pid=0x4EE1)
        with patch("hu_lab.public.prepare_usb") as prepare:
            factory = ExplicitAOATransportFactory(normal, self.tmp.name)
            prepare.assert_not_called()
            stream = factory()
            self.assertIs(stream, prepare.return_value.return_value)
            factory()
            prepare.assert_called_once()
            self.assertTrue(prepare.call_args.kwargs["aoa_start"])
            self.assertEqual(prepare.call_args.kwargs["expected_identity"], normal)
            self.assertEqual(prepare.return_value.call_count, 2)

    def test_aoa_preparation_binds_serial_but_performs_no_claim_or_switch(self):
        normal = replace(self.identity, pid=0x4EE1)
        usb = FakeUSB([descriptor(normal)])
        prepared = prepare_usb(normal.physical_key, self.tmp.name,
            aoa_start=True, expected_identity=normal, usb_factory=lambda: usb)
        self.assertEqual(usb.calls, ["discover", ("open_exact", normal), "read_serial", "close"])
        self.assertTrue(prepared.allow_aoa_start)
        self.assertFalse(prepared.start_requested)
        self.assertEqual(prepared.identity.serial_sha256, "a" * 64)
        self.assertTrue(usb.handle.closed)
        self.assertTrue(usb.closed)

    def test_aoa_preparation_rejects_both_discovery_and_open_enumeration_races(self):
        normal = replace(self.identity, pid=0x4EE1)
        changed = replace(normal, address=19)
        for changed_at_open in (False, True):
            usb = FakeUSB([descriptor(normal if changed_at_open else changed)])
            if changed_at_open:
                usb.opened_descriptor = descriptor(changed)
            with self.subTest(changed_at_open=changed_at_open), self.assertRaisesRegex(RecoveryBlocked, "changed"):
                prepare_usb(normal.physical_key, self.tmp.name, aoa_start=True,
                    expected_identity=normal, usb_factory=lambda: usb)
            self.assertNotIn("read_serial", usb.calls)
            self.assertNotIn("claim_accessory", usb.calls)
            self.assertTrue(usb.closed)


class DesktopSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.number = 0

    def make_client(self, config=DesktopConfig(), audio_sink=None):
        self.number += 1
        client = create_client(config, Path(self.tmp.name) / str(self.number), audio_sink)
        # No writer was started, so avoid waiting for synthetic queued commands.
        def cleanup():
            client.halt.set()
            client.stop()
        self.addCleanup(cleanup)
        return client

    def test_profiles_are_per_instance_and_preserve_baseline_class(self):
        v2 = self.make_client(DesktopConfig(profile="v2", fps=24))
        v1 = self.make_client()
        self.assertIs(type(v2.session), DesktopV2Session)
        self.assertIs(type(v1.session), HUSession)
        self.assertIs(client_module.HUSession, HUSession)
        self.assertEqual(fields(v2.session.start()[0].payload)[1], 2)
        self.assertEqual(fields(v1.session.start()[0].payload)[1], 1)
        replies = v2.session.receive(Packet(CMD, VERSION_MATCH, protobuf(f1=1)))
        self.assertFalse(any(p.message == VIDEO_INIT for p in replies))
        replies = v2.session.receive(Packet(CMD, MD_FEATURE_REQUEST))
        init = next(p for p in replies if p.message == VIDEO_INIT)
        self.assertEqual(fields(init.payload), {1: 1280, 2: 720, 3: 24})

    def test_v2_accepts_current_metadata_flags_but_rejects_wrong_ids(self):
        session = DesktopV2Session()
        good = list_body([dict(f1=0, f2=1), dict(f1=1, f2=0), dict(f1=2, f2=1)])
        self.assertEqual(session.receive(Packet(CMD, CARLIFE_SUBSCRIBE_DONE, good)), [])
        for values in ([dict(f1=0, f2=1)], [dict(f1=0, f2=2), dict(f1=1, f2=0), dict(f1=2, f2=1)]):
            with self.assertRaises(ProtocolError):
                session.receive(Packet(CMD, CARLIFE_SUBSCRIBE_DONE, list_body(values)))

    def test_resolution_fallback_is_explicit(self):
        for allow in (False, True):
            session = DesktopV2Session(1280, 720, 30, allow_mode_fallback=allow)
            session.start()
            session.receive(Packet(CMD, VERSION_MATCH, protobuf(f1=1)))
            session.receive(Packet(CMD, MD_FEATURE_REQUEST))
            accepted = Packet(CMD, VIDEO_INIT_DONE, protobuf(f1=800, f2=480, f3=25))
            if allow:
                session.receive(accepted)
                self.assertEqual(session.accepted["width"], 800)
                self.assertEqual((session.requested["width"], session.requested["height"]), (1280, 720))
            else:
                with self.assertRaisesRegex(ProtocolError, "dimensions differ"):
                    session.receive(accepted)
                self.assertIsNone(session.accepted)

    def test_tcp_lan_opt_in_does_not_weaken_existing_dev_transport(self):
        client = self.make_client(DesktopConfig(host="192.0.2.5", port=7240))
        self.assertEqual(client.backend, "TCP_STREAM")
        self.assertEqual(client.host, "192.0.2.5")
        with self.assertRaisesRegex(ValueError, "loopback"):
            HUClient("192.0.2.5", 7240, self.tmp.name)

    def test_invalid_settings_do_not_create_clients(self):
        for config in (DesktopConfig(host="http://example.com"), DesktopConfig(port=0),
                       DesktopConfig(fps=61), DesktopConfig(width=0), DesktopConfig(transport="usb"),
                       DesktopConfig(profile="invented")):
            with self.subTest(config=config), self.assertRaises((ValueError, DesktopConnectionError)):
                create_client(config, self.tmp.name)

    def test_mode_switch_requires_explicit_checkbox_and_client_creation_is_passive(self):
        normal = USBIdentity(230, (4, 7), 0x18D1, 0x4EE1, 18)
        with patch("hu_lab.desktop_session._desktop_usb_factory") as usb:
            with self.assertRaisesRegex(DesktopConnectionError, "explicitly enable"):
                self.make_client(DesktopConfig(transport="usb", usb_identity=normal))
            enabled = self.make_client(DesktopConfig(transport="usb", usb_identity=normal, aoa_start=True))
            self.assertIsInstance(enabled.transport_factory, ExplicitAOATransportFactory)
            self.assertIsNone(enabled.transport_factory.prepared)
            usb.assert_not_called()
        with self.assertRaisesRegex(ValueError, "USB-only"):
            DesktopConfig(aoa_start=True).validate()

    def test_keys_use_carlife_enum_and_require_live_state(self):
        client = self.make_client()
        with self.assertRaisesRegex(ProtocolError, "live streaming"):
            client.hard_key("next")
        client.session.state = "STREAMING"
        for key, expected in (("home", 1), ("back", 14), ("previous", 15),
                              ("next", 16), ("play", 31), ("stop", 32)):
            client.hard_key(key)
            packet = client.commands.get_nowait()
            client.commands.task_done()
            self.assertEqual((packet.channel, packet.message, fields(packet.payload)),
                             (TOUCH, HARD_KEY, {1: expected}))
        with self.assertRaisesRegex(ProtocolError, "Unsupported"):
            client.hard_key("android_media_next_87")
        client.halt.set()
        with self.assertRaisesRegex(ProtocolError, "live streaming"):
            client.hard_key("home")

    def test_audio_callback_failure_does_not_kill_audio_diagnostics(self):
        class Sink:
            def submit(self, packet):
                raise RuntimeError("speaker disconnected")
        client = self.make_client(audio_sink=Sink())
        client.session.state = "STREAMING"
        client._receive_packet(Packet(MEDIA, MEDIA_INIT, protobuf(f1=48000, f2=2, f3=16)), BytesIO())
        client._receive_packet(Packet(MEDIA, MEDIA_DATA, b"\x01\x00\x02\x00"), BytesIO())
        self.assertFalse(client.halt.is_set())
        self.assertIsNone(client.error)
        self.assertEqual(client.audio_callback_errors, 2)
        self.assertEqual(client.summary()["audio"]["lanes"]["MEDIA"]["pcm_frames"], 1)

    def test_v2_empty_video_heartbeat_does_not_enter_decoder(self):
        client = self.make_client(DesktopConfig(profile="v2"))
        client.session.state = "STREAMING"
        client._receive_packet(Packet(VIDEO, VIDEO_DATA, b"", 123), BytesIO())
        self.assertEqual(client.reference_empty_video_heartbeats, 1)
        self.assertEqual(client.video_chunks, 0)
        self.assertEqual(client.media.size, 0)


if __name__ == "__main__":
    unittest.main()

"""Reconnect the previously verified AOA device after its USB address changes."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from hu_lab.public import prepare_usb
from hu_lab.usb import USBEnumerationChanged
from tests.test_usb_acquisition import ACCESSORY, NORMAL, SERIAL, MemoryBus


class AccessoryReselectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.asserted_identity = replace(ACCESSORY, serial_sha256=hashlib.sha256(SERIAL).hexdigest())

    def prepare(self, expected, usb_factory):
        return prepare_usb(expected.physical_key, self.output, aoa_start=True,
                           expected_identity=expected, usb_factory=usb_factory)

    def test_reconnect_verified_accessory_at_new_address_without_another_aoa_start(self):
        current = replace(ACCESSORY, address=ACCESSORY.address + 1)
        bus = MemoryBus(current)
        release = Mock(return_value=0)

        def usb_factory():
            context = bus.factory()
            context.lib.libusb_release_interface = release
            return context

        with patch('hu_lab.native_usb.usb_lock_directory', return_value=self.output):
            factory = self.prepare(self.asserted_identity, usb_factory)
            stream = factory()
            try:
                self.assertEqual(stream.evidence['entry'], 'ALREADY_ACCESSORY')
                self.assertEqual(stream.evidence['identity']['address'], current.address)
                self.assertEqual(bus.opens, [current, current])
                self.assertEqual(bus.claims, [0])
                self.assertEqual(bus.controls, [])
            finally:
                stream.close()
        release.assert_called_once()

    def test_replacement_phone_fails_serial_check_before_any_control_or_claim(self):
        bus = MemoryBus(replace(ACCESSORY, address=ACCESSORY.address + 1))
        mismatched = replace(self.asserted_identity, serial_sha256='f' * 64)
        with self.assertRaises(USBEnumerationChanged) as caught:
            self.prepare(mismatched, bus.factory)
        self.assertEqual(bus.opens, [bus.current])
        self.assertEqual(bus.serial_reads, 1)
        self.assertEqual((bus.controls, bus.claims), ([], []))
        self.assertEqual(caught.exception.connection_failure['stage'], 'preflight_accessory_open')
        self.assertEqual(caught.exception.observed_identity.address, bus.current.address)
        self.assertFalse((self.output / 'usb-selection.json').exists())

    def test_address_rebinding_requires_known_serial_and_exact_accessory_and_port(self):
        for expected, current in (
            (ACCESSORY, replace(ACCESSORY, address=12)),
            (self.asserted_identity, replace(ACCESSORY, ports=(3, 1))),
            (self.asserted_identity, replace(ACCESSORY, bus=3)),
            (self.asserted_identity, replace(ACCESSORY, pid=0x2d00, address=12)),
            (self.asserted_identity, NORMAL),
            (replace(NORMAL, serial_sha256=self.asserted_identity.serial_sha256), replace(NORMAL, address=12)),
        ):
            with self.subTest(expected=expected, current=current):
                bus = MemoryBus(current)
                with self.assertRaises((USBEnumerationChanged, ValueError)):
                    self.prepare(expected, bus.factory)
                self.assertEqual((bus.opens, bus.controls, bus.claims), ([], [], []))

    def test_ambiguous_port_is_not_opened_and_a_second_address_change_is_rejected(self):
        bus = MemoryBus(replace(ACCESSORY, address=12))
        context = bus.factory()
        with patch.object(context, 'discover', return_value=context.discover() * 2), self.assertRaises(ValueError):
            self.prepare(self.asserted_identity, lambda: context)
        rows = context.discover()
        bus.current = replace(bus.current, address=13)
        with patch.object(context, 'discover', return_value=rows), self.assertRaises(USBEnumerationChanged):
            self.prepare(self.asserted_identity, lambda: context)
        self.assertEqual((bus.opens, bus.controls, bus.claims), ([], [], []))

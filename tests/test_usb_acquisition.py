"""USB acquisition failures, using the actual adapter with an in-memory libusb."""
import ctypes as C
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hu_lab.native_usb import NativeUSBTransportFactory
from hu_lab.platform_support import open_owner_lock
from hu_lab.public import prepare_usb
from hu_lab.usb import LibUSB, USBEnumerationChanged, USBError, USBIdentity, open_for_acquisition


NORMAL = USBIdentity(1, (2, 3), 0x1234, 0x5678, 4)
ACCESSORY = replace(NORMAL, vid=0x18d1, pid=0x2d01, address=9)
SERIAL = b"private-test-serial"


class MemoryBus:
    """Keep real open_exact, serial verification and AOA control flow."""
    def __init__(self, initial=NORMAL, *, fail_open=0, fail_claim=False):
        self.current, self.fail_open, self.fail_claim = initial, fail_open, fail_claim
        self.opens, self.controls, self.claims = [], [], []
        self.serial_reads = self.freed_lists = 0
        self.wait_error = self.fail_control = None
        self.fail_cleanup = False

    def factory(self):
        bus = self

        class Context(LibUSB):
            def __init__(self):
                self.lib = SimpleNamespace(libusb_open=bus.open, libusb_close=bus.close,
                    libusb_free_device_list=bus.free_list,
                    libusb_get_string_descriptor_ascii=bus.serial,
                    libusb_control_transfer=bus.control, libusb_claim_interface=bus.claim)

            def _list(self):
                return [bus.current], 1

            def _describe(self, identity):
                return dict(identity=asdict(identity), physical_key=identity.physical_key,
                    configuration=1, serial_index=1, interfaces=[dict(number=0, alternate=0,
                        **{'class': 255}, subclass=255, protocol=0, endpoints=[
                            dict(attributes=2, address=0x81, max_packet=512),
                            dict(attributes=2, address=2, max_packet=512)])])

            def wait_accessory(self, original, cancel, timeout):
                if bus.wait_error is not None:
                    raise bus.wait_error
                return super().wait_accessory(original, cancel, timeout)

            def close(self):
                bus.close()
        return Context()

    def free_list(self, *_):
        self.freed_lists += 1

    def close(self, *_):
        if self.fail_cleanup and self.claims:
            raise OSError("secondary failure")

    def open(self, device, destination):
        self.opens.append(device)
        if len(self.opens) == self.fail_open:
            return -5
        C.cast(destination, C.POINTER(C.c_void_p))[0] = C.c_void_p(1)
        return 0

    def serial(self, handle, index, buffer, length):
        self.serial_reads += 1
        buffer[:len(SERIAL)] = SERIAL
        return len(SERIAL)

    def control(self, handle, direction, request, value, index, buffer, length, timeout):
        self.controls.append(request)
        if request == self.fail_control:
            return -7
        if request == 51:
            buffer[:2] = (2, 0)
        elif request == 53:
            self.current = ACCESSORY
        return length

    def claim(self, handle, interface):
        self.claims.append(interface)
        return -5 if self.fail_claim else 0


def identity_record(identity):
    return dict(bus=identity.bus, ports=list(identity.ports), vid=identity.vid,
                pid=identity.pid, vid_pid=f'{identity.vid:04x}:{identity.pid:04x}', address=identity.address)


class AcquisitionEvidenceTests(unittest.TestCase):
    def fail_connection(self, bus, *, failed_log=False):
        selected = bus.current
        with tempfile.TemporaryDirectory() as temporary, patch('hu_lab.native_usb.usb_lock_directory',
                                                               return_value=Path(temporary)):
            output = Path(temporary) / 'session'
            with self.assertRaises(USBError) as caught:
                factory = prepare_usb(selected.physical_key, output, aoa_start=True,
                                      usb_factory=bus.factory, expected_identity=selected)
                factory()
            log = output / 'native-transport.jsonl'
            if log.exists() and not failed_log:
                failure = json.loads(log.read_text().splitlines()[-1])
                self.assertEqual(failure['stage'], 'acquisition_failed')
                self.assertEqual(failure['connection_failure'], caught.exception.connection_failure)
            return caught.exception

    def test_identical_libusb_open_error_preserves_each_actual_stage_and_target(self):
        for initial, fail_open, stage in (
            (NORMAL, 1, 'preflight_normal_open'), (ACCESSORY, 1, 'preflight_accessory_open'),
            (NORMAL, 2, 'native_normal_open'), (ACCESSORY, 2, 'native_accessory_open'),
            (NORMAL, 3, 'post_aoa_accessory_open'),
        ):
            with self.subTest(stage=stage):
                bus = MemoryBus(initial, fail_open=fail_open)
                error = self.fail_connection(bus)
                details = error.connection_failure
                self.assertEqual(str(error), 'open selected device: libusb NOT_FOUND')
                self.assertEqual((details['operation'], details['code'], details['name']),
                                 (error.operation, error.code, 'NOT_FOUND'))
                self.assertEqual(details['stage'], stage)
                self.assertEqual(details['reason'], 'usb_error')
                self.assertEqual(details['selected_identity'], identity_record(initial))
                self.assertEqual(details['attempted_identity'], identity_record(bus.opens[-1]))
                self.assertEqual(len(bus.opens), fail_open)
                self.assertNotIn('serial', json.dumps(details))
                self.assertEqual(details['aoa_control_complete'], fail_open == 3)
                self.assertEqual(bus.controls, [51] + [52] * 6 + [53] if fail_open == 3 else [])
                self.assertEqual(bus.claims, [])

    def test_claim_is_not_reported_as_an_open_or_an_unconfirmed_transition(self):
        for initial in (NORMAL, ACCESSORY):
            with self.subTest(initial=initial):
                bus = MemoryBus(initial, fail_claim=True)
                error = self.fail_connection(bus)
                report = error.connection_failure
                self.assertEqual(report['stage'], 'accessory_claim')
                self.assertIn('claim accessory interface', report['operation'])
                self.assertEqual(report['attempted_identity'], identity_record(ACCESSORY))
                self.assertEqual(report['aoa_control_complete'], initial == NORMAL)
                self.assertEqual(bus.claims, [0])
                self.assertEqual(bus.controls.count(53), int(initial == NORMAL))

    def test_changed_address_between_lists_stops_before_open(self):
        bus = MemoryBus(replace(NORMAL, address=7))
        with self.assertRaises(USBEnumerationChanged) as caught:
            open_for_acquisition(bus.factory(), NORMAL, selected_identity=NORMAL, stage='native_normal_open')
        details = caught.exception.connection_failure
        self.assertEqual(details['selected_identity'], identity_record(NORMAL))
        self.assertEqual(details['observed_identity'], identity_record(bus.current))
        self.assertEqual(details['attempted_identity'], None)
        self.assertEqual((details['reason'], details['code'], details['name']),
                         ('enumeration_changed', None, None))
        self.assertEqual(bus.opens, [])
        self.assertEqual((bus.serial_reads, bus.controls, bus.claims), (0, [], []))
        self.assertEqual(bus.freed_lists, 1)

    def test_failure_of_enumeration_never_claims_a_native_open_was_attempted(self):
        bus = MemoryBus()
        context = bus.factory()
        original = USBError('device list', -5)
        with patch.object(context, '_list', side_effect=original), self.assertRaises(USBError) as caught:
            open_for_acquisition(context, NORMAL, selected_identity=NORMAL, stage='native_normal_open')
        self.assertIs(caught.exception, original)
        self.assertEqual(original.connection_failure['operation'], 'device list')
        self.assertIsNone(original.connection_failure['attempted_identity'])
        self.assertEqual(bus.opens, [])

    def test_preflight_missing_or_replaced_selection_never_attempts_open(self):
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                bus = MemoryBus(replace(NORMAL, address=8))
                context = bus.factory()
                rows = [] if missing else context.discover()
                with patch.object(context, 'discover', return_value=rows), self.assertRaises(
                        ValueError if missing else USBEnumerationChanged) as caught:
                    prepare_usb(NORMAL.physical_key, temporary, usb_factory=lambda: context, expected_identity=NORMAL)
                record = caught.exception.connection_failure
                self.assertEqual(record['stage'], 'preflight_selection')
                self.assertEqual(record['reason'], 'enumeration_changed')
                self.assertIsNone(record['attempted_identity'])
                self.assertEqual(record['observed_identity'], None if missing else identity_record(bus.current))
                self.assertEqual(bus.opens, [])

    def test_control_failure_is_not_an_open_or_confirmation_of_aoa_completion(self):
        for request in (51, 52, 53):
            with self.subTest(request=request):
                bus = MemoryBus()
                bus.fail_control = request
                error = self.fail_connection(bus)
                record = error.connection_failure
                self.assertEqual(record['stage'], 'aoa_control')
                self.assertEqual((record['operation'], record['code']), (f'control request {request}', -7))
                self.assertEqual(record['attempted_identity'], identity_record(NORMAL))
                self.assertIsNone(record['aoa_control_complete'])
                self.assertEqual(bus.controls[-1], request)
                self.assertEqual(bus.controls.count(53), int(request == 53))
                self.assertEqual(bus.opens, [NORMAL, NORMAL])

    def test_post_start_wait_preserves_timeout_and_scan_error_without_retry(self):
        for original in (TimeoutError('accessory did not appear'), USBError('device list', -5)):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as temporary:
                bus = MemoryBus()
                bus.wait_error = original
                with patch('hu_lab.native_usb.usb_lock_directory', return_value=Path(temporary)):
                    factory = prepare_usb(NORMAL.physical_key, temporary, aoa_start=True,
                                          usb_factory=bus.factory, expected_identity=NORMAL)
                    with self.assertRaises(type(original)) as caught:
                        factory()
                self.assertIs(caught.exception, original)
                record = original.connection_failure
                self.assertEqual(record['stage'], 'post_aoa_wait')
                self.assertTrue(record['aoa_control_complete'])
                self.assertIsNone(record['attempted_identity'])
                self.assertEqual(record['reason'], 'timeout' if isinstance(original, TimeoutError) else 'usb_error')
                self.assertEqual(bus.controls, [51] + [52] * 6 + [53])
                self.assertEqual(bus.opens, [NORMAL, NORMAL])
                self.assertEqual(bus.claims, [])

    def test_secondary_cleanup_and_logging_failures_do_not_replace_primary(self):
        bus = MemoryBus(ACCESSORY, fail_claim=True)
        bus.fail_cleanup = True
        owners = []
        def acquire_owner(path):
            owners.append(open_owner_lock(path))
            return owners[-1]
        original_event = NativeUSBTransportFactory.event
        def event(factory, stage, **details):
            if stage == 'acquisition_failed':
                raise OSError('log not writable')
            return original_event(factory, stage, **details)
        with patch.object(NativeUSBTransportFactory, 'event', event), patch(
                'hu_lab.native_usb.open_owner_lock', side_effect=acquire_owner):
            error = self.fail_connection(bus, failed_log=True)
        self.assertTrue(owners[0].closed)
        self.assertEqual(error.operation, 'claim accessory interface (no detach/seize fallback)')
        self.assertEqual(error.connection_failure['cleanup_errors'],
                         ['handle.close: OSError', 'context.close: OSError', 'acquisition_failed log: OSError'])

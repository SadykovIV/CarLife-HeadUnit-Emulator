import plistlib
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hu_lab.macos_usb_owner import _interface_owner, accessory_interface_owner
from hu_lab.usb import USBIdentity


PHONE = USBIdentity(1, (2,), 0x18d1, 0x2d01, 2)
SESSION = 0x100000002
CHILDREN = 'IORegistryEntryChildren'


def interface(number, owner=None, **values):
    row = {'IOObjectClass': 'IOUSBHostInterface', 'bInterfaceNumber': number,
           'bAlternateSetting': 0, 'bConfigurationValue': 1, **values}
    if owner is not None:
        row['UsbExclusiveOwner'] = owner
    return row


def device(*interfaces, **values):
    return {'sessionID': SESSION, 'idVendor': PHONE.vid, 'idProduct': PHONE.pid,
            'USB Address': PHONE.address, CHILDREN: list(interfaces), **values}


class MacOSAccessoryOwnerTests(unittest.TestCase):
    def test_owner_is_interface_zero_even_when_parent_and_adb_interface_are_owned_by_adb(self):
        phone = device(interface(1, 'pid 21, adb'), interface(0, 'pid 58, CarLife HeadUnit'),
                       UsbExclusiveOwner='pid 21, adb')
        self.assertEqual(_interface_owner([phone], PHONE, SESSION), 'pid 58, CarLife HeadUnit')
        del phone[CHILDREN][1]['UsbExclusiveOwner']
        self.assertIsNone(_interface_owner([phone], PHONE, SESSION))

    def test_only_matching_device_enumeration_contributes_an_owner(self):
        ours = device(interface(0, 'CarLife'))
        for field, value in (('sessionID', SESSION + 1), ('idProduct', 0x2d00), ('idVendor', 0x04e8),
                            ('USB Address', PHONE.address + 1)):
            with self.subTest(field=field):
                foreign = device(interface(0, 'Different application'), **{field: value})
                self.assertIsNone(_interface_owner([foreign], PHONE, SESSION))
                self.assertEqual(_interface_owner([foreign, ours], PHONE, SESSION), 'CarLife')

    def test_an_ambiguous_device_or_interface_cannot_be_attributed(self):
        phone = device(interface(0, 'First'), interface(1, 'adb'))
        self.assertIsNone(_interface_owner([phone, device(interface(0, 'Second'))], PHONE, SESSION))
        phone[CHILDREN].append(interface(0, 'Second'))
        self.assertIsNone(_interface_owner([phone], PHONE, SESSION))

    def test_wrong_class_alt_or_configuration_and_missing_nodes_do_not_supply_an_owner(self):
        for row in (interface(0, 'Driver', IOObjectClass='ChildService'),
                    interface(0, 'Other alternate', bAlternateSetting=1),
                    interface(0, 'Other configuration', bConfigurationValue=2),
                    interface(1, 'adb'), interface(0), interface(0, b'bad type')):
            with self.subTest(row=row):
                self.assertIsNone(_interface_owner([device(row)], PHONE, SESSION))
        no_children = device()
        del no_children[CHILDREN]
        self.assertIsNone(_interface_owner([no_children], PHONE, SESSION))
        self.assertIsNone(_interface_owner([], PHONE, SESSION))

    def test_owner_text_is_one_bounded_line(self):
        owner = ' \n pid 42,\r\n\t CarLife    HeadUnit  '
        self.assertEqual(_interface_owner([device(interface(0, owner))], PHONE, SESSION),
                         'pid 42, CarLife HeadUnit')
        self.assertIsNone(_interface_owner([device(interface(0, '\n\t '))], PHONE, SESSION))
        self.assertEqual(_interface_owner([device(interface(0, 'a' * 300))], PHONE, SESSION), 'a' * 128)

    def test_lookup_needs_session_and_tolerates_unavailable_registry(self):
        handle = SimpleNamespace(identity=PHONE, descriptor={})
        with patch('hu_lab.macos_usb_owner.subprocess.run') as run:
            self.assertIsNone(accessory_interface_owner(handle))
            run.assert_not_called()
            handle.descriptor['darwin_session_id'] = SESSION
            run.return_value.stdout = plistlib.dumps([device(interface(0, 'CarLife'))])
            self.assertEqual(accessory_interface_owner(handle), 'CarLife')
            run.return_value.stdout = b'unavailable'
            self.assertIsNone(accessory_interface_owner(handle))
            run.return_value.stdout = b'<?xml version="1.0"?><plist><array>'
            self.assertIsNone(accessory_interface_owner(handle))
            run.return_value.stdout = plistlib.dumps({'unavailable': True})
            self.assertIsNone(accessory_interface_owner(handle))
            run.side_effect = subprocess.TimeoutExpired('ioreg', 0.6)
            self.assertIsNone(accessory_interface_owner(handle))

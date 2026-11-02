"""Setup eligibility only: mock Windows inventory, no USB or driver operations."""
from copy import deepcopy
from dataclasses import asdict, replace
import unittest

from hu_lab.usb import USBIdentity
from hu_lab import windows_usb_interfaces as w
from tests.test_windows_usb_interfaces import PnP


HW0=r'USB\VID_18D1&PID_2D01&MI_00'
REV0=r'USB\VID_18D1&PID_2D01&REV_0100&MI_00'
HW1=r'USB\VID_18D1&PID_2D01&MI_01'


def interface(number,sub,proto,ep):
    return {'number':number,'alternate':0,'class':255,'subclass':sub,'protocol':proto,
            'endpoints':[dict(address=ep|0x80,attributes=2,max_packet=512),
                         dict(address=ep,attributes=2,max_packet=512)]}


class DriverSetupTargetTests(unittest.TestCase):
    def setUp(self):
        self.id=USBIdentity(3,(6,),0x18d1,0x2d01,2,'FakePrivateHash')
        self.descriptor=dict(identity=asdict(self.id),windows_devinst=100,device_bcd=0x0100,
            configuration=1,interfaces=[interface(0,255,0,1),interface(1,66,1,2)])
        self.cm=PnP()
        self.cm.add(100,r'USB\VID_18D1&PID_2D01\PrivateParent',service='usbccgp')
        self.cm.props[100,w.LOCATION]=(r'PCIROOT(0)#USBROOT(0)#USB(6)',)
        self.cm.add(101,HW0+r'\PrivateChild',100,'OtherDriver')
        self.cm.props[101,w.HARDWARE]=(REV0.lower(),HW0.swapcase())
        self.cm.props[101,w.COMPATIBLE]=(r'USB\Class_FF&SubClass_FF&Prot_00',)
        self.cm.add(102,HW1+r'\PrivateADB',100,'WinUSB')
        self.cm.props[102,w.HARDWARE]=(HW1,)
        self.cm.props[102,w.COMPATIBLE]=(r'USB\Class_FF&SubClass_42&Prot_01',)
        # An identical product also has a qualifying hardware ID. Do not select
        # its devnode through a global hardware-ID or serial-based search.
        self.cm.add(200,r'USB\VID_18D1&PID_2D01\OtherPrivateParent',service='usbccgp')
        self.cm.add(201,HW0+r'\OtherPrivateChild',200,None)
        self.cm.props[201,w.HARDWARE]=(HW0,)

    def find(self):
        return w.find_driver_setup_target(self.id,self.descriptor,api=self.cm)

    def fails(self,action,reason):
        with self.assertRaises(w.WindowsInterfaceError) as c: action()
        self.assertEqual(c.exception.selection_reason,reason)
        self.assertNotIn('Private',str(c.exception))

    def test_exact_unbound_child_and_private_per_instance_native_handoff(self):
        target=self.find()
        self.assertEqual((target.status,target.hardware_id,target.target_devinst),('installable',HW0,101))
        self.assertEqual(target.handoff(),dict(parent_devinst=100,child_devinst=101,ports=(6,),
            adb_devinst=102,hardware_id=HW0))
        self.assertEqual(target.native_instance_id(),self.cm.instance(101))
        self.assertNotIn('Private',repr(target))
        self.assertEqual(self.cm.requested,[])  # No interface paths, WinUSB handles or claims.
        self.cm.props[101,w.SERVICE]=('WinUSB',)
        self.fails(target.native_instance_id,'device_changed')
        self.fails(target.handoff,'device_changed')
        self.assertEqual(self.find().status,'already_winusb')
        self.cm.props[101,w.SERVICE]=()
        self.cm.props[101,w.LOWER_FILTERS]=('Other','WINUSB')
        self.assertEqual(self.find().status,'already_winusb')

    def test_revision_id_must_be_reported_not_invented(self):
        self.cm.props[101,w.HARDWARE]=(REV0.lower(),)
        self.assertEqual(self.find().hardware_id,REV0)
        self.cm.props[101,w.HARDWARE]=(REV0.replace('0100','0200'),HW0)
        self.fails(self.find,'device_changed')

    def test_compatible_id_alias_parent_adb_and_grandchild_are_never_substituted(self):
        self.cm.props[101,w.HARDWARE]=()
        self.cm.props[101,w.COMPATIBLE]=(HW0,)
        self.fails(self.find,'missing_mi00')
        self.cm.props[101,w.HARDWARE]=(HW0,)
        for path in (r'SAMSUNG\VID_18D1&PID_2D01&MI_00\PrivateChild',r'USB\VID_18D1&PID_2D01&ADB\PrivateChild',
                     HW1+r'\PrivateChild',r'USB\VID_18D1&PID_2D01\PrivateChild'):
            with self.subTest(path=path):
                self.cm.add(101,path,100)
                self.fails(self.find,'missing_mi00')
        self.cm.add(103,r'OEM\BRIDGE\Private',100)
        self.cm.add(101,HW0+r'\PrivateChild',103,None)
        self.fails(self.find,'missing_mi00')

    def test_substring_mixed_or_inconsistent_mi00_evidence_rejected(self):
        for ids in ((HW0+'&EXTRA',),(HW0[4:],),(HW0,HW1),(r'UſB\VID_18D1&PID_2D01&MI_00',)):
            self.cm.props[101,w.HARDWARE]=ids
            self.fails(self.find,'missing_mi00')
        self.cm.props[101,w.HARDWARE]=(HW0,)
        self.cm.props[101,w.COMPATIBLE]=(r'USB\Class_FF&SubClass_42&Prot_01',)
        self.fails(self.find,'invalid_accessory_descriptor')

    def test_configuration_interface_class_alternate_and_both_bulk_pairs_required(self):
        for update in (
                lambda d:d.update(configuration=2),
                lambda d:d['interfaces'][0].update(alternate=1),
                lambda d:d['interfaces'][0].update(subclass=66,protocol=1),
                lambda d:d['interfaces'][1].update(protocol=0),
                lambda d:d['interfaces'][0]['endpoints'][1].update(attributes=3),
                lambda d:d['interfaces'][0]['endpoints'][1].update(address=0x82),
                lambda d:d['interfaces'][1]['endpoints'][0].update(address=0x81),
                lambda d:d['interfaces'].append(interface(3,255,0,3))):
            old=self.descriptor; self.descriptor=deepcopy(old)
            update(self.descriptor)
            self.fails(self.find,'invalid_accessory_descriptor')
            self.descriptor=old
        self.id=replace(self.id,pid=0x2d00)
        self.fails(self.find,'invalid_accessory_descriptor')

    def test_adb_must_be_a_separate_present_sibling_of_this_exact_parent(self):
        target=self.find()
        self.cm.add(102,HW1+r'\PrivateADB',200,'WinUSB')
        self.fails(self.find,'adb_not_separate')
        self.fails(target.verify_present,'device_changed')
        self.cm.add(102,r'USB\VID_18D1&PID_2D01&ADB\PrivateADB',100,'WinUSB')
        self.cm.props[102,w.HARDWARE]=()
        self.assertEqual(self.find().target_devinst,101)
        self.cm.add(102,r'USB\VID_9999&PID_8888&ADB\PrivateADB',100,'WinUSB')
        self.fails(self.find,'adb_not_separate')
        self.cm.add(102,r'OEM\ADB\PrivateAlias',100,'WinUSB')
        self.cm.props[102,w.HARDWARE]=(HW0,)
        self.fails(self.find,'adb_not_separate')

    def test_ambiguous_stale_or_changed_target_not_returned_to_installer(self):
        target=self.find()
        self.cm.add(105,HW0+r'\SecondPrivateTarget',100)
        self.cm.props[105,w.HARDWARE]=(HW0,)
        self.fails(self.find,'ambiguous')
        self.fails(target.native_instance_id,'ambiguous')
        del self.cm.nodes[105]
        self.cm.props[100,w.LOCATION]=(r'PCIROOT(0)#USBROOT(0)#USB(7)',)
        self.fails(lambda:target.target_devinst,'device_changed')
        self.cm.props[100,w.LOCATION]=(r'PCIROOT(0)#USBROOT(0)#USB(6)',)
        self.cm.props[101,w.SERVICE]=('NewFunctionDriver',)
        self.fails(target.verify_present,'device_changed')
        self.cm.add(100,r'USB\VID_18D1&PID_2D01\ReusedPrivateParent',service='usbccgp')
        self.fails(target.native_instance_id,'device_changed')


if __name__=='__main__':
    unittest.main()

"""Private mock PnP tree. No system USB calls, real Windows machine or VM."""
import ctypes as C
from dataclasses import asdict, replace
import unittest
from unittest.mock import Mock
from uuid import UUID

from hu_lab.usb import USBIdentity
from hu_lab import windows_usb_interfaces as w


OEM = UUID('bb53e7c9-c0cc-4316-afa9-4ac2e4f9021c')
USB_GENERIC = UUID('a5dcbf10-6530-11d2-901f-00c04fb951ed')
PRIVATE_LINK = r'\\?\OEMENUM#FUNCTION_A#PrivateIdentifier#{bb53e7c9-c0cc-4316-afa9-4ac2e4f9021c}'


class PnP:
    def __init__(self):
        self.nodes = {}
        self.props = {}
        self.registered = {}
        self.requested = []

    def add(self, token, instance, parent=None, service=None):
        self.nodes[token] = (instance,parent)
        self.props[token,w.SERVICE] = () if service is None else (service,)

    def instance(self,node):
        return self.nodes[node][0]

    def locate(self,instance):
        values = [t for t,(s,_) in self.nodes.items() if s.casefold()==instance.casefold()]
        if len(values)!=1: raise w.WindowsInterfaceError('device_changed')
        return values[0]

    def parent(self,node):
        return self.nodes[node][1]

    def children(self,node):
        return tuple(t for t,(_,parent) in self.nodes.items() if parent==node)

    def strings(self,node,prop):
        return self.props.get((node,prop),())

    def classes(self):
        return USB_GENERIC,OEM

    def paths(self,guid,instance):
        node = self.locate(instance)
        self.requested.append((node,guid))
        return self.registered.get((node,guid),())


class WindowsUSBInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.id = USBIdentity(3,(2,7),0x18d1,0x2d01,9,'PrivateSerialHash')
        self.desc = dict(identity=asdict(self.id),windows_devinst=100)
        self.pnp=PnP()
        self.pnp.add(100,r'USB\VID_18D1&PID_2D01\PrivateRootA',service='dg_ssudbus')
        self.pnp.props[100,w.LOCATION]=(r'PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USB(7)',)
        # This other identical product has an accessible WinUSB child and an
        # indistinguishable VID/PID. Its devinst and port are NOT selected.
        self.pnp.add(200,r'USB\VID_18D1&PID_2D01\PrivateRootB',service='usbccgp')
        self.pnp.add(201,r'USB\VID_18D1&PID_2D01&MI_00\OtherPrivateChild',200,'WinUSB')
        self.pnp.registered[201,OEM]=(PRIVATE_LINK.replace('PrivateIdentifier','OtherPrivate'),)

    def find(self):
        return w.find_winusb_candidates(self.id,self.desc,api=self.pnp)

    def reason(self,code,action):
        with self.assertRaises(w.WindowsInterfaceError) as caught: action()
        self.assertEqual(caught.exception.selection_reason,code)
        self.assertNotIn('Private',str(caught.exception))
        self.assertNotIn(str(OEM),str(caught.exception))

    def accessory(self):
        # Custom bridge: the useful grandchild has neither a USB VID/PID nor
        # MI_00 in its name. Exact ancestry and active service determine scope.
        self.pnp.add(101,r'OEMENUM\FUNCTIONBUS\PrivateBus',100,'OEMFunctionBus')
        self.pnp.add(102,r'SAMSUNGANDROID\UNLABELLED\PrivateIdentifier',101,'WinUSB')
        self.pnp.registered[102,OEM]=(PRIVATE_LINK,)

    def test_custom_registered_function_from_exact_selected_parent_only(self):
        self.accessory()
        self.pnp.add(103,r'USB\VID_18D1&PID_2D01&ADB\PrivateAdb',100,'WINUSB')
        self.pnp.registered[103,OEM]=(PRIVATE_LINK.replace('PrivateIdentifier','PrivateAdb'),)
        chosen, = self.find()
        self.assertEqual((chosen.path,chosen.role),(PRIVATE_LINK,'unlabelled'))
        self.assertTrue(chosen.verify_present())
        self.assertNotIn('Private',repr(chosen))
        self.assertNotIn(str(OEM),repr(chosen))
        self.assertEqual({node for node,guid in self.pnp.requested},{102})
        self.assertNotIn(USB_GENERIC,[guid for _,guid in self.pnp.requested])

    def test_adb_without_mi_or_second_interface_is_not_mapped_to_zero(self):
        for hardware,compatible in ((r'USB\VID_18D1&PID_2D01&ADB\PrivateAdb',()),
                (r'USB\VID_18D1&PID_2D01&MI_01\PrivateAdb',()),
                (r'OEM\UNLABELLED\PrivateAdb',(r'USB\Class_FF&SubClass_42&Prot_01',))):
            with self.subTest(hardware=hardware):
                self.pnp.add(103,hardware,100,'WinUSB')
                self.pnp.props[103,w.COMPATIBLE]=compatible
                self.pnp.add(104,r'OEM\HIDDEN_CHILD\Private',103,'WinUSB')
                self.reason('adb_only',self.find)
                self.assertFalse(self.pnp.requested)

    def test_nonzero_other_mi_and_other_physical_device_are_excluded(self):
        self.pnp.add(103,r'USB\VID_18D1&PID_2D01&MI_03\Private',100,'WinUSB')
        self.pnp.add(104,r'USB\VID_18D1&PID_2D01\UnexpectedPhysical',100,'WinUSB')
        self.pnp.add(105,r'USB\VID_1234&PID_5678&MI_00\Unrelated',100,'WinUSB')
        self.reason('no_winusb',self.find)
        self.assertFalse(self.pnp.requested)

    def test_exact_winusb_filter_is_inspectable_and_its_binding_must_remain_current(self):
        self.accessory()
        self.pnp.props[102,w.SERVICE]=('WUDFRd',)
        self.pnp.props[102,w.UPPER_FILTERS]=('NotWinUSB','WinUSBFilter')
        self.reason('no_winusb',self.find)
        for key in (w.UPPER_FILTERS,w.LOWER_FILTERS):
            with self.subTest(property=key):
                self.pnp.props[102,key]=('OtherFilter','wInUsB')
                candidate,=self.find()
                self.assertEqual(candidate.path,PRIVATE_LINK)
                self.assertTrue(candidate.verify_present())
                self.pnp.props[102,key]=()
                self.reason('device_changed',candidate.verify_present)
        self.pnp.add(103,r'USB\VID_18D1&PID_2D01&ADB\Private',100,'OtherDriver')
        self.pnp.props[103,w.UPPER_FILTERS]=('WinUSB',)
        self.reason('adb_only',self.find)

    def test_mi00_and_accessory_only_winusb_physical_node(self):
        self.pnp.add(103,r'USB\VID_18D1&PID_2D01&MI_00\PrivateAdb',100,'WinUSB')
        self.pnp.registered[103,OEM]=(PRIVATE_LINK,)
        self.assertEqual(self.find()[0].role,'mi_00')
        self.id=replace(self.id,pid=0x2d00)
        self.desc.update(identity=asdict(self.id))
        self.pnp.add(100,r'USB\VID_18D1&PID_2D00\PrivateAccessoryOnly',service='WinUSB')
        physical_link = r'\\?\USB#VID_18D1&PID_2D00#PrivatePhysical#{a5dcbf10-6530-11d2-901f-00c04fb951ed}'
        self.pnp.registered[100,USB_GENERIC]=(physical_link,)
        self.pnp.registered[100,OEM]=(PRIVATE_LINK,)
        self.assertEqual([(p.role,p.path) for p in self.find()],[('device',PRIVATE_LINK),('device',physical_link)])
        self.pnp.registered[100,OEM]=()
        generic_only,=self.find()
        self.assertEqual(generic_only.path,physical_link)
        self.assertTrue(generic_only.verify_present())

    def test_missing_devinst_changed_port_or_devnode_identity_has_no_fallback(self):
        initial=dict(self.desc)
        for update,code in (({'windows_devinst':None},'enumeration_failed'),
                ({'identity':asdict(replace(self.id,address=8))},'device_changed'),
                ({'identity':asdict(replace(self.id,ports=(2,5)))},'device_changed')):
            self.desc = {**initial,**update}
            self.reason(code,self.find)
        self.desc=initial
        self.pnp.props[100,w.LOCATION]=(r'PCIROOT(0)#USBROOT(0)#USB(2)#USB(8)',)
        self.reason('device_changed',self.find)
        self.pnp.add(100,r'USB\VID_18D1&PID_2D01&ADB\Private',service='WinUSB')
        self.reason('device_changed',self.find)

    def test_stale_path_service_port_parent_or_reused_inst_is_rejected(self):
        def moved(): self.pnp.add(101,self.pnp.instance(101),200,'OEMFunctionBus')
        def rebound(): self.pnp.props[102,w.SERVICE]=('WUDFRd',)
        def removed(): self.pnp.registered[102,OEM]=()
        def reused(): self.pnp.add(100,r'USB\VID_18D1&PID_2D01\NewPrivateDevice',service='dg_ssudbus')
        def ports(): self.pnp.props[100,w.LOCATION]=(r'PCIROOT(0)#USBROOT(0)#USB(7)',)
        for change in (moved,rebound,removed,reused,ports):
            with self.subTest(change=change.__name__):
                self.setUp()
                self.accessory()
                candidate,=self.find()
                change()
                self.reason('device_changed',candidate.verify_present)

    def test_ambiguous_path_ownership_and_privacy_safe_error(self):
        self.accessory()
        self.pnp.add(103,r'USB\VID_18D1&PID_2D01&MI_00\PrivateDuplicate',100,'WinUSB')
        self.pnp.registered[103,OEM]=(PRIVATE_LINK.upper(),)
        self.reason('ambiguous',self.find)
        self.pnp.paths=Mock(side_effect=OSError(r'C:\Users\PrivateName\secret'))
        self.reason('enumeration_failed',self.find)

    def test_registry_and_multisz_ctypes_contracts(self):
        # Exercise units, GUID byte order and the unplug/replug buffer rescan.
        cm = object.__new__(w._ConfigManager)
        text='WinUSB\0'.encode('utf-16-le')
        def prop(node,key,kind,buf,n,flags):
            self.assertEqual((node,key,flags),(102,w.SERVICE,0))
            kind._obj.value=1; n._obj.value=len(text); C.memmove(buf,text,len(text))
            return 0
        cm._prop=prop
        self.assertEqual(cm.strings(102,w.SERVICE),('WinUSB',))
        def size(n,raw,instance,flags):
            self.assertEqual((bytes(raw),instance,flags),(OEM.bytes_le,'private',0))
            n._obj.value=128; return 0
        retries=[]
        def paths(raw,instance,buf,n,flags):
            retries.append(flags)
            if len(retries)==1: return 0x1a
            buf.value=PRIVATE_LINK; return 0
        cm._size,cm._paths=size,paths
        self.assertEqual(cm.paths(OEM,'private'),(PRIVATE_LINK,))
        self.assertEqual(retries,[0,0])

    def test_registered_class_enumeration_skips_invalid_entries_and_stops_at_no_such_value(self):
        cm=object.__new__(w._ConfigManager)
        def classes(i,raw,flags):
            self.assertEqual(flags,1)  # CM_ENUMERATE_CLASSES_INTERFACE
            if i==0: return 0x1f  # CR_INVALID_DATA: sparse/invalid entry
            if i==1:
                C.memmove(raw,OEM.bytes_le,16)
                return 0
            self.assertEqual(i,2)
            return 0x25  # CR_NO_SUCH_VALUE, documented terminal return
        cm._classes=classes
        self.assertEqual(list(cm.classes()),[OEM])

    def test_rejected_dos_path_and_generic_usb_class_never_become_candidates(self):
        self.accessory()
        self.pnp.registered[102,OEM]=(r'\\?\GLOBALROOT\Device\Private',)
        self.reason('enumeration_failed',self.find)
        self.pnp.registered[102,OEM]=()
        self.pnp.registered[102,USB_GENERIC]=(PRIVATE_LINK,)
        self.reason('no_winusb',self.find)


if __name__=='__main__':
    unittest.main()

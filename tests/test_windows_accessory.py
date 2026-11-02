"""Full automatic selection from a fake Windows device tree and actual pipe descriptors."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hu_lab import windows_usb_interfaces as wi
from hu_lab import windows_winusb as wn
from hu_lab.public import prepare_usb
from tests.test_usb_acquisition import NORMAL, ACCESSORY, MemoryBus, SERIAL
from tests.test_windows_usb_interfaces import PnP, OEM


def link(name):
    return rf'\\?\USBFN#{name}#PrivateFunction#{{{OEM}}}'


class APIs:
    def __init__(self):
        self.bus = MemoryBus()
        self.pnp = PnP()
        self.pnp.add(100, r'USB\VID_18D1&PID_2D01\PhysicalA', service='dg_ssudbus')
        self.pnp.add(11, r'USB\VID_18D1&PID_2D01&ADB\NamedAdb',100,'WinUSB')
        self.pnp.add(12, r'OEMENUM\UNLABELLED\Right',100,'WinUSB')
        self.pnp.add(13, r'OEMENUM\UNLABELLED\DisguisedAdb',100,'WinUSB')
        self.pnp.add(200, r'USB\VID_18D1&PID_2D01\PhysicalB', service='usbccgp')
        self.pnp.add(201, r'USB\VID_18D1&PID_2D01&MI_00\OtherPhone',200,'WinUSB')
        for token,name in ((11,'NamedAdb'),(12,'Right'),(13,'DisguisedAdb'),(201,'OtherPhone')):
            self.pnp.registered[token,OEM]=(link(name),)
        self.opened, self.closed, self.data = [], [], []
        self.fail = None

    def factory(self):
        obj = self.bus.factory()
        obj._verify_winusb_accessory = True
        describe = obj._describe
        def current(identity):
            d = describe(identity)
            d['windows_devinst'] = 100 if identity == ACCESSORY else 10
            d['interfaces'][0]['endpoints'][1]['address'] = 1
            return d
        obj._describe = current
        return obj

    def open_file(self,path):
        self.opened.append(path)
        if self.fail and path == link('Right'): raise self.fail
        return path

    def initialize(self,file): return file
    def free(self,handle): pass
    def close_file(self,file): self.closed.append(file)

    def interface_info(self,handle):
        adb = handle != link('Right')
        n = 2 if adb else 1
        return dict(number=int(adb),alternate=0,usb_class=255,subclass=66 if adb else 255,
                    protocol=1 if adb else 0,pipes=[dict(type=2,address=x,max_packet=512) for x in (n,0x80+n)])

    def descriptor(self,handle,kind,index,lang,capacity):
        assert handle == link('Right'), 'No serial or device descriptor request to ADB'
        if kind == 1:
            d=bytearray(18);d[:2]=bytes((18,1))
            d[8:12]=ACCESSORY.vid.to_bytes(2,'little')+ACCESSORY.pid.to_bytes(2,'little');d[16]=1
            return bytes(d)
        if index == 0:return bytes((4,3,9,4))
        value=SERIAL.decode('ascii').encode('utf-16-le')
        return bytes((len(value)+2,3))+value

    def transfer(self,file,interface,op,pipe,buf,length,deadline,cancel,setup=None):
        assert file == interface == link('Right'), 'Data on the wrong Windows function'
        self.data.append((op,pipe))
        if op == 'read': buf[:2]=b'OK'; return 2
        return length


class AutoSelectionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.api=APIs()
        for p in (patch.object(wi,'_ConfigManager',return_value=self.api.pnp),
                  patch.object(wn,'WinUSBAPI',return_value=self.api),
                  patch('hu_lab.native_usb.usb_lock_directory',return_value=self.root)):
            p.start(); self.addCleanup(p.stop)

    def connect(self, initial=None):
        if initial is None: initial=NORMAL
        self.api.bus.current=initial
        return prepare_usb(initial.physical_key,self.root/'session',aoa_start=True,
                           expected_identity=initial,usb_factory=self.api.factory)()

    def test_after_transition_only_verified_handle_carries_video_commands_and_hid(self):
        stream=self.connect()
        try:
            self.assertEqual(stream.evidence['entry'],'HOST_INITIATED_51_52_53')
            evidence=stream.evidence['windows_selection']
            self.assertEqual(evidence['verified']['number'],0)
            self.assertEqual([x['result'] for x in evidence['examined']], ['adb_interface','selected'])
            self.assertNotIn('PrivateFunction',json.dumps(evidence))
            self.assertEqual(stream.handle.serial_sha256(),hashlib.sha256(SERIAL).hexdigest())
            stream.sendall(b'VERSION')
            self.assertEqual(stream.recv(16384),b'OK')
            stream.handle.hid_control(54,1)
            self.assertEqual(self.api.data,[('write',1),('read',129),('control',0)])
            self.assertEqual(self.api.bus.controls,[51]+[52]*6+[53])
            self.assertEqual(self.api.bus.opens,[NORMAL,NORMAL])  # never re-open AOA with libusb
            self.assertEqual(self.api.opened,[link('DisguisedAdb'),link('Right')])
        finally: stream.close()
        self.assertCountEqual(self.api.closed,self.api.opened)

    def test_already_accessory_preflight_uses_the_same_verified_path(self):
        stream=self.connect(ACCESSORY)
        try:
            self.assertEqual(stream.evidence['entry'],'ALREADY_ACCESSORY')
            self.assertEqual(self.api.bus.controls,[])
            self.assertEqual(self.api.bus.opens,[])
            self.assertEqual(self.api.opened,2*[link('DisguisedAdb'),link('Right')])
        finally: stream.close()

    def test_only_adb_exposed_no_bulk_no_legacy_fallback_no_switch_replay(self):
        for n in (12,13): self.api.pnp.props[n,wi.SERVICE]=('WUDFRd',)
        with self.assertRaises(wi.WindowsInterfaceError) as caught:self.connect()
        d=caught.exception.connection_failure
        self.assertEqual((d['stage'],d['selection_reason'],d['aoa_control_complete']),
                         ('post_aoa_accessory_open','adb_only',True))
        self.assertIsNone(d['attempted_identity'])
        self.assertEqual(d['windows_selection']['expected_in'],129)
        self.assertEqual(self.api.opened,[])
        self.assertEqual(self.api.data,[])
        self.assertEqual(self.api.bus.opens,[NORMAL,NORMAL])
        self.assertEqual(self.api.bus.controls,[51]+[52]*6+[53])

    def test_win32_open_error_survives_export_and_does_not_become_libusb_io(self):
        self.api.fail=wn.WinUSBError('CreateFileW(selected interface)',32)
        with self.assertRaises(RuntimeError) as caught:self.connect(ACCESSORY)
        d=caught.exception.connection_failure
        self.assertEqual((d['selection_reason'],d['winerror'],d['code']),('cannot_open',32,None))
        self.assertEqual(d['attempted_identity']['vid_pid'],'18d1:2d01')
        self.assertNotIn('PrivateFunction',json.dumps(d))
        self.assertFalse(self.api.data)
        self.assertFalse(self.api.bus.opens)

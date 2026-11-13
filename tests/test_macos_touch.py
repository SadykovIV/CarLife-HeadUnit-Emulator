"""Cocoa bridge snapshots only; no real device, UI automation or event tap."""
import unittest
import ctypes
import sys
from unittest.mock import Mock

from hu_lab.macos_touch import MacOSTrackpad, _CocoaTouchAPI, _Point


class MacOSTrackpadTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Objective-C ABI')
    def test_native_point_struct_roundtrip_without_window_or_device(self):
        # Exercise the actual ctypes/objc_msgSend CGPoint argument and return
        # ABI with NSValue, without constructing or injecting input events.
        foundation = ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
        self.assertIsNotNone(foundation)
        api = _CocoaTouchAPI()
        obj = ctypes.c_void_p
        pool = api.send(obj, api.objc.objc_getClass(b'NSAutoreleasePool'), 'new')
        try:
            value = api.send(obj, api.objc.objc_getClass(b'NSValue'), 'valueWithPoint:',
                             (_Point(.25, .75),), (_Point,))
            point = api.send(_Point, value, 'pointValue')
            self.assertEqual((point.x, point.y), (.25, .75))
        finally:
            api.send(None, pool, 'drain')

    def test_identity_y_axis_and_final_positions_from_same_native_event(self):
        api = Mock()
        api.snapshot.return_value = (12.3459, [(90, .8, .1, 2), (12, .2, .75, 4), (44, .4, .3, 8)], False)
        sample = MacOSTrackpad(api).read(12345)
        self.assertEqual(sample.contacts, ((12, .2, .25), (90, .8, .9)))
        self.assertEqual(sample.released, ((44, .4, .7),))
        self.assertFalse(sample.cancelled)

    def test_queued_or_synthetic_qt_event_cannot_borrow_current_native_contacts(self):
        api = Mock()
        api.snapshot.return_value = (12.345, [(1, .5, .5, 1)], False)
        reader = MacOSTrackpad(api)
        self.assertIsNone(reader.read(0))
        api.snapshot.assert_not_called()
        self.assertIsNone(reader.read(15000))
        self.assertIsNone(reader.read(12340))

    def test_os_cancel_invalid_coordinates_and_duplicate_identity_fail_closed(self):
        for rows in ([(1, .5, .5, 16)], [(1, float('nan'), .5, 2)],
                     [(1, 1.1, .5, 2)], [(1, .2, .5, 2), (1, .7, .5, 2)]):
            api = Mock()
            api.snapshot.return_value = (1.0, rows, False)
            self.assertTrue(MacOSTrackpad(api).read(1000).cancelled)

    def test_unavailable_bridge_does_not_repeatedly_load_or_call_it(self):
        api = Mock()
        api.snapshot.side_effect = OSError("unavailable")
        reader = MacOSTrackpad(api)
        self.assertIsNone(reader.read(1000))
        self.assertIsNone(reader.read(1001))
        api.snapshot.assert_called_once()


if __name__ == '__main__':
    unittest.main()

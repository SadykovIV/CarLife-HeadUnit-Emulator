"""Qt driver setup workflow; every USB, helper and connection boundary is mocked."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import dispose_qt_object
from hu_lab.usb import USBIdentity
from hu_lab.windows_driver_setup import SetupPlan

try:
    import av
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from hu_lab import desktop
except ImportError:
    desktop=None


@unittest.skipIf(desktop is None,'PySide6 and PyAV desktop extras are required')
class WindowsDriverSetupWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ident=USBIdentity(3,(6,),0x18d1,0x2d01,4,'private-test-hash')
        rows=[dict(identity=asdict(self.ident), label='Selected accessory',desktop_supported=True)]
        self.windows=self.enter_patch(desktop,'_windows_driver_ui',return_value=True)
        self.discovery=self.enter_patch(desktop,'list_usb_devices',return_value=rows)
        self.inspect=self.enter_patch(desktop,'inspect_setup')
        self.apply=self.enter_patch(desktop,'apply_setup')
        self.connect=self.enter_patch(desktop,'create_client',side_effect=AssertionError('Unexpected connect'))
        self.window=desktop.HeadUnitWindow(Path(self.tmp.name))
        self.window.show()
        self.window.transport.setCurrentIndex(self.window.transport.findData('usb'))
        self.window.tabs.setCurrentIndex(next(i for i in range(self.window.tabs.count())
            if self.window.tabs.tabText(i)=='Подключение'))
        self.wait_for(lambda:self.window.usb_devices.currentData() and self.idle())
        self.discovery.reset_mock()

    def tearDown(self):
        self.window.close()
        self.wait_for(lambda:not self.window.isVisible() and self.idle())
        dispose_qt_object(self.window)

    def enter_patch(self,obj,name,**kwargs):
        p=patch.object(obj,name,**kwargs)
        result=p.start()
        self.addCleanup(p.stop)
        return result

    def idle(self):
        return not self.window.busy and not self.window.pending

    def wait_for(self,predicate,timeout=5):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            self.app.processEvents()
            if predicate(): return
            QTest.qWait(10)
        self.fail('Timed out: '+self.window.driver_status.text())

    def plan(self,action):
        target=None if action=='ready' else SimpleNamespace(hardware_id=r'USB\VID_18D1&PID_2D01&MI_00')
        return SetupPlan(action,self.ident,123,target)

    @contextmanager
    def answer_modal(self,accept):
        # The real QMessageBox.exec() runs its event loop. Select its accept
        # button with a Qt mouse event, or press Escape to take its cancel path.
        real_exec=QMessageBox.exec
        seen=[]
        def execute(dialog):
            button=next(x for x in dialog.buttons() if dialog.buttonRole(x)==QMessageBox.ButtonRole.AcceptRole)
            seen.append(dict(text=dialog.text(),details=dialog.informativeText(),accept=button.text(),
                             default=dialog.defaultButton().text(),escape=dialog.escapeButton().text()))
            if accept:
                QTimer.singleShot(0,lambda:QTest.mouseClick(button,Qt.LeftButton))
            else:
                QTimer.singleShot(0,lambda:QTest.keyClick(dialog,Qt.Key_Escape))
            return real_exec(dialog)
        with patch.object(QMessageBox,'exec',execute):
            yield seen

    def click_setup(self):
        self.assertTrue(self.window.driver_button.isVisible())
        QTest.mouseClick(self.window.driver_button,Qt.LeftButton)
        self.wait_for(lambda:self.inspect.called and self.idle())

    def assert_no_connect(self):
        self.connect.assert_not_called()
        self.assertIsNone(self.window.client)
        self.assertIsNone(self.window.last_run)

    def test_working_device_is_inspected_without_modal_or_elevation(self):
        self.inspect.return_value=self.plan('ready')
        with self.answer_modal(False) as dialogs:
            self.click_setup()
        self.inspect.assert_called_once_with(self.ident)
        self.assertEqual(dialogs,[])
        self.apply.assert_not_called()
        self.discovery.assert_not_called()
        self.assertIn('уже доступен',self.window.driver_status.text())
        self.assert_no_connect()

    def test_cancel_is_default_and_escape_does_not_apply_the_driver(self):
        self.inspect.return_value=self.plan('install')
        with self.answer_modal(False) as dialogs:
            self.click_setup()
        dialog,=dialogs
        self.assertEqual((dialog['default'],dialog['escape']),('Отмена','Отмена'))
        self.assertIn(self.ident.physical_key,dialog['details'])
        self.assertIn(r'USB\VID_18D1&PID_2D01&MI_00',dialog['details'])
        self.assertNotIn(self.ident.serial_sha256,dialog['details'])
        self.assertIn('может быть заменён',dialog['details'])
        self.apply.assert_not_called()
        self.discovery.assert_not_called()
        self.assertIn('Драйверы не изменены',self.window.driver_status.text())
        self.assert_no_connect()

    def test_approval_applies_displayed_target_only_then_reports_verified_result(self):
        plan=self.plan('install'); self.inspect.return_value=plan
        self.apply.return_value='ready'
        with self.answer_modal(True) as dialogs:
            self.click_setup()
        self.assertEqual([d['accept'] for d in dialogs],['Назначить WinUSB'])
        self.assertIs(self.apply.call_args.args[0],plan)
        self.apply.assert_called_once()
        self.inspect.assert_called_once_with(self.ident)
        self.discovery.assert_called_once_with()
        self.assertIn('Интерфейс CarLife проверен',self.window.driver_status.text())
        self.assertTrue(self.window.connect_button.isEnabled())
        self.assert_no_connect()

    def test_elevated_helper_cancel_after_registration_approval_never_connects(self):
        plan=self.plan('register'); self.inspect.return_value=plan
        self.apply.return_value='cancelled'
        with self.answer_modal(True) as dialogs:
            self.click_setup()
        dialog,=dialogs
        self.assertEqual(dialog['accept'],'Зарегистрировать')
        self.assertIn('WinUSB уже назначен',dialog['details'])
        self.assertIs(self.apply.call_args.args[0],plan)
        self.apply.assert_called_once()
        self.assertIn('отменена',self.window.driver_status.text())
        self.discovery.assert_not_called()
        self.assert_no_connect()

    def test_busy_active_non_windows_and_other_transport_guards_do_not_start_setup(self):
        entered,release=threading.Event(),threading.Event()
        def inspection(_):
            entered.set()
            if not release.wait(5): raise AssertionError('Test did not release driver inspection')
            return self.plan('ready')
        self.inspect.side_effect=inspection
        try:
            QTest.mouseClick(self.window.driver_button,Qt.LeftButton)
            self.wait_for(entered.is_set)
            self.assertFalse(self.window.driver_button.isEnabled())
            self.window.setup_windows_driver()  # an invocation while the worker is pending
            self.assertEqual(len(self.window.pending),1)
        finally:
            release.set()
        self.wait_for(self.idle)
        self.inspect.assert_called_once()
        self.inspect.reset_mock()
        self.windows.return_value=False
        self.window.transport_changed()
        self.assertFalse(self.window.driver_group.isVisible())
        self.window.setup_windows_driver()
        self.windows.return_value=True
        self.window.transport.setCurrentIndex(self.window.transport.findData('tcp'))
        self.window.setup_windows_driver()
        self.window.transport.setCurrentIndex(self.window.transport.findData('usb'))
        self.window.client=object()  # established session guard; process no events while substituted
        try: self.window.setup_windows_driver()
        finally: self.window.client=None
        self.inspect.assert_not_called()
        self.apply.assert_not_called()
        self.assert_no_connect()


if __name__=='__main__':
    unittest.main()

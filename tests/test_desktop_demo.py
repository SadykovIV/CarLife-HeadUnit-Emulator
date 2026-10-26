"""Demo socket shutdown classification without network or platform dependencies."""
import errno
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from hu_lab.desktop_demo import DemoPeer


def abort_errors():
    plain_windows = OSError('Windows established connection aborted')
    plain_windows.winerror = 10053
    plain_errno = OSError('Established connection aborted')
    plain_errno.errno = errno.ECONNABORTED
    return (ConnectionAbortedError('Established connection aborted'),
            plain_windows, plain_errno)


class DemoShutdownTests(unittest.TestCase):
    def run_failure(self, error, *, closing=False, previous_error=None):
        entered, release = threading.Event(), threading.Event()
        listener, connection = Mock(), Mock()
        listener.accept.return_value = (connection, ('127.0.0.1', 12345))
        listener.getsockname.return_value = ('127.0.0.1', 12346)

        class FailingPeer(DemoPeer):
            def _session(self):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('Test did not release the peer')
                raise error

        with patch('hu_lab.desktop_demo.socket.socket', return_value=listener):
            peer = FailingPeer()
        try:
            self.assertTrue(entered.wait(2))
            peer.error = previous_error
            if closing:
                peer.begin_close()
                # The client still owns the order of final controls/TCP close.
                connection.shutdown.assert_not_called()
                connection.close.assert_not_called()
            release.set()
            peer.thread.join(2)
            self.assertFalse(peer.thread.is_alive())
            return peer.error
        finally:
            release.set()
            peer.close()
            connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_abort_is_expected_only_after_explicit_shutdown_begins(self):
        for error in abort_errors():
            with self.subTest(error_type=type(error).__name__, attributes=error.__dict__):
                self.assertIsNone(self.run_failure(error, closing=True))
                self.assertEqual(self.run_failure(error), str(error))

    def test_unrelated_io_errors_remain_visible_during_shutdown(self):
        for closing in (False, True):
            with self.subTest(closing=closing):
                error = OSError(errno.EIO, 'Synthetic I/O failure')
                self.assertEqual(self.run_failure(error, closing=closing), str(error))

    def test_runtime_errors_remain_visible_during_shutdown(self):
        for closing in (False, True):
            with self.subTest(closing=closing):
                self.assertEqual(self.run_failure(RuntimeError('Encoder failed'), closing=closing),
                                 'RuntimeError: Encoder failed')

    def test_begin_close_does_not_erase_an_existing_error(self):
        self.assertEqual(self.run_failure(abort_errors()[0], closing=True,
                                         previous_error='Earlier runtime failure'),
                         'Earlier runtime failure')


if __name__ == '__main__':
    unittest.main()

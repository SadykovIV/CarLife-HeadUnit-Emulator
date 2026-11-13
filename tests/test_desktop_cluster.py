import io
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PIL import Image
    from PySide6.QtWidgets import QApplication, QFrame, QWidget
    from hu_lab.desktop_cluster import ClusterPage, artwork_pixmap
    from hu_lab.desktop_theme import stylesheet
except ImportError:
    ClusterPage = None

from hu_lab.cluster import (ClusterState, MAX_ARTWORK_BYTES, MEDIA_INFO, MEDIA_PROGRESS_BAR,
                            MODULE_STATUS, NAVI_NEXT_TURN_INFO, SUBSCRIBE_DONE, SUBSCRIBE_START)
from hu_lab.desktop_demo import DemoCluster
from hu_lab.desktop_session import DesktopV2Session
from hu_lab.honda_reference import list_body
from hu_lab.protocol import CMD, FrameParser, Packet, protobuf
from tests.test_cluster import handshake


@unittest.skipIf(ClusterPage is None, 'desktop extras required')
class ClusterDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def cover(image_format='JPEG', size=(256, 256), color='#257f9e'):
        encoded = io.BytesIO()
        Image.new('RGB', size, color).save(encoded, image_format)
        return encoded.getvalue()

    def test_wire_jpeg_cover_is_rendered_and_progress_does_not_decode_again(self):
        session = DesktopV2Session()
        handshake(session)
        session.receive(Packet(CMD, SUBSCRIBE_DONE, list_body([dict(f1=2, f2=1)])))
        page = ClusterPage(lambda: None)
        self.addCleanup(page.close)
        blob = self.cover()
        # Same complete proto2 schema and binary JPEG encoding as
        # HeadSwap Msgs.mediaInfo / MetaImages.albumJpeg (not a text/base64 mock).
        frame = Packet(CMD, MEDIA_INFO, protobuf(f1='Android#1', f2='Тестовый трек',
            f3='Исполнитель', f4='Альбом', f5=blob, f6=180, f7=1, f8='', f9=0)).encode()
        parser = FrameParser()
        for offset in range(0, len(frame), 127):
            for packet in parser.feed(frame[offset:offset + 127]):
                session.receive(packet)
        parser.eof()
        self.assertEqual(session.cluster.snapshot()['music']['artwork'], blob)
        with patch('hu_lab.desktop_cluster.artwork_pixmap', wraps=artwork_pixmap) as decode:
            page.refresh(session.cluster, demo=False)
            self.assertFalse(page.artwork.isHidden())
            self.assertEqual(page.artwork.pixmap().size().toTuple(), (64, 64))
            self.assertEqual(page.song.text(), 'Тестовый трек')
            old_pixels = page.artwork.pixmap().cacheKey()
            session.receive(Packet(CMD, MEDIA_PROGRESS_BAR, protobuf(f1=1000)))
            page.refresh(session.cluster, demo=False)
            self.assertEqual(page.artwork.pixmap().cacheKey(), old_pixels)
            self.assertEqual(decode.call_count, 1)
        # A fresh metadata packet replaces art, even when track text is equal.
        session.receive(Packet(CMD, MEDIA_INFO, protobuf(f2='Тестовый трек',
            f5=self.cover('PNG', (256, 128), '#b65439'))))
        page.refresh(session.cluster, demo=False)
        self.assertEqual(page.artwork.pixmap().size().toTuple(), (64, 32))
        self.assertNotEqual(page.artwork.pixmap().cacheKey(), old_pixels)

    def test_artwork_clears_on_missing_invalid_idle_stop_and_disconnect(self):
        page = ClusterPage(lambda: None)
        self.addCleanup(page.close)
        for end in ('missing', 'empty', 'invalid', 'oversized', 'idle', 'stop', 'disconnect'):
            with self.subTest(end=end):
                state = ClusterState()
                state.requested_once()
                state.acknowledge(list_body([dict(f1=2, f2=1)]))
                state.consume(Packet(CMD, MEDIA_INFO, protobuf(f2='First', f5=self.cover())))
                page.refresh(state, demo=False)
                self.assertFalse(page.artwork.isHidden())
                if end == 'idle':
                    state.consume(Packet(CMD, MODULE_STATUS, list_body([dict(f1=3, f2=0)])))
                elif end == 'stop':
                    state.close()
                elif end == 'disconnect':
                    state = None
                else:
                    body = protobuf(f2='Next')
                    if end != 'missing':
                        blob = {'empty': b'', 'invalid': b'\xff\xd8\xffbad',
                                'oversized': b'x' * (MAX_ARTWORK_BYTES + 1)}[end]
                        body += protobuf(f5=blob)
                    state.consume(Packet(CMD, MEDIA_INFO, body))
                page.refresh(state, demo=False)
                self.assertTrue(page.artwork.isHidden())
                self.assertTrue(page.artwork.pixmap().isNull())
                if end in ('missing', 'empty', 'invalid', 'oversized'):
                    self.assertEqual(page.song.text(), 'Next')

    def test_artwork_decoder_limits_bytes_dimensions_and_formats(self):
        for image_format in ('JPEG', 'PNG'):
            with self.subTest(image_format=image_format):
                self.assertIsNotNone(artwork_pixmap(self.cover(image_format, (1024, 1))))
                self.assertIsNone(artwork_pixmap(self.cover(image_format, (1025, 1))))
                self.assertIsNone(artwork_pixmap(self.cover(image_format, (1, 1025))))
        for blob in (None, b'', b'file:///tmp/cover.jpg', b'https://example.com/cover.jpg',
                     b'<svg width="64" height="64"></svg>', b'\x89PNG\r\n\x1a\n',
                     self.cover() + b'\0' * MAX_ARTWORK_BYTES):
            self.assertIsNone(artwork_pixmap(blob))

    def test_peer_icon_unknown_turn_zero_and_hidden_values(self):
        page = ClusterPage(lambda: None)
        self.addCleanup(page.close)
        state = ClusterState()
        state.requested_once()
        state.acknowledge(list_body([dict(f1=n, f2=1) for n in (0, 1, 2)]))
        icon = io.BytesIO()
        Image.new('RGBA', (32, 32), 'white').save(icon, 'PNG')
        body = protobuf(f1=1, f2=999, f3='<b>улица</b>', f4=1200, f5=0, f6=0)
        state.consume(Packet(CMD, NAVI_NEXT_TURN_INFO, body + protobuf(f6=icon.getvalue())))
        page.refresh(state, demo=False)
        self.assertFalse(page.arrow.pixmap().isNull())
        self.assertEqual(page.maneuver.text(), 'Манёвр 999')
        self.assertEqual(page.distance.text(), '0 м')
        self.assertEqual(page.road.text(), '<b>улица</b>')
        state.consume(Packet(CMD, NAVI_NEXT_TURN_INFO, body + protobuf(f6=b'bad')))
        page.refresh(state, demo=False)
        self.assertEqual(page.arrow.text(), '?')
        state.consume(Packet(CMD, NAVI_NEXT_TURN_INFO, protobuf(f1=3)))
        page.refresh(state, demo=False)
        self.assertEqual(page.distance.text(), '—')
        self.assertEqual(page.arrow.text(), '—')

    def test_cards_fit_inspector_and_keep_long_peer_text_in_both_themes(self):
        state = ClusterState()
        state.requested_once()
        state.acknowledge(list_body([dict(f1=n, f2=1) for n in (0, 2)]))
        road = '<b>Улица</b> ' + 'Набережная' * 20
        song = 'Название трека ' + 'безпробелов' * 20
        state.consume(Packet(CMD, NAVI_NEXT_TURN_INFO, protobuf(f1=1, f2=3, f3=road, f5=350)))
        state.consume(Packet(CMD, MEDIA_INFO, protobuf(f2=song, f3='Исполнитель', f4='Альбом', f5=self.cover())))
        page = ClusterPage(lambda: None)
        # Production embeds this page in the inspector. Native window managers
        # can impose a larger minimum width on a standalone decorated window.
        inspector = QWidget()
        inspector.resize(800, 600)
        page.setParent(inspector)
        self.addCleanup(inspector.close)
        page.refresh(state, demo=False)
        inspector.show()
        page.show()
        for theme in ('light', 'dark'):
            page.setStyleSheet(stylesheet(theme))
            # 300/420 px inspector, less outside margins and a possible scrollbar.
            for width in (250, 370):
                with self.subTest(theme=theme, width=width):
                    page.resize(width, page.heightForWidth(width))
                    self.app.processEvents()
                    self.assertEqual(page.width(), width)
                    nav, music = page.findChildren(QFrame, 'clusterCard')
                    self.assertLess(nav.geometry().bottom(), music.y())
                    self.assertEqual((nav.width(), music.width()), (width, width))
                    self.assertLess(page.song.width(), page.song.fontMetrics().horizontalAdvance(song))
                    self.assertFalse(page.artwork.isHidden())
                    self.assertLess(page.artwork.geometry().right(), page.song.x())
                    self.assertEqual(page.song.text(), song)
                    self.assertEqual(page.road.text(), road)
                    self.assertIn('&lt;b&gt;', page.road.toolTip())
                    self.assertFalse(page.grab().isNull())

    def test_demo_idle_and_subscription_states_clear_hidden_values(self):
        started = []
        page = ClusterPage(lambda: started.append(True))
        self.addCleanup(page.close)
        page.show()
        self.assertTrue(page.demo_button.isVisible())
        page.demo_button.click()
        self.assertEqual(started, [True])
        state = ClusterState()
        state.requested_once()
        page.refresh(state, demo=True)
        self.assertEqual(page.source.text(), 'Демо')
        self.assertTrue(page.demo_button.isHidden())
        self.assertEqual(page.song.text(), 'Ожидание ответа телефона')
        state.acknowledge(list_body([dict(f1=0, f2=0), dict(f1=2, f2=1)]))
        page.refresh(state, demo=False)
        self.assertEqual(page.source.text(), 'CarLife')
        self.assertEqual(page.maneuver.text(), 'Подписка не поддерживается')
        self.assertEqual(page.song.text(), 'Ожидание трека')
        self.assertTrue(all(widget.isHidden() for widget in (page.arrow, page.road, page.artist, page.album)))
        page.refresh(None, demo=False)
        self.assertEqual(page.source.text(), 'Отключено')
        self.assertTrue(page.demo_button.isVisible())

    def test_demo_publishes_new_maneuvers_and_tracks_only_after_start(self):
        peer = DemoCluster()
        self.assertFalse(peer.updates(0))
        ids = list_body([dict(f1=n) for n in (0, 2)])
        peer.command(Packet(CMD, SUBSCRIBE_START, ids))
        state = ClusterState()
        state.requested_once()
        state.acknowledge(list_body([dict(f1=n, f2=1) for n in (0, 1, 2)]))
        for t, expected in ((0,(3,350)), (4,(3,0)), (5,(7,120)), (12,(1,750))):
            for packet in peer.updates(t):
                state.consume(packet)
            data = state.snapshot()
            self.assertEqual((data['navigation']['turn_id'], data['navigation']['distance_m']), expected)
            self.assertEqual(data['music']['title'], 'За поворотом' if t==12 else 'Ночной рейс')
            self.assertIsNotNone(artwork_pixmap(data['music']['artwork']))
        for packet in peer.updates(15):
            state.consume(packet)
        self.assertIsNone(state.snapshot()['navigation'])

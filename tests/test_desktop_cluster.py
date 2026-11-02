import io
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PIL import Image
    from PySide6.QtWidgets import QApplication, QFrame
    from hu_lab.desktop_cluster import ClusterPage
    from hu_lab.desktop_theme import stylesheet
except ImportError:
    ClusterPage = None

from hu_lab.cluster import ClusterState, MEDIA_INFO, NAVI_NEXT_TURN_INFO, SUBSCRIBE_START
from hu_lab.desktop_demo import DemoCluster
from hu_lab.honda_reference import list_body
from hu_lab.protocol import CMD, Packet, protobuf


@unittest.skipIf(ClusterPage is None, 'desktop extras required')
class ClusterDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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
        state.consume(Packet(CMD, MEDIA_INFO, protobuf(f2=song, f3='Исполнитель', f4='Альбом')))
        page = ClusterPage(lambda: None)
        self.addCleanup(page.close)
        page.refresh(state, demo=False)
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
        for packet in peer.updates(15):
            state.consume(packet)
        self.assertIsNone(state.snapshot()['navigation'])

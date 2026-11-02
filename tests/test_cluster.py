"""Real desktop session/client dispatch with synthetic CarLife frames, no USB or ADB."""
from pathlib import Path
import tempfile
import unittest

from hu_lab.cluster import (ClusterState, MODULE_STATUS, NAVI_NEXT_TURN_INFO, MEDIA_INFO,
    MEDIA_PROGRESS_BAR, SUBSCRIBE, SUBSCRIBE_DONE, SUBSCRIBE_START, SUBSCRIBE_STOP)
from hu_lab.desktop_session import DesktopConfig, DesktopV1Session, DesktopV2Session, create_client
from hu_lab.honda_reference import MD_FEATURE_REQUEST, list_body, repeated_entries
from hu_lab.protocol import CMD, VERSION_MATCH, VIDEO_INIT_DONE, VIDEO_START, Packet, protobuf


def cmd(msg,body): return Packet(CMD,msg,body)


def subscriptions(packets):
    return [(p.message,[e[1] for e in repeated_entries(p.payload)]) for p in packets]


def handshake(s):
    s.start()
    s.receive(cmd(VERSION_MATCH,protobuf(f1=1)))
    if isinstance(s,DesktopV2Session): s.receive(cmd(MD_FEATURE_REQUEST,b''))
    return s.receive(cmd(VIDEO_INIT_DONE,protobuf(f1=s.width,f2=s.height,f3=s.fps)))


class ClusterTests(unittest.TestCase):
    def test_v1_and_v2_negotiate_once_start_only_confirmed_stop_once(self):
        for cls in (DesktopV1Session,DesktopV2Session):
            with self.subTest(cls=cls):
                s=cls()
                out=handshake(s)
                self.assertIn(VIDEO_START,[p.message for p in out])
                self.assertEqual(subscriptions([p for p in out if p.message==SUBSCRIBE]),[(SUBSCRIBE,[0,1,2])])
                self.assertFalse(s.receive(cmd(VIDEO_INIT_DONE,protobuf(f1=1280,f2=720,f3=30))))
                done=cmd(SUBSCRIBE_DONE,list_body([dict(f1=2,f2=1),dict(f1=1,f2=1),dict(f1=0,f2=0)]))
                self.assertEqual(subscriptions(s.receive(done)),[(SUBSCRIBE_START,[2])])
                self.assertEqual(s.receive(done),[])
                self.assertEqual(s.cluster.snapshot()['subscriptions']['navigation']['state'],'unsupported')
                self.assertEqual(subscriptions(s.cluster.close()),[(SUBSCRIBE_STOP,[2])])
                self.assertFalse(s.cluster.close())
                self.assertEqual(s.cluster.snapshot()['subscriptions']['music']['state'],'stopped')

    def test_partial_ack_and_route_rearm_do_not_duplicate_start_or_poison_stream(self):
        s=DesktopV2Session(); handshake(s)
        bad=[b'\x80',list_body([dict(f1=0,f2=2)]),list_body([dict(f1=0,f2=0),dict(f1=0,f2=1)])]
        for body in bad: self.assertFalse(s.receive(cmd(SUBSCRIBE_DONE,body)))
        self.assertEqual(s.cluster.snapshot()['invalid_packets'],len(bad))
        subset=cmd(SUBSCRIBE_DONE,list_body([dict(f1=0,f2=1),dict(f1=99,f2=1)]))
        self.assertEqual(subscriptions(s.receive(subset)),[(SUBSCRIBE_START,[0])])
        self.assertEqual(s.cluster.snapshot()['subscriptions']['music']['state'],'requested')
        run=cmd(MODULE_STATUS,list_body([dict(f1=2,f2=1)]))
        self.assertEqual(subscriptions(s.receive(run)),[(SUBSCRIBE_START,[0])])
        self.assertFalse(s.receive(run))
        self.assertFalse(s.receive(cmd(MODULE_STATUS,list_body([dict(f1=2,f2=0)]))))
        self.assertEqual(subscriptions(s.receive(run)),[(SUBSCRIBE_START,[0])])
        self.assertEqual(subscriptions(s.receive(cmd(SUBSCRIBE_DONE,list_body([dict(f1=2,f2=1)])))),[(SUBSCRIBE_START,[2])])
        self.assertEqual(subscriptions(s.receive(cmd(SUBSCRIBE_DONE,list_body([dict(f1=0,f2=0)])))),[(SUBSCRIBE_STOP,[0])])
        self.assertEqual(s.state,'STREAMING')

    def test_next_turn_preserves_both_field_six_wire_types_and_clears_stale_data(self):
        s=DesktopV1Session(); handshake(s)
        s.receive(cmd(SUBSCRIBE_DONE,list_body([dict(f1=0,f2=1)])))
        icon=b'\x89PNG\r\n\x1a\nsynthetic'
        for field6 in (protobuf(f6=12)+protobuf(f6=icon),protobuf(f6=icon)+protobuf(f6=12)):
            s.receive(cmd(NAVI_NEXT_TURN_INFO,protobuf(f1=1,f2=7,f3='Улица   ',f4=620,f5=600)+field6))
            nav=s.cluster.snapshot()['navigation']
            self.assertEqual((nav['turn_id'],nav['road'],nav['distance_m'],nav['total_distance_m'],nav['time'],nav['icon']),
                             (7,'Улица',600,620,12,icon))
        s.pause()
        s.receive(cmd(NAVI_NEXT_TURN_INFO,protobuf(f1=2,f2=3,f3='',f4=250,f5=200,f6=0)))
        self.assertEqual((s.cluster.snapshot()['navigation']['distance_m'],s.state),(200,'PAUSED'))
        old=s.cluster.snapshot(); old['navigation']['distance_m']=0
        self.assertEqual(s.cluster.snapshot()['navigation']['distance_m'],200)
        # Invalid optional metadata preserves the last good reading and video.
        s.receive(cmd(NAVI_NEXT_TURN_INFO,b'\x08\x80'))
        s.receive(cmd(NAVI_NEXT_TURN_INFO,protobuf(f1=9,f2=1)))
        self.assertEqual(s.cluster.snapshot()['invalid_packets'],2)
        self.assertEqual(s.cluster.snapshot()['navigation']['distance_m'],200)
        s.receive(cmd(NAVI_NEXT_TURN_INFO,protobuf(f1=3)))
        self.assertIsNone(s.cluster.snapshot()['navigation'])
        s.cluster.close()
        s.receive(cmd(NAVI_NEXT_TURN_INFO,protobuf(f1=1,f2=7,f5=100)))
        self.assertIsNone(s.cluster.snapshot()['navigation'])

    def test_music_art_progress_and_module_idle(self):
        s=DesktopV2Session(); handshake(s)
        s.receive(cmd(SUBSCRIBE_DONE,list_body([dict(f1=2,f2=1)])))
        body=protobuf(f1='Player',f2='Track',f3='Artist',f4='Album',f5=b'jpeg',f6=162,f7=4,f8='id',f9=0)
        s.receive(cmd(MEDIA_INFO,body)); s.receive(cmd(MEDIA_PROGRESS_BAR,protobuf(f1=12345)))
        music=s.cluster.snapshot()['music']
        self.assertEqual((music['title'],music['artist'],music['album'],music['source'],music['artwork'],music['duration_s'],music['position_ms']),
                         ('Track','Artist','Album','Player',b'jpeg',162,12345))
        s.receive(cmd(MEDIA_INFO,protobuf(f1='',f2='New track',f5=b'x'*33000,f6=0)))
        self.assertIsNone(s.cluster.snapshot()['music']['position_ms'])
        self.assertEqual(s.cluster.snapshot()['music']['artwork'],b'')
        s.receive(cmd(MODULE_STATUS,list_body([dict(f1=3,f2=0)])))
        s.receive(cmd(MEDIA_PROGRESS_BAR,protobuf(f1=144)))
        self.assertIsNone(s.cluster.snapshot()['music'])
        self.assertEqual(s.cluster.snapshot()['subscriptions']['music']['module_status'],0)

    def test_real_huclient_command_dispatch_and_session_reset_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            clients=[]
            try:
                for profile in ('v1','honda-v2'):
                    c=create_client(DesktopConfig(profile=profile),Path(root)/profile)
                    clients.append(c)
                    self.assertIsNone(c.session.cluster.snapshot()['navigation'])
                    # Drive the same dispatch path as the framed reader. No
                    # socket, USB handle or codec worker is created in this test.
                    c.session.start()
                    c._receive_packet(cmd(VERSION_MATCH,protobuf(f1=1)),None)
                    if profile=='honda-v2':
                        c._receive_packet(cmd(MD_FEATURE_REQUEST,b''),None)
                    c._receive_packet(cmd(VIDEO_INIT_DONE,protobuf(f1=1280,f2=720,f3=30)),None)
                    c._receive_packet(cmd(SUBSCRIBE_DONE,list_body([dict(f1=0,f2=1)])),None)
                    c._receive_packet(cmd(NAVI_NEXT_TURN_INFO,protobuf(f1=1,f2=7,f5=600)),None)
                    self.assertEqual(c.session.cluster.snapshot()['navigation']['distance_m'],600)
                    self.assertIn(SUBSCRIBE_START,[p.message for p in c.commands.queue])
                    c.halt.set(); c.stop()
                    self.assertIsNone(c.session.cluster.snapshot()['navigation'])
            finally:
                for c in clients: c.halt.set(); c.stop()


if __name__=='__main__':
    unittest.main()

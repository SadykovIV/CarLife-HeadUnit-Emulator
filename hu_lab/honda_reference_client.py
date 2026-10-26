"""V2 reference client; keyframe RESET preserves decoder references.

Session selection is injected per client, without module-global bindings.
"""
from . import client as client_module
from .protocol import VIDEO, VIDEO_DATA


class HondaReferenceClient(client_module.HUClient):
    def _receive_packet(self, packet, video, received_ns=None):
        with self.state_lock:
            if (packet.channel == VIDEO and packet.message == VIDEO_DATA and not packet.payload
                    and self.session.state in ("STREAMING", "PAUSED")):
                # PAUSE keepalive can remain in the endpoint when START resumes video.
                # It is a protocol heartbeat, never an AVC AU or an encoder PTS sample.
                self._check_native_handshake_deadline()
                self.session.receive(packet)
                self.reference_empty_video_heartbeats = getattr(self, "reference_empty_video_heartbeats", 0) + 1
                self.trace.event("reference_empty_video_heartbeat", state=self.session.state,
                                 timestamp=packet.timestamp, generation=self.decoder_generation,
                                 total=self.reference_empty_video_heartbeats, encoded_access_unit=False)
                return
        return super()._receive_packet(packet, video, received_ns)

    def summary(self):
        result = super().summary()
        result["reference_empty_video_heartbeats"] = getattr(self, "reference_empty_video_heartbeats", 0)
        return result

    def reset(self):
        with self.state_lock:
            if self.pending_touch:
                self.cancel_touch('reference_keyframe_reset')
            for packet in self.session.reset():
                self.send(packet)
            self.trace.event('reference_keyframe_reset_requested',
                             generation=self.decoder_generation,
                             decoder_references_preserved=True,
                             init_sent=False)

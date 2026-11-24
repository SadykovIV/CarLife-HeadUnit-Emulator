"""HiSight RTSP session setup."""
import re
import time
from .cipher import Cipher
from .control_wire import Messages
from .nearby_wire import ProtocolError


def message(start, sequence, body=b'', headers=None):
    if not re.fullmatch(r'[0-9]{1,9}', str(sequence)):
        raise ProtocolError('invalid HiSight sequence')
    rows = [start, 'CSeq: ' + str(sequence)]
    for name, value in (headers or {}).items():
        if any(c in name + str(value) for c in '\r\n\0'):
            raise ProtocolError('invalid HiSight header')
        rows.append(name + ': ' + str(value))
    if body:
        rows.extend(['Content-Type: text/parameters', 'Content-Length: ' + str(len(body))])
    return ('\r\n'.join(rows) + '\r\n\r\n').encode('ascii') + body


class Session:
    def __init__(self, key, width, height, fps):
        self.width, self.height, self.fps = width, height, fps
        self.cipher = Cipher(key, 65001)
        self.encrypted = False
        self.buffer = bytearray()
        self.messages = Messages('RTSP')
        self.pending = []
        self.options_sent = False
        self.target = '*'
        self.media_endpoint = None
        self.input_endpoint = None
        self.setup_sequence = None
        self.sequence = 2
        self.on_plaintext = None
        self.peer_teardown = False
        self.teardown_sequence = None
        self.teardown_acknowledged = False

    def teardown(self):
        if (not self.encrypted or not self.media_endpoint or self.peer_teardown
                or self.teardown_sequence is not None):
            return None
        self.sequence += 1
        self.teardown_sequence = self.sequence
        return self.pack(message('TEARDOWN ' + self.target + ' RTSP/1.0', self.sequence))

    def request(self, method, headers=None):
        self.sequence += 1
        self.pending.append(self.pack(message(method + ' ' + self.target + ' RTSP/1.0',
                                              self.sequence, headers=headers)))
        return self.sequence

    def pack(self, payload, plain=False):
        if self.encrypted and not plain:
            payload = self.cipher.encrypt(payload)
        return len(payload).to_bytes(4, 'big') + payload

    def initial(self):
        return self.pack(message('ANNOUNCE * RTSP/1.0', 1,
            b'encrypt_description: encrypt_list=aes128ctr;version=1\r\n'), plain=True)

    def close(self):
        self.cipher.close()

    def receive(self, data):
        self.buffer.extend(data)
        events, plaintexts = [], []
        while len(self.buffer) >= 4:
            size = int.from_bytes(self.buffer[:4], 'big')
            if not 0 < size <= 65000:
                raise ProtocolError('invalid HiSight record length')
            if len(self.buffer) < size + 4:
                break
            payload = bytes(self.buffer[4:size + 4])
            del self.buffer[:size + 4]
            if self.encrypted:
                payload = self.cipher.decrypt(payload)
            plaintexts.append(payload)
            if self.on_plaintext:
                self.on_plaintext(payload)
            head, separator, body = payload.partition(b'\r\n\r\n')
            if not separator:
                # SETUP responses from the observed source end with only one
                # CRLF; their four-byte record boundary terminates headers.
                head, separator, body = payload.rstrip(b'\r\n'), b'\r\n\r\n', b''
            if separator:
                # Some sources count the Transport header in Content-Length.
                rows = [row for row in head.split(b'\r\n')
                        if row and not row.lower().startswith(b'content-length:')]
                payload = (b'\r\n'.join(rows) + b'\r\nContent-Length: '
                           + str(len(body)).encode() + separator + body)
            decoder = Messages('RTSP')
            messages = decoder.feed(payload)
            if decoder.buffer.strip(b'\r\n'):
                raise ProtocolError('incomplete HiSight message')
            for m in messages:
                events.append(m.metadata())
                seq = m.headers.get('cseq', '')
                method = m.start.split()[0]
                if method == 'ANNOUNCE':
                    if self.encrypted or b'aes128ctr' not in m.body:
                        raise ProtocolError('unsupported HiSight encryption negotiation')
                    self.pending.append(self.pack(message('RTSP/1.0 200 OK', seq), plain=True))
                    self.encrypted = True
                    events.append(dict(hisight_encryption=True))
                elif method == 'OPTIONS':
                    self.pending.append(self.pack(message('RTSP/1.0 200 OK', seq, headers={
                        'Public': 'hisight1.0, GET_PARAMETER, SET_PARAMETER'})))
                    if not self.options_sent:
                        self.pending.append(self.pack(message('OPTIONS * RTSP/1.0', 2,
                                                              headers={'Require': 'hisight1.0'})))
                        self.options_sent = True
                elif method == 'GET_PARAMETER':
                    requested = {line.partition(b':')[0].strip().decode('ascii')
                                 for line in m.body.splitlines() if line.strip()}
                    capabilities = {
                        'his_version': '1.0',
                        'his_video_formats': (f'codecs 1;fps {self.fps};gop {self.fps};bitrate 5000000;'
                            f'vbr-min 1000000;vbr-max 8000000;dpi 160;scr-w {self.width};scr-h {self.height};width {self.width};height {self.height}'),
                        'his_audio_codecs': '1',
                        'his_device_type': 'device_type 4;subtype 0;',
                        'his_uibc_capability': ('input_category_list=GENERIC;generic_cap_list='
                            'left_point_down, left_point_up, left_move, left_key_down, left_key_up;'),
                    }
                    body = ''.join(k + ': ' + v + '\r\n' for k, v in capabilities.items()
                                   if k in requested).encode('ascii')
                    self.pending.append(self.pack(message('RTSP/1.0 200 OK', seq, body)))
                    events.append(dict(capabilities_replied=True, requested=sorted(requested)))
                elif method == 'SET_PARAMETER':
                    parameters = {}
                    for line in m.body.decode('ascii').splitlines():
                        name, sep, value = line.partition(':')
                        if sep:
                            parameters[name.strip().lower()] = value.strip()
                    events.append(dict(parameter_names=sorted(parameters)))
                    self.pending.append(self.pack(message('RTSP/1.0 200 OK', seq)))
                    if 'his_presentation_url' in parameters:
                        target = parameters['his_presentation_url'].split()[0]
                        if not re.fullmatch(r'rtsp://[A-Za-z0-9.:/_?=&%-]{1,256}', target):
                            raise ProtocolError('invalid HiSight presentation URL')
                        self.target = target
                    if parameters.get('his_trigger_method') == 'SETUP':
                        self.setup_sequence = self.request('SETUP', {
                            'Transport': 'RTP/AVP/UDP;unicast;client_port=0'})
                    elif parameters.get('his_trigger_method') == 'TEARDOWN':
                        self.peer_teardown = True
                elif method == 'TEARDOWN':
                    self.pending.append(self.pack(message('RTSP/1.0 200 OK', seq)))
                    self.peer_teardown = True
                elif method == 'RTSP/1.0' and seq == str(self.teardown_sequence):
                    self.teardown_acknowledged = m.start.startswith('RTSP/1.0 200 ')
                elif method == 'RTSP/1.0' and self.setup_sequence is not None and seq == str(self.setup_sequence):
                    if not m.start.startswith('RTSP/1.0 200 '):
                        raise ProtocolError('HiSight setup refused')
                    ports = re.search(r'(?:^|;)server_port=([0-9]+)', m.headers.get('transport', ''))
                    if not ports or not 1 <= int(ports[1]) <= 65535:
                        raise ProtocolError('missing HiSight media port')
                    self.media_endpoint = int(ports[1])
                    remote = re.search(r'(?:^|;)remotectl_port=([0-9]+)', m.headers.get('transport', ''))
                    if remote and 1 <= int(remote[1]) <= 65535:
                        self.input_endpoint = int(remote[1])
                    events.append(dict(media_port=self.media_endpoint))
        return events, plaintexts

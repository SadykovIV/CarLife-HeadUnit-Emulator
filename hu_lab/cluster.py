"""Desktop CarLife instrument data. No media control, route creation or USB actions.

IDs/schema: apollo-DuerOS CarLife-Vehicle-Lib, CTranRecvPackageProcess.h and
CarlifeNaviNextTurnInfo / CarlifeMediaInfo / CarlifeMediaProgressBar protos.
HeadSwap and native MD also send navigation field 6 as bytes (icon), sometimes
alongside the original varint time. Native MD/HeadSwap subscriptions: route=0,
music=2; MODULE_STATUS has a different namespace (navigation=2, music=3).
"""
import threading
from .honda_reference import list_body, repeated_entries
from .protocol import CMD, VIDEO_INIT_DONE, VIDEO_START, Packet, ProtocolError

MODULE_STATUS, NAVI_NEXT_TURN_INFO = 0x10026, 0x10030
MEDIA_INFO, MEDIA_PROGRESS_BAR = 0x10035, 0x10036
SUBSCRIBE, SUBSCRIBE_DONE, SUBSCRIBE_START, SUBSCRIBE_STOP = 0x18043, 0x10044, 0x18045, 0x18046
MODULES = {0:'navigation',2:'music'}
STATUS_IDS = {2:0,3:2}
REQUEST_IDS = (0,1,2)


def _subscription(message, ids):
    return Packet(CMD,message,list_body([dict(f1=i) for i in ids]))


def _values(data):
    """Keep distinct wire types for overloaded field 6. Read one bounded CMD body."""
    if len(data)>65535: raise ProtocolError('cluster command exceeds bound')
    pos=0
    def integer():
        nonlocal pos
        value=0
        for shift in range(0,70,7):
            if pos>=len(data): raise ProtocolError('truncated cluster varint')
            b=data[pos]; pos+=1
            if shift==63 and b>1: raise ProtocolError('cluster varint overflow')
            value|=(b&127)<<shift
            if b<128: return value
        raise ProtocolError('cluster varint overflow')
    result={}
    for _ in range(128):
        if pos==len(data): return result
        tag=integer(); number,wire=tag>>3,tag&7
        if not 1<=number<1<<29: raise ProtocolError('invalid cluster field')
        if wire==0: value=integer()
        elif wire in (1,2,5):
            n=integer() if wire==2 else (8 if wire==1 else 4)
            if n>len(data)-pos: raise ProtocolError('truncated cluster field')
            value=data[pos:pos+n]; pos+=n
        else: raise ProtocolError('invalid cluster wire type')
        result[number,wire]=value
    raise ProtocolError('too many cluster fields')


def _quantity(f,n):
    value=f.get((n,0))
    return value if value is not None and value<=0x7fffffff else None


def _text(f,n):
    # Keep only the short user-visible field, without control characters.
    raw=f.get((n,2),b'')
    return ''.join(s if s.isprintable() else ' ' for s in raw.decode('utf-8','replace')[:512]).strip()


def _image(f,n):
    raw=f.get((n,2),b'')
    return raw if len(raw)<=32768 else b''


class ClusterState:
    def __init__(self):
        self.lock=threading.RLock()
        self.subscriptions={name:dict(state='idle',module_status=None) for name in MODULES.values()}
        self.navigation=self.music=None
        self.revision=self.invalid_packets=0
        self.requested=self.closed=False

    def snapshot(self):
        with self.lock:
            return dict(revision=self.revision, invalid_packets=self.invalid_packets,
                subscriptions={k:dict(v) for k,v in self.subscriptions.items()},
                navigation=dict(self.navigation) if self.navigation is not None else None,
                music=dict(self.music) if self.music is not None else None)

    def requested_once(self):
        with self.lock:
            if self.closed or self.requested: return False
            self.requested=True
            for sub in self.subscriptions.values(): sub['state']='requested'
            self.revision+=1
            return True

    def acknowledge(self,payload):
        with self.lock:
            if self.closed or not self.requested: return []
            # Accept independently confirmed modules. A missing entry is
            # unconfirmed, not proof of rejection. Ignore unknown module IDs;
            # malformed/contradictory acknowledgements must not kill projection.
            try:
                if len(payload)>8192: raise ProtocolError('cluster subscription list exceeds bound')
                entries=repeated_entries(payload)
                ids=[e[1] for e in entries]
                if len(set(ids))!=len(ids) or any(e.get(2,0) not in (0,1) for e in entries):
                    raise ProtocolError('invalid metadata support entries')
            except ProtocolError:
                self.invalid_packets+=1
                return []
            starts=[]; stops=[]
            for e in entries:
                mod=e[1]
                if mod not in MODULES: continue  # Assistant module 1 was queried, not consumed.
                name=MODULES[mod]; sub=self.subscriptions[name]
                new='active' if e.get(2,0) else 'unsupported'
                if new==sub['state']: continue
                if new=='active': starts.append(mod)
                if sub['state']=='active' and new!='active':
                    stops.append(mod)
                    setattr(self,name,None)
                sub['state']=new; self.revision+=1
            return ([_subscription(SUBSCRIBE_STOP,stops)] if stops else []) + (
                [_subscription(SUBSCRIBE_START,starts)] if starts else [])

    def consume(self,packet):
        """Return responses for recognized events, or None for other packets."""
        if packet.channel!=CMD or packet.message not in (MODULE_STATUS,NAVI_NEXT_TURN_INFO,MEDIA_INFO,MEDIA_PROGRESS_BAR):
            return None
        with self.lock:
            if self.closed: return []
            try:
                if packet.message==MODULE_STATUS:
                    if len(packet.payload)>8192: raise ProtocolError('cluster status list exceeds bound')
                    entries=repeated_entries(packet.payload)
                    if (len({e[1] for e in entries})!=len(entries)
                            or any(not isinstance(e.get(2),int) for e in entries)):
                        raise ProtocolError('invalid cluster module status')
                    restart=[]
                    for e in entries:
                        mod=STATUS_IDS.get(e[1])
                        if mod is None: continue
                        name=MODULES[mod]; sub=self.subscriptions[name]; status=e[2]
                        if sub['module_status']==status: continue
                        sub['module_status']=status
                        if status==0: setattr(self,name,None)
                        # Honda peers rearm route guidance when NAVI enters
                        # RUNNING. Do not echo repeated MODULE_STATUS packets.
                        if mod==0 and status==1 and sub['state']=='active': restart.append(mod)
                        self.revision+=1
                    return [_subscription(SUBSCRIBE_START,restart)] if restart else []
                name='navigation' if packet.message==NAVI_NEXT_TURN_INFO else 'music'
                if self.subscriptions[name]['state']!='active': return []
                f=_values(packet.payload)
                if packet.message==NAVI_NEXT_TURN_INFO:
                    action=_quantity(f,1)
                    if action==3: self.navigation=None
                    elif action in (1,2) and _quantity(f,2) is not None:
                        self.navigation=dict(action=action, turn_id=_quantity(f,2), road=_text(f,3),
                            total_distance_m=_quantity(f,4), distance_m=_quantity(f,5),
                            time=_quantity(f,6), icon=_image(f,6))
                    else: raise ProtocolError('invalid route action or turn')
                elif packet.message==MEDIA_INFO:
                    if (2,2) not in f: raise ProtocolError('missing media title field')
                    self.music=dict(title=_text(f,2),artist=_text(f,3),album=_text(f,4),source=_text(f,1),
                        artwork=_image(f,5),duration_s=_quantity(f,6),position_ms=None)
                else:
                    position=_quantity(f,1)
                    if position is None: raise ProtocolError('invalid media progress')
                    if self.music is None: return []
                    self.music['position_ms']=position
                self.revision+=1
            except (ProtocolError,UnicodeError):
                self.invalid_packets+=1
            return []

    def close(self):
        with self.lock:
            if self.closed: return []
            active=[mod for mod,name in MODULES.items() if self.subscriptions[name]['state']=='active']
            self.closed=True
            for sub in self.subscriptions.values():
                sub.update(state='stopped',module_status=None)
            self.navigation=self.music=None
            self.revision+=1
            return [_subscription(SUBSCRIBE_STOP,active)] if active else []


class DesktopClusterSession:
    """Mixed into desktop sessions only; reference/lab session profiles stay unchanged."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.cluster=ClusterState()

    def receive(self,packet):
        if packet.channel==CMD and packet.message==SUBSCRIBE_DONE:
            return self.cluster.acknowledge(packet.payload)
        result=self.cluster.consume(packet)
        if result is not None: return result
        out=super().receive(packet)
        if (packet.channel==CMD and packet.message==VIDEO_INIT_DONE and any(p.message==VIDEO_START for p in out)
                and self.cluster.requested_once() and not any(p.message==SUBSCRIBE for p in out)):
            out.append(_subscription(SUBSCRIBE,REQUEST_IDS))
        return out

"""Separate synthetic HU profile derived from historical Wizcar Honda log.
No hardware IO. Never a byte-exact capture or proof of Honda authentication.
Baseline HUSession, framing, RESET and decoder remain unmodified.
"""
from .protocol import (HUSession, Packet, ProtocolError, CMD, HU_VERSION, VERSION_MATCH,
    HU_INFO, VIDEO_INIT, VIDEO_INIT_DONE, VIDEO_START, VIDEO_RESET, protobuf, fields, AUTH_IDS)
STATISTIC_INFO=0x18027
MD_AUTHEN_RESULT=0x1004B
MD_FEATURE_REQUEST, HU_FEATURE_RESPONSE=0x10051,0x18052
CAR_DATA_SUBSCRIBE,CAR_DATA_DONE=0x10031,0x10032
CARLIFE_SUBSCRIBE,CARLIFE_SUBSCRIBE_DONE=0x18043,0x10044
FEATURES=('BLUETOOTH_AUTO_PAIR','BLUETOOTH_INTERNAL_UI','FOCUS_UI','VOICE_WAKEUP')


def repeated_entries(body):
    """Bounded proto repeated field2 reader; preserve every requested module ID."""
    if len(body)>65535: raise ProtocolError('reference list too large')
    pos=0;entries=[];count=None
    def vi():
        nonlocal pos
        n=0
        for shift in range(0,70,7):
            if pos==len(body): raise ProtocolError('reference list truncated')
            b=body[pos];pos+=1
            if shift==63 and b>1: raise ProtocolError('reference list overflow')
            n|=(b&127)<<shift
            if not b&128:return n
        raise ProtocolError('reference list overflow')
    while pos<len(body):
        tag=vi();field,wire=tag>>3,tag&7
        if not field:raise ProtocolError('reference zero field')
        if wire==0:
            value=vi()
            if field==1:
                if count is not None:raise ProtocolError('reference duplicate count')
                count=value
        elif wire==2:
            size=vi()
            if size>len(body)-pos:raise ProtocolError('reference list length')
            value=body[pos:pos+size];pos+=size
            if field==2:entries.append(fields(value))
        else:raise ProtocolError('unsupported reference list wire type')
    if count!=len(entries) or len(entries)>32 or any(not isinstance(e.get(1),int) for e in entries):
        raise ProtocolError('reference list count or moduleID mismatch')
    return entries


def list_body(entries):
    return protobuf(f1=len(entries))+b''.join(protobuf(f2=protobuf(**e)) for e in entries)


def hu_info():
    # Optional empty fields are intentionally explicit: synthetic64B, while the
    # historical log says62B. That log cannot identify which empty field was absent.
    f={f'f{i}':'' for i in range(1,24)}
    f.update(f1='Linux',f19='1.0',f20='21',f21=21)
    return protobuf(**f)


def statistic_info():
    return protobuf(f1='21000242',f2='1.1.0',f3=1,f4='20772124',f5=0,f6=1,f7=0,f8='')


def feature_info():
    return list_body([dict(f1=k,f2=0) for k in FEATURES])


class HondaReferenceSession(HUSession):
    profile='SYNTHETIC_HONDA_WIZCAR_LOG_V2'
    interoperability='SELF_REFERENCE_FIXTURE_NOT_HONDA_HU'
    def __init__(self,*args,wire_fps=30,double_version=False,subscribe_ids=(0,),version_minor=0,omit_start=False,**kwargs):
        if wire_fps not in (0,30):raise ProtocolError('reference wire fps must be0 or30')
        if version_minor not in (0,1):raise ProtocolError('reference version minor must be0 or1')
        if not subscribe_ids or len(subscribe_ids)>8 or any(i not in (0,1,2) for i in subscribe_ids):
            raise ProtocolError('unsupported reference subscription case')
        super().__init__(*args,**kwargs)
        self.wire_fps=wire_fps;self.double_version=bool(double_version);self.subscribe_ids=tuple(subscribe_ids)
        self.version_minor=version_minor;self.omit_start=bool(omit_start)
        self.reference_events=[];self.initial_init_sent=False;self.subscription_sent=False
        self.requested['wire_fps']=wire_fps
        self.requested['provenance']='synthetic fixture from historical Honda log; fps0/doubleVERSION are additional explicit stress cases'
    def start(self):
        if self.state!='NEW':raise ProtocolError('duplicate local start')
        self.state='WAIT_VERSION'
        p=Packet(CMD,HU_VERSION,protobuf(f1=2,f2=self.version_minor))
        self.reference_events.append({'event':'reference_start','double_version':self.double_version,'wire_fps':self.wire_fps,'version_minor':self.version_minor,'omit_start':self.omit_start})
        return [p,p] if self.double_version else [p]
    def receive(self,p):
        if p.message==MD_AUTHEN_RESULT:
            value=fields(p.payload).get(1)
            if value not in (0,1):raise ProtocolError('invalid MD_AUTHEN_RESULT boolean')
            self.reference_events.append({'event':'md_declared_authen_result','value':bool(value),'cryptographic_auth_verified':False})
            return []
        # Other authentication/encryption messages remain strictly unsupported.
        if p.message in AUTH_IDS:return super().receive(p)
        if p.message==VERSION_MATCH and self.state=='WAIT_VERSION':
            if fields(p.payload).get(1)!=1:raise ProtocolError('MD rejected reference version2')
            self.state='WAIT_INIT'
            return [Packet(CMD,HU_INFO,hu_info()),Packet(CMD,STATISTIC_INFO,statistic_info())]
        if p.message==MD_FEATURE_REQUEST:
            if self.state not in ('WAIT_INIT','STREAMING','PAUSED'):raise ProtocolError('feature request before VERSION match')
            out=[Packet(CMD,HU_FEATURE_RESPONSE,feature_info())]
            if not self.initial_init_sent:
                self.initial_init_sent=True
                out.append(Packet(CMD,VIDEO_INIT,protobuf(f1=self.width,f2=self.height,f3=self.wire_fps)))
            self.reference_events.append({'event':'feature_reply','init_sent':len(out)==2})
            return out
        if p.message==CAR_DATA_SUBSCRIBE:
            if not p.payload:
                self.reference_events.append({'event':'empty_vehicle_subscription_no_response'})
                return []
            entries=repeated_entries(p.payload)
            self.reference_events.append({'event':'vehicle_data_unsupported','ids':[e[1] for e in entries]})
            return [Packet(CMD,CAR_DATA_DONE,list_body([dict(f1=e[1],f2=0) for e in entries]))]
        if p.message==CARLIFE_SUBSCRIBE_DONE:
            entries=repeated_entries(p.payload)
            ids=[e[1] for e in entries]
            if ids!=list(self.subscribe_ids) or any(e.get(2)!=0 for e in entries):
                raise ProtocolError('reference expects exact subscription echo and supportFlag=false for unavailable metadata')
            self.reference_events.append({'event':'metadata_unsupported_confirmed','ids':ids})
            return []
        out=super().receive(p)
        if p.message==VIDEO_INIT_DONE and any(q.message==VIDEO_START for q in out) and not self.subscription_sent:
            self.subscription_sent=True
            if self.omit_start:
                out=[q for q in out if q.message!=VIDEO_START]
                self.reference_events.append({'event':'video_start_intentionally_omitted','fallback_not_guaranteed':True})
            out.append(Packet(CMD,CARLIFE_SUBSCRIBE,list_body([dict(f1=i) for i in self.subscribe_ids])))
        return out
    def reset(self):
        if self.state!='STREAMING':raise ProtocolError('reference keyframe RESET outside streaming')
        self.reference_events.append({'event':'keyframe_reset_only','no_init':True})
        return [Packet(CMD,VIDEO_RESET)]
    def evidence(self):
        return {'profile':self.profile,'not_byte_exact_replay':True,'not_actual_honda_hu':True,
                'wire_fps':self.wire_fps,'double_version':self.double_version,
                'version_minor':self.version_minor,'omit_start':self.omit_start,
                'requested':self.requested,'accepted':self.accepted,'events':self.reference_events,
                'auth_verified':False}

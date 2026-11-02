"""Enumerate Windows WinUSB function paths under one selected AOA USB device.

The packaged libusb 1.0.30 WinUSB backend explicitly exposes the selected
libusb_device's Windows DEVINST via libusb_get_session_data(): core.c:893-911.
Use that node, never a search for the first matching VID/PID or serial.

Config Manager's CM_Enumerate_Classes(CM_ENUMERATE_CLASSES_INTERFACE) includes
vendor-registered classes. CM_Get_Device_Interface_ListW is constrained to an
exact instance, class and PRESENT. This module does not open any USB handle.
The caller must verify the actual WinUSB interface descriptor and pipes before
allowing traffic: a Windows path or a successful WinUsb_Initialize is not proof.
"""
import ctypes as C
import re
import sys
import uuid
from dataclasses import dataclass


_REASONS = {
    'device_changed': 'Selected accessory enumeration or Windows port changed; refresh the device list.',
    'adb_only': 'Windows exposes only an ADB WinUSB function for the selected accessory, not a CarLife function.',
    'no_winusb': 'No eligible present WinUSB function path on the selected accessory.',
    'ambiguous': 'Windows did not identify one consistent owner for the USB interface path.',
    'enumeration_failed': 'Windows could not verify the function paths of the selected accessory.',
    'invalid_accessory_descriptor': 'The selected USB descriptors do not prove a standard AOA accessory interface.',
    'missing_mi00': 'Windows did not enumerate a standard accessory MI_00 hardware ID on this USB port.',
    'adb_not_separate': 'The ADB function could not be verified as a separate device from the accessory interface.',
}


class WindowsInterfaceError(RuntimeError):
    """Safe to put in an export. Instance IDs, symbolic links and GUIDs stay private."""
    def __init__(self, reason):
        self.selection_reason = reason
        super().__init__(_REASONS[reason])


class WinUSBPath:
    __slots__ = ('path', 'role', '_validate')

    def __init__(self, path, role, validate):
        self.path, self.role, self._validate = path, role, validate

    def __repr__(self):
        return f'WinUSBPath(role={self.role!r})'

    def verify_present(self):
        try:
            self._validate()
        except WindowsInterfaceError:
            raise
        except Exception:
            raise WindowsInterfaceError('enumeration_failed') from None
        return True


# Microsoft cfgmgr32.h: CM_DRP_* and CONFIGRET. All DLL loads are lazy.
HARDWARE, COMPATIBLE, SERVICE, LOCATION = 2, 3, 5, 0x24
UPPER_FILTERS, LOWER_FILTERS = 0x12, 0x13  # CM_DRP_*FILTERS, REG_MULTI_SZ
NO_NODE, SMALL, NO_VALUE, INVALID_DATA = 0x0d, 0x1a, 0x25, 0x1f
_USB_DEVICE = uuid.UUID('a5dcbf10-6530-11d2-901f-00c04fb951ed')
_USB_CLASSES = {  # Handle physical-device class separately; never select a hub/controller.
    _USB_DEVICE,
    uuid.UUID('f18a0e88-c30c-11d0-8815-00a0c906bed8'),
    uuid.UUID('3abf6f2d-71c4-462a-8a92-1e6861e6af27'),
}


class _ConfigManager:
    def __init__(self):
        if sys.platform != 'win32':
            raise WindowsInterfaceError('enumeration_failed')
        lib = C.WinDLL('cfgmgr32.dll', winmode=0x800, use_last_error=True)  # System32
        u, p, w, v = C.c_uint32, C.POINTER(C.c_uint32), C.c_wchar_p, C.c_void_p
        def fn(name, *args):
            f = getattr(lib, name)
            f.restype, f.argtypes = u, list(args)
            return f
        self._id_size = fn('CM_Get_Device_ID_Size', p, u, u)
        self._id = fn('CM_Get_Device_IDW', u, v, u, u)
        self._locate = fn('CM_Locate_DevNodeW', p, w, u)
        self._parent = fn('CM_Get_Parent', p, u, u)
        self._child = fn('CM_Get_Child', p, u, u)
        self._sibling = fn('CM_Get_Sibling', p, u, u)
        self._prop = fn('CM_Get_DevNode_Registry_PropertyW', u, u, p, v, p, u)
        self._classes = fn('CM_Enumerate_Classes', u, v, u)
        self._size = fn('CM_Get_Device_Interface_List_SizeW', p, v, w, u)
        self._paths = fn('CM_Get_Device_Interface_ListW', v, w, v, u, u)

    @staticmethod
    def check(rc):
        if rc:
            raise WindowsInterfaceError('device_changed' if rc in (NO_NODE,0x24) else 'enumeration_failed')

    def instance(self, node):
        n = C.c_uint32()
        self.check(self._id_size(C.byref(n),node,0))
        if not 0 < n.value < 4096: raise WindowsInterfaceError('enumeration_failed')
        buf = C.create_unicode_buffer(n.value+1)
        self.check(self._id(node,buf,len(buf),0))
        return buf.value

    def locate(self, instance):
        node = C.c_uint32()
        self.check(self._locate(C.byref(node),instance,0))  # NORMAL, not PHANTOM
        return node.value

    def parent(self, node):
        parent = C.c_uint32()
        self.check(self._parent(C.byref(parent),node,0))
        return parent.value

    def children(self, node):
        current = C.c_uint32()
        rc = self._child(C.byref(current),node,0)
        seen = set()
        while rc != NO_NODE:  # no more children/siblings
            self.check(rc)
            child = current.value
            if child in seen or len(seen) >= 64: raise WindowsInterfaceError('ambiguous')
            seen.add(child)
            yield child
            rc = self._sibling(C.byref(current),child,0)

    def strings(self, node, prop):
        # CM_DRP_SERVICE is REG_SZ; the others are REG_MULTI_SZ, UTF-16.
        n, kind = C.c_uint32(65536), C.c_uint32()
        data = C.create_string_buffer(n.value)
        rc = self._prop(node,prop,C.byref(kind),data,C.byref(n),0)
        if rc == NO_VALUE: return ()
        self.check(rc)
        if kind.value not in (1,7) or n.value > len(data) or n.value % 2:
            raise WindowsInterfaceError('enumeration_failed')
        return tuple(s for s in data.raw[:n.value].decode('utf-16-le').split('\0') if s)

    def classes(self):
        for i in range(4096):
            raw = (C.c_ubyte*16)()
            rc = self._classes(i,raw,1)  # CM_ENUMERATE_CLASSES_INTERFACE
            if rc == NO_VALUE: return
            if rc == INVALID_DATA: continue  # documented sparse/invalid class entries
            self.check(rc)
            yield uuid.UUID(bytes_le=bytes(raw))
        raise WindowsInterfaceError('enumeration_failed')

    def paths(self, guid, instance):
        raw = (C.c_ubyte*16).from_buffer_copy(guid.bytes_le)
        for _ in range(3):
            n = C.c_uint32()
            self.check(self._size(C.byref(n),raw,instance,0))  # PRESENT, this instance only
            if n.value <= 1: return ()
            if n.value > 65536: raise WindowsInterfaceError('enumeration_failed')
            buf = C.create_unicode_buffer(n.value)
            rc = self._paths(raw,instance,buf,n,0)
            if rc == SMALL: continue
            self.check(rc)
            return tuple(s for s in buf[:].split('\0') if s)
        raise WindowsInterfaceError('device_changed')


@dataclass(frozen=True, repr=False)
class _Node:
    token: int
    instance: str


def _own_node(cm, node):
    if cm.instance(node.token).casefold() != node.instance.casefold() or cm.locate(node.instance) != node.token:
        raise WindowsInterfaceError('device_changed')


def _usb_location(cm, token, ports):
    # Independent check when Windows supplies a USB LocationPaths property.
    # DEVINST from the exact libusb enumeration is the mandatory identity;
    # never infer a replacement parent from this optional location information.
    paths = cm.strings(token, LOCATION)
    chains = {tuple(int(p) for p in re.findall(r'(?:^|#)USB\(([0-9]+)\)(?=#|$)',s,re.I))
              for s in paths}
    chains.discard(())
    if chains and chains != {tuple(ports)}:
        raise WindowsInterfaceError('device_changed')
    return tuple(sorted(s.casefold() for s in paths))


def _winusb_binding(cm, token):
    # As in libusb 1.0.30 windows_winusb.c:get_api_type. A listed service
    # or filter permits descriptor inspection only; it is not proof of pipes.
    return tuple(prop for prop in (SERVICE,UPPER_FILTERS,LOWER_FILTERS)
                 if any(s.casefold()=='winusb' for s in cm.strings(token,prop)))


def _role(cm, node, physical):
    if node == physical:
        return 'device'
    # Never inspect the instance-specific tail as an interface tag or VID/PID.
    ids = [node.instance.split('\\',2)[1], *cm.strings(node.token,HARDWARE), *cm.strings(node.token,COMPATIBLE)]
    patterns = '&'.join(s.upper() for s in ids)
    if (re.search(r'(?:^|[\\&_\-])ADB(?:$|[\\&_\-])',patterns)
            or re.search(r'(?:^|[\\&])CLASS_FF&SUBCLASS_42&PROT_01(?:$|[\\&])',patterns)):
        return 'adb'
    tags = set(re.findall(r'(?:^|[\\&])MI_([A-F0-9]{2})(?:$|(?=[\\&]))',patterns))
    if len(tags)>1: return 'excluded'
    return 'mi_'+tags.pop().lower() if tags else 'unlabelled'


def _check_path_string(path):
    # Consume the symbolic link from CM as opaque; do not infer a function,
    # parent or port from its filename. Reject DOS/network/non-interface paths.
    if (not isinstance(path,str) or not path.startswith('\\\\?\\') or '#' not in path[4:]
            or any(c in path[4:] for c in ('\\','/',':','\0')) or len(path)>4096):
        raise WindowsInterfaceError('enumeration_failed')


def find_winusb_candidates(identity, descriptor, *, api=None):
    """Return candidates to inspect, not permission to exchange bulk data.

    descriptor must be from a fresh *exact* libusb selection. Its Windows
    DEVINST is local and ephemeral. Serial verification, actual WinUSB pipe
    checks and the existing ownership lock remain the caller's responsibility.
    """
    try:
        return _find(identity,descriptor,api if api is not None else _ConfigManager())
    except WindowsInterfaceError:
        raise
    except Exception:
        raise WindowsInterfaceError('enumeration_failed') from None


def _physical_root(identity, descriptor, cm):
    token = descriptor.get('windows_devinst')
    row = descriptor.get('identity',{})
    if ((identity.vid,identity.pid) not in ((0x18d1,0x2d00),(0x18d1,0x2d01))
            or not identity.ports or type(token) is not int or not 0 < token <= 0xffffffff):
        raise WindowsInterfaceError('enumeration_failed')
    if (any(row.get(k)!=getattr(identity,k) for k in ('bus','address','vid','pid'))
            or tuple(row.get('ports',()))!=tuple(identity.ports)):
        raise WindowsInterfaceError('device_changed')
    root = _Node(token,cm.instance(token))
    expected = rf'USB\\VID_18D1&PID_{identity.pid:04X}(?:&REV_[A-F0-9]{{4}})?\\[^\\]+'
    if not re.fullmatch(expected,root.instance,re.I|re.ASCII): raise WindowsInterfaceError('device_changed')
    _own_node(cm,root)
    location = _usb_location(cm,token,identity.ports)
    return root,location


def _find(identity, descriptor, cm):
    root,location = _physical_root(identity,descriptor,cm)
    token=root.token
    work, seen = [(root,())], set()
    eligible, saw_adb = [], False
    while work:
        node, ancestors = work.pop()
        if node.token in seen or len(seen)>=64: raise WindowsInterfaceError('ambiguous')
        seen.add(node.token)
        role = _role(cm,node,root)
        binding = _winusb_binding(cm,node.token)
        if role == 'adb' or (identity.pid == 0x2d01 and role == 'mi_01'):
            saw_adb |= bool(binding)
            continue  # Do not consider descendants of a known ADB function.
        if role not in ('device','mi_00','unlabelled'): continue
        if binding: eligible.append((node,ancestors,role,binding))
        for child in cm.children(node.token):
            if cm.parent(child) != node.token: raise WindowsInterfaceError('device_changed')
            child = _Node(child,cm.instance(child))
            # A second physical USB PDO is never a function of this accessory.
            if re.match(r'USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}(?:&REV_[0-9A-F]{4})?\\',child.instance,re.I):
                continue
            parts = child.instance.split('\\',2)
            if len(parts)!=3: raise WindowsInterfaceError('device_changed')
            vidpid = re.search(r'(?:^|&)VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&|$)',parts[1],re.I)
            if vidpid and tuple(int(x,16) for x in vidpid.groups()) != (identity.vid,identity.pid): continue
            work.append((child,ancestors+(node,)))

    def verify(node, ancestors, role, binding, guid, path):
        chain = (*ancestors,node)
        for parent, child in zip(chain,chain[1:]):
            if cm.parent(child.token) != parent.token: raise WindowsInterfaceError('device_changed')
        for member in chain:
            _own_node(cm,member)
            if _role(cm,member,root) not in ('device','mi_00','unlabelled'):
                raise WindowsInterfaceError('device_changed')
        if (_usb_location(cm,token,identity.ports) != location or _role(cm,node,root)!=role
                or _winusb_binding(cm,node.token) != binding
                or path.casefold() not in {p.casefold() for p in cm.paths(guid,node.instance)}):
            raise WindowsInterfaceError('device_changed')

    by_path = {}
    if eligible:
        # Prefer registered function classes. A directly WinUSB-bound USB PDO
        # (e.g. single-interface 2d00) may expose only the generic device
        # class. Try that class last and only on the verified physical node.
        classes = [g for g in cm.classes() if g not in _USB_CLASSES]
        for guid in (*classes,_USB_DEVICE):
            for node, ancestors, role, binding in eligible:
                if guid == _USB_DEVICE and node != root: continue
                for path in cm.paths(guid,node.instance):
                    _check_path_string(path)
                    key = path.casefold()
                    if key in by_path:
                        if by_path[key][0] != node.token: raise WindowsInterfaceError('ambiguous')
                        continue
                    if len(by_path)>=64: raise WindowsInterfaceError('ambiguous')
                    def validate(n=node,a=ancestors,r=role,b=binding,g=guid,p=path):
                        verify(n,a,r,b,g,p)
                    by_path[key] = (node.token,WinUSBPath(path,role,validate))
    _own_node(cm,root)
    if not by_path: raise WindowsInterfaceError('adb_only' if saw_adb else 'no_winusb')
    ordered = tuple(pair[1] for pair in by_path.values())
    for candidate in ordered: candidate.verify_present()
    return ordered


_MI_ID = re.compile(r'USB\\VID_18D1&PID_2D01(?:&REV_([A-F0-9]{4}))?&MI_(00|01)',re.I|re.ASCII)
_PNP_PROFILE = re.compile(r'USB\\CLASS_([A-F0-9]{2})(?:&SUBCLASS_([A-F0-9]{2})(?:&PROT_([A-F0-9]{2}))?)?',
                          re.I|re.ASCII)


def _setup_profile(identity, descriptor):
    # The AOSP accessory function uses FF/FF/00; ADB is FF/42/01. Driver
    # setup deliberately has a narrower admission rule than normal connections.
    if (identity.vid,identity.pid)!=(0x18d1,0x2d01) or descriptor.get('configuration')!=1:
        raise WindowsInterfaceError('invalid_accessory_descriptor')
    interfaces=descriptor.get('interfaces',())
    if len(interfaces)!=2 or descriptor.get('config_error'):
        raise WindowsInterfaceError('invalid_accessory_descriptor')
    profiles=[]
    for number,expected in ((0,(255,255,0)),(1,(255,66,1))):
        alts=[i for i in interfaces if i.get('number')==number and i.get('alternate')==0]
        if len(alts)!=1 or tuple(alts[0].get(k) for k in ('class','subclass','protocol'))!=expected:
            raise WindowsInterfaceError('invalid_accessory_descriptor')
        eps=alts[0].get('endpoints',())
        if len(eps)!=2:
            raise WindowsInterfaceError('invalid_accessory_descriptor')
        pairs=[]
        for ep in eps:
            addr, size=ep.get('address'),ep.get('max_packet')
            if (ep.get('attributes')!=2 or type(addr) is not int or not 1<=addr<=0x8f
                    or addr&0x70 or addr&15==0 or type(size) is not int or not 1<=size<=1024):
                raise WindowsInterfaceError('invalid_accessory_descriptor')
            pairs.append((addr,size))
        if sum(bool(addr&0x80) for addr,size in pairs)!=1:
            raise WindowsInterfaceError('invalid_accessory_descriptor')
        profiles.append(tuple(sorted(pairs)))
    if {a for a,_ in profiles[0]} & {a for a,_ in profiles[1]}:
        raise WindowsInterfaceError('invalid_accessory_descriptor')
    return tuple(profiles)


def _standard_hardware_ids(cm, node, number, bcd):
    # Standard enumerator AND hardware IDs are required. Never synthesize a
    # hardware ID from an instance path, a compatible ID, or a path's MI token.
    pieces=node.instance.split('\\',2)
    instance_id=_MI_ID.fullmatch('\\'.join(pieces[:2])) if len(pieces)==3 else None
    if not instance_id or instance_id[2]!=number: return ()
    reported=cm.strings(node.token,HARDWARE)
    revisions={instance_id[1].upper()} if instance_id[1] else set()
    for s in reported:
        match=_MI_ID.fullmatch(s)
        if not match or match[2]!=number: return ()
        if match[1]: revisions.add(match[1].upper())
    if len(revisions)>1 or (bcd is not None and any(int(s,16)!=bcd for s in revisions)):
        raise WindowsInterfaceError('device_changed')
    return tuple(sorted({s.upper() for s in reported}))


def _pnp_profile_matches(cm,node,profile):
    for entry in cm.strings(node.token,COMPATIBLE):
        match=_PNP_PROFILE.fullmatch(entry)
        if match and any(x is not None and int(x,16)!=v for x,v in zip(match.groups(),profile)):
            return False
    return True


class DriverSetupTarget:
    """One read-only eligibility decision, never a driver binding operation."""
    __slots__=('_status','_hardware_id','_validate')

    def __init__(self,status,hardware_id,validate):
        self._status,self._hardware_id,self._validate=status,hardware_id,validate

    @property
    def status(self):
        return self._status

    @property
    def hardware_id(self):
        return self._hardware_id

    def __repr__(self):
        return f'DriverSetupTarget(status={self.status!r}, hardware_id={self.hardware_id!r})'

    def _current(self):
        try:
            return self._validate()
        except WindowsInterfaceError:
            raise
        except Exception:
            raise WindowsInterfaceError('enumeration_failed') from None
    def verify_present(self):
        self._current()
        return True

    @property
    def target_devinst(self):
        return self._current().child.token

    def native_instance_id(self):
        """Private instance for exact native API handoff. Never log or export."""
        return self._current().child.instance

    def handoff(self):
        """One freshly validated selection, no paths or private instance IDs.

        PnP can still change after this call: the elevated helper must repeat
        validation and target the one devnode, never a global hardware-ID match.
        """
        current=self._current()
        return dict(parent_devinst=current.root.token, child_devinst=current.child.token,
                    ports=current.ports, adb_devinst=current.adb.token, hardware_id=self.hardware_id)


@dataclass(frozen=True,repr=False)
class _SetupSnapshot:
    root: _Node
    ports: tuple
    location: tuple
    child: _Node
    hardware_ids: tuple
    binding: tuple
    profile: tuple
    adb: _Node


def _setup_snapshot(identity,descriptor,cm):
    profile=_setup_profile(identity,descriptor)
    root,location=_physical_root(identity,descriptor,cm)
    matches,adb=[],[]
    mi00_seen=False
    for token in cm.children(root.token):
        if cm.parent(token)!=root.token: raise WindowsInterfaceError('device_changed')
        child=_Node(token,cm.instance(token))
        _own_node(cm,child)
        ids=_standard_hardware_ids(cm,child,'00',descriptor.get('device_bcd'))
        role=_role(cm,child,root)
        instance_parts=child.instance.split('\\',2)
        standard=_MI_ID.fullmatch('\\'.join(instance_parts[:2]))
        if standard and standard[2]=='00':
            if mi00_seen: raise WindowsInterfaceError('ambiguous')
            mi00_seen=True
            if ids:
                if role!='mi_00' or not _pnp_profile_matches(cm,child,(255,255,0)):
                    raise WindowsInterfaceError('invalid_accessory_descriptor')
                binding=tuple(tuple(s.casefold() for s in cm.strings(token,p))
                              for p in (SERVICE,UPPER_FILTERS,LOWER_FILTERS))
                matches.append((child,ids,binding))
            continue  # A node identifying itself as MI_00 cannot also be its ADB sibling.
        # MI_00 can also identify a collection starting at interface zero.
        # Verify ADB is a separate sibling before allowing a setup handoff.
        sibling=_standard_hardware_ids(cm,child,'01',descriptor.get('device_bcd'))
        names=[instance_parts[1],*cm.strings(child.token,HARDWARE)]
        unrelated=any((int(v,16),int(p,16))!=(identity.vid,identity.pid)
                      for name in names for v,p in re.findall(r'VID_([0-9a-f]{4})&PID_([0-9a-f]{4})',name,re.I))
        wrong_interface=any(mi!='01' for name in names for mi in re.findall(
            r'(?:^|[\\&])MI_([0-9a-f]{2})(?:&|$)',name,re.I))
        if (not unrelated and not wrong_interface and ((sibling and role=='mi_01') or role=='adb')
                and _pnp_profile_matches(cm,child,(255,66,1))):
            adb.append(child)
    if not matches: raise WindowsInterfaceError('missing_mi00')
    if len(matches)!=1 or len(adb)>1: raise WindowsInterfaceError('ambiguous')
    if not adb: raise WindowsInterfaceError('adb_not_separate')
    child,ids,binding=matches[0]
    _own_node(cm,root)
    if (_usb_location(cm,root.token,identity.ports)!=location
            or any(cm.parent(n.token)!=root.token for n in (child,adb[0]))):
        raise WindowsInterfaceError('device_changed')
    for n in (child,adb[0]): _own_node(cm,n)
    return _SetupSnapshot(root,tuple(identity.ports),location,child,ids,binding,profile,adb[0])


def find_driver_setup_target(identity,descriptor,*,api=None):
    """Inspect only the selected present 2d01 physical node and its direct children.

    Requires fresh descriptors from that exact libusb enumeration. Returning a
    target neither installs a driver nor authorizes a global hardware-ID update.
    Rerun from a fresh enumeration after an elevation or process handoff.
    """
    try:
        cm=api if api is not None else _ConfigManager()
        before=_setup_snapshot(identity,descriptor,cm)
        def validate():
            try:
                after=_setup_snapshot(identity,descriptor,cm)
            except WindowsInterfaceError as error:
                if error.selection_reason in ('missing_mi00','adb_not_separate'):
                    raise WindowsInterfaceError('device_changed') from None
                raise
            if after!=before:
                raise WindowsInterfaceError('device_changed')
            return after
        generic=r'USB\VID_18D1&PID_2D01&MI_00'
        hardware_id=generic if generic in before.hardware_ids else before.hardware_ids[0]
        bound=any('winusb' in names for names in before.binding)
        return DriverSetupTarget('already_winusb' if bound else 'installable',hardware_id,validate)
    except WindowsInterfaceError:
        raise
    except Exception:
        raise WindowsInterfaceError('enumeration_failed') from None

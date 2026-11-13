"""Read the exclusive owner of the selected macOS AOA interface from IORegistry."""
import plistlib
import subprocess
from xml.parsers.expat import ExpatError


def _interface_owner(devices, identity, session_id):
    # Darwin libusb_get_session_data is the device's IOKit sessionID. A new
    # enumeration on the same physical port has a different sessionID.
    if not isinstance(devices, list):
        return None
    matches = [d for d in devices if isinstance(d, dict) and d.get('sessionID') == session_id
               and d.get('idVendor') == identity.vid and d.get('idProduct') == identity.pid
               and d.get('USB Address') == identity.address]
    if len(matches) != 1:
        return None
    interfaces = [i for i in matches[0].get('IORegistryEntryChildren', ())
                  if isinstance(i, dict) and i.get('IOObjectClass') == 'IOUSBHostInterface'
                  and i.get('bInterfaceNumber') == 0 and i.get('bAlternateSetting') == 0
                  and i.get('bConfigurationValue') == 1]
    if len(interfaces) == 1:
        owner = interfaces[0].get('UsbExclusiveOwner')
        if isinstance(owner, str):
            return ' '.join(owner.split())[:128] or None
    return None


def accessory_interface_owner(handle):
    session_id = handle.descriptor.get('darwin_session_id')
    if not session_id:
        return None
    try:
        result = subprocess.run(
            ['/usr/sbin/ioreg', '-a', '-l', '-r', '-c', 'IOUSBHostDevice', '-d', '2'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.6, check=True)
        return _interface_owner(plistlib.loads(result.stdout), handle.identity, session_id)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError, ExpatError):
        # Registry diagnostics are optional. The normal libusb claim still
        # decides whether the selected interface can be used.
        return None

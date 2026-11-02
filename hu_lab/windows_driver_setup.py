"""Unprivileged driver setup flow. Only an explicit, confirmed install launches the helper."""
from contextlib import contextmanager
from dataclasses import dataclass, field

from .platform_support import lock_owner, open_owner_lock, usb_lock_directory
from .usb import LibUSB, USBIdentity, select_exact
from .windows_usb_interfaces import WindowsInterfaceError, find_driver_setup_target


class SetupUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SetupPlan:
    action: str
    identity: USBIdentity = field(repr=False)
    parent: int = field(repr=False)
    target: object = field(default=None, repr=False)


@contextmanager
def _own_port(identity):
    if not identity.ports or not all(type(n) is int and 1 <= n <= 255 for n in identity.ports):
        raise SetupUnavailable('Не удалось определить физический USB-порт телефона.')
    key = identity.physical_key.replace(':', '-')
    with open_owner_lock(usb_lock_directory() / f'carlife-usb-{key}.lock') as owner:
        try:
            lock_owner(owner)
        except BlockingIOError as error:
            raise SetupUnavailable('Этот USB-порт занят другой сессией CarLife. Сначала отключите её.') from error
        yield


def _descriptor(usb, identity):
    try:
        row = select_exact(usb.discover(), identity)
    except ValueError as error:
        raise SetupUnavailable('Выбранное устройство больше недоступно. Обновите список USB.') from error
    token = row.get('windows_devinst')
    if row['identity']['address'] != identity.address or not token:
        raise SetupUnavailable('Изменился адрес USB-устройства. Обновите список и выберите его заново.')
    return row


def _usable(usb, identity, parent):
    # The RC2 transport verifies actual IF0 and both pipe IDs on a single live
    # handle. A service name, a successful install or an exit code cannot
    # establish that Windows has published a usable interface.
    try:
        with usb.open_exact(identity) as handle:
            if handle.descriptor.get('windows_devinst') != parent:
                raise SetupUnavailable('USB-устройство изменилось во время проверки.')
            verified = getattr(handle, 'verified_interface', None)
            if not verified or verified['number'] != 0:
                raise SetupUnavailable('Не удалось проверить интерфейс 0 через WinUSB.')
            return True
    except Exception as error:
        if getattr(error, 'selection_reason', None) in ('no_winusb', 'adb_only', 'no_verified_accessory'):
            return False
        raise


def inspect_setup(identity, *, usb_factory=LibUSB):
    if (identity.vid, identity.pid) != (0x18d1, 0x2d01):
        raise SetupUnavailable('Для настройки нужен аксессуар 18d1:2d01. '
                               'Если телефон ещё в обычном режиме, сначала нажмите «Подключить».')
    with _own_port(identity), usb_factory() as usb:
        row = _descriptor(usb, identity)
        parent = row['windows_devinst']
        if _usable(usb, identity, parent):
            return SetupPlan('ready', identity, parent)
        try:
            target = find_driver_setup_target(identity, row)
        except WindowsInterfaceError as error:
            if error.selection_reason in ('missing_mi00', 'adb_not_separate', 'ambiguous', 'invalid_accessory_descriptor'):
                raise SetupUnavailable('Windows не подтвердила на этом порту интерфейс '
                    'USB\\VID_18D1&PID_2D01&MI_00 и отдельную функцию ADB. Настройка недоступна.') from error
            raise
        return SetupPlan('register' if target.status == 'already_winusb' else 'install', identity, parent, target)


def apply_setup(plan, *, launcher=None, usb_factory=LibUSB):
    if plan.action not in ('install', 'register') or plan.target is None:
        raise SetupUnavailable('Для этого устройства установка не требуется.')
    if launcher is None:
        from .windows_setup_launcher import launch_driver_setup as launcher
    with _own_port(plan.identity):
        with usb_factory() as usb:
            row = _descriptor(usb, plan.identity)
            if row['windows_devinst'] != plan.parent:
                raise SetupUnavailable('Выбранное устройство изменилось. Повторите проверку.')
            plan.target.verify_present()
            if _usable(usb, plan.identity, plan.parent):
                return 'ready'
        outcome = launcher(plan.target)
        if outcome not in ('installed', 'registered', 'already_ready', 'reconnect'):
            return outcome
        try:
            with usb_factory() as usb:
                row = _descriptor(usb, plan.identity)
                if row['windows_devinst'] == plan.parent and _usable(usb, plan.identity, plan.parent):
                    return 'ready'
        except (OSError, RuntimeError, ValueError):
            # Installing can retire the old devnode. Do not follow another device.
            pass
        return 'reconnect'

"""Launch only the packaged native MI_00 driver helper, after revalidating its target."""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import re
import sys


HELPER_NAME = "CarLifeWinUsbSetup.exe"
_STATUSES = {0: "installed", 10: "already_ready", 11: "reconnect", 12: "reboot", 13: "cancelled"}
_FAILURES = {
    20: "Выбранный интерфейс MI_00 изменился или больше не подходит для установки.",
    21: "Подходящий встроенный подписанный драйвер WinUSB не найден или его подлинность не подтверждена.",
    22: "Windows не разрешила установить драйвер для выбранного интерфейса.",
    23: "Windows не подтвердила привязку WinUSB к выбранному интерфейсу. Обновите список устройств.",
    24: "Безопасный откат привязки драйвера для этого устройства недоступен.",
}


def _arguments(target):
    status = getattr(target, "status", None)
    if status not in ("installable", "already_winusb"):
        raise RuntimeError("Для установки требуется проверенный интерфейс AOA MI_00.")
    current = target.handoff()
    if not isinstance(current, dict):
        raise RuntimeError("Windows не подтвердила выбранный интерфейс.")
    hardware = current.get("hardware_id")
    if not isinstance(hardware, str) or not re.fullmatch(
            r"USB\\VID_18D1&PID_2D01(?:&REV_[0-9A-F]{4})?&MI_00", hardware, re.I):
        raise RuntimeError("Подтверждённый аппаратный идентификатор не является AOA MI_00.")
    parent, child, adb = (current.get(key) for key in ("parent_devinst", "child_devinst", "adb_devinst"))
    if (not all(type(n) is int and 0 < n <= 0xffffffff for n in (parent, child, adb))
            or len({parent, child, adb}) != 3):
        raise RuntimeError("Не подтверждены отдельные функции AOA и ADB выбранного устройства.")
    ports = current.get("ports")
    if (not isinstance(ports, (list, tuple)) or not 1 <= len(ports) <= 7
            or not all(type(n) is int and 1 <= n <= 255 for n in ports)):
        raise RuntimeError("Не подтверждён физический USB-порт выбранного устройства.")
    mode = "--install-mi00" if status == "installable" else "--register-mi00"
    return f"{mode} {parent} {child} " + ".".join(map(str, ports))


def _open_pinned(path):
    """Deny writes/deletes until the elevated process finishes using these bytes."""
    import msvcrt
    k = C.WinDLL("kernel32.dll", winmode=0x800, use_last_error=True)
    k.CreateFileW.argtypes = [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p,
                              C.c_uint32, C.c_uint32, C.c_void_p]
    k.CreateFileW.restype = C.c_void_p
    k.CloseHandle.argtypes, k.CloseHandle.restype = [C.c_void_p], C.c_int32
    handle = k.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle in (None, C.c_void_p(-1).value):
        raise RuntimeError("Не удалось открыть файл помощника для проверки.")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        k.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


class _ExecuteInfo(C.Structure):
    _fields_ = [("cbSize", C.c_uint32), ("fMask", C.c_uint32), ("hwnd", C.c_void_p),
                ("lpVerb", C.c_wchar_p), ("lpFile", C.c_wchar_p), ("lpParameters", C.c_wchar_p),
                ("lpDirectory", C.c_wchar_p), ("nShow", C.c_int32), ("hInstApp", C.c_void_p),
                ("lpIDList", C.c_void_p), ("lpClass", C.c_wchar_p), ("hkeyClass", C.c_void_p),
                ("dwHotKey", C.c_uint32), ("hIcon", C.c_void_p), ("hProcess", C.c_void_p)]


def _elevate(path, arguments):
    shell = C.WinDLL("shell32.dll", winmode=0x800, use_last_error=True)
    kernel = C.WinDLL("kernel32.dll", winmode=0x800, use_last_error=True)
    shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [C.POINTER(_ExecuteInfo)], C.c_int32
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [C.c_void_p, C.c_uint32], C.c_uint32
    kernel.GetExitCodeProcess.argtypes, kernel.GetExitCodeProcess.restype = [C.c_void_p, C.POINTER(C.c_uint32)], C.c_int32
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [C.c_void_p], C.c_int32
    info = _ExecuteInfo()
    info.cbSize, info.fMask = C.sizeof(info), 0x40 | 0x100  # NOCLOSEPROCESS | NOASYNC
    info.lpVerb, info.lpFile, info.lpParameters = "runas", str(path), arguments
    info.lpDirectory, info.nShow = str(path.parent), 1
    if not shell.ShellExecuteExW(C.byref(info)):
        native = C.get_last_error()
        if native == 1223:  # ERROR_CANCELLED at UAC
            return 13
        raise RuntimeError(f"Windows не запустила помощник установки (код {native}).")
    if not info.hProcess:
        raise RuntimeError("Windows не вернула процесс помощника установки.")
    try:
        if kernel.WaitForSingleObject(info.hProcess, 0xffffffff) != 0:
            raise RuntimeError("Не удалось дождаться завершения помощника установки.")
        code = C.c_uint32()
        if not kernel.GetExitCodeProcess(info.hProcess, C.byref(code)):
            raise RuntimeError("Не удалось получить результат помощника установки.")
        return code.value
    finally:
        kernel.CloseHandle(info.hProcess)


def _launch(target, root, pin=_open_pinned, elevate=_elevate):
    root = root.resolve()
    path = root / HELPER_NAME
    try:
        if path.resolve(strict=True) != path or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Путь помощника установки отличается от файла в комплекте приложения.")
        info = json.loads((root / "build-info.json").read_text(encoding="utf-8"))["driver_setup_helper"]
        digest = info["sha256"]
        if info["name"] != HELPER_NAME or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid manifest")
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("Не найден проверяемый помощник установки. Полностью распакуйте ZIP приложения.") from None
    try:
        with pin(path) as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise RuntimeError("SHA256 помощника установки не совпадает. Повторно распакуйте исходный ZIP.")
            # Do the topology and hardware-ID check as close as possible to UAC.
            arguments = _arguments(target)
            code = elevate(path, arguments)
    except OSError:
        raise RuntimeError("Windows не подтвердила запуск или результат помощника установки.") from None
    if code not in _STATUSES:
        raise RuntimeError(_FAILURES.get(code, "Помощник не подтвердил установку драйвера."))
    if code == 0 and arguments.startswith("--register-mi00 "):
        return "registered"
    return _STATUSES[code]


def launch_driver_setup(target):
    """Call from a worker, only after explicit confirmation in the application."""
    if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("Установка драйвера доступна только в приложении для Windows x64.")
    return _launch(target, Path(__file__).resolve().parents[1])

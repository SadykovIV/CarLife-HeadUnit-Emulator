"""Portable Windows bootstrap; no installed Python, Qt or USB library needed."""
from pathlib import Path
import os
import sys
import traceback

ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / 'runtime' / 'Lib' / 'site-packages'
sys._carlife_portable = True
# Keep directory handles alive for delayed Qt/FFmpeg DLL loads.
_dll_directories = [os.add_dll_directory(str(path)) for path in
                    (ROOT, ROOT / 'runtime', PACKAGES / 'PySide6', PACKAGES / 'shiboken6',
                     PACKAGES / 'av.libs', PACKAGES / 'pillow.libs')
                    if path.is_dir()]


def main():
    try:
        if '--bundle-check' in sys.argv:
            from bundle_entry import bundle_check
            return bundle_check()
        from hu_lab.desktop import main as desktop_main
        return desktop_main()
    except Exception:
        # pythonw has no stderr: preserve a useful diagnostic for startup errors.
        destination = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CarLife HeadUnit'
        destination.mkdir(parents=True, exist_ok=True)
        report = destination / 'startup-error.txt'
        report.write_text(traceback.format_exc(), encoding='utf-8')
        if '--bundle-check' not in sys.argv and '--smoke-test' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'Не удалось запустить CarLife HeadUnit Emulator.\n\nПодробности: {report}',
                                            'CarLife HeadUnit Emulator', 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

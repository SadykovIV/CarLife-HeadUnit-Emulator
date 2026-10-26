# Changelog

## 0.3.1 — 2026-10-26

First GitHub release of the standalone **CarLife HeadUnit Emulator**, extracted from the desktop application in [Carlife_on_box](https://github.com/SadykovIV/Carlife_on_box).

- Rename the 0.3.0 application from **CarLife HeadUnit** to **CarLife HeadUnit Emulator**. Package names are `CarLife HeadUnit Emulator.app` on macOS and `CarLifeHeadUnitEmulator.exe` on Windows.
- Preserve the `CarLife HeadUnit` user-data directory so existing settings and session records remain available.
- Publish a focused desktop source tree, English and Russian READMEs, build instructions, automated tests and third-party notices.
- Retain the 0.3.0 interface and features: top connection/device selection, always-enabled USB AOA transport, dark/light/system themes, video/audio diagnostics, two-contact input and optional AOA 2.0 US/RU HID keyboard with one-way host clipboard paste.
- Keep Legacy dual as the default two-contact profile and Action3 as an explicit device compatibility option.

The current macOS arm64 package requires macOS 26+. The Windows x64 portable package is cross-built and has not been run on Windows. Physical HID text delivery remains unverified; this release does not expand the prior hardware compatibility claims. See the [validation history and limits](docs/MAC_HEADUNIT_APP.md#проверка-готовой-сборки).

## 0.3.0 — 2026-10-25

Prior local release named **CarLife HeadUnit**, before extraction into this repository.

- Add light and system themes alongside the dark theme, and the official Baidu CarLife artwork with source attribution.
- Add live two-contact gestures through trackpad, mouse, wheel and Qt touch events, with rotation/crop mapping and contact cleanup.
- Add optional AOA 2.0 HID keyboard input for US/RU layouts, text submission and one-way paste from the host clipboard.
- Extend automated tests for desktop input, themes, keyboard reports and session lifecycle. These tests use synthetic peers and mocked USB.

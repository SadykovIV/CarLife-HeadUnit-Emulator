# Changelog

## [0.5.1](https://github.com/SadykovIV/CarLife-HeadUnit-Emulator/releases/tag/v0.5.1) — 2026-11-13

- Connect to a compatible phone over Wi-Fi. Put both devices on the same network or use the phone hotspot, then discover the phone or enter its IP address.
- Move, zoom and rotate maps with independent contacts on a Mac trackpad or a touchscreen. Legacy dual supports two fingers. Compatible devices can use up to four with Action3.
- Show received JPEG or PNG album covers beside the track title and artist in the instrument cluster. Switching to a track without artwork clears the previous cover.
- Keep music and voice prompts playing while the window processes gestures or redraws the projection.

## [0.4.1](https://github.com/SadykovIV/CarLife-HeadUnit-Emulator/releases/tag/v0.4.1) — 2026-11-06

- Fix USB connection to Wizcar A2, which could enter accessory mode but fail protocol negotiation. Send the 8-byte channel header and message body in separate USB transfers, matching Baidu HU framing, while preserving the write deadline and protection against replay after a failed write.
- Verify connection and 1280×720 video at approximately 30 fps on a physical Wizcar A2 connected to macOS. Windows uses the same transport fix; physical Windows USB operation was not validated.
- **Known limitation:** reconnecting Wizcar A2 after ending a session still requires unplugging and reconnecting the adapter, then refreshing the USB list. Reconnection without a physical unplug is not fixed in this release.

See the [release manifest](https://github.com/SadykovIV/CarLife-HeadUnit-Emulator/releases/download/v0.4.1/RELEASE.json) for the validation record.

## [0.4.0](https://github.com/SadykovIV/CarLife-HeadUnit-Emulator/releases/tag/v0.4.0) — 2026-11-03

- Keep the phone projection visible while **«Приборка и HUD»** shows the turn arrow, distance and current track alongside it. The same panel offers a local instrument demo and access to Windows driver setup.
- Use CarLife v2 · Honda for every desktop USB, TCP and demo connection. There is no protocol selector; older v1 preferences cannot switch a session back to v1.
- On Windows, guide users through setup of the separate CarLife **MI_00** only when it is needed. A working installation and the separate ADB function are left alone.

## [0.3.2-rc.3](https://github.com/SadykovIV/CarLife-HeadUnit-Emulator/releases/tag/v0.3.2-rc.3) — 2026-11-02 (prerelease)

- Add an instrument cluster showing the next maneuver, distance, track and artist from CarLife subscriptions. A local driving demo exercises the same messages without a phone or car.
- Verify the actual Windows accessory interface and bulk endpoints before exchanging data. Offer a confirmed setup helper for an eligible `18d1:2d01` CarLife `MI_00`; leave working bindings and the separate ADB interface alone.
- Keep the selected USB port when the phone re-enumerates. Diagnostics and JSON export now identify the current connection attempt and its original error, even after a previous successful session.

## 0.3.1 — 2026-10-26

First GitHub release of the standalone **CarLife HeadUnit Emulator**, extracted from the desktop application in [Carlife_on_box](https://github.com/SadykovIV/Carlife_on_box).

- Rename the 0.3.0 application from **CarLife HeadUnit** to **CarLife HeadUnit Emulator**. Package names are `CarLife HeadUnit Emulator.app` on macOS and `CarLifeHeadUnitEmulator.exe` on Windows.
- Preserve the `CarLife HeadUnit` user-data directory so existing settings and session records remain available.
- Publish a focused desktop source tree, English and Russian READMEs, build instructions, automated tests and third-party notices.
- Retain the 0.3.0 interface and features: top connection/device selection, always-enabled USB AOA transport, dark/light/system themes, video/audio diagnostics, two-contact input and optional AOA 2.0 US/RU HID keyboard with one-way host clipboard paste.
- Keep Legacy dual as the default two-contact profile and Action3 as an explicit device compatibility option.
- Use the standard Baidu/CarLife accessory identity so Android can launch HeadSwap and other applications with CarLife USB accessory filters.
- Fix USB HID registration: use Android-compatible ID 1, reuse the AOA version captured during connection, and avoid a second GET_PROTOCOL request that can clear accessory identity. Start optional HID only after CarLife reaches STREAMING.
- Do not send release/unregister after a definite registration rejection; distinguish optional keyboard errors from cleanup failures. Preserve the original USB write failure when cancellation wakes the reader first.
- Keep rapid pause/resume working when old video frames remain in flight, while retaining bounded keyframe recovery and strict initial stream validation.
- Retain the QtNetwork Python binding required by QtMultimedia in the Windows package, including dependencies loaded dynamically through Shiboken.
- Mark intentional demo shutdown before closing the client connection so Windows loopback aborts during teardown do not report a successful demo as failed.

The 0.3.1 macOS arm64 package requires macOS 26+. Both 0.3.1 packages passed native GUI demos with clean shutdown; the Windows package was checked on a Windows 2025 GitHub runner. Those tests did not cover physical Windows USB or audio output. A source run with one HeadSwap device confirmed USB negotiation, video and HID registration/cleanup; physical HID text delivery was not verified. See the [validation history and limits](docs/MAC_HEADUNIT_APP.md#проверка-готовой-сборки).

## 0.3.0 — 2026-10-25

Prior local release named **CarLife HeadUnit**, before extraction into this repository.

- Add light and system themes alongside the dark theme, and the official Baidu CarLife artwork with source attribution.
- Add live two-contact gestures through trackpad, mouse, wheel and Qt touch events, with rotation/crop mapping and contact cleanup.
- Add optional AOA 2.0 HID keyboard input for US/RU layouts, text submission and one-way paste from the host clipboard.
- Extend automated tests for desktop input, themes, keyboard reports and session lifecycle. These tests use synthetic peers and mocked USB.

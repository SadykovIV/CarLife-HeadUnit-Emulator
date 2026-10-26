# Third-party references and dependencies

The repository publishes the desktop head-unit emulator's implementation, build scripts and tests. It does not relicense or distribute proprietary OEM firmware, modified OEM APKs, Android SDKs, JDKs, signing credentials or the research archive.

## Protocol and interoperability references

- [CarLife-MD-Lib](https://github.com/Doris-duo/CarLife-MD-Lib/tree/b3cc871f7c5c4b41947a3c512caee76624eaffd9): Apache-2.0 in the inspected upstream source. Used as a protocol/reference implementation source.
- [Apollo DuerOS mirror](https://github.com/milesking/apollo-DuerOS/tree/6a07419818f46aadda34c5f55e63d4a1fc8136ef): Apache-2.0 in the inspected source. CarLife headers and protobuf descriptions informed wire-format work. The historical ApolloAuto upstream was unavailable during research; this link identifies the inspected mirror.
- [HeadSwap](https://github.com/YegorkaPomidorka/headswap): GPL-3.0 in the inspected source. Used for protocol behavior and interoperability comparison. No HeadSwap application, Kotlin implementation or GPL source file is bundled in this export. Short wire examples describe protocol bytes, not a redistribution of that application.
- [AOSP](https://source.android.com/): Android USB accessory, HID keyboard and display/input documentation and API references.

The publication review did not identify copied HeadSwap implementation blocks in the exported Python/C source. This is a scoped review, not an assertion about every possible future contribution. Preserve the original license if you add upstream code.

## Build and host dependencies

CPython, PyInstaller and its build dependencies, Clang/LLD, libusb, PyAV/FFmpeg, Pillow and PySide6/Qt are obtained separately. They retain their own licenses and distribution terms; the project license does not replace them. Desktop builds do not require an Android SDK or JDK. In particular, review PySide6/Qt and FFmpeg licensing if distributing a packaged HU application.

H.264 test fixtures are stored as hexadecimal data with provenance in `android-md/tests/fixtures/`. The `libx264-*` and `mode-baseline-*` fixtures come from independently generated neutral gray/black images. The three `mode-qcom-captured-*` files contain only historical Qualcomm encoder SPS/PPS codec configuration, retained to test header normalization; they contain no picture frames or audio. No live display recording or research log is included.

## Trademark notice

Baidu CarLife, Android Auto, CarPlay, Carlinkit, Honda and Alpine names identify compatibility targets or references. Their owners do not sponsor or endorse this project.

---

Русский: репозиторий содержит собственную реализацию и исходники интеграции. OEM-прошивки/APK, SDK/JDK, ключи и исследовательские дампы не включены. Baidu/Apollo использовались как справочные реализации протокола, HeadSwap — для сравнения поведения и совместимости; его приложение и GPL-исходники не включены. Все внешние зависимости сохраняют собственные лицензии. При переносе чужого кода сохраняйте его лицензию и уведомления; лицензия этого проекта не распространяется на сторонние прошивки и инструменты. Названия продуктов используются только для указания совместимости и источников.

## Desktop distribution

CarLife HeadUnit Emulator is extracted from [Carlife_on_box](https://github.com/SadykovIV/Carlife_on_box). Original project attribution is retained in NOTICE. The packaged desktop application includes CPython, Qt/PySide6, PyAV/FFmpeg, Pillow and libusb; their available license texts are included alongside the binaries. Build tools are separately installed.

The Baidu CarLife icon comes from the official Baidu App Store listing. The original artwork and its provenance are in `hu_lab/assets/ICON_SOURCE.json`. The icon and product marks remain the property of their owners, are excluded from the Apache-2.0 grant for project code, and do not imply endorsement.

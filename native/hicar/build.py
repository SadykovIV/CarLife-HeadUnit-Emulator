"""Build the desktop HiChain library from pinned public sources. All outputs stay in build/."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
BUILD = ROOT.parents[1] / "build" / "hicar"
LOCK = json.loads((ROOT / "dependency-lock.json").read_text())


def sources(name, cache):
    info = LOCK[name]
    digest = info["archive_sha256"]
    path = cache / (name + "-" + digest[:16] + ".archive")
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        url = info.get("archive") or (info["repository"].replace("github.com", "codeload.github.com") + "/tar.gz/refs/tags/" + info["tag"])
        with urllib.request.urlopen(url, timeout=90) as response, path.with_suffix(".part").open("wb") as target:
            shutil.copyfileobj(response, target)
        path.with_suffix(".part").replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("Dependency digest mismatch: " + name)
    dest = BUILD / "sources" / (name + "-" + digest[:16])
    if not dest.is_dir():
        unpack = dest.with_suffix(".unpacking")
        unpack.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path) as archive:
            archive.extractall(unpack, filter="data")
        next(p for p in unpack.iterdir() if p.is_dir()).rename(dest)
        unpack.rmdir()
    return dest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("macos-arm64", "windows-x64"))
    parser.add_argument("--cache", type=Path, default=BUILD / "downloads")
    parser.add_argument("--mingw-bin", type=Path, help="LLVM-MinGW bin directory when cross-compiling")
    parser.add_argument("--bundle-to", type=Path)
    parser.add_argument("--licenses-to", type=Path)
    args = parser.parse_args()
    if os.environ.get("HSD_HICAR_CACHE"):
        args.cache = Path(os.environ["HSD_HICAR_CACHE"])
    if not args.mingw_bin and os.environ.get("HSD_MINGW_BIN"):
        args.mingw_bin = Path(os.environ["HSD_MINGW_BIN"])
    out = BUILD / args.target
    out.mkdir(parents=True, exist_ok=True)
    win = args.target == "windows-x64"
    name = "hsd_hicar_auth.dll" if win else "libhsd_hicar_auth.dylib"
    digest = hashlib.sha256()
    for entry in sorted(ROOT.rglob("*")):
        if entry.is_file() and "__pycache__" not in entry.parts:
            digest.update(entry.relative_to(ROOT).as_posix().encode() + bytes([0]) + entry.read_bytes())
    manifest = out / "build.json"
    def record():
        return {"target": args.target, "sources": digest.hexdigest(), "library": hashlib.sha256((out / name).read_bytes()).hexdigest()}
    def package():
        if args.bundle_to:
            args.bundle_to.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out / name, args.bundle_to / name)
        if args.licenses_to:
            dest = args.licenses_to / "HiCar"
            dest.mkdir(parents=True, exist_ok=True)
            for src in sorted(out.glob("*-LICENSE.txt")):
                shutil.copy2(src, dest)
            for src in sorted((ROOT / "sources").glob("*/LICENSE")):
                shutil.copy2(src, dest / (src.parent.name + "-LICENSE.txt"))
            shutil.copy2(ROOT / "UPSTREAM-NOTICES.txt", dest)
            shutil.copy2(ROOT / "dependency-lock.json", dest)
    if manifest.is_file() and (out / name).is_file() and json.loads(manifest.read_text()) == record():
        package()
        return
    env = os.environ.copy()
    flags = ["-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
             "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"]
    prefix = ""
    if win:
        if args.mingw_bin:
            env["PATH"] = str(args.mingw_bin.resolve()) + os.pathsep + env["PATH"]
        prefix = "x86_64-w64-mingw32-"
        flags.extend(["-DCMAKE_SYSTEM_NAME=Windows", "-DCMAKE_C_COMPILER=" + prefix + "clang",
                      "-DCMAKE_CXX_COMPILER=" + prefix + "clang++", "-DCMAKE_RC_COMPILER=" + prefix + "windres"])

    def run(cmd, cwd=out):
        subprocess.run(list(map(str, cmd)), env=env, cwd=cwd, check=True)

    def build(name, src, target, options):
        folder = out / name
        run(["cmake", "-S", src, "-B", folder, "-G", "Ninja", *flags, *options])
        run(["cmake", "--build", folder, "--parallel", "6", "--target", target])
        return folder

    deps = {n: sources(n, args.cache) for n in ("mbedtls", "cjson", "openssl")}
    ssl = out / "openssl"
    ssl.mkdir(exist_ok=True)
    if not (ssl / "Makefile").exists():
        config = ["perl", deps["openssl"] / "Configure", "mingw64" if win else "darwin64-arm64-cc",
                  "no-shared", "no-module", "no-tests", "no-asm"]
        if win:
            config.append("--cross-compile-prefix=" + prefix)
        run(config, ssl)
    # libcrypto.a depends on generated headers, which are also needed by the adapter.
    run(["make", "-j6", "build_generated"], ssl)
    run(["make", "-j6", "libcrypto.a"], ssl)
    mbed = build("mbedtls", deps["mbedtls"], "mbedcrypto", ["-DENABLE_TESTING=OFF", "-DENABLE_PROGRAMS=OFF"])
    cjson = build("cjson", deps["cjson"], "cjson", ["-DBUILD_SHARED_LIBS=OFF", "-DENABLE_CJSON_TEST=OFF"])
    lib = build("auth", ROOT, "hsd_hicar_auth", [
        "-DCRYPTO_LIB=" + str(ssl / "libcrypto.a"),
        "-DCRYPTO_INCLUDE=" + str(ssl / "include") + ";" + str(deps["openssl"] / "include"),
        "-DMBEDTLS_INCLUDE=" + str(deps["mbedtls"] / "include"),
        "-DMBEDCRYPTO_LIB=" + str(mbed / "library/libmbedcrypto.a"),
        "-DCJSON_INCLUDE=" + str(deps["cjson"]), "-DCJSON_LIB=" + str(cjson / "libcjson.a")])
    shutil.copy2(lib / name, out / name)
    for dependency, src in deps.items():
        license_path = src / ("LICENSE.txt" if dependency == "openssl" else "LICENSE")
        shutil.copy2(license_path, out / (dependency + "-LICENSE.txt"))
    manifest.write_text(json.dumps(record(), indent=2) + "\n")
    package()
    print(out / name)


if __name__ == "__main__":
    main()

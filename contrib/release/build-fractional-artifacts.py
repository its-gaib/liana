#!/usr/bin/env python3
"""Package native, unsigned fractional-fee fork builds. Never publishes a release."""

import argparse
import hashlib
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
)


def zip_tree(source, destination):
    # ZipInfo created from files preserves executable permission bits on macOS.
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            archive.write(path, path.relative_to(source.parent))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package(args):
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    binaries = args.binary_dir or ROOT / "target" / args.platform / "release"
    windows = args.platform.endswith("msvc")
    names = ["liana-gui.exe"] if windows else ["liana-gui", "lianad", "liana-cli"]
    for name in names:
        if not (binaries / name).is_file():
            raise FileNotFoundError(binaries / name)
    prefix = f"liana-{args.version}-{args.platform}-noncodesigned"
    produced = []
    with tempfile.TemporaryDirectory(prefix="liana-fractional-package-") as temporary:
        workspace = Path(temporary)
        staging = workspace / prefix
        staging.mkdir()
        for name in names:
            shutil.copy2(binaries / name, staging / name)
        for name in ("README.md", "LICENCE"):
            shutil.copy2(ROOT / name, staging / name)
        (staging / "FORK-BUILD.txt").write_text(
            f"Liana fractional-fee fork {args.version}\n"
            f"Source commit: {args.commit}\n"
            f"Native platform: {args.platform}\n"
            "Rust toolchain: 1.89.0; Cargo.lock enforced\n"
            "This is a community fork build, not an official Liana release.\n"
            "Artifacts are not developer code-signed or notarized. Checksums only\n"
            "identify files; they do not provide an independent signature.\n"
            "Linux binaries are built on Ubuntu 22.04 (glibc 2.35 or newer).\n"
            "macOS binaries target macOS 14 or newer. Windows uses native MSVC.\n",
            encoding="utf-8",
        )
        if windows:
            archive = output / f"{prefix}.zip"
            zip_tree(staging, archive)
            produced.append(archive)
            executable = output / f"{prefix}.exe"
            shutil.copy2(binaries / "liana-gui.exe", executable)
            produced.append(executable)
        else:
            archive = output / f"{prefix}.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(staging, arcname=staging.name)
            produced.append(archive)
            if args.platform.endswith("linux-gnu"):
                produced.append(package_debian(staging, workspace, output, args.version))
            else:
                app = workspace / "Liana Sub-sat.app"
                contents = app / "Contents"
                (contents / "MacOS").mkdir(parents=True)
                (contents / "Resources").mkdir()
                # Read only fixed, tracked template entries; never extract arbitrary paths.
                with zipfile.ZipFile(ROOT / "contrib/release/macos/Liana.app.zip") as template:
                    info = plistlib.loads(template.read("Liana.app/Contents/Info.plist"))
                    icon = template.read("Liana.app/Contents/Resources/Liana.icns")
                info.update(
                    CFBundleName="Liana Sub-sat",
                    CFBundleDisplayName="Liana Sub-sat",
                    CFBundleIdentifier="local.liana.subsat",
                    CFBundleVersion=f"15.0.{args.version.rsplit('.', 1)[1]}",
                    LianaForkVersion=args.version,
                    CFBundleShortVersionString="15.0.0",
                    LSMinimumSystemVersion="14.0",
                )
                (contents / "Info.plist").write_bytes(plistlib.dumps(info))
                (contents / "Resources/Liana.icns").write_bytes(icon)
                shutil.copy2(binaries / "liana-gui", contents / "MacOS/Liana")
                shutil.copy2(staging / "FORK-BUILD.txt", contents / "Resources/FORK-BUILD.txt")
                archive = output / f"{prefix}-app.zip"
                zip_tree(app, archive)
                produced.append(archive)
    checksum = output / f"{prefix}-SHA256SUMS.txt"
    checksum.write_text(
        "".join(f"{sha256(path)}  {path.name}\n"
                for path in produced),
        encoding="ascii",
    )
    for path in produced + [checksum]:
        print(path)


def package_debian(staging, workspace, output, version):
    package_root = workspace / "deb"
    binary_dir = package_root / "usr/bin"
    binary_dir.mkdir(parents=True)
    for name in ("liana-gui", "lianad", "liana-cli"):
        shutil.copy2(staging / name, binary_dir / name)
        (binary_dir / name).chmod(0o755)
    docs = package_root / "usr/share/doc/liana-sub-sat"
    docs.mkdir(parents=True)
    for name in ("README.md", "LICENCE", "FORK-BUILD.txt"):
        shutil.copy2(staging / name, docs / name)
    with zipfile.ZipFile(ROOT / "contrib/release/debian/package.zip") as template:
        icon = package_root / "usr/share/icons/liana-icon.png"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(template.read("package/usr/share/icons/liana-icon.png"))
        desktop = package_root / "usr/share/applications/LianaSubSat.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(
            template.read("package/usr/share/applications/Liana.desktop").decode()
            .replace("Name=Liana", "Name=Liana Sub-sat"), encoding="utf-8"
        )
    control = (
        "Package: liana-sub-sat\n"
        f"Version: {version}-1\n"
        "Architecture: amd64\n"
        "Maintainer: Liana Sub-sat fork builders <noreply@invalid.example>\n"
        "Conflicts: liana\n"
        "Priority: optional\n"
        "Section: utils\n"
        "Description: Community Liana fork with fractional transaction fees\n"
        " Native Ubuntu 22.04 build; not an official Liana release.\n"
    )
    # Derive actual shared-library requirements on the build host, rather than
    # claiming the older glibc baseline of upstream's reproducible builds.
    debian = workspace / "debian"
    debian.mkdir()
    (debian / "control").write_text("Source: liana-sub-sat\n\n" + control, encoding="utf-8")
    dependencies = subprocess.check_output(
        ["dpkg-shlibdeps", "-O"] + [f"-e{binary_dir / name}" for name in ("liana-gui", "lianad", "liana-cli")],
        cwd=workspace, text=True,
    )
    depends = next(line.removeprefix("shlibs:Depends=") for line in dependencies.splitlines()
                   if line.startswith("shlibs:Depends="))
    if not depends:
        raise RuntimeError("dpkg-shlibdeps returned no shared-library dependencies")
    metadata = package_root / "DEBIAN"
    metadata.mkdir()
    (metadata / "control").write_text(control + f"Depends: {depends}\n", encoding="utf-8")
    package_path = output / f"liana-sub-sat_{version}-1_amd64.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(package_root), str(package_path)], check=True)
    return package_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--binary-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "fractional-release-assets")
    args = parser.parse_args()
    if not re.fullmatch(r"15\.0\.0-sub-sat\.[1-9][0-9]*", args.version):
        parser.error("expected a 15.0.0-sub-sat.N fork version")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("expected a full lowercase Git commit SHA")
    package(args)

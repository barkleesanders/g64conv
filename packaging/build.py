"""Build a native portable archive using the current Python environment.

Run on the target operating system and architecture; PyInstaller does not
cross-compile. The output contains Python and PyAV, but requires the external
ffmpeg and ffprobe commands on PATH.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def native_target() -> tuple[str, str]:
    systems = {"Linux": "linux", "Darwin": "macos"}
    architectures = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}
    try:
        return systems[platform.system()], architectures[platform.machine()]
    except KeyError as exc:
        raise SystemExit(f"Unsupported native target: {platform.system()} {platform.machine()}") from exc


def add_notices(bundle: Path) -> None:
    """Preserve the licenses supplied by every bundled distribution."""
    notices = bundle / "licenses"
    notices.mkdir()
    for name in ("av", "pyinstaller"):
        distribution = importlib.metadata.distribution(name)
        copied = 0
        for item in distribution.files or []:
            if any(part.lower().startswith(("license", "copying", "copyright")) for part in item.parts):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    destination = notices / name / str(item).replace("../", "")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    copied += 1
        if not copied:
            raise RuntimeError(f"No license found in the installed {name} distribution")
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python LICENSE.txt missing; cannot package without its notice")
    shutil.copy2(python_license, notices / "PYTHON-LICENSE.txt")
    shutil.copy2(ROOT / "packaging" / "THIRD-PARTY.txt", bundle / "THIRD-PARTY.txt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    system, architecture = native_target()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve() / f"g64conv-{system}-{architecture}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="g64conv-build-") as temporary:
        build = Path(temporary)
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--noupx",
             "--name", "g64conv", "--paths", str(ROOT / "src"), "--collect-all", "av",
             "--copy-metadata", "av", "--distpath", str(build / "dist"), "--workpath", str(build / "work"),
             "--specpath", str(build), str(ROOT / "packaging" / "entrypoint.py")],
            cwd=ROOT, check=True, timeout=600,
        )
        bundle = build / "dist" / "g64conv"
        for name in ("LICENSE", "README.md"):
            shutil.copy2(ROOT / name, bundle / name)
        add_notices(bundle)
        (bundle / "BUILD-INFO.json").write_text(json.dumps({
            "g64conv": importlib.metadata.version("g64conv"),
            "python": platform.python_version(),
            "pyav": importlib.metadata.version("av"),
            "pyinstaller": importlib.metadata.version("pyinstaller"),
            "system": system,
            "architecture": architecture,
            "build_os": platform.platform(),
        }, indent=2) + "\n")
        # Keep symlinks intact: PyInstaller uses them for native shared libraries.
        with tarfile.open(output, "w:gz", dereference=False) as archive:
            archive.add(bundle, arcname="g64conv")
    print(output)


if __name__ == "__main__":
    main()

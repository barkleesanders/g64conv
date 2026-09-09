"""Exercise the real shell installer with local release downloads, never the network."""

import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The shell installer targets macOS and Linux; Windows uses the portable ZIP")

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


@pytest.fixture
def installation(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("sh", "tar", "gzip", "mktemp", "awk", "mkdir", "rm", "ln", "mv", "readlink", "cp"):
        (commands / name).symlink_to(shutil.which(name))
    hasher = "sha256sum" if shutil.which("sha256sum") else "shasum"
    (commands / hasher).symlink_to(shutil.which(hasher))

    def command(name, content):
        path = commands / name
        path.write_text("#!/bin/sh\nset -eu\n" + content)
        path.chmod(0o755)

    command("uname", 'case "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n')
    command("ffmpeg", "exit 0\n")
    command("ffprobe", "exit 0\n")
    command("curl", '''
while [ "$#" -gt 0 ]; do
    case "$1" in
      https://*) url=$1 ;;
      -o) shift; output=$1 ;;
    esac
    shift
done
printf '%s\\n' "$url" >> "$FIXTURE_DOWNLOADS"
cp "$FIXTURE_RELEASE/${url##*/}" "$output"
''')
    release = tmp_path / "release"
    release.mkdir()
    bundle = tmp_path / "g64conv"
    bundle.mkdir()
    executable = bundle / "g64conv"
    executable.write_text("#!/bin/sh\necho 'g64conv 0.2.0'\n")
    executable.chmod(0o755)
    asset = release / "g64conv-linux-x86_64.tar.gz"
    with tarfile.open(asset, "w:gz") as archive:
        archive.add(bundle, arcname="g64conv")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    (release / "SHA256SUMS").write_text(f"{digest}  {asset.name}\n")
    prefix = tmp_path / "prefix with spaces"
    downloads = tmp_path / "downloads.log"
    env = {**os.environ, "PATH": str(commands), "G64CONV_PREFIX": str(prefix),
           "G64CONV_VERSION": "v0.2.0", "FIXTURE_RELEASE": str(release),
           "FIXTURE_DOWNLOADS": str(downloads)}

    def install():
        return subprocess.run(["/bin/sh", str(INSTALLER)], env=env, capture_output=True, text=True, check=False)

    return install, prefix, release, commands, downloads, env


def test_install_and_update_keep_running_version(installation):
    install, prefix, _, _, downloads, _ = installation
    result = install()
    assert result.returncode == 0, result.stderr
    binary = prefix / "bin/g64conv"
    assert binary.is_symlink()
    previous = binary.resolve()
    assert subprocess.check_output([str(binary), "--version"], text=True).strip() == "g64conv 0.2.0"
    result = install()
    assert result.returncode == 0, result.stderr
    assert binary.resolve() != previous
    assert previous.is_file()
    assert "/download/v0.2.0/" in downloads.read_text()


def test_bad_checksum_preserves_previous_installation(installation):
    install, prefix, release, _, _, _ = installation
    assert install().returncode == 0
    previous = (prefix / "bin/g64conv").resolve()
    with (release / "g64conv-linux-x86_64.tar.gz").open("ab") as archive:
        archive.write(b"tampered")
    result = install()
    assert result.returncode != 0
    assert "Checksum mismatch" in result.stderr
    assert (prefix / "bin/g64conv").resolve() == previous


@pytest.mark.parametrize("symlink", [False, True])
def test_refuses_other_installations(installation, symlink):
    install, prefix, _, _, downloads, _ = installation
    binary = prefix / "bin/g64conv"
    binary.parent.mkdir(parents=True)
    if symlink:
        binary.symlink_to("/another/tool/g64conv")
    else:
        binary.write_text("unrelated tool")
    result = install()
    assert result.returncode != 0
    assert "already exists" in result.stderr or "another installation" in result.stderr
    assert not downloads.exists()


def test_missing_ffprobe_explains_dependency_before_download(installation):
    install, _, _, commands, downloads, _ = installation
    (commands / "ffprobe").unlink()
    result = install()
    assert result.returncode != 0
    assert "Install FFmpeg first" in result.stderr
    assert not downloads.exists()


def test_invalid_version_never_downloads(installation):
    install, _, _, _, downloads, env = installation
    env["G64CONV_VERSION"] = "v0.2.0/../../bad"
    result = install()
    assert result.returncode != 0
    assert "Invalid G64CONV_VERSION" in result.stderr
    assert not downloads.exists()


def test_failed_executable_keeps_previous_installation(installation):
    install, prefix, release, _, _, _ = installation
    assert install().returncode == 0
    previous = (prefix / "bin/g64conv").resolve()
    candidate = release / "bad-candidate"
    candidate.mkdir()
    (candidate / "g64conv").write_text("#!/bin/sh\nexit 126\n")
    (candidate / "g64conv").chmod(0o755)
    asset = release / "g64conv-linux-x86_64.tar.gz"
    with tarfile.open(asset, "w:gz") as archive:
        archive.add(candidate, arcname="g64conv")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    (release / "SHA256SUMS").write_text(f"{digest}  {asset.name}\n")
    result = install()
    assert result.returncode != 0
    assert "previous installation preserved" in result.stderr
    assert (prefix / "bin/g64conv").resolve() == previous
    assert subprocess.check_output([str(previous), "--version"], text=True).strip() == "g64conv 0.2.0"
    assert len(list((prefix / "lib/g64conv").iterdir())) == 1

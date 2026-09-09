"""Exercise a frozen executable from outside the source/build environment.

Usage: python packaging/smoke.py /path/to/unpacked/g64conv/g64conv
Only the Python standard library is used by this harness. All application
operations run through the supplied executable, never through Python imports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def run(arguments: list[str], directory: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(arguments, cwd=directory, env=environment, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f"{arguments!r} exited {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def frame_hashes(ffmpeg: str, path: Path, directory: Path, environment: dict[str, str]) -> list[str]:
    output = run([ffmpeg, "-v", "error", "-noautorotate", "-i", str(path), "-f", "framemd5", "-"], directory, environment)
    return [line.rsplit(",", 1)[1].strip() for line in output.splitlines() if line and not line.startswith("#")]


def add_rotation_fixture(source: Path) -> None:
    """Add a 90-degree decoder message to the synth writer's v5.31 fixture.

    This deliberately uses only struct/zipfile, so the test harness never
    imports the installed application to make the frozen executable work.
    Layout: format.py parse_header/split_rtp/decoder_message_rotation.
    """
    with zipfile.ZipFile(source) as archive:
        members = [(item, archive.read(item)) for item in archive.infolist()]
    with zipfile.ZipFile(source, "w") as archive:
        for item, data in members:
            transformed = data
            if item.filename.endswith(".g64"):
                assert data[:30] == b"Genetec Omnicast Archive v5.31"
                offset = 30 + 8 + 52
                for _ in range(2):  # collection and encoder GUID + UTF-16 name
                    offset += 16
                    length = struct.unpack_from("<i", data, offset)[0]
                    offset += 4 + length * 2
                # Remaining GUIDs, empty properties/XML/watermark, seek marker/padding.
                offset += 3 * 16 + 4 + 4 + 1 + 2 + 1 + 4
                filetime, flags, size = struct.unpack_from("<qBI", data, offset)
                payload = data[offset + 13:offset + 13 + size]
                assert struct.unpack_from("<I", data, offset + 13 + size)[0] == size + 13
                decoder = bytes(4) + struct.pack(">hhhII", 0, 4, 0, 0, 2) + b"90"
                rtp = struct.pack(">BBHII", 0x80, 103, 0, 0, 0) + decoder
                subpacket = struct.pack(">BBH", 0, 24, len(rtp)) + bytes(2) + rtp
                payload = payload[:12] + subpacket + payload[12:]
                replacement = struct.pack("<qBI", filetime, flags, len(payload)) + payload
                replacement += struct.pack("<I", len(payload) + 13)
                transformed = data[:offset] + replacement + data[offset + 13 + size + 4:]
            archive.writestr(item, transformed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = str(args.executable.resolve(strict=True))
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("Smoke test requires ffmpeg and ffprobe")
    with tempfile.TemporaryDirectory(prefix="g64conv-smoke-") as temporary:
        directory = Path(temporary)
        tool_bin = directory / "tools"
        tool_bin.mkdir()
        (tool_bin / "ffmpeg").symlink_to(ffmpeg)
        (tool_bin / "ffprobe").symlink_to(ffprobe)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("PYTHON", "VIRTUAL_ENV", "CONDA", "LD_LIBRARY_PATH", "DYLD_"))}
        # No Python or developer package directories on PATH. The application
        # must use its own interpreter and PyAV, while external tools stay reachable.
        environment["PATH"] = str(tool_bin)
        version = run([executable, "--version"], directory, environment).strip()
        if not version.startswith("g64conv "):
            raise AssertionError(version)
        video = directory / "pattern.mp4"
        run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], directory, environment)
        reference = directory / "reference.h264"
        run([ffmpeg, "-v", "error", "-i", str(video), "-an", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "23", "-g", "12", "-bf", "0", "-x264-params", "repeat-headers=1",
             "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(reference)], directory, environment)
        source = directory / "synthetic.g64x"
        run([executable, "synth", str(video), str(source), "--segment-seconds", "0.7"], directory, environment)
        add_rotation_fixture(source)
        report = directory / "report.json"
        run([executable, "convert", str(source), "-o", str(directory / "out"), "--report", str(report)],
            directory, environment)
        results = json.loads(report.read_text())
        assert len(results) == 1, results
        converted = results[0]
        assert converted["status"] == "OK", converted
        assert converted["frames_written"] == 20 and converted["frames_damaged"] == 0, converted
        assert converted["verify"]["mp4_decoded_frames"] == 20, converted
        assert converted["genetec_rotation_deg"] == 90, converted
        assert converted["verify"]["mp4_rotation"] % 360 == 270, converted
        expected = frame_hashes(ffmpeg, reference, directory, environment)
        actual = frame_hashes(ffmpeg, Path(converted["output"]), directory, environment)
        assert len(expected) == 20 and actual == expected, (actual, expected)
        with (directory / "gui.log").open("w+") as log:
            process = subprocess.Popen([executable, "gui", "--no-browser", "--port", "0", "-o", str(directory / "out")],
                                       cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 20
                while True:
                    log.seek(0)
                    text = log.read()
                    match = re.search(r"http://127\.0\.0\.1:\d+/", text)
                    if match:
                        break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError(f"Frozen GUI failed to start:\n{text}")
                    time.sleep(0.1)
                base = match.group(0)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(base, timeout=5) as response:
                    assert response.status == 200 and b"<title>g64conv</title>" in response.read()
                with opener.open(base + "api/state", timeout=5) as response:
                    state = json.load(response)
                assert state["tools"]["ffmpeg"] is True, state
                assert any(item["name"].endswith(".mp4") for item in state["outputs"]), state
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        print(f"PASS {version}: frozen synth/convert, 20 identical decoded frames, 90-degree rotation, GUI page and output listing")


if __name__ == "__main__":
    main()

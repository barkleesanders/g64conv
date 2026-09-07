"""External tool discovery and small ffmpeg/ffprobe helpers."""

from __future__ import annotations

import json
import shutil
import subprocess


class MissingDependency(Exception):
    pass


def require(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise MissingDependency(f"{name} not found on PATH (install ffmpeg: https://ffmpeg.org)")
    return p


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("text", True)
    return subprocess.run(cmd, capture_output=True, **kw)


def ffprobe_json(path: str, *entries: str, count_frames: bool = False) -> dict:
    cmd = [require("ffprobe"), "-v", "error"]
    if count_frames:
        cmd += ["-count_frames", "-count_packets"]
    cmd += ["-select_streams", "v:0", "-show_entries", ":".join(entries), "-of", "json", path]
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError("ffprobe failed: " + p.stderr.strip())
    j = json.loads(p.stdout or "{}")
    j["_stderr"] = [l for l in p.stderr.splitlines() if l.strip()]
    return j


def ffmpeg_version() -> str:
    p = run([require("ffmpeg"), "-version"])
    return p.stdout.splitlines()[0] if p.stdout else "unknown"

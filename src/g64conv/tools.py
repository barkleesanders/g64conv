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
    return subprocess.run(cmd, capture_output=True, check=False, **kw)


def ffprobe_json(path: str, *entries: str, count_frames: bool = False) -> dict:
    cmd = [require("ffprobe"), "-v", "error"]
    if count_frames:
        cmd += ["-count_frames", "-count_packets"]
    cmd += ["-select_streams", "v:0", "-show_entries", ":".join(entries), "-of", "json", path]
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError("ffprobe failed: " + p.stderr.strip())
    j = json.loads(p.stdout or "{}")
    j["_stderr"] = [ln for ln in p.stderr.splitlines() if ln.strip()]
    return j


def ffmpeg_version() -> str:
    p = run([require("ffmpeg"), "-version"])
    return p.stdout.splitlines()[0] if p.stdout else "unknown"


def run_ffmpeg_progress(cmd: list[str], duration_s: float | None, on_progress=None) -> subprocess.CompletedProcess:
    """Run an ffmpeg command, reporting completion fraction through ``on_progress``.

    ``cmd`` is a full ffmpeg command line whose LAST element is the output;
    ``-progress pipe:1 -nostats`` is inserted before it so ffmpeg streams
    ``out_time_us=`` lines on stdout while errors still go to stderr. The
    fraction is ``out_time / duration_s`` (clamped); without a duration the
    callback receives -1 (unknown)."""
    if on_progress is None:
        return run(cmd)
    full = [*cmd[:-1], "-progress", "pipe:1", "-nostats", cmd[-1]]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            # ffmpeg labels both fields in microseconds (out_time_ms is a historical misnomer)
            if duration_s and duration_s > 0:
                on_progress(max(0.0, min(1.0, us / 1_000_000 / duration_s)))
            else:
                on_progress(-1.0)
        elif line == "progress=end":
            on_progress(1.0)
    err = proc.stderr.read() if proc.stderr else ""
    rc = proc.wait()
    return subprocess.CompletedProcess(full, rc, stdout="", stderr=err)


def media_duration(path: str) -> float | None:
    try:
        j = ffprobe_json(path, "format=duration")
        return float(j.get("format", {}).get("duration", 0)) or None
    except (RuntimeError, ValueError, TypeError):
        return None

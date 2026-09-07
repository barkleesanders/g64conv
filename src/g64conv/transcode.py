"""Turn the verified MP4 into the other containers/formats people actually need."""

from __future__ import annotations

import os

from .tools import media_duration, require, run_ffmpeg_progress

FORMATS = ("mp4", "mkv", "mov", "webm", "gif", "frames", "hls")


def transcode(mp4_path: str, fmt: str, out_dir: str | None = None, *, fps: float | None = None,
              scale_width: int | None = None, progress=None) -> str:
    """Return the path (file or directory) of the produced output.
    ``progress(fraction)`` is called while ffmpeg runs when given."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")
    if fmt == "mp4":
        return mp4_path
    ffmpeg = require("ffmpeg")
    out_dir = out_dir or os.path.dirname(mp4_path)
    stem = os.path.splitext(os.path.basename(mp4_path))[0]
    vf = []
    if scale_width:
        vf.append(f"scale={scale_width}:-2")
    if fps:
        vf.append(f"fps={fps:g}")
    base = [ffmpeg, "-v", "error", "-y", "-i", mp4_path]
    if fmt in ("mkv", "mov"):
        out = os.path.join(out_dir, f"{stem}.{fmt}")
        cmd = base + ["-c", "copy", out]          # lossless container change
    elif fmt == "webm":
        out = os.path.join(out_dir, f"{stem}.webm")
        cmd = base + (["-vf", ",".join(vf)] if vf else []) + [
            "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-an", out]
    elif fmt == "gif":
        out = os.path.join(out_dir, f"{stem}.gif")
        f = ",".join(vf + ["split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer"])
        cmd = base + ["-filter_complex", f, "-loop", "0", out]
    elif fmt == "frames":
        out = os.path.join(out_dir, f"{stem}_frames")
        os.makedirs(out, exist_ok=True)
        cmd = base + (["-vf", ",".join(vf)] if vf else []) + [
            "-frame_pts", "1", os.path.join(out, "frame_%d.png")]
    else:  # hls
        out = os.path.join(out_dir, f"{stem}_hls")
        os.makedirs(out, exist_ok=True)
        cmd = base + ["-c", "copy", "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
                      "-hls_segment_filename", os.path.join(out, "seg_%05d.ts"),
                      os.path.join(out, "index.m3u8")]
    p = run_ffmpeg_progress(cmd, media_duration(mp4_path) if progress else None, progress)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg {fmt} failed: {p.stderr.strip()}")
    return out

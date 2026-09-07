"""Fisheye dewarping with ffmpeg's v360 filter.

Two mountings exist for these cameras and they need different projections:

* **ceiling** — the camera looks straight down. The floor fills the middle of
  the circle, the walls form a ring near the rim, the rim is the horizon. The
  vendor client's "double panorama" splits that circle into two normal-looking
  180-degree strips:

      v360=fisheye:hequirect:ih_fov=FOV:iv_fov=FOV:rorder=pyr:pitch=90:yaw={0|180}

  ``pitch=90`` swings the view from straight-down to the rim so the walls stand
  upright above the floor; ``yaw`` picks the half.

* **wall** — the camera looks horizontally out of a wall. Only the lower
  hemisphere carries picture (the upper half of the circle is black: it would
  show the wall/ceiling and the camera masks it). The horizon runs through the
  centre of the circle and people already stand upright, so no swing is
  needed; one hequirect view of the hemisphere straightens the verticals:

      v360=fisheye:hequirect:ih_fov=FOV:iv_fov=FOV:pitch=0:yaw=0

Both outputs are linear in elevation (no cylindrical stretching) and cropped
to the band from a little above the horizon down to the nadir, which is where
a downward-looking view actually has picture. ``mount="auto"`` measures a
frame: a dark upper half over a lit lower half is a wall mount. Verified on
synthetic scenes with an asymmetric marker: wall order and handedness are
preserved (no mirror) in both mountings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .tools import ffprobe_json, require, run

MODES = ("double", "panorama")
MOUNTS = ("auto", "ceiling", "wall")


@dataclass
class DewarpSpec:
    mode: str = "double"        # ceiling mount: "double" (two 180 views) or "panorama" (one 360 strip)
    mount: str = "auto"         # "ceiling", "wall", or "auto" (measure a frame)
    fov: float = 180.0          # lens field of view assumed for the circular image
    width: int = 0              # output width per view (0 = auto from input)
    above_horizon_deg: float = 15.0
    crf: int = 18
    preset: str = "medium"


def detect_mount(input_path: str, at: float | None = None) -> str:
    """'wall' when the upper half of a frame is dark over a lit lower half
    (the camera masks the hemisphere it cannot see), else 'ceiling'."""
    ffmpeg = require("ffmpeg")
    if at is None:
        j = ffprobe_json(input_path, "format=duration")
        try:
            at = float(j.get("format", {}).get("duration", 0)) * 0.1
        except (TypeError, ValueError):
            at = 0.0
    p = run([ffmpeg, "-v", "error", "-noautorotate", "-ss", f"{at:.3f}", "-i", input_path, "-frames:v", "1",
             "-vf", "scale=32:32:flags=area", "-f", "rawvideo", "-pix_fmt", "gray", "-"], text=False)
    if p.returncode != 0 or len(p.stdout) < 32 * 32:
        raise RuntimeError("could not read a frame to detect the mounting: " + p.stderr.decode(errors="replace").strip())
    px = p.stdout[: 32 * 32]
    # only the middle 16 columns: the corners of a square fisheye frame are black in both mountings
    rows = [px[r * 32 + 8: r * 32 + 24] for r in range(32)]
    top = sum(sum(r) for r in rows[2:16]) / (14 * 16)
    bottom = sum(sum(r) for r in rows[16:30]) / (14 * 16)
    # "black" is the frame's own black level, taken from the corners outside the circle:
    # limited-range video black is 16, not 0 (measured 19 on a real camera's masked half).
    corners = [px[r * 32 + c] for r in (0, 1, 30, 31) for c in (0, 1, 30, 31)]
    black = sum(corners) / len(corners)
    return "wall" if bottom - black > 20 and top - black < 0.15 * (bottom - black) else "ceiling"


def _vf(spec: DewarpSpec, yaw: int, w: int, h_fov: float, pitch: int = 90) -> str:
    # hequirect/equirect map elevation linearly: 180 deg over the full height.
    full_h = w * 180 // int(h_fov)
    full_h -= full_h % 2
    keep = spec.above_horizon_deg + 90.0
    crop_h = int(full_h * keep / 180.0)
    crop_h -= crop_h % 2
    out = "hequirect" if h_fov == 180 else "equirect"
    # rorder=pyr: pitch first (swing the view to the rim), THEN yaw about the new vertical
    # axis to pick the half. With the default order yaw rotates about the fisheye axis
    # before the pitch and both halves come out identical (measured on the fixture).
    return (f"v360=fisheye:{out}:ih_fov={spec.fov:g}:iv_fov={spec.fov:g}:rorder=pyr:pitch={pitch}:yaw={yaw}"
            f":w={w}:h={full_h},crop={w}:{crop_h}:0:{full_h - crop_h}")


def dewarp(input_path: str, out_dir: str, spec: DewarpSpec | None = None,
           stem: str | None = None) -> list[str]:
    """Write dewarped view(s) of a fisheye video. Returns output paths."""
    spec = spec or DewarpSpec()
    if spec.mode not in MODES:
        raise ValueError(f"unknown dewarp mode {spec.mode!r}")
    if spec.mount not in MOUNTS:
        raise ValueError(f"unknown mount {spec.mount!r}")
    ffmpeg = require("ffmpeg")
    j = ffprobe_json(input_path, "stream=width,height")
    st = j["streams"][0]
    in_w = int(st["width"])
    stem = stem or os.path.splitext(os.path.basename(input_path))[0]
    mount = detect_mount(input_path) if spec.mount == "auto" else spec.mount
    os.makedirs(out_dir, exist_ok=True)
    outs = []
    if mount == "wall":
        w = spec.width or (in_w * 2 - (in_w * 2) % 2)
        jobs = [("wall", 0, 180.0, 0)]
    elif spec.mode == "double":
        w = spec.width or (in_w * 2 - (in_w * 2) % 2)
        jobs = [("A", 0, 180.0, 90), ("B", 180, 180.0, 90)]
    else:
        w = spec.width or (in_w * 4 - (in_w * 4) % 2)
        jobs = [("pano", 0, 360.0, 90)]
    for tag, yaw, h_fov, pitch in jobs:
        out = os.path.join(out_dir, f"{stem}_{tag}.mp4")
        # -noautorotate: the source's display-rotation tag describes the fisheye as mounted,
        # which is irrelevant to the projection; the circle is dewarped as recorded.
        p = run([ffmpeg, "-v", "error", "-y", "-noautorotate", "-i", input_path,
                 "-vf", _vf(spec, yaw, w, h_fov, pitch), "-c:v", "libx264", "-preset", spec.preset,
                 "-crf", str(spec.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg dewarp failed for {tag}: {p.stderr.strip()}")
        outs.append(out)
    return outs

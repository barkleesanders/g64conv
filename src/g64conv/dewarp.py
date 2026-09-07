"""Fisheye dewarping with ffmpeg's v360 filter.

A ceiling-mounted 360 camera records a circular fisheye image: the floor fills
the middle, the walls form a ring near the rim, and the rim itself is the
horizon. The vendor client offers a "double panorama" view that splits that
circle into two normal-looking 180-degree strips. The same thing here:

  v360=fisheye:hequirect:ih_fov=FOV:iv_fov=FOV:rorder=pyr:pitch=90:yaw={0|180}

``pitch=90`` swings the view from straight-down to the rim so the walls stand
upright above the floor; ``yaw`` picks the half. The half-equirectangular
output is linear in elevation (no cylindrical stretching), and it is cropped to
the elevation band from a little above the horizon down to the nadir, which is
where a downward camera actually has picture. Verified on a synthetic scene
with an asymmetric marker: wall order and handedness are preserved (no mirror).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .tools import ffprobe_json, require, run

MODES = ("double", "panorama")


@dataclass
class DewarpSpec:
    mode: str = "double"
    fov: float = 180.0          # lens field of view assumed for the circular image
    width: int = 0              # output width per view (0 = auto from input)
    above_horizon_deg: float = 15.0
    crf: int = 18
    preset: str = "medium"


def _vf(spec: DewarpSpec, yaw: int, w: int, h_fov: float) -> str:
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
    return (f"v360=fisheye:{out}:ih_fov={spec.fov:g}:iv_fov={spec.fov:g}:rorder=pyr:pitch=90:yaw={yaw}"
            f":w={w}:h={full_h},crop={w}:{crop_h}:0:{full_h - crop_h}")


def dewarp(input_path: str, out_dir: str, spec: DewarpSpec | None = None,
           stem: str | None = None) -> list[str]:
    """Write dewarped view(s) of a fisheye video. Returns output paths."""
    spec = spec or DewarpSpec()
    if spec.mode not in MODES:
        raise ValueError(f"unknown dewarp mode {spec.mode!r}")
    ffmpeg = require("ffmpeg")
    j = ffprobe_json(input_path, "stream=width,height")
    st = j["streams"][0]
    in_w = int(st["width"])
    stem = stem or os.path.splitext(os.path.basename(input_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    outs = []
    if spec.mode == "double":
        w = spec.width or (in_w * 2 - (in_w * 2) % 2)
        jobs = [("A", 0, 180.0), ("B", 180, 180.0)]
    else:
        w = spec.width or (in_w * 4 - (in_w * 4) % 2)
        jobs = [("pano", 0, 360.0)]
    for tag, yaw, h_fov in jobs:
        out = os.path.join(out_dir, f"{stem}_{tag}.mp4")
        # -noautorotate: the source's display-rotation tag describes the fisheye as mounted,
        # which is irrelevant to the projection; the circle is dewarped as recorded.
        p = run([ffmpeg, "-v", "error", "-y", "-noautorotate", "-i", input_path,
                 "-vf", _vf(spec, yaw, w, h_fov), "-c:v", "libx264", "-preset", spec.preset,
                 "-crf", str(spec.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg dewarp failed for {tag}: {p.stderr.strip()}")
        outs.append(out)
    return outs

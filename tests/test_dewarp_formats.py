"""Dewarp geometry (wall order, handedness) and multi-format output."""
import os

from g64conv.dewarp import DewarpSpec, detect_mount, dewarp
from g64conv.transcode import transcode

from conftest import mean_rgb, probe_int


def _closest(rgb, palette):
    return min(palette, key=lambda k: sum((a - b) ** 2 for a, b in zip(rgb, palette[k])))


WALLS = {"red": (0xC0, 0x40, 0x40), "green": (0x40, 0xC0, 0x40), "blue": (0x40, 0x40, 0xC0), "yellow": (0xC0, 0xC0, 0x40)}


def test_double_view_wall_order_and_no_mirror(tmp_path, fisheye_video, ffmpeg):
    outs = dewarp(fisheye_video, str(tmp_path), DewarpSpec(mode="double", mount="ceiling", width=720))
    assert [os.path.basename(o) for o in outs] == ["fisheye_A.mp4", "fisheye_B.mp4"]
    a, b = outs
    w = 720
    # view A: left third blue, middle red, right third green (as seen standing in the room)
    band = 0.22  # just below the horizon crop -> wall band
    h = probe_int(a, "height")
    y = int(h * band)
    assert _closest(mean_rgb(ffmpeg, a, 60, y, 100, 20), WALLS) == "blue"
    assert _closest(mean_rgb(ffmpeg, a, w // 2 - 50, y, 100, 20), WALLS) == "red"
    assert _closest(mean_rgb(ffmpeg, a, w - 160, y, 100, 20), WALLS) == "green"
    # the white marker sits at the top-LEFT of the red wall (source yaw -43..-29 deg, elevation
    # -1..-16 deg). In view A that is x = (yaw+90)/180*w = 188..244 and y = (15-elev)/105*h.
    # A mirrored dewarp would put it at the symmetric spot on the right (x ~ 476..532).
    left = mean_rgb(ffmpeg, a, 196, int(h * 0.18), 40, int(h * 0.08))
    right = mean_rgb(ffmpeg, a, 484, int(h * 0.18), 40, int(h * 0.08))
    assert sum(left) > sum(right) + 150, (left, right)
    # view B is the opposite half: yellow back wall in the middle
    assert _closest(mean_rgb(ffmpeg, b, w // 2 - 50, y, 100, 20), WALLS) == "yellow"
    assert probe_int(a, "nb_read_frames") == 5


def test_panorama_mode(tmp_path, fisheye_video, ffmpeg):
    outs = dewarp(fisheye_video, str(tmp_path), DewarpSpec(mode="panorama", mount="ceiling", width=1440))
    assert len(outs) == 1
    assert probe_int(outs[0], "width") == 1440


def test_mount_detection(fisheye_video, wall_fisheye_video):
    assert detect_mount(fisheye_video) == "ceiling"
    assert detect_mount(wall_fisheye_video) == "wall"


def test_wall_mount_view_order_and_no_mirror(tmp_path, wall_fisheye_video, ffmpeg):
    # mount=auto must pick "wall" from the masked upper hemisphere and emit ONE view
    outs = dewarp(wall_fisheye_video, str(tmp_path), DewarpSpec(mount="auto", width=720))
    assert [os.path.basename(o) for o in outs] == ["wallfisheye_wall.mp4"]
    v = outs[0]; w = 720
    h = probe_int(v, "height")
    y = int(h * 0.22)
    # facing the front wall: blue on the left quarter, red across the middle, green on the right
    assert _closest(mean_rgb(ffmpeg, v, 30, y, 100, 20), WALLS) == "blue"
    assert _closest(mean_rgb(ffmpeg, v, w // 2 - 50, y, 100, 20), WALLS) == "red"
    assert _closest(mean_rgb(ffmpeg, v, w - 130, y, 100, 20), WALLS) == "green"
    # same marker geometry as the ceiling view: x = (yaw+90)/180*w, y = (15-elev)/105*h
    left = mean_rgb(ffmpeg, v, 196, int(h * 0.18), 40, int(h * 0.08))
    right = mean_rgb(ffmpeg, v, 484, int(h * 0.18), 40, int(h * 0.08))
    assert sum(left) > sum(right) + 150, (left, right)
    # the floor (grey) is at the bottom, not the masked ceiling (black)
    floor = mean_rgb(ffmpeg, v, w // 2 - 50, h - 30, 100, 20)
    assert 0x20 < floor[0] < 0x50 and abs(floor[0] - floor[2]) < 12, floor


def test_all_formats(tmp_path, sample_video):
    produced = {}
    for fmt in ("mkv", "mov", "webm", "gif", "frames", "hls"):
        produced[fmt] = transcode(sample_video, fmt, str(tmp_path))
    assert probe_int(produced["mkv"], "nb_read_frames") == 20
    assert probe_int(produced["mov"], "nb_read_frames") == 20
    assert probe_int(produced["webm"], "nb_read_frames") == 20
    assert os.path.getsize(produced["gif"]) > 1000
    assert len([f for f in os.listdir(produced["frames"]) if f.endswith(".png")]) == 20
    assert os.path.exists(os.path.join(produced["hls"], "index.m3u8"))
    assert any(f.endswith(".ts") for f in os.listdir(produced["hls"]))

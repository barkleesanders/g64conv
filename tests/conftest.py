import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def ffmpeg():
    p = shutil.which("ffmpeg")
    if not p:
        pytest.skip("ffmpeg not installed")
    return p


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory, ffmpeg):
    """2 s of ffmpeg's moving test pattern at 10 fps, 320x240."""
    p = tmp_path_factory.mktemp("src") / "testsrc.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
    return str(p)


def _room_pano(ffmpeg, path):
    vf = (
        "drawbox=x=0:y=0:w=2048:h=512:c=0x101010:t=fill,"
        "drawbox=x=768:y=512:w=512:h=248:c=0xc04040:t=fill,"     # front (red), yaw 0
        "drawbox=x=1280:y=512:w=512:h=248:c=0x40c040:t=fill,"    # right (green)
        "drawbox=x=256:y=512:w=512:h=248:c=0x4040c0:t=fill,"     # left (blue)
        "drawbox=x=0:y=512:w=256:h=248:c=0xc0c040:t=fill,"       # back (yellow)
        "drawbox=x=1792:y=512:w=256:h=248:c=0xc0c040:t=fill,"
        "drawbox=x=780:y=520:w=80:h=80:c=white:t=fill"           # marker: top-left of the front wall
    )
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x303030:s=2048x1024:d=1",
                    "-frames:v", "1", "-vf", vf, str(path)], check=True)


@pytest.fixture(scope="session")
def wall_fisheye_video(tmp_path_factory, ffmpeg):
    """The same room seen by a WALL-mounted fisheye looking horizontally at the
    front (red) wall; the upper hemisphere is masked black as such cameras do."""
    d = tmp_path_factory.mktemp("wallfish")
    pano = d / "pano.png"
    _room_pano(ffmpeg, pano)
    video = d / "wallfisheye.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-loop", "1", "-i", str(pano), "-t", "1", "-r", "5",
                    "-vf", "v360=equirect:fisheye:ih_fov=360:iv_fov=180:h_fov=180:v_fov=180:pitch=0:w=360:h=360,"
                           "drawbox=x=0:y=0:w=360:h=180:c=black:t=fill",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    return str(video)


@pytest.fixture(scope="session")
def fisheye_video(tmp_path_factory, ffmpeg):
    """A synthetic ceiling-camera fisheye: a room whose four walls are distinct
    colours (left blue, front red, right green, back yellow) with a white marker
    in the top-left corner of the front wall, projected to a 360x360 fisheye by
    a camera looking straight down. Returns (video_path, wall_colours)."""
    d = tmp_path_factory.mktemp("fish")
    pano = d / "pano.png"
    _room_pano(ffmpeg, pano)
    video = d / "fisheye.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-loop", "1", "-i", str(pano), "-t", "1", "-r", "5",
                    "-vf", "v360=equirect:fisheye:ih_fov=360:iv_fov=180:h_fov=180:v_fov=180:pitch=-90:w=360:h=360",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    return str(video)


def mean_rgb(ffmpeg, video, x, y, w, h, t=0.2):
    """Average colour of a region of one frame (no numpy needed)."""
    p = subprocess.run([ffmpeg, "-v", "error", "-ss", str(t), "-i", video, "-frames:v", "1",
                        "-vf", f"crop={w}:{h}:{x}:{y},scale=1:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, check=True)
    return tuple(p.stdout[:3])


def probe_int(video, key):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                          "-show_entries", f"stream={key}", "-of", "csv=p=0", video],
                         capture_output=True, text=True, check=True).stdout.strip()
    return int(out)


def framemd5(ffmpeg, video):
    p = subprocess.run([ffmpeg, "-v", "error", "-i", video, "-f", "framemd5", "-"], capture_output=True, text=True, check=True)
    return [l.split(",")[-1].strip() for l in p.stdout.splitlines() if l and not l.startswith("#")]

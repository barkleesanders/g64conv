"""Write a synthetic archive from a test pattern, convert it back, and prove the
decoded frames are identical to the intermediate H.264 (bit-exact copy)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import framemd5
from g64conv import format as g64
from g64conv.archive import discover
from g64conv.cli import main
from g64conv.convert import apply_display_rotation, convert_source, verify
from g64conv.writer import write_g64, write_g64x


def test_g64_header_roundtrip(tmp_path, sample_video):
    paths = write_g64(sample_video, str(tmp_path / "s.g64"), collection="unit test")
    seg = g64.Segment.parse("s.g64", Path(paths[0]).read_bytes())
    assert seg.header.startcode == "Genetec Omnicast Archive v5.31"
    assert seg.header.collection_name == "unit test"
    assert not seg.header.watermarked and not seg.header.frames_encrypted
    assert len(seg.frames) == 20  # 2 s at 10 fps
    assert seg.frames[0].keyframe_flag
    # trailer of every frame = payload size + 13, exactly as the reader expects
    for fr in seg.frames:
        trailer = int.from_bytes(seg.data[fr.payload_offset + fr.payload_size:fr.payload_offset + fr.payload_size + 4], "little")
        assert trailer == fr.payload_size + 13


def test_convert_is_bit_exact(tmp_path, sample_video, ffmpeg):
    arc = write_g64x(sample_video, str(tmp_path / "s.g64x"), segment_seconds=0.7)
    srcs = discover(arc)
    assert len(srcs) == 1 and len(srcs[0].segments) == 3  # 0-0.7, 0.7-1.4, 1.4-2.0
    r = convert_source(srcs[0], str(tmp_path))
    assert r.frames_written == 20 and r.frames_damaged == 0 and r.frames_non_video == 0
    assert (r.width, r.height) == (320, 240)
    assert verify(r), r.verify
    assert r.verify["mp4_decoded_frames"] == 20
    # the H.264 inside the archive was produced by the writer's own encode; decoding
    # our MP4 must give the same pixels as decoding that intermediate stream
    p = subprocess.run([ffmpeg, "-v", "error", "-i", sample_video, "-an", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "23", "-g", "12", "-bf", "0", "-x264-params", "repeat-headers=1", "-f", "h264",
                        str(tmp_path / "ref.h264")], capture_output=True, check=False)
    assert p.returncode == 0
    assert framemd5(ffmpeg, r.output) == framemd5(ffmpeg, str(tmp_path / "ref.h264"))
    # frame times survive: 20 frames at 100 ms => 1.9 s between first and last
    assert r.duration_s == 1.9


def test_damaged_fragment_is_reported_not_hidden(tmp_path, sample_video):
    """Drop one RTP sub-packet from a keyframe (what a recorder does under packet
    loss) and require the converter to keep the frame count and flag the frame."""
    paths = write_g64(sample_video, str(tmp_path / "d.g64"))
    data = bytearray(Path(paths[0]).read_bytes())
    seg = g64.Segment.parse("d.g64", bytes(data))
    fr = next(f for f in seg.frames if f.keyframe_flag)
    subs = list(g64.split_rtp(bytes(data), fr.payload_offset, fr.payload_size))
    assert len(subs) > 3, "keyframe must be fragmented for this test to be meaningful"
    # zero the length of the LAST FU-A sub-packet so the chain ends early (the reader's own
    # behaviour): the picture is written incomplete, exactly like a recorder that dropped a packet
    o = fr.payload_offset + 12
    for i, (_, rtp) in enumerate(subs):
        if i == len(subs) - 1:
            data[o + 2:o + 4] = b"\0\0"
            break
        o += 6 + len(rtp)
    (tmp_path / "d2.g64").write_bytes(bytes(data))
    r = convert_source(discover(str(tmp_path / "d2.g64"))[0], str(tmp_path / "out"))
    assert r.frames_written == 20
    assert r.frames_damaged == 1
    assert r.damaged_frame_times_utc == [fr.time.isoformat()]


def test_cli_json_and_exit_codes(tmp_path, sample_video):
    arc = write_g64x(sample_video, str(tmp_path / "c.g64x"))
    rc = main(["convert", arc, "-o", str(tmp_path / "out"), "--quiet", "--report", str(tmp_path / "r.json")])
    assert rc == 0
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep[0]["status"] == "OK" and rep[0]["frames_written"] == 20
    assert main(["convert", str(tmp_path / "nope.g64x"), "--quiet"]) == 2
    assert main(["convert", arc, "-o", str(tmp_path / "dry"), "--dry-run", "--quiet"]) == 0
    assert not os.path.exists(tmp_path / "dry")
    assert main(["probe", arc, "--json", "--quiet"]) == 0


def test_probe_reads_synthetic(tmp_path, sample_video, capsys):
    arc = write_g64(sample_video, str(tmp_path / "p.g64"))[0]
    assert main(["probe", arc, "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep[0]["frames"] == 20
    assert set(rep[0]["compression_types"]) == {"24"}          # GenericH264 sub-packets only
    assert "7" in rep[0]["nal_units"] and "8" in rep[0]["nal_units"]   # SPS/PPS in-band on IDR
    assert rep[0]["span_s"] == 1.9


@pytest.mark.parametrize("degrees", [90, 270])
def test_display_rotation_preserves_frames(tmp_path, sample_video, ffmpeg, degrees):
    """Exercise actual FFmpeg, including older distribution versions in native CI."""
    video = tmp_path / "rotated.mp4"
    video.write_bytes(Path(sample_video).read_bytes())
    apply_display_rotation(str(video), degrees)
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "side_data=rotation", "-of", "json", str(video)
    ], text=True))
    assert probe["streams"][0]["side_data_list"][0]["rotation"] % 360 == degrees
    # Disable playback rotation while comparing the stored pixels.
    def stored_pixels(path):
        return subprocess.check_output([
            ffmpeg, "-v", "error", "-noautorotate", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"
        ])
    assert stored_pixels(video) == stored_pixels(sample_video)

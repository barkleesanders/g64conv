"""Archive -> MP4 (bit-exact H.264 copy with the camera's own frame times)."""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

from . import format as g64
from .archive import Source, safe_name
from .h264 import (
    NAL_IDR,
    NAL_PPS,
    NAL_SPS,
    START_CODE,
    Depacketizer,
    annexb,
    sps_dimensions,
)
from .tools import ffprobe_json, require, run


@dataclass
class ConvertResult:
    source: str
    output: str
    width: int
    height: int
    frames_in_archive: int
    frames_written: int
    frames_non_video: int
    frames_damaged: int
    keyframes: int
    start_utc: str
    duration_s: float
    timezone_bias_minutes: int | None
    genetec_watermark_type: int | None
    genetec_rotation_deg: int
    ffmpeg_display_rotation_ccw: int
    rotation_messages: dict[int, int]
    damaged_frame_times_utc: list[str]
    h264_compression_types: list[int]
    segments: list[dict] = field(default_factory=list)
    verify: dict = field(default_factory=dict)
    status: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def genetec_to_ffmpeg_rotation(deg: int) -> int:
    """The archive's rotation is clockwise; ffmpeg's -display_rotation is
    counter-clockwise. 180 is its own inverse."""
    return (360 - deg) % 360


def open_output(out_path: str, sps: bytes, pps_list: list[bytes], first_nals: list[bytes]):
    """Create the MP4 and its video stream WITHOUT opening an encoder.

    PyAV's ``add_stream('h264')`` binds a libx264 encoder; the first ``mux()``
    opens it, which replaces the extradata with x264's own SPS/PPS. The MP4
    muxer then sees non-avcC extradata, treats packets as Annex-B and keeps only
    the NAL units it can find by start-code scanning (5 of 259 survived in
    testing). Seeding a raw-h264 demuxer with the real parameter sets and
    cloning that stream gives a decoder-backed stream whose extradata is the
    camera's SPS/PPS, from which the muxer builds a correct avcC."""
    import av

    body = [n for n in first_nals if (n[0] & 0x1F) not in (NAL_SPS, NAL_PPS)]
    seed = io.BytesIO(START_CODE + sps + b"".join(START_CODE + p for p in pps_list)
                      + b"".join(START_CODE + n for n in body))
    src = av.open(seed, format="h264")
    tmpl = src.streams.video[0]
    if tmpl.codec_context.width == 0:
        raise g64.G64Error("could not parse the SPS for stream dimensions")
    out = av.open(out_path, "w")
    st = out.add_stream_from_template(tmpl)
    st.time_base = Fraction(1, 1000)
    src.close()
    return out, st


def convert_source(source: Source, out_dir: str, *, keep_h264: bool = False,
                   log: Callable[[str], None] = lambda s: None,
                   progress: Callable[[int, int], None] = lambda done, total: None) -> ConvertResult:
    """``progress(done_segments, total_segments)`` is called as each segment is finished."""
    import av

    frames_total = frames_written = damaged = non_video = keyframes = 0
    t0 = None
    sps = None
    pps: list[bytes] = []
    width = height = 0
    out_container = None
    stream = None
    out_path = ""
    h264_fh = None
    last_pts = -1
    codec_seen: set[int] = set()
    tz_bias = None
    wm = None
    rotations: dict[int, int] = {}
    damaged_times: list[str] = []
    segs_info: list[dict] = []
    os.makedirs(out_dir, exist_ok=True)
    try:
        n_segs = len(source.segments)
        for seg_i, (seg_name, load) in enumerate(source.segments):
            seg = g64.Segment.parse(seg_name, load())
            h = seg.header
            if h.frames_encrypted:
                raise g64.G64Error(f"{seg_name}: frames are SRTP-encrypted; cannot convert without the key")
            tz_bias = h.tz_bias_min
            wm = h.wm_type if h.watermarked else None
            seg_first = seg_last = None
            for fr in seg.frames:
                frames_total += 1
                seg_first = seg_first or fr.filetime
                seg_last = fr.filetime
                dp = Depacketizer()
                saw_video = False
                for comp, rtp in g64.split_rtp(seg.data, fr.payload_offset, fr.payload_size):
                    pk = g64.parse_rtp(rtp)
                    if pk is None:
                        continue
                    if pk.payload_type == g64.PT_DECODER_MESSAGE:
                        r = g64.decoder_message_rotation(pk.payload)
                        if r is not None:
                            rotations[r] = rotations.get(r, 0) + 1
                        continue
                    if pk.payload_type in g64.SIDE_CHANNEL_PT:
                        continue
                    if comp in g64.HEVC_COMPRESSION:
                        raise g64.G64Error(f"{seg_name}: HEVC (compression type {comp}) is not supported yet")
                    codec_seen.add(comp)
                    saw_video = True
                    dp.add(pk.payload)
                nals = dp.finish()
                if not saw_video:
                    non_video += 1
                    continue
                if dp.dropped_fragments or not nals:
                    damaged += 1
                    damaged_times.append(fr.time.isoformat())
                if not nals:
                    continue
                for n in nals:
                    nt = n[0] & 0x1F
                    if nt == NAL_SPS and sps is None:
                        sps = n
                    elif nt == NAL_PPS and n not in pps:
                        pps.append(n)
                if stream is None:
                    has_slice = any((n[0] & 0x1F) in (1, 5) for n in nals)
                    if sps is None or not pps or not has_slice:
                        continue  # need parameter sets AND a picture before the first sample
                    width, height = sps_dimensions(sps)
                    t0 = fr.filetime
                    out_path = os.path.join(
                        out_dir, f"{safe_name(source.label)}_{fr.time.strftime('%Y-%m-%dT%H%M%SZ')}.mp4")
                    out_container, stream = open_output(out_path, sps, pps, nals)
                    if keep_h264:
                        h264_fh = open(out_path[:-4] + ".h264", "wb")
                pts = (fr.filetime - t0) // 10000  # FILETIME 100 ns -> ms
                if pts <= last_pts:
                    pts = last_pts + 1  # two samples may never share a timestamp
                last_pts = pts
                data = annexb(nals)  # Annex-B: the mov muxer converts to length-prefixed itself
                pkt = av.Packet(data)
                pkt.pts = pkt.dts = pts
                pkt.time_base = Fraction(1, 1000)
                pkt.stream = stream
                is_key = fr.keyframe_flag or any((n[0] & 0x1F) == NAL_IDR for n in nals)
                pkt.is_keyframe = is_key
                keyframes += int(is_key)
                out_container.mux(pkt)
                if h264_fh:
                    h264_fh.write(data)
                frames_written += 1
            segs_info.append({
                "segment": seg_name, "frames": len(seg.frames), "version": h.startcode,
                "start_utc": g64.filetime_to_datetime(seg_first).isoformat() if seg_first else None,
                "end_utc": g64.filetime_to_datetime(seg_last).isoformat() if seg_last else None,
            })
            log(f"   {seg_name}: {len(seg.frames)} frames")
            progress(seg_i + 1, n_segs)
    finally:
        if out_container is not None:
            out_container.close()
        if h264_fh:
            h264_fh.close()
    if stream is None or t0 is None:
        raise g64.G64Error(
            f"{source.label}: no decodable H.264 frames found (compression types seen: {sorted(codec_seen)})")
    rotation = max(rotations, key=rotations.get) % 360 if rotations else 0
    if rotation:
        apply_display_rotation(out_path, genetec_to_ffmpeg_rotation(rotation))
    return ConvertResult(
        source=source.label, output=out_path, width=width, height=height,
        frames_in_archive=frames_total, frames_written=frames_written,
        frames_non_video=non_video, frames_damaged=damaged, keyframes=keyframes,
        start_utc=g64.filetime_to_datetime(t0).isoformat(), duration_s=round(last_pts / 1000, 3),
        timezone_bias_minutes=tz_bias, genetec_watermark_type=wm,
        genetec_rotation_deg=rotation, ffmpeg_display_rotation_ccw=genetec_to_ffmpeg_rotation(rotation),
        rotation_messages=rotations, damaged_frame_times_utc=damaged_times[:50],
        h264_compression_types=sorted(codec_seen), segments=segs_info)


def apply_display_rotation(path: str, degrees: int) -> None:
    """Carry the camera's display rotation into the MP4 as a display matrix
    (stream copy, pixels untouched). Older FFmpeg uses the rotate metadata tag."""
    tmp = path[:-4] + ".rot.mp4"
    p = run([require("ffmpeg"), "-v", "error", "-y", "-display_rotation", str(degrees), "-i", path,
             "-c", "copy", "-movflags", "+faststart", tmp])
    if p.returncode != 0 and "Unrecognized option 'display_rotation'" in p.stderr:
        # FFmpeg 4/5 (e.g. Ubuntu 22.04) converts this tag into a display matrix.
        # New FFmpeg ignores the tag, so prefer its explicit input option above.
        p = run([require("ffmpeg"), "-v", "error", "-y", "-i", path,
                 "-c", "copy", "-metadata:s:v:0", f"rotate={degrees}", "-movflags", "+faststart", tmp])
    if p.returncode != 0 or not os.path.exists(tmp):
        raise g64.G64Error("could not set display rotation: " + p.stderr.strip())
    os.replace(tmp, path)


def verify(result: ConvertResult) -> bool:
    """Decode the whole MP4 and compare against the archive. Decoder diagnostics
    are tolerated only for frames the archive itself delivered incomplete.

    (Do not verify through ``ffmpeg -f null``: the null muxer rescales to a
    coarse timebase and reports 1 ms timestamps as duplicate DTS - a false
    failure measured on a 25,938-frame file.)"""
    try:
        # This unique list name works on FFmpeg 4.4 and newer. The ambiguous
        # "side_data" also selects frame/packet sections; old ffprobe emits
        # malformed JSON for H.264 SEI data when those sections are selected.
        j = ffprobe_json(result.output, "stream=codec_name,width,height,nb_read_frames,nb_read_packets",
                         "stream_side_data_list", "format=duration", count_frames=True)
    except RuntimeError as e:
        result.verify = {"error": str(e)}
        return False
    st = j["streams"][0]
    errs = j["_stderr"]
    frames = int(st.get("nb_read_frames", 0))
    packets = int(st.get("nb_read_packets", 0))
    rot = None
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = sd["rotation"]
    result.verify = {
        "mp4_packets": packets, "mp4_decoded_frames": frames,
        "mp4_duration_s": float(j.get("format", {}).get("duration", 0)),
        "decode_errors": len(errs), "decode_error_sample": errs[:3],
        "codec": st.get("codec_name"), "mp4_dims": f"{st.get('width')}x{st.get('height')}",
        "mp4_rotation": rot,
    }
    counts_ok = (frames == result.frames_written and packets == result.frames_written
                 and st.get("codec_name") == "h264" and st.get("width") == result.width)
    if counts_ok and errs and result.frames_damaged:
        result.verify["decode_errors_attributed_to_source_damage"] = True
        return True
    return counts_ok and not errs

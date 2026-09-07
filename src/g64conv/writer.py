"""Write synthetic .g64 / .g64x archives from any video.

This exists so the test-suite (and anyone reproducing a bug) can build a valid
archive from ffmpeg's test pattern instead of needing real camera exports. It
writes an unwatermarked v5.31 archive: the same header/frame/RTP layout the
reader parses, with H.264 packetized per RFC 6184 (single NAL or FU-A)."""

from __future__ import annotations

import datetime
import io
import os
import struct
import uuid
import zipfile
from fractions import Fraction

from . import format as g64
from .h264 import NAL_IDR, split_annexb
from .tools import require, run

MTU_PAYLOAD = 1400
ARCHIVER_RTP_HEADER = bytes([0x80, g64.PT_ARCHIVER_FRAME]) + bytes(10)


def _wstr(s: str) -> bytes:
    b = s.encode("utf-16-le")
    return struct.pack("<i", len(b) // 2) + b


def _guid_bytes(u: uuid.UUID) -> bytes:
    return u.bytes_le


def build_header(collection: str = "synthetic", start: datetime.datetime | None = None,
                 end: datetime.datetime | None = None) -> bytes:
    out = bytearray()
    out += g64.START_CODE_PREFIX.encode() + b"v5.31"
    out += b"\0" * (30 - len(out))
    out += struct.pack("<q", g64.datetime_to_filetime(end) if end else -1)
    tz = bytearray(52)  # index 0, bias 0 (UTC), no DST rule
    out += tz
    out += _guid_bytes(uuid.uuid4()) + _wstr(collection)
    out += _guid_bytes(uuid.uuid4()) + _wstr(collection)
    out += _guid_bytes(uuid.uuid4()) + _guid_bytes(uuid.uuid4()) + _guid_bytes(uuid.uuid4())
    out += struct.pack("<i", 0)      # file properties: nothing set (not encrypted)
    out += struct.pack("<i", 0)      # no XML
    out += b"\0"                     # not watermarked
    out += struct.pack("<H", 0)      # no third-party watermark
    out += b"\1"                     # seek table "at end" (we write an empty one)
    out += b"\0\0\0\0"               # padding before the first frame
    return bytes(out)


def _rtp(seq: int, ts: int, payload: bytes, marker: bool) -> bytes:
    return struct.pack(">BBHII", 0x80, g64.PT_H264 | (0x80 if marker else 0), seq & 0xFFFF,
                       ts & 0xFFFFFFFF, 0x5EED5EED) + payload


def packetize(nals: list[bytes], seq: int, ts: int) -> tuple[list[bytes], int]:
    """RFC 6184: single-NAL packets, FU-A for NALs bigger than the MTU."""
    pkts = []
    for i, n in enumerate(nals):
        last_nal = i == len(nals) - 1
        if len(n) <= MTU_PAYLOAD:
            pkts.append(_rtp(seq, ts, n, last_nal))
            seq += 1
            continue
        hdr, body = n[0], n[1:]
        ind = (hdr & 0xE0) | 28
        typ = hdr & 0x1F
        o = 0
        while o < len(body):
            chunk = body[o:o + MTU_PAYLOAD - 2]
            o += len(chunk)
            end = o >= len(body)
            fu = typ | (0x80 if o == len(chunk) else 0) | (0x40 if end else 0)
            pkts.append(_rtp(seq, ts, bytes([ind, fu]) + chunk, last_nal and end))
            seq += 1
    return pkts, seq


def frame_bytes(filetime: int, nals: list[bytes], seq: int, ts: int,
                compression: int = g64.COMPRESSION_GENERIC_H264) -> tuple[bytes, int]:
    pkts, seq = packetize(nals, seq, ts)
    payload = bytearray(ARCHIVER_RTP_HEADER)
    for p in pkts:
        payload += struct.pack(">BBH", 0, compression, len(p)) + b"\0\0" + p
    key = any((n[0] & 0x1F) == NAL_IDR for n in nals)
    out = struct.pack("<qBI", filetime, g64.OPT_KEYFRAME if key else 0, len(payload))
    out += payload
    out += struct.pack("<I", len(payload) + 13)
    return out, seq


def access_units_from_video(video_path: str, *, crf: int = 23, gop: int = 12) -> list[tuple[int, list[bytes]]]:
    """Encode any video to H.264 and return [(pts_ms, [nal, ...]), ...] one per access unit."""
    import av

    ffmpeg = require("ffmpeg")
    p = run([ffmpeg, "-v", "error", "-i", video_path, "-an", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(crf), "-g", str(gop), "-bf", "0", "-x264-params", "repeat-headers=1",
             "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"], text=False)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg encode failed: " + p.stderr.decode(errors="replace").strip())
    fr = ffprobe_fps(video_path)
    c = av.open(io.BytesIO(p.stdout), format="h264")
    aus = []
    for i, pkt in enumerate(c.demux(video=0)):
        if pkt.size == 0:
            continue
        aus.append((round(i * 1000 / fr), split_annexb(bytes(pkt))))
    c.close()
    return aus


def ffprobe_fps(video_path: str) -> float:
    from .tools import ffprobe_json
    j = ffprobe_json(video_path, "stream=r_frame_rate,avg_frame_rate")
    st = j["streams"][0]
    for k in ("avg_frame_rate", "r_frame_rate"):
        v = st.get(k)
        if v and v not in ("0/0", "0"):
            f = Fraction(v)
            if f > 0:
                return float(f)
    return 25.0


def write_g64(video_path: str, out_path: str, *, collection: str = "synthetic",
              start: datetime.datetime | None = None, segment_seconds: float | None = None) -> list[str]:
    """Write one .g64 (or several _N segments when ``segment_seconds`` is set).
    Returns the written paths."""
    start = start or datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    aus = access_units_from_video(video_path)
    if not aus:
        raise RuntimeError("no access units produced")
    base = g64.datetime_to_filetime(start)
    chunks: list[list[tuple[int, list[bytes]]]] = [[]]
    for pts_ms, nals in aus:
        if segment_seconds and chunks[-1] and pts_ms // int(segment_seconds * 1000) != chunks[-1][0][0] // int(segment_seconds * 1000):
            chunks.append([])
        chunks[-1].append((pts_ms, nals))
    paths = []
    seq = 1
    stem, ext = os.path.splitext(out_path)
    for ci, chunk in enumerate(chunks):
        path = out_path if ci == 0 else f"{stem}_{ci}{ext}"
        first = g64.filetime_to_datetime(base + chunk[0][0] * 10000)
        last = g64.filetime_to_datetime(base + chunk[-1][0] * 10000)
        buf = bytearray(build_header(collection, first, last))
        for pts_ms, nals in chunk:
            fb, seq = frame_bytes(base + pts_ms * 10000, nals, seq, pts_ms * 90)
            buf += fb
        buf += struct.pack("<q", -1)  # end of frame chain
        buf += struct.pack("<i", 0)   # empty seek table
        with open(path, "wb") as fh:
            fh.write(buf)
        paths.append(path)
    return paths


def _iso_ms(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def write_g64x(video_path: str, out_path: str, *, collection: str = "synthetic",
               start: datetime.datetime | None = None, segment_seconds: float | None = None) -> str:
    """Write a .g64x export archive (stored zip + FileInfo.xml manifest)."""
    import tempfile

    start = start or datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    with tempfile.TemporaryDirectory() as td:
        segs = write_g64(video_path, os.path.join(td, "video.g64"), collection=collection,
                         start=start, segment_seconds=segment_seconds)
        files = []
        for s in segs:
            with open(s, "rb") as fh:
                data = fh.read()
            seg = g64.Segment.parse(os.path.basename(s), data)
            files.append((os.path.basename(s), data, seg.frames[0].time, seg.frames[-1].time))
        src_guid = str(uuid.uuid4())
        xml = ["<G64xArchiveFileHeader>", "  <FileVersion>1</FileVersion>",
               f"  <entityName>{collection}</entityName>", "  <UniqueSources>",
               (f'    <UniqueSource Id="0" Collection="{src_guid}" CollectionName="{collection}" '
                f'Encoder="{src_guid}" EncoderName="{collection}" />'),
               "  </UniqueSources>", "  <Files>"]
        for name, _, a, b in files:
            xml.append(f'    <File SourceId="0" start="{_iso_ms(a)}" end="{_iso_ms(b)}" '
                       f'FileName="{name}" WM="false" Encrypted="false" />')
        xml += ["  </Files>", "</G64xArchiveFileHeader>"]
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for name, data, _, _ in files:
                zf.writestr(name, data)
            zf.writestr("FileInfo.xml", "\n".join(xml))
    return out_path

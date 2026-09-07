"""Genetec Omnicast Archive (.g64) container parsing.

Byte layout (all little-endian unless noted), derived from the archive files
themselves and the reader behaviour of the vendor player, written down here in
our own words so this package carries no vendor code:

File header
  30 B   start code, e.g. ``Genetec Omnicast Archive v5.31``
  1 B    (v5.32 only) header-encrypted flag
  8 B    end time, Windows FILETIME UTC, -1 = unknown
  52 B   time zone block (48 B before v4.10): u64 index, i32 bias minutes, ...
  16 B   collection GUID + name (i32 char count, UTF-16LE)
  (v5.30+) 16 B encoder GUID + name, 16 B usage GUID, 16 B origin GUID, 16 B media-type GUID
  4 B    file properties (bit 1 = SRTP-encrypted frames)
  (v4.03+) i32 byte length + UTF-16LE XML
  1 B    watermark flag; if set: i32 pubkey len + pubkey, i16 data len, u16 type, data
  (v5.31+) u16 third-party watermark type

Footer marker (right after the header)
  1 B    "seek table lives at end of file"; if 0: i32 size + inline table
  4 B    padding, then the first frame

Frame
  8 B    FILETIME start time (-1 or all-zero ends the chain)
  1 B    option flags (bit 2 = keyframe)
  4 B    payload size
  payload:
    if the RTP payload type byte says 102 ("archiver frame"):
        12 B RTP header, then repeated
        { u8 version, u8 VideoCompressionType, u16 BE length, 2 B pad } + one RTP packet
    else: the payload is a single RTP packet
  N B    watermark data (length from the header) if the file is watermarked
  4 B    trailer = payload size + 13
"""

from __future__ import annotations

import datetime
import struct
from dataclasses import dataclass, field
from typing import Iterator

FILETIME_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
START_CODE_PREFIX = "Genetec Omnicast Archive "
VERSIONS = {
    "v2.00": 1, "v2.11": 2, "v2.30": 3, "v3.01": 4, "v4.02": 5, "v4.03": 6,
    "v4.10": 7, "v4.80": 8, "v5.30": 9, "v5.31": 10, "v5.32": 11,
}
VERSION_NAMES = {v: k for k, v in VERSIONS.items()}
PT_ARCHIVER_FRAME = 102
PT_DECODER_MESSAGE = 103
PT_H264 = 96
OPT_KEYFRAME = 0x04
FILE_PROPERTY_ENCRYPTED = 0x2
DECODER_MSG_VIDEO_ROTATION = 4
# RTP payload types the archive uses for non-video side channels
SIDE_CHANNEL_PT = {103, 104, 126, 127, 90, 113, 114}
# VideoCompressionType values that carry H.264
H264_COMPRESSION = {24, 29, 31, 33, 36, 39, 42, 45, 47, 48, 49, 50, 51, 52, 53}
HEVC_COMPRESSION = {61, 65, 66, 67, 68, 69}
COMPRESSION_GENERIC_H264 = 24


class G64Error(Exception):
    """A structural problem with an archive (not a decode problem)."""


def filetime_to_datetime(v: int) -> datetime.datetime:
    return FILETIME_EPOCH + datetime.timedelta(microseconds=v // 10)


def datetime_to_filetime(dt: datetime.datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int((dt - FILETIME_EPOCH).total_seconds() * 10_000_000)


def _i32(b: bytes, o: int) -> int:
    return struct.unpack_from("<i", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _i64(b: bytes, o: int) -> int:
    return struct.unpack_from("<q", b, o)[0]


def _guid(g: bytes) -> str:
    """.NET Guid byte order: the first three fields are little-endian."""
    return "%08x-%04x-%04x-%s-%s" % (
        _u32(g, 0), struct.unpack_from("<H", g, 4)[0], struct.unpack_from("<H", g, 6)[0],
        g[8:10].hex(), g[10:16].hex(),
    )


@dataclass
class Header:
    startcode: str
    version: int
    header_size: int
    end_time_utc: datetime.datetime | None
    tz_bias_min: int
    collection_guid: str
    collection_name: str
    encoder_guid: str = ""
    encoder_name: str = ""
    usage_guid: str = ""
    origin_guid: str = ""
    mediatype_guid: str = ""
    file_properties: int = 0
    xml: str = ""
    watermarked: bool = False
    wm_type: int = 0
    wm_encdata_len: int = 0
    thirdparty_wm: int = 0

    @property
    def frames_encrypted(self) -> bool:
        return bool(self.file_properties & FILE_PROPERTY_ENCRYPTED)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["end_time_utc"] = self.end_time_utc.isoformat() if self.end_time_utc else None
        d["frames_encrypted"] = self.frames_encrypted
        return d


def parse_header(b: bytes) -> Header:
    start = b[0:30].rstrip(b"\0").decode("utf-8", "replace")
    if not start.startswith(START_CODE_PREFIX):
        raise G64Error(f"not a Genetec Omnicast Archive (start code {start!r})")
    ver = VERSIONS.get(start.split()[-1])
    if ver is None:
        raise G64Error(f"unknown archive version {start!r}")
    o = 30
    if ver >= 11:
        if b[o]:
            raise G64Error("password-encrypted header (v5.32) is not supported")
        o += 1
    et = _i64(b, o); o += 8
    end_time = None if et == -1 else filetime_to_datetime(et)
    tzsize = 52 if ver >= 7 else 48
    tz_bias = _i32(b, o + (8 if ver >= 7 else 4)); o += tzsize

    def guid() -> str:
        nonlocal o
        g = b[o:o + 16]; o += 16
        return _guid(g)

    def wstr() -> str:
        nonlocal o
        n = _i32(b, o); o += 4
        if n <= 0 or n > 102400:
            return ""
        s = b[o:o + n * 2].decode("utf-16-le", "replace"); o += n * 2
        return s

    h = Header(startcode=start, version=ver, header_size=0, end_time_utc=end_time,
               tz_bias_min=tz_bias, collection_guid=guid(), collection_name=wstr())
    if ver >= 9:
        h.encoder_guid = guid(); h.encoder_name = wstr()
        h.usage_guid = guid(); h.origin_guid = guid(); h.mediatype_guid = guid()
    h.file_properties = _i32(b, o); o += 4
    if ver >= 6:
        n = _i32(b, o); o += 4
        if 0 < n < 102400:
            h.xml = b[o:o + n].decode("utf-16-le", "replace"); o += n
    wm = b[o]; o += 1
    h.watermarked = bool(wm)
    if wm:
        pk = _i32(b, o); o += 4
        if pk < 0 or pk > 102400:
            raise G64Error(f"invalid watermark public key size {pk}")
        o += pk
        ed = struct.unpack_from("<h", b, o)[0]; o += 2
        h.wm_type = struct.unpack_from("<H", b, o)[0]; o += 2
        if ed <= 0:
            raise G64Error(f"invalid watermark data size {ed}")
        o += ed
        h.wm_encdata_len = ed
    if ver >= 10:
        h.thirdparty_wm = struct.unpack_from("<H", b, o)[0]; o += 2
    h.header_size = o
    return h


def first_frame_offset(b: bytes, o: int) -> int:
    """Skip the footer marker: 1 byte 'seek table at end', else an inline table."""
    seek_at_end = b[o]; o += 1
    if not seek_at_end:
        n = _i32(b, o); o += 4
        if 0 < n <= 50_000_000:
            o += n
    return o + 4


@dataclass
class Frame:
    offset: int
    filetime: int
    option: int
    payload_offset: int
    payload_size: int

    @property
    def time(self) -> datetime.datetime:
        return filetime_to_datetime(self.filetime)

    @property
    def keyframe_flag(self) -> bool:
        return bool(self.option & OPT_KEYFRAME)


def iter_frames(b: bytes, o: int, wm_len: int) -> Iterator[Frame]:
    n = len(b)
    while o + 13 <= n:
        t = _i64(b, o)
        if t == -1 or b[o:o + 8] == b"\0" * 8:
            return
        opt = b[o + 8]; size = _u32(b, o + 9)
        if size < 12 or o + 13 + size > n:
            return
        yield Frame(o, t, opt, o + 13, size)
        o = o + 13 + size + wm_len + 4


def split_rtp(b: bytes, po: int, size: int) -> Iterator[tuple[int, bytes]]:
    """Yield (compression_type, rtp_packet) for every RTP packet in a frame payload."""
    if (b[po + 1] & 0x7F) != PT_ARCHIVER_FRAME:
        yield 0, b[po:po + size]
        return
    o = po + 12; end = po + size
    while o + 6 <= end:
        comp = b[o + 1]; ln = struct.unpack_from(">H", b, o + 2)[0]; o += 6
        if ln == 0 or o + ln > end:
            return
        yield comp, b[o:o + ln]
        o += ln


@dataclass
class RtpPacket:
    payload_type: int
    marker: bool
    sequence: int
    timestamp: int
    payload: bytes


def parse_rtp(p: bytes) -> RtpPacket | None:
    """RFC 3550 header: honours CSRC list, header extension and padding."""
    if len(p) < 12:
        return None
    cc = p[0] & 0x0F
    hs = 12 + cc * 4
    if p[0] & 0x10 and hs + 4 <= len(p):
        hs += 4 + struct.unpack_from(">H", p, hs + 2)[0] * 4
    if hs > len(p):
        hs = 12
    pad = p[-1] if (p[0] & 0x20) else 0
    return RtpPacket(p[1] & 0x7F, bool(p[1] & 0x80), struct.unpack_from(">H", p, 2)[0],
                     struct.unpack_from(">I", p, 4)[0], p[hs:len(p) - pad])


def decoder_message_rotation(payload: bytes) -> int | None:
    """Vendor 'decoder message' side channel (RTP PT 103): 4 opaque bytes, then a
    14-byte big-endian header {i16, i16 type, i16 compression, u32, u32 body_len}
    and the body. Type 4 is the camera's display rotation as ASCII degrees."""
    seg = payload[4:]
    if len(seg) < 14 or struct.unpack_from(">h", seg, 2)[0] != DECODER_MSG_VIDEO_ROTATION:
        return None
    n = struct.unpack_from(">I", seg, 10)[0]
    body = seg[14:14 + n].split(b"\0")[0].strip()
    return int(body) if body.isdigit() else None


@dataclass
class Segment:
    """One parsed .g64 file."""
    name: str
    header: Header
    frames: list[Frame] = field(default_factory=list)
    data: bytes = b""

    @classmethod
    def parse(cls, name: str, data: bytes) -> "Segment":
        h = parse_header(data)
        start = first_frame_offset(data, h.header_size)
        return cls(name, h, list(iter_frames(data, start, h.wm_encdata_len)), data)

"""RFC 6184 H.264-over-RTP depacketization, SPS parsing, and AVCC building."""

from __future__ import annotations

import struct

START_CODE = b"\0\0\0\1"
NAL_IDR = 5
NAL_SPS = 7
NAL_PPS = 8


class Depacketizer:
    """Reassemble the NAL units of one access unit from its RTP payloads.

    Handles single-NAL (types 1-23), STAP-A (24) and FU-A (28). Fragments that
    arrive without their start fragment, or that never see an end fragment, are
    counted in ``dropped_fragments`` so the caller can report source damage."""

    def __init__(self) -> None:
        self.nals: list[bytes] = []
        self._fu: bytearray | None = None
        self.dropped_fragments = 0

    def add(self, payload: bytes) -> None:
        if not payload:
            return
        t = payload[0] & 0x1F
        if 1 <= t <= 23:
            self._flush_fu()
            self.nals.append(bytes(payload))
        elif t == 24:  # STAP-A
            self._flush_fu()
            o = 1
            while o + 2 <= len(payload):
                ln = struct.unpack_from(">H", payload, o)[0]
                o += 2
                if ln == 0 or o + ln > len(payload):
                    break
                self.nals.append(bytes(payload[o:o + ln]))
                o += ln
        elif t == 28:  # FU-A
            if len(payload) < 2:
                return
            fu = payload[1]
            if fu & 0x80:  # start
                self._flush_fu()
                self._fu = bytearray([(payload[0] & 0xE0) | (fu & 0x1F)]) + payload[2:]
            elif self._fu is not None:
                self._fu += payload[2:]
            else:
                self.dropped_fragments += 1
                return
            if fu & 0x40:  # end
                self.nals.append(bytes(self._fu))
                self._fu = None
        else:
            self._flush_fu()  # STAP-B / MTAP / FU-B are not produced by these archives

    def _flush_fu(self) -> None:
        if self._fu is not None:
            self.nals.append(bytes(self._fu))
            self._fu = None
            self.dropped_fragments += 1

    def finish(self) -> list[bytes]:
        self._flush_fu()
        n, self.nals = self.nals, []
        return n


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def bit(self) -> int:
        v = (self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1
        self.p += 1
        return v

    def bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self) -> int:
        z = 0
        while self.bit() == 0:
            z += 1
            if z > 32:
                raise ValueError("bad exp-golomb code")
        return (1 << z) - 1 + self.bits(z)

    def se(self) -> int:
        v = self.ue()
        return (v + 1) // 2 if v & 1 else -(v // 2)


def unescape(nal: bytes) -> bytes:
    """Remove emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    z = 0
    for c in nal:
        if z >= 2 and c == 3:
            z = 0
            continue
        out.append(c)
        z = z + 1 if c == 0 else 0
    return bytes(out)


def sps_dimensions(sps: bytes) -> tuple[int, int]:
    """Coded width/height (after cropping) from an SPS NAL including its header byte."""
    r = _BitReader(unescape(sps[1:]))
    profile = r.bits(8)
    r.bits(8)
    r.bits(8)
    r.ue()
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = r.ue()
        if chroma == 3:
            r.bit()
        r.ue()
        r.ue()
        r.bit()
        if r.bit():  # scaling matrices
            for i in range(8 if chroma != 3 else 12):
                if r.bit():
                    last, nxt = 8, 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + r.se() + 256) % 256
                        last = last if nxt == 0 else nxt
    r.ue()
    poc = r.ue()
    if poc == 0:
        r.ue()
    elif poc == 1:
        r.bit()
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()
    r.bit()
    w_mbs = r.ue() + 1
    h_map = r.ue() + 1
    frame_mbs_only = r.bit()
    if not frame_mbs_only:
        r.bit()
    r.bit()
    w = w_mbs * 16
    h = (2 - frame_mbs_only) * h_map * 16
    if r.bit():  # frame cropping
        cl, cr, ct, cb = r.ue(), r.ue(), r.ue(), r.ue()
        if chroma == 0:
            sub_w, sub_h = 1, 2 - frame_mbs_only
        else:
            sub_w = 2 if chroma in (1, 2) else 1
            sub_h = (2 if chroma == 1 else 1) * (2 - frame_mbs_only)
        w -= (cl + cr) * sub_w
        h -= (ct + cb) * sub_h
    return w, h


def annexb(nals: list[bytes]) -> bytes:
    return b"".join(START_CODE + n for n in nals)


def split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into NAL units (without start codes)."""
    out = []
    i = 0
    n = len(data)
    starts = []
    while True:
        j = data.find(b"\0\0\1", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while e > s and data[e - 1] == 0:  # trailing zero of a 4-byte start code
            e -= 1
        if e > s:
            out.append(data[s:e])
    return out

"""Inspect an archive: header fields, frame statistics, NAL histogram."""

from __future__ import annotations

from . import format as g64
from .archive import discover


def probe(path: str, max_frames: int | None = None) -> list[dict]:
    out = []
    for src in discover(path):
        for name, load in src.segments:
            seg = g64.Segment.parse(name, load())
            nal_hist: dict[str, int] = {}
            comp_hist: dict[int, int] = {}
            pt_hist: dict[int, int] = {}
            rotations: dict[int, int] = {}
            times = []
            for i, fr in enumerate(seg.frames):
                if max_frames and i >= max_frames:
                    break
                times.append(fr.filetime)
                for comp, rtp in g64.split_rtp(seg.data, fr.payload_offset, fr.payload_size):
                    pk = g64.parse_rtp(rtp)
                    if pk is None:
                        continue
                    comp_hist[comp] = comp_hist.get(comp, 0) + 1
                    pt_hist[pk.payload_type] = pt_hist.get(pk.payload_type, 0) + 1
                    if pk.payload_type == g64.PT_DECODER_MESSAGE:
                        r = g64.decoder_message_rotation(pk.payload)
                        if r is not None:
                            rotations[r] = rotations.get(r, 0) + 1
                    if pk.payload_type == g64.PT_H264 and pk.payload:
                        nt = pk.payload[0] & 0x1F
                        key = (str(nt) if nt < 24 else "STAP-A" if nt == 24
                               else f"FU-A:{pk.payload[1] & 0x1F}" if nt == 28 else f"agg{nt}")
                        nal_hist[key] = nal_hist.get(key, 0) + 1
            d = {"source": src.label, "segment": name, "header": seg.header.to_dict(),
                 "frames": len(seg.frames), "compression_types": comp_hist, "rtp_payload_types": pt_hist,
                 "nal_units": nal_hist, "rotation_messages": rotations}
            if len(times) > 1:
                deltas = sorted((times[i + 1] - times[i]) / 10000 for i in range(len(times) - 1))
                d["frame_interval_ms"] = {"min": deltas[0], "median": deltas[len(deltas) // 2], "max": deltas[-1]}
                d["span_s"] = round((times[-1] - times[0]) / 1e7, 3)
                d["start_utc"] = g64.filetime_to_datetime(times[0]).isoformat()
            out.append(d)
    return out

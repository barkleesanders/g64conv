"""The local web GUI: upload, convert, list, play (Range), and the guards."""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from g64conv.gui import API_HEADER, make_server
from g64conv.writer import write_g64x


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    out = tmp_path_factory.mktemp("gui-out")
    srv = make_server(str(out), port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", str(out)
    srv.shutdown()
    srv.server_close()


def _req(url, method="GET", body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _wait_idle(base, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, _, b = _req(base + "/api/state")
        st = json.loads(b)
        if not st["busy"]:
            return st
        time.sleep(0.2)
    raise AssertionError("gui runner never went idle")


def test_page_and_state(server):
    base, out = server
    code, hdr, body = _req(base + "/")
    assert code == 200 and b"<title>g64conv</title>" in body
    assert "Content-Security-Policy" in hdr
    st = json.loads(_req(base + "/api/state")[2])
    assert st["out_dir"] == os.path.realpath(out) or st["out_dir"] == out
    assert st["tools"]["ffmpeg"] is True


def test_guards(server, tmp_path):
    base, _ = server
    # mutation without the custom header is refused (a cross-origin page cannot add it without CORS)
    code, _, body = _req(base + "/api/convert", "POST", {"paths": ["/nope.g64x"]})
    assert code == 403 and API_HEADER in json.loads(body)["error"]
    # a foreign Origin is refused even with the header
    code, _, _ = _req(base + "/api/convert", "POST", {"paths": ["/nope.g64x"]}, {API_HEADER: "1", "Origin": "https://evil.example"})
    assert code == 403
    # DNS-rebinding: a Host that is not loopback is refused
    code, _, _ = _req(base + "/api/state", headers={"Host": "attacker.example"})
    assert code == 403
    # non-existent input, wrong extension, non-list
    code, _, b = _req(base + "/api/convert", "POST", {"paths": ["/definitely/missing.g64x"]}, {API_HEADER: "1"})
    assert code == 400 and "not a file" in json.loads(b)["error"]
    code, _, _ = _req(base + "/api/convert", "POST", {"paths": "x"}, {API_HEADER: "1"})
    assert code == 400
    # uploads accept archives only
    code, _, _ = _req(base + "/api/upload?name=evil.sh", "PUT", raw=b"x", headers={API_HEADER: "1"})
    assert code == 400
    # /files never leaves the output directory
    code, _, _ = _req(base + "/files/../pyproject.toml")
    assert code == 404
    code, _, _ = _req(base + "/files/%2e%2e/pyproject.toml")
    assert code == 404


def test_upload_convert_play(server, sample_video, ffmpeg, tmp_path):
    base, out = server
    arc = tmp_path / "sample.g64x"
    write_g64x(sample_video, str(arc), collection="cam", segment_seconds=0.8)
    data = arc.read_bytes()
    code, _, body = _req(base + "/api/upload?name=../../sample.g64x", "PUT", raw=data, headers={API_HEADER: "1"})
    assert code == 200
    up = json.loads(body)
    assert up["size"] == len(data) and os.path.dirname(up["path"]) == os.path.join(out, "uploads")
    code, _, body = _req(base + "/api/convert", "POST", {"paths": [up["path"]], "formats": ["mkv"], "dewarp": "none"}, {API_HEADER: "1"})
    assert code == 200
    st = _wait_idle(base)
    job = st["jobs"][0]
    assert job["status"] == "ok", job
    assert job["progress"] == 1.0
    res = job["results"][0]
    assert res["frames_written"] == 20 and res["verify"]["mp4_decoded_frames"] == 20
    assert any("OK" in line and "20 written" in line for line in job["log"])
    names = [o["name"] for o in st["outputs"]]
    mp4 = next(o for o in st["outputs"] if o["name"].endswith(".mp4"))
    assert mp4["playable"] and mp4["width"] == 320 and mp4["frames"] == 20
    assert any(n.endswith(".mkv") for n in names)
    # the <video> element seeks with Range requests
    code, hdr, body = _req(base + mp4["url"], headers={"Range": "bytes=0-99"})
    assert code == 206 and len(body) == 100 and hdr["Content-Range"] == f"bytes 0-99/{mp4['size']}"
    assert hdr["Content-Type"] == "video/mp4" and hdr["Accept-Ranges"] == "bytes"
    code, _, body = _req(base + mp4["url"], headers={"Range": f"bytes={mp4['size']}-"})
    assert code == 416
    code, _, body = _req(base + mp4["url"])
    assert code == 200 and len(body) == mp4["size"]
    # a follow-up transcode + dewarp job through the API
    code, _, _ = _req(base + "/api/transcode", "POST", {"paths": [os.path.join(out, mp4["name"])], "formats": ["gif"]}, {API_HEADER: "1"})
    assert code == 200
    st = _wait_idle(base)
    assert st["jobs"][0]["status"] == "ok" and any(n.endswith(".gif") for n in [o["name"] for o in st["outputs"]])


def test_subfolder_outputs_are_listed(server, sample_video):
    base, out = server
    import shutil
    sub = os.path.join(out, "dewarped")
    os.makedirs(sub, exist_ok=True)
    shutil.copy(sample_video, os.path.join(sub, "cam_A.mp4"))
    os.makedirs(os.path.join(out, "uploads", "nested"), exist_ok=True)
    shutil.copy(sample_video, os.path.join(out, "uploads", "nested", "ignored.mp4"))
    st = json.loads(_req(base + "/api/state")[2])
    names = [o["name"] for o in st["outputs"]]
    assert "dewarped/cam_A.mp4" in names and not any("ignored" in n for n in names)
    item = next(o for o in st["outputs"] if o["name"] == "dewarped/cam_A.mp4")
    assert item["url"] == "/files/dewarped/cam_A.mp4" and item["width"] == 320
    code, _, body = _req(base + item["url"], headers={"Range": "bytes=0-9"})
    assert code == 206 and len(body) == 10


def test_odd_filenames_and_bad_length(server, sample_video):
    base, out = server
    import shutil
    odd = os.path.join(out, "cam #2 50%.mp4")
    shutil.copy(sample_video, odd)
    st = json.loads(_req(base + "/api/state")[2])
    item = next(o for o in st["outputs"] if o["name"] == "cam #2 50%.mp4")
    assert item["url"] == "/files/cam%20%232%2050%25.mp4"
    code, _, body = _req(base + item["url"], headers={"Range": "bytes=0-9"})
    assert code == 206 and len(body) == 10
    # a malformed Content-Length is a 400, not a dropped connection
    code, _, body = _req(base + "/api/convert", "POST", raw=b"{}", headers={API_HEADER: "1", "Content-Length": "abc"})
    assert code == 400 and "Content-Length" in json.loads(body)["error"]


def test_convert_failure_is_reported(server, tmp_path):
    base, _ = server
    bad = tmp_path / "bad.g64"
    bad.write_bytes(b"not an archive at all" * 10)
    code, _, _ = _req(base + "/api/convert", "POST", {"paths": [str(bad)]}, {API_HEADER: "1"})
    assert code == 200
    st = _wait_idle(base)
    job = st["jobs"][0]
    assert job["status"] == "failed" and job["message"], job

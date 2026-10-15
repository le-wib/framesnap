"""The local server that streams the selected clip to the preview."""
import http.client
import socket
import time

import pytest

from framesnap import preview
from framesnap.preview import PreviewServer, Unsatisfiable, parse_range

SIZE = 50_000


@pytest.fixture()
def server(tmp_path):
    clip = tmp_path / "clip.mov"
    clip.write_bytes(bytes(i % 251 for i in range(SIZE)))
    srv = PreviewServer()
    yield srv, clip
    srv.close()


def get(srv, path, headers=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.request(method, path, headers=headers or {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), body


def path_of(url):
    return url.split(str(url.split("/")[2]), 1)[1]


# ------------------------------------------------------------------ parse_range

@pytest.mark.parametrize("header,expected", [
    (None, None), ("", None), ("items=0-1", None),
    ("bytes=0-99", (0, 99)), ("bytes=100-", (100, SIZE - 1)),
    ("bytes=-500", (SIZE - 500, SIZE - 1)), ("bytes=-999999", (0, SIZE - 1)),
    ("bytes=0-999999999", (0, SIZE - 1)),
    ("bytes=900-100", None),                  # invalid: the header is ignored
    ("bytes=0-1,5-6", None),                  # several ranges: the whole file
    ("bytes=abc-def", None),
])
def test_parse_range(header, expected):
    assert parse_range(header, SIZE) == expected


@pytest.mark.parametrize("header", ["bytes=%d-" % SIZE, "bytes=-0", "bytes=60000-60010"])
def test_unsatisfiable_ranges(header):
    with pytest.raises(Unsatisfiable):
        parse_range(header, SIZE)


# ------------------------------------------------------------------ server

def test_the_clip_needs_its_secret_url(server):
    srv, clip = server
    url = srv.url_for(str(clip))
    status, headers, body = get(srv, path_of(url))
    assert status == 200 and body == clip.read_bytes()
    assert headers["Content-Type"] == "video/quicktime" and headers["Accept-Ranges"] == "bytes"
    for wrong in ("/", "/video", "/v/", "/v/" + "A" * 32, path_of(url) + "x"):
        assert get(srv, wrong)[0] == 404, wrong


def test_tokens_are_long_and_change_every_time(server):
    srv, clip = server
    a, b = srv.url_for(str(clip)), srv.url_for(str(clip))
    assert a != b and len(a.rsplit("/", 1)[1]) >= 32
    assert get(srv, path_of(a))[0] == 404 and get(srv, path_of(b))[0] == 200


def test_foreign_host_header_is_refused(server):
    srv, clip = server
    p = path_of(srv.url_for(str(clip)))
    assert get(srv, p, {"Host": "evil.example:%d" % srv.port})[0] == 403
    assert get(srv, p, {"Host": "localhost:%d" % srv.port})[0] == 200


def test_ranges_are_served_exactly(server):
    srv, clip = server
    data = clip.read_bytes()
    p = path_of(srv.url_for(str(clip)))
    status, h, body = get(srv, p, {"Range": "bytes=0-99"})
    assert status == 206 and body == data[:100] and h["Content-Range"] == "bytes 0-99/%d" % SIZE
    status, h, body = get(srv, p, {"Range": "bytes=-500"})
    assert status == 206 and body == data[-500:]
    status, h, body = get(srv, p, {"Range": "bytes=49000-999999999"})
    assert status == 206 and body == data[49000:] and h["Content-Length"] == str(SIZE - 49000)
    status, h, body = get(srv, p, {"Range": "bytes=900-100"})
    assert status == 200 and body == data
    status, h, body = get(srv, p, {"Range": "bytes=%d-" % SIZE})
    assert status == 416 and h["Content-Range"] == "bytes */%d" % SIZE and body == b""
    status, h, body = get(srv, p, method="HEAD")
    assert status == 200 and h["Content-Length"] == str(SIZE) and body == b""


def test_revoking_stops_the_stream(server):
    srv, clip = server
    p = path_of(srv.url_for(str(clip)))
    srv.revoke(str(clip.parent / "other.mp4"))          # another clip: nothing changes
    assert get(srv, p)[0] == 200
    srv.revoke(str(clip))
    assert get(srv, p)[0] == 404 and srv.serving is None


def test_a_slow_reader_does_not_block_other_requests(server):
    srv, clip = server
    p = path_of(srv.url_for(str(clip)))
    slow = socket.create_connection(("127.0.0.1", srv.port))
    slow.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % (p, srv.port)).encode())
    time.sleep(0.2)                                      # the first request stays open, unread
    t0 = time.time()
    assert get(srv, p, {"Range": "bytes=0-9"})[0] == 206
    assert time.time() - t0 < 3
    slow.close()


def test_api_clear_and_remove_revoke_the_preview(tmp_path):
    from framesnap.app import Api
    from framesnap.core import collect_jobs

    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"\0" * 1000)
    api = Api()
    with api._c._lock:                     # a clip in the list, without probing it
        api._c._jobs[1] = collect_jobs([clip])[0]
        api._c._items.append({"id": 1, "name": "a.mp4", "status": "ready"})
    url = api.get_video_path(1)
    srv = preview.get_server()
    assert get(srv, path_of(url))[0] == 200
    api.remove(1)
    assert get(srv, path_of(url))[0] == 404
    with api._c._lock:
        api._c._jobs[2] = collect_jobs([clip])[0]
        api._c._items.append({"id": 2, "name": "a.mp4", "status": "ready"})
    url = api.get_video_path(2)
    api.clear()
    assert get(srv, path_of(url))[0] == 404
    api._shutdown()
    assert preview._server is None

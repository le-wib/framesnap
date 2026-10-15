"""Local HTTP server for the video preview under the timeline.

The page cannot read local files directly, so the selected clip is streamed from
``http://127.0.0.1:<random port>/v/<token>``. Protections:

* it listens on the loopback interface only,
* every URL carries a fresh random token (128 bits or more); a page from another
  site cannot guess it, so it cannot read the clip even though the answer allows
  any origin (``crossorigin="anonymous"`` is needed for the sound in WKWebView),
* the ``Host`` header must name the loopback address (no DNS rebinding),
* the token is revoked when the clip is removed or the list is cleared, and the
  server is closed with the app.

Range requests follow RFC 9110: one range (``a-b``, ``a-`` or the suffix form
``-n``), clamped to the file size, 416 when it cannot be satisfied; anything else
(several ranges, a malformed header) is answered with the whole file.
"""

from __future__ import annotations

import hmac
import http.server
import os
import secrets
import socketserver
import threading
from typing import Optional, Tuple

CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime"}
CHUNK = 256 * 1024


class Unsatisfiable(ValueError):
    pass


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """(start, end) inclusive for a single byte range, None to send the whole file.

    Raises ``Unsatisfiable`` when the range starts beyond the end of the file.
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[6:].strip()
    if "," in spec or "-" not in spec:
        return None
    first, last = (x.strip() for x in spec.split("-", 1))
    if first == "":
        if not last.isdigit():
            return None
        n = int(last)
        if n == 0 or size == 0:
            raise Unsatisfiable(header)
        return max(0, size - n), size - 1
    if not first.isdigit() or (last and not last.isdigit()):
        return None
    start = int(first)
    if last and int(last) < start:
        return None                       # invalid range-spec: ignore the header
    if start >= size:
        raise Unsatisfiable(header)
    end = min(int(last), size - 1) if last else size - 1
    return start, end


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class PreviewServer:
    """Serves one clip at a time to the page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._path: Optional[str] = None
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                owner._handle(self, body=True)

            def do_HEAD(self) -> None:  # noqa: N802
                owner._handle(self, body=False)

            def log_message(self, *_) -> None:  # no access log
                pass

        self._server = _Server(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- control
    def url_for(self, path: str) -> str:
        """Serve ``path`` from now on, under a new secret URL (the previous one stops working)."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._token, self._path = token, str(path)
        return "http://127.0.0.1:%d/v/%s" % (self.port, token)

    def revoke(self, path: Optional[str] = None) -> None:
        """Stop serving ``path`` (or whatever is served when ``path`` is None)."""
        with self._lock:
            if path is None or (self._path is not None and os.path.normcase(str(path))
                                == os.path.normcase(self._path)):
                self._token, self._path = None, None

    @property
    def serving(self) -> Optional[str]:
        with self._lock:
            return self._path

    def close(self) -> None:
        self.revoke()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- request
    def _handle(self, req: http.server.BaseHTTPRequestHandler, body: bool) -> None:
        host = (req.headers.get("Host") or "").strip().lower()
        if host not in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            self._plain(req, 403, "forbidden")
            return
        with self._lock:
            token, path = self._token, self._path
        wanted = req.path.split("?", 1)[0]
        if not token or not path or not hmac.compare_digest(wanted, "/v/" + token):
            self._plain(req, 404, "not found")
            return
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            self._plain(req, 404, "not found")
            return
        with fh:
            try:
                rng = parse_range(req.headers.get("Range"), size)
            except Unsatisfiable:
                req.send_response(416)
                req.send_header("Content-Range", "bytes */%d" % size)
                req.send_header("Content-Length", "0")
                self._common(req)
                req.end_headers()
                return
            start, end = rng if rng else (0, size - 1)
            length = max(0, end - start + 1)
            req.send_response(206 if rng else 200)
            req.send_header("Content-Type", CONTENT_TYPES.get(os.path.splitext(path)[1].lower(),
                                                              "application/octet-stream"))
            req.send_header("Content-Length", str(length))
            if rng:
                req.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self._common(req)
            req.end_headers()
            if not body:
                return
            fh.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    req.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                req.close_connection = True

    @staticmethod
    def _common(req: http.server.BaseHTTPRequestHandler) -> None:
        req.send_header("Accept-Ranges", "bytes")
        req.send_header("Access-Control-Allow-Origin", "*")
        req.send_header("Cache-Control", "no-store")
        req.send_header("X-Content-Type-Options", "nosniff")

    @staticmethod
    def _plain(req: http.server.BaseHTTPRequestHandler, code: int, text: str) -> None:
        data = text.encode("ascii")
        req.send_response(code)
        req.send_header("Content-Type", "text/plain")
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)


_server: Optional[PreviewServer] = None
_server_lock = threading.Lock()


def get_server() -> PreviewServer:
    global _server
    with _server_lock:
        if _server is None:
            _server = PreviewServer()
        return _server


def revoke(path: Optional[str] = None) -> None:
    with _server_lock:
        srv = _server
    if srv is not None:
        srv.revoke(path)


def shutdown() -> None:
    global _server
    with _server_lock:
        srv, _server = _server, None
    if srv is not None:
        srv.close()

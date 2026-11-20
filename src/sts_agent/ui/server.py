"""Localhost HTTP server of the interface: the page, the state feed and the commands.

    GET  /?panel=frame|controls|log|side|all&t=TOKEN   the page (frame: the single window's page)
    GET  /api/state?t=TOKEN&v=..&log=..&think=..&live=..&livelen=..&route=..
         waits up to 20 s for a state newer than ``v``, then returns the snapshot
         with only the log/thinking entries the page does not have yet
    POST /api/command   header X-STS-Token, JSON body {"cmd": ..., ...}

It listens on 127.0.0.1 only. Every request must carry the token generated for
this launch and a localhost Host header, so another web page open in a browser
cannot read the state or press the buttons.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PAGE = Path(__file__).with_name("panels.html")
PANELS = ("controls", "log", "side", "all", "frame")
_WAIT_S = 20.0


class UiServer:
    def __init__(self, hub, on_command, port: int = 0):
        self.hub, self.on_command = hub, on_command
        self.token = secrets.token_urlsafe(18)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):   # no per-request console noise
                pass

            def _send(self, status, body: bytes, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, data):
                self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

            def _allowed(self, query) -> bool:
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
                token = self.headers.get("X-STS-Token") or (query.get("t") or [""])[0]
                return host in ("127.0.0.1", "localhost") and secrets.compare_digest(token, server.token)

            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                if not self._allowed(query):
                    return self._json(403, {"error": "forbidden"})
                if url.path == "/":
                    try:
                        return self._send(200, PAGE.read_bytes(), "text/html")
                    except OSError as exc:
                        return self._json(500, {"error": f"page missing: {exc}"})
                if url.path == "/api/state":
                    def num(name, default=0):
                        try:
                            return int((query.get(name) or [default])[0])
                        except ValueError:
                            return default
                    server.hub.wait(num("v", -1), _WAIT_S)
                    return self._json(200, server.hub.snapshot(
                        log_after=num("log"), think_after=num("think"), live_id=num("live"),
                        live_len=num("livelen"), route_rev=num("route", -1)))
                self._json(404, {"error": "not found"})

            def do_POST(self):
                url = urlsplit(self.path)
                if not self._allowed(parse_qs(url.query)):
                    return self._json(403, {"error": "forbidden"})
                if url.path != "/api/command":
                    return self._json(404, {"error": "not found"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) if 0 < length <= 4096 else b"{}")
                    if not isinstance(body, dict) or not isinstance(body.get("cmd"), str):
                        raise ValueError("cmd missing")
                except (ValueError, OSError) as exc:
                    return self._json(400, {"ok": False, "message": f"bad request: {exc}"})
                try:
                    ok, message = server.on_command(body["cmd"], body)
                except Exception as exc:   # report it on the page instead of dropping the connection
                    ok, message = False, f"{type(exc).__name__}: {exc}"
                self._json(200, {"ok": bool(ok), "message": str(message)[:300]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="ui-server", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, panel: str, **extra) -> str:
        more = "".join(f"&{key}={value}" for key, value in extra.items())
        return f"http://127.0.0.1:{self.port}/?panel={panel}&t={self.token}{more}"

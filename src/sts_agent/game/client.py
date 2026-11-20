"""Game client: HTTP communication with the STS2MCP mod.

Only this module talks to the network. It uses the standard library (urllib)
so the project has no runtime dependencies.

Error model (important for safe execution):

* ``GameUnavailable``  - the request was never delivered (connection refused,
  DNS failure). For actions this means the action was NOT sent.
* ``AmbiguousActionError`` - an action request may have reached the game but we
  did not get a usable answer (read timeout, connection dropped mid-response,
  HTTP 5xx). The caller must inspect the game before deciding what happened and
  must never blindly resend.
* ``HttpError`` - a non-2xx response for a read request.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

SINGLEPLAYER_PATH = "/api/v1/singleplayer"


class GameClientError(Exception):
    pass


class GameUnavailable(GameClientError):
    """The request could not be delivered (nothing was sent to the game)."""


class HttpError(GameClientError):
    def __init__(self, status: int, body: Any, message: str = ""):
        super().__init__(message or f"HTTP {status}: {body!r}")
        self.status = status
        self.body = body


class AmbiguousActionError(GameClientError):
    """An action request may or may not have been applied by the game."""

    def __init__(self, message: str, elapsed_s: float):
        super().__init__(message)
        self.elapsed_s = elapsed_s


@dataclass
class ActionResponse:
    """Result of a POST action. ``accepted`` mirrors the mod's ``status == "ok"``.

    For ``play_card`` the mod only *enqueues* the card play; ``accepted`` means
    "queued", not "resolved". Resolution is confirmed by observing the game.
    """

    accepted: bool
    http_status: int
    body: Any
    elapsed_s: float

    @property
    def error(self) -> str | None:
        if isinstance(self.body, dict):
            return self.body.get("error")
        return None

    def to_record(self) -> dict:
        return {
            "accepted": self.accepted,
            "http_status": self.http_status,
            "body": self.body,
            "elapsed_s": round(self.elapsed_s, 4),
        }


class GameClientProtocol(Protocol):
    def hello(self) -> dict: ...
    def get_state(self) -> dict: ...
    def post_action(self, payload: dict) -> ActionResponse: ...
    def get_optional(self, path: str) -> dict | None: ...


def parse_mod_version(hello_message: str | None) -> str | None:
    """Extract the version from ``"Hello from STS2 MCP v0.4.0"``."""
    if not hello_message:
        return None
    m = re.search(r"v(\d+(?:\.\d+)+[\w.\-]*)", hello_message)
    return m.group(1) if m else None


class GameClient:
    def __init__(self, base_url: str, read_timeout_s: float = 5.0, action_timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.read_timeout_s = read_timeout_s
        self.action_timeout_s = action_timeout_s
        # Never route localhost game traffic through a system proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None, timeout: float) -> tuple[int, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, _decode(resp.read())
        except urllib.error.HTTPError as e:  # non-2xx with a body
            try:
                body = _decode(e.read())
            except Exception:  # pragma: no cover - defensive
                body = None
            return e.code, body

    # -- reads -------------------------------------------------------------
    def _get(self, path: str) -> dict:
        try:
            status, body = self._request("GET", path, None, self.read_timeout_s)
        except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError, OSError) as e:
            raise GameUnavailable(f"GET {path} failed: {_reason(e)}") from e
        if status != 200:
            raise HttpError(status, body)
        if not isinstance(body, dict):
            raise HttpError(status, body, f"GET {path}: expected a JSON object")
        return body

    def hello(self) -> dict:
        return self._get("/")

    def get_state(self) -> dict:
        return self._get(SINGLEPLAYER_PATH + "?format=json")

    def get_optional(self, path: str) -> dict | None:
        """GET an endpoint that may not exist in older mod builds."""
        try:
            return self._get(path)
        except HttpError:
            return None

    # -- actions -----------------------------------------------------------
    def post_action(self, payload: dict) -> ActionResponse:
        start = time.monotonic()
        try:
            status, body = self._request("POST", SINGLEPLAYER_PATH, payload, self.action_timeout_s)
        except urllib.error.URLError as e:
            elapsed = time.monotonic() - start
            inner = getattr(e, "reason", None)
            if isinstance(inner, ConnectionRefusedError) or _is_dns_error(inner):
                raise GameUnavailable(f"POST not delivered: {_reason(e)}") from e
            raise AmbiguousActionError(f"POST outcome unknown: {_reason(e)}", elapsed) from e
        except ConnectionRefusedError as e:
            raise GameUnavailable(f"POST not delivered: {e}") from e
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise AmbiguousActionError(
                f"POST outcome unknown: {_reason(e)}", time.monotonic() - start
            ) from e
        elapsed = time.monotonic() - start
        if status >= 500:
            # The mod returns 500 when the main-thread action threw. Part of the
            # action may have run, so treat as ambiguous.
            raise AmbiguousActionError(f"POST returned HTTP {status}: {body!r}", elapsed)
        accepted = isinstance(body, dict) and body.get("status") == "ok"
        return ActionResponse(accepted=accepted, http_status=status, body=body, elapsed_s=elapsed)


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_non_json_body": text[:2000]}


def _reason(e: BaseException) -> str:
    r = getattr(e, "reason", None)
    return f"{type(e).__name__}: {r if r is not None else e}"


def _is_dns_error(inner: Any) -> bool:
    return isinstance(inner, socket.gaierror)

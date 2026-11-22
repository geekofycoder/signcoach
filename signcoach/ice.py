"""STUN/TURN servers for the WebRTC connection.

Railway accepts only HTTP and TCP from the internet, so the camera and voice cannot reach
the server directly. Both sides connect out to a TURN relay instead. This module gets
short-lived credentials for Cloudflare's relay and prepares them for the browser and for
the server (aiortc has stricter rules than browsers).
"""

import asyncio
import re
import time

import httpx
from aiortc import RTCIceServer
from aiortc.rtcicetransport import parse_stun_turn_uri
from loguru import logger

API_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
TTL_SECONDS = 6 * 3600
REFRESH_BEFORE = 3600  # ask for new credentials when less than an hour is left
DEFAULT_ICE = [{"urls": ["stun:stun.l.google.com:19302"]}]

_URL = re.compile(r"(stun|turns?):[^:?/]+(?::(\d+))?")


def _browser_ok(url: str) -> bool:
    """Browsers block port 53, and the page waits for it to time out because it does not trickle ICE."""
    m = _URL.match(url)
    return bool(m) and m.group(2) != "53"


def _aiortc_ok(url: str) -> bool:
    try:
        parse_stun_turn_uri(url)
    except ValueError:
        return False
    return _browser_ok(url)


def clean_servers(raw) -> list[dict]:
    """Cloudflare's reply as a list of {urls, username?, credential?}, minus URLs nobody can use.

    A TURN entry without credentials is dropped: it would only fail to authenticate.
    """
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        urls = entry.get("urls") or []
        urls = [urls] if isinstance(urls, str) else urls
        urls = [u for u in urls if isinstance(u, str) and _browser_ok(u)]
        has_login = bool(entry.get("username") and entry.get("credential"))
        if not urls or (any(u.startswith("turn") for u in urls) and not has_login):
            continue
        item = {"urls": urls}
        if has_login:
            item.update(username=entry["username"], credential=entry["credential"])
        out.append(item)
    return out


def _turn_rank(url: str) -> int:
    """TCP and TLS first: Railway may block outbound UDP, and TLS on a normal port passes most firewalls."""
    if url.startswith("turns:"):
        return 0
    return 1 if "transport=tcp" in url else 2


def server_servers(servers: list[dict]) -> list[RTCIceServer]:
    """What aiortc gets. It keeps only the first STUN and the first TURN address, so choose them."""
    stun = None
    turns = []
    for entry in servers:
        for url in entry["urls"]:
            if not _aiortc_ok(url):
                continue
            if url.startswith("stun:"):
                stun = stun or url
            elif entry.get("username"):
                turns.append((_turn_rank(url), url, entry))
    out = []
    if stun:
        out.append(RTCIceServer(urls=stun))
    if turns:
        _, url, entry = min(turns, key=lambda t: t[0])  # first of the best rank
        out.append(RTCIceServer(urls=url, username=entry["username"], credential=entry["credential"]))
    return out


class IceProvider:
    """Hands out ICE servers, keeping Cloudflare credentials cached until shortly before they expire."""

    def __init__(self, key_id: str | None = None, api_token: str | None = None, transport=None):
        self._key_id = key_id
        self._token = api_token
        self._transport = transport  # tests pass httpx.MockTransport
        self._cached: list[dict] | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls):
        import os

        return cls(os.getenv("CLOUDFLARE_TURN_KEY_ID"), os.getenv("CLOUDFLARE_TURN_API_TOKEN"))

    @property
    def relay_enabled(self) -> bool:
        return bool(self._key_id and self._token)

    async def servers(self) -> list[dict]:
        """Server list for the browser. Never raises: without a relay it is plain STUN."""
        if not self.relay_enabled:
            return DEFAULT_ICE
        async with self._lock:
            if self._cached and time.monotonic() < self._expires:
                return self._cached
            try:
                fresh = await self._fetch()
            except Exception as e:
                # Log the kind of failure only: the error text can contain the key id.
                status = getattr(getattr(e, "response", None), "status_code", None)
                logger.warning(f"Could not get relay credentials from Cloudflare: {type(e).__name__} {status or ''}")
                return self._cached or DEFAULT_ICE  # old credentials may still be valid
            self._cached = fresh
            self._expires = time.monotonic() + TTL_SECONDS - REFRESH_BEFORE
            return fresh

    async def _fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
            r = await client.post(
                API_URL.format(key_id=self._key_id),
                headers={"Authorization": f"Bearer {self._token}"},
                json={"ttl": TTL_SECONDS},
            )
        r.raise_for_status()
        servers = clean_servers(r.json().get("iceServers"))
        if not any("username" in s for s in servers):
            raise ValueError("reply had no usable TURN server")
        return servers

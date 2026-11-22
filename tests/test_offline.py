"""Offline checks: no camera, no network, no model. Run: python tests/test_offline.py"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signcoach.judge import Judgement, encode_jpeg, parse_judgement  # noqa: E402
from signcoach.lessons import LessonState  # noqa: E402
from signcoach.policy import SPEECH_COOLDOWN, CoachPolicy  # noqa: E402


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def spoken(actions):
    return [a.text for a in actions if a.kind == "speak"]


def kinds(actions):
    return [a.payload.get("kind") for a in actions if a.kind == "message"]


def test_parse_judgement():
    j = parse_judgement(json.dumps({
        "verdict": "close", "issue": "thumb out", "fix": "Tuck your thumb.",
        "problem_part": "thumb", "hand_box": {"x": 0.9, "y": 0.1, "w": 0.5, "h": 0.4}}))
    assert j.verdict == "close" and j.hand_box["w"] <= 0.1 + 1e-9  # clamped inside the frame
    assert parse_judgement("not json").verdict == "unclear"
    assert parse_judgement('{"verdict": "banana"}').verdict == "unclear"
    assert parse_judgement('{"verdict":"no_hand","hand_box":{"x":.1,"y":.1,"w":.5,"h":.5}}').hand_box is None


def test_encode_jpeg():
    data = encode_jpeg(bytes(64 * 48 * 3), (64, 48), "RGB")
    assert data[:2] == b"\xff\xd8"


def test_success_needs_two_frames_then_advances():
    clock, lesson = Clock(), LessonState()
    p = CoachPolicy(lesson, clock)
    first = lesson.current.id
    a = p.on_judgement(Judgement("correct"))
    assert "success" not in kinds(a)
    a = p.on_judgement(Judgement("correct"))
    assert "success" in kinds(a) and lesson.current.id != first
    assert spoken(a) and lesson.current.label in spoken(a)[0]


def test_wrong_frame_resets_hold():
    p = CoachPolicy(LessonState(), Clock())
    p.on_judgement(Judgement("correct"))
    p.on_judgement(Judgement("incorrect", fix="x"))
    assert "success" not in kinds(p.on_judgement(Judgement("correct")))


def test_correction_waits_for_two_wrong_frames_and_respects_cooldown():
    clock = Clock()
    p = CoachPolicy(LessonState(), clock)
    j = Judgement("incorrect", fix="Tuck your thumb in.", problem_part="thumb")
    assert spoken(p.on_judgement(j)) == []                       # first wrong frame: stay quiet
    assert spoken(p.on_judgement(j)) == ["Tuck your thumb in."]  # second: speak
    clock.t += SPEECH_COOLDOWN - 1
    assert spoken(p.on_judgement(j)) == []                       # inside cooldown
    clock.t += 2
    again = spoken(p.on_judgement(j))
    assert again and again[0] != "Tuck your thumb in."           # never the same sentence twice


def test_visual_cue_sent_every_frame():
    p = CoachPolicy(LessonState(), Clock())
    for v in ("incorrect", "unclear", "no_hand", "close"):
        assert "judgement" in kinds(p.on_judgement(Judgement(v)))


def test_no_hand_prompt_once_then_cooldown():
    clock = Clock()
    p = CoachPolicy(LessonState(), clock)
    out = []
    for _ in range(4):
        out += spoken(p.on_judgement(Judgement("no_hand")))
    assert len(out) == 1 and "hand" in out[0]
    for _ in range(4):
        out += spoken(p.on_judgement(Judgement("no_hand")))
    assert len(out) == 1


def test_struggling_offer_after_three_corrections():
    clock = Clock()
    p = CoachPolicy(LessonState(), clock)
    j = Judgement("incorrect", fix="Fix one.")
    seen = []
    for i in range(12):
        j.fix = f"Fix {i}."
        seen += kinds(p.on_judgement(j))
        clock.t += SPEECH_COOLDOWN + 1
    assert "struggling" in seen


def test_skip_does_not_count_as_completed_and_ends_cleanly():
    lesson = LessonState()
    p = CoachPolicy(lesson, Clock())
    p.skip()
    assert lesson.completed == [] and lesson.index == 1
    for _ in range(len(lesson.signs)):
        out = p.skip()
    assert "finished" in kinds(out) and lesson.index == len(lesson.signs) - 1


def test_select_unknown_sign_is_ignored():
    p = CoachPolicy(LessonState(), Clock())
    assert p.select("ZZZ") == []
    assert p.select("l") and p.lesson.current.id == "L"


def test_every_sign_has_a_reference_image_with_credit():
    from signcoach.lessons import SIGNS

    signs_dir = Path(__file__).resolve().parent.parent / "static" / "signs"
    for s in SIGNS:
        assert s.image.startswith("/static/signs/"), s.id
        assert (signs_dir / s.image.rsplit("/", 1)[1]).is_file(), f"missing file for {s.id}"
        assert s.image_credit and s.image_credit_url.startswith("https://"), s.id


def test_lesson_message_carries_the_reference_image():
    p = CoachPolicy(LessonState(), Clock())
    msg = next(a.payload for a in p.intro_actions() if a.payload.get("kind") == "lesson")
    assert msg["sign"]["image"] == "/static/signs/A.svg"
    assert msg["sign"]["image_credit"] and msg["sign"]["image_credit_url"]


def test_caption_grows_word_by_word_and_restarts_per_line():
    from signcoach.captions import CaptionBuilder

    b = CaptionBuilder()
    assert b.add_word("Nice,", "c1") == "Nice,"
    assert b.add_word("that's", "c1") == "Nice, that's"
    assert b.add_word("  ", "c1") is None  # blank words are ignored
    assert b.add_word("it!", "c1") == "Nice, that's it!"
    assert b.add_word("Try", "c2") == "Try"  # a new spoken line replaces the caption
    b.speech_ended()
    assert b.add_word("Hello", "c2") == "Hello"  # even within the same context id
    assert b.add_word("世界", "c3", includes_spaces=True) == "世界"
    assert b.add_word("你好", "c3", includes_spaces=True) == "世界你好"  # no space added


def test_caption_sync_follows_the_voice_and_passes_every_frame_on():
    """Real pipecat pipeline: a stand-in transport collects what is sent to the browser."""
    import asyncio

    from pipecat.frames.frames import (
        BotStartedSpeakingFrame, EndFrame, Frame, OutputTransportMessageUrgentFrame, TTSTextFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.utils.text.base_text_aggregator import TextType
    from pipecat.workers.runner import WorkerRunner

    from signcoach.captions import CaptionSync

    sent, downstream = [], []

    class FakeOutput(FrameProcessor):  # plays the part of transport.output()
        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, OutputTransportMessageUrgentFrame):
                sent.append(frame.message)  # what the real transport writes to the data channel
            else:
                await self.push_frame(frame, direction)

    class Tail(FrameProcessor):  # plays the part of the assistant aggregator
        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            downstream.append(frame)
            await self.push_frame(frame, direction)

    def word(text):
        f = TTSTextFrame(text, text_type=TextType.WORD)
        f.context_id = "ctx"
        return f

    async def run():
        worker = PipelineWorker(Pipeline([FakeOutput(), CaptionSync(), Tail()]), params=PipelineParams())
        await worker.queue_frames([BotStartedSpeakingFrame(), word("Hold"), word("it"), word("up."), EndFrame()])
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()

    asyncio.run(run())
    # Pipecat adds its own "rtvi-ai" messages to the same channel; the page only reads ours.
    ours = [m for m in sent if m.get("type") == "signcoach"]
    assert [m["text"] for m in ours] == ["Hold", "Hold it", "Hold it up."]
    assert all(m["kind"] == "speech" for m in ours)
    # The chat history is built downstream from these same frames, so none may go missing.
    assert [f.text for f in downstream if isinstance(f, TTSTextFrame)] == ["Hold", "it", "up."]


# A reply shaped like Cloudflare's generate-ice-servers, including the port 53 URLs browsers cannot use.
CLOUDFLARE_REPLY = {"iceServers": [
    {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:53?transport=udp",
              "turn:turn.cloudflare.com:80?transport=tcp", "turns:turn.cloudflare.com:5349?transport=tcp",
              "turns:turn.cloudflare.com:443?transport=tcp"],
     "username": "user", "credential": "secret"},
]}


def test_relay_list_for_browser_drops_port_53_and_unusable_entries():
    from signcoach.ice import clean_servers

    servers = clean_servers(CLOUDFLARE_REPLY["iceServers"])
    urls = [u for s in servers for u in s["urls"]]
    import re

    assert not any(re.search(r":53(\?|$)", u) for u in urls)  # port 53 only; port 5349 (TLS) must stay
    assert len(urls) == 5 and "turns:turn.cloudflare.com:5349?transport=tcp" in urls
    assert servers[1]["username"] == "user" and servers[1]["credential"] == "secret"
    assert clean_servers({"urls": "stun:x.example:3478"}) == [{"urls": ["stun:x.example:3478"]}]  # a single object
    assert clean_servers([{"urls": ["turn:x.example:3478?transport=udp"]}]) == []  # TURN without login is useless
    assert clean_servers("junk") == [] and clean_servers(None) == []


def test_relay_list_for_server_is_one_stun_and_one_tcp_or_tls_turn_that_aiortc_accepts():
    from aiortc.rtcicetransport import connection_kwargs

    from signcoach.ice import clean_servers, server_servers

    servers = server_servers(clean_servers(CLOUDFLARE_REPLY["iceServers"]))
    assert len(servers) == 2  # aiortc keeps only the first of each kind, so we pick them
    kw = connection_kwargs(servers)  # what aiortc will really use
    assert kw["stun_server"] == ("stun.cloudflare.com", 3478)
    assert kw["turn_server"] == ("turn.cloudflare.com", 5349)
    assert kw["turn_ssl"] is True and kw["turn_transport"] == "tcp"  # not UDP: Railway may block it
    assert kw["turn_username"] == "user" and kw["turn_password"] == "secret"


def test_relay_credentials_are_cached_and_failures_fall_back():
    import asyncio

    import httpx

    from signcoach.ice import DEFAULT_ICE, IceProvider

    calls, fail = [], []

    def reply(request: httpx.Request):
        calls.append(request)
        return httpx.Response(500) if fail else httpx.Response(201, json=CLOUDFLARE_REPLY)

    async def run():
        none = IceProvider(None, None)
        assert not none.relay_enabled and await none.servers() == DEFAULT_ICE
        p = IceProvider("KEY123", "TOKEN456", transport=httpx.MockTransport(reply))
        first, second = await p.servers(), await p.servers()
        assert first is second and len(calls) == 1  # the second call used the cache
        req = calls[0]
        assert req.url.path == "/v1/turn/keys/KEY123/credentials/generate-ice-servers"
        assert req.headers["authorization"] == "Bearer TOKEN456" and b'"ttl"' in req.content
        # Cloudflare down: keep using the cached credentials, never raise.
        p._expires = 0
        fail.append(1)
        assert await p.servers() is first
        broken = IceProvider("KEY123", "TOKEN456", transport=httpx.MockTransport(reply))
        assert await broken.servers() == DEFAULT_ICE  # nothing cached yet: plain STUN

    asyncio.run(run())


def test_relay_servers_can_come_from_any_provider_via_one_variable():
    import asyncio
    import json

    from aiortc.rtcicetransport import connection_kwargs

    from signcoach.ice import DEFAULT_ICE, IceProvider, parse_static, server_servers

    # Shaped like the array a provider's dashboard shows (Metered, ExpressTURN, Xirsys, ...).
    pasted = json.dumps([
        {"urls": "stun:relay.example.com:80"},
        {"urls": "turn:relay.example.com:80", "username": "u1", "credential": "p1"},
        {"urls": "turn:relay.example.com:443?transport=tcp", "username": "u1", "credential": "p1"},
        {"urls": "turns:relay.example.com:443?transport=tcp", "username": "u1", "credential": "p1"},
    ])
    servers = parse_static(pasted)
    assert len(servers) == 4 and servers[3]["username"] == "u1"
    kw = connection_kwargs(server_servers(servers))
    assert kw["turn_server"] == ("relay.example.com", 443) and kw["turn_ssl"] is True  # TLS chosen for the server

    assert parse_static(None) == [] and parse_static("  ") == []
    assert parse_static("{not json") == []  # bad paste: ignored, and the text is never logged
    assert parse_static('[{"urls": "stun:relay.example.com:80"}]') == []  # STUN alone is no relay

    async def run():
        p = IceProvider(static_servers=servers)  # no HTTP transport given: any fetch attempt would fail
        assert p.relay_enabled and await p.servers() == servers
        assert not IceProvider().relay_enabled and await IceProvider().servers() == DEFAULT_ICE
        both = IceProvider("KEY", "TOKEN", static_servers=servers)
        assert both.relay_enabled  # Cloudflare takes priority when both are set

    asyncio.run(run())


def test_metered_username_and_password_build_the_relay_list():
    import os

    from aiortc.rtcicetransport import connection_kwargs

    from signcoach.ice import IceProvider, metered_servers, server_servers

    servers = metered_servers("someuser", "somepass")
    urls = [u for s in servers for u in s["urls"]]
    assert len(urls) == 5 and "turns:global.relay.metered.ca:443?transport=tcp" in urls
    kw = connection_kwargs(server_servers(servers))
    assert kw["turn_server"] == ("global.relay.metered.ca", 443) and kw["turn_ssl"] is True
    assert kw["turn_username"] == "someuser" and kw["turn_password"] == "somepass"
    assert metered_servers("someuser", None) == [] and metered_servers("", "x") == []

    old = {k: os.environ.get(k) for k in ("METERED_USERNAME", "METERED_PASSWORD", "ICE_SERVERS_JSON",
                                          "CLOUDFLARE_TURN_KEY_ID", "CLOUDFLARE_TURN_API_TOKEN")}
    try:
        for k in old:
            os.environ.pop(k, None)
        assert not IceProvider.from_env().relay_enabled
        os.environ.update(METERED_USERNAME="someuser", METERED_PASSWORD="somepass")
        assert IceProvider.from_env().relay_enabled  # the two variables are enough
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_passcode_and_session_cap():
    from signcoach.access import SessionCounter, passcode_ok

    assert passcode_ok(None, None) and passcode_ok("anything", "")  # no passcode set: open
    assert passcode_ok("open sesame", "open sesame")
    assert not passcode_ok("wrong", "open sesame") and not passcode_ok(None, "open sesame")
    c = SessionCounter(2)
    c.acquire(); assert not c.full()
    c.acquire(); assert c.full()
    c.release(); assert not c.full()
    c.release(); c.release(); assert c.active == 0  # never goes negative
    unlimited = SessionCounter(0)
    for _ in range(50):
        unlimited.acquire()
    assert not unlimited.full()


def test_session_timers_end_dead_and_overlong_sessions():
    import asyncio

    from signcoach.bot import end_after, end_if_never_connected

    class Worker:
        def __init__(self):
            self.cancelled, self.sent = 0, []

        async def cancel(self):
            self.cancelled += 1

        async def queue_frames(self, frames):
            self.sent += frames

    async def run():
        never, connected = Worker(), asyncio.Event()
        await end_if_never_connected(never, connected, timeout=0.05)
        assert never.cancelled == 1  # nobody connected: the pipeline is stopped

        ok, up = Worker(), asyncio.Event()
        up.set()
        await end_if_never_connected(ok, up, timeout=0.05)
        assert ok.cancelled == 0  # connected in time: left alone

        w = Worker()
        await end_after(w, 0.05, notice_seconds=0.01)
        assert w.cancelled == 1 and w.sent[0].message["kind"] == "session_end"  # told the page first

    asyncio.run(run())


def test_server_guards_with_a_passcode_and_a_full_house():
    import os

    from fastapi.testclient import TestClient

    import server

    c = TestClient(server.app)
    assert c.get("/health").json() == {"ok": True}
    old = os.environ.get("APP_PASSCODE")
    try:
        os.environ.pop("APP_PASSCODE", None)
        assert c.get("/api/config").json()["passcode_required"] is False
        assert c.get("/api/ice").status_code == 200  # open when no passcode is set

        os.environ["APP_PASSCODE"] = "open sesame"
        assert c.get("/api/config").json()["passcode_required"] is True  # public, so the page knows to ask
        assert c.get("/api/ice").status_code == 401
        assert c.get("/api/ice", headers={"X-Passcode": "wrong"}).status_code == 401
        assert c.get("/api/ice", headers={"X-Passcode": "open sesame"}).status_code == 200
        body = {"sdp": "v=0", "type": "offer"}
        assert c.post("/api/offer", json=body).status_code == 401  # nothing runs without the passcode
        assert c.patch("/api/offer", json={"pc_id": "x", "candidates": []}).status_code == 401

        server.sessions.active = server.sessions.limit  # every slot taken
        r = c.post("/api/offer", json=body, headers={"X-Passcode": "open sesame"})
        assert r.status_code == 503 and "busy" in r.json()["detail"]
    finally:
        server.sessions.active = 0
        if old is None:
            os.environ.pop("APP_PASSCODE", None)
        else:
            os.environ["APP_PASSCODE"] = old


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"all {len(tests)} sign coach checks passed")

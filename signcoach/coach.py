"""Pipecat processor that turns webcam frames into on-screen cues and spoken guidance."""

import asyncio
import json
import time

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputImageRawFrame,
    OutputTransportMessageUrgentFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .judge import SignJudge, encode_jpeg
from .lessons import LessonState
from .policy import Action, CoachPolicy

MIN_JUDGE_INTERVAL = 1.0  # seconds between frame checks; one check at a time
MESSAGE_TYPE = "signcoach"


class VisionCoach(FrameProcessor):
    """Sits right after the transport input.

    Camera frames are consumed here (never forwarded), so they don't pile up in the
    chat model's context. Everything else passes through untouched.
    """

    def __init__(self, judge: SignJudge, lesson: LessonState | None = None, **kwargs):
        super().__init__(**kwargs)
        self._judge = judge
        self.lesson = lesson or LessonState()
        self.policy = CoachPolicy(self.lesson)
        self._busy = False
        self._last_judged = 0.0
        self._user_speaking = False
        self._active = False  # becomes True once the learner connects

    # -- frame handling -------------------------------------------------------------
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputImageRawFrame):
            self._maybe_judge(frame)
            return  # swallow

        if isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False

        await self.push_frame(frame, direction)

    def _maybe_judge(self, frame: InputImageRawFrame):
        now = time.monotonic()
        if not self._active or self._busy or now - self._last_judged < MIN_JUDGE_INTERVAL:
            return
        self._busy = True
        self._last_judged = now
        self.create_task(self._judge_frame(frame), name="sign-judge")

    async def _judge_frame(self, frame: InputImageRawFrame):
        sign = self.lesson.current  # the sign this frame is judged against
        try:
            jpeg = await asyncio.to_thread(encode_jpeg, frame.image, frame.size, frame.format or "RGB")
            judgement = await self._judge.judge(jpeg, sign)
        except Exception as e:  # network, rate limit, bad frame: skip this frame, keep going
            logger.warning(f"Frame check failed: {type(e).__name__}: {e}")
            return
        finally:
            self._busy = False
        if sign.id != self.lesson.current.id:
            return  # the learner moved to another sign while we were waiting
        await self._run(self.policy.on_judgement(judgement))

    # -- lesson control (called from bot.py and the LLM tools) -----------------------
    async def start(self):
        self._active = True
        await self._run(self.policy.intro_actions(first=True))

    async def select_sign(self, sign_id: str) -> bool:
        actions = self.policy.select(sign_id)
        await self._run(actions)
        return bool(actions)

    async def skip_sign(self):
        await self._run(self.policy.skip())

    async def repeat_sign(self):
        await self._run(self.policy.intro_actions())

    async def handle_client_message(self, message):
        """Buttons on the web page send {"type": "signcoach", "action": ...}."""
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except json.JSONDecodeError:
                return
        if not isinstance(message, dict) or message.get("type") != MESSAGE_TYPE:
            return
        action = message.get("action")
        if action == "skip":
            await self.skip_sign()
        elif action == "repeat":
            await self.repeat_sign()
        elif action == "select" and isinstance(message.get("id"), str):
            await self.select_sign(message["id"])

    # -- output ---------------------------------------------------------------------
    async def _run(self, actions: list[Action]):
        for a in actions:
            if a.kind == "message":
                await self.push_frame(
                    OutputTransportMessageUrgentFrame(message={"type": MESSAGE_TYPE, **a.payload})
                )
            elif a.kind == "speak":
                if not self._user_speaking:
                    # The caption follows the voice: CaptionSync shows each word as it is spoken.
                    await self.push_frame(TTSSpeakFrame(a.text))
                else:
                    # The learner is talking, so the voice stays quiet. Show the line at once
                    # so the guidance is not lost.
                    await self.push_frame(
                        OutputTransportMessageUrgentFrame(
                            message={"type": MESSAGE_TYPE, "kind": "speech", "text": a.text}
                        )
                    )

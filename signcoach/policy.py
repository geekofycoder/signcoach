"""When to praise, when to correct out loud, when to move on.

Pure logic with an injectable clock, so it can be tested without a camera or a model.
The visual cue goes to the screen on every judged frame; speech is rationed here so the
learner is not nagged.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from .judge import Judgement
from .lessons import LessonState, Sign

HOLD_FRAMES = 2  # consecutive "correct" judgements needed to count as success
WRONG_FRAMES_BEFORE_SPEECH = 2  # consecutive wrong judgements before the first spoken fix
SPEECH_COOLDOWN = 8.0  # seconds between spoken corrections
NO_HAND_COOLDOWN = 15.0
NO_HAND_FRAMES = 4
STRUGGLE_AFTER_SPOKEN = 3  # spoken corrections on one sign before offering help

PRAISE = ["Nice, that's it!", "Yes, that's right!", "Perfect, well done!", "Great, you've got it!"]


@dataclass
class Action:
    kind: str  # "speak" | "message"
    text: str = ""
    payload: dict = field(default_factory=dict)


class CoachPolicy:
    def __init__(self, lesson: LessonState, clock: Callable[[], float] = time.monotonic):
        self.lesson = lesson
        self._clock = clock
        self._reset_for_sign()
        self._last_speech = -1e9
        self._last_no_hand_speech = -1e9

    def _reset_for_sign(self):
        self.correct_streak = 0
        self.wrong_streak = 0
        self.no_hand_streak = 0
        self.spoken_fixes = 0
        self._last_fix_text = ""

    # -- lesson control -------------------------------------------------------------
    def intro_actions(self, first: bool = False) -> list[Action]:
        sign = self.lesson.current
        lead = "Hi! I'm your sign language coach. Let's start. " if first else ""
        text = f"{lead}Let's try {sign.label}. {sign.tip}"
        return [self._lesson_message(sign), self._speak(text)]

    def select(self, sign_id: str) -> list[Action]:
        sign = self.lesson.select(sign_id)
        if sign is None:
            return []
        self._reset_for_sign()
        return self.intro_actions()

    def skip(self) -> list[Action]:
        if self.lesson.skip() is None:
            return [Action("message", payload={"kind": "finished", **self.lesson.progress()}),
                    self._speak("That's the last sign. You can pick any sign to practise again.")]
        self._reset_for_sign()
        return self.intro_actions()

    # -- per-frame ------------------------------------------------------------------
    def on_judgement(self, j: Judgement) -> list[Action]:
        sign = self.lesson.current
        actions: list[Action] = []
        now = self._clock()

        if j.verdict == "correct":
            self.correct_streak += 1
            self.wrong_streak = 0
            self.no_hand_streak = 0
        elif j.verdict in ("close", "incorrect"):
            self.correct_streak = 0
            self.wrong_streak += 1
            self.no_hand_streak = 0
        elif j.verdict == "no_hand":
            self.correct_streak = 0
            self.wrong_streak = 0
            self.no_hand_streak += 1
        # "unclear": leave streaks alone, it tells us nothing

        actions.append(
            Action("message", payload={"kind": "judgement", "sign_id": sign.id,
                                       "hold": self.correct_streak, "hold_needed": HOLD_FRAMES,
                                       **j.to_message()})
        )

        if self.correct_streak >= HOLD_FRAMES:
            actions.extend(self._success(sign))
            return actions

        if j.verdict in ("close", "incorrect") and self.wrong_streak >= WRONG_FRAMES_BEFORE_SPEECH:
            if now - self._last_speech >= SPEECH_COOLDOWN:
                actions.extend(self._correction(sign, j, now))
        elif j.verdict == "no_hand" and self.no_hand_streak >= NO_HAND_FRAMES:
            if now - max(self._last_speech, self._last_no_hand_speech) >= NO_HAND_COOLDOWN:
                self._last_no_hand_speech = now
                actions.append(self._speak("I can't see your hand. Hold it up in front of the camera."))
        return actions

    # -- helpers --------------------------------------------------------------------
    def _success(self, sign: Sign) -> list[Action]:
        nxt = self.lesson.advance()
        self._reset_for_sign()
        self._last_speech = self._clock()
        out = [Action("message", payload={"kind": "success", "sign_id": sign.id, **self.lesson.progress()})]
        if nxt is None:
            out.append(Action("message", payload={"kind": "finished", **self.lesson.progress()}))
            out.append(self._speak(f"{random.choice(PRAISE)} That was the last sign. Great work today!",
                                   record_time=False))
        else:
            out.append(self._lesson_message(nxt))
            out.append(self._speak(f"{random.choice(PRAISE)} Next, {nxt.label}. {nxt.tip}", record_time=False))
        return out

    def _correction(self, sign: Sign, j: Judgement, now: float) -> list[Action]:
        self.spoken_fixes += 1
        self._last_speech = now
        fix = j.fix.strip() or sign.tip
        if fix == self._last_fix_text:
            fix = sign.tip  # don't repeat the same sentence; fall back to the lesson tip
        self._last_fix_text = fix
        out = [self._speak(fix, record_time=False)]
        if self.spoken_fixes % STRUGGLE_AFTER_SPOKEN == 0:
            out.append(Action("message", payload={"kind": "struggling", "sign_id": sign.id}))
            out.append(self._speak(
                "This one is tricky. Take your time, or say skip and we'll try another sign.",
                record_time=False))
        return out

    def _lesson_message(self, sign: Sign) -> Action:
        return Action("message", payload={
            "kind": "lesson",
            "sign": {"id": sign.id, "label": sign.label, "tip": sign.tip,
                     "description": sign.description, "key_parts": list(sign.key_parts),
                     "image": sign.image, "image_credit": sign.image_credit,
                     "image_credit_url": sign.image_credit_url},
            **self.lesson.progress(),
        })

    def _speak(self, text: str, record_time: bool = True) -> Action:
        if record_time:
            self._last_speech = self._clock()
        return Action("speak", text=text)

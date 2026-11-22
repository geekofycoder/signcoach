"""Captions that appear as the words are spoken, not when they are queued.

The coach hands text to the voice a second or two before it is heard (voice synthesis,
plus anything still playing). Showing that text straight away makes the screen run ahead
of the speech. Instead, `CaptionSync` sits after the output transport, which holds each
word the voice reports until its audio plays, and shows the line one word at a time.
"""

import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    OutputTransportMessageUrgentFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .coach import MESSAGE_TYPE


class CaptionBuilder:
    """Grows the caption word by word. Pure logic, tested offline."""

    def __init__(self):
        self.text = ""
        self._context = None
        self._fresh = True  # the next word starts a new caption

    def add_word(self, word: str, context_id=None, includes_spaces: bool = False) -> str | None:
        """Add a spoken word. Returns the caption to show, or None if the word is blank."""
        if not word.strip():
            return None
        # A new spoken line (new voice context) or a new burst of speech replaces the old caption.
        if self._fresh or context_id != self._context:
            self.text = ""
        self._fresh = False
        self._context = context_id
        if includes_spaces:  # the word already carries its own spacing (e.g. Chinese or Japanese)
            self.text += word
        else:
            self.text += (" " if self.text else "") + word.strip()
        return self.text.strip()

    def speech_ended(self):
        """The voice went quiet: keep showing the line, but the next word starts a fresh one."""
        self._fresh = True


class CaptionSync(FrameProcessor):
    """Place directly after `transport.output()`.

    Every frame is forwarded untouched: the assistant aggregator after this processor
    builds the chat history from the same text frames. Captions go back to the browser
    as upstream messages, which the output transport sends to the data channel.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._builder = CaptionBuilder()
        self._started_at: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._started_at = time.monotonic()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._builder.speech_ended()
        elif isinstance(frame, TTSTextFrame):
            text = self._builder.add_word(frame.text, frame.context_id, frame.includes_inter_frame_spaces)
            if text is None:
                return
            if self._started_at is not None:  # live check that words land with the audio
                logger.debug(f"caption +{time.monotonic() - self._started_at:.2f}s after audio start: {frame.text!r}")
            await self.push_frame(
                OutputTransportMessageUrgentFrame(
                    message={"type": MESSAGE_TYPE, "kind": "speech", "text": text}
                ),
                FrameDirection.UPSTREAM,
            )

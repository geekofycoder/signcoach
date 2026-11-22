"""The Pipecat pipeline for one learner's session."""

import asyncio
import os

from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import OutputTransportMessageUrgentFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import TransportParams, VideoInSourceParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from .captions import CaptionSync
from .coach import MESSAGE_TYPE, VisionCoach
from .judge import SignJudge
from .lessons import SIGNS

CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-5-5")
MAX_SESSION_SECONDS = int(os.getenv("MAX_SESSION_SECONDS", "900"))  # 0 = no limit
CONNECT_TIMEOUT = 45  # seconds to wait for the learner's browser to finish connecting

SYSTEM_PROMPT = f"""You are a warm, patient American Sign Language (ASL) coach talking with a \
learner by voice while they practise in front of their webcam.

How the session works:
- A separate vision system watches the camera, decides whether the learner's handshape is right, \
and speaks corrections itself. You do NOT judge handshapes and must never claim to see the camera.
- Your job is the conversation: answer questions about signs and Deaf culture, encourage the \
learner, and act on spoken requests such as "skip", "go back to A" or "say that again".
- Use the tools to change the lesson. Signs available: {", ".join(s.id for s in SIGNS)}.

Speaking style: your words are spoken aloud. Keep replies to one or two short sentences, with no \
lists, markdown or emoji. If a request is unclear, ask one short question."""


def _tools() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="choose_sign",
                description="Switch the lesson to a specific sign the learner asked for.",
                properties={"sign_id": {"type": "string", "enum": [s.id for s in SIGNS]}},
                required=["sign_id"],
            ),
            FunctionSchema(
                name="skip_sign",
                description="Move on to the next sign when the learner wants to skip.",
                properties={},
                required=[],
            ),
            FunctionSchema(
                name="repeat_sign",
                description="Repeat the instructions for the current sign.",
                properties={},
                required=[],
            ),
        ]
    )


async def end_if_never_connected(worker, connected: asyncio.Event, timeout: float = CONNECT_TIMEOUT):
    """If the connection never completes (for example the relay failed), stop the pipeline.

    Otherwise it would keep its speech-to-text connection open and hold a session slot forever.
    """
    try:
        await asyncio.wait_for(connected.wait(), timeout)
    except asyncio.TimeoutError:
        logger.warning("Learner never finished connecting; ending the session")
        await worker.cancel()


async def end_after(worker, seconds: float, notice_seconds: float = 1.0):
    """Stop the session when its time is up, after telling the page why."""
    await asyncio.sleep(seconds)
    logger.info("Session time limit reached")
    await worker.queue_frames(
        [OutputTransportMessageUrgentFrame(
            message={"type": MESSAGE_TYPE, "kind": "session_end", "reason": "time_limit"})]
    )
    await asyncio.sleep(notice_seconds)  # let the notice go out before the connection closes
    await worker.cancel()


async def run_bot(webrtc_connection: SmallWebRTCConnection):
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            video_in_enabled=True,
            # One frame a second is plenty for a static handshape and keeps API cost down.
            video_in_sources={"camera": VideoInSourceParams(framerate=1)},
        ),
    )

    coach = VisionCoach(SignJudge())

    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts_kwargs = {"api_key": os.environ["CARTESIA_API_KEY"]}
    if os.getenv("CARTESIA_VOICE_ID"):
        tts_kwargs["voice_id"] = os.environ["CARTESIA_VOICE_ID"]
    tts = CartesiaTTSService(**tts_kwargs)

    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(model=CHAT_MODEL, max_tokens=400),
    )

    # The coach already speaks the new instructions when the lesson changes, so each
    # tool result tells the chat model not to say them again.
    quiet = "The coach is already announcing this on screen and aloud. Do not repeat it."

    async def choose_sign(params):
        ok = await coach.select_sign(params.arguments.get("sign_id", ""))
        await params.result_callback({"ok": ok, "note": quiet})

    async def skip_sign(params):
        await coach.skip_sign()
        await params.result_callback({"ok": True, "note": quiet})

    async def repeat_sign(params):
        await coach.repeat_sign()
        await params.result_callback({"ok": True, "note": quiet})

    llm.register_function("choose_sign", choose_sign)
    llm.register_function("skip_sign", skip_sign)
    llm.register_function("repeat_sign", repeat_sign)

    context = LLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=_tools(),
    )
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            coach,  # camera frames end here; everything else flows on
            stt,
            user_agg,
            llm,
            tts,
            transport.output(),
            CaptionSync(),  # captions follow the audio, so it must come after the transport
            assistant_agg,
        ]
    )
    worker = PipelineWorker(pipeline, params=PipelineParams())

    connected = asyncio.Event()

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        logger.info("Learner connected")
        connected.set()
        await coach.start()

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        logger.info("Learner left")
        await worker.cancel()

    @transport.event_handler("on_app_message")
    async def on_app_message(transport, message, sender):
        await coach.handle_client_message(message)

    # Both timers start now, not when the learner connects: a session that never connects must
    # still end. They are cancelled when the session ends normally.
    timers = [asyncio.create_task(end_if_never_connected(worker, connected))]
    if MAX_SESSION_SECONDS > 0:
        timers.append(asyncio.create_task(end_after(worker, MAX_SESSION_SECONDS)))

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        for t in timers:
            t.cancel()

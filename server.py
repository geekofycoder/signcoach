"""Web server: serves the page and answers the browser's WebRTC offer.

Run:  python server.py   then open http://localhost:7860
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import uvicorn  # noqa: E402
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pipecat.transports.smallwebrtc.request_handler import (  # noqa: E402
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from signcoach.access import SessionCounter, passcode_ok  # noqa: E402
from signcoach.bot import run_bot  # noqa: E402
from signcoach.ice import IceProvider, server_servers  # noqa: E402

REQUIRED_KEYS = ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID")
STATIC = Path(__file__).parent / "static"

app = FastAPI()
handler = SmallWebRTCRequestHandler()
ice = IceProvider.from_env()  # a TURN relay when the Cloudflare variables are set, plain STUN otherwise
sessions = SessionCounter(int(os.getenv("MAX_SESSIONS", "2")))
# Reference pictures of each sign live in static/signs and are shown on the page.
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def require_passcode(request: Request):
    """Guards everything that costs money or hands out relay credentials. APP_PASSCODE unset = open."""
    if not passcode_ok(request.headers.get("x-passcode"), os.getenv("APP_PASSCODE")):
        raise HTTPException(status_code=401, detail="Wrong or missing passcode")


async def run_session(connection):
    sessions.acquire()
    try:
        await run_bot(connection)
    finally:
        sessions.release()  # always, even if the learner never finished connecting


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def config():
    """Public: tells the page whether to ask for a passcode."""
    return {"passcode_required": bool(os.getenv("APP_PASSCODE")), "relay": ice.relay_enabled}


@app.get("/api/ice", dependencies=[Depends(require_passcode)])
async def ice_servers():
    return {"iceServers": await ice.servers()}


@app.post("/api/offer", dependencies=[Depends(require_passcode)])
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    if sessions.full():
        raise HTTPException(status_code=503, detail="The coach is busy with other learners. Try again in a few minutes.")
    if ice.relay_enabled:  # the server connects out to the same relay as the browser
        handler.update_ice_servers(server_servers(await ice.servers()))

    async def on_connection(connection):
        background_tasks.add_task(run_session, connection)

    return await handler.handle_web_request(request=request, webrtc_connection_callback=on_connection)


@app.patch("/api/offer", dependencies=[Depends(require_passcode)])
async def ice_candidate(request: SmallWebRTCPatchRequest):
    await handler.handle_patch_request(request)
    return {"status": "success"}


@app.on_event("shutdown")
async def shutdown():
    await handler.close()


if __name__ == "__main__":
    missing = [k for k in REQUIRED_KEYS if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)} (copy .env.example to .env)")
    # The page uses your camera and microphone, so it listens on localhost only unless
    # HOST says otherwise (the Docker image sets HOST=0.0.0.0).
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "7860")))

# Sign Coach

A Pipecat voice and vision coach that watches your webcam while you practise American Sign Language
handshapes. Claude checks your hand about once a second. The page shows visual cues on top of your
video, and the coach speaks up when you get something wrong or struggle.

## Run

Needs Python 3.10 to 3.13 (pipecat has no wheels for 3.15 yet).

```powershell
uv venv --python 3.12 .venv            # add --native-tls behind a TLS-inspecting proxy
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy .env.example .env                 # fill in the three API keys
.venv\Scripts\python.exe server.py     # then open http://localhost:7860
```

Keys: Anthropic (vision and chat), Deepgram (speech to text), Cartesia (text to speech).
Cartesia also needs `CARTESIA_VOICE_ID`: there is no default voice, and without one every spoken line fails
with "You must provide a voice ID".

`.venv\Scripts\python.exe tests\test_offline.py` runs the offline checks of the coaching logic.

## How it works

```
browser (WebRTC: mic + camera)  <->  SmallWebRTC transport
  camera frames, 1 per second ->  VisionCoach  -> Claude (image + target sign -> JSON verdict)
                                      |-> on-screen cues   (data channel, drawn on a canvas)
                                      |-> spoken guidance  (TTSSpeakFrame)
  mic -> Deepgram -> Claude chat (tools: choose_sign, skip_sign, repeat_sign) -> Cartesia -> speaker
```

| File | Job |
|---|---|
| `signcoach/lessons.py` | The 10 static signs (A B C L O V W Y, I love you, 5) and lesson progress |
| `signcoach/judge.py` | The Claude vision call. Returns verdict, problem part, one spoken fix, hand box |
| `signcoach/policy.py` | When to praise, correct, offer help or advance. Pure logic, tested offline |
| `signcoach/coach.py` | Pipecat processor: samples frames, runs the judge, emits cues and speech |
| `signcoach/captions.py` | Caption sync: shows each word on screen as it is spoken (coach lines and Claude's replies) |
| `signcoach/ice.py` | TURN relay credentials from Cloudflare, prepared for the browser and for the server |
| `signcoach/access.py` | Passcode check and the cap on simultaneous sessions |
| `signcoach/bot.py` | Pipeline: transport, coach, Deepgram, Claude chat, Cartesia, caption sync, session time limit |
| `static/index.html` | Webcam page with overlay, sign card with reference drawing, progress and buttons |
| `static/signs/` | Reference drawings of each sign, with authors and licenses in `CREDITS.md` |

Coaching rules (in `policy.py`): a sign counts after 2 correct checks in a row; a spoken correction
needs 2 wrong checks in a row and waits 8 seconds before the next; the same sentence is never said
twice in a row; after 3 spoken corrections on one sign the coach offers to skip.

## Settings

`VISION_MODEL` and `CHAT_MODEL` default to `claude-opus-5-5`. Smaller models such as `claude-haiku-5-5`
answer faster and cost less; try that if the feedback feels slow. `VISION_EFFORT` defaults to `low`.

## Deploy on Railway

Railway only accepts HTTP and TCP from the internet, and WebRTC needs UDP, so the camera and voice go
through a TURN relay (Cloudflare's, free up to 1,000 GB a month). The server fetches short-lived relay
credentials and gives them to the page, so the Cloudflare token never reaches the browser.

1. Get a TURN relay. Either create a TURN key in Cloudflare (Realtime > TURN; note the key ID and API token;
   the dashboard asks for a payment card), or use any other provider and paste the ICE servers array from
   its dashboard into `ICE_SERVERS_JSON`.
2. In Railway, create a service from this GitHub repo. It builds the `Dockerfile` by itself.
3. Set these variables on the service:

| Variable | Needed | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY` | yes | The three service keys |
| `CARTESIA_VOICE_ID` | yes | Voice to speak with |
| `CLOUDFLARE_TURN_KEY_ID`, `CLOUDFLARE_TURN_API_TOKEN` | one relay option is needed on Railway | Cloudflare TURN relay |
| `METERED_USERNAME`, `METERED_PASSWORD` | a third relay option | A Metered credential. Free plan: 500 MB a month, then the relay stops until the next cycle. Used only when neither of the other two options is set |
| `ICE_SERVERS_JSON` | the other relay option | Relay servers from any provider, as a JSON array on one line. Ignored when the Cloudflare values are set. With neither, the app uses plain STUN, which only works where UDP gets through |
| `APP_PASSCODE` | strongly advised | The page asks for it. Without one, anyone with the address spends your API credits |
| `MAX_SESSION_SECONDS`, `MAX_SESSIONS` | optional | Default 900 seconds and 2 at once |
| `VISION_MODEL`, `CHAT_MODEL` | optional | Smaller models cost less |

4. Under Settings > Networking, generate a public domain. Browsers need HTTPS for the camera, and
   Railway domains are HTTPS.
5. Optional: set the healthcheck path to `/health`.

To check the relay from your own machine before deploying, put the Cloudflare values in `.env`, start the
server and open `http://localhost:7860/?relay=1`. That forces every connection through the relay.

Railway has deprecated `railway.json`, so there is no config file for it here.

## Limits

- Static handshapes only. Signs with movement (HELLO, THANK YOU) need several frames per check.
- One frame can only show a handshape, not palm orientation over time or facial grammar.
- The hand box and "Check: thumb" label are Claude's estimate and can be off.
- Each second of practice sends a camera frame to the Anthropic API.
- Captions follow the voice word by word, using the timestamps Cartesia returns. To check the timing on
  your machine, look for `caption +N.NNs after audio start` lines in the server log.
- The reference drawings are a guide to the handshape; your mirrored camera preview can make your own
  hand look flipped next to them.
- Not yet tested end to end against live Anthropic, Deepgram and Cartesia services.

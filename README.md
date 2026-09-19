# SPAGASUS JARVIS

A real, functional, voice-controlled AI assistant for your computer —
Iron-Man style. Wake it with **two claps** or **"Hey Jarvis"**, talk to it
naturally, and it opens apps, controls the system, manages files, searches
the web, plays music, remembers facts, runs daily routines, and reports
real system telemetry. Pair your phone for remote control.

The AI core is the **SG Saturn** — a glowing Saturn-style orb that swells
with your voice and changes color by state.

```
                SPAGASUS JARVIS CORE
                        |
            +-----------+-----------+
            |           |           |
         Voice        Text       Mobile App
            |           |           |
            +-----------+-----------+
                        |
                  TOOL ROUTER
            +-----------+-----------+
            |           |           |
      Computer      Files/Web     Devices
      Control       System         & Memory
```

## Quick start

```bash
cd spagasus-jarvis
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
copy .env.example .env          # set PHONE_TOKEN and optional AI keys

.venv/Scripts/python run.py     # foreground (supervisor keeps it alive)
# or background:
.venv/Scripts/pythonw run.py
```

> ⚠️ **Start it only via `run.py` or `pythonw -m backend.main`.** Running
> `uvicorn backend.main:app` directly skips `start()` — the mic never arms
> and voice listening silently stops working. `-m backend.main` runs
> `start()` (which calls `uvicorn.run` itself); `run.py` does the same and
> supervises restarts.

Then open **http://localhost:8790** — the SPAGASUS JARVIS dashboard.
Pair your phone at **http://<your-pc-ip>:8790/m** with the `PHONE_TOKEN`.

### Keeping the phone online + unlocking

- The companion **auto-reconnects** after any Wi-Fi/socket drop (backoff) and
  also heartbeats over HTTP every ~20s, and the server only marks a device
  offline after **5 minutes** of silence — so paired phones stay **online**
  through Wi-Fi blips, locked screens and backgrounded tabs.
- "unlock all devices" pushes an unlock request: the phone **vibrates, beeps,
  shows a notification and a full-screen overlay**, re-alerting every 5s until
  acknowledged.
- A web page **cannot** bypass the phone's lock screen (fingerprint/PIN is OS
  security). JARVIS therefore also unlocks over **ADB** (the only real way from
  the PC): say **"unlock phone"** / **"unlock all devices"** and it wakes the
  screen and dismisses the keyguard on any attached phone.
- **To enable real unlock:** on the phone turn on Developer options → **USB
  debugging** (Android 11+: also Wireless debugging). Connect it:
  - USB: plug in the cable, accept the "Allow USB debugging" prompt.
  - Wireless (Android 11+): Settings → Developer options → Wireless debugging →
    *Pair device with pairing code*, then on the PC run:
    `adb pair <ip:port> <code>` then `adb connect <ip:port>` (IP/port shown on
    the phone). ADB lives at `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`.
  - Swipe / no-PIN locks unlock fully; a fingerprint/PIN lock still needs your
    biometric by design.
- Alternative: Tasker + AutoInput on the phone (Display → Keyguard → Dismiss)
  triggered by this page's notification - works over Wi-Fi without ADB.

## What actually works (real, not mocked)

| Feature | How |
|---|---|
| Wake | Two claps **or** "Hey Jarvis" (voice-activity + STT check) |
| Speech → text | Raw PCM straight to Google's speech API (no flac binary — reliable) |
| Text → speech | Neural edge-tts voice via Windows MCI (pyttsx3 fallback) |
| Continuous conversation | Keeps listening after each reply; no re-clapping |
| Open apps | Real Start-Menu catalog (129+ apps), Store apps via AUMID |
| System monitoring | Real psutil data: CPU, RAM, disk, battery, network, temp |
| Screen | Real screenshots; vision-model adapter when `VISION_API_URL` is set |
| Files | Create / read / search / rename / move; **delete asks confirmation** |
| Web | **Live answers** from the web (DuckDuckGo + Bing + Wikipedia, no API key), open sites, play songs |
| Memory | SQLite: facts, conversation, task log, audit log |
| Routines | "every day at 8 am open chrome" → scheduled daily |
| Mobile | Real phone page: WS pairing, voice (Web Speech), battery, commands, quick actions |
| Confirmation | Shutdown/restart/delete/raw shell always ask "say yes" first |
| Self-healing | `run.py` supervisor restarts the server if it dies |

## Commands you can say

```
"Hey Jarvis" / two claps          wake - greets you by time of day (morning/afternoon/evening/night)
                                   and tells you how many routines are scheduled today
"open whatsapp"                   launch the desktop app
"call arvind" / "call to arvind"  places a REAL WhatsApp Desktop call (chat is
                                   found by name - no number needed, and fuzzy
                                   matching handles mispronunciations: saying
                                   "aravind" still finds "Arvind")
"video call arvind"               starts a WhatsApp video call
"open chrome and open whatsapp"   runs every step of a chain
"open chrome and play shape of you"
"what's my cpu usage"             real psutil
"what's on my screen"             screenshot (+ vision if configured)
"create file notes.txt with buy milk"
"search for today's news"    answers with real results, not just a tab
"who is elon musk"            answers from the web
"play kadhal psycho"
"remember arvind number 98765 43210"   then "call arvind" dials straight to that number
"remember my wifi password is guest1234"
"what do you remember" / "clear my memory"
"every day at 8 am open chrome and open whatsapp"
"my routines" / "disable routine 1"
"what devices are connected"
"shut down"                       asks confirmation first
"run command dir"                 asks confirmation first
```

## Dangerous actions

`delete file`, `shutdown`, `restart`, and `run command` never execute
immediately — JARVIS asks "say yes to confirm" and only then acts. Every
command is written to the audit log.

## AI adapters (optional)

Everything works without them, but for richer answers:

- **LLM** — set `LLM_API_KEY` (+ optional `LLM_BASE_URL`/`LLM_MODEL`) for
  general knowledge responses through an OpenAI-compatible chat API.
- **Vision** — set `VISION_API_URL`; JARVIS POSTs the screenshot as base64
  JSON and uses the returned `description` for "what's on my screen".
- **Whisper** — set `STT_ENGINE=whisper` for fully local speech
  recognition (`pip install openai-whisper`, model downloads on first use).

## Security

- Secrets live only in `.env` (never in the frontend).
- Mobile API/WebSocket require `?token=<PHONE_TOKEN>`.
- Confirmation gate for destructive tools; 500-entry audit log in SQLite.
- Adapters keep credentials server-side.

## API

```
GET   /                    dashboard
GET   /m                   mobile companion
GET   /api/status          status, stats, devices, actions, turns, mic level
POST  /api/wake            trigger a wake cycle
POST  /api/command         {"text": "open chrome"}  (?token= for mobile)
GET   /api/memory          facts + turns + actions
GET/POST/PATCH/DELETE /api/routines
GET   /api/devices
WS    /ws?token=&name=     realtime state + mobile commands
```

## Roadmap (incremental)

1. ✅ UI + SG Saturn core (voice-reactive, status-driven)
2. ✅ Voice in/out (wake word, claps, neural TTS)
3. ✅ Rule-based intents + LLM adapter
4. ✅ Computer control (apps, keys, files, screenshots, power)
5. 🟡 Vision pipeline (adapter ready; needs a vision endpoint)
6. 🟡 Browser automation (Playwright adapter — planned)
7. ✅ Persistent memory (SQLite) + routines
8. ✅ Mobile companion (web app; native Android later)
9. ✅ Multi-device dashboard (PC + phone over WebSocket)

## Project layout

```
backend/        FastAPI server, core, tools, speech, vision, memory, routines
frontend/       Dashboard (HTML/CSS/JS)
web/            Mobile companion page
database/       SQLite DB + screenshots (created at runtime)
config/         Configuration (backend/config.py + .env)
speech/ tools/ vision/ memory/   organized entry points for the modules
run.py          Launcher + supervisor
```

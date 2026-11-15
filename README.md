# MAVIS

MAVIS (Multi-Agent Voice Intake System) is a hospital appointment-intake application. Patients can speak or type their details, receive emergency warning prompts, request an appointment, and track a booking token. Doctors can review appointments and AI-generated intake summaries, while administrators manage the physician roster and review the audit log.

> **Important:** MAVIS is a software project, not a medical device or emergency dispatch service. It does not replace professional medical advice and cannot contact emergency services from the browser. For an emergency, call your local emergency number.

## What you can do

- **Patient portal:** Start an intake without an account, request an appointment, and track a confirmed token.
- **Doctor portal:** Review the appointment queue and clinical handoff summaries, update appointment status, and record a patient-reported follow-up response.
- **Admin portal:** Manage doctor profiles, appointment availability, and review the audit ledger.
- **Voice intake:** Use a microphone or type responses in the browser. Voice features depend on browser support and optional external services.

New installations start with doctor accounts and appointment availability, but no sample patient appointments. Empty tables in the portals explain what to do next.

## Technology

| Technology | What it does in MAVIS |
|---|---|
| Python 3.13 | Runs the backend application and its AI, scheduling, and data-access code. |
| FastAPI and Uvicorn | FastAPI provides the HTTP and WebSocket API; Uvicorn runs the server. FastAPI also serves the web portal. |
| HTML, CSS, and JavaScript | Build the browser interface and its patient, doctor, admin, and voice-intake workflows. There is no separate frontend build step. |
| Tailwind CSS and Google Fonts | Tailwind supplies utility classes from its CDN; Inter and Outfit are loaded from Google Fonts for the interface. |
| Pydantic and email-validator | Validate data sent to the API, including login email addresses and appointment requests. |
| SQLAlchemy, aiosqlite, and asyncpg | SQLAlchemy maps Python objects to database tables. `aiosqlite` connects to the default local database; `asyncpg` connects to PostgreSQL. |
| SQLite and PostgreSQL | SQLite is the no-install local database. PostgreSQL is supported for a separately configured deployment. |
| Redis | Provides shared temporary booking holds and queue data when available. Local development can use MAVIS's in-memory fallback instead. |
| LiveKit | Supplies browser audio-room tokens and the integration point for WebRTC/SIP telephony. A LiveKit server and credentials are required for an actual LiveKit connection. |
| faster-whisper | Optional local speech-to-text fallback. The model is downloaded when first needed, so the first transcription can take longer. |
| Groq API (Whisper and Llama 3) | Groq Whisper can transcribe uploaded audio, and the configured Llama 3 model supports language-model extraction. Both require a Groq API key; deterministic local logic is used when the key or service is unavailable. |
| Edge TTS | Generates the backend's spoken responses. The browser's SpeechSynthesis API is used as a fallback when audio synthesis is unavailable. |
| Piper TTS | Piper is **not currently connected to the runtime**. The portal uses Edge TTS and browser speech synthesis; Piper would require a separate integration before it is used. |
| LangGraph and LangChain Core | Provide the agent/workflow components used for intake and triage. |
| PyJWT and SHA-256 | PyJWT signs and validates login tokens for protected staff routes. The audit ledger uses SHA-256 hashes to link its records. |
| Web Audio API and browser SpeechRecognition | Capture microphone audio and, where supported, provide browser-based speech recognition and playback. |

## Default login credentials

These accounts are created when the backend starts for the first time:

| Role | Email | Password |
|---|---|---|
| Administrator | `admin@mavis.hospital` | `admin123` |
| Doctor | `dr.a@mavis.hospital` | `doctor123` |
| Doctor | `dr.b@mavis.hospital` | `doctor123` |
| Doctor | `dr.c@mavis.hospital` | `doctor123` |
| Doctor | `dr.d@mavis.hospital` | `doctor123` |
| Doctor | `dr.e@mavis.hospital` | `doctor123` |
| Doctor | `dr.f@mavis.hospital` | `doctor123` |

The sign-in form is prefilled with the Dr. B account for local convenience. Patients do not need an account. These credentials are for local development only: do not expose a development deployment to the public internet, and replace or disable the starter credentials before using real patient data.

## Run locally on Windows

### 1. Install prerequisites

Install **Python 3.13** from [python.org](https://www.python.org/downloads/windows/) and select **Add Python to PATH** during setup. Install [Git for Windows](https://git-scm.com/download/win) if you are cloning the repository.

Open PowerShell in the folder containing this README. Verify Python is available:

```powershell
py -3.13 --version
```

If `py` is not recognized, reopen PowerShell after installing Python or use the Python 3.13 executable directly.

### 2. Create and activate a virtual environment

Run these commands from the project folder:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, allow locally installed scripts for your Windows user, then activate again:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\.venv\Scripts\Activate.ps1
```

### 3. Install Python packages

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The standard setup is ready for local use. To add the optional local speech recognizer and LiveKit token support, install:

```powershell
python -m pip install faster-whisper livekit-api
```

### 4. Create your environment file

Copy the example file and open it:

```powershell
Copy-Item .env.example .env
notepad .env
```

SQLite and the Redis in-memory fallback work for a basic local run. To enable Groq-backed transcription and Llama 3 extraction, add your own key from [Groq Console](https://console.groq.com/keys):

If you want to enable Groq, set `GROQ_API_KEY` directly in `backend/config.py`; it is intentionally not read from `.env`. Keep any real key local and do not commit it. The app creates a temporary random JWT secret when `JWT_SECRET` is blank; set a stable random value in your ignored `.env` if you want tokens to remain valid across restarts. PowerShell can generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the generated value into `.env` as `JWT_SECRET="..."`.

### 5. Database setup and schema creation

The default `DATABASE_URL` uses `mavis.db` in the project folder, so no database server is needed. On application startup, MAVIS creates missing tables and ensures the starter logins, doctors, and appointment slots are present.

**There is no Alembic migration directory or migration command in this repository.** The current schema setup uses SQLAlchemy `create_all`; it creates missing tables but does not migrate changed columns in an existing database. Back up any database containing data before changing the schema.

PostgreSQL is optional. To start a local PostgreSQL container with Docker Desktop:

```powershell
docker run --name mavis-postgres -e POSTGRES_USER=mavis -e POSTGRES_PASSWORD=mavis-local-password -e POSTGRES_DB=mavis -p 5432:5432 -d postgres:16
```

Then set this value in `.env` and restart the backend:

```dotenv
DATABASE_URL="postgresql+asyncpg://mavis:mavis-local-password@localhost:5432/mavis"
```

Redis is also optional for local use. To run it in Docker:

```powershell
docker run --name mavis-redis -p 6379:6379 -d redis:7-alpine
```

Keep `REDIS_URL="redis://localhost:6379/0"` in `.env`. Without Redis, the in-memory fallback is used; its state is not shared across multiple backend processes.

### 6. Start the application

In the activated environment, run:

```powershell
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Keep this terminal open. The FastAPI backend serves the frontend as part of the same application, so **there is no separate frontend command or Node.js setup**. Open the portal in another PowerShell window:

```powershell
Start-Process "http://127.0.0.1:8000"
```

Useful local URLs:

- Portal: <http://127.0.0.1:8000>
- Health check: <http://127.0.0.1:8000/api/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>

Stop the server with **Ctrl+C** in the server terminal.

## Use the portals

1. **Patient:** Choose patient access, start a voice intake, and speak or type answers. A booking token is returned only after an available slot is successfully reserved.
2. **Track a booking:** Open the Patient Portal and enter the token shown after confirmation. If no booking exists, the page starts empty and tells you where the token comes from.
3. **Doctor:** Sign in with a doctor account to see that doctor's queue, view available clinical briefs, and update appointment status. After marking an appointment complete, use Follow-Up to record the patient's reported response.
4. **Administrator:** Sign in with the administrator account to manage the physician roster and inspect recorded audit events.

## Notes and limitations

- Audio transcription can use Groq when configured and falls back to local faster-whisper when available. Local model files may be downloaded on first use.
- Browser microphone access generally requires `localhost` or HTTPS and permission in the browser.
- LiveKit requires a reachable LiveKit server and valid API key/secret; the example values in `.env.example` are placeholders.
- The follow-up workflow records a response entered by staff. It does not place an automated outbound phone call.
- The emergency screen provides guidance but does not place a call or connect a user to hospital staff.
- Existing audit records are retained to preserve the hash-linked history. Legacy sample appointments are removed on startup; starter authentication accounts are preserved.

## License

See [LICENSE](LICENSE).

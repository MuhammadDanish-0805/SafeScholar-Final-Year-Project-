# SafeScholar

**AI-powered academic integrity & prompt-safety system** for educational environments.

SafeScholar is a Chrome extension + FastAPI backend that monitors student interactions with generative AI tools (ChatGPT, Gemini, Claude, etc.), classifies risky prompts in real time, logs incidents, and provides a staff dashboard for review and appeals.

---

## Project Structure

```
SafeScholar/
├── extension/                 # Chrome / Edge browser extension
│   ├── manifest.json
│   ├── background.js
│   ├── content.js             # Main detection + UI logic
│   └── page-hook.js
├── backend/                   # FastAPI server (runs on Raspberry Pi or local)
│   ├── main.py                # API entrypoint
│   ├── database.py
│   ├── auth.py
│   ├── scoring.py
│   ├── ai_features.py
│   ├── gemini_client.py       # LLM classifier client
│   ├── qwen_detector.py
│   ├── qwen_document.py
│   ├── qwen_vision.py
│   ├── staff_signup.py
│   ├── requirements.txt
│   └── static/                # Dashboard UI (HTML + logo)
├── .env.example
├── .gitignore
└── README.md
```

---

## Prerequisites

- Python 3.10+
- Google Chrome / Microsoft Edge
- A Google Gemini API key (free tier works)

---

## Backend Setup

```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp ../.env.example .env
# Edit .env and set LLM_API_KEY=...

# Run the server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at `http://127.0.0.1:8000`.  
Dashboard pages are served from the same origin (e.g. `/static/dashboard.html`).

### Raspberry Pi deployment note

1. Copy the `backend/` folder to the Pi.
2. Install the same requirements.
3. Run with a process manager (systemd, supervisor, or `uvicorn` under a user service).
4. Update the extension’s `API_BASE` (and `host_permissions` in `manifest.json`) to the Pi’s IP address.

---

## Extension Setup

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** and select the `extension/` folder
4. The extension will inject into supported AI chat sites and talk to the backend at `http://127.0.0.1:8000` by default.

To point the extension at a remote backend (e.g. Raspberry Pi), edit the `API_BASE` constant at the top of `content.js` and add the host to `host_permissions` in `manifest.json`.

---

## Key Features

- Real-time prompt classification (jailbreak, academic dishonesty, prompt injection, etc.)
- Severity scoring + incident logging
- Staff dashboard with review / appeal workflow
- Document & vision analysis helpers
- Session-based restriction handling

---

## Development Notes

- The SQLite database is created automatically on first run.
- Secrets and the database file are excluded via `.gitignore`.
- Backup / test scripts and virtual environments from development have been removed for a clean repository.

---

## License

This project was developed as a Final Year Project.  
All rights reserved by the project authors unless otherwise stated.

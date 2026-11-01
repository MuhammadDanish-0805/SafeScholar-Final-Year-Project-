"""
main.py
SafeScholar dashboard backend.

Run locally now:
    uvicorn main:app --reload

Later, this runs exactly the same way on the Raspberry Pi —
just copy the folder over, pip install -r requirements.txt, and run
the same uvicorn command (or wire it up as a systemd service).
"""

import hmac
import json
import os
import re
import time
import threading
import requests
from datetime import datetime, timezone
from typing import Optional
from qwen_detector import analyze_with_qwen

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from database import get_connection, init_db
from auth import create_access_token, verify_password, decode_access_token
from scoring import score_prompt, severity_for, action_for
from ai_features import explain_score, llm_flagged_result, second_opinion, weekly_summary
from staff_signup import router as staff_router
from fastapi import UploadFile, File, Form
from qwen_vision import analyze_image_with_qwen
from qwen_document import analyze_document_with_qwen, extract_document_text
import io
import zipfile
from starlette.concurrency import run_in_threadpool
from pathlib import Path
import uuid
from fastapi import Depends
from fastapi.responses import FileResponse

# Private evidence storage (outside the static folder)
EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


# File types that can be stored as evidence.
EVIDENCE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
IMAGE_MIME_TYPES = ("image/png", "image/jpeg")
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def save_evidence_file(file_bytes, mime_type):
    extension = EVIDENCE_EXTENSIONS.get(mime_type)

    if extension is None:
        raise ValueError("Unsupported evidence type")

    filename = f"evidence_{uuid.uuid4().hex}{extension}"
    file_path = EVIDENCE_DIR / filename

    # Exclusive creation prevents overwriting existing evidence.
    with open(file_path, "xb") as evidence_file:
        evidence_file.write(file_bytes)

    return filename


def log_incident_with_evidence(
    *, session_id, device_label, category, score, severity, action,
    breakdown, file_bytes, mime_type, original_filename
):
    """
    Saves the evidence file, then writes the incident and its evidence row in ONE
    transaction. If anything fails, roll back and delete the file so we never keep an
    incident without its file (or a file without an incident). Returns the incident id.
    """
    evidence_filename = save_evidence_file(file_bytes, mime_type)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO incidents
            (timestamp, session_id, severity, category,
             device_label, message, score, action, breakdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                (session_id or "unknown")[:100],
                severity,
                category,
                (device_label or "Unknown device")[:100],
                breakdown["summary"],
                score,
                action,
                json.dumps(breakdown)
            )
        )
        incident_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO incident_evidence
            (incident_id, filename, original_filename, mime_type, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (incident_id, evidence_filename, original_filename, mime_type, now)
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        (EVIDENCE_DIR / evidence_filename).unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail="Could not save the incident and its evidence"
        ) from exc
    finally:
        conn.close()

    return incident_id


app = FastAPI(title="SafeScholar Dashboard API")

# Allow the dashboard frontend (Bootstrap + JS) to call this API
# during local dev, regardless of what port it's served from.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Staff self-signup + email verification: /staff/signup, /staff/verify, /staff/resend
app.include_router(staff_router)


@app.on_event("startup")
def on_startup():
    init_db()
    # Keep the original appeal decision; record administrator unlocks separately.
    conn = get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS appeal_unlocks (
            incident_id INTEGER PRIMARY KEY,
            unlocked_at TEXT NOT NULL
        )""")
        conn.commit()
    finally:
        conn.close()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@app.post("/login", response_model=LoginResponse)
def login(credentials: LoginRequest):
    """
    Checks username/password against admin_users and returns a login
    token on success. Create your first admin with: python create_admin.py
    Staff accounts log in with their staff email and must be verified first.
    """
    conn = get_connection()
    user = conn.execute(
        "SELECT * FROM admin_users WHERE username = ? OR email = ?",
        (credentials.username, credentials.username.strip().lower()),
    ).fetchone()
    conn.close()

    if not user or not verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    # Checked after the password so we don't reveal which emails are registered.
    if not user["is_verified"]:
        raise HTTPException(status_code=403, detail="Verify your staff email before logging in.")

    token = create_access_token(username=user["username"])
    return LoginResponse(access_token=token)


def require_evidence_admin(
    authorization: Optional[str] = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Administrator login required"
        )

    token = authorization.removeprefix("Bearer ").strip()
    username = decode_access_token(token)

    if not username:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired login token"
        )

    conn = get_connection()

    try:
        user = conn.execute(
            """
            SELECT username, is_verified
            FROM admin_users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()
    finally:
        conn.close()

    if not user or not user["is_verified"]:
        raise HTTPException(
            status_code=403,
            detail="Administrator access denied"
        )

    return username



@app.get("/health")
def health_check():
    """Basic liveness check — useful once this moves to the Pi too."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/incidents")
def list_incidents(
    severity: Optional[str] = Query(None, description="Filter by severity"),
    category: Optional[str] = Query(None, description="Filter by category"),
    device_label: Optional[str] = Query(None, description="Filter by device"),
    limit: int = Query(50, le=500),
    admin: str = Depends(require_evidence_admin),
):
    """
    Returns incidents, most recent first. Supports the same filters
    your dashboard's severity/category/device controls will need.
    This is also the endpoint your live feed will poll every 10s.
    """
    conn = get_connection()
    query = "SELECT incidents.*, appeals.status AS appeal_status, appeals.reason AS appeal_reason, EXISTS(SELECT 1 FROM incident_evidence ev WHERE ev.incident_id = incidents.id) AS has_evidence FROM incidents LEFT JOIN appeals ON appeals.incident_id = incidents.id WHERE 1=1"
    params = []

    if severity:
        query += " AND incidents.severity = ?"
        params.append(severity)
    if category:
        query += " AND incidents.category = ?"
        params.append(category)
    if device_label:
        query += " AND incidents.device_label = ?"
        params.append(device_label)

    query += " ORDER BY incidents.timestamp DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    incidents = []
    for row in rows:
        item = dict(row)
        # breakdown is stored as JSON text; send it to the dashboard as an object
        if item.get("breakdown"):
            try:
                item["breakdown"] = json.loads(item["breakdown"])
            except ValueError:
                item["breakdown"] = None
        incidents.append(item)
    return incidents


class AnalyzeRequest(BaseModel):
    prompt: str = Field(..., max_length=20000)
    session_id: str = Field(..., min_length=1, max_length=100)
    device_label: Optional[str] = Field(None, max_length=100)
    # P(malicious) from the DistilBERT classifier, if the extension/classifier sends it.
    # A borderline value (0.40-0.60) lets the AI second review look at prompts the rules missed.
    classifier_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


# Optional shared secret for the extension. If INGEST_API_KEY is set when the server
# starts, /incidents/analyze requires a matching X-API-Key header.
INGEST_API_KEY = os.getenv("INGEST_API_KEY", "")


@app.post("/incidents/analyze")
def analyze_prompt(req: AnalyzeRequest, x_api_key: Optional[str] = Header(None)):
    """
    The Chrome extension sends the raw prompt here. The server scores it with the
    CVSS-style guidelines (scoring.py), saves an incident if anything matched, and
    replies with the score, severity, action and the step-by-step explanation so
    the extension can show a warning/block banner.

    The prompt text itself is NOT stored, only the score, the matched categories
    and short evidence snippets.
    """
    if INGEST_API_KEY and not hmac.compare_digest(x_api_key or "", INGEST_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")

    

    # Existing scoring engine provides the fallback and score
    result = score_prompt(req.prompt)

    # New Qwen AI classification
    


    incident_id = None

    # AI add-ons. scoring.py owns the numbers; the AI can only add a warning for a prompt
    # the rules missed (option 2) or write a plain-English reason (option 1).
    if not result["is_incident"]:
        opinion = second_opinion(req.prompt, req.classifier_confidence)
        if opinion and opinion["label"] == "malicious":
            result = llm_flagged_result(opinion)
    elif "ai_reason" not in result:
        result["ai_reason"] = explain_score(result)

        
    # Qwen AI classification
    qwen_result = analyze_with_qwen(req.prompt)

    if qwen_result is not None:
        category = qwen_result["category"]
        severity = qwen_result["severity"].lower()
        reason = qwen_result["reason"]

        if category != "BENIGN" and not result["is_incident"]:
            severity_scores = {
                "low": 2.0,
                "medium": 4.0,
                "high": 6.0,
                "critical": 6.0
            }

            result["is_incident"] = True
            result["score"] = severity_scores[severity]
            result["severity"] = (
                "medium" if severity != "low" else "low"
            )
            result["action"] = "warn"
            result["primary_category"] = category
            result["summary"] = "Qwen flagged: " + category
            result["steps"] = [
                "AI classification: " + category,
                "AI assessment: " + severity,
                "Reason: " + reason,
                "Flagged for review; existing blocking rules unchanged."
            ]

        result["ai_reason"] = reason


    if result["is_incident"]:
        conn = get_connection()
        cursor = conn.execute(
            """
            INSERT INTO incidents
                (timestamp, session_id, severity, category, device_label, message,
                 score, action, breakdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                req.session_id,
                result["severity"],
                result["primary_category"],
                req.device_label,
                result["summary"],
                result["score"],
                result["action"],
                json.dumps(result),
            ),
        )
        incident_id = cursor.lastrowid
        conn.commit()
        conn.close()

    return {
        "incident_id": incident_id,
        "is_incident": result["is_incident"],
        "score": result["score"],
        "severity": result["severity"],
        "action": result["action"],
        "blocked": result["action"] == "block",
        "category": result["primary_category"],
        "steps": result["steps"],
        "ai_reason": result.get("ai_reason"),
            }


class AppealRequest(BaseModel):
    incident_id: int
    reason: str = Field(..., min_length=5, max_length=2000)
    session_id: str = Field(..., min_length=1, max_length=100)


@app.post("/appeals")
def submit_appeal(req: AppealRequest):
    conn = get_connection()
    try:
        incident = conn.execute("SELECT id, severity, session_id FROM incidents WHERE id=?", (req.incident_id,)).fetchone()
        if not incident or incident["severity"].lower() != "critical":
            raise HTTPException(400, "Only Critical incidents can be appealed")
        if not hmac.compare_digest(incident["session_id"], req.session_id):
            raise HTTPException(403, "Session does not match the incident")
        existing = conn.execute("SELECT id FROM appeals WHERE incident_id=?", (req.incident_id,)).fetchone()
        if existing:
            raise HTTPException(409, "Appeal already submitted")
        conn.execute("INSERT INTO appeals(incident_id, reason, submitted_at) VALUES(?,?,?)", (req.incident_id, req.reason.strip(), datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return {"status": "pending", "incident_id": req.incident_id}
    finally:
        conn.close()


class AppealDecision(BaseModel):
    status: str


@app.patch("/appeals/{incident_id}")
def decide_appeal(incident_id: int, req: AppealDecision, authorization: Optional[str] = Header(None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    username = decode_access_token(token)
    if not username:
        raise HTTPException(401, "Admin login required")
    if req.status not in ("approved", "rejected"):
        raise HTTPException(400, "Decision must be approved or rejected")
    conn = get_connection()
    try:
        cursor = conn.execute("UPDATE appeals SET status=?, reviewed_at=?, reviewed_by=? WHERE incident_id=? AND status='pending'", (req.status, datetime.now(timezone.utc).isoformat(), username, incident_id))
        if cursor.rowcount == 0:
            raise HTTPException(409, "No pending appeal for this incident")
        conn.commit()
        return {"incident_id": incident_id, "status": req.status}
    finally:
        conn.close()


# Administrator-configured PIN; never ship this secret in the browser extension.
# Set SAFESCHOLAR_UNLOCK_PIN to exactly four digits in the backend environment.
_unlock_attempts = {}
_unlock_guard = threading.Lock()

class UnlockRequest(BaseModel):
    incident_id: int
    session_id: str = Field(..., min_length=1, max_length=100)
    pin: str = Field(..., pattern=r"^[0-9]{4}$")

@app.post("/appeals/unlock")
def unlock_rejected_appeal(req: UnlockRequest):
    # Default admin unlock PIN is 0512. Set SAFESCHOLAR_UNLOCK_PIN to override it.
    configured_pin = os.environ.get("SAFESCHOLAR_UNLOCK_PIN", "0512")
    if not re.fullmatch(r"[0-9]{4}", configured_pin):
        raise HTTPException(503, "Admin unlock PIN is not configured on the server")
    key = req.session_id
    now = time.monotonic()
    with _unlock_guard:
        attempts, blocked_until = _unlock_attempts.get(key, (0, 0.0))
        if now < blocked_until:
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes")
        if not hmac.compare_digest(req.pin, configured_pin):
            attempts += 1
            _unlock_attempts[key] = (attempts, now + 900 if attempts >= 5 else 0.0)
            raise HTTPException(403, "Incorrect unlock PIN")
        _unlock_attempts.pop(key, None)
    conn = get_connection()
    try:
        incident = conn.execute("SELECT session_id, severity FROM incidents WHERE id=?", (req.incident_id,)).fetchone()
        if not incident or not hmac.compare_digest(incident["session_id"], req.session_id) or incident["severity"].lower() != "critical":
            raise HTTPException(404, "Restricted incident not found")
        appeal = conn.execute("SELECT 1 FROM appeals WHERE incident_id=? AND status='rejected'", (req.incident_id,)).fetchone()
        if not appeal:
            raise HTTPException(409, "No rejected appeal to unlock")
        conn.execute("INSERT OR REPLACE INTO appeal_unlocks (incident_id, unlocked_at) VALUES (?, ?)",
                     (req.incident_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return {"status": "unlocked", "incident_id": req.incident_id}
    finally:
        conn.close()


@app.get("/appeals/session/restriction")
def session_restriction(session_id: str = Query(..., min_length=1, max_length=100)):
    """Read-only check for rejected Critical appeals tied to this browser session."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT appeals.incident_id FROM appeals
            JOIN incidents ON incidents.id = appeals.incident_id
            WHERE incidents.session_id = ? AND LOWER(incidents.severity) = 'critical'
              AND appeals.status = 'rejected'
              AND NOT EXISTS (SELECT 1 FROM appeal_unlocks u WHERE u.incident_id = appeals.incident_id)
            LIMIT 1
        """, (session_id,)).fetchone()
        return {"blocked": row is not None}
    finally:
        conn.close()


@app.get("/appeals/{incident_id}/status")
def appeal_status(incident_id: int, session_id: str):
    conn = get_connection()
    try:
        incident = conn.execute("SELECT session_id FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not incident or not hmac.compare_digest(incident["session_id"], session_id):
            raise HTTPException(404, "Incident not found")
        appeal = conn.execute("SELECT status FROM appeals WHERE incident_id=?", (incident_id,)).fetchone()
        if appeal and appeal["status"] == "rejected":
            unlocked = conn.execute("SELECT 1 FROM appeal_unlocks WHERE incident_id=?", (incident_id,)).fetchone()
            if unlocked:
                return {"status": "unlocked"}
        return {"status": appeal["status"] if appeal else "not_submitted"}
    finally:
        conn.close()


@app.get("/incidents/summary")
def incidents_summary(admin: str = Depends(require_evidence_admin)):
    """Quick counts by severity — handy for dashboard summary cards/badges."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT severity, COUNT(*) as count FROM incidents GROUP BY severity"
    ).fetchall()
    conn.close()
    return {row["severity"]: row["count"] for row in rows}


@app.get("/incidents/weekly-summary")
def incidents_weekly_summary(
    refresh: bool = Query(False, description="Skip the 10-minute cache"),
    admin: str = Depends(require_evidence_admin),
):
    """Short staff briefing for the last 7 days (AI-written when an API key is set)."""
    conn = get_connection()
    try:
        return weekly_summary(conn, force=refresh)
    finally:
        conn.close()


# Mounted AFTER the API routes above so /login, /incidents, etc. are
# always matched first. Visit http://127.0.0.1:8000/app/index.html
# to open the dashboard login page.
app.mount("/app", StaticFiles(directory="static", html=True), name="static")


# AI image severity -> score. Labels and actions then come from scoring.py, so the
# image path follows the same policy as text (block >= 7.0, warn 4.0-6.9).
# To stop the AI from ever blocking on its own, lower HIGH/CRITICAL below 7.0.
IMAGE_SEVERITY_SCORES = {"LOW": 2.0, "MEDIUM": 4.0, "HIGH": 7.0, "CRITICAL": 9.0}


@app.post("/incidents/analyze-image")
async def analyze_image(
    file: UploadFile = File(...),
    prompt: str = Form(""),
    session_id: str = Form("unknown"),
    device_label: str = Form("Unknown device"),
    x_api_key: Optional[str] = Header(None)
):
    # Preserve the existing optional API-key check
    if INGEST_API_KEY and not hmac.compare_digest(
        x_api_key or "", INGEST_API_KEY
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")

    allowed_types = {"image/png", "image/jpeg"}
    max_size = 5 * 1024 * 1024  # 5 MB

    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=415,
            detail="Only PNG and JPEG images are supported"
        )

    image_bytes = await file.read(max_size + 1)

    if len(image_bytes) > max_size:
        raise HTTPException(status_code=413, detail="Image exceeds the 5 MB limit")

    # Verify file signatures rather than trusting the MIME type
    is_png = image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = image_bytes.startswith(b"\xff\xd8\xff")

    if not (
        (file.content_type == "image/png" and is_png)
        or (file.content_type == "image/jpeg" and is_jpeg)
    ):
        raise HTTPException(status_code=415, detail="Invalid image file")

    try:
        result = await run_in_threadpool(
            analyze_image_with_qwen,
            prompt,
            image_bytes,
            file.content_type
        )
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Image analysis unavailable or invalid AI response"
        ) from exc

    category = result["category"]
    ai_severity = result["severity"]

    # Benign images are not logged and nothing is saved.
    if category == "BENIGN":
        return {
            "status": "analyzed",
            "incident_id": None,
            "is_incident": False,
            "filename": file.filename,
            "category": category,
            "severity": "none",
            "ai_severity": ai_severity,
            "score": 0.0,
            "action": "none",
            "reason": result["reason"],
            "image_summary": result["image_summary"],
            "logged": False
        }

    score = IMAGE_SEVERITY_SCORES[ai_severity]
    severity = severity_for(score)
    action = action_for(score)

    breakdown = {
        "is_incident": True,
        "score": score,
        "severity": severity,
        "action": action,
        "primary_category": category,
        "summary": "Qwen image detection: " + category,
        "ai_reason": result["reason"],
        "image_summary": result["image_summary"],
        "ai_assessment": ai_severity,
        "steps": [
            "AI image classification: " + category,
            "AI assessment: " + ai_severity,
            "Reason: " + result["reason"],
            "Image summary: " + result["image_summary"]
        ]
    }

    incident_id = log_incident_with_evidence(
        session_id=session_id,
        device_label=device_label,
        category=category,
        score=score,
        severity=severity,
        action=action,
        breakdown=breakdown,
        file_bytes=image_bytes,
        mime_type=file.content_type,
        original_filename=file.filename
    )

    return {
        "status": "analyzed",
        "incident_id": incident_id,
        "is_incident": True,
        "filename": file.filename,
        "category": category,
        "severity": severity,
        "ai_severity": ai_severity,
        "score": score,
        "action": action,
        "reason": result["reason"],
        "image_summary": result["image_summary"],
        "logged": True
    }


DOCUMENT_MAX_SIZE = 15 * 1024 * 1024  # 15 MB


def detect_document_type(data):
    """Identify the file from its first bytes, not from the name the client sent."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            return None
    return None


@app.post("/incidents/analyze-document")
async def analyze_document(
    file: UploadFile = File(...),
    prompt: str = Form(""),
    session_id: str = Form("unknown"),
    device_label: str = Form("Unknown device"),
    x_api_key: Optional[str] = Header(None)
):
    if INGEST_API_KEY and not hmac.compare_digest(
        x_api_key or "", INGEST_API_KEY
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")

    data = await file.read(DOCUMENT_MAX_SIZE + 1)

    if len(data) > DOCUMENT_MAX_SIZE:
        raise HTTPException(status_code=413, detail="Document exceeds the 15 MB limit")

    kind = detect_document_type(data)

    if kind is None:
        raise HTTPException(
            status_code=415,
            detail="Only PDF and Word (.docx) documents are supported"
        )

    mime_type = "application/pdf" if kind == "pdf" else DOCX_MIME

    try:
        text = await run_in_threadpool(extract_document_text, kind, data)
    except ImportError as exc:
        print(
            "[SafeScholar] PDF reading is not installed. Run:  python -m pip install pypdf",
            flush=True
        )
        raise HTTPException(
            status_code=503,
            detail="PDF reading is not installed on the server (run: python -m pip install pypdf)"
        ) from exc

    if not text.strip():
        # Nothing to read (scanned PDF, encrypted or damaged file). It is logged at LOW
        # severity and allowed, so the administrator can still see that it was sent.
        category = "OTHER_SUSPICIOUS"
        ai_severity = "LOW"
        reason = (
            "The document has no readable text (for example a scanned or "
            "protected PDF), so it could not be checked automatically."
        )
        document_summary = "Unreadable document: " + (file.filename or "unnamed")
    else:
        try:
            result = await run_in_threadpool(
                analyze_document_with_qwen,
                prompt,
                file.filename,
                text
            )
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Document analysis unavailable or invalid AI response"
            ) from exc

        category = result["category"]
        ai_severity = result["severity"]
        reason = result["reason"]
        document_summary = result["document_summary"]

    # Benign documents are not logged and nothing is saved.
    if category == "BENIGN":
        return {
            "status": "analyzed",
            "incident_id": None,
            "is_incident": False,
            "filename": file.filename,
            "category": category,
            "severity": "none",
            "ai_severity": ai_severity,
            "score": 0.0,
            "action": "none",
            "reason": reason,
            "document_summary": document_summary,
            "logged": False
        }

    score = IMAGE_SEVERITY_SCORES[ai_severity]
    severity = severity_for(score)
    action = action_for(score)

    breakdown = {
        "is_incident": True,
        "score": score,
        "severity": severity,
        "action": action,
        "primary_category": category,
        "summary": "Qwen document detection: " + category,
        "ai_reason": reason,
        "document_summary": document_summary,
        "ai_assessment": ai_severity,
        "steps": [
            "AI document classification: " + category,
            "AI assessment: " + ai_severity,
            "Reason: " + reason,
            "Document summary: " + document_summary
        ]
    }

    incident_id = log_incident_with_evidence(
        session_id=session_id,
        device_label=device_label,
        category=category,
        score=score,
        severity=severity,
        action=action,
        breakdown=breakdown,
        file_bytes=data,
        mime_type=mime_type,
        original_filename=file.filename
    )

    return {
        "status": "analyzed",
        "incident_id": incident_id,
        "is_incident": True,
        "filename": file.filename,
        "category": category,
        "severity": severity,
        "ai_severity": ai_severity,
        "score": score,
        "action": action,
        "reason": reason,
        "document_summary": document_summary,
        "logged": True
    }


@app.get("/incidents/{incident_id}/evidence")
def get_incident_evidence(
    incident_id: int,
    admin: str = Depends(require_evidence_admin)
):
    conn = get_connection()

    try:
        evidence = conn.execute(
            """
            SELECT filename, mime_type, original_filename
            FROM incident_evidence
            WHERE incident_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (incident_id,)
        ).fetchone()
    finally:
        conn.close()

    if evidence is None:
        raise HTTPException(
            status_code=404,
            detail="No evidence found for this incident"
        )

    # Resolve the filename within the private evidence directory.
    evidence_path = (EVIDENCE_DIR / evidence["filename"]).resolve()

    if not evidence_path.is_relative_to(EVIDENCE_DIR.resolve()):
        raise HTTPException(
            status_code=403,
            detail="Invalid evidence path"
        )

    if not evidence_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Evidence file not found"
        )

    if evidence["mime_type"] not in EVIDENCE_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Unsupported evidence type"
        )

    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"
    }

    if evidence["mime_type"] in IMAGE_MIME_TYPES:
        # Screenshots are shown inline in the dashboard.
        return FileResponse(
            path=str(evidence_path),
            media_type=evidence["mime_type"],
            headers=headers
        )

    # PDF / Word files are always sent as a download, never opened by the browser.
    extension = EVIDENCE_EXTENSIONS[evidence["mime_type"]]
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", evidence["original_filename"] or "")
    base = base.strip(" .")[:80] or f"incident_{incident_id}_evidence"
    if not base.lower().endswith(extension):
        base += extension

    return FileResponse(
        path=str(evidence_path),
        media_type=evidence["mime_type"],
        filename=base,
        headers=headers
    )

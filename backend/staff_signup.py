"""
staff_signup.py
Staff self-signup with email-code verification for the admin dashboard.

Only addresses ending in exactly "edu@gmail.com" can register, and the
account can't log in until the emailed 6-digit code is entered. The code is
what proves the person really owns that staff mailbox, so a student can't
sign up by just typing a staff address.

Staff accounts live in the same admin_users table as your existing admin.
The staff email is also stored as their username, so /login works as before.

Email sending (optional for local dev):
    If SMTP_HOST isn't set, the code is printed in the uvicorn console instead.
    For real emails set these environment variables before starting uvicorn:
        SMTP_HOST=smtp.gmail.com  SMTP_PORT=587
        SMTP_USER=you@gmail.com   SMTP_PASS=<gmail app password>
"""

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from auth import SECRET_KEY, hash_password
from database import get_connection

# ----------------------------- CONFIG -----------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

EMAIL_SUFFIX = "edu@gmail.com"  # the part every allowed address must end with
DEPARTMENTS = {"FPGS", "CID", "EED", "MED", "ADMIN"}
CODE_TTL_MINUTES = 10
MAX_CODE_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60

# Matches the WHOLE address: some characters, then exactly "edu@gmail.com".
# Accepts: ahmadedu@gmail.com, siti.rahmahedu@gmail.com
# Rejects: edu@gmail.com (nothing before "edu"), ahmadedu@gmail.com.evil.com, ahmad@gmail.com
EMAIL_RE = re.compile(r"[a-z0-9._-]+" + re.escape(EMAIL_SUFFIX))
STAFF_ID_RE = re.compile(r"[A-Za-z0-9-]{3,20}")

router = APIRouter(prefix="/staff", tags=["staff-signup"])


# ----------------------------- HELPERS ----------------------------
@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> datetime:
    return datetime.now(timezone.utc)


def hash_code(email: str, code: str) -> str:
    return hmac.new(SECRET_KEY.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def issue_code(db: sqlite3.Connection, email: str) -> str:
    """Generate a fresh 6-digit code, store only its hash, and reset the attempt counter."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.execute(
        """UPDATE admin_users
           SET code_hash = ?, code_expires_at = ?, code_attempts = 0, code_sent_at = ?
           WHERE email = ?""",
        (
            hash_code(email, code),
            (now() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat(),
            now().isoformat(),
            email,
        ),
    )
    return code


def send_code_email(email: str, name: str, code: str):
    if not SMTP_HOST:
        print(f"[DEV] Verification code for {email}: {code}")
        return
    msg = EmailMessage()
    msg["Subject"] = "Your SafeScholar verification code"
    msg["From"] = SMTP_FROM
    msg["To"] = email
    msg.set_content(
        f"Hi {name},\n\n"
        f"Your SafeScholar admin dashboard verification code is: {code}\n"
        f"It expires in {CODE_TTL_MINUTES} minutes.\n\n"
        "If you didn't create this account, you can ignore this email."
    )
    try:
        if SMTP_PORT == 465:
            # Port 465 is SSL from the first byte, so STARTTLS would make the server hang up.
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
    except Exception as exc:  # runs in the background, so just log it
        print(f"[ERROR] Could not send verification email to {email}: {exc}")


# ----------------------------- SCHEMAS ----------------------------
class SignupIn(BaseModel):
    full_name: str
    staff_id: str
    email: str
    department: str
    password: str
    confirm_password: str

    @field_validator("full_name")
    @classmethod
    def _name(cls, v):
        v = " ".join(v.split())
        if not 2 <= len(v) <= 100:
            raise ValueError("Enter your full name.")
        return v

    @field_validator("staff_id")
    @classmethod
    def _staff_id(cls, v):
        v = v.strip().upper()
        if not STAFF_ID_RE.fullmatch(v):
            raise ValueError("Staff ID must be 3-20 letters, numbers or hyphens.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip().lower()
        if not EMAIL_RE.fullmatch(v):
            raise ValueError(f"Use your staff email ending in {EMAIL_SUFFIX}.")
        return v

    @field_validator("department")
    @classmethod
    def _department(cls, v):
        v = v.strip().upper()
        if v not in DEPARTMENTS:
            raise ValueError("Select a department.")
        return v

    @field_validator("password")
    @classmethod
    def _password(cls, v):
        if len(v) < 8 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("Password needs at least 8 characters, including a letter and a number.")
        if len(v.encode("utf-8")) > 72:  # bcrypt's limit
            raise ValueError("Password is too long (72 bytes max).")
        return v

    @model_validator(mode="after")
    def _passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        return self


class VerifyIn(BaseModel):
    email: str
    code: str

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return v.strip().lower()

    @field_validator("code")
    @classmethod
    def _code(cls, v):
        v = v.strip()
        if not re.fullmatch(r"\d{6}", v):
            raise ValueError("Enter the 6-digit code.")
        return v


class ResendIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return v.strip().lower()


# ----------------------------- ROUTES -----------------------------
@router.post("/signup", status_code=201)
def signup(data: SignupIn, background: BackgroundTasks):
    pw_hash = hash_password(data.password)

    with db_session() as db:
        clashes = db.execute(
            "SELECT * FROM admin_users WHERE email = ? OR staff_id = ? OR username = ?",
            (data.email, data.staff_id, data.email),
        ).fetchall()

        for row in clashes:
            # Taken by a verified account, or the Staff ID / username belongs to someone else.
            if row["is_verified"] or row["email"] != data.email:
                raise HTTPException(409, "An account with this email or Staff ID already exists.")

        if clashes:
            # Same email, still unverified: let them correct their details and get a new code.
            db.execute(
                """UPDATE admin_users
                   SET full_name = ?, staff_id = ?, department = ?, password_hash = ?
                   WHERE email = ?""",
                (data.full_name, data.staff_id, data.department, pw_hash, data.email),
            )
        else:
            db.execute(
                """INSERT INTO admin_users
                   (username, password_hash, full_name, staff_id, email, department, is_verified, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (data.email, pw_hash, data.full_name, data.staff_id, data.email,
                 data.department, now().isoformat()),
            )
        code = issue_code(db, data.email)

    background.add_task(send_code_email, data.email, data.full_name, code)
    return {"message": "Account created. Check your staff email for the code.", "email": data.email}


@router.post("/verify")
def verify(data: VerifyIn):
    invalid = HTTPException(400, "That code is invalid or has expired.")

    with db_session() as db:
        row = db.execute("SELECT * FROM admin_users WHERE email = ?", (data.email,)).fetchone()
        if not row or row["is_verified"] or not row["code_hash"]:
            raise invalid
        if row["code_attempts"] >= MAX_CODE_ATTEMPTS:
            raise HTTPException(429, "Too many wrong attempts. Request a new code.")
        if datetime.fromisoformat(row["code_expires_at"]) < now():
            raise invalid

        if not hmac.compare_digest(row["code_hash"], hash_code(data.email, data.code)):
            db.execute(
                "UPDATE admin_users SET code_attempts = code_attempts + 1 WHERE email = ?",
                (data.email,),
            )
            db.commit()  # keep the attempt count even though we raise below
            raise invalid

        db.execute(
            """UPDATE admin_users
               SET is_verified = 1, code_hash = NULL, code_expires_at = NULL, code_attempts = 0
               WHERE email = ?""",
            (data.email,),
        )
    return {"message": "Email verified. You can now log in."}


@router.post("/resend")
def resend(data: ResendIn, background: BackgroundTasks):
    # Same reply whether or not the account exists, so emails can't be probed.
    reply = {"message": "If this account is waiting for verification, a new code has been sent."}

    with db_session() as db:
        row = db.execute("SELECT * FROM admin_users WHERE email = ?", (data.email,)).fetchone()
        if not row or row["is_verified"]:
            return reply
        if row["code_sent_at"]:
            elapsed = (now() - datetime.fromisoformat(row["code_sent_at"])).total_seconds()
            if elapsed < RESEND_COOLDOWN_SECONDS:
                wait = int(RESEND_COOLDOWN_SECONDS - elapsed) + 1
                raise HTTPException(429, f"Wait {wait} seconds before requesting another code.")
        code = issue_code(db, data.email)
        name = row["full_name"]

    background.add_task(send_code_email, data.email, name, code)
    return reply

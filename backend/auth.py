"""
auth.py
Password hashing (bcrypt) and login token (JWT) helpers for the
admin dashboard. Kept separate from main.py so the auth logic is
easy to find and reuse.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

# In a real deployment, load this from an environment variable instead
# of hardcoding it. For now (local dev / FYP), this is fine.
SECRET_KEY = "safescholar-dev-secret-change-this-before-deployment"
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60


def hash_password(plain_password: str) -> str:
    """Hash a plain-text password for storage. Never store plain text."""
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a plain-text password attempt against the stored hash."""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), password_hash.encode("utf-8")
    )


def create_access_token(username: str) -> str:
    """Create a short-lived login token after a successful password check."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """Return the username from a token, or None if it's invalid/expired."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

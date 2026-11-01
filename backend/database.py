"""
database.py
Sets up the SQLite database and the `incidents` table used by the
real-time dashboard. Runs identically on your laptop or the Pi later —
SQLite is just a local file, no server needed.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "safescholar.db"

# Extra admin_users columns for staff self-signup. They're added with ALTER TABLE
# so your existing safescholar.db keeps working. Accounts that already exist
# (like the one from create_admin.py) default to is_verified = 1.
STAFF_COLUMNS = {
    "full_name": "TEXT",
    "staff_id": "TEXT",
    "email": "TEXT",
    "department": "TEXT",
    "is_verified": "INTEGER NOT NULL DEFAULT 1",
    "code_hash": "TEXT",
    "code_expires_at": "TEXT",
    "code_attempts": "INTEGER NOT NULL DEFAULT 0",
    "code_sent_at": "TEXT",
    "created_at": "TEXT",
}

# Extra incidents columns for CVSS-style scoring (added with ALTER TABLE, so old
# rows keep working; they just have NULL score/breakdown). `breakdown` holds the
# JSON explanation of how the score was calculated.
INCIDENT_COLUMNS = {
    "score": "REAL",        # 0.0 to 10.0
    "action": "TEXT",       # 'block', 'warn' or 'allow'
    "breakdown": "TEXT",    # JSON: matched categories, steps, evidence
}


def get_connection():
    """Open a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    return conn


def _add_missing_columns(conn, table, columns):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    """Create the incidents and admin_users tables if they don't already exist, and upgrade older ones."""
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,       -- ISO 8601 string
            session_id TEXT NOT NULL,      -- which browser session flagged this
            severity TEXT NOT NULL,        -- e.g. 'low', 'medium', 'high', 'critical'
            category TEXT NOT NULL,        -- e.g. 'jailbreak', 'prompt_injection'
            device_label TEXT,             -- optional device/browser identifier
            message TEXT                   -- short description of the incident
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL    -- bcrypt hash, never plain text
        )
        """
    )

    
    # Store evidence metadata linked to detected incidents.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            filename TEXT NOT NULL UNIQUE,
            original_filename TEXT,
            mime_type TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY (incident_id)
                REFERENCES incidents(id)
                ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_evidence_incident
        ON incident_evidence(incident_id)
        """
    )

    # Upgrade older databases: add any missing staff-signup / scoring columns.
    _add_missing_columns(conn, "admin_users", STAFF_COLUMNS)
    _add_missing_columns(conn, "incidents", INCIDENT_COLUMNS)

    # One account per staff email / staff ID (NULLs are allowed for old admin rows).
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_users_email ON admin_users(email)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_users_staff_id ON admin_users(staff_id)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS appeals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id INTEGER NOT NULL UNIQUE REFERENCES incidents(id),
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
        submitted_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewed_by TEXT
    )""")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")

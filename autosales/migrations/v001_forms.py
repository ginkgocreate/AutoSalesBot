"""Version 1: add form records, shared delivery history, and reply company links."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    # IF NOT EXISTS also adopts databases created by the unversioned form prototype.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forms (
            id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id),
            page_url TEXT NOT NULL,
            mode TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            flags TEXT NOT NULL DEFAULT '',
            signature TEXT NOT NULL DEFAULT '',
            plan TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            demoted INTEGER NOT NULL DEFAULT 0,
            found_at TEXT NOT NULL,
            decided_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS form_sends (
            id INTEGER PRIMARY KEY,
            form_id INTEGER NOT NULL REFERENCES forms(id),
            company_id INTEGER NOT NULL REFERENCES companies(id),
            template TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_form_sends_company ON form_sends(company_id)")
    if "company_id" not in {row[1] for row in conn.execute("PRAGMA table_info(replies)")}:
        conn.execute("ALTER TABLE replies ADD COLUMN company_id INTEGER REFERENCES companies(id)")
    if "demoted" not in {row[1] for row in conn.execute("PRAGMA table_info(forms)")}:
        conn.execute("ALTER TABLE forms ADD COLUMN demoted INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE replies SET company_id=(SELECT company_id FROM contacts WHERE id=replies.contact_id) "
        "WHERE company_id IS NULL AND contact_id IS NOT NULL"
    )


def validate(conn: sqlite3.Connection) -> None:
    required = {
        "forms": {
            "id",
            "company_id",
            "page_url",
            "mode",
            "kind",
            "reason",
            "flags",
            "signature",
            "plan",
            "status",
            "demoted",
            "found_at",
            "decided_at",
        },
        "form_sends": {"id", "form_id", "company_id", "template", "status", "detail", "sent_at"},
        "replies": {
            "id",
            "contact_id",
            "message_id",
            "from_addr",
            "kind",
            "choice",
            "snippet",
            "received_at",
            "company_id",
        },
    }
    for table, expected in required.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not expected.issubset(actual):
            raise sqlite3.DatabaseError(f"DBスキーマを確認できません: {table}")


def downgrade(conn: sqlite3.Connection) -> None:
    # The runner rejects every nonempty database before invoking this operation.
    # Recreating the empty replies table supports SQLite versions without DROP COLUMN.
    conn.execute("DROP TABLE form_sends")
    conn.execute("DROP TABLE forms")
    conn.execute("DROP TABLE replies")
    conn.execute("""
        CREATE TABLE replies (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER REFERENCES contacts(id),
            message_id TEXT NOT NULL UNIQUE,
            from_addr TEXT NOT NULL,
            kind TEXT NOT NULL,
            choice INTEGER,
            snippet TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL
        )
    """)

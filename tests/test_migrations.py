"""Exercise migrations against the committed pre-form schema and temporary databases."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosales import db
from autosales.migrations import MigrationError, downgrade, upgrade


# Snapshot of the schema in main@fb4b35d, independent of the current schema.
LEGACY_SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, site_url TEXT NOT NULL,
    host TEXT NOT NULL UNIQUE, area TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '', store_count INTEGER,
    segment TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new',
    status_note TEXT NOT NULL DEFAULT '', refusal_confirmed TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, crawled_at TEXT
);
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
    email TEXT NOT NULL UNIQUE, source_url TEXT NOT NULL, flags TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', found_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE crawl_log (
    id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
    url TEXT NOT NULL, result TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL
);
CREATE TABLE sends (
    id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id),
    template TEXT NOT NULL, subject TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL
);
CREATE TABLE replies (
    id INTEGER PRIMARY KEY, contact_id INTEGER REFERENCES contacts(id),
    message_id TEXT NOT NULL UNIQUE, from_addr TEXT NOT NULL, kind TEXT NOT NULL,
    choice INTEGER, snippet TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL
);
CREATE TABLE suppressions (
    id INTEGER PRIMARY KEY, target TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE site_emails (
    company_id INTEGER NOT NULL REFERENCES companies(id), email TEXT NOT NULL,
    PRIMARY KEY (company_id, email)
);
CREATE INDEX idx_site_emails_email ON site_emails(email);
CREATE INDEX idx_contacts_company ON contacts(company_id);
CREATE INDEX idx_sends_contact ON sends(contact_id);
"""


class Migrations(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fixture.db"

    def legacy(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.executescript(LEGACY_SCHEMA)
        self.addCleanup(conn.close)
        return conn

    def connect(self) -> sqlite3.Connection:
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def populate(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO companies(id,name,site_url,host,status,created_at) "
            "VALUES (1,'Fixture','https://fixture.test','fixture.test','crawled','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO contacts(id,company_id,email,source_url,status,found_at) "
            "VALUES (1,1,'info@fixture.test','https://fixture.test','approved','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO sends(contact_id,template,subject,status,sent_at) "
            "VALUES (1,'fixture','fixture','unknown','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO replies(contact_id,message_id,from_addr,kind,choice,snippet,received_at) "
            "VALUES (1,'fixture-reply','info@fixture.test','reply',2,'retained','2026-01-02')"
        )
        conn.execute("INSERT INTO suppressions(target,reason,created_at) VALUES ('fixture.test','optout','2026-01-02')")
        conn.execute("INSERT INTO site_emails(company_id,email) VALUES (1,'info@fixture.test')")
        conn.execute(
            "INSERT INTO crawl_log(company_id,url,result,fetched_at) "
            "VALUES (1,'https://fixture.test','ok','2026-01-01')"
        )
        conn.commit()

    def test_legacy_upgrade_retains_history_and_backfills_company(self) -> None:
        old = self.legacy()
        self.populate(old)
        before = {
            name: old.execute(f'SELECT * FROM "{name}"').fetchall()
            for name in ("companies", "contacts", "sends", "suppressions", "site_emails", "crawl_log")
        }
        old.close()
        conn = self.connect()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        for name, rows in before.items():
            self.assertEqual([tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')], rows)
        reply = conn.execute("SELECT * FROM replies").fetchone()
        self.assertEqual((reply["company_id"], reply["choice"], reply["snippet"]), (1, 2, "retained"))
        self.assertTrue(db.company_contacted(conn, 1))
        self.assertEqual(db.sendable_contacts(conn), [])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_new_database_and_repeated_connect_keep_one_version(self) -> None:
        self.connect().close()
        conn = self.connect()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertIn("demoted", {row[1] for row in conn.execute("PRAGMA table_info(forms)")})
        self.assertIn("company_id", {row[1] for row in conn.execute("PRAGMA table_info(replies)")})

    def test_unversioned_form_database_is_adopted_without_losing_state(self) -> None:
        conn = self.connect()
        self.populate(conn)
        conn.execute(
            "INSERT INTO forms(id,company_id,page_url,mode,demoted,found_at) "
            "VALUES (1,1,'https://fixture.test/contact','manual',1,'2026-01-02')"
        )
        conn.execute(
            "INSERT INTO form_sends(form_id,company_id,template,status,sent_at) "
            "VALUES (1,1,'fixture','unknown','2026-01-02')"
        )
        conn.execute(
            "INSERT INTO replies(message_id,from_addr,kind,received_at,company_id) "
            "VALUES ('form-reply','form@fixture.test','optout','2026-01-02',1)"
        )
        conn.execute("PRAGMA user_version=0")
        conn.commit()
        conn.close()
        current = self.connect()
        self.assertEqual(current.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(current.execute("SELECT demoted FROM forms").fetchone()[0], 1)
        self.assertEqual(current.execute("SELECT status FROM form_sends").fetchone()[0], "unknown")
        self.assertEqual(
            current.execute("SELECT company_id FROM replies WHERE message_id='form-reply'").fetchone()[0], 1
        )

    def test_empty_database_downgrade_upgrade_round_trip(self) -> None:
        conn = self.connect()
        downgrade(conn)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertNotIn("company_id", {row[1] for row in conn.execute("PRAGMA table_info(replies)")})
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='forms'").fetchone())
        upgrade(conn, db.SCHEMA)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_nonempty_downgrade_is_rejected_without_changing_data(self) -> None:
        conn = self.connect()
        self.populate(conn)
        before = list(conn.iterdump())
        with self.assertRaisesRegex(MigrationError, "空|empty"):
            downgrade(conn)
        self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_newer_schema_is_rejected_before_any_schema_changes(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE future_data (value TEXT)")
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        before = list(conn.iterdump())
        conn.close()
        with self.assertRaises(MigrationError):
            db.connect(self.path)
        check = sqlite3.connect(self.path)
        self.addCleanup(check.close)
        self.assertEqual(list(check.iterdump()), before)
        self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_failed_upgrade_rolls_back_schema_and_version(self) -> None:
        conn = self.legacy()
        conn.execute("CREATE TABLE forms (id INTEGER PRIMARY KEY, unexpected TEXT)")
        conn.commit()
        before = list(conn.iterdump())
        with self.assertRaises((MigrationError, sqlite3.DatabaseError)):
            upgrade(conn, db.SCHEMA)
        self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_migration_does_not_commit_an_existing_transaction(self) -> None:
        conn = self.connect()
        conn.execute("INSERT INTO suppressions(target,created_at) VALUES ('pending.test','2026-01-01')")
        with self.assertRaises(MigrationError):
            upgrade(conn, db.SCHEMA)
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

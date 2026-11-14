"""SQLite の入れ物。送ってよい相手かどうかの判定もここに一本化する。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    site_url    TEXT NOT NULL,
    host        TEXT NOT NULL UNIQUE,
    area        TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    store_count INTEGER,
    segment     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    -- new / crawled / no_email / refused / blocked / error / skipped
    status      TEXT NOT NULL DEFAULT 'new',
    status_note TEXT NOT NULL DEFAULT '',
    -- 1 = 「営業お断り」の検知を人が見て、誤検知だと判断した
    refusal_checked INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    crawled_at  TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    email       TEXT NOT NULL UNIQUE,
    source_url  TEXT NOT NULL,
    flags       TEXT NOT NULL DEFAULT '',
    -- pending / approved / rejected / bounced
    status      TEXT NOT NULL DEFAULT 'pending',
    found_at    TEXT NOT NULL,
    decided_at  TEXT
);
CREATE TABLE IF NOT EXISTS crawl_log (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    url         TEXT NOT NULL,
    result      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sends (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id),
    template    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    message_id  TEXT NOT NULL DEFAULT '',
    -- dry_run / sent / failed
    status      TEXT NOT NULL,
    error       TEXT NOT NULL DEFAULT '',
    sent_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replies (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER REFERENCES contacts(id),
    message_id  TEXT NOT NULL UNIQUE,
    from_addr   TEXT NOT NULL,
    -- reply / optout / bounce
    kind        TEXT NOT NULL,
    choice      INTEGER,
    snippet     TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppressions (
    id          INTEGER PRIMARY KEY,
    -- メールアドレス、または "@" なしのドメイン
    target      TEXT NOT NULL UNIQUE,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_sends_contact ON sends(contact_id);
"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def normalize_email(email: str) -> str:
    return email.strip().lower()


def email_domain(email: str) -> str:
    return normalize_email(email).rpartition("@")[2]


def add_suppression(conn: sqlite3.Connection, target: str, reason: str = "") -> bool:
    """停止リストに入れる。アドレスかドメイン（@なし）を受け付ける。新規なら True。"""
    target = target.strip().lower().lstrip("@")
    if not target:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO suppressions(target, reason, created_at) VALUES (?,?,?)",
        (target, reason, now()),
    )
    conn.commit()
    return cur.rowcount > 0


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    email = normalize_email(email)
    domain = email_domain(email)
    row = conn.execute(
        "SELECT 1 FROM suppressions WHERE target IN (?, ?) LIMIT 1", (email, domain)
    ).fetchone()
    return row is not None


def sent_today(conn: sqlite3.Connection) -> int:
    today = datetime.now().astimezone().date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM sends WHERE status='sent' AND substr(sent_at,1,10)=?", (today,)
    ).fetchone()
    return int(row[0])


def sendable_contacts(conn: sqlite3.Connection, segment: str | None = None) -> list[sqlite3.Row]:
    """いま送ってよい相手だけを返す。送信側はこの関数以外から宛先を取らないこと。

    条件:
      - 人が承認した連絡先（status='approved'）
      - 会社が「営業お断り」などで除外されていない（status='crawled'）
      - 停止リストに無い（アドレス・ドメインの両方）
      - その会社にまだ1通も送っていない（1社1通、追い送りなし）
    1社に承認済みが複数あるときは、注意フラグの少ないものを1件だけ選ぶ。
    """
    sql = """
        SELECT ct.id AS contact_id, ct.email, ct.source_url, ct.flags,
               co.id AS company_id, co.name, co.segment, co.area, co.category
        FROM contacts ct JOIN companies co ON co.id = ct.company_id
        WHERE ct.status = 'approved'
          AND co.status = 'crawled'
          AND NOT EXISTS (
              SELECT 1 FROM sends s JOIN contacts c2 ON c2.id = s.contact_id
              WHERE c2.company_id = co.id AND s.status = 'sent')
    """
    params: list[str] = []
    if segment:
        sql += " AND co.segment = ?"
        params.append(segment)
    sql += " ORDER BY co.id, (ct.flags != ''), ct.id"
    rows = conn.execute(sql, params).fetchall()
    picked: list[sqlite3.Row] = []
    seen_companies: set[int] = set()
    for row in rows:
        if row["company_id"] in seen_companies:
            continue
        if is_suppressed(conn, row["email"]):
            continue
        seen_companies.add(row["company_id"])
        picked.append(row)
    return picked

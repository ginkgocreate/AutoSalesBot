"""SQLite の入れ物。送ってよい相手かどうかの判定もここに一本化する。"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

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
    -- 人が見て「営業お断りではない」と判断した検知文（改行区切り）
    refusal_confirmed TEXT NOT NULL DEFAULT '',
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
    -- dry_run / sent / failed / unknown（届いたかもしれない。送った扱いにする）
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
-- 直近の巡回で、その会社のサイトに載っていたアドレス（連絡先にしなかったものも含む）
CREATE TABLE IF NOT EXISTS site_emails (
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    email       TEXT NOT NULL,
    PRIMARY KEY (company_id, email)
);
CREATE INDEX IF NOT EXISTS idx_site_emails_email ON site_emails(email);
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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(companies)")}
    if "refusal_confirmed" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN refusal_confirmed TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn


# 「送った」とみなす状態。unknown は、サーバーの返事が来る前に回線が切れたもの（届いた可能性がある）
CONTACTED = ("sent", "unknown")
_EMAIL_RE = re.compile(r"[a-z0-9][a-z0-9._%+\-]*@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+")
_DOMAIN_RE = re.compile(r"[a-z0-9\-]+(?:\.[a-z0-9\-]+)+")
# 会社ごと止める理由ではないもの（そのアドレスだけを止める）
_ADDRESS_ONLY_REASONS = ("不達", "refused_site:")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def email_domain(email: str) -> str:
    return normalize_email(email).rpartition("@")[2]


def normalize_target(target: str) -> str | None:
    """停止リストに入れる形に直す。アドレスかドメインとして読めなければ None。

    URL（https://www.example.jp/contact）や www 付き、全角でも受け付ける。
    """
    text = unicodedata.normalize("NFKC", target or "").strip().lower()
    m = _EMAIL_RE.search(text)
    if m:
        return m.group(0)
    if "://" in text or "/" in text:
        try:
            text = urlsplit(text if "://" in text else "//" + text).hostname or ""
        except ValueError:
            return None
    text = text.lstrip("@").rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text if _DOMAIN_RE.fullmatch(text) else None


def add_suppression(conn: sqlite3.Connection, target: str, reason: str = "") -> bool:
    """停止リストに入れる。新規なら True、すでにあれば False。読めない入力は ValueError。"""
    normalized = normalize_target(target)
    if normalized is None:
        raise ValueError(f"アドレスかドメインとして読めません: {target!r}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO suppressions(target, reason, created_at) VALUES (?,?,?)",
        (normalized, reason, now()),
    )
    conn.commit()
    return cur.rowcount > 0


def _candidates(email: str) -> list[str]:
    """アドレス、そのドメイン、親ドメイン（mail.a.co.jp → a.co.jp → co.jp）。"""
    email = normalize_email(email)
    labels = email_domain(email).split(".")
    return [email] + [".".join(labels[i:]) for i in range(len(labels) - 1)]


def suppression_reason(conn: sqlite3.Connection, email: str) -> str | None:
    """送ってはいけないアドレスなら、その理由を返す。問題なければ None。

    停止リスト（アドレス・ドメイン・親ドメイン）と、「営業お断り」のサイトに載っていたアドレスを見る。
    複数当たるときは、会社ごと止める理由（本人・手動の停止）を優先して返す。
    """
    cands = _candidates(email)
    marks = ",".join("?" * len(cands))
    row = conn.execute(
        f"SELECT reason FROM suppressions WHERE target IN ({marks}) "
        "ORDER BY (reason LIKE '不達%' OR reason LIKE 'refused_site:%'), id LIMIT 1", cands
    ).fetchone()
    if row is not None:
        return row[0]
    row = conn.execute(
        "SELECT se.company_id FROM site_emails se JOIN companies co ON co.id = se.company_id "
        "WHERE se.email = ? AND co.status = 'refused' LIMIT 1", (cands[0],)
    ).fetchone()
    return None if row is None else f"refused_site:{row[0]}"


def _is_company_level(reason: str | None) -> bool:
    return reason is not None and not reason.startswith(_ADDRESS_ONLY_REASONS)


def blocked_companies(conn: sqlite3.Connection) -> set[int]:
    """停止の連絡（本人から・手動）を受けた会社。どのアドレスにも送らない。

    連絡先だけでなく、サイトに載っていた全アドレスと、会社のサイトのドメインで照らす。
    """
    blocked: set[int] = set()
    rows = conn.execute(
        "SELECT company_id, email FROM contacts UNION SELECT company_id, email FROM site_emails "
        "UNION SELECT id, 'x@' || host FROM companies"
    ).fetchall()
    for r in rows:
        if r[0] not in blocked and _is_company_level(suppression_reason(conn, r[1])):
            blocked.add(r[0])
    # 担当者個人のアドレスで停止を受けた場合も、その人の会社（同じドメインのサイト）ごと止める
    stop_domains = {
        email_domain(t[0]) for t in conn.execute("SELECT target, reason FROM suppressions")
        if "@" in t[0] and _is_company_level(t[1])
    }
    if stop_domains:
        for cid, host in conn.execute("SELECT id, host FROM companies"):
            if any(d == host or d.endswith("." + host) or host.endswith("." + d) for d in stop_domains):
                blocked.add(cid)
    return blocked


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    return suppression_reason(conn, email) is not None


def company_contacted(conn: sqlite3.Connection, company_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sends s JOIN contacts c ON c.id = s.contact_id "
        "WHERE c.company_id = ? AND s.status IN ('sent','unknown') LIMIT 1", (company_id,)
    ).fetchone()
    return row is not None


def sent_today(conn: sqlite3.Connection) -> int:
    today = datetime.now().astimezone().date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM sends WHERE status IN ('sent','unknown') AND substr(sent_at,1,10)=?", (today,)
    ).fetchone()
    return int(row[0])


def sendable_contacts(conn: sqlite3.Connection, segment: str | None = None) -> list[sqlite3.Row]:
    """いま送ってよい相手だけを返す。送信側はこの関数以外から宛先を取らないこと。

    条件:
      - 人が承認した連絡先（status='approved'）
      - 会社が「営業お断り」などで除外されていない（status='crawled'）
      - 停止リストに無い（アドレス・ドメイン・親ドメイン）
      - その会社の誰かから停止の連絡を受けていない（別のアドレスに繰り上げて送らない）
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
              WHERE c2.company_id = co.id AND s.status IN ('sent','unknown'))
    """
    params: list[str] = []
    if segment is not None:
        sql += " AND co.segment = ?"
        params.append(segment)
    sql += " ORDER BY co.id, (ct.flags != ''), ct.id"
    rows = conn.execute(sql, params).fetchall()
    blocked = blocked_companies(conn)
    picked: list[sqlite3.Row] = []
    seen_companies: set[int] = set()
    for row in rows:
        if row["company_id"] in seen_companies or row["company_id"] in blocked:
            continue
        if is_suppressed(conn, row["email"]):
            continue
        seen_companies.add(row["company_id"])
        picked.append(row)
    return picked

"""返信の取り込み（IMAP）。「不要」は停止リストへ、番号（1〜4）は集計へ、不達は除外へ。

受信箱の未読・既読は変えない（BODY.PEEK で読む）。同じメールは Message-ID で二重に数えない。
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Callable

from . import db
from .config import Config
from .extract import FREE_MAIL_DOMAINS

OPTOUT_RE = re.compile(
    r"(不要|配信停止|停止(して|を|希望|願)|送らないで|送信しないで|お断り|結構です|要りません|いりません|"
    r"unsubscribe|remove me|stop)",
    re.I,
)
BOUNCE_FROM_RE = re.compile(r"(mailer-daemon|postmaster|mail delivery)", re.I)
# 引用が始まる行（ここから下は相手が書いた文ではない）
QUOTE_START_RE = re.compile(
    r"^(>|＞|-{2,}\s*Original Message|-{3,}|_{5,}|─{5,}|"
    r"On .{5,80} wrote:|\d{4}[年/].{3,60}(wrote|書きました|のメッセージ)[:：]?|"
    r"差出人\s*[:：]|From\s*[:：]|送信元\s*[:：])",
    re.I,
)
CHOICE_RE = re.compile(r"(?<![0-9])([1-4])(?![0-9])")


@dataclass
class Classified:
    kind: str  # reply / optout / bounce
    choice: int | None
    snippet: str


def _text_of(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
    return unicodedata.normalize("NFKC", text)


def own_words(text: str) -> str:
    """引用より上の、相手が自分で書いた部分だけを返す。"""
    lines: list[str] = []
    for line in text.splitlines():
        if QUOTE_START_RE.match(line.strip()):
            break
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def classify_reply(text: str) -> Classified:
    body = own_words(unicodedata.normalize("NFKC", text))
    snippet = " ".join(body.split())[:200]
    head = "\n".join([ln for ln in body.splitlines() if ln.strip()][:5])
    choice = None
    m = CHOICE_RE.search(head)
    # 数字だけ、または行の先頭の数字を番号返信とみなす（「3店舗あります」などは数えない）
    for line in head.splitlines():
        lm = re.match(r"^\s*[(（]?([1-4])[)）.．、,:：\s]*(番|です|でお願いします)?\s*$", line)
        lm = lm or re.match(r"^\s*([1-4])\s*(番|[.．、:：)）])", line)
        if lm:
            choice = int(lm.group(1))
            break
    if choice is None and m and len(head.strip()) <= 12:
        choice = int(m.group(1))
    if OPTOUT_RE.search(head):
        return Classified("optout", choice, snippet)
    return Classified("reply", choice, snippet)


def _known_emails(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["email"]: row["id"] for row in conn.execute("SELECT id, email FROM contacts")}


def _contacts_by_domain(conn: sqlite3.Connection) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for row in conn.execute(
        "SELECT ct.id, ct.email FROM contacts ct "
        "WHERE EXISTS (SELECT 1 FROM sends s WHERE s.contact_id = ct.id AND s.status='sent')"
    ):
        out.setdefault(db.email_domain(row["email"]), []).append(row["id"])
    return out


def process_message(conn: sqlite3.Connection, raw: bytes, own_addresses: set[str]) -> str | None:
    """1通を処理して、記録した種類（reply/optout/bounce）を返す。関係ないメールは None。"""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = (msg.get("Message-ID") or "").strip()
    from_addr = db.normalize_email(parseaddr(str(msg.get("From", "")))[1])
    if not message_id or not from_addr or from_addr in own_addresses:
        return None
    if conn.execute("SELECT 1 FROM replies WHERE message_id=?", (message_id,)).fetchone():
        return None

    known = _known_emails(conn)
    text = _text_of(msg)
    received = db.now()
    is_report = msg.get_content_type() == "multipart/report"

    if is_report or BOUNCE_FROM_RE.search(str(msg.get("From", ""))):
        # 不達通知。本文と添付の中から、こちらが送った宛先を探す
        haystack = unicodedata.normalize("NFKC", raw.decode("utf-8", "replace")).lower() + "\n" + text.lower()
        hit = [e for e in known if e in haystack and e not in own_addresses]
        if not hit:
            return None
        for addr in hit:
            conn.execute("UPDATE contacts SET status='bounced', decided_at=? WHERE id=?", (received, known[addr]))
            db.add_suppression(conn, addr, "不達")
        conn.execute(
            "INSERT INTO replies(contact_id, message_id, from_addr, kind, snippet, received_at)"
            " VALUES (?,?,?,?,?,?)",
            (known[hit[0]], message_id, from_addr, "bounce", ", ".join(hit)[:200], received),
        )
        conn.commit()
        return "bounce"

    contact_id = known.get(from_addr)
    if contact_id is None:
        # 別の担当者のアドレスから返ってくることがある。送った先と同じドメインなら同じ会社とみなす
        domain_hits = _contacts_by_domain(conn).get(db.email_domain(from_addr), [])
        if len(domain_hits) != 1 or db.email_domain(from_addr) in FREE_MAIL_DOMAINS:
            return None
        contact_id = domain_hits[0]
    else:
        sent = conn.execute(
            "SELECT 1 FROM sends WHERE contact_id=? AND status='sent'", (contact_id,)
        ).fetchone()
        if not sent:
            return None  # こちらから送っていない相手のメールは営業の返信ではない

    result = classify_reply(text)
    subject = unicodedata.normalize("NFKC", str(msg.get("Subject", "")))
    if result.kind != "optout" and re.search(r"unsubscribe|配信停止|不要", subject, re.I) \
            and not re.match(r"^\s*(re|返信)\s*[:：]", subject, re.I):
        result.kind = "optout"
    if result.kind == "optout":
        db.add_suppression(conn, from_addr, "本人から停止の連絡")
        row = conn.execute("SELECT email FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if row:
            db.add_suppression(conn, row["email"], "本人から停止の連絡")
    conn.execute(
        "INSERT INTO replies(contact_id, message_id, from_addr, kind, choice, snippet, received_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (contact_id, message_id, from_addr, result.kind, result.choice, result.snippet, received),
    )
    conn.commit()
    return result.kind


def fetch_replies(
    conn: sqlite3.Connection,
    cfg: Config,
    days: int = 14,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    if not cfg.get("IMAP_HOST"):
        raise SystemExit("IMAP_HOST が未設定です（.env）")
    own = {a.lower() for a in (cfg.get("SENDER_EMAIL"), cfg.get("REPLY_TO"), cfg.optout_email,
                               cfg.get("IMAP_USER")) if a and "@" in a}
    counts = {"reply": 0, "optout": 0, "bounce": 0, "checked": 0}
    imap = imaplib.IMAP4_SSL(cfg.get("IMAP_HOST"), cfg.get_int("IMAP_PORT"))
    try:
        imap.login(cfg.get("IMAP_USER"), cfg.get("IMAP_PASS"))
        imap.select(cfg.get("IMAP_FOLDER"), readonly=True)
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = imap.search(None, "SINCE", since)
        if typ != "OK":
            raise SystemExit("受信箱の検索に失敗しました")
        for num in data[0].split():
            typ, parts = imap.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            counts["checked"] += 1
            kind = process_message(conn, parts[0][1], own)
            if kind:
                counts[kind] += 1
                log(f"[{kind}] 1件を記録しました")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return counts


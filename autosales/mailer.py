"""文面の組み立てと送信。

宛先は db.sendable_contacts() が返したものだけ。署名（送信者名・住所・停止の連絡先）は
ここで必ず付けるので、ひな形から消すことはできない（特定電子メール法の表示義務）。
"""
from __future__ import annotations

import re
import smtplib
import sqlite3
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Callable

from . import db
from .config import Config

PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
RULE = "─" * 20


class TemplateError(Exception):
    pass


@dataclass
class Template:
    name: str
    subject: str
    body: str


def load_template(path: Path) -> Template:
    if not path.is_file():
        raise TemplateError(f"ひな形が見つかりません: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not re.match(r"^(件名|Subject)\s*[:：]", lines[0]):
        raise TemplateError(f"{path.name}: 1行目は「件名: ○○」にしてください")
    subject = re.split(r"[:：]", lines[0], maxsplit=1)[1].strip()
    body = "\n".join(lines[1:]).strip("\n")
    if not subject or not body.strip():
        raise TemplateError(f"{path.name}: 件名か本文が空です")
    return Template(name=path.stem, subject=subject, body=body)


def footer(cfg: Config) -> str:
    contact = []
    if cfg.get("SENDER_TEL"):
        contact.append(f"電話 {cfg.get('SENDER_TEL')}")
    contact.append(f"メール {cfg.get('SENDER_EMAIL')}")
    optout = cfg.optout_email
    reply_target = cfg.get("REPLY_TO") or cfg.get("SENDER_EMAIL")
    how = (
        "このメールに「不要」とご返信ください。"
        if optout.lower() == reply_target.lower()
        else f"{optout} 宛に「不要」とお送りください。"
    )
    return "\n".join([
        RULE,
        f"{cfg.get('SENDER_COMPANY')}　{cfg.get('SENDER_PERSON')}",
        cfg.get("SENDER_ADDRESS"),
        "／".join(contact),
        f"今後のご案内が不要な場合は、{how}以後お送りしません。",
    ])


def render(template: Template, cfg: Config, contact: sqlite3.Row | dict) -> tuple[str, str]:
    """(件名, 本文) を返す。埋められない差し込みが残っていたら止める。"""
    values = {
        "会社名": contact["name"],
        "エリア": contact["area"],
        "業態": contact["category"],
        "掲載URL": contact["source_url"],
        "送信者会社名": cfg.get("SENDER_COMPANY"),
        "担当者名": cfg.get("SENDER_PERSON"),
        "提供開始時期": cfg.get("SERVICE_START"),
        "デモURL": cfg.get("DEMO_URL"),
    }

    def fill(text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            key = m.group(1)
            value = (values.get(key) or "").strip()
            if not value:
                raise TemplateError(f"差し込み {{{{{key}}}}} に入れる値がありません")
            return value
        return PLACEHOLDER_RE.sub(sub, text)

    subject = " ".join(fill(template.subject).split())
    body = fill(template.body).rstrip() + "\n\n" + footer(cfg) + "\n"
    return subject, body


def build_message(cfg: Config, to_addr: str, subject: str, body: str) -> EmailMessage:
    sender = cfg.get("SENDER_EMAIL")
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("SENDER_COMPANY"), sender))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    if cfg.get("REPLY_TO"):
        msg["Reply-To"] = cfg.get("REPLY_TO")
    msg["List-Unsubscribe"] = f"<mailto:{cfg.optout_email}?subject=unsubscribe>"
    msg.set_content(body, subtype="plain", charset="utf-8", cte="base64")
    return msg


def _safe_name(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9@._\-]", "_", email)


def _connect_smtp(cfg: Config) -> smtplib.SMTP:
    host, port = cfg.get("SMTP_HOST"), cfg.get_int("SMTP_PORT")
    security = cfg.get("SMTP_SECURITY").lower()
    if security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
        if security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
    if cfg.get("SMTP_USER"):
        smtp.login(cfg.get("SMTP_USER"), cfg.get("SMTP_PASS"))
    return smtp


@dataclass
class SendSummary:
    planned: int = 0
    sent: int = 0
    failed: int = 0
    dry_run: int = 0
    skipped_limit: int = 0


def send_batch(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    *,
    live: bool = False,
    segment: str | None = None,
    limit: int | None = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> SendSummary:
    """送ってよい相手に1社1通ずつ送る。live=False なら .eml を outbox/ に書くだけ。"""
    missing = cfg.missing_sender_fields()
    if missing:
        raise TemplateError("送信者の表示が未設定です（.env）: " + ", ".join(missing))
    if live and not cfg.get("SMTP_HOST"):
        raise TemplateError("SMTP_HOST が未設定です（.env）")

    summary = SendSummary()
    targets = db.sendable_contacts(conn, segment=segment)
    summary.planned = len(targets)

    room = None
    if live:
        room = max(0, cfg.get_int("DAILY_LIMIT") - db.sent_today(conn))
    cap = min(x for x in (room, limit, len(targets)) if x is not None)
    summary.skipped_limit = len(targets) - cap
    targets = targets[:cap]
    if not targets:
        return summary

    # 先に全員分を組み立てる。差し込みの不備があれば1通も送らずに止まる
    prepared = []
    for row in targets:
        subject, body = render(template, cfg, row)
        prepared.append((row, subject, body))

    if not live:
        outdir = cfg.root / "outbox" / db.now()[:10]
        outdir.mkdir(parents=True, exist_ok=True)
        for row, subject, body in prepared:
            msg = build_message(cfg, row["email"], subject, body)
            path = outdir / f"{row['contact_id']:05d}_{_safe_name(row['email'])}.eml"
            path.write_bytes(bytes(msg))
            conn.execute(
                "INSERT INTO sends(contact_id, template, subject, message_id, status, sent_at)"
                " VALUES (?,?,?,?, 'dry_run', ?)",
                (row["contact_id"], template.name, subject, msg["Message-ID"], db.now()),
            )
            summary.dry_run += 1
            log(f"[下書き] {row['name']} <{row['email']}> → {path.relative_to(cfg.root)}")
        conn.commit()
        return summary

    interval = max(0.0, cfg.get_float("SEND_INTERVAL_SEC"))
    smtp = _connect_smtp(cfg)
    try:
        for i, (row, subject, body) in enumerate(prepared):
            # 送る直前にもう一度確かめる（待っている間に停止の連絡が入ることがある）
            if db.is_suppressed(conn, row["email"]):
                log(f"[停止済み] {row['name']} <{row['email']}>")
                continue
            if i:
                sleep(interval)
            msg = build_message(cfg, row["email"], subject, body)
            status, error = "sent", ""
            try:
                refused = smtp.send_message(msg, from_addr=cfg.get("SENDER_EMAIL"), to_addrs=[row["email"]])
                if refused:
                    status, error = "failed", str(refused)
            except smtplib.SMTPRecipientsRefused as e:
                status, error = "failed", str(e.recipients)
                codes = [code for code, _ in e.recipients.values()]
                if codes and all(500 <= code < 600 for code in codes):
                    # 宛先が存在しないなどの恒久的な拒否。次回以降は送らない
                    conn.execute("UPDATE contacts SET status='bounced', decided_at=? WHERE id=?",
                                 (db.now(), row["contact_id"]))
            except smtplib.SMTPServerDisconnected as e:
                status, error = "failed", f"接続が切れました: {e}"
            except smtplib.SMTPException as e:
                status, error = "failed", f"{type(e).__name__}: {e}"
            conn.execute(
                "INSERT INTO sends(contact_id, template, subject, message_id, status, error, sent_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (row["contact_id"], template.name, subject, msg["Message-ID"], status, error, db.now()),
            )
            conn.commit()
            if status == "sent":
                summary.sent += 1
                log(f"[送信] {row['name']} <{row['email']}>")
            else:
                summary.failed += 1
                log(f"[失敗] {row['name']} <{row['email']}> {error}")
                if "接続が切れました" in error:
                    break
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return summary

"""文面の組み立てと送信。

宛先は db.sendable_contacts() が返したものだけ。署名（送信者名・住所・停止の連絡先）は
ここで必ず付けるので、ひな形から消すことはできない（特定電子メール法の表示義務）。
"""

from __future__ import annotations

import contextlib
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


def footer(cfg: Config, via_form: bool = False) -> str:
    contact = []
    if cfg.get("SENDER_TEL"):
        contact.append(f"電話 {cfg.get('SENDER_TEL')}")
    contact.append(f"メール {cfg.get('SENDER_EMAIL')}")
    optout = cfg.optout_email
    reply_target = cfg.get("REPLY_TO") or cfg.get("SENDER_EMAIL")
    # フォームから送るときは「このメールに返信」が通じないので、必ず宛先を書く
    how = (
        "このメールに「不要」とご返信ください。"
        if optout.lower() == reply_target.lower() and not via_form
        else f"{optout} 宛に「不要」とお送りください。"
    )
    return "\n".join(
        [
            RULE,
            f"{cfg.get('SENDER_COMPANY')}　{cfg.get('SENDER_PERSON')}",
            cfg.get("SENDER_ADDRESS"),
            "／".join(contact),
            f"今後のご案内が不要な場合は、{how}以後お送りしません。",
        ]
    )


def render(template: Template, cfg: Config, contact: sqlite3.Row | dict, via_form: bool = False) -> tuple[str, str]:
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
    body = fill(template.body).rstrip() + "\n\n" + footer(cfg, via_form) + "\n"
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
    unknown: int = 0
    stopped: str = ""  # 途中で止めた理由
    skipped: int = 0


# 宛先そのものが存在しないときだけ「不達」にする。中継拒否や認証の失敗は設定の問題なので連絡先は触らない
_NO_SUCH_USER_RE = re.compile(
    r"5\.1\.(?:1|2|3|6|10)\b|user unknown|unknown user|no such user|does not exist|doesn't exist|"
    r"unknown recipient|recipient unknown|unknown address|address unknown|mailbox not found|"
    r"mailbox unavailable|invalid recipient|recipient address rejected: user|account.{0,20}disabled",
    re.I,
)


def _is_no_such_user(code: int, text: str) -> bool:
    if not 500 <= code < 600:
        return False
    if re.search(r"sender|relay|spf|dkim|auth|blocked|blacklist|blocklist|policy|spam", text, re.I):
        return False  # 送る側の問題。宛先は悪くない
    if re.search(r"5\.(?:7|5|3|4|2)\.\d+", text) and not re.search(r"5\.1\.\d+", text):
        return False  # 5.7.x（拒否・中継不可）などは宛先不明ではない
    return _NO_SUCH_USER_RE.search(text) is not None


@contextlib.contextmanager
def _send_lock(cfg: Config):
    """送信の同時実行を防ぐ。2本同時に動くと、同じ会社に2通届いたり上限を超えたりする。"""
    path = cfg.root / "data" / "send.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass  # Windows など。1本ずつ動かしてください
        except OSError:
            raise TemplateError("別の送信（send --live）が実行中です。終わってからやり直してください")
        yield
    finally:
        handle.close()


def plan_count(conn: sqlite3.Connection, cfg: Config, targets: int, limit: int | None, live: bool) -> int:
    """今回送る数。本番では、今日の残り枠を絶対に超えない。"""
    room = max(0, cfg.get_int("DAILY_LIMIT") - db.sent_today(conn)) if live else None
    return max(0, min(x for x in (room, limit, targets) if x is not None))


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
    if limit is not None and limit < 1:
        raise TemplateError("--limit は1以上で指定してください")
    if not live:
        return _write_drafts(conn, cfg, template, segment, limit, log)
    with _send_lock(cfg):
        return _send_live(conn, cfg, template, segment, limit, log, sleep)


def _prepare(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    segment: str | None,
    limit: int | None,
    live: bool,
    summary: SendSummary,
) -> list[tuple[sqlite3.Row, str, str]]:
    targets = db.sendable_contacts(conn, segment=segment)
    summary.planned = len(targets)
    cap = plan_count(conn, cfg, len(targets), limit, live)
    summary.skipped_limit = len(targets) - cap
    # 先に全員分を組み立てる。差し込みの不備があれば1通も送らずに止まる
    return [(row, *render(template, cfg, row)) for row in targets[:cap]]


def _write_drafts(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    segment: str | None,
    limit: int | None,
    log: Callable[[str], None],
) -> SendSummary:
    summary = SendSummary()
    prepared = _prepare(conn, cfg, template, segment, limit, False, summary)
    if not prepared:
        return summary
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


def _send_live(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    segment: str | None,
    limit: int | None,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
) -> SendSummary:
    summary = SendSummary()
    prepared = _prepare(conn, cfg, template, segment, limit, True, summary)
    if not prepared:
        return summary
    interval = max(0.0, cfg.get_float("SEND_INTERVAL_SEC"))
    daily_limit = cfg.get_int("DAILY_LIMIT")
    smtp: smtplib.SMTP | None = None
    refused_streak: list[int] = []  # 続けて断られた連絡先（宛先不明にしたもの）

    def finish(send_id: int, status: str, error: str = "") -> None:
        conn.execute("UPDATE sends SET status=?, error=?, sent_at=? WHERE id=?", (status, error, db.now(), send_id))
        conn.commit()

    try:
        first = True
        for row, subject, body in prepared:
            if not first:
                sleep(interval)
            # 待ったあと、送る直前に、宛先を決めるのと同じ決まりでもう一度確かめる
            # （待っている間に停止の連絡・見送り・お断りの検知・別の送信が入ることがある）
            if db.sent_today(conn) >= daily_limit:
                summary.stopped = "今日の上限に達しました"
                break
            still_ok = {r["contact_id"] for r in db.sendable_contacts(conn, segment=segment)}
            if row["contact_id"] not in still_ok:
                log(f"[見送り] {row['name']} <{row['email']}>（停止・見送り・送信済みのいずれか）")
                continue
            first = False

            # 待っている間に回線が切れていたら、つなぎ直す（まだ何も送っていないので安全）
            try:
                if smtp is None or smtp.noop()[0] != 250:
                    raise smtplib.SMTPServerDisconnected("reconnect")
            except (smtplib.SMTPException, OSError):
                with contextlib.suppress(Exception):
                    if smtp is not None:
                        smtp.close()
                try:
                    smtp = _connect_smtp(cfg)
                except (smtplib.SMTPException, OSError) as e:
                    summary.stopped = f"メールサーバーにつながりません: {type(e).__name__}: {e}"
                    break

            msg = build_message(cfg, row["email"], subject, body)
            # 送る前に「届いたか不明」として書いておく。途中で止められても（Ctrl-C など）再送しないため
            cur = conn.execute(
                "INSERT INTO sends(contact_id, template, subject, message_id, status, error, sent_at)"
                " VALUES (?,?,?,?, 'unknown', '送信中に中断', ?)",
                (row["contact_id"], template.name, subject, msg["Message-ID"], db.now()),
            )
            conn.commit()
            send_id = cur.lastrowid
            try:
                refused = smtp.send_message(msg, from_addr=cfg.get("SENDER_EMAIL"), to_addrs=[row["email"]])
            except smtplib.SMTPRecipientsRefused as e:
                code, raw = next(iter(e.recipients.values()), (0, b""))
                text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                finish(send_id, "failed", f"{code} {text}")
                summary.failed += 1
                log(f"[失敗] {row['name']} <{row['email']}> {code} {text}")
                if not 500 <= code < 600:
                    continue  # 一時的な拒否。次回もう一度試す
                earlier = conn.execute(
                    "SELECT COUNT(*) FROM sends WHERE contact_id=? AND status='failed' AND error LIKE '5%'",
                    (row["contact_id"],),
                ).fetchone()[0]
                if _is_no_such_user(code, text) or earlier >= 2:
                    # 宛先が存在しない（または別の日にも断られた）。次回以降は送らない
                    conn.execute(
                        "UPDATE contacts SET status='bounced', decided_at=? WHERE id=?", (db.now(), row["contact_id"])
                    )
                    conn.commit()
                refused_streak.append(row["contact_id"])
                if len(refused_streak) >= 2:
                    # 続けて2社に断られたら、相手ではなくこちらの設定の問題を疑う。宛先不明の印は取り消す
                    marks = ",".join("?" * len(refused_streak))
                    conn.execute(
                        f"UPDATE contacts SET status='approved' WHERE status='bounced' AND id IN ({marks})",
                        refused_streak,
                    )
                    conn.commit()
                    summary.stopped = "続けて断られました。SMTP の設定（送信元アドレスなど）を確認してください"
                    break
                continue
            except (
                smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError,
                smtplib.SMTPHeloError,
                smtplib.SMTPNotSupportedError,
            ) as e:
                finish(send_id, "failed", f"{type(e).__name__}: {e}")
                summary.failed += 1
                summary.stopped = f"サーバーが送信を受け付けません: {e}"
                log(f"[失敗] {row['name']} <{row['email']}> {e}")
                break
            except (smtplib.SMTPServerDisconnected, OSError) as e:
                # 送っている途中で切れた。相手に届いたかどうか分からないので「送った扱い」にして再送しない
                finish(send_id, "unknown", f"{type(e).__name__}: {e}")
                summary.unknown += 1
                summary.stopped = "送信の途中で回線が切れました（届いたか不明の1通は、送った扱いにして再送しません）"
                log(f"[不明] {row['name']} <{row['email']}> 回線が切れました")
                smtp = None
                break
            except smtplib.SMTPException as e:
                finish(send_id, "failed", f"{type(e).__name__}: {e}")
                summary.failed += 1
                summary.stopped = f"送信に失敗しました: {e}"
                log(f"[失敗] {row['name']} <{row['email']}> {e}")
                break
            if refused:
                finish(send_id, "failed", str(refused))
                summary.failed += 1
                continue
            finish(send_id, "sent")
            refused_streak.clear()
            summary.sent += 1
            log(f"[送信] {row['name']} <{row['email']}>")
    finally:
        if smtp is not None:
            with contextlib.suppress(Exception):
                smtp.quit()
    return summary

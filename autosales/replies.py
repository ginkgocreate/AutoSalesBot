"""返信の取り込み（IMAP）。「不要」は停止リストへ、番号（1〜4）は集計へ、不達は除外へ。

受信箱の未読・既読は変えない（BODY.PEEK で読む）。同じメールは Message-ID で二重に数えない。
こちらが送ったメールと関係のないメールは、中身を保存しない。
"""
from __future__ import annotations

import base64
import email
import email.policy
import html as html_lib
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
from .extract import is_free_mail

# はっきりした停止の意思
_OPTOUT_STRONG = re.compile(
    r"配信停止|送信停止|配信.{0,4}解除|送らないで|送ってこないで|送信しないで|送付しないで|連絡しないで|"
    r"メール(?:を|は)?しないで|リストから(?:外|削除)|迷惑メール|迷惑です|"
    r"(?:メール|ご?案内|ご?連絡|送信|配信|営業).{0,10}(?:ご遠慮|遠慮|お控え|控えて|お断り|止めて|やめて|停止)|"
    r"(?:停止|解除|削除)(?:して(?:ください|下さい)|を?(?:お願い|希望|願))|"
    r"お断り(?:し|いた|致|申|です)|[い要]らない(?:です|ので)|"
    r"不要(?!ですか|でしょうか|なら|な(?:ので|ため)|か[?？]|かどうか)|"
    r"unsubscribe|remove me|(?<![-A-Za-z])stop(?![-A-Za-z])",
    re.I,
)
# 「不要なので…」は、続きで決まる（「不要なのでやめて」は停止、「説明は不要なので資料だけ」は関心あり）
_FUYOU_NANODE = re.compile(r"不要な(?:ので|ため)(.{0,25})")
_WANTS_MORE = re.compile(r"資料|詳し|ください|下さい|お願い|ほしい|欲しい|興味|教えて")
_WANTS_STOP = re.compile(r"やめて|止めて|しないで|結構|お断り|遠慮|控え|停止|解除")
# 弱い言い方。番号の返信が無く、ほかに用件が無さそうなときだけ停止とみなす
_OPTOUT_WEAK = re.compile(r"必要(?:ありません|ない|ございません)|要りません|いりません|結構です|間に合って")
# 引用の見出し。この行だけ飛ばす（引用の本体は > 付きなので別に飛ばされる。下に返信が続くことがある）
_QUOTE_INTRO_RE = re.compile(
    r"^(On .{5,100} wrote:|\d{4}[年/\-].{3,80}(wrote|書きました|のメッセージ|のメール)[:：]?|\d{4}[年/\-].{3,100}<[^<>]+@[^<>]+>\s*[:：])$", re.I)
# ここから下は、転送された元のメール。相手が書いた文ではない
_QUOTE_BLOCK_RE = re.compile(
    r"^(-{2,}\s*Original Message|-{5,}|_{5,}|─{5,}|差出人\s*[:：]|From\s*[:：]|送信元\s*[:：]|送信者\s*[:：])", re.I)
_QUOTED_LINE_RE = re.compile(r"^\s*[>＞]")
# 番号と認める書き方（それ以外の数字は数えない。住所・日付・「3店舗」などを拾わないため）
_CHOICE_ONLY = re.compile(r"^[(（「『【]?([1-4])[)）」』】]?\s*(?:番(?!地|号))?\s*(?:です|でお願いします|でお願いいたします)?[。.．!！]?$")
_CHOICE_HEAD = re.compile(r"^[(（「『【]?([1-4])(?:[)）」』】]|番(?!地|号)|\s*[、。.．:：](?!\s*[0-9])|\s+(?![0-9]))")
_CHOICE_TAIL = re.compile(r"(?:は|→|[:：=]|。|番号|回答|No\.?)\s*([1-4])\s*(?:番)?[。.．]?$", re.I)
_CHOICE_SAID = re.compile(r"(?<![0-9./:\-])([1-4])\s*(?:番(?!地|号))?\s*(?:です|でお願い|になります|が近い|に近い|に該当|かな|だと思)")
_CHOICE_LIST = re.compile(r"(?<![0-9])[1-4]\s*(?:[、,と・&/〜~]|か|または|or)\s*[1-4](?![0-9])", re.I)
_BOUNCE_LOCALS = {"mailer-daemon", "postmaster"}
_NO_USER_TEXT = re.compile(r"user unknown|unknown user|no such user|does not exist|5\.1\.[1236]\b|mailbox not found", re.I)


@dataclass
class Classified:
    kind: str  # reply / optout / bounce
    choice: int | None
    snippet: str


def _decode_iso2022jp(payload: bytes) -> str:
    """ISO-2022-JP を、機種依存文字（①㈱）や半角カナも含めて読む。標準の読み手はこれらを読めない。"""
    out: list[str] = []
    mode = "ascii"
    for i, piece in enumerate(re.split(rb"(\x1b\$[@B]|\x1b\([BJI])", payload)):
        if i % 2:
            mode = "kanji" if piece[1:2] == b"$" else ("kana" if piece.endswith(b"I") else "ascii")
        elif mode == "kanji":
            out.append(bytes(b | 0x80 for b in piece).decode("euc_jis_2004", "replace"))
        elif mode == "kana":
            out.append(b"".join(bytes([0x8E, b | 0x80]) for b in piece).decode("euc_jp", "replace"))
        else:
            out.append(piece.decode("ascii", "replace"))
    return "".join(out)


def _decode_part(part: EmailMessage) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = (part.get_content_charset() or "").lower()
    if "2022" in charset or "cp5022" in charset or b"\x1b$B" in payload or b"\x1b$@" in payload:
        return _decode_iso2022jp(payload)
    if charset in ("shift_jis", "shift-jis", "sjis", "x-sjis", "ms932", "windows-31j", "cp932"):
        charset = "cp932"
    for enc in ([charset] if charset else []) + ["utf-8", "cp932"]:
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


def _text_of(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = _decode_part(part)
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?is)<blockquote.*?</blockquote>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
    return unicodedata.normalize("NFKC", text)


def own_words(text: str) -> str:
    """相手が自分で書いた部分だけを返す。引用（> の行と、その見出し）は飛ばす。"""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _QUOTED_LINE_RE.match(line) or _QUOTE_INTRO_RE.match(stripped):
            continue
        if _QUOTE_BLOCK_RE.match(stripped):
            break
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _find_choice(body: str) -> int | None:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    head = lines[:5]
    if any(_CHOICE_LIST.search(ln) for ln in head):
        return None  # 「1と2」のように複数。人が読む
    found: set[int] = set()
    for ln in head:
        for pattern in (_CHOICE_ONLY, _CHOICE_HEAD, _CHOICE_TAIL):
            m = pattern.search(ln)
            if m:
                found.add(int(m.group(1)))
    for ln in lines[:12]:
        found.update(int(m.group(1)) for m in _CHOICE_SAID.finditer(ln))
    return found.pop() if len(found) == 1 else None


def _wants_stop(body: str) -> bool:
    if _OPTOUT_STRONG.search(body):
        return True
    for m in _FUYOU_NANODE.finditer(body):
        rest = m.group(1)
        if _WANTS_STOP.search(rest) or not _WANTS_MORE.search(rest):
            return True
    return False


def classify_reply(text: str, subject: str = "", auto: bool = False) -> Classified:
    body = own_words(unicodedata.normalize("NFKC", text))
    snippet = " ".join(body.split())[:200]
    choice = None if auto else _find_choice(body)  # 自動返信の数字（「3営業日」など）は数えない
    subject = re.sub(r"^\s*((re|fw|fwd|返信|転送)\s*[:：]\s*)+", "", unicodedata.normalize("NFKC", subject), flags=re.I)
    if _wants_stop(body):
        return Classified("optout", choice, snippet)
    if not body.strip() and subject and (_wants_stop(subject) or subject.strip() in ("不要", "不要です")):
        return Classified("optout", None, subject[:200])  # 本文が空で、件名だけに書いてくる人がいる
    if choice is None and _OPTOUT_WEAK.search(body) and len("".join(body.split())) <= 12:
        return Classified("optout", None, snippet)
    return Classified("reply", choice, snippet)


def _ids(header: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", header or "")


def _sent_contacts(conn: sqlite3.Connection) -> dict[str, int]:
    """こちらが送った（または届いたかもしれない）宛先。 email -> contact_id"""
    return {
        row["email"]: row["id"]
        for row in conn.execute(
            "SELECT DISTINCT ct.id, ct.email FROM contacts ct JOIN sends s ON s.contact_id = ct.id "
            "WHERE s.status IN ('sent','unknown')"
        )
    }


def _bounced_addresses(msg: EmailMessage, raw: bytes, text: str, sent: dict[str, int]) -> list[str] | None:
    """不達通知から、届かなかった宛先（こちらが送ったもの）を返す。不達通知でなければ None。"""
    statuses = [p for p in msg.walk() if p.get_content_type() == "message/delivery-status"]
    if statuses:
        failed: list[str] = []
        for part in statuses:
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else [part]
            for block in blocks:
                get = block.get if hasattr(block, "get") else (lambda *_: None)
                action = str(get("Action") or "").strip().lower()
                status = str(get("Status") or "").strip()
                rcpt = str(get("Final-Recipient") or get("Original-Recipient") or "")
                addr = db.normalize_email(rcpt.split(";", 1)[-1])
                if action == "failed" and status.startswith("5") and addr in sent:
                    failed.append(addr)
        return failed  # 遅延の通知（delayed / 4.x.x）は空で返り、何もしない
    haystack = (unicodedata.normalize("NFKC", raw.decode("utf-8", "replace")) + "\n" + text).lower()
    if not _NO_USER_TEXT.search(haystack) and "undeliver" not in haystack and "delivery" not in haystack \
            and "配信" not in haystack and "届" not in haystack:
        return None
    hits = []
    for addr in sent:
        if re.search(r"(?<![a-z0-9._%+\-])" + re.escape(addr) + r"(?![a-z0-9._%+\-])", haystack):
            hits.append(addr)
    return hits


def process_message(conn: sqlite3.Connection, raw: bytes, own_addresses: set[str]) -> str | None:
    """1通を処理して、記録した種類（reply/optout/bounce）を返す。関係ないメールは None。"""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = (str(msg.get("Message-ID") or "")).strip()
    from_addr = db.normalize_email(parseaddr(str(msg.get("From", "")))[1])
    if not from_addr or from_addr in own_addresses:
        return None
    if not message_id:
        # Message-ID の無いメールも二重に数えないよう、中身から決まった印を作る
        import hashlib
        message_id = "<no-id-" + hashlib.sha256(raw).hexdigest()[:32] + ">"
    if conn.execute("SELECT 1 FROM replies WHERE message_id=?", (message_id,)).fetchone():
        return None

    sent = _sent_contacts(conn)
    text = _text_of(msg)
    received = db.now()
    local = from_addr.partition("@")[0]
    is_report = msg.get_content_type() == "multipart/report"

    # ---- 不達通知 ----
    if is_report or (local in _BOUNCE_LOCALS and from_addr not in sent):
        hit = _bounced_addresses(msg, raw, text, sent)
        if not hit:
            return None
        for addr in hit:
            conn.execute("UPDATE contacts SET status='bounced', decided_at=? WHERE id=?", (received, sent[addr]))
            db.add_suppression(conn, addr, "不達")
        conn.execute(
            "INSERT INTO replies(contact_id, message_id, from_addr, kind, snippet, received_at)"
            " VALUES (?,?,?,?,?,?)",
            (sent[hit[0]], message_id, from_addr, "bounce", ", ".join(hit)[:200], received),
        )
        conn.commit()
        return "bounce"

    # ---- どの送信への返信かを決める ----
    contact_ids: list[int] = []
    thread_ids = _ids(str(msg.get("In-Reply-To") or "")) + _ids(str(msg.get("References") or ""))
    if thread_ids:
        marks = ",".join("?" * len(thread_ids))
        contact_ids = [r[0] for r in conn.execute(
            f"SELECT DISTINCT contact_id FROM sends WHERE status IN ('sent','unknown') AND message_id IN ({marks})",
            thread_ids)]
    if not contact_ids:
        row = conn.execute(
            "SELECT ct.id, ct.company_id FROM contacts ct WHERE ct.email=?", (from_addr,)
        ).fetchone()
        if row is not None and db.company_contacted(conn, row["company_id"]):
            # 同じ会社の別のアドレス（確認待ちなど）から返ってきた場合も、その会社の返信として扱う
            sent_in_company = conn.execute(
                "SELECT DISTINCT c.id FROM contacts c JOIN sends s ON s.contact_id=c.id "
                "WHERE c.company_id=? AND s.status IN ('sent','unknown')", (row["company_id"],)).fetchall()
            contact_ids = [r[0] for r in sent_in_company]
    if not contact_ids:
        # 別の担当者のアドレスから返ってくることがある。送った先と同じドメインなら同じ会社とみなす
        domain = db.email_domain(from_addr)
        if not is_free_mail(domain):
            contact_ids = [cid for addr, cid in sent.items() if db.email_domain(addr) == domain]
    if not contact_ids:
        return None  # こちらが送った相手と結び付かないメールは、中身を保存しない

    subject = str(msg.get("Subject", ""))
    auto = str(msg.get("Auto-Submitted", "no")).strip().lower() not in ("", "no")
    result = classify_reply(text, subject=subject, auto=auto)
    if result.kind != "optout" and re.search(r"unsubscribe|配信停止", unicodedata.normalize("NFKC", subject), re.I) \
            and not re.match(r"^\s*(re|返信)\s*[:：]", subject, re.I):
        result.kind = "optout"
    if result.kind == "optout":
        reason = "本人から停止の連絡"
        try:
            db.add_suppression(conn, from_addr, reason)
            if not is_free_mail(db.email_domain(from_addr)):
                # 本部などからの停止は、同じドメインの店すべてに効かせる
                db.add_suppression(conn, db.email_domain(from_addr), reason)
        except ValueError:
            pass  # 差出人を停止リストの形に直せなくても、送った先の停止は必ず入れる
        for cid in contact_ids:
            row = conn.execute("SELECT email FROM contacts WHERE id=?", (cid,)).fetchone()
            if row:
                db.add_suppression(conn, row["email"], reason)
    conn.execute(
        "INSERT INTO replies(contact_id, message_id, from_addr, kind, choice, snippet, received_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (contact_ids[0], message_id, from_addr, result.kind, result.choice, result.snippet, received),
    )
    conn.commit()
    return result.kind


def _imap_folder(name: str) -> str:
    """フォルダ名を IMAP の書き方（修正UTF-7、引用符つき）に直す。日本語や空白を含む名前のため。"""
    out: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            enc = base64.b64encode(buf.encode("utf-16-be")).decode("ascii").rstrip("=").replace("/", ",")
            out.append("&" + enc + "-")
            buf = ""

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return '"' + "".join(out).replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    counts = {"reply": 0, "optout": 0, "bounce": 0, "checked": 0, "unreadable": 0}
    folder = cfg.get("IMAP_FOLDER") or "INBOX"
    try:
        imap = imaplib.IMAP4_SSL(cfg.get("IMAP_HOST"), cfg.get_int("IMAP_PORT"))
    except OSError as e:
        raise SystemExit(f"IMAP サーバーにつながりません: {e}")
    try:
        try:
            imap.login(cfg.get("IMAP_USER"), cfg.get("IMAP_PASS"))
            typ, _ = imap.select(_imap_folder(folder), readonly=True)
        except imaplib.IMAP4.error as e:
            raise SystemExit(f"IMAP のログインかフォルダの選択に失敗しました: {e}")
        if typ != "OK":
            raise SystemExit(f"フォルダが開けません: {folder}")
        since = (datetime.now() - timedelta(days=max(1, days))).strftime("%d-%b-%Y")
        typ, data = imap.search(None, "SINCE", since)
        if typ != "OK":
            raise SystemExit("受信箱の検索に失敗しました")
        for num in (data[0] or b"").split() if data else []:
            typ, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in (parts or []) if isinstance(p, tuple) and len(p) > 1), None)
            if typ != "OK" or raw is None:
                counts["unreadable"] += 1
                log(f"[注意] {num.decode()} 番のメールを取得できませんでした（次回もう一度試します）")
                continue
            counts["checked"] += 1
            try:
                kind = process_message(conn, raw, own)
            except Exception as e:  # 1通の不備で、後ろのメールの取り込みを止めない
                counts["unreadable"] += 1
                log(f"[注意] {num.decode()} 番のメールを処理できませんでした: {type(e).__name__}: {e}")
                continue
            if kind:
                counts[kind] += 1
                log(f"[{kind}] 1件を記録しました")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return counts

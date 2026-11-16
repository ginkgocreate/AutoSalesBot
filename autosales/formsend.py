"""お問い合わせフォームへの自動送信。

送るのは db.sendable_forms() が返した相手だけ（人が承認した、CAPTCHA のない普通のフォーム）。
1日の上限・1社1回・停止リスト・送信の間隔は、メールと共通。
送る直前にページを読み直し、承認したときと作りが変わっていたら送らない。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from typing import Callable
from urllib.parse import urlencode, urlsplit

from . import db
from .config import Config
from .crawler import DEMOTED, Crawler, FetchError, Robots, _decode_with, save_form
from .extract import find_refusals, parse_html, site_host
from .forms import FormValueError, _BACK_BUTTON, fill, find_contact_form, find_guard, parse_forms
from .mailer import SendSummary, Template, TemplateError, _send_lock, plan_count, render

_SUCCESS = re.compile(
    r"ありがとうございま|送信(?:が)?(?:完了|しました|されました|いたしました|致しました)|"
    r"受け?付け?(?:ました|いたしました|致しました|完了)|完了(?:しました|いたしました)|承りました|"
    r"thank you|thanks for|successfully|has been sent|message sent",
    re.I,
)
# 完了の語があっても、こういう語が一緒にあれば「送れた」とは決めない
_ERROR = re.compile(
    r"エラー|失敗|できません|ませんでした|完了しません|正しく|不正|無効|未入力|入力してください|入力されていません|"
    r"入力内容に|もう一度|やり直|error|invalid|failed|not sent|could not|unable",
    re.I,
)
_DELIVERED = re.compile(
    r"ありがとうございました|送信(?:が)?(?:完了しました|完了いたしました|しました|されました|いたしました|致しました)|"
    r"受け?付け?(?:ました|いたしました|致しました|完了)|承りました|"
    r"successfully|has been sent|message sent",
    re.I,
)
_HELP_CONDITION = re.compile(r"場合|ときは|際は|\b(?:if|when)\b", re.I)
_INPUT_ERROR = re.compile(r"入力内容に(?:誤り|エラー)|必須項目が未入力|必須項目が入力されていません", re.I)
_THANKS_PATH = {
    "thanks",
    "thankyou",
    "thank",
    "thx",
    "complete",
    "completed",
    "completion",
    "finish",
    "finished",
    "kanryo",
    "kanryou",
}
_FINAL_BUTTON = re.compile(r"送信|送る|確定|submit|send|申し込|問い?合わ?せる|この内容で|はい|ok", re.I)
# 一部の文字コード（Shift_JIS など）に無い字の言い換え
_SAFE_CHARS = str.maketrans({"〜": "～", "—": "―", "−": "－", "‐": "-", "•": "・", "─": "-", "\u00a0": " "})
_CHARSET_ALIASES = {
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "ms_kanji": "cp932",
    "windows-31j": "cp932",
    "ms932": "cp932",
    "euc-jp": "euc_jp",
}


def sender_values(cfg: Config) -> dict[str, str]:
    return {
        "company": cfg.get("SENDER_COMPANY"),
        "person": cfg.get("SENDER_PERSON"),
        "kana": cfg.get("SENDER_PERSON_KANA"),
        "email": cfg.get("REPLY_TO") or cfg.get("SENDER_EMAIL"),
        "tel": cfg.get("SENDER_TEL"),
        "zip": cfg.get("SENDER_ZIP"),
        "address": cfg.get("SENDER_ADDRESS"),
        "url": cfg.get("DEMO_URL"),
    }


def render_for(template: Template, cfg: Config, row: sqlite3.Row | dict) -> tuple[str, str]:
    contact = {"name": row["name"], "area": row["area"], "category": row["category"], "source_url": row["page_url"]}
    return render(template, cfg, contact, via_form=True)


def _encode(value: str, charset: str) -> bytes:
    charset = _CHARSET_ALIASES.get(charset.strip().lower(), charset)  # Shift_JIS と書いたページは、実際には cp932
    value = value.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        return value.encode(charset)
    except LookupError:
        raise FormValueError(f"このフォームの文字コード（{charset}）を扱えません")
    except UnicodeEncodeError:
        pass
    out = bytearray()
    for ch in value.translate(_SAFE_CHARS):
        try:
            out += ch.encode(charset)
        except UnicodeEncodeError:
            # 表せない字は、同じ意味の字に直す（㈱→(株)、①→1 など）。それでも無理なら送らない
            try:
                out += unicodedata.normalize("NFKC", ch).encode(charset)
            except UnicodeEncodeError:
                raise FormValueError(f"このフォームの文字コード（{charset}）で表せない字があります: {ch!r}")
    return bytes(out)


def encode_pairs(pairs: list[tuple[str, str]], charset: str, multipart: bool) -> tuple[bytes, str]:
    """(送る中身, Content-Type) を返す。ブラウザと同じ形にする。"""
    encoded = [(_encode(name, charset), _encode(value, charset)) for name, value in pairs]
    if not multipart:
        body = urlencode(encoded).encode("ascii")
        return body, "application/x-www-form-urlencoded"
    boundary = "----AutoSalesBot" + os.urandom(12).hex()
    out = bytearray()
    for name, value in encoded:
        safe = name.replace(b'"', b"%22").replace(b"\r", b"%0D").replace(b"\n", b"%0A")
        out += b"--" + boundary.encode() + b'\r\nContent-Disposition: form-data; name="' + safe + b'"\r\n\r\n'
        out += value + b"\r\n"
    out += b"--" + boundary.encode() + b"--\r\n"
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def judge(
    html: str,
    url: str,
    status: int,
    body_name: str,
    body: str,
    *,
    redirected: bool = False,
    allow_confirm: bool = True,
    ours: tuple[str, ...] = (),
    form_kind: str | None = None,
    cf7_id: str | None = None,
) -> tuple[str, str, dict | None]:
    """送信後に返ってきたページから、結果を決める。 (状態, 補足, 確認画面の送信内容)

    状態:
      sent    … 完了の表示を確かめられた
      failed  … 送れていないと分かる（入力画面にこちらの入力が残ったまま戻された、など）。人が送る方に回す
      stuck   … 確認画面までは進んだが、自動では進められない。最終の送信はしていない。人が送る方に回す
      unknown … どちらとも決められない。届いたかもしれないので、もう一度は送らない
      confirm … 確認画面。もう1回送る
    迷ったら unknown にする（failed にすると人がもう一度送るので、届いていた場合に2通になる）。
    redirected は、送信のあとに転送されたか（送信そのものは受け付けられている）。
    ours は、こちらが入れた値（メールアドレスなど）。入力画面に残っていれば、戻されたと分かる。
    """
    if status >= 400:
        if redirected or status >= 500:
            return "unknown", f"HTTP {status}", None
        return "failed", f"HTTP {status}", None
    page = parse_forms(html, url)
    text = parse_html(html, url).text
    # Completion evidence blocks retries even when a static note also contains error words.
    delivered = bool(_DELIVERED.search(text))
    cf7_forms = [form for form in page.forms if "wpcf7-form" in form.cls.split()]
    # A different widget's status must never make an accepted inquiry retryable.
    if len(cf7_forms) > 1:
        return "unknown", "複数のフォームがあり、送信結果を特定できません", None
    if cf7_forms:
        form = cf7_forms[0]
        states = {"sent", "invalid", "failed", "spam", "unaccepted", "aborted"}
        state = form.attrs.get("data-status") or next((s for s in form.cls.split() if s in states), "")
        has_body = any(f.name == body_name and f.tag == "textarea" for f in form.fields)
        id_matches = cf7_id is None or any(
            f.type == "hidden" and f.name == "_wpcf7" and f.value == cf7_id for f in form.fields
        )
        if form_kind not in (None, "cf7") or not has_body or not id_matches:
            state = ""
        if state == "sent":
            return "sent", "", None
        if state in ("invalid", "failed", "spam", "unaccepted", "aborted"):
            if delivered or redirected:
                return "unknown", "フォームの状態と完了表示が一致しません（重ねて送りません）", None
            return "failed", f"フォームの返事: {state}", None
    normalized_body = _squash(body)
    snippet = normalized_body[:30]
    # Help for a hypothetical failure must not invalidate a separate success message.
    errors = [s for s in re.split(r"[。.!?！？\n]+", text) if _ERROR.search(s) and not _HELP_CONDITION.search(s)]
    error = bool(errors)
    returned_empty = False
    echoed_input = False
    confirmation_seen = False
    confirmations = []
    for form in page.forms:
        if form.implicit:
            continue
        typing = [
            f
            for f in form.fields
            if (f.tag == "textarea" or f.type in ("text", "email", "tel", "url", "number", ""))
            and f.tag != "button"
            and not f.styled_hidden
        ]
        boxes = [f for f in form.fields if f.type in ("checkbox", "radio") and not f.styled_hidden]
        buttons = [f for f in form.fields if f.type in ("submit", "image", "button") or f.tag == "button"]
        forward = [b for b in buttons if not _BACK_BUTTON.search(f"{b.value} {b.text} {b.name}")]
        body_fields = [f for f in form.fields if f.tag == "textarea" and f.name == body_name]
        carries = bool(normalized_body) and any(
            f.type == "hidden" and f.name == body_name and _squash(f.value) == normalized_body for f in form.fields
        )
        # An address copied into another form does not prove that the inquiry failed.
        if body_fields:
            returned_empty = True
            echoed_input |= any(snippet and snippet in _squash(f.value) for f in body_fields) or any(
                f.value and f.value in ours for f in typing if f.tag != "textarea"
            )
        # Page-level text can accompany unrelated carts, navigation, or subscriptions.
        # Only a form preserving the complete inquiry in the original field may proceed.
        if not typing and carries:
            confirmation_seen = True
            final = [b for b in forward if b.type == "submit" and _FINAL_BUTTON.search(f"{b.value} {b.text}")]
            if len(final) != 1 and len([b for b in forward if b.type == "submit"]) == 1:
                final = [b for b in forward if b.type == "submit"]
            if len(final) != 1 or boxes or any(f.tag == "select" for f in form.fields):
                continue
            if form.method != "post" or site_host(form.action) != site_host(url):
                continue
            pairs = [(f.name, f.value) for f in form.fields if f.type == "hidden" and f.name]
            if final[0].name:
                pairs.append((final[0].name, final[0].value or final[0].text))
            confirmations.append(
                {
                    "action": form.action.split("#", 1)[0],
                    "pairs": pairs,
                    "multipart": "multipart" in form.enctype,
                    "method": form.method,
                }
            )
    if echoed_input:
        if delivered or redirected:
            return "unknown", "完了後に入力内容が表示されています（重ねて送りません）", None
        return "failed", "入力画面に戻されました（こちらの入力が残っています）", None
    if confirmation_seen:
        if delivered or not allow_confirm:
            return "unknown", "完了後または2回目の確認画面が表示されました（重ねて送りません）", None
        if len(confirmations) == 1 and not returned_empty:
            return "confirm", "", confirmations[0]
        return "stuck", "確認画面を自動で進められません（送信先が一意でない・ボタンが画像・チェックが必要など）", None
    if returned_empty:
        # Static field instructions are not evidence that a POST was rejected.
        if not redirected and not _SUCCESS.search(text) and any(_INPUT_ERROR.search(s) for s in errors):
            return "failed", "入力画面に戻されました（エラーの表示があります）", None
        return "unknown", "入力フォームがもう一度表示されました（送れたかどうかを確かめられません）", None
    if _SUCCESS.search(text) and not error:
        return "sent", "", None
    # 転送先のページ名（最後の区切り）が thanks.html / complete.php のような名前のとき
    words = set(re.split(r"[._\-]+", urlsplit(url).path.lower().rstrip("/").rsplit("/", 1)[-1]))
    if redirected and words & _THANKS_PATH and not error:
        return "sent", "", None
    return "unknown", "完了の表示を確認できませんでした", None


def submit(
    crawler: Crawler,
    robots: Robots,
    row: sqlite3.Row,
    sender: dict[str, str],
    subject: str,
    body: str,
    before_post: Callable[[], None],
    seen: dict | None = None,
) -> tuple[str, str]:
    """1社のフォームに送る。 (状態, 補足) を返す。

    状態: sent / unknown / failed（送れていない。人が送る方に回す）/ skipped（今回は送らない。フォームはそのまま）
          / changed（作りが変わった）/ refused（お断りの表示）
    before_post は、最初の送信（POST）を出す直前に1回だけ呼ばれる。
    seen を渡すと、読み直したフォームの作りを seen["plan"] に入れる（作りが変わったときの控え直し用）。
    """
    host = row["host"]
    session = crawler.new_session()
    try:
        url, status, _, raw, charset, _ = crawler._open(row["page_url"], robots=robots, stay_on=host, opener=session)
    except FetchError as e:
        return "skipped", f"フォームのページを開けません: {e}"
    if status >= 400:
        return "skipped", f"フォームのページを開けません: HTTP {status}"
    html, encoding = _decode_with(raw, charset)

    confirmed = {line for line in (row["refusal_confirmed"] or "").split("\n") if line}

    def refusal_in(page_html: str, page_url: str) -> str:
        found = [s for s in find_refusals(parse_html(page_html, page_url)) if s not in confirmed]
        return found[0] if found else ""

    refusal = refusal_in(html, url)
    if refusal:
        return "refused", refusal

    plan = find_contact_form(html, url, encoding)
    if plan is None:
        return "changed", "フォームが見つかりません"
    if plan.mode == "auto" and not robots.can_fetch(plan.action):
        plan.mode, plan.reason = "skip", "送信先が robots.txt で禁止されています"
    if seen is not None:
        seen["plan"] = plan
    if plan.mode != "auto" or plan.signature() != row["signature"]:
        return "changed", plan.reason or "承認したときと、欄の並びが違います"
    try:
        pairs = fill(plan.items, sender, subject, body)
        if plan.submit:
            pairs.append((plan.submit["name"], plan.submit["value"]))
        use_rest = bool(plan.rest_url) and robots.can_fetch(plan.rest_url)
        data, ctype = encode_pairs(pairs, plan.charset or encoding, use_rest or "multipart" in plan.enctype)
    except FormValueError as e:
        return "skipped", str(e)  # 設定や文字の都合。フォームのせいではないので、人に回さない
    ours = tuple(v for v in (sender.get("email", ""), sender.get("person", ""), sender.get("company", "")) if v)

    final_sent = False  # 相手に届く送信（確認画面がある場合は、その先の最終送信）を出したか
    try:
        before_post()
        if use_rest:
            final_sent = True
            _, code, _, raw, charset, _ = crawler._open(
                plan.rest_url,
                robots=robots,
                stay_on=host,
                data=data,
                content_type=ctype,
                opener=session,
                referer=url,
                accept="application/json, */*;q=0.1",
            )
            return _judge_cf7_rest(code, _decode_with(raw, charset)[0])
        final_sent = True
        final, code, _, raw, charset, _ = crawler._open(
            plan.action, robots=robots, stay_on=host, data=data, content_type=ctype, opener=session, referer=url
        )
        result_html = _decode_with(raw, charset)[0]
        state, detail, confirm = judge(
            result_html,
            final,
            code,
            plan.body_name,
            body,
            redirected=crawler.redirected_after_post,
            ours=ours,
            form_kind=plan.kind,
            cf7_id=dict(pairs).get("_wpcf7", ""),
        )
        if state != "confirm" or not confirm:
            return state, detail
        # ここまでは確認画面を出しただけで、相手には届いていない
        final_sent = False
        refusal = refusal_in(result_html, final)
        if refusal:
            return "refused", refusal
        guard = find_guard(result_html)
        if guard:
            return "stuck", f"確認画面に、{guard}"
        if (
            confirm["method"] != "post"
            or site_host(confirm["action"]) != host
            or not robots.can_fetch(confirm["action"])
        ):
            return "stuck", "確認画面の送信先に送れません"
        data, ctype = encode_pairs(confirm["pairs"], plan.charset or encoding, confirm["multipart"])
        final_sent = True
        final2, code, _, raw, charset, _ = crawler._open(
            confirm["action"], robots=robots, stay_on=host, data=data, content_type=ctype, opener=session, referer=final
        )
        state, detail, _ = judge(
            _decode_with(raw, charset)[0],
            final2,
            code,
            plan.body_name,
            body,
            redirected=crawler.redirected_after_post,
            allow_confirm=False,
            ours=ours,
            form_kind=plan.kind,
            cf7_id=dict(pairs).get("_wpcf7", ""),
        )
        return ("unknown" if state == "stuck" else state), detail
    except FormValueError as e:
        return ("unknown" if final_sent else "stuck"), str(e)
    except FetchError as e:
        # 相手に届く送信を出したあとの失敗は、届いたかもしれない
        if not final_sent:
            return "stuck", str(e)
        return ("unknown" if e.maybe_sent else "skipped"), str(e)


def _judge_cf7_rest(code: int, text: str) -> tuple[str, str]:
    if code in (401, 403, 404, 405):
        return "failed", f"フォームの送信先が使えません（HTTP {code}）"
    try:
        reply = json.loads(text)
        state = str(reply.get("status", ""))
    except (ValueError, AttributeError):
        return "unknown", f"返事を読めませんでした（HTTP {code}）"
    message = str(reply.get("message", ""))[:120]
    if state == "mail_sent":
        return "sent", message
    if state in ("validation_failed", "acceptance_missing", "spam", "aborted", "mail_failed"):
        fields = ", ".join(str(f.get("field", "")) for f in reply.get("invalid_fields") or [] if isinstance(f, dict))
        return "failed", f"フォームの返事: {state} {message} {fields}".strip()
    return "unknown", f"フォームの返事: {state or code}"


def _draft_text(row: sqlite3.Row, items: list[dict], pairs: list[tuple[str, str]], subject: str, body: str) -> str:
    lines = [f"会社: {row['name']}", f"フォーム: {row['page_url']}", "", "入力する内容:"]
    values = dict(pairs)
    for item in items:
        if item["role"] in ("keep", "empty", "default", "honeypot", "body"):
            continue
        shown = (
            "（チェックを入れる）"
            if item["role"] == "check"
            else f"「{item.get('choice', '')}」を選ぶ"
            if item["role"] == "choice"
            else values.get(item["name"], "")
        )
        lines.append(f"  {item.get('label') or item['name']}: {shown}")
    lines += ["", f"件名（件名の欄があるとき）: {subject}", "", "----- 本文 -----", body]
    return "\n".join(lines)


def _prepare(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    segment: str | None,
    limit: int | None,
    live: bool,
    summary: SendSummary,
    log: Callable[[str], None],
) -> list[tuple]:
    sender = sender_values(cfg)
    ready = []
    for row in db.sendable_forms(conn, segment=segment, mode="auto"):
        subject, body = render_for(template, cfg, row)
        items = json.loads(row["plan"])["items"]
        try:
            pairs = fill(items, sender, subject, body)
        except FormValueError as e:
            log(f"[設定不足] {row['name']}: {e}")
            continue
        ready.append((row, subject, body, items, pairs))
    summary.planned = len(ready)
    if live:
        # Preflight skips must not prevent later eligible companies from being tried.
        return ready
    cap = plan_count(conn, cfg, len(ready), limit, live)
    summary.skipped_limit = len(ready) - cap
    return ready[:cap]


def send_forms(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    *,
    live: bool = False,
    segment: str | None = None,
    limit: int | None = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    crawler: Crawler | None = None,
) -> SendSummary:
    """承認済みのフォームに1社1回ずつ送る。live=False なら、入力内容を outbox/ に書くだけ。"""
    missing = cfg.missing_sender_fields()
    if missing:
        raise TemplateError("送信者の表示が未設定です（.env）: " + ", ".join(missing))
    if limit is not None and limit < 1:
        raise TemplateError("--limit は1以上で指定してください")
    summary = SendSummary()
    if not live:
        prepared = _prepare(conn, cfg, template, segment, limit, False, summary, log)
        outdir = cfg.root / "outbox" / db.now()[:10]
        for row, subject, body, items, pairs in prepared:
            outdir.mkdir(parents=True, exist_ok=True)
            path = outdir / f"form_{row['company_id']:05d}_{re.sub(r'[^A-Za-z0-9._-]', '_', row['host'])}.txt"
            path.write_text(_draft_text(row, items, pairs, subject, body), encoding="utf-8")
            conn.execute(
                "INSERT INTO form_sends(form_id, company_id, template, status, sent_at) VALUES (?,?,?, 'dry_run', ?)",
                (row["form_id"], row["company_id"], template.name, db.now()),
            )
            summary.dry_run += 1
            log(f"[下書き] {row['name']} → {path.relative_to(cfg.root)}")
        conn.commit()
        return summary
    with _send_lock(cfg):
        return _send_live(
            conn,
            cfg,
            template,
            segment,
            limit,
            log,
            sleep,
            summary,
            crawler or Crawler(user_agent=cfg.get("CRAWL_USER_AGENT"), delay=cfg.get_float("CRAWL_DELAY_SEC")),
        )


def _send_live(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    segment: str | None,
    limit: int | None,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
    summary: SendSummary,
    crawler: Crawler,
) -> SendSummary:
    prepared = _prepare(conn, cfg, template, segment, limit, True, summary, log)
    cap = plan_count(conn, cfg, len(prepared), limit, True)
    interval = max(0.0, cfg.get_float("SEND_INTERVAL_SEC"))
    daily_limit = cfg.get_int("DAILY_LIMIT")
    sender = sender_values(cfg)
    failures = 0
    first = True
    for index, (row, subject, body, _, _) in enumerate(prepared):
        # Failed POST attempts also count against the explicit per-run budget.
        if summary.sent + summary.unknown + summary.failed >= cap:
            summary.skipped_limit = len(prepared) - index
            break
        if not first:
            sleep(interval)
        # 送る直前に、相手を決めるのと同じ決まりでもう一度確かめる（待っている間に停止の連絡などが入ることがある）
        if db.sent_today(conn) >= daily_limit:
            summary.skipped_limit = len(prepared) - index
            summary.stopped = "今日の上限に達しました"
            break
        if row["form_id"] not in {r["form_id"] for r in db.sendable_forms(conn, segment=segment, mode="auto")}:
            summary.skipped += 1
            log(f"[見送り] {row['name']}（停止・見送り・連絡済みのいずれか）")
            continue
        first = False

        send_id: list[int] = []
        seen: dict = {}

        def before_post() -> None:
            # 送る前に「届いたか不明」として書いておく。途中で止められても（Ctrl-C など）もう一度送らないため
            cur = conn.execute(
                "INSERT INTO form_sends(form_id, company_id, template, status, detail, sent_at) "
                "VALUES (?,?,?, 'unknown', '送信中に中断', ?)",
                (row["form_id"], row["company_id"], template.name, db.now()),
            )
            conn.commit()
            send_id.append(cur.lastrowid)

        try:
            parts = urlsplit(row["page_url"])
            robots = crawler._robots(f"{parts.scheme}://{parts.netloc}", row["host"])
            if robots is None:
                state, detail = "skipped", "robots.txt を確かめられません"
            else:
                state, detail = submit(crawler, robots, row, sender, subject, body, before_post, seen)
        except FetchError as e:
            state, detail = ("unknown" if send_id else "skipped"), str(e)
        except Exception as e:  # 1社の不備で全体を止めない。送信を出したあとなら「不明」にする
            state, detail = ("unknown" if send_id else "skipped"), f"{type(e).__name__}: {e}"

        now = db.now()
        if send_id:
            status = state if state in ("sent", "unknown") else "failed"
            conn.execute(
                "UPDATE form_sends SET status=?, detail=?, sent_at=? WHERE id=?",
                (status, detail[:300], now, send_id[0]),
            )
        elif state not in ("changed", "refused", "skipped"):
            conn.execute(
                "INSERT INTO form_sends(form_id, company_id, template, status, detail, sent_at) "
                "VALUES (?,?,?, 'failed', ?, ?)",
                (row["form_id"], row["company_id"], template.name, detail[:300], now),
            )
        if state == "sent":
            summary.sent += 1
            failures = 0
            log(f"[送信] {row['name']} {row['page_url']}")
        elif state == "unknown":
            summary.unknown += 1
            failures = 0
            log(f"[不明] {row['name']} {detail}（送った扱いにして、もう一度は送りません）")
        elif state == "refused":
            summary.skipped += 1
            conn.execute(
                "UPDATE companies SET status='refused', status_note=? WHERE id=?",
                (("営業お断りの表示: " + detail)[:400], row["company_id"]),
            )
            log(f"[見送り] {row['name']} 営業お断りの表示が見つかりました: {detail}")
        elif state == "changed":
            summary.skipped += 1
            if seen.get("plan") is not None:
                save_form(conn, row["company_id"], seen["plan"], now)  # 新しい作りを控え、確認待ちに戻す
            conn.execute("UPDATE forms SET status='pending' WHERE id=? AND status='approved'", (row["form_id"],))
            log(f"[見送り] {row['name']} フォームが変わっています（{detail}）。確認待ちに戻しました")
        elif state == "skipped":
            summary.skipped += 1
            log(f"[見送り] {row['name']} {detail}（今回は送っていません）")
        else:
            summary.failed += 1
            failures += 1
            conn.execute(
                "UPDATE forms SET mode='manual', demoted=1, reason=? WHERE id=?",
                (f"{DEMOTED}（{detail}）"[:300], row["form_id"]),
            )
            log(f"[失敗] {row['name']} {detail}（人が送る方に回しました）")
        conn.commit()
        if failures >= 3:
            summary.stopped = "3社続けて送れませんでした。こちらの設定や文面に原因がないか確認してください"
            break
    return summary

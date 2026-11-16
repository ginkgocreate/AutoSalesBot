"""Inspect local configuration without contacting services or opening a database."""

from __future__ import annotations

import ipaddress
import math
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from .config import Config
from .mailer import PLACEHOLDER_RE, TemplateError, load_template

DEFAULT_TEMPLATES = ("honbu", "show", "honbu_form", "show_form")
_SETTINGS = (
    "SENDER_COMPANY",
    "SENDER_PERSON",
    "SENDER_ADDRESS",
    "SENDER_EMAIL",
    "OPTOUT_EMAIL",
    "REPLY_TO",
    "SERVICE_START",
    "DEMO_URL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "SMTP_SECURITY",
    "IMAP_HOST",
    "IMAP_PORT",
    "IMAP_USER",
    "IMAP_PASS",
    "IMAP_FOLDER",
    "DAILY_LIMIT",
    "SEND_INTERVAL_SEC",
    "CRAWL_USER_AGENT",
    "CRAWL_DELAY_SEC",
    "CRAWL_MAX_PAGES",
)
_LOCAL_PART = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(rf"{_LOCAL_PART}@{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+")
_HOST = re.compile(rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*")


def _email(value: str) -> bool:
    return bool(_EMAIL.fullmatch(value)) and len(value) <= 254


def _host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOST.fullmatch(value)) and len(value) <= 253


def _loopback(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _placeholder_email(value: str) -> bool:
    domain = value.rpartition("@")[2].lower()
    return (
        domain == "localhost"
        or domain.endswith((".localhost", ".invalid", ".test", ".example"))
        or any(
            domain == example or domain.endswith("." + example)
            for example in ("example.com", "example.net", "example.org")
        )
    )


def inspect_readiness(cfg: Config, template_names: Iterable[str] = DEFAULT_TEMPLATES) -> dict:
    """Return redacted stage readiness; ready means configured, never delivered."""
    stages = ("research", "draft", "email", "replies")
    blockers = {stage: [] for stage in stages}
    checks = []

    def check(code: str, ok: bool, affected: tuple[str, ...], failure: str) -> None:
        ok = bool(ok)
        message = f"{code}: 設定を確認しました。" if ok else failure
        checks.append({"code": code, "ok": ok, "message": message})
        if not ok:
            for stage in affected:
                blockers[stage].append(message)

    def number(key: str, affected: tuple[str, ...], *, integer=False, minimum=0, maximum=None) -> None:
        try:
            value = int(cfg.get(key)) if integer else float(cfg.get(key))
            valid = math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
        except (TypeError, ValueError, OverflowError):
            valid = False
        kind = "整数" if integer else "有限の数値"
        check(key, valid, affected, f"{key}: 有効な範囲の{kind}を設定してください。")

    for key in ("SENDER_COMPANY", "SENDER_PERSON", "SENDER_ADDRESS"):
        check(key, bool(cfg.get(key).strip()), ("draft", "email"), f"{key} が未設定です。")
    for key in ("SENDER_EMAIL", "REPLY_TO", "OPTOUT_EMAIL"):
        value = cfg.get(key)
        valid = _email(value) if value or key == "SENDER_EMAIL" else True
        check(key, valid, ("draft", "email", "replies"), f"{key}: メールアドレスの形式を確認してください。")
        check(
            key + ".production",
            not value or not _placeholder_email(value),
            ("email", "replies"),
            f"{key}: ダミー用ドメインです。実運用のメールアドレスへ変更してください。",
        )

    check(
        "CRAWL_USER_AGENT",
        bool(cfg.get("CRAWL_USER_AGENT").strip()) and not re.search(r"[\r\n]", cfg.get("CRAWL_USER_AGENT")),
        ("research",),
        "CRAWL_USER_AGENT: 空欄や改行を含まない識別名を設定してください。",
    )
    number("CRAWL_MAX_PAGES", ("research",), integer=True, minimum=1)
    number("CRAWL_DELAY_SEC", ("research",))
    number("DAILY_LIMIT", ("email",), integer=True, minimum=1)
    number("SEND_INTERVAL_SEC", ("email",))

    for protocol, stage in (("SMTP", "email"), ("IMAP", "replies")):
        host = cfg.get(protocol + "_HOST")
        check(
            protocol + "_HOST", bool(host) and _host(host), (stage,), f"{protocol}_HOST: 接続先名を設定してください。"
        )
        number(protocol + "_PORT", (stage,), integer=True, minimum=1, maximum=65535)
        user, password = cfg.get(protocol + "_USER").strip(), cfg.get(protocol + "_PASS")
        credentials_ok = bool(user and password.strip())
        if protocol == "SMTP" and _loopback(host) and not user and not password:
            credentials_ok = True
        check(
            protocol + ".credentials",
            credentials_ok,
            (stage,),
            f"{protocol}_USER / {protocol}_PASS: 認証情報を両方設定してください。",
        )
    security = cfg.get("SMTP_SECURITY").lower()
    check(
        "SMTP_SECURITY",
        security in ("ssl", "starttls") or (security == "none" and _loopback(cfg.get("SMTP_HOST"))),
        ("email",),
        "SMTP_SECURITY: ssl または starttls を設定してください。none はlocalhostの検証専用です。",
    )
    check(
        "IMAP_FOLDER",
        bool(cfg.get("IMAP_FOLDER").strip()) and not re.search(r"[\r\n]", cfg.get("IMAP_FOLDER")),
        ("replies",),
        "IMAP_FOLDER: 受信フォルダ名を設定してください。",
    )

    names = tuple(template_names)
    check("templates.selected", bool(names), ("draft", "email"), "確認するテンプレートを1つ以上指定してください。")
    template_root = (Path(cfg.root) / "templates").resolve()
    configured_fields = {
        "送信者会社名": "SENDER_COMPANY",
        "担当者名": "SENDER_PERSON",
        "提供開始時期": "SERVICE_START",
        "デモURL": "DEMO_URL",
    }
    contact_fields = {"会社名", "エリア", "業態", "掲載URL"}
    for index, name in enumerate(names, 1):
        code = f"template.{index}"
        template = None
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name):
            path = template_root / f"{name}.txt"
            try:
                if path.resolve().parent == template_root:
                    template = load_template(path)
            except (OSError, UnicodeError, TemplateError):
                pass
        check(
            code + ".readable",
            template is not None,
            ("draft", "email"),
            f"テンプレート{index}: 読み込み・形式を確認してください。",
        )
        if template is None:
            continue
        fields = set(PLACEHOLDER_RE.findall(template.subject + "\n" + template.body))
        known = fields <= contact_fields | configured_fields.keys()
        check(
            code + ".placeholders", known, ("draft", "email"), f"テンプレート{index}: 未対応の差し込み項目があります。"
        )
        for field in sorted(fields & configured_fields.keys()):
            key = configured_fields[field]
            check(
                code + "." + key,
                bool(cfg.get(key).strip()),
                ("draft", "email"),
                f"テンプレート{index}: {key} が未設定です。",
            )
        if "デモURL" in fields and cfg.get("DEMO_URL").strip():
            try:
                parsed = urlsplit(cfg.get("DEMO_URL"))
                valid_url = parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username
            except ValueError:
                valid_url = False
            check(
                code + ".DEMO_URL.format",
                valid_url,
                ("draft", "email"),
                f"テンプレート{index}: DEMO_URLの形式を確認してください。",
            )

    ready = {stage: not blockers[stage] for stage in stages}
    state = "research_only" if ready["research"] else "blocked"
    if ready["draft"]:
        state = "draft_only"
    if ready["email"]:
        state = "production_configured" if ready["replies"] else "email_configured"
    return {
        **{f"{stage}_ready": ready[stage] for stage in stages},
        "readiness_state": state,
        "delivery_verified": False,
        "note": "ローカル設定だけの確認です。接続・認証・到達・返信受信の成功を意味しません。",
        "blockers": blockers,
        "checks": checks,
        "settings_present": {key: bool(cfg.get(key).strip()) for key in _SETTINGS},
        "templates_checked": len(names),
    }

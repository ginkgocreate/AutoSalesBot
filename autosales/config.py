"""設定の読み込み。.env（KEY=VALUE）と環境変数だけを使う。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "SENDER_COMPANY": "",
    "SENDER_PERSON": "",
    "SENDER_ADDRESS": "",
    "SENDER_TEL": "",
    "SENDER_EMAIL": "",
    "OPTOUT_EMAIL": "",
    "REPLY_TO": "",
    "SERVICE_START": "",
    "DEMO_URL": "https://analink.lextribe.com/",
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASS": "",
    "SMTP_SECURITY": "starttls",
    "DAILY_LIMIT": "20",
    "SEND_INTERVAL_SEC": "60",
    "IMAP_HOST": "",
    "IMAP_PORT": "993",
    "IMAP_USER": "",
    "IMAP_PASS": "",
    "IMAP_FOLDER": "INBOX",
    "CRAWL_USER_AGENT": "AutoSalesBot/0.1",
    "CRAWL_DELAY_SEC": "2",
    "CRAWL_MAX_PAGES": "6",
    "DB_PATH": "data/autosales.db",
}


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 形式を読む。# で始まる行と空行は飛ばす。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@dataclass
class Config:
    root: Path
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str:
        return self.values.get(key, DEFAULTS.get(key, ""))

    def get_int(self, key: str) -> int:
        raw = self.get(key)
        try:
            return int(raw)
        except ValueError:
            raise SystemExit(f"設定 {key} は整数で指定してください（現在: {raw!r}）")

    def get_float(self, key: str) -> float:
        raw = self.get(key)
        try:
            return float(raw)
        except ValueError:
            raise SystemExit(f"設定 {key} は数値で指定してください（現在: {raw!r}）")

    def path(self, key: str) -> Path:
        p = Path(self.get(key))
        return p if p.is_absolute() else self.root / p

    @property
    def optout_email(self) -> str:
        return self.get("OPTOUT_EMAIL") or self.get("SENDER_EMAIL")

    def missing_sender_fields(self) -> list[str]:
        """特定電子メール法の表示に必要な項目のうち、空のものを返す。"""
        required = ["SENDER_COMPANY", "SENDER_PERSON", "SENDER_ADDRESS", "SENDER_EMAIL"]
        return [k for k in required if not self.get(k).strip()]


def load_config(root: Path | None = None, env: dict[str, str] | None = None) -> Config:
    """優先順位: 環境変数 > .env > 既定値。"""
    root = Path(root) if root else Path(os.environ.get("ASB_HOME") or Path.cwd())
    values = dict(DEFAULTS)
    values.update(parse_env_file(root / ".env"))
    source = os.environ if env is None else env
    for key in DEFAULTS:
        if key in source and source[key] != "":
            values[key] = source[key]
    return Config(root=root, values=values)

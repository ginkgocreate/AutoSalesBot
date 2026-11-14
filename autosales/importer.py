"""候補リスト（CSV）の取り込み。1行＝1社（または1店）の公式サイト。"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from . import db
from .crawler import is_non_official
from .extract import site_host

# 見出しは日本語でも英語でもよい
COLUMNS = {
    "name": ("会社名", "店名", "名前", "name"),
    "url": ("サイト", "公式サイト", "url", "URL", "サイトURL", "site"),
    "area": ("エリア", "地域", "area"),
    "category": ("業態", "業種", "category"),
    "stores": ("店舗数", "stores"),
    "segment": ("層", "segment"),
    "notes": ("メモ", "備考", "notes"),
}


def _pick(row: dict[str, str], key: str) -> str:
    for header in COLUMNS[key]:
        if header in row and row[header] is not None:
            return row[header].strip()
    return ""


def normalize_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        return None
    return raw


def import_csv(conn: sqlite3.Connection, path: Path, source: str = "") -> dict[str, int]:
    counts = {"added": 0, "duplicate": 0, "invalid": 0, "non_official": 0}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {(k or "").strip(): (v or "") for k, v in row.items()}
            url = normalize_url(_pick(row, "url"))
            name = _pick(row, "name")
            if not url or not name:
                counts["invalid"] += 1
                continue
            host = site_host(url)
            if is_non_official(host):
                # ポータルやSNSのアドレスは「自分のサイトでの公表」に当たらないので入れない
                counts["non_official"] += 1
                continue
            stores_raw = _pick(row, "stores")
            stores = int(stores_raw) if stores_raw.isdigit() else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO companies"
                "(name, site_url, host, area, category, store_count, segment, notes, source, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name, url, host, _pick(row, "area"), _pick(row, "category"), stores,
                 _pick(row, "segment"), _pick(row, "notes"), source or path.name, db.now()),
            )
            counts["added" if cur.rowcount else "duplicate"] += 1
    conn.commit()
    return counts

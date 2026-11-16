"""候補リスト（CSV）の取り込み。1行＝1社（または1店）の公式サイト。"""

from __future__ import annotations

import csv
import io
import sqlite3
import unicodedata
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
    raw = unicodedata.normalize("NFKC", raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None
    labels = host.split(".")
    if parts.scheme not in ("http", "https") or len(labels) < 2 or not all(labels) or " " in host:
        return None
    if parts.username or ":" in host or not labels[-1][:1].isalpha():
        return None  # info@example.jp のようなアドレスや、IPアドレスを直接書いたURLは会社のサイトではない
    return raw


def open_csv(path: Path) -> io.StringIO:
    """UTF-8（BOMあり・なし）か、Excel の既定の Shift_JIS で保存されたCSVを読む。"""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return io.StringIO(data.decode(enc), newline="")
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: 文字コードを読めません。UTF-8 か Shift_JIS で保存してください")


def add_company(
    conn: sqlite3.Connection,
    counts: dict[str, int],
    *,
    name: str,
    url: str,
    area: str = "",
    category: str = "",
    stores: int | None = None,
    segment: str = "",
    notes: str = "",
    source: str = "",
) -> bool:
    """候補を1社入れる。入れば True。counts に結果を数える。"""
    normalized = normalize_url(url)
    name = " ".join(unicodedata.normalize("NFKC", name or "").split())[:200]
    host = site_host(normalized) if normalized else ""
    if not normalized or not name or not host:
        counts["invalid"] += 1
        return False
    if is_non_official(host):
        # ポータルやSNSのアドレスは「自分のサイトでの公表」に当たらないので入れない
        counts["non_official"] += 1
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO companies"
        "(name, site_url, host, area, category, store_count, segment, notes, source, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, normalized, host, area[:100], category[:100], stores, segment[:50], notes[:500], source[:200], db.now()),
    )
    counts["added" if cur.rowcount else "duplicate"] += 1
    return cur.rowcount > 0


def import_csv(conn: sqlite3.Connection, path: Path, source: str = "") -> dict[str, int]:
    counts = {"added": 0, "duplicate": 0, "invalid": 0, "non_official": 0}
    with open_csv(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {(k or "").strip(): (v if isinstance(v, str) else "") for k, v in row.items()}
            stores_raw = unicodedata.normalize("NFKC", _pick(row, "stores"))
            stores = int(stores_raw) if stores_raw.isascii() and stores_raw.isdigit() else None
            add_company(
                conn,
                counts,
                name=_pick(row, "name"),
                url=_pick(row, "url"),
                area=_pick(row, "area"),
                category=_pick(row, "category"),
                stores=stores,
                segment=_pick(row, "segment"),
                notes=_pick(row, "notes"),
                source=source or path.name,
            )
    conn.commit()
    return counts

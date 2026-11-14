"""会社・店の公式サイトを数ページだけ見て、公表されているメールアドレスを集める。

守ること:
  - robots.txt に従う。禁止されたページは読まない
  - 1サイトあたり数ページまで。同じサイトへは間隔を空けて取りに行く
  - 公式サイトの中だけを見る（ポータルやSNSは「自己の公表」に当たらないので対象外）
  - 「営業メールお断り」の類を見つけたら、その会社には送らない
"""
from __future__ import annotations

import ipaddress
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urldefrag, urlsplit

from . import db
from .extract import (
    Page,
    classify_email,
    extract_emails,
    find_refusal,
    has_obfuscated_address,
    parse_html,
    site_host,
)

MAX_BYTES = 1_500_000
TIMEOUT = 15

# ここに載っているのは公式サイトではないので、巡回もアドレスの採用もしない
NON_OFFICIAL_HOSTS = (
    "instagram.com", "x.com", "twitter.com", "facebook.com", "threads.net", "threads.com",
    "tiktok.com", "youtube.com", "line.me", "lin.ee", "ameblo.jp", "note.com",
    "naramori.com", "hotpepper.jp", "tabelog.com", "gnavi.co.jp", "retty.me",
    "google.com", "google.co.jp", "goo.gl", "g.page", "itp.ne.jp", "ekiten.jp",
    "pokepara.jp", "caba2.net", "nightsbridge.com", "host2.jp", "baitoru.com",
    "townwork.net", "indeed.com", "linktr.ee", "lit.link", "peraichi.com",
)

# 連絡先が載っていそうなページ（優先）と、求人ページ（アドレスに注意フラグを付ける）
_PRIORITY = re.compile(
    r"(contact|inquiry|enquiry|toiawase|otoiawase|company|corporate|about|profile|outline|"
    r"gaiyo|gaiyou|access|info|tokusho|tokutei|law|legal|"
    r"お問い?合わ?せ|問合せ|会社概要|会社案内|会社情報|企業情報|運営会社|運営者|"
    r"特定商取引|店舗情報|アクセス|私たちについて)",
    re.I,
)
_RECRUIT = re.compile(r"(recruit|saiyo|saiyou|kyujin|career|job|求人|採用|リクルート|スタッフ募集|キャスト募集)", re.I)
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|mp4|mov|mp3|docx?|xlsx?|pptx?|css|js|ico|xml|json)$", re.I)


class FetchError(Exception):
    pass


def is_non_official(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in NON_OFFICIAL_HOSTS)


def _is_public_host(host: str) -> bool:
    """社内ネットワークや自分自身を指すURLを弾く（リストに紛れ込んだ不正なURL対策）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def _decode(body: bytes, header_charset: str | None) -> str:
    candidates: list[str] = []
    if header_charset:
        candidates.append(header_charset)
    m = re.search(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", body[:4096], re.I)
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp932", "euc_jp"]
    for enc in candidates:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


@dataclass
class SiteResult:
    host: str
    pages: list[tuple[str, str, str]] = field(default_factory=list)  # (url, 結果, 補足)
    emails: dict[str, tuple[str, list[str]]] = field(default_factory=dict)  # email -> (掲載URL, フラグ)
    refusal: tuple[str, str] | None = None  # (URL, 抜粋)
    obfuscated_url: str | None = None
    status: str = "no_email"
    note: str = ""


class Crawler:
    def __init__(
        self,
        user_agent: str = "AutoSalesBot/0.1",
        delay: float = 2.0,
        max_pages: int = 6,
        allow_private: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.delay = max(0.0, delay)
        self.max_pages = max(1, max_pages)
        self.allow_private = allow_private
        self._sleep = sleep
        self._last_fetch: dict[str, float] = {}
        crawler = self

        class _Redirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                crawler._check_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        self._opener = urllib.request.build_opener(_Redirects())

    # ---- 取得 ----
    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError(f"http/https 以外のURLです: {url}")
        if not self.allow_private and not _is_public_host(parts.hostname):
            raise FetchError(f"公開サイトではありません: {parts.hostname}")

    def _wait(self, host: str, extra: float = 0.0) -> None:
        gap = max(self.delay, extra)
        last = self._last_fetch.get(host)
        if last is not None:
            remaining = gap - (time.monotonic() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_fetch[host] = time.monotonic()

    def _open(self, url: str, crawl_delay: float = 0.0) -> tuple[str, int, str, bytes, str | None]:
        """(最終URL, ステータス, Content-Type, 本文, 文字コード) を返す。"""
        self._check_url(url)
        self._wait(urlsplit(url).hostname or "", crawl_delay)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5",
            "Accept-Language": "ja,en;q=0.5",
        })
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                body = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
                return (resp.geturl(), resp.status, resp.headers.get_content_type(),
                        body, resp.headers.get_content_charset())
        except urllib.error.HTTPError as e:
            e.close()
            return (url, e.code, "", b"", None)
        except FetchError:
            raise
        except Exception as e:  # タイムアウト・DNS・TLS など
            raise FetchError(f"{type(e).__name__}: {e}") from e

    def _robots(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        """robots.txt を読む。None は「このサイトは見てはいけない」。つながらなければ FetchError。"""
        rp = urllib.robotparser.RobotFileParser()
        _, status, _, body, charset = self._open(origin + "/robots.txt")
        if status in (401, 403) or status >= 500:
            return None
        if status >= 400:
            rp.parse([])  # robots.txt が無い = 制限なし
        else:
            rp.parse(_decode(body, charset).splitlines())
        return rp

    def _allowed(self, result: SiteResult, url: str) -> urllib.robotparser.RobotFileParser | None:
        """url を読んでよければ robots を返す。だめなら result に理由を書いて None。"""
        parts = urlsplit(url)
        try:
            robots = self._robots(f"{parts.scheme}://{parts.netloc}")
        except FetchError as e:
            result.status, result.note = "error", str(e)
            result.pages.append((url, "error", str(e)))
            return None
        if robots is None:
            result.status, result.note = "blocked", "robots.txt を確かめられないため、見ていません"
            return None
        if not robots.can_fetch(self.user_agent, url):
            result.status, result.note = "blocked", "robots.txt で禁止されています"
            result.pages.append((url, "blocked", "robots.txt"))
            return None
        return robots

    # ---- 1サイト分 ----
    def crawl_site(self, start_url: str) -> SiteResult:
        host = site_host(start_url)
        result = SiteResult(host=host)
        if is_non_official(host):
            result.status, result.note = "skipped", "ポータル・SNSのURLです。公式サイトのURLを入れてください"
            return result

        # 中身を読む前に robots.txt を確かめる
        robots = self._allowed(result, start_url)
        if robots is None:
            return result
        try:
            final_url, status, ctype, body, charset = self._open(start_url)
        except FetchError as e:
            result.status, result.note = "error", str(e)
            result.pages.append((start_url, "error", str(e)))
            return result

        final_host = site_host(final_url)
        if is_non_official(final_host):
            result.status, result.note = "skipped", f"公式サイトではない場所に転送されました: {final_host}"
            return result
        result.host = final_host
        if urlsplit(final_url).netloc != urlsplit(start_url).netloc:
            # 別のホストに転送された。転送先の robots.txt で確かめ直し、だめなら読んだ中身は使わない
            robots = self._allowed(result, final_url)
            if robots is None:
                return result
        crawl_delay = 0.0
        try:
            crawl_delay = float(robots.crawl_delay(self.user_agent) or 0)
        except (TypeError, ValueError):
            pass
        crawl_delay = min(crawl_delay, 30.0)

        queue: list[tuple[str, bool]] = []  # (url, 求人ページか)
        seen = {urldefrag(final_url)[0].rstrip("/")}
        fetched = 0
        current: tuple[str, int, str, bytes, str | None] | None = (final_url, status, ctype, body, charset)
        current_is_recruit = False

        while current is not None:
            url, status, ctype, body, charset = current
            fetched += 1
            if status >= 400:
                result.pages.append((url, "error", f"HTTP {status}"))
            elif ctype not in ("text/html", "application/xhtml+xml", "text/plain"):
                result.pages.append((url, "skipped", ctype))
            elif site_host(url) != result.host:
                result.pages.append((url, "skipped", "別サイトへの転送"))
            else:
                page = parse_html(_decode(body, charset), url)
                self._collect(result, page, current_is_recruit or bool(_RECRUIT.search(page.title)))
                if fetched == 1:
                    queue = self._pick_links(page, result.host, seen)
                result.pages.append((url, "ok", page.title[:80]))

            current = None
            while queue and fetched < self.max_pages:
                next_url, is_recruit = queue.pop(0)
                if not robots.can_fetch(self.user_agent, next_url):
                    result.pages.append((next_url, "blocked", "robots.txt"))
                    continue
                try:
                    current = self._open(next_url, crawl_delay)
                    current_is_recruit = is_recruit
                    break
                except FetchError as e:
                    fetched += 1
                    result.pages.append((next_url, "error", str(e)))

        if result.refusal:
            result.status = "refused"
            result.note = f"営業お断りの表示: {result.refusal[1]}"
        elif result.emails:
            result.status = "crawled"
        elif not any(r == "ok" for _, r, _ in result.pages):
            result.status, result.note = "error", "読めるページがありませんでした"
        else:
            result.status = "no_email"
            if result.obfuscated_url:
                result.note = f"アドレスが崩して書かれています（目で確認）: {result.obfuscated_url}"
        return result

    def _collect(self, result: SiteResult, page: Page, is_recruit: bool) -> None:
        if result.refusal is None:
            snippet = find_refusal(page)
            if snippet:
                result.refusal = (page.url, snippet)
        if result.obfuscated_url is None and has_obfuscated_address(page):
            result.obfuscated_url = page.url
        for email in extract_emails(page):
            flags = classify_email(email, result.host, page_is_recruit=is_recruit)
            known = result.emails.get(email)
            # 求人ページ以外でも見つかったら、求人フラグは外す
            if known is None or ("recruit" in known[1] and "recruit" not in flags):
                result.emails[email] = (page.url, flags)

    def _pick_links(self, page: Page, host: str, seen: set[str]) -> list[tuple[str, bool]]:
        priority: list[tuple[str, bool]] = []
        recruit: list[tuple[str, bool]] = []
        for href, text in page.links:
            url = urldefrag(href)[0]
            parts = urlsplit(url)
            key = url.rstrip("/")
            if parts.scheme not in ("http", "https") or site_host(url) != host:
                continue
            if key in seen or _SKIP_EXT.search(parts.path):
                continue
            label = f"{parts.path} {parts.query} {text}"
            if _PRIORITY.search(label):
                seen.add(key)
                priority.append((url, False))
            elif _RECRUIT.search(label):
                seen.add(key)
                recruit.append((url, True))
        # 連絡先ページを先に。求人ページは最後に1つだけ（応募専用アドレスの見分け用）
        return priority + recruit[:1]


def save_result(conn: sqlite3.Connection, company_id: int, result: SiteResult,
                refusal_checked: bool = False) -> int:
    """巡回の結果を保存する。新しく入った連絡先の数を返す。

    refusal_checked=True は、人が掲載ページを見て「営業お断りではない」と判断済みの会社。
    """
    ts = db.now()
    if result.status == "refused" and refusal_checked:
        result.status = "crawled" if result.emails else "no_email"
        result.note = f"検知した表示（人が確認済み）: {result.refusal[1] if result.refusal else ''}"
    for url, res, detail in result.pages:
        conn.execute(
            "INSERT INTO crawl_log(company_id, url, result, detail, fetched_at) VALUES (?,?,?,?,?)",
            (company_id, url, res, detail, ts),
        )
    if result.refusal:
        conn.execute(
            "INSERT INTO crawl_log(company_id, url, result, detail, fetched_at) VALUES (?,?,?,?,?)",
            (company_id, result.refusal[0], "refusal", result.refusal[1], ts),
        )
    added = 0
    if result.status == "crawled":
        for email, (source_url, flags) in result.emails.items():
            cur = conn.execute(
                "INSERT OR IGNORE INTO contacts(company_id, email, source_url, flags, status, found_at) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (company_id, email, source_url, ",".join(flags), ts),
            )
            added += cur.rowcount
    conn.execute(
        "UPDATE companies SET status=?, status_note=?, crawled_at=? WHERE id=?",
        (result.status, result.note, ts, company_id),
    )
    conn.commit()
    return added


def crawl_pending(
    conn: sqlite3.Connection,
    crawler: Crawler,
    limit: int | None = None,
    recrawl: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    where = "status IN ('new','error','no_email','blocked')" if recrawl else "status = 'new'"
    sql = f"SELECT id, name, site_url, refusal_checked FROM companies WHERE {where} ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        result = crawler.crawl_site(row["site_url"])
        added = save_result(conn, row["id"], result, refusal_checked=bool(row["refusal_checked"]))
        counts[result.status] = counts.get(result.status, 0) + 1
        counts["_contacts"] = counts.get("_contacts", 0) + added
        tail = f" アドレス{added}件" if added else (f" {result.note}" if result.note else "")
        log(f"[{result.status}] {row['name']} ({result.host}){tail}")
    return counts

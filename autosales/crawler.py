"""会社・店の公式サイトを数ページだけ見て、公表されているメールアドレスを集める。

守ること:
  - robots.txt に従う。禁止されたページは読まない（転送先も1つずつ確かめる）
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
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit

from . import db
from .extract import Page, classify_email, extract_contacts, find_refusals, parse_html, site_host

MAX_BYTES = 1_500_000
TIMEOUT = 15
MAX_REDIRECTS = 5
ROBOTS_NAME = "autosalesbot"  # robots.txt の User-agent 行と照らす名前

# ここに載っているのは公式サイトではないので、巡回もアドレスの採用もしない
NON_OFFICIAL_HOSTS = (
    "instagram.com", "x.com", "twitter.com", "facebook.com", "threads.net", "threads.com",
    "tiktok.com", "youtube.com", "youtu.be", "line.me", "lin.ee", "ameblo.jp", "note.com",
    "naramori.com", "hotpepper.jp", "tabelog.com", "gnavi.co.jp", "gorp.jp", "retty.me",
    "google.com", "google.co.jp", "goo.gl", "g.page", "business.site", "itp.ne.jp", "ekiten.jp",
    "pokepara.jp", "caba2.net", "nightsbridge.com", "host2.jp", "baitoru.com", "owst.jp",
    "townwork.net", "indeed.com", "linktr.ee", "lit.link", "peraichi.com", "wikipedia.org",
    "tripadvisor.jp", "tripadvisor.com", "yelp.com", "yelp.co.jp", "foursquare.com",
    "favy.jp", "hitosara.com", "ozmall.co.jp", "ikyu.com", "jalan.net", "rakuten.co.jp",
    "booking.com", "untappd.com", "yahoo.co.jp", "navitime.co.jp",
)

# 連絡先ページ → 会社情報ページ → 規約ページ の順に見る。求人ページは応募専用アドレスの見分け用
_CONTACT = re.compile(r"(contact|inquiry|enquiry|toiawase|otoiawase|お問い?合わ?せ|問合せ|問い合せ)", re.I)
_COMPANY = re.compile(
    r"(company|corporate|about|profile|outline|gaiyo|gaiyou|access|info|tokusho|tokutei|law|legal|"
    r"会社概要|会社案内|会社情報|企業情報|運営会社|運営者|特定商取引|店舗情報|アクセス|私たちについて)",
    re.I,
)
_POLICY = re.compile(r"(policy|privacy|terms|サイトポリシー|プライバシー|利用規約|ご利用にあたって|個人情報)", re.I)
_RECRUIT = re.compile(r"(recruit|saiyo|saiyou|kyujin|career|job|求人|採用|リクルート|スタッフ募集|キャスト募集)", re.I)
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|mp4|mov|mp3|docx?|xlsx?|pptx?|css|js|ico|xml|json)$", re.I)
_URL_SAFE = "/%:@!$&'()*+,;=~-._"


class FetchError(Exception):
    pass


def is_non_official(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in NON_OFFICIAL_HOSTS)


def _is_public_host(host: str) -> bool:
    """社内ネットワークや自分自身を指すURLを弾く（リストに紛れ込んだ不正なURL対策）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _is_public_ip(ip):
            return False
    return bool(infos)


def _is_public_ip(ip) -> bool:  # noqa: ANN001
    return bool(ip.is_global) and not (
        ip.is_reserved or getattr(ip, "is_site_local", False) or ip.is_multicast
    )


def _ascii_url(url: str) -> str:
    """日本語のパス（/お問い合わせ.html）や日本語ドメインを、送れる形に直す。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        netloc = host if host.isascii() else host.encode("idna").decode("ascii")
        if parts.port:
            netloc += f":{parts.port}"
    except (ValueError, UnicodeError) as e:
        raise FetchError(f"URLを読めません: {url}") from e
    return urlunsplit((parts.scheme, netloc, quote(parts.path, safe=_URL_SAFE),
                       quote(parts.query, safe=_URL_SAFE + "?"), ""))


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


class Robots:
    """robots.txt の読み取り。Python の版で結果が変わらないよう、自前で判定する。

    最も長く一致した規則を採り、同じ長さなら Allow を優先する。* と $ に対応。
    """

    def __init__(self, text: str) -> None:
        groups: list[tuple[list[str], list[tuple[bool, str]], float | None]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        delay: float | None = None
        in_rules = False
        for raw in text.lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, in_rules = [], [], None, False
                agents.append(value.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if value:
                    rules.append((key == "allow", value))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    delay = float(value)
                except ValueError:
                    pass
        if agents:
            groups.append((agents, rules, delay))
        named = [g for g in groups if any(a != "*" and a and a in ROBOTS_NAME for a in g[0])]
        chosen = named or [g for g in groups if "*" in g[0]]
        self.rules = [r for g in chosen for r in g[1]]
        delays = [g[2] for g in chosen if g[2] is not None]
        self.crawl_delay = min(max(delays), 30.0) if delays else 0.0

    @staticmethod
    def _norm(text: str) -> str:
        # %e3 と %E3 は同じもの。大文字にそろえてから比べる
        return re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), text)

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = ".*".join(re.escape(part) for part in body.split("*"))
        return re.match(regex + ("$" if anchored else ""), path) is not None

    def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        path = self._norm(path)
        best_len, allowed = -1, True
        for allow, pattern in self.rules:
            for candidate in {self._norm(pattern), self._norm(quote(pattern, safe=_URL_SAFE + "?"))}:
                if self._matches(candidate, path):
                    if len(pattern) > best_len or (len(pattern) == best_len and allow):
                        best_len, allowed = len(pattern), allow
        return allowed


@dataclass
class SiteResult:
    host: str
    pages: list[tuple[str, str, str]] = field(default_factory=list)  # (url, 結果, 補足)
    emails: dict[str, tuple[str, list[str]]] = field(default_factory=dict)  # email -> (掲載URL, フラグ)
    refusals: list[tuple[str, str]] = field(default_factory=list)  # (URL, 抜粋)
    obfuscated_url: str | None = None
    status: str = "no_email"
    note: str = ""

    @property
    def refusal(self) -> tuple[str, str] | None:
        return self.refusals[0] if self.refusals else None


class Crawler:
    def __init__(
        self,
        user_agent: str = "AutoSalesBot/0.1",
        delay: float = 2.0,
        max_pages: int = 8,
        allow_private: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.delay = max(0.0, delay)
        self.max_pages = max(1, max_pages)
        self.allow_private = allow_private
        self._sleep = sleep
        self._last_fetch: dict[str, float] = {}

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                return None  # 転送は自動で追わない。1つずつ確かめてから取りに行く

        self._opener = urllib.request.build_opener(_NoRedirect())

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

    def _open(
        self,
        url: str,
        robots: Robots | None = None,
        stay_on: str | None = None,
    ) -> tuple[str, int, str, bytes, str | None, bool]:
        """(最終URL, ステータス, Content-Type, 本文, 文字コード, 途中で切れたか) を返す。

        転送は最大 MAX_REDIRECTS 回まで手で追う。1回ごとに、同じサイトの中か（stay_on）、
        robots.txt で許されているか（robots）、公開サイトかを確かめる。
        """
        delay = robots.crawl_delay if robots else 0.0
        for _ in range(MAX_REDIRECTS + 1):
            url = _ascii_url(url)
            self._check_url(url)
            if stay_on is not None and site_host(url) != stay_on:
                raise FetchError(f"別のサイトへの転送です: {site_host(url)}")
            if robots is not None and not robots.can_fetch(url):
                raise FetchError("robots.txt で禁止されています")
            self._wait(urlsplit(url).hostname or "", delay)
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5",
                "Accept-Language": "ja,en;q=0.5",
            })
            try:
                with self._opener.open(req, timeout=TIMEOUT) as resp:
                    self._check_peer(resp)
                    body = resp.read(MAX_BYTES + 1)
                    return (url, resp.status, resp.headers.get_content_type(),
                            body[:MAX_BYTES], resp.headers.get_content_charset(), len(body) > MAX_BYTES)
            except urllib.error.HTTPError as e:
                location = e.headers.get("Location") if e.headers else None
                code = e.code
                e.close()
                if code in (301, 302, 303, 307, 308) and location:
                    url = urljoin(url, location)
                    continue
                return (url, code, "", b"", None, False)
            except FetchError:
                raise
            except Exception as e:  # タイムアウト・DNS・TLS など
                raise FetchError(f"{type(e).__name__}: {e}") from e
        raise FetchError("転送が多すぎます")

    def _check_peer(self, resp) -> None:  # noqa: ANN001
        """実際につながった先を確かめる（名前の引き直しで内部のアドレスに向けられた場合の備え）。"""
        if self.allow_private:
            return
        try:
            peer = resp.fp.raw._sock.getpeername()[0]
        except Exception:
            return  # 接続先を取り出せない実装では、事前の確認だけに頼る
        if not _is_public_ip(ipaddress.ip_address(peer.split("%")[0])):
            raise FetchError(f"公開サイトではありません: {peer}")

    def _robots(self, origin: str, host: str) -> Robots | None:
        """robots.txt を読む。None は「このサイトは見てはいけない」。つながらなければ FetchError。"""
        _, status, _, body, charset, _ = self._open(origin + "/robots.txt", stay_on=host)
        if status in (401, 403) or status >= 500:
            return None
        if status >= 400:
            return Robots("")  # robots.txt が無い = 制限なし
        return Robots(_decode(body, charset))

    # ---- 1サイト分 ----
    def crawl_site(self, start_url: str) -> SiteResult:
        host = site_host(start_url)
        result = SiteResult(host=host)
        if not host or is_non_official(host):
            result.status, result.note = "skipped", "ポータル・SNSのURLです。公式サイトのURLを入れてください"
            return result

        # 中身を読む前に robots.txt を確かめる
        try:
            parts = urlsplit(_ascii_url(start_url))
            robots = self._robots(f"{parts.scheme}://{parts.netloc}", host)
        except FetchError as e:
            result.status, result.note = "error", str(e)
            result.pages.append((start_url, "error", str(e)))
            return result
        if robots is None:
            result.status, result.note = "blocked", "robots.txt を確かめられないため、見ていません"
            return result

        try:
            first = self._open(start_url, robots=robots, stay_on=host)
        except FetchError as e:
            text = str(e)
            if "robots.txt" in text:
                result.status, result.note = "blocked", "robots.txt で禁止されています"
                result.pages.append((start_url, "blocked", "robots.txt"))
            elif "別のサイトへの転送" in text:
                # 転送先が別会社のこともある。人が確かめて、正しければURLを直して入れ直す
                result.status, result.note = "skipped", f"{text}（人が確認してください）"
            else:
                result.status, result.note = "error", text
                result.pages.append((start_url, "error", text))
            return result

        queue: list[tuple[str, str]] = []  # (url, 種類)
        contact_unread = False
        truncated = False
        fetched = 0
        current: tuple[str, int, str, bytes, str | None, bool] | None = first
        kind = "top"
        first_parts = urlsplit(first[0])
        seen = {(first_parts.path.rstrip("/") + "?" + first_parts.query).rstrip("?")}

        while current is not None:
            url, status, ctype, body, charset, cut = current
            fetched += 1
            ok = False
            if status >= 400:
                result.pages.append((url, "error", f"HTTP {status}"))
            elif ctype not in ("text/html", "application/xhtml+xml", "text/plain"):
                result.pages.append((url, "skipped", ctype))
            else:
                ok = True
                truncated = truncated or cut
                page = parse_html(_decode(body, charset), url)
                self._collect(result, page, kind == "recruit" or bool(_RECRUIT.search(page.title)))
                if kind == "top":
                    queue = self._pick_links(page, host, seen)
                result.pages.append((url, "ok", page.title[:80]))
            if kind == "contact" and not ok:
                contact_unread = True

            current = None
            while queue and fetched < self.max_pages:
                next_url, kind = queue.pop(0)
                try:
                    current = self._open(next_url, robots=robots, stay_on=host)
                    break
                except FetchError as e:
                    fetched += 1
                    blocked = "robots.txt" in str(e)
                    result.pages.append((next_url, "blocked" if blocked else "error", str(e)))
                    if kind == "contact":
                        contact_unread = True

        # 上限に達して読めなかったお問い合わせページが残っている
        if any(k == "contact" for _, k in queue):
            contact_unread = True

        # 読み切れていないものは、人の確認なしに承認されないよう印を付ける
        extra = (["contact_unread"] if contact_unread else []) + (["page_truncated"] if truncated else [])
        if extra:
            result.emails = {e: (src, flags + extra) for e, (src, flags) in result.emails.items()}

        if result.refusals:
            result.status = "refused"
            result.note = ("営業お断りの表示: " + " ／ ".join(s for _, s in result.refusals))[:400]
        elif result.emails:
            result.status = "crawled"
        elif not any(r == "ok" for _, r, _ in result.pages):
            result.status, result.note = "error", "読めるページがありませんでした"
        else:
            result.status = "no_email"
            if result.obfuscated_url:
                result.note = f"アドレスが崩して書かれています（自動では拾いません）: {result.obfuscated_url}"
        return result

    def _collect(self, result: SiteResult, page: Page, is_recruit: bool) -> None:
        for snippet in find_refusals(page):
            if all(snippet != s for _, s in result.refusals):
                result.refusals.append((page.url, snippet))
        if result.obfuscated_url is None and page.obfuscated:
            result.obfuscated_url = page.url
        for email, extract_flags in extract_contacts(page):
            flags = classify_email(email, result.host, page_is_recruit=is_recruit) + extract_flags
            known = result.emails.get(email)
            # 求人ページ以外でも見つかったら、求人フラグは外す
            if known is None or ("recruit" in known[1] and "recruit" not in flags):
                result.emails[email] = (page.url, flags)

    def _pick_links(self, page: Page, host: str, seen: set[str]) -> list[tuple[str, str]]:
        buckets: dict[str, list[tuple[str, str]]] = {"contact": [], "company": [], "policy": [], "recruit": []}
        for href, text in page.links:
            url = urldefrag(href)[0]
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            # http と https、www の有無が違うだけの同じページを2回読まない
            key = (parts.path.rstrip("/") + "?" + parts.query).rstrip("?")
            if parts.scheme not in ("http", "https") or site_host(url) != host:
                continue
            if key in seen or _SKIP_EXT.search(parts.path):
                continue
            label = f"{unquote(parts.path)} {unquote(parts.query)} {text}"
            for kind, pattern in (("contact", _CONTACT), ("recruit", _RECRUIT),
                                  ("company", _COMPANY), ("policy", _POLICY)):
                if pattern.search(label):
                    seen.add(key)
                    buckets[kind].append((url, kind))
                    break
        # お断りの表示はお問い合わせページに書かれることが多いので、必ず先に読む
        # 4本目以降のお問い合わせページは、余裕があれば読む（読めなければ contact_unread の印が付く）
        return (buckets["contact"][:3] + buckets["company"][:3] + buckets["policy"][:2]
                + buckets["recruit"][:1] + buckets["contact"][3:])


def save_result(conn: sqlite3.Connection, company_id: int, result: SiteResult,
                confirmed: str = "") -> int:
    """巡回の結果を保存する。新しく入った連絡先の数を返す。

    confirmed は、人が見て「営業お断りではない」と判断した検知文（改行区切り）。
    今回の検知がすべてその中にあるときだけ、除外を解く。1件でも新しい検知があれば除外のまま。
    """
    ts = db.now()
    confirmed_set = {line for line in confirmed.split("\n") if line}
    if result.status == "refused" and confirmed_set and all(s in confirmed_set for _, s in result.refusals):
        result.status = "crawled" if result.emails else "no_email"
        result.note = "検知した表示は人が確認済み"

    for url, res, detail in result.pages:
        conn.execute(
            "INSERT INTO crawl_log(company_id, url, result, detail, fetched_at) VALUES (?,?,?,?,?)",
            (company_id, url, res, detail, ts),
        )
    for url, snippet in result.refusals:
        conn.execute(
            "INSERT INTO crawl_log(company_id, url, result, detail, fetched_at) VALUES (?,?,?,?,?)",
            (company_id, url, "refusal", snippet, ts),
        )

    # 先に会社の状態を書く（下の「送ってよいか」の判定が、この会社の新しい状態を見るため）
    conn.execute(
        "UPDATE companies SET status=?, status_note=?, crawled_at=? WHERE id=?",
        (result.status, result.note, ts, company_id),
    )
    if result.status in ("refused", "crawled", "no_email"):
        # サイトに載っていたアドレスを控える。お断りのサイトのアドレスは、別のサイトで見つかっても送らない
        conn.execute("DELETE FROM site_emails WHERE company_id=?", (company_id,))
        conn.executemany("INSERT OR IGNORE INTO site_emails(company_id, email) VALUES (?,?)",
                         [(company_id, email) for email in result.emails])
    added = 0
    if result.status == "refused":
        for email in result.emails:
            conn.execute(
                "UPDATE contacts SET status='rejected', decided_at=? "
                "WHERE email=? AND status IN ('pending','approved')", (ts, email),
            )
    if result.status == "crawled":
        for email, (source_url, flags) in result.emails.items():
            if db.is_suppressed(conn, email):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO contacts(company_id, email, source_url, flags, status, found_at) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (company_id, email, source_url, ",".join(flags), ts),
            )
            added += cur.rowcount
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
    sql = f"SELECT id, name, site_url, refusal_confirmed FROM companies WHERE {where} ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        try:
            result = crawler.crawl_site(row["site_url"])
        except Exception as e:  # 1社の不備で全体を止めない
            result = SiteResult(host=site_host(row["site_url"]), status="error",
                                note=f"{type(e).__name__}: {e}")
        added = save_result(conn, row["id"], result, confirmed=row["refusal_confirmed"])
        counts[result.status] = counts.get(result.status, 0) + 1
        counts["_contacts"] = counts.get("_contacts", 0) + added
        tail = f" アドレス{added}件" if added else (f" {result.note}" if result.note else "")
        log(f"[{result.status}] {row['name']} ({result.host}){tail}")
    return counts

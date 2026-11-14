"""HTML からメールアドレス・リンク・「営業お断り」の表示を取り出す。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")

# 画像名（logo@2x.png など）や、明らかに店のものではないアドレスを捨てる
_ASSET_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "ico", "woff", "woff2"}
_JUNK_DOMAINS = {
    "example.com", "example.co.jp", "example.jp", "example.org", "example.net",
    "sentry.io", "wixpress.com", "sentry.wixpress.com", "domain.com", "email.com",
    "xxx.com", "xxxx.com", "sample.com", "sample.co.jp", "yourdomain.com",
}
_JUNK_LOCALS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "abuse"}

# 求人応募専用とみなす宛先（営業メールは送らない）
_RECRUIT_LOCALS = re.compile(r"(recruit|saiyo|saiyou|kyujin|job|jobs|entry|career|hr|baito|arbeit)", re.I)
_PRIVACY_LOCALS = re.compile(r"(privacy|kojinjoho|dpo|security|legal)", re.I)

FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.co.jp", "yahoo.com", "icloud.com", "me.com", "mac.com",
    "outlook.com", "outlook.jp", "hotmail.com", "hotmail.co.jp", "live.jp", "live.com",
    "docomo.ne.jp", "ezweb.ne.jp", "au.com", "softbank.ne.jp", "i.softbank.jp",
    "ymobile.ne.jp", "rakuten.jp", "nifty.com", "ybb.ne.jp", "ocn.ne.jp", "aol.com",
}

# 「営業メールお断り」の類。語と断り表現が近くにあれば拒否表示とみなす（取りこぼすより広めに拾う）
_REFUSAL_TOPIC = r"(?:営業|セールス|売り?込み|勧誘|広告|宣伝|販売促進|販促|特定電子メール|迷惑メール|ＤＭ|DM|ダイレクトメール)"
_REFUSAL_VERB = r"(?:お断り|おことわり|断り|ご遠慮|遠慮|禁止|禁じ|拒否|お控え|控えて|送信しないで|送らないで|受け付けて(?:おり|い)ません|対応(?:いた)?しかねます|返信(?:いた)?しません)"
REFUSAL_RE = re.compile(
    _REFUSAL_TOPIC + r"[^。\n]{0,40}?" + _REFUSAL_VERB + r"|" + _REFUSAL_VERB + r"[^。\n]{0,20}?" + _REFUSAL_TOPIC
)

# アドレスをわざと崩して書いている形（機械で拾われたくない意思表示なので、自動では拾わない）
OBFUSCATED_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+\s*(?:\[at\]|\(at\)|\{at\}|\[@\]|★|☆|●|■|◆|アットマーク|\(あっと\)|＠を半角)\s*[A-Za-z0-9\-]+\.",
    re.I,
)

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


@dataclass
class Page:
    url: str
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (絶対URL, リンクの文字)
    mailtos: list[str] = field(default_factory=list)
    title: str = ""


class _Parser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.chunks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.mailtos: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        attr = {k: (v or "") for k, v in attrs}
        if tag == "base" and attr.get("href"):
            self.base_url = urljoin(self.base_url, attr["href"])
        if tag in ("a", "area"):
            href = attr.get("href", "").strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href)
            elif href and not href.lower().startswith(("javascript:", "tel:", "#", "data:")):
                self._link_href = urljoin(self.base_url, href)
                self._link_text = [attr.get("title", ""), attr.get("aria-label", "")]
        if tag == "img" and self._link_href is not None:
            self._link_text.append(attr.get("alt", ""))
        if tag in ("br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "dt", "dd", "section"):
            self.chunks.append("\n")
        else:
            # <span>MAIL</span><a>info@…</a> が「MAILinfo@…」とつながらないよう、タグの境目で区切る
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link_href is not None:
            self.links.append((self._link_href, " ".join(t for t in self._link_text if t).strip()))
            self._link_href = None
            self._link_text = []
        if tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "dd", "section"):
            self.chunks.append("\n")
        else:
            self.chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._link_href is not None:
            self._link_text.append(data)
        self.chunks.append(data)


def parse_html(html: str, url: str) -> Page:
    parser = _Parser(url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # 壊れたHTMLでも、そこまでに読めた分は使う
    text = unicodedata.normalize("NFKC", "".join(parser.chunks))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return Page(
        url=url,
        text=text.strip(),
        links=parser.links,
        mailtos=parser.mailtos,
        title=" ".join("".join(parser.title_parts).split()),
    )


def _clean_email(raw: str) -> str | None:
    email = unicodedata.normalize("NFKC", raw).strip().strip(".,;:<>()[]\"'").lower()
    m = EMAIL_RE.fullmatch(email)
    if not m:
        return None
    local, _, domain = email.rpartition("@")
    tld = domain.rsplit(".", 1)[-1]
    if tld in _ASSET_TLDS or not tld.isalpha() or len(tld) < 2:
        return None
    if domain in _JUNK_DOMAINS or local in _JUNK_LOCALS:
        return None
    if len(local) > 64 or len(email) > 254 or ".." in email:
        return None
    return email


def extract_emails(page: Page) -> list[str]:
    """ページに書かれているアドレスを、出てきた順に重複なしで返す。"""
    found: list[str] = []
    for href in page.mailtos:
        target = unquote(href[len("mailto:"):].split("?", 1)[0])
        for part in target.split(","):
            email = _clean_email(part)
            if email and email not in found:
                found.append(email)
    for match in EMAIL_RE.finditer(page.text):
        email = _clean_email(match.group(0))
        if email and email not in found:
            found.append(email)
    return found


def find_refusal(page: Page) -> str | None:
    """「営業メールお断り」の類が書かれていれば、その前後の文を返す。"""
    m = REFUSAL_RE.search(page.text)
    if not m:
        return None
    start = max(0, m.start() - 30)
    end = min(len(page.text), m.end() + 30)
    return " ".join(page.text[start:end].split())


def has_obfuscated_address(page: Page) -> bool:
    return OBFUSCATED_RE.search(page.text) is not None


def site_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def classify_email(email: str, host: str, page_is_recruit: bool = False) -> list[str]:
    """人が確認するときの注意フラグを返す。空なら問題なし。"""
    local, _, domain = email.rpartition("@")
    flags: list[str] = []
    if _RECRUIT_LOCALS.search(local) or page_is_recruit:
        flags.append("recruit")
    if _PRIVACY_LOCALS.search(local):
        flags.append("privacy")
    if domain in FREE_MAIL_DOMAINS:
        flags.append("free_mail")
    elif not (domain == host or domain.endswith("." + host) or host.endswith("." + domain)):
        flags.append("offsite_domain")
    return flags

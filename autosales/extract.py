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
    "plala.or.jp", "biglobe.ne.jp", "so-net.ne.jp", "nifty.ne.jp", "dion.ne.jp",
    "eonet.ne.jp", "kcn.ne.jp", "zaq.ne.jp", "bbtec.net", "infoweb.ne.jp", "mineo.jp",
    "uqmobile.jp", "protonmail.com", "proton.me", "excite.co.jp", "mail.com", "gmx.com",
}

# 「営業メールお断り」の類。話題の語と断りの表現が近くにあれば拒否表示とみなす。
# 見逃すより広めに拾う（誤検知は unrefuse で人が解除できる）。
_REFUSAL_TOPIC = (
    r"(?:営業(?!時間|日|中|所|開始|終了|再開|許可)|セールス|売り?込み?|勧誘|広告|宣伝|販売促進|販促|"
    r"特定電子メール|迷惑メール(?!対策|フィルタ|設定)|(?<![A-Za-z])DM(?![A-Za-z])|ダイレクトメール|業者)"
)
_REFUSAL_VERB = (
    r"(?:お?断わ?り|おことわり|ご遠慮|遠慮|禁止|禁じ|(?<!受信)拒否|お控え|控えて|送信しないで|送らないで|"
    r"受け?付け?(?:て|して)?(?:おり|い)?ません|受け?付け?(?:いたし|致し|でき)?かね|"
    r"(?:対応|返信|返答|回答|返事|お答え|お応え)(?:は|を)?(?:(?:いた|致)?し(?:かね|ておりません|ていません|ません)|でき(?:ません|かね|ない))|"
    r"応じ(?:かね|られません|ません|ておりません)|"
    r"お受け(?:して(?:おり|い)ません|しかね|でき(?:ません|かねます)|(?:いた|致)し(?:かねます|ません|ておりません))|"
    r"不要|(?<![A-Za-z])NG(?![A-Za-z])|差し控え|ご容赦|ご勘弁|おやめ|お止め)"
)
# 文の途中の改行（<br> や見出しと本文の分かれ目）を1つまでまたぐ
REFUSAL_RE = re.compile(
    _REFUSAL_TOPIC + r"[^。\n]{0,40}?\n?[^。\n]{0,15}?" + _REFUSAL_VERB
    + r"|" + _REFUSAL_VERB + r"[^。\n]{0,20}?\n?[^。\n]{0,10}?" + _REFUSAL_TOPIC
)

# アドレスをわざと崩して書いている形（機械で拾われたくない意思表示なので、自動では拾わない）。
# 記号（★●など）は行頭の飾りにも使われるので、前後に空白がなくアドレスの形になっているときだけ。
OBFUSCATED_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+(?:\s*(?:\[at\]|\(at\)|\{at\}|\[@\]|\(a\)|【at】|_at_|\(アット\)|アットマーク|\(あっと\))\s*"
    r"|[★☆●■◆*])[A-Za-z0-9\-]+\.[A-Za-z]",
    re.I,
)
# 生のHTMLで見る崩し方: @ を文字参照で書く／全角＠にして「半角に直して」と断る
_AN = r"(?:[A-Za-z0-9]|&#x?[0-9A-Fa-f]{2,4};)"
_CHARREF_AT_RE = re.compile(_AN + r"(?:&#0*64;|&#x0*40;|&commat;)" + _AN, re.I)
_ZEN_ADDR = r"[0-9A-Za-zＡ-Ｚａ-ｚ０-９._\-]＠[0-9A-Za-zＡ-Ｚａ-ｚ０-９\-]+[.．]"
_NOTE = r"(?:半角|全角|小文字|大文字|に変えて|に直し|に変換|に変更|に置き換え|に書き換え|スパム対策|スパム防止|迷惑メール対策|迷惑メール防止)"
_ZENKAKU_NOTE_RE = re.compile(_ZEN_ADDR + r"[^<]{0,120}?" + _NOTE + r"|" + _NOTE + r"[^<]{0,120}?" + _ZEN_ADDR)
# 「MAIL.info@…」「TEL:0742-12-3456info@…」のように、見出しや電話番号がくっついた形
_GLUED_LABEL_RE = re.compile(r"^(?:e-?mail|mail|tel|fax)[._:\-]+(?=[a-z0-9])", re.I)
_GLUED_DIGITS_RE = re.compile(r"^[0-9\-]{9,}(?=[a-z])")
_JOIN = "\x00"  # タグをまたいでつないだ位置の目印

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "dt", "dd", "section", "ul", "ol", "table"}


@dataclass
class Page:
    url: str
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (絶対URL, リンクの文字)
    mailtos: list[str] = field(default_factory=list)
    title: str = ""
    obfuscated: bool = False  # アドレスを崩して書いているページ
    text_split: str = ""  # タグの境目をすべて区切った本文（タグをまたいだアドレスの見分け用）


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
        self._gap = False  # 直前にタグの境目があった

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        attr = {k: (v or "") for k, v in attrs}
        if tag == "base" and attr.get("href"):
            try:
                self.base_url = urljoin(self.base_url, attr["href"])
            except ValueError:
                pass
        if tag in ("a", "area"):
            href = attr.get("href", "").strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href)
            elif href and not href.lower().startswith(("javascript:", "tel:", "#", "data:")):
                try:
                    self._link_href = urljoin(self.base_url, href)
                    self._link_text = [attr.get("title", ""), attr.get("aria-label", "")]
                except ValueError:
                    self._link_href = None  # 壊れた href。そのリンクだけ捨てる
        if tag == "img" and attr.get("alt"):
            # 画像の代替文字も本文として読む（「営業メールお断り」が画像のことがある）
            if self._link_href is not None:
                self._link_text.append(attr["alt"])
            if not self._skip_depth:
                self.chunks.append(" " + attr["alt"] + " ")
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")
            self._gap = False
        else:
            self._gap = True

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
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")
            self._gap = False
        else:
            self._gap = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._link_href is not None:
            self._link_text.append(data)
        if self._gap and data:
            # <span>MAIL</span><a>info@…</a> が「MAILinfo@…」とつながらないよう、タグの境目で区切る。
            # ただし <b>sato</b>.taro@… や info@<wbr>shop.jp のように、アドレスの途中にタグがある形は区切らない。
            prev = self.chunks[-1][-1:] if self.chunks and self.chunks[-1] else ""
            inside_address = (prev.isascii() and prev.isalnum() and data[0] in ".@") or prev == "@"
            self.chunks.append(_JOIN if inside_address else " ")
        self._gap = False
        self.chunks.append(data)


def parse_html(html: str, url: str) -> Page:
    parser = _Parser(url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # 壊れたHTMLでも、そこまでに読めた分は使う
    marked = "".join(parser.chunks)
    raw_text = marked.replace(_JOIN, "")

    def tidy(value: str) -> str:
        value = unicodedata.normalize("NFKC", value)
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        return re.sub(r" ?\n[ \n]*", "\n", value).strip()

    text = tidy(raw_text)
    obfuscated = bool(
        _CHARREF_AT_RE.search(html) or _ZENKAKU_NOTE_RE.search(raw_text) or OBFUSCATED_RE.search(text)
    )
    return Page(
        url=url,
        text=text,
        text_split=tidy(marked.replace(_JOIN, " ")),
        links=parser.links,
        mailtos=parser.mailtos,
        title=" ".join("".join(parser.title_parts).split()),
        obfuscated=obfuscated,
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


def extract_contacts(page: Page) -> list[tuple[str, list[str]]]:
    """ページに書かれているアドレスを、出てきた順に重複なしで返す。 [(アドレス, 注意フラグ)]

    アドレスを崩して書いているページからは1件も採らない。
    """
    if page.obfuscated:
        return []
    found: dict[str, list[str]] = {}
    for href in page.mailtos:
        target = unquote(href[len("mailto:"):].split("?", 1)[0])
        for part in target.split(","):
            email = _clean_email(part)
            if email:
                found.setdefault(email, [])
    plain = {m.group(0).lower() for m in EMAIL_RE.finditer(page.text_split or page.text)}
    for match in EMAIL_RE.finditer(page.text):
        written = match.group(0)
        email = _clean_email(written)
        if not email:
            continue
        flags: list[str] = []
        if written.lower() not in plain:
            # <b>sato</b>.taro@… のようにタグをまたいでつないだ形。隠し文字を挟む収集よけのこともある
            flags.append("tag_joined")
        local, _, domain = email.rpartition("@")
        label = _GLUED_LABEL_RE.match(local)
        digits = _GLUED_DIGITS_RE.match(local)
        if digits:
            # 「TEL:0742-12-3456info@…」のように、電話番号がくっついた形
            email = _clean_email(local[digits.end():] + "@" + domain) or email
            flags.append("label_glued")
        elif label:
            flags.append("label_glued")
            if written[: label.end()] != written[: label.end()].lower():
                # 「MAIL.info@…」のように見出しが大文字なら、見出しを落とす。
                # 全部小文字（mail.info@…）は本当のアドレスかもしれないので、書かれたとおりに残す
                email = _clean_email(local[label.end():] + "@" + domain) or email
        if email not in found:
            found[email] = flags
    return list(found.items())


def extract_emails(page: Page) -> list[str]:
    return [email for email, _ in extract_contacts(page)]


def find_refusals(page: Page) -> list[str]:
    """「営業メールお断り」の類が書かれていれば、その前後の文をすべて返す。"""
    out: list[str] = []
    for m in REFUSAL_RE.finditer(page.text):
        start = max(0, m.start() - 30)
        end = min(len(page.text), m.end() + 30)
        snippet = " ".join(page.text[start:end].split())
        if snippet not in out:
            out.append(snippet)
    return out


def find_refusal(page: Page) -> str | None:
    found = find_refusals(page)
    return found[0] if found else None


def has_obfuscated_address(page: Page) -> bool:
    return page.obfuscated


def site_host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if not host.isascii():
            host = host.encode("idna").decode("ascii")  # 日本語ドメインは xn-- の形にそろえる
    except (ValueError, UnicodeError):
        return ""
    return host[4:] if host.startswith("www.") else host


def is_free_mail(domain: str) -> bool:
    """フリーメールやプロバイダのアドレスか（amber.plala.or.jp のようなサブドメイン付きも含む）。"""
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in FREE_MAIL_DOMAINS)


def classify_email(email: str, host: str, page_is_recruit: bool = False) -> list[str]:
    """人が確認するときの注意フラグを返す。空なら問題なし。"""
    local, _, domain = email.rpartition("@")
    flags: list[str] = []
    if _RECRUIT_LOCALS.search(local) or page_is_recruit:
        flags.append("recruit")
    if _PRIVACY_LOCALS.search(local):
        flags.append("privacy")
    if is_free_mail(domain):
        flags.append("free_mail")
    elif not (domain == host or domain.endswith("." + host) or host.endswith("." + domain)):
        flags.append("offsite_domain")
    return flags

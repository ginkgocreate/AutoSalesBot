"""お問い合わせフォームを見つけて、何をどの欄に入れるかを決める。

ここでは読むだけで、送信はしない（送信は formsend.py）。決めること:
  - そのフォームは問い合わせ用か（検索・ログイン・コメント欄は対象外。予約用・求人用・資料請求用には送らない）
  - 自動で送れるか（auto）、人がブラウザで送るか（manual）、送らないか（skip）
  - 自動で送る場合、どの欄に何を入れるか

迷ったら人に回す。自動にするのは、問い合わせ用だと分かり、必須の欄の意味がすべて分かり、
CAPTCHA や迷惑判定の仕組みが見当たらないフォームだけ。そういう仕組みは回避しない。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from .extract import site_host

_VOID = {"input", "br", "img", "hr", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
_SKIP_TAGS = {"script", "style", "noscript", "template"}
_ROW_HEADS = {"th", "dt", "legend"}
_PAGE_HEADS = {"h1", "h2", "h3", "h4"}
_TEXT_TYPES = {"text", "email", "tel", "url", "number", "search", ""}
_BUTTON_TYPES = {"submit", "button", "image", "reset"}

# 外部のフォームサービス（公式サイトの外にあるので、人がブラウザで送る）
FORM_SERVICE_HOSTS = (
    "forms.gle",
    "docs.google.com",
    "form.run",
    "formrun.com",
    "smp.ne.jp",
    "formzu.net",
    "tayori.com",
    "hsforms.com",
    "hubspot.com",
    "typeform.com",
    "form-mailer.jp",
    "secure-link.jp",
    "formok.com",
    "jotform.com",
    "forms.office.com",
    "kintoneapp.com",
    "satori.marketing",
    "form.k3r.jp",
    "qooker.jp",
    "bownow.jp",
)

# 見出しの文字から、欄の意味を決める
_TEXT_ROLES = [
    ("email", re.compile(r"e-?mail|(?<![a-z])mail(?![a-z])|メール|メアド")),
    ("fax", re.compile(r"(?<![a-z])fax(?![a-z])|ファックス|ファクス")),
    ("tel", re.compile(r"(?<![a-z])tel(?![a-z])|phone|電話|携帯|でんわ")),
    ("zip", re.compile(r"(?<![a-z])zip|postal|postcode|郵便|〒")),
    ("address", re.compile(r"address|(?<![a-z])addr(?![a-z])|住所|所在地|番地|市区町村|建物|都道府県")),
    ("kana", re.compile(r"kana|furi|(?<![a-z])ruby(?![a-z])|yomi|フリガナ|ふりがな|カナ|よみがな|ヨミ")),
    (
        "company",
        re.compile(
            r"company|(?<![a-z])corp|organi[sz]ation|会社|貴社|御社|法人|屋号|店舗名|店名|団体|組織|勤務先|企業|社名"
        ),
    ),
    ("dept", re.compile(r"部署|役職|所属|department|division|position")),
    (
        "name",
        re.compile(
            r"(?<![a-z])name(?![a-z])|氏名|名前|なまえ|担当者|代表者|ご芳名|(?<![a-z])(?:sei|mei)(?![a-z])|^姓$|^名$"
        ),
    ),
    ("subject", re.compile(r"subject|(?<![a-z])title(?![a-z])|件名|題名|タイトル|表題")),
    ("url", re.compile(r"(?<![a-z])url(?![a-z])|website|homepage|ホームページ|サイトの?(?:url|アドレス)")),
]
# name 属性は、区切った語の単位で照らす（hotel の tel、mailform の mail に当てない）
_NAME_ROLES = [
    ("email", re.compile(r"\b(?:e?mail|mailaddress|mailaddr|emailaddress)\b")),
    ("fax", re.compile(r"\bfax\b")),
    ("tel", re.compile(r"\b(?:tel|telephone|phone|mobile|denwa)\b")),
    ("zip", re.compile(r"\b(?:zip|zipcode|postal|postcode|yubin)\b")),
    ("address", re.compile(r"\b(?:address|addr|jusho|pref|prefecture)\b")),
    ("kana", re.compile(r"\b(?:kana|furigana|furi|yomi|ruby)\b")),
    ("company", re.compile(r"\b(?:company|corp|corporation|organization|kaisha)\b")),
    ("name", re.compile(r"\b(?:name|yourname|fullname|username|namae|shimei|sei|mei)\b")),
    ("subject", re.compile(r"\b(?:subject|title|kenmei)\b")),
    ("url", re.compile(r"\b(?:url|website|homepage)\b")),
]
_BODY = re.compile(r"message|inquiry|enquiry|content|body|comment|detail|内容|本文|メッセージ|質問|相談|意見|用件|要望")
_CATEGORY = re.compile(
    r"種別|種類|項目|区分|カテゴリ|category|(?<![a-z])(?:type|kind)(?![a-z])|subject|件名|お問い?合わ?せ|"
    r"ご?用件|目的|内容|content|naiyou?|inquiry|toiawase"
)
_METHOD = re.compile(r"連絡方法|返信方法|ご希望の連絡|連絡手段|ご連絡方法")
_PREF = re.compile(r"(?<![a-z])pref|都道府県")
_HONEYPOT = re.compile(
    r"honeypot|honey|(?<![a-z])hp[-_]|[-_]hp(?![a-z])|ohnohoney|bot[-_]?trap|nospam|antispam|"
    r"空欄のまま|入力しないで|何も入力|記入しないで|leave.{0,20}(?:blank|empty)|do not fill|スパム対策"
)
_HIDDEN_CLASSES = {
    "hidden",
    "hide",
    "d-none",
    "sr-only",
    "visually-hidden",
    "screen-reader-text",
    "invisible",
    "offscreen",
    "is-hidden",
    "u-hidden",
    "display-none",
    "gform_validation_container",
}
_HIRAGANA_HINT = re.compile(r"ひらがな|ふりがな|よみがな")
_REQUIRED = re.compile(r"必須|required|\*|※|＊")
_OPTIONAL = re.compile(r"任意|optional")
_REQUIRED_CLASS = re.compile(r"required|validates-as-required|must|hissu|necessary", re.I)
_SEI = re.compile(r"姓|せい|セイ|(?<![a-z])sei(?![a-z])|last|family|苗字|名字")
_MEI = re.compile(r"めい|メイ|(?<![a-z])mei(?![a-z])|first|given|(?<![氏姓会社店])名(?![前字称])")
_NOTE = re.compile(r"^[※＊*(（]")

# 種別の選択肢。正直に選べるもの（提案・取引）→「その他」→ 一般の問い合わせ、の順で選ぶ
_OPT_GOOD = re.compile(
    r"ご?提案|お?取引(?!先)|協業|業務提携|アライアンス|法人(?:の|向け)|ビジネス|営業の?ご?(?:連絡|案内|提案)|セールス|"
    r"サービスのご?案内|(?<![a-z])(?:business|partner|proposal|sales)",
    re.I,
)
_OPT_OTHER = re.compile(r"^(?:その他|そのほか|上記以外|該当なし|other)", re.I)
_OPT_GENERAL = re.compile(
    r"^(?:その他の?|一般的?な?の?|サービス(?:について|に関する|へ)の?)?(?:お問い?合わ?せ|ご相談|ご質問|ご意見)(?:・ご?(?:相談|質問|意見))?$|"
    r"^(?:general|inquiry|contact)$",
    re.I,
)
_OPT_BAD = re.compile(
    r"予約|採用|求人|応募|取材|クレーム|苦情|落とし物|忘れ物|キャンセル|退会|解約|見学|体験|入会|資料請求|"
    r"見積|注文|購入|返品|キャスト|面接|出演|貸切|パーティ|宿泊|営業時間|営業日|駐車|診療|診察|歯科|レッスン|入居"
)
_RESERVE = re.compile(r"人数|日にち|日時|来店|希望日|予約|チェックイン|利用日|時間帯|名様|コース|ご利用時間|希望時間")
_RECRUIT = re.compile(r"応募|エントリー|履歴書|職種|志望|面接|年齢|生年月日|性別|経験")

# フォームの目的。見出し・送信ボタン・送信先から読む
_P_CONTACT = re.compile(
    r"お?問い?合わ?せ|問合せ|contact|inquiry|enquiry|toiawase|ご相談|ご意見|ご質問|メッセージ", re.I
)
_P_COMMENT = re.compile(r"コメント|口コミ|クチコミ|レビュー|掲示板|書き込|投稿|comment|review|bbs|respond", re.I)
_P_RESERVE = re.compile(r"予約|reserv|booking", re.I)
_P_RECRUIT = re.compile(r"応募|エントリー|採用|求人|recruit|saiyo|kyujin|career", re.I)
_P_REQUEST = re.compile(
    r"資料請求|見積|カタログ|注文|申し?込|会員登録|入会|体験|見学|査定|診断|"
    r"(?<![a-z])(?:request|estimate|order|apply|signup|register)",
    re.I,
)
_P_INSIDE = re.compile(
    r"(?:予約|応募|エントリー|資料請求|見積り?|申し?込み?)(?:専用)?フォーム|ご?予約内容|ご予約はこちら"
)
_SALES_CHECK = re.compile(r"営業|勧誘|セールス|売り?込")
_CONSENT_TOPIC = re.compile(
    r"個人情報|プライバシー|privacy|規約|ポリシー|policy|利用条件|取り?扱い?|同意事項|上記", re.I
)
_CONSENT_VERB = re.compile(r"同意|承諾|了承|agree|accept|確認しました|確認の上|確認のうえ", re.I)
_OTHER_WISH = re.compile(
    r"カタログ|案内|メルマガ|メールマガジン|配信|希望|送付|受け取|newsletter|お知らせ|ニュース", re.I
)

_SEND_BUTTON = re.compile(r"確認|送信|送る|submit|send|次へ|進む|申し込|問い?合わ?せる|この内容で|ok|確定|はい", re.I)
_BACK_BUTTON = re.compile(
    r"戻る|戻って|もどる|修正|訂正|変更|やり直|書き直|前へ|前の|リセット|クリア|取り?消|キャンセル|"
    r"back|reset|clear|cancel|edit",
    re.I,
)
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|left\s*:\s*-\d{3,}|opacity\s*:\s*0(?![.\d])", re.I
)

# ページのどこかにあれば、人が送ることにする仕組み（回避はしない）
_GUARDS = [
    (re.compile(r"recaptcha", re.I), "reCAPTCHA があります"),
    (re.compile(r"hcaptcha", re.I), "hCaptcha があります"),
    (re.compile(r"turnstile|challenges\.cloudflare\.com", re.I), "Cloudflare の認証があります"),
    (
        re.compile(
            r"captcha|画像認証|認証コード|表示されている(?:文字|数字)|表示された(?:文字|数字)|画像の(?:文字|数字)|"
            r"計算の?(?:結果|答え)|足し算|ロボットでは|wpcf7-quiz|altcha|mosparo|friendlycaptcha|geetest|arkose",
            re.I,
        ),
        "画像認証やクイズがあります",
    ),
    (
        re.compile(
            r"_wpcf7_ak_js|ak_js|akismet|cleantalk|apbct|ct_checkjs|wpa_field|wp-?armou?r|zero-?spam|"
            r"anti-?spam|cf7-?antispam|spam-?protect|stopforumspam",
            re.I,
        ),
        "迷惑判定の仕組み（Akismet など）があります",
    ),
]

FLAG_LABELS = {
    "category": "種別を選んでいる",
    "consent": "同意のチェックを入れる",
    "unknown_fields": "意味の分からない欄がある（空か、最初の選択肢のまま送る）",
    "no_email_field": "メールアドレスの欄がない",
    "multi_form": "同じページに問い合わせフォームが複数ある",
    "contact_unread": "お問い合わせページを読めていない（お断りの表示を要確認）",
    "page_truncated": "ページが長すぎて最後まで読めていない",
}
ROLE_LABELS = {
    "company": "会社名",
    "name": "担当者名",
    "name_sei": "担当者名（姓）",
    "name_mei": "担当者名（名）",
    "kana": "フリガナ",
    "kana_sei": "フリガナ（姓）",
    "kana_mei": "フリガナ（名）",
    "email": "メールアドレス",
    "tel": "電話番号",
    "tel1": "電話番号(1)",
    "tel2": "電話番号(2)",
    "tel3": "電話番号(3)",
    "zip": "郵便番号",
    "zip1": "郵便番号(前)",
    "zip2": "郵便番号(後)",
    "pref": "都道府県",
    "address": "住所",
    "subject": "件名",
    "body": "本文",
    "url": "サイトのURL",
}


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def find_guard(html: str) -> str:
    """CAPTCHA や迷惑判定の仕組みがあれば、その説明を返す。無ければ ""。"""
    for pattern, why in _GUARDS:
        if pattern.search(html):
            return why
    return ""


@dataclass
class RawField:
    tag: str
    type: str
    name: str = ""
    value: str = ""
    id: str = ""
    placeholder: str = ""
    cls: str = ""
    required_attr: bool = False
    styled_hidden: bool = False
    checked: bool = False
    chunks: list[str] = field(default_factory=list)  # 直前に出てきた文字（前の欄より後、古い順）
    heading: str = ""  # 直前の行見出し（th / dt / legend）
    wrap_label: str = ""  # 囲んでいる <label> の文字
    after: str = ""  # 直後の文字（チェックボックス・ラジオの選択肢名）
    text: str = ""  # ボタンの文字
    options: list[tuple[str, str, bool]] = field(default_factory=list)  # (値, 表示, selected)
    label: str = ""  # 人に見せる見出し
    own_label: str = ""  # <label> で結び付いた見出し（あれば）


@dataclass
class RawForm:
    action: str
    method: str = "get"
    enctype: str = ""
    accept_charset: str = ""
    cls: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    fields: list[RawField] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    pre_heading: str = ""  # フォームの直前の見出し（h1〜h4）
    implicit: bool = False  # <form> の外に置かれた欄（JavaScript で送る作り）


class _FormParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.forms: list[RawForm] = []
        self.orphan = RawForm(action=base_url, implicit=True)
        self.cur: RawForm | None = None
        self.labels_for: dict[str, str] = {}
        self.api_root = ""
        self.iframes: list[str] = []
        self.title = ""
        self._stack: list[tuple[str, bool]] = []
        self._hidden = 0
        self._skip = 0
        self._label: dict | None = None
        self._head: list[str] | None = None
        self._head_tag = ""
        self._heading = ""
        self._page_heading = ""
        self._recent: list[str] = []
        self._select: RawField | None = None
        self._option: dict | None = None
        self._button: RawField | None = None
        self._textarea: RawField | None = None
        self._after: RawField | None = None
        self._in_title = False

    def _join(self, base: str, href: str) -> str:
        try:
            return urljoin(base, href)
        except ValueError:
            return ""

    def _close_option(self) -> None:
        if self._option is not None and self._select is not None:
            text = _norm(" ".join(self._option["parts"]))
            value = self._option["value"]
            self._select.options.append((text if value is None else value, text, self._option["selected"]))
        self._option = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        # </option> を省いた古い書き方でも、次のタグで選択肢を閉じる
        if self._option is not None:
            self._close_option()
        if self._select is not None and tag not in ("option", "optgroup"):
            self._select = None
        a = {k: (v or "") for k, v in attrs}
        classes = set(a.get("class", "").lower().split())
        hidden_here = (
            bool(_HIDDEN_STYLE.search(a.get("style", "")))
            or "hidden" in a
            or bool(classes & _HIDDEN_CLASSES)
            or any("honeypot" in c or "wpcf7-hp" in c for c in classes)
        )
        if tag not in _VOID:
            self._stack.append((tag, hidden_here))
            if hidden_here:
                self._hidden += 1
        if tag == "title":
            self._in_title = True
        elif tag == "base" and a.get("href"):
            self.base = self._join(self.base, a["href"]) or self.base
        elif tag == "link" and "api.w.org" in a.get("rel", ""):
            self.api_root = self._join(self.base, a.get("href", ""))
        elif tag == "iframe" and a.get("src"):
            self.iframes.append(self._join(self.base, a["src"]))
        elif tag == "form":
            self.cur = RawForm(
                action=self._join(self.base, a.get("action") or self.base) or self.base,
                method=(a.get("method") or "get").lower(),
                enctype=a.get("enctype", "").lower(),
                accept_charset=a.get("accept-charset", ""),
                cls=a.get("class", ""),
                attrs=a,
                pre_heading=self._page_heading,
            )
            self.forms.append(self.cur)
            self._recent, self._heading = [], ""
        elif tag == "label":
            self._label = {"for": a.get("for", ""), "parts": [], "fields": []}
        elif tag in _ROW_HEADS or tag in _PAGE_HEADS:
            self._head, self._head_tag = [], tag
        elif tag in ("input", "textarea", "select", "button"):
            self._field(tag, a, hidden_here)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value") if "value" in a else None, "parts": [], "selected": "selected" in a}

    def _field(self, tag: str, a: dict[str, str], hidden_here: bool) -> None:
        ftype = (
            (a.get("type") or ("submit" if tag == "button" else "text")).lower() if tag in ("input", "button") else tag
        )
        trap = a.get("tabindex", "").strip() == "-1" and a.get("autocomplete", "").lower() in (
            "off",
            "nope",
            "new-password",
        )
        f = RawField(
            tag=tag,
            type=ftype,
            name=a.get("name", ""),
            value=a.get("value", ""),
            id=a.get("id", ""),
            placeholder=" ".join(
                x for x in (a.get("placeholder", ""), a.get("aria-label", ""), a.get("title", "")) if x
            ),
            cls=a.get("class", ""),
            required_attr="required" in a or a.get("aria-required", "").lower() == "true",
            styled_hidden=self._hidden > 0 or hidden_here or trap,
            checked="checked" in a,
            chunks=list(self._recent[-4:]),
            heading=self._heading,
        )
        if self.cur is None and not self.orphan.fields:
            self.orphan.pre_heading = self._page_heading
        (self.cur or self.orphan).fields.append(f)
        if self._label is not None:
            self._label["fields"].append(f)
        self._after = f if ftype in ("checkbox", "radio") else None
        if ftype != "hidden":
            self._recent = []
        if tag == "select":
            self._select = f
        elif tag == "button":
            self._button = f
        elif tag == "textarea":
            self._textarea = f

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if self._option is not None:
            self._close_option()
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, hidden in self._stack[i:]:
                    if hidden:
                        self._hidden -= 1
                del self._stack[i:]
                break
        if tag == "title":
            self._in_title = False
        elif tag == "form":
            self.cur = None
        elif tag == "label" and self._label is not None:
            text = _norm(" ".join(self._label["parts"]))
            if self._label["for"]:
                self.labels_for[self._label["for"]] = text
            for f in self._label["fields"]:
                f.wrap_label = text
            self._label = None
        elif tag == self._head_tag and self._head is not None:
            text = _norm(" ".join(self._head))
            if tag in _PAGE_HEADS:
                self._page_heading = text
            else:
                self._heading = text
            self._head = None
        elif tag == "select":
            self._select = None
        elif tag == "button":
            self._button = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._textarea is not None:
            self._textarea.value += data  # 入力画面に戻されたとき、こちらの入力が残っているかを見るため
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
            return
        if self._option is not None:
            self._option["parts"].append(text)
            return
        if self._button is not None:
            self._button.text += text
            return
        if self._after is not None:
            self._after.after = (self._after.after + " " + text).strip()[:80]
        if self._label is not None:
            self._label["parts"].append(text)
        if self._head is not None:
            self._head.append(text)
        self._recent.append(text)
        if self.cur is not None and len(self.cur.text) < 400:
            self.cur.text.append(text)


@dataclass
class ParsedPage:
    url: str
    forms: list[RawForm]
    api_root: str
    iframes: list[str]
    html: str
    title: str = ""


def _display_label(f: RawField) -> str:
    if f.own_label:
        return f.own_label
    chunks = [c for c in f.chunks if not _NOTE.match(c)] or f.chunks
    label = _norm(" ".join(chunks[-2:]))
    if f.heading and f.heading in _norm(" ".join(f.chunks)):
        label = f.heading if f.heading in label or not label else f"{f.heading} {label}"
    tokens = label.split()
    while len(tokens) > 1 and len(tokens[0]) == 1 and tokens[0] not in "姓名〒":
        tokens.pop(0)  # 前の欄の単位（「時」「分」「-」など）
    label = " ".join(tokens)
    if len(re.sub(r"[\s\-ー~〜・:：]", "", label)) < 2 and f.heading:
        label = f"{f.heading} {label}".strip()
    return label


def parse_forms(html: str, url: str) -> ParsedPage:
    parser = _FormParser(url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # 壊れたHTMLでも、そこまでに読めた分は使う
    forms = list(parser.forms)
    if any(f.tag == "textarea" for f in parser.orphan.fields):
        forms.append(parser.orphan)
    for form in forms:
        for f in form.fields:
            f.own_label = (parser.labels_for.get(f.id, "") if f.id else "") or f.wrap_label
            f.label = _display_label(f)[:80]
    return ParsedPage(
        url=url, forms=forms, api_root=parser.api_root, iframes=parser.iframes, html=html, title=_norm(parser.title)
    )


@dataclass
class Plan:
    mode: str  # auto / manual / skip
    kind: str  # cf7 / html / external / script
    page_url: str
    action: str = ""
    method: str = "post"
    enctype: str = ""
    charset: str = ""
    reason: str = ""
    items: list[dict] = field(default_factory=list)
    submit: dict | None = None
    flags: list[str] = field(default_factory=list)
    rest_url: str = ""  # Contact Form 7 の送信先（ページ自身が使うもの）
    body_name: str = ""
    shape: list[list[str]] = field(default_factory=list)  # 名前のある欄すべての (名前, 種類, 固定の値)

    def signature(self) -> str:
        """承認したときと同じフォームかを見分ける印。

        欄の名前・種類・見出し・選ぶ値、送らない欄や隠し項目の有無まで含める。
        毎回変わる隠し項目の値（トークン）だけは含めない。
        """
        parts = urlsplit(self.action)
        query = sorted((k, v) for k, v in parse_qsl(parts.query) if _stable(v))
        core = [
            self.kind,
            parts.path,
            query,
            self.method,
            self.submit,
            [
                [
                    i["name"],
                    i["role"],
                    i.get("choice", ""),
                    _norm(i.get("label", "")),
                    i.get("value", "") if i["role"] in ("default", "check", "choice") else "",
                ]
                for i in self.items
                if i["role"] != "keep"
            ],
            self.shape,
        ]
        return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "action": self.action,
                "method": self.method,
                "sig": self.signature(),
                "items": [{k: v for k, v in i.items() if k != "value" or i["role"] != "keep"} for i in self.items],
                "submit": self.submit,
            },
            ensure_ascii=False,
        )

    def describe(self) -> str:
        return describe_items(self.items)


_TOKEN_NAME = re.compile(r"token|csrf|nonce|hash|ticket|sess|time|stamp|key|rand|uniq|_wp|captcha|ip|host|agent", re.I)


def _stable(value: str) -> bool:
    """毎回変わらない、意味のある短い値か（contact / reserve など）。トークンや番号は False。"""
    return bool(re.fullmatch(r"[A-Za-z_\-]{1,24}", value)) or (
        0 < len(value) <= 20 and not value.isascii() and not re.search(r"\d{4,}", value)
    )


def describe_items(items: list[dict]) -> str:
    """確認用の一覧。「お名前→担当者名 / 種別→『その他』」の形。"""
    parts: list[str] = []
    for i in items:
        role = i["role"]
        label = i.get("label") or i["name"]
        if role in ("keep", "empty", "honeypot"):
            continue
        if role == "choice":
            parts.append(f"{label}→「{i.get('choice', '')}」")
        elif role == "default":
            if i.get("value"):
                parts.append(f"{label}→「{i.get('choice') or i['value']}」のまま")
        elif role == "check":
            parts.append(f"{label}→チェック")
        else:
            parts.append(f"{label}→{ROLE_LABELS.get(role, role)}")
    return " / ".join(parts)


def _role_in(text: str, table=_TEXT_ROLES) -> str:  # noqa: ANN001
    for role, pattern in table:
        if pattern.search(text):
            return role
    return ""


def _name_words(f: RawField) -> str:
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", f"{f.name} {f.id}")
    return " ".join(re.findall(r"[a-z]+", raw.lower()))


def _text_role(f: RawField) -> tuple[str, bool]:
    """1行の入力欄に何を入れるか。 (役, 見出しと name が食い違っているか)

    見出し（<label>、近くの文字）を先に見る。見出しがあるのに意味が分からない欄は、
    name 属性だけで決めない（name=plan_name の「ご利用プラン」を氏名にしないため）。
    """
    by_name = "email" if f.type == "email" else "tel" if f.type == "tel" else _role_in(_name_words(f), _NAME_ROLES)
    if f.own_label:
        sources = [f.own_label]
    else:
        # 近い文字から順に見る。「※」で始まる注記（前の欄の説明）は見出しにしない
        sources = [c for c in reversed(f.chunks) if not _NOTE.match(c)][:3]
    has_label = any(len(re.sub(r"[\s\W_]|必須|任意", "", unicodedata.normalize("NFKC", s))) >= 2 for s in sources)
    by_label = ""
    for source in sources:
        by_label = _role_in(_norm(source).lower())
        if by_label:
            break
    if by_label:
        return by_label, bool(by_name) and by_name != by_label and by_name not in ("name",)
    if has_label:
        return "", False
    by_placeholder = _role_in(_norm(f.placeholder).lower())
    if by_placeholder:
        return by_placeholder, False
    if by_name:
        return by_name, False
    return _role_in(_norm(f.heading).lower()), False


def _is_required(f: RawField) -> bool:
    text = _norm(f"{f.label} {f.heading} {' '.join(f.chunks)}")
    if _OPTIONAL.search(_norm(f.label)):
        return False
    return f.required_attr or bool(_REQUIRED_CLASS.search(f.cls)) or bool(_REQUIRED.search(text))


def _pick_option(options: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(値, 表示) の中から、営業の連絡として選んでよいものを選ぶ。無ければ None。"""
    usable = [(v, t) for v, t in options if v != "" and t and not _OPT_BAD.search(t)]
    for pattern in (_OPT_GOOD, _OPT_OTHER, _OPT_GENERAL):
        for value, text in usable:
            if pattern.search(text):
                return value, text
    return None


def _readable(value: str) -> bool:
    v = value.strip().lower()
    return bool(v) and not re.fullmatch(r"[\d\W_]+|on|true|false|yes|no|[a-z]{0,2}\d+", v)


def _member_labels(members: list[RawField]) -> list[str | None]:
    """ラジオ・チェックボックスの選択肢名。確かめられないものは None（文字の前後がずれている作りがあるため）。"""
    shared = len(members) > 1 and len({m.own_label for m in members}) == 1
    out: list[str | None] = []
    for m in members:
        if m.own_label and not shared:
            out.append(_norm(m.own_label))
        elif _readable(m.value):
            out.append(_norm(m.value))
        elif len(members) == 1 and m.after:
            out.append(_norm(m.after))
        else:
            out.append(None)
    return out


def build_plan(form: RawForm, page: ParsedPage, charset: str = "utf-8") -> Plan | None:
    """問い合わせフォームなら送り方を決めて返す。検索・ログイン・コメント欄のフォームは None。"""
    fields = form.fields
    if any(f.type == "password" for f in fields):
        return None
    visible_areas = [f for f in fields if f.tag == "textarea" and not f.styled_hidden]
    if not visible_areas:
        return None

    host = site_host(page.url)
    buttons = [f for f in fields if f.type in ("submit", "image") or (f.tag == "button" and f.type == "submit")]
    button_text = " ".join(f"{b.value} {b.text}" for b in buttons)
    action_path = urlsplit(form.action).path
    form_ident = f"{form.attrs.get('id', '')} {form.cls} {form.attrs.get('name', '')}"
    form_text = _norm(" ".join(form.text))
    purpose = _norm(f"{form.pre_heading} {button_text} {action_path} {form_ident}")

    # 公開されるコメント欄・口コミ欄は、問い合わせフォームではない
    if (
        "wp-comments-post" in action_path
        or any(f.name == "comment_post_ID" for f in fields)
        or _P_COMMENT.search(f"{form_ident} {button_text} {form.pre_heading}")
    ):
        return None

    is_cf7 = "wpcf7" in form.cls or any(f.name == "_wpcf7" for f in fields)
    plan = Plan(
        mode="auto",
        kind="cf7" if is_cf7 else "html",
        page_url=page.url,
        action=form.action.split("#", 1)[0],
        method=form.method,
        enctype=form.enctype,
        charset=(form.accept_charset.split(",")[0].split()[0] if form.accept_charset.strip() else charset),
    )
    manual: list[str] = []
    skip = ""
    flags: list[str] = []

    # 本文の欄: 名前や見出しがそれらしいもの、無ければ最初の textarea
    body_field = next(
        (f for f in visible_areas if _BODY.search(_norm(f"{f.label} {f.name} {f.id}").lower())), visible_areas[0]
    )
    plan.body_name = body_field.name

    groups: dict[tuple[str, str], list[RawField]] = {}
    for f in fields:
        if f.type in ("radio", "checkbox") and f.name:
            groups.setdefault((f.type, f.name), []).append(f)
    done_groups: set[tuple[str, str]] = set()
    unknown_required: list[str] = []
    reserve_fields: list[str] = []
    reserve_required = False
    recruit_words: set[str] = set()

    for f in fields:
        if f.name and f.type not in _BUTTON_TYPES and f.tag != "button":
            fixed = f.value if f.type == "hidden" and _stable(f.value) and not _TOKEN_NAME.search(f.name) else ""
            if [f.name, f.type, fixed] not in plan.shape:
                plan.shape.append([f.name, f.type, fixed])
        if not f.name and f.type not in _BUTTON_TYPES:
            continue
        label = f.label or f.placeholder or f.name
        item = {"name": f.name, "role": "empty", "label": label[:40]}
        described = _norm(f"{f.label} {f.placeholder} {_name_words(f)}").lower()
        required = _is_required(f)
        hidden = f.styled_hidden or bool(_HONEYPOT.search(f"{f.name} {f.id} {f.cls} {f.label} {f.placeholder}".lower()))
        if not hidden and f.type != "hidden":
            # ラジオ・チェックは選択肢の文字（「ご予約について」など）ではなく、問いの見出しで見る
            topic = _norm(f.heading).lower() if f.type in ("radio", "checkbox") else described
            if _RESERVE.search(topic) or f.type in ("date", "time", "datetime-local"):
                reserve_fields.append(label)
                reserve_required = reserve_required or required
            recruit_words.update(_RECRUIT.findall(topic))
        if f.type == "hidden":
            item.update(role="keep", value=f.value)
        elif f.type in _BUTTON_TYPES or f.tag == "button":
            continue
        elif hidden:
            item["role"] = "honeypot"  # 人には見えない欄。ブラウザと同じく空のまま送る
        elif f.type == "file":
            if required:
                manual.append("ファイルの添付が必須です")
            continue
        elif f.type in ("checkbox", "radio"):
            key = (f.type, f.name)
            if key in done_groups:
                continue
            done_groups.add(key)
            members = groups[key]
            names = _member_labels(members)
            group_required = any(_is_required(m) for m in members)
            question = _norm(f"{f.heading} {' '.join(f.chunks)}")
            item["label"] = ((names[0] if len(members) == 1 else "") or f.heading or question or f.name)[:40]
            if None in names:
                # 選択肢の名前を確かめられない。必須なら人に回し、任意なら選ばない
                if group_required:
                    unknown_required.append(item["label"])
                continue
            options = [(m.value or "on", n) for m, n in zip(members, names)]
            if f.type == "checkbox" and len(members) == 1:
                own = f"{names[0]} {f.name}".lower()
                if _SALES_CHECK.search(own):
                    skip = "営業目的の連絡ではないことの確認を求めています"
                    continue
                if _OTHER_WISH.search(own):
                    continue  # 案内メールやカタログの希望。チェックしない
                if (
                    _CONSENT_VERB.search(own)
                    and (_CONSENT_TOPIC.search(own) or not re.search(r"[ぁ-んァ-ヶ一-龥]{12,}", own))
                ) or re.search(r"\b(?:acceptance|agree|agreement|privacy|consent)\b", _name_words(f)):
                    item.update(role="check", value=options[0][0])
                    flags.append("consent")
                elif group_required:
                    unknown_required.append(item["label"])
                    continue
                else:
                    continue
            elif _METHOD.search(question):
                pick = next(((v, t) for v, t in options if re.search(r"メール|mail", t, re.I)), None)
                if pick:
                    item.update(role="choice", value=pick[0], choice=pick[1])
                elif group_required:
                    unknown_required.append(item["label"])
                    continue
                else:
                    continue
            else:
                pick = _pick_option(options)
                if pick and (group_required or _OPT_GOOD.search(pick[1])):
                    item.update(role="choice", value=pick[0], choice=pick[1])
                    flags.append("category")
                elif group_required:
                    unknown_required.append(item["label"])
                    continue
                else:
                    continue  # 任意の選択は選ばない（ブラウザと同じく送らない）
        elif f.tag == "select":
            real = [(v, t) for v, t, _ in f.options]
            default, default_text = next(((v, t) for v, t, sel in f.options if sel), real[0] if real else ("", ""))
            # 見出しの無い選択欄は、「選択してください」の行が見出しの代わりになっている
            prompt = " ".join(t for v, t in real if v == "")
            hay = f"{described} {_norm(prompt).lower()}"
            if not f.label and prompt:
                item["label"] = prompt[:40]
            topics = sum(1 for _, t in real if re.search(r"について|お問い?合わ?せ|ご相談|その他", t))
            if _PREF.search(hay):
                item.update(role="pref", options=real, default=default, required=required)
            elif _CATEGORY.search(hay) or topics >= 2 or (default and _OPT_BAD.search(default_text)):
                pick = _pick_option(real)
                if pick and (required or default != "" or _OPT_GOOD.search(pick[1])):
                    item.update(role="choice", value=pick[0], choice=pick[1])
                    flags.append("category")
                elif required or default != "":
                    unknown_required.append(item["label"])  # 「予約」「採用」などしか選べない
                    continue
                else:
                    item.update(role="default", value="")
            elif required and default == "":
                unknown_required.append(item["label"])
                continue
            else:
                item.update(role="default", value=default, choice=default_text)
                if default:
                    flags.append("unknown_fields")
        elif f.tag == "textarea":
            if f is body_field:
                item["role"] = "body"
            elif required:
                unknown_required.append(label)
                continue
        elif f.type in ("date", "time", "datetime-local", "month", "week", "range", "color"):
            if required:
                unknown_required.append(label)
            continue
        elif f.type in _TEXT_TYPES:
            role, conflict = _text_role(f)
            if role in ("fax", "dept"):
                role = ""
            if conflict:
                role = ""  # 見出しと name が別の意味を指している。決めつけない
            if role == "kana":
                words = _norm(f"{f.label} {f.placeholder}")
                item["kana"] = (
                    "hira"
                    if _HIRAGANA_HINT.search(words) and not re.search(r"フリガナ|カタカナ|カナ", words)
                    else "kata"
                )
                if re.search(r"会社|法人|社名|店", words):
                    role = ""  # 会社名のフリガナは持っていない
            if role == "url" and not required:
                role = ""  # 任意のURL欄は空のまま（人に見えない欄のことがある）
            if role:
                item["role"] = role
            elif required:
                unknown_required.append(label)
                continue
            elif f.type != "search":
                flags.append("unknown_fields")
        else:
            if required:
                unknown_required.append(label)
            continue
        item["required"] = required
        plan.items.append(item)

    _split_runs(plan.items, fields)

    # 送信ボタン
    send = [b for b in buttons if not _BACK_BUTTON.search(f"{b.value} {b.text} {b.name}")]
    chosen = next((b for b in send if _SEND_BUTTON.search(f"{b.value} {b.text}")), send[0] if send else None)
    if chosen is None:
        manual.append("送信ボタンが JavaScript で動く作りです")
    elif chosen.type == "image":
        manual.append("送信ボタンが画像です")
    elif chosen.name:
        plan.submit = {"name": chosen.name, "value": chosen.value or chosen.text}

    # 送らないフォーム・人が確かめるフォーム（目的）
    is_contact = bool(_P_CONTACT.search(f"{purpose} {form_text}"))
    other_purpose = next(
        (
            why
            for pattern, why in (
                (_P_RESERVE, "予約用のフォームです"),
                (_P_RECRUIT, "求人応募用のフォームです"),
                (_P_REQUEST, "資料請求・申し込み用のフォームです"),
            )
            if pattern.search(purpose)
        ),
        "",
    )
    inside = _P_INSIDE.search(form_text)  # フォームの中の見出し（「ご予約フォーム」など）
    if not other_purpose and inside:
        other_purpose = (
            "予約用のフォームです"
            if "予約" in inside.group(0)
            else "求人応募用のフォームです"
            if re.search("応募|エントリー", inside.group(0))
            else "資料請求・申し込み用のフォームです"
        )
    if not skip:
        if other_purpose and not _P_CONTACT.search(purpose):
            skip = other_purpose
        elif len(recruit_words) >= 3 or len(set(_RECRUIT.findall(form_text))) >= 4:
            skip = "求人応募用のフォームです"
        elif reserve_fields and (reserve_required or not is_contact):
            skip = "予約用のフォームです"
        elif other_purpose:
            manual.append(f"問い合わせ以外と共用のフォームかもしれません（{other_purpose[:-6]}）")
        elif reserve_fields:
            manual.append(
                "日時や人数の欄があります（予約用の可能性）: " + "、".join(dict.fromkeys(reserve_fields))[:60]
            )
        if not skip and not is_contact and not _P_CONTACT.search(f"{page.title} {urlsplit(page.url).path}"):
            manual.append("問い合わせ用のフォームか確かめられません")

    # 人が送るフォーム（仕組み）
    guard = find_guard(page.html)
    if guard:
        manual.append(guard)
    if form.implicit:
        manual.append("JavaScript で送る作りです")
        plan.kind = "script"
    elif form.method != "post":
        manual.append("送信方法が POST ではありません")
    if site_host(plan.action) != host:
        manual.append("送信先が外部のサービスです")
        plan.kind = "external"
    if (
        any(
            k.startswith(
                ("data-ajax", "data-netlify", "@submit", "v-on:submit", "x-on:submit", "onsubmit", "ng-submit")
            )
            for k in form.attrs
        )
        and not is_cf7
    ):
        manual.append("JavaScript で送る作りです")
    if unknown_required:
        manual.append("必須の欄に入れる値が決められません: " + "、".join(dict.fromkeys(unknown_required)))

    if is_cf7 and page.api_root and site_host(page.api_root) == host:
        form_id = next((f.value for f in fields if f.name == "_wpcf7"), "")
        if form_id.isdigit():
            route = f"contact-form-7/v1/contact-forms/{form_id}/feedback"
            root = urlsplit(page.api_root)
            query = parse_qsl(root.query, keep_blank_values=True)
            if any(key == "rest_route" for key, _ in query):
                # WordPress uses a query route when pretty permalinks are disabled.
                query = [
                    (key, value.rstrip("/") + "/" + route if key == "rest_route" else value) for key, value in query
                ]
                plan.rest_url = root._replace(query=urlencode(query), fragment="").geturl()
            else:
                # Treat the advertised API root as a directory, even without a trailing slash.
                plan.rest_url = root._replace(path=root.path.rstrip("/") + "/" + route, fragment="").geturl()
    if not any(i["role"] == "email" for i in plan.items):
        flags.append("no_email_field")
    plan.flags = list(dict.fromkeys(flags))
    if skip:
        plan.mode, plan.reason = "skip", skip
    elif manual:
        plan.mode, plan.reason = "manual", " ／ ".join(dict.fromkeys(manual))
    return plan


def _split_runs(items: list[dict], fields: list[RawField]) -> None:
    """姓・名、電話番号の3分割、郵便番号の2分割のように、続けて並んだ同じ種類の欄を割り振る。"""
    by_name = {f.name: f for f in fields if f.name}

    def hay(item: dict) -> str:
        f = by_name.get(item["name"])
        return _norm(f"{f.label} {f.name} {f.id} {f.placeholder}").lower() if f else ""

    def together(run: list[dict]) -> bool:
        """同じ見出しの下に並んでいるか、名前が連番（tel1, tel2…）なら、1つの値を分けた欄とみなす。"""
        raws = [by_name.get(item["name"]) for item in run]
        if any(r is None for r in raws):
            return False
        headings = {r.heading for r in raws}
        stems = {re.sub(r"[\d\[\]_\-]+$", "", r.name) for r in raws}
        return (len(headings) == 1 and "" not in headings) or (len(stems) == 1 and "" not in stems)

    i = 0
    while i < len(items):
        role = items[i]["role"]
        j = i
        while j + 1 < len(items) and items[j + 1]["role"] == role:
            j += 1
        run = items[i : j + 1]
        if role in ("name", "kana") and len(run) == 2:
            hays = [hay(item) for item in run]
            named = any(_SEI.search(h) or _MEI.search(h) for h in hays)
            if named or together(run):
                order = ("mei", "sei") if _MEI.search(hays[0]) and not _SEI.search(hays[0]) else ("sei", "mei")
                run[0]["role"], run[1]["role"] = f"{role}_{order[0]}", f"{role}_{order[1]}"
        elif role == "tel" and len(run) == 3 and together(run):
            for k, item in enumerate(run, 1):
                item["role"] = f"tel{k}"
        elif role == "zip" and len(run) == 2 and together(run):
            run[0]["role"], run[1]["role"] = "zip1", "zip2"
        i = j + 1


def find_contact_form(html: str, url: str, charset: str = "utf-8") -> Plan | None:
    """ページの中から問い合わせフォームを1つ選ぶ。auto → manual → skip の順に優先する。"""
    page = parse_forms(html, url)
    plans = [p for p in (build_plan(f, page, charset) for f in page.forms) if p is not None]
    if not plans:
        # 外部のフォームサービスを埋め込んでいる（Google フォームなど）
        for src in page.iframes:
            if src.lower().startswith(("http://", "https://")) and is_form_service(site_host(src)):
                return Plan(
                    mode="manual", kind="external", page_url=url, action=src, reason="外部のフォームサービスです"
                )
        return None
    order = {"auto": 0, "manual": 1, "skip": 2}
    plans.sort(key=lambda p: order[p.mode])
    best = plans[0]
    if sum(1 for p in plans if p.mode != "skip") > 1:
        best.flags.append("multi_form")
    return best


def is_form_service(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in FORM_SERVICE_HOSTS)


# ---- 値を入れる ----
class FormValueError(Exception):
    """設定（.env）が足りない、または文字コードの都合で、欄に入れる値を作れない。"""


def _kana(text: str, kind: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if kind == "hira" and 0x30A1 <= code <= 0x30F6:
            ch = chr(code - 0x60)
        elif kind == "kata" and 0x3041 <= code <= 0x3096:
            ch = chr(code + 0x60)
        out.append(ch)
    return "".join(out)


def _parts(text: str, n: int, sep: str) -> list[str] | None:
    parts = [p for p in re.split(sep, unicodedata.normalize("NFKC", text).strip()) if p]
    return parts if len(parts) == n else None


def fill(items: list[dict], sender: dict[str, str], subject: str, body: str) -> list[tuple[str, str]]:
    """欄の名前と入れる値の組を、フォームの並び順で返す。

    sender は company / person / kana / email / tel / zip / address / url。
    必須の欄に入れる値が設定に無ければ FormValueError。
    """
    pairs: list[tuple[str, str]] = []

    def need(item: dict, value: str, what: str) -> str:
        if not value and item.get("required"):
            raise FormValueError(f"必須の「{item.get('label') or item['name']}」に入れる {what} が未設定です（.env）")
        return value

    address = re.sub(r"^〒?\s*\d{3}-?\d{4}\s*", "", sender.get("address", ""))
    for item in items:
        role, name = item["role"], item["name"]
        if role in ("keep", "choice", "check", "default"):
            value = item.get("value", "")
        elif role in ("empty", "honeypot"):
            value = ""
        elif role == "company":
            value = sender["company"]
        elif role == "name":
            value = sender["person"]
        elif role in ("name_sei", "name_mei", "kana_sei", "kana_mei"):
            source = sender["person"] if role.startswith("name") else sender.get("kana", "")
            parts = _parts(source, 2, r"[\s　]+")
            if parts is None:
                if not source and not item.get("required"):
                    value = ""
                else:
                    key = "SENDER_PERSON" if role.startswith("name") else "SENDER_PERSON_KANA"
                    raise FormValueError(
                        f"{key} を「姓 名」のように空白で区切ってください（姓と名が別の欄のフォーム用）"
                    )
            else:
                value = parts[0 if role.endswith("sei") else 1]
            if role.startswith("kana"):
                value = _kana(value, item.get("kana", "kata"))
        elif role == "kana":
            value = _kana(need(item, sender.get("kana", ""), "SENDER_PERSON_KANA"), item.get("kana", "kata"))
        elif role == "email":
            value = sender["email"]
        elif role == "tel":
            value = need(item, sender.get("tel", ""), "SENDER_TEL")
        elif role in ("tel1", "tel2", "tel3"):
            parts = _parts(sender.get("tel", ""), 3, r"[-‐－ー\s()]+")
            if parts is None:
                if sender.get("tel") or item.get("required"):
                    raise FormValueError(
                        "SENDER_TEL を 06-1234-5678 のようにハイフンで区切ってください（3つに分かれた欄用）"
                    )
                value = ""
            else:
                value = parts[int(role[-1]) - 1]
        elif role == "zip":
            value = need(item, sender.get("zip", ""), "SENDER_ZIP")
        elif role in ("zip1", "zip2"):
            parts = _parts(sender.get("zip", ""), 2, r"[-‐－ー\s]+")
            if parts is None:
                if sender.get("zip") or item.get("required"):
                    raise FormValueError("SENDER_ZIP を 530-0001 のようにハイフンで区切ってください")
                value = ""
            else:
                value = parts[int(role[-1]) - 1]
        elif role == "pref":
            hit = next((v for v, t in item.get("options", []) if t and len(t) >= 2 and t in address), None)
            if hit is None and item.get("required"):
                raise FormValueError("都道府県の欄を選べません。SENDER_ADDRESS を都道府県名から書いてください")
            value = hit if hit is not None else item.get("default", "")
        elif role == "address":
            value = need(item, address, "SENDER_ADDRESS")
        elif role == "subject":
            value = subject
        elif role == "body":
            value = body
        elif role == "url":
            value = need(item, sender.get("url", ""), "DEMO_URL")
        else:
            value = ""
        pairs.append((name, value))
    return pairs

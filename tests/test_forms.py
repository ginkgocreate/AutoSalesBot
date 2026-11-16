"""お問い合わせフォームの検出・自動送信・人が送るときの補助画面の確認。外にはつながない。"""

from __future__ import annotations

import csv
import email
import http.server
import io
import socket
import socketserver
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, unquote

from autosales import cli, db
from autosales.assist import Assist
from autosales.config import load_config
from autosales.crawler import Crawler, crawl_pending
from autosales.forms import FormValueError, fill, find_contact_form
from autosales.formsend import encode_pairs, judge, send_forms
from autosales.mailer import load_template, send_batch
from autosales.replies import process_message

ROOT = Path(__file__).resolve().parent.parent
URL = "https://shop.test/contact/"

CF7 = """
<link rel="https://api.w.org/" href="{api}">
<form action="/contact/#wpcf7-f12-o1" method="post" class="wpcf7-form init" data-status="init">
<input type="hidden" name="_wpcf7" value="12"><input type="hidden" name="_wpcf7_unit_tag" value="wpcf7-f12-o1">
<input type="hidden" name="_token" value="{token}">
<p><label>会社名<br><input type="text" name="your-company"></label></p>
<p><label>お名前（必須）<br><input type="text" name="your-name" class="wpcf7-validates-as-required"></label></p>
<p><label>メールアドレス（必須）<br><input type="email" name="your-email" class="wpcf7-validates-as-required"></label></p>
<p><label>お問い合わせ内容<br><textarea name="your-message"></textarea></label></p>
<p><input type="submit" value="送信"></p>
</form>
"""
PLAIN = """
<form action="/contact/send.php" method="post">
<input type="hidden" name="csrf" value="{token}">
<table>
<tr><th>会社名</th><td><input type="text" name="company"></td></tr>
<tr><th>お名前 <span>必須</span></th><td><input type="text" name="name"></td></tr>
<tr><th>メールアドレス <span>必須</span></th><td><input type="text" name="mail"></td></tr>
<tr><th>電話番号</th><td><input type="text" name="tel"></td></tr>
<tr><th>お問い合わせ内容 <span>必須</span></th><td><textarea name="body"></textarea></td></tr>
</table>
<div style="display:none"><input type="text" name="website"></div>
<input type="submit" name="confirm" value="確認画面へ">
</form>
"""
SENDER = {
    "company": "株式会社テスト",
    "person": "山田 太郎",
    "kana": "ヤマダ タロウ",
    "email": "sales@sender.test",
    "tel": "06-1234-5678",
    "zip": "530-0001",
    "address": "大阪府大阪市北区1-1",
    "url": "https://demo.test/",
}


def plan_of(html: str, url: str = URL):
    return find_contact_form(html, url)


def roles(plan) -> dict[str, str]:
    return {i["name"]: i["role"] for i in plan.items}


class FormDetection(unittest.TestCase):
    def test_plain_contact_forms_are_auto(self) -> None:
        cf7 = plan_of(CF7.format(api="https://shop.test/wp-json/", token="a"))
        self.assertEqual((cf7.mode, cf7.kind), ("auto", "cf7"))
        self.assertEqual(cf7.rest_url, "https://shop.test/wp-json/contact-form-7/v1/contact-forms/12/feedback")
        self.assertEqual(
            roles(cf7),
            {
                "_wpcf7": "keep",
                "_wpcf7_unit_tag": "keep",
                "_token": "keep",
                "your-company": "company",
                "your-name": "name",
                "your-email": "email",
                "your-message": "body",
            },
        )
        plain = plan_of(PLAIN.format(token="a"))
        self.assertEqual((plain.mode, plain.kind, plain.flags), ("auto", "html", []))
        self.assertEqual(roles(plain)["website"], "honeypot")
        self.assertEqual(plain.submit, {"name": "confirm", "value": "確認画面へ"})
        self.assertIn("お名前 必須→担当者名", plain.describe())

    def test_guarded_forms_go_to_a_person(self) -> None:
        base = CF7.format(api="", token="a")
        for extra, word in [
            ('<script src="https://www.google.com/recaptcha/api.js?render=k"></script>', "reCAPTCHA"),
            ('<div class="cf-turnstile"></div>', "Cloudflare"),
            ('<input type="hidden" name="_wpcf7_ak_js" value="1">', "Akismet"),
            ('<img src="/captcha.php">', "画像認証"),
        ]:
            plan = plan_of(base + extra)
            self.assertEqual(plan.mode, "manual", extra)
            self.assertIn(word, plan.reason)

    def test_forms_that_need_javascript_or_unknown_values_are_manual(self) -> None:
        cases = {
            "JavaScript": PLAIN.format(token="a").replace(
                '<input type="submit" name="confirm" value="確認画面へ">', '<input type="button" value="送信">'
            ),
            "必須の欄": PLAIN.format(token="a")
            .replace("<tr><th>電話番号</th>", "<tr><th>ご紹介者 必須</th>")
            .replace('name="tel"', 'name="shokai"'),
            "外部": PLAIN.format(token="a").replace('action="/contact/send.php"', 'action="https://form.run/x"'),
            "POST": PLAIN.format(token="a").replace('method="post"', 'method="get"'),
        }
        for word, html in cases.items():
            plan = plan_of(html)
            self.assertEqual(plan.mode, "manual", word)
            self.assertIn(word, plan.reason)
        outside = '<input name="name"><input type="email" name="mail"><textarea name="body"></textarea>'
        self.assertEqual(plan_of(outside).mode, "manual")
        iframe = '<iframe src="https://docs.google.com/forms/d/e/x/viewform"></iframe>'
        self.assertEqual((plan_of(iframe).mode, plan_of(iframe).kind), ("manual", "external"))

    def test_reservation_and_recruit_forms_are_never_sent(self) -> None:
        reserve = PLAIN.format(token="a").replace(
            "<tr><th>電話番号</th>",
            '<tr><th>ご利用人数 必須</th><td><input type="number" name="n"></td></tr>'
            '<tr><th>日にち 必須</th><td><input type="date" name="d"></td></tr><tr><th>電話番号</th>',
        )
        self.assertEqual(plan_of(reserve).mode, "skip")
        self.assertIn("予約", plan_of(reserve).reason)
        recruit = PLAIN.format(token="a").replace(
            "<table>", "<p>応募フォーム</p><p>希望職種</p><p>年齢</p><p>面接希望日</p><table>"
        )
        self.assertEqual(plan_of(recruit).mode, "skip")

    def test_search_and_login_forms_are_ignored(self) -> None:
        self.assertIsNone(plan_of('<form action="/search"><input name="q"><input type="submit"></form>'))
        self.assertIsNone(
            plan_of(
                '<form method="post"><input name="id"><input type="password" name="pw">'
                '<textarea name="memo"></textarea></form>'
            )
        )

    def test_category_is_chosen_honestly_or_left_to_a_person(self) -> None:
        def with_select(options: str, required: str = "必須") -> str:
            return PLAIN.format(token="a").replace(
                "<tr><th>電話番号</th>",
                f'<tr><th>お問い合わせ種別 {required}</th><td><select name="kind"><option value="">選択</option>'
                f"{options}</select></td></tr><tr><th>電話番号</th>",
            )

        plan = plan_of(with_select("<option>ご予約</option><option>採用について</option><option>その他</option>"))
        item = next(i for i in plan.items if i["name"] == "kind")
        self.assertEqual((plan.mode, item["choice"], plan.flags), ("auto", "その他", ["category"]))
        plan = plan_of(with_select("<option>その他</option><option>サービスのご提案</option>"))
        self.assertEqual(next(i for i in plan.items if i["name"] == "kind")["choice"], "サービスのご提案")
        # 選べるものが無ければ、人が送る
        plan = plan_of(with_select("<option>ご予約</option><option>採用について</option>"))
        self.assertEqual(plan.mode, "manual")
        # 任意なら選ばずに送る
        plan = plan_of(with_select("<option>ご予約</option><option>その他</option>", required=""))
        self.assertEqual((plan.mode, next(i for i in plan.items if i["name"] == "kind")["role"]), ("auto", "default"))

    def test_consent_is_checked_and_newsletter_is_not(self) -> None:
        html = PLAIN.format(token="a").replace(
            '<input type="submit"',
            '<label><input type="checkbox" name="agree" value="1">個人情報の取扱いに同意する</label>'
            '<label><input type="checkbox" name="mm" value="1">メールマガジンを受け取る</label><input type="submit"',
        )
        plan = plan_of(html)
        self.assertEqual((roles(plan).get("agree"), "mm" in roles(plan), plan.flags), ("check", False, ["consent"]))
        self.assertIn(("agree", "1"), fill(plan.items, SENDER, "件名", "本文"))

    def test_fill_puts_values_in_the_right_fields(self) -> None:
        html = """<form method="post" action="/s"><dl>
          <dt>お名前</dt><dd><input name="n_a"><input name="n_b"></dd>
          <dt>フリガナ</dt><dd><input name="kana"></dd>
          <dt>ふりがな（ひらがな）</dt><dd><input name="hira"></dd>
          <dt>電話番号</dt><dd><input name="t1">-<input name="t2">-<input name="t3"></dd>
          <dt>郵便番号</dt><dd><input name="zip"></dd><dt>ご住所</dt><dd><input name="addr"></dd>
          <dt>都道府県</dt><dd><select name="pref"><option value="">--</option><option value="27">大阪府</option></select></dd>
          <dt>件名</dt><dd><input name="title"></dd><dt>ホームページ</dt><dd><input name="hp"></dd>
          <dt>内容</dt><dd><textarea name="msg"></textarea></dd></dl><button>送信する</button></form>"""
        plan = plan_of(html)
        self.assertEqual(plan.mode, "auto", plan.reason)
        got = dict(fill(plan.items, SENDER, "件名です", "本文です"))
        self.assertEqual(
            got,
            {
                "n_a": "山田",
                "n_b": "太郎",
                "kana": "ヤマダ タロウ",
                "hira": "やまだ たろう",
                "t1": "06",
                "t2": "1234",
                "t3": "5678",
                "zip": "530-0001",
                "addr": "大阪府大阪市北区1-1",
                "pref": "27",
                "title": "件名です",
                "hp": "",
                "msg": "本文です",
            },
        )
        with self.assertRaises(FormValueError):
            fill(plan.items, dict(SENDER, person="山田"), "s", "b")  # 姓と名に分けられない
        # 見出しが別々でも、「姓」「名」と書いてあれば分ける。名が先のフォームにも合わせる
        split = plan_of(
            '<form method="post" action="/s"><table><tr><th>名</th><td><input name="first_name"></td></tr>'
            '<tr><th>姓</th><td><input name="last_name"></td></tr></table>'
            '<textarea name="m"></textarea><input type="submit" value="送信"></form>'
        )
        self.assertEqual(
            dict(fill(split.items, SENDER, "s", "b")), {"first_name": "太郎", "last_name": "山田", "m": "b"}
        )

    def test_signature_ignores_tokens_but_notices_new_fields(self) -> None:
        a = plan_of(PLAIN.format(token="aaa")).signature()
        self.assertEqual(a, plan_of(PLAIN.format(token="bbb")).signature())
        changed = PLAIN.format(token="aaa").replace(
            "<tr><th>電話番号</th>", '<tr><th>部署</th><td><input name="dept"></td></tr><tr><th>電話番号</th>'
        )
        self.assertNotEqual(a, plan_of(changed).signature())

    def test_judge_reads_the_result_page(self) -> None:
        body = "突然のご連絡失礼いたします。株式会社テストの山田と申します。"
        ours = ("sales@sender.test",)

        def state(html: str, url: str = URL, status: int = 200, **kw) -> str:
            return judge(html, url, status, "body", body, ours=ours, **kw)[0]

        self.assertEqual(state("<p>お問い合わせありがとうございました。</p>"), "sent")
        self.assertEqual(state("<p>トップページ</p>", "https://shop.test/contact/thanks.html", redirected=True), "sent")
        # URL に thanks とあるだけ（転送されていない）、丁寧なエラー画面、否定形は、送れたことにしない
        self.assertEqual(state("<p>トップページ</p>", "https://shop.test/thanks.php"), "unknown")
        self.assertEqual(
            state("<p>トップ</p>", "https://shop.test/success-story/abandoned/", redirected=True), "unknown"
        )
        self.assertEqual(
            state("<p>お問い合わせありがとうございます。このお問い合わせは送信できませんでした。</p>"), "unknown"
        )
        self.assertEqual(state("<p>送信が完了しませんでした。</p>"), "unknown")
        # こちらの入力が残ったまま入力画面に戻されたら、送れていない
        echoed = PLAIN.format(token="a").replace('name="mail"', 'name="mail" value="sales@sender.test"')
        self.assertEqual(state("<p>お問い合わせありがとうございます。</p>" + echoed), "failed")
        self.assertEqual(state(PLAIN.format(token="a").replace("</textarea>", body + "</textarea>")), "failed")
        self.assertEqual(state("<p>入力内容に誤りがあります</p>" + PLAIN.format(token="a")), "failed")
        # 完了の表示と一緒に空の入力フォームを出し直すサイト。届いているかもしれないので、failed にはしない
        self.assertEqual(state("<p>送信が完了しました。</p>" + PLAIN.format(token="a")), "unknown")
        self.assertEqual(
            state('<form class="wpcf7-form sent" data-status="sent"><textarea name="body"></textarea></form>'), "sent"
        )
        self.assertEqual(
            state('<form class="wpcf7-form invalid" data-status="invalid"><textarea name="body"></textarea></form>'),
            "failed",
        )
        # 送信を受け付けたあとの転送先が 404 でも、送れていないとは言えない
        self.assertEqual(state("", status=404, redirected=True), "unknown")
        self.assertEqual(state("", status=403), "failed")
        self.assertEqual(state("", status=502), "unknown")
        self.assertEqual(state("<p>こんにちは</p>"), "unknown")

        def confirm(buttons: str, extra: str = "") -> str:
            return (
                f"<p>お問い合わせありがとうございます。以下の内容でよろしければ送信してください</p>"
                f'<form method="post" action="/contact/send.php"><input type="hidden" name="body" value="{body}">'
                f'<input type="hidden" name="step" value="2">{extra}{buttons}</form>'
            )

        page = confirm(
            '<input type="submit" name="back" value="入力画面へ戻って確認する">'
            '<input type="submit" name="send" value="送信する">'
        )
        result, _, nxt = judge(page, URL, 200, "body", body)
        self.assertEqual(result, "confirm")
        self.assertEqual(nxt["pairs"], [("body", body), ("step", "2"), ("send", "送信する")])
        self.assertEqual(state(page, allow_confirm=False), "unknown")
        # 戻る以外のボタンが1つだけなら、文字が何でもそれを押す
        result, _, nxt = judge(
            confirm('<input type="submit" name="go" value="この内容で問い合わせる">'), URL, 200, "body", body
        )
        self.assertEqual((result, nxt["pairs"][-1]), ("confirm", ("go", "この内容で問い合わせる")))
        # 自動では進められない確認画面は、送った扱いにせず、人に回す
        self.assertEqual(state(confirm('<input type="image" src="send.png" name="send">')), "stuck")
        self.assertEqual(
            state(confirm('<input type="submit" value="送信する">', '<input type="checkbox" name="agree">同意する')),
            "stuck",
        )
        self.assertEqual(
            state(
                confirm('<input type="submit" name="a" value="送信する"><input type="submit" name="b" value="確定">')
            ),
            "stuck",
        )

    def test_encoding_follows_the_form(self) -> None:
        data, ctype = encode_pairs([("名前", "山田〜"), ("b", "1行目\n2行目")], "cp932", False)
        self.assertEqual(ctype, "application/x-www-form-urlencoded")
        self.assertEqual(
            parse_qsl(data.decode("ascii"), encoding="cp932"), [("名前", "山田～"), ("b", "1行目\r\n2行目")]
        )
        data, ctype = encode_pairs([("a", "あ")], "utf-8", True)
        self.assertIn("multipart/form-data; boundary=", ctype)
        self.assertIn('name="a"\r\n\r\nあ\r\n'.encode("utf-8"), data)
        with self.assertRaises(FormValueError):
            encode_pairs([("a", "🙂")], "cp932", False)


class _Web(http.server.BaseHTTPRequestHandler):
    sites: dict[str, dict[str, object]] = {}
    posts: dict[str, object] = {}  # site -> 関数(path, fields, headers) -> (status, body, headers)
    received: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
    drop_post = False  # 送信を受け取ったあと、返事をせずに切る
    post_encoding = "utf-8"

    def _site(self) -> str:
        return self.headers.get("Host", "").split(":")[0].replace(".localhost", "")

    def _reply(
        self, status: int, body: str, headers: dict[str, str] | None = None, ctype: str = "text/html; charset=utf-8"
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(self.path)
        body = self.sites.get(self._site(), {}).get(path)
        if body is None:
            self._reply(404, "")
        elif isinstance(body, bytes):  # Shift_JIS のページ
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=Shift_JIS")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._reply(200, str(body), ctype="text/plain" if path == "/robots.txt" else "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/"):
            msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
            fields = {
                p.get_param("name", header="content-disposition"): p.get_payload(decode=True).decode("utf-8")
                for p in msg.get_payload()
            }
        else:
            fields = dict(parse_qsl(raw.decode("ascii"), keep_blank_values=True, encoding=type(self).post_encoding))
        type(self).received.append((self._site(), self.path, fields, dict(self.headers)))
        if type(self).drop_post:
            self.connection.close()
            return
        handler = self.posts.get(self._site())
        if handler is None:
            self._reply(404, "")
            return
        status, body, headers = handler(self.path, fields, dict(self.headers))  # type: ignore[operator]
        self._reply(
            status, body, headers, ctype="application/json" if body.startswith("{") else "text/html; charset=utf-8"
        )

    def log_message(self, *args) -> None:
        pass


class _TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def thanks(path, fields, headers):  # noqa: ANN001
    return 200, "<p>送信が完了しました。ありがとうございました。</p>", {}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        real = socket.getaddrinfo

        def fake(host, *a, **k):
            if isinstance(host, str) and host.endswith(".localhost"):
                host = "127.0.0.1"
            return real(host, *a, **k)

        cls.dns = mock.patch("socket.getaddrinfo", fake)
        cls.dns.start()
        cls.web = _TCP(("127.0.0.1", 0), _Web)
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()
        cls.port = cls.web.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.web.shutdown()
        cls.web.server_close()
        cls.dns.stop()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "templates").mkdir()
        for path in (ROOT / "templates").glob("*.txt"):
            (self.root / "templates" / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self.env = {
            "SENDER_COMPANY": "株式会社テスト",
            "SENDER_PERSON": "山田 太郎",
            "SENDER_ADDRESS": "大阪府大阪市北区1-1",
            "SENDER_EMAIL": "sales@sender.test",
            "SENDER_TEL": "06-1234-5678",
            "SERVICE_START": "2027年春",
            "DAILY_LIMIT": "5",
            "SEND_INTERVAL_SEC": "0",
        }
        self.cfg = load_config(self.root, env=self.env)
        self.conn = db.connect(self.cfg.path("DB_PATH"))
        _Web.sites, _Web.posts, _Web.drop_post, _Web.post_encoding = {}, {}, False, "utf-8"
        _Web.received.clear()
        self.template = load_template(self.root / "templates" / "honbu_form.txt")
        self.out: list[str] = []
        self.crawler = Crawler(delay=0, allow_private=True)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    # ---- 部品 ----
    def site(
        self,
        name: str,
        contact: str,
        top: str = '<a href="/contact/">お問い合わせ</a>',
        segment: str = "honbu",
        post=thanks,
        robots: str | None = None,
    ) -> int:  # noqa: ANN001
        host = f"{name}.localhost"
        _Web.sites[name] = {"/": top, "/contact/": contact}
        if robots is not None:
            _Web.sites[name]["/robots.txt"] = robots
        _Web.posts[name] = post
        cur = self.conn.execute(
            "INSERT INTO companies(name, site_url, host, segment, status, created_at) VALUES (?,?,?,?,'new',?)",
            (f"{name}社", f"http://{host}:{self.port}/", host, segment, db.now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def crawl(self, recrawl: bool = False) -> None:
        crawl_pending(self.conn, self.crawler, recrawl=recrawl, log=self.out.append)

    def form(self, company_id: int):
        return self.conn.execute("SELECT * FROM forms WHERE company_id=?", (company_id,)).fetchone()

    def approve_all(self) -> None:
        self.conn.execute("UPDATE forms SET status='approved' WHERE mode='auto'")
        self.conn.commit()

    def send(self, **kw):
        return send_forms(
            self.conn,
            self.cfg,
            self.template,
            live=True,
            log=self.out.append,
            sleep=lambda s: None,
            crawler=self.crawler,
            **kw,
        )

    def posted(self, name: str) -> list[dict[str, str]]:
        return [fields for site, _, fields, _ in _Web.received if site == name]

    def sends(self, company_id: int) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute("SELECT status FROM form_sends WHERE company_id=? ORDER BY id", (company_id,))
        ]

    def cli(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main_for_test(self.cfg, self.conn, list(argv))
        return code, buf.getvalue()


class CrawlFindsForms(Base):
    def test_form_is_recorded_for_a_site_without_an_address(self) -> None:
        a = self.site("a", PLAIN.format(token="t1"))
        b = self.site("b", PLAIN.format(token="t1") + "<p>営業目的のお問い合わせはご遠慮ください。</p>")
        c = self.site(
            "c", CF7.format(api="", token="t") + '<script src="https://www.google.com/recaptcha/api.js"></script>'
        )
        d = self.site("d", PLAIN.format(token="t"), robots="User-agent: *\nDisallow: /contact/send.php\n")
        self.crawl()
        self.assertEqual((self.form(a)["mode"], self.form(a)["status"]), ("auto", "pending"))
        self.assertEqual(self.conn.execute("SELECT status FROM companies WHERE id=?", (b,)).fetchone()[0], "refused")
        self.assertEqual(self.form(c)["mode"], "manual")
        self.assertEqual(self.form(d)["mode"], "skip")
        self.approve_all()
        self.assertEqual([r["company_id"] for r in db.sendable_forms(self.conn)], [a])
        self.assertEqual([r["company_id"] for r in db.sendable_forms(self.conn, mode="manual")], [c])
        self.assertEqual(_Web.received, [])  # 巡回では何も送らない

    def test_frameset_pages_are_read(self) -> None:
        cid = self.site("f", "", top='<frameset><frame src="menu.htm"><frame src="main.htm"></frameset>')
        _Web.sites["f"]["/menu.htm"] = '<a href="/contact/">お問い合わせ</a>'
        _Web.sites["f"]["/main.htm"] = '<a href="mailto:info@f.localhost">メール</a>'
        _Web.sites["f"]["/contact/"] = PLAIN.format(token="t")
        self.crawl()
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT email FROM contacts WHERE company_id=?", (cid,))],
            ["info@f.localhost"],
        )
        self.assertEqual(self.form(cid)["mode"], "auto")
        # アドレスが確認待ちの間は、メールを優先する（フォームでは送らない）
        self.approve_all()
        self.assertEqual(db.sendable_forms(self.conn), [])

    def test_external_form_service_link_is_left_to_a_person(self) -> None:
        cid = self.site("g", "<p>店舗案内</p>", top='<a href="https://forms.gle/abc">お問い合わせフォーム</a>')
        self.crawl()
        row = self.form(cid)
        self.assertEqual((row["mode"], row["kind"], row["page_url"]), ("manual", "external", "https://forms.gle/abc"))


class AutoSend(Base):
    def test_draft_then_live_send(self) -> None:
        a = self.site("a", PLAIN.format(token="fresh"))
        self.crawl()
        self.assertEqual(self.cli("form", "send")[1].strip().splitlines()[0][:10], "自動で送れるフォーム")  # 未承認
        code, out = self.cli("form", "approve", "--unflagged")
        self.assertIn("承認 1件", out)
        s = send_forms(self.conn, self.cfg, self.template, log=self.out.append)
        self.assertEqual((s.dry_run, _Web.received), (1, []))
        draft = next((self.root / "outbox").rglob("form_*.txt")).read_text(encoding="utf-8")
        self.assertIn("お名前 必須: 山田 太郎", draft)
        self.assertIn("お問い合わせフォームからお送りしています", draft)

        _Web.sites["a"]["/contact/"] = PLAIN.format(token="newer")  # トークンは送る直前のものを使う
        s = self.send()
        self.assertEqual((s.sent, s.failed, s.unknown), (1, 0, 0))
        site, path, fields, headers = _Web.received[0]
        self.assertEqual(path, "/contact/send.php")
        self.assertEqual(
            {k: fields[k] for k in ("csrf", "company", "name", "mail", "tel", "website", "confirm")},
            {
                "csrf": "newer",
                "company": "株式会社テスト",
                "name": "山田 太郎",
                "mail": "sales@sender.test",
                "tel": "06-1234-5678",
                "website": "",
                "confirm": "確認画面へ",
            },
        )
        self.assertIn("a社", fields["body"])
        self.assertIn("sales@sender.test 宛に「不要」とお送りください", fields["body"])
        self.assertIn("株式会社テスト　山田 太郎", fields["body"])
        self.assertTrue(headers["Referer"].endswith("/contact/"))
        self.assertIn("AutoSalesBot", headers["User-Agent"])
        # 1社1回。もう一度動かしても送らない。今日の数にも入る
        self.assertEqual(self.send().sent, 0)
        self.assertEqual((len(_Web.received), db.sent_today(self.conn), self.sends(a)), (1, 1, ["dry_run", "sent"]))
        self.assertTrue(db.company_contacted(self.conn, a))

    def test_confirm_screen_is_followed_once(self) -> None:
        def post(path, fields, headers):  # noqa: ANN001
            if "send" in fields:
                return 200, "<p>送信しました。</p>", {}
            hidden = "".join(
                f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items() if k != "confirm"
            )
            return (
                200,
                (
                    f'<form method="post" action="/contact/send.php">{hidden}'
                    '<input type="submit" name="back" value="戻る"><input type="submit" name="send" value="送信する"></form>'
                ),
                {},
            )

        a = self.site("a", PLAIN.format(token="t"), post=post)
        self.crawl()
        self.approve_all()
        self.assertEqual(self.send().sent, 1)
        self.assertEqual([("send" in f, "back" in f) for f in self.posted("a")], [(False, False), (True, False)])
        self.assertEqual(self.posted("a")[1]["name"], "山田 太郎")
        self.assertEqual(self.sends(a), ["sent"])

    def test_failure_moves_the_form_to_manual_and_stays_there(self) -> None:
        a = self.site(
            "a",
            PLAIN.format(token="t"),
            post=lambda p, f, h: (200, "<p>入力内容に誤りがあります</p>" + PLAIN.format(token="t"), {}),
        )
        self.crawl()
        self.approve_all()
        s = self.send()
        self.assertEqual((s.sent, s.failed), (0, 1))
        self.assertEqual((self.form(a)["mode"], self.sends(a)), ("manual", ["failed"]))
        self.assertFalse(db.company_contacted(self.conn, a))
        self.conn.execute("UPDATE companies SET status='no_email' WHERE id=?", (a,))
        self.crawl(recrawl=True)
        self.assertEqual(self.form(a)["mode"], "manual")  # 読み直しても、自動には戻さない
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(len(self.posted("a")), 1)

    def test_changed_form_is_not_sent(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        _Web.sites["a"]["/contact/"] = PLAIN.format(token="t").replace(
            "<tr><th>電話番号</th>", '<tr><th>ご予算</th><td><input name="budget"></td></tr><tr><th>電話番号</th>'
        )
        s = self.send()
        self.assertEqual((s.sent, s.failed, _Web.received), (0, 0, []))
        self.assertEqual((self.form(a)["status"], self.sends(a)), ("pending", []))
        self.assertIn("budget", self.form(a)["plan"])  # 新しい作りを控え直している

    def test_refusal_added_after_approval_stops_the_send(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        _Web.sites["a"]["/contact/"] = PLAIN.format(token="t") + "<p>※営業・勧誘のご連絡は固くお断りします</p>"
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(_Web.received, [])
        self.assertEqual(self.conn.execute("SELECT status FROM companies WHERE id=?", (a,)).fetchone()[0], "refused")

    def test_lost_connection_after_post_is_never_resent(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        _Web.drop_post = True
        s = self.send()
        self.assertEqual((s.sent, s.unknown, self.sends(a)), (0, 1, ["unknown"]))
        _Web.drop_post = False
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(len(self.posted("a")), 1)
        self.assertEqual(db.sent_today(self.conn), 1)

    def test_cf7_goes_through_its_own_endpoint(self) -> None:
        api = f"http://a.localhost:{self.port}/wp-json/"
        replies = {"ok": '{"status": "mail_sent", "message": "ありがとうございます"}'}
        a = self.site("a", CF7.format(api=api, token="t"), post=lambda p, f, h: (200, replies["ok"], {}))
        b = self.site(
            "b",
            CF7.format(api=api.replace("a.", "b."), token="t"),
            post=lambda p, f, h: (
                200,
                '{"status": "validation_failed", "message": "入力内容に問題があります", "invalid_fields": [{"field": "your-name"}]}',
                {},
            ),
        )
        self.crawl()
        self.approve_all()
        s = self.send()
        self.assertEqual((s.sent, s.failed), (1, 1))
        site, path, fields, headers = _Web.received[0]
        self.assertEqual(path, "/wp-json/contact-form-7/v1/contact-forms/12/feedback")
        self.assertIn("multipart/form-data", headers["Content-Type"])
        self.assertEqual(
            (fields["_wpcf7"], fields["your-name"], fields["your-email"]), ("12", "山田 太郎", "sales@sender.test")
        )
        self.assertEqual((self.sends(a), self.form(b)["mode"]), (["sent"], "manual"))
        self.assertIn("your-name", self.form(b)["reason"])

    def test_cp932_site_gets_cp932(self) -> None:
        a = self.site("a", PLAIN.format(token="t").encode("cp932"))
        _Web.post_encoding = "cp932"
        self.crawl()
        self.approve_all()
        self.assertEqual(self.send().sent, 1)
        fields = self.posted("a")[0]
        self.assertEqual((fields["name"], fields["company"]), ("山田 太郎", "株式会社テスト"))
        self.assertIn("お問い合わせフォームからお送りしています", fields["body"])
        self.assertEqual(self.sends(a), ["sent"])


class SharedRules(Base):
    def _mail_contact(self, company_id: int, addr: str, status: str = "approved") -> None:
        self.conn.execute(
            "INSERT INTO contacts(company_id, email, source_url, status, found_at) VALUES (?,?,?,?,?)",
            (company_id, addr, "https://x.test/", status, db.now()),
        )
        self.conn.execute("UPDATE companies SET status='crawled' WHERE id=?", (company_id,))
        self.conn.commit()

    def test_one_contact_per_company_across_mail_and_form(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        b = self.site("b", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        # a社はメールで送れる → フォームでは送らない
        self._mail_contact(a, "info@a.localhost")
        self.assertEqual([r["company_id"] for r in db.sendable_forms(self.conn)], [b])
        # b社にフォームで送ったあとにアドレスが見つかっても、メールは送らない
        self.assertEqual(self.send().sent, 1)
        self._mail_contact(b, "info@b.localhost")
        self.assertEqual([r["email"] for r in db.sendable_contacts(self.conn)], ["info@a.localhost"])
        # アドレスを見送りにした会社は、フォームに回せる
        self.conn.execute("UPDATE contacts SET status='rejected' WHERE email='info@a.localhost'")
        self.assertEqual([r["company_id"] for r in db.sendable_forms(self.conn)], [a])

    def test_daily_limit_is_shared_with_mail(self) -> None:
        ids = [self.site(n, PLAIN.format(token="t")) for n in ("a", "b", "c")]
        self.crawl()
        self.approve_all()
        cfg = load_config(self.root, env=dict(self.env, DAILY_LIMIT="2"))
        # メールで1通送った扱いにする
        other = self.conn.execute(
            "INSERT INTO companies(name, site_url, host, status, created_at) "
            "VALUES ('m','http://m.test/','m.test','crawled',?)",
            (db.now(),),
        ).lastrowid
        ct = self.conn.execute(
            "INSERT INTO contacts(company_id, email, source_url, status, found_at) VALUES (?,?,?,?,?)",
            (other, "i@m.test", "u", "approved", db.now()),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO sends(contact_id, template, subject, status, sent_at) VALUES (?,?,?,?,?)",
            (ct, "honbu", "s", "sent", db.now()),
        )
        self.conn.commit()
        s = send_forms(
            self.conn, cfg, self.template, live=True, log=self.out.append, sleep=lambda x: None, crawler=self.crawler
        )
        self.assertEqual((s.sent, s.skipped_limit, db.sent_today(self.conn)), (1, 2, 2))
        self.assertEqual(len(_Web.received), 1)
        del ids

    def test_stop_list_and_refusal_block_forms(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        b = self.site("b", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        db.add_suppression(self.conn, "a.localhost", "手動")
        self.conn.execute("UPDATE companies SET status='refused' WHERE id=?", (b,))
        self.assertEqual(db.sendable_forms(self.conn), [])
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(_Web.received, [])
        self.assertEqual(
            self.cli("form", "approve", "--ids", str(self.form(b)["id"]))[1].splitlines()[0], "承認 0件 / 見送り 0件"
        )
        del a

    def test_missing_setting_skips_only_the_forms_that_need_it(self) -> None:
        need_kana = PLAIN.format(token="t").replace(
            "<tr><th>電話番号</th>", '<tr><th>フリガナ 必須</th><td><input name="kana"></td></tr><tr><th>電話番号</th>'
        )
        a = self.site("a", need_kana)
        self.site("b", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        self.assertEqual(self.send().sent, 1)
        self.assertEqual((self.posted("a"), len(self.posted("b"))), ([], 1))
        self.assertTrue(any("SENDER_PERSON_KANA" in line for line in self.out))
        self.assertEqual(self.form(a)["mode"], "auto")  # 設定すれば送れるので、自動のまま

    def test_reply_from_a_form_contact_is_recorded_and_optout_blocks(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        b = self.site("b", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        self.assertEqual(self.send(limit=1).sent, 1)

        def mail(sender: str, text: str, mid: str) -> str | None:
            raw = (
                f"From: {sender}\r\nTo: sales@sender.test\r\nSubject: Re\r\nMessage-ID: <{mid}@x>\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n\r\n{text}\r\n"
            ).encode("utf-8")
            return process_message(self.conn, raw, {"sales@sender.test"})

        self.assertEqual(mail("tantou@a.localhost", "2", "m1"), "reply")
        row = self.conn.execute("SELECT contact_id, company_id, choice FROM replies").fetchone()
        self.assertEqual(tuple(row), (None, a, 2))
        self.assertIsNone(mail("someone@b.localhost", "2", "m2"))  # b社にはまだ送っていない
        self.assertIsNone(mail("x@gmail.com", "2", "m3"))
        self.assertEqual(mail("boss@a.localhost", "不要です", "m4"), "optout")
        self.assertTrue(db.is_suppressed(self.conn, "anyone@a.localhost"))
        code, out = self.cli("stats")
        self.assertIn("送信 1社（うちフォーム 1社）/ 返信 1社 / 停止 1社", out)
        self.assertIn("番号 1:0 2:1", out)
        code, _ = self.cli("export")
        with open(self.root / "data" / "list.csv", encoding="utf-8-sig") as f:
            rows = {r["会社名"]: r for r in csv.DictReader(f)}
        self.assertEqual(
            (rows["a社"]["フォームの送り方"], bool(rows["a社"]["フォーム送信日時"]), rows["b社"]["フォーム送信日時"]),
            ("自動", True, ""),
        )
        self.assertEqual(rows["a社"]["フォームへの反応"], "optout")
        del b

    def test_review_csv_round_trip(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        c = self.site("c", CF7.format(api="", token="t") + '<div class="g-recaptcha"></div>')
        self.crawl()
        code, out = self.cli("form", "review")
        self.assertIn("自動 1件 / 人が送る 1件", out)
        path = self.root / "data" / "form_review.csv"
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["会社名"], r["送り方"]) for r in rows], [("a社", "自動"), ("c社", "人が送る")])
        self.assertIn("お名前 必須→担当者名", rows[0]["入力の内容"])
        self.assertIn("reCAPTCHA", rows[1]["人が送る理由"])
        rows[0]["判定"] = "ＯＫ"
        rows[1]["判定"] = "ok"
        rows[1]["フォームのページ"] = "https://other.test/"  # 行がずれたCSV
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        code, out = self.cli("form", "approve", "--csv", str(path))
        self.assertEqual(code, 1)
        self.assertIn("承認 1件", out)
        self.assertEqual((self.form(a)["status"], self.form(c)["status"]), ("approved", "pending"))


class AssistScreen(Base):
    def setUp(self) -> None:
        super().setUp()
        guarded = CF7.format(api="", token="t") + '<script src="https://www.google.com/recaptcha/api.js"></script>'
        self.a = self.site("a", guarded)
        self.b = self.site("b", guarded)
        self.crawl()
        self.app = Assist(self.conn, self.cfg, self.template)
        self.app.port = 8123
        self.host = "127.0.0.1:8123"
        self.base = f"/{self.app.token}/"

    def get(self) -> str:
        status, _, body = self.app.handle("GET", self.base, self.host)
        self.assertEqual(status, 200)
        return body.decode("utf-8")

    def mark(self, company_id: int, action: str, note: str = "") -> str:
        body = f"form_id={self.form(company_id)['id']}&action={action}&note={note}".encode("utf-8")
        status, headers, _ = self.app.handle("POST", self.base + "mark", self.host, body)
        self.assertEqual((status, headers["Location"]), (303, self.base))
        return self.get()

    def test_page_shows_the_draft_and_never_posts_anywhere(self) -> None:
        page = self.get()
        for text in (
            "a社",
            "reCAPTCHA",
            "株式会社テスト",
            "山田 太郎",
            "sales@sender.test",
            "06-1234-5678",
            "お問い合わせフォームからお送りしています",
            "残り 2社",
            'rel="noopener noreferrer"',
        ):
            self.assertIn(text, page)
        self.assertEqual(_Web.received, [])

    def test_only_the_right_address_and_token_can_open_it(self) -> None:
        self.assertEqual(self.app.handle("GET", "/wrong/", self.host)[0], 404)
        self.assertEqual(self.app.handle("GET", self.base, "evil.test")[0], 404)
        self.assertEqual(self.app.handle("POST", "/wrong/mark", self.host, b"form_id=1&action=sent")[0], 404)
        self.assertEqual(self.sends(self.a), [])
        headers = self.app.handle("GET", self.base, self.host)[1]
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_buttons_record_the_result(self) -> None:
        page = self.mark(self.a, "sent")
        self.assertIn("a社 を送信済みにしました", page)
        self.assertIn("b社", page)
        self.assertEqual((self.sends(self.a), db.sent_today(self.conn)), (["manual"], 1))
        self.assertTrue(db.company_contacted(self.conn, self.a))
        self.assertIn("すでに連絡済み", self.mark(self.a, "sent"))  # 押し直しても二重に数えない
        self.assertEqual(self.sends(self.a), ["manual"])
        self.assertIn("もうありません", self.mark(self.b, "later"))
        self.assertEqual([r["company_id"] for r in Assist(self.conn, self.cfg, self.template).targets()], [self.b])
        self.mark(self.b, "reject", "予約用")
        self.assertEqual(
            (self.form(self.b)["status"], Assist(self.conn, self.cfg, self.template).targets()), ("rejected", [])
        )
        self.assertIn("送信 1社（うちフォーム 1社）", self.cli("stats")[1])

    def test_daily_limit_closes_the_screen(self) -> None:
        cfg = load_config(self.root, env=dict(self.env, DAILY_LIMIT="1"))
        app = Assist(self.conn, cfg, self.template)
        app.port = 8123
        app.mark(self.form(self.a)["id"], "sent")
        page = app.handle("GET", f"/{app.token}/", self.host)[2].decode("utf-8")
        self.assertIn("今日の上限に達しました", page)
        self.assertNotIn("b社", page)

    def test_mail_send_also_respects_form_contacts(self) -> None:
        self.app.mark(self.form(self.a)["id"], "sent")
        self.conn.execute(
            "INSERT INTO contacts(company_id, email, source_url, status, found_at) VALUES (?,?,?,?,?)",
            (self.a, "info@a.localhost", "u", "approved", db.now()),
        )
        self.conn.execute("UPDATE companies SET status='crawled' WHERE id=?", (self.a,))
        self.conn.commit()
        mail_template = load_template(self.root / "templates" / "honbu.txt")
        s = send_batch(self.conn, self.cfg, mail_template, log=self.out.append)
        self.assertEqual(s.dry_run, 0)


if __name__ == "__main__":
    unittest.main()

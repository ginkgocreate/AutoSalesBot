"""通しの確認。外には一切つながず、手元に立てた仮のWebサーバーとSMTPサーバーだけを使う。

python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import email
import email.policy
import http.server
import socketserver
import socket
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from autosales import db
from autosales.config import load_config
from autosales.crawler import Crawler, crawl_pending, is_non_official
from autosales.extract import classify_email, extract_emails, find_refusal, has_obfuscated_address, parse_html
from autosales.importer import import_csv
from autosales.mailer import TemplateError, load_template, render, send_batch
from autosales.replies import classify_reply, process_message

ROOT = Path(__file__).resolve().parent.parent

SITES = {
    # 普通の会社: トップにリンク、会社概要にアドレス、求人ページに応募用アドレス
    "ok": {
        "/": '<html><head><title>OK社</title></head><body><a href="/company">会社概要</a>'
        '<a href="/recruit">求人</a><a href="/menu">メニュー</a><img src="/logo@2x.png"></body></html>',
        "/company": "<html><body><p>お問い合わせ: ｉｎｆｏ＠OK.localhost</p>"
        '<a href="mailto:info@ok.localhost?subject=hi">mail</a></body></html>',
        "/recruit": "<html><head><title>求人</title></head><body>応募は saiyo@ok.localhost まで</body></html>",
        "/menu": "<html><body>menu@ok.localhost</body></html>",
    },
    # 営業お断りの表示がある会社
    "refuse": {
        "/": '<html><body><a href="/contact">お問い合わせ</a></body></html>',
        "/contact": "<html><body>info@refuse.localhost<p>※営業・セールス目的のメールは固くお断りいたします。</p></body></html>",
    },
    # robots.txt で全面禁止
    "robots": {
        "/robots.txt": "User-agent: *\nDisallow: /\n",
        "/": "<html><body>info@robots.localhost</body></html>",
    },
    # アドレスを崩して書いている
    "obf": {"/": "<html><body>連絡先: info[at]obf.localhost</body></html>"},
    # Shift_JIS のページ
    "sjis": {
        "/": '<html><head><meta charset="Shift_JIS"></head><body>連絡先 info@sjis.localhost 会社概要</body></html>'
    },
}


class _SiteHandler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        site = self.headers.get("Host", "").split(".")[0]
        type(self).hits.append(f"{site}{self.path}")
        pages = SITES.get(site, {})
        body = pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        if site == "sjis":
            data, ctype = body.encode("cp932"), "text/html"
        elif self.path == "/robots.txt":
            data, ctype = body.encode(), "text/plain"
        else:
            data, ctype = body.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


class _FakeSMTP(socketserver.StreamRequestHandler):
    """最小限のSMTP。受け取ったメールを server.messages にためる。"""

    def handle(self) -> None:
        self.wfile.write(b"220 fake ESMTP\r\n")
        rcpt: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            upper = cmd.upper()
            if upper.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-fake\r\n250 8BITMIME\r\n")
            elif upper.startswith("MAIL FROM"):
                rcpt = []
                self.wfile.write(b"250 ok\r\n")
            elif upper.startswith("RCPT TO"):
                addr = cmd.split(":", 1)[1].strip().strip("<>")
                if addr.startswith("gone@"):
                    self.wfile.write(b"550 no such user\r\n")
                else:
                    rcpt.append(addr)
                    self.wfile.write(b"250 ok\r\n")
            elif upper == "DATA":
                self.wfile.write(b"354 go\r\n")
                chunks = []
                while True:
                    data = self.rfile.readline()
                    if data in (b".\r\n", b""):
                        break
                    chunks.append(data[1:] if data.startswith(b"..") else data)
                self.server.messages.append((rcpt, b"".join(chunks)))  # type: ignore[attr-defined]
                self.wfile.write(b"250 queued\r\n")
            elif upper == "RSET":
                self.wfile.write(b"250 ok\r\n")
            elif upper == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"502 not implemented\r\n")


class _ThreadedTCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ExtractTests(unittest.TestCase):
    def test_emails_and_junk(self) -> None:
        page = parse_html(
            '<a href="mailto:Info@Shop.test?subject=x">m</a> <img src="a@2x.png">'
            "<script>var a='hidden@script.test'</script> ｓａｌｅｓ＠shop.test noreply@shop.test",
            "https://shop.test/",
        )
        self.assertEqual(extract_emails(page), ["info@shop.test", "sales@shop.test"])

    def test_adjacent_inline_tags_do_not_glue_text(self) -> None:
        page = parse_html(
            '<div><span>MAIL</span><a href="mailto:info@shop.test">info@shop.test</a></div>', "https://shop.test/"
        )
        self.assertEqual(extract_emails(page), ["info@shop.test"])

    def test_refusal_variants(self) -> None:
        for text in [
            "営業メールはお断りしております",
            "セールス目的のご連絡はご遠慮ください",
            "当フォームからの売り込みは禁止です",
            "お断り：広告・宣伝のメール",
        ]:
            self.assertIsNotNone(find_refusal(parse_html(f"<p>{text}</p>", "https://a.test/")), text)
        for text in ["営業時間 19:00〜LAST 定休日 月曜", "無断転載を禁止します", "ご予約はお電話で"]:
            self.assertIsNone(find_refusal(parse_html(f"<p>{text}</p>", "https://a.test/")), text)

    def test_obfuscated_is_not_harvested(self) -> None:
        page = parse_html("<p>info[at]shop.test / info★shop.test</p>", "https://shop.test/")
        self.assertEqual(extract_emails(page), [])
        self.assertTrue(has_obfuscated_address(page))

    def test_flags(self) -> None:
        self.assertEqual(classify_email("info@shop.test", "shop.test"), [])
        self.assertEqual(classify_email("info@shop.test", "bar.shop.test"), [])
        self.assertEqual(classify_email("recruit@shop.test", "shop.test"), ["recruit"])
        self.assertEqual(classify_email("x@gmail.com", "shop.test"), ["free_mail"])
        self.assertEqual(classify_email("x@webstudio.test", "shop.test"), ["offsite_domain"])

    def test_portals_are_not_official(self) -> None:
        self.assertTrue(is_non_official("www.instagram.com"))
        self.assertTrue(is_non_official("naramori.com"))
        self.assertFalse(is_non_official("example.co.jp"))


class ReplyTests(unittest.TestCase):
    def test_classify(self) -> None:
        self.assertEqual(classify_reply("1\n\n> 近いものを、番号だけ\n> 4 今は必要ない").choice, 1)
        self.assertEqual(classify_reply("３番です。よろしくお願いします").choice, 3)
        r = classify_reply("不要です。\n\n2026年11月14日 12:00 info@x.test wrote:\n> 1 業者への")
        self.assertEqual((r.kind, r.choice), ("optout", None))
        r = classify_reply("お世話になります。3店舗あります。詳しく聞きたいです。")
        self.assertEqual((r.kind, r.choice), ("reply", None))
        self.assertEqual(classify_reply("4").kind, "reply")


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 「○○.localhost」を手元の仮サーバーに向ける（OSによっては名前が引けないため）
        real = socket.getaddrinfo

        def fake(host, *args, **kwargs):
            if isinstance(host, str) and host.endswith(".localhost"):
                host = "127.0.0.1"
            return real(host, *args, **kwargs)

        cls.dns = mock.patch("socket.getaddrinfo", fake)
        cls.dns.start()
        cls.web = _ThreadedTCP(("127.0.0.1", 0), _SiteHandler)
        cls.smtp = _ThreadedTCP(("127.0.0.1", 0), _FakeSMTP)
        cls.smtp.messages = []  # type: ignore[attr-defined]
        for srv in (cls.web, cls.smtp):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.web_port = cls.web.server_address[1]
        cls.smtp_port = cls.smtp.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        for srv in (cls.web, cls.smtp):
            srv.shutdown()
            srv.server_close()
        cls.dns.stop()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "templates").mkdir()
        for name in ("honbu.txt", "show.txt"):
            (self.root / "templates" / name).write_text(
                (ROOT / "templates" / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        self.env = {
            "SENDER_COMPANY": "株式会社テスト",
            "SENDER_PERSON": "山田",
            "SENDER_ADDRESS": "奈良県奈良市1-1",
            "SENDER_TEL": "0742-00-0000",
            "SENDER_EMAIL": "sales@sender.test",
            "SERVICE_START": "2027年春",
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(self.smtp_port),
            "SMTP_SECURITY": "none",
            "DAILY_LIMIT": "2",
            "SEND_INTERVAL_SEC": "0",
        }
        self.cfg = load_config(self.root, env=self.env)
        self.conn = db.connect(self.cfg.path("DB_PATH"))
        self.smtp.messages.clear()  # type: ignore[attr-defined]
        _SiteHandler.hits.clear()
        self.log: list[str] = []

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _seed(self, names: list[str]) -> None:
        path = self.root / "seeds.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["会社名", "サイト", "エリア", "業態", "店舗数", "層"])
            for n in names:
                w.writerow([f"{n}社", f"http://{n}.localhost:{self.web_port}/", "奈良市", "ラウンジ", "2", "honbu"])
            w.writerow(["インスタだけの店", "https://www.instagram.com/someshop/", "", "", "", ""])
            w.writerow(["", "https://noname.test/", "", "", "", ""])
        self.counts = import_csv(self.conn, path)

    def _crawl(self) -> dict[str, int]:
        crawler = Crawler(delay=0, max_pages=6, allow_private=True)
        return crawl_pending(self.conn, crawler, log=self.log.append)

    def _status(self, name: str) -> str:
        return self.conn.execute("SELECT status FROM companies WHERE name=?", (f"{name}社",)).fetchone()[0]

    def test_import_and_crawl(self) -> None:
        self._seed(["ok", "refuse", "robots", "obf", "sjis"])
        self.assertEqual(self.counts, {"added": 5, "duplicate": 0, "invalid": 1, "non_official": 1})
        self._crawl()
        self.assertEqual(self._status("ok"), "crawled")
        self.assertEqual(self._status("refuse"), "refused")
        self.assertEqual(self._status("robots"), "blocked")
        self.assertEqual(self._status("obf"), "no_email")
        self.assertEqual(self._status("sjis"), "crawled")
        # robots.txt で禁止されたサイトは、robots.txt 以外を1ページも読んでいない
        self.assertEqual([h for h in _SiteHandler.hits if h.startswith("robots/")], ["robots/robots.txt"])
        # 「メニュー」のような関係ないページは見に行かない
        self.assertNotIn("ok/menu", _SiteHandler.hits)
        rows = {r["email"]: r for r in self.conn.execute("SELECT * FROM contacts")}
        self.assertEqual(sorted(rows), ["info@ok.localhost", "info@sjis.localhost", "saiyo@ok.localhost"])
        self.assertIn("recruit", rows["saiyo@ok.localhost"]["flags"])
        self.assertTrue(rows["info@ok.localhost"]["source_url"].endswith("/company"))
        self.assertTrue(all(r["status"] == "pending" for r in rows.values()))
        # お断りの会社のアドレスは連絡先に入れない
        self.assertNotIn("info@refuse.localhost", rows)

    def test_nothing_is_sendable_without_approval(self) -> None:
        self._seed(["ok"])
        self._crawl()
        self.assertEqual(db.sendable_contacts(self.conn), [])

    def _approve_all(self) -> None:
        self.conn.execute("UPDATE contacts SET status='approved' WHERE flags=''")
        self.conn.commit()

    def test_dry_run_then_live_send(self) -> None:
        self._seed(["ok", "sjis", "refuse"])
        self._crawl()
        self._approve_all()
        template = load_template(self.root / "templates" / "honbu.txt")

        s = send_batch(self.conn, self.cfg, template, live=False, log=self.log.append)
        self.assertEqual(s.dry_run, 2)
        self.assertEqual(len(self.smtp.messages), 0)  # type: ignore[attr-defined]
        emls = sorted((self.root / "outbox").rglob("*.eml"))
        self.assertEqual(len(emls), 2)
        # 下書きは「送った」に数えない
        self.assertEqual(len(db.sendable_contacts(self.conn)), 2)

        s = send_batch(self.conn, self.cfg, template, live=True, log=self.log.append, sleep=lambda _: None)
        self.assertEqual((s.sent, s.failed), (2, 0))
        got = self.smtp.messages  # type: ignore[attr-defined]
        self.assertEqual(sorted(r[0] for r, _ in got), ["info@ok.localhost", "info@sjis.localhost"])
        msg = email.message_from_bytes(got[0][1], policy=email.policy.default)
        body = msg.get_content()
        self.assertEqual(msg["Subject"], "照明・音響・映像の相談先を、一つにまとめるご提案")
        self.assertIn("株式会社テスト", str(msg["From"]))
        self.assertIn("mailto:sales@sender.test", msg["List-Unsubscribe"])
        for must in ["ok社", "株式会社テスト　山田", "奈良県奈良市1-1", "「不要」とご返信ください", "2027年春"]:
            self.assertIn(must, body)
        self.assertNotIn("{{", body)

        # 1社1通。もう一度実行しても同じ相手には送らない
        s = send_batch(self.conn, self.cfg, template, live=True, log=self.log.append, sleep=lambda _: None)
        self.assertEqual((s.planned, s.sent), (0, 0))
        self.assertEqual(len(got), 2)

    def test_daily_limit_and_suppression(self) -> None:
        self._seed(["ok", "sjis"])
        self._crawl()
        self._approve_all()
        for i in range(3):
            self.conn.execute(
                "INSERT INTO companies(name, site_url, host, segment, status, created_at) VALUES (?,?,?,?,?,?)",
                (f"追加{i}社", f"https://extra{i}.test/", f"extra{i}.test", "honbu", "crawled", db.now()),
            )
            cid = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.conn.execute(
                "INSERT INTO contacts(company_id, email, source_url, status, found_at) VALUES (?,?,?,?,?)",
                (cid, f"info@extra{i}.test", f"https://extra{i}.test/", "approved", db.now()),
            )
        self.conn.commit()
        db.add_suppression(self.conn, "info@ok.localhost", "テスト")
        db.add_suppression(self.conn, "extra0.test", "ドメインごと")
        targets = [r["email"] for r in db.sendable_contacts(self.conn)]
        self.assertEqual(targets, ["info@sjis.localhost", "info@extra1.test", "info@extra2.test"])

        template = load_template(self.root / "templates" / "honbu.txt")
        s = send_batch(self.conn, self.cfg, template, live=True, log=self.log.append, sleep=lambda _: None)
        self.assertEqual((s.sent, s.skipped_limit), (2, 1))  # DAILY_LIMIT=2
        s = send_batch(self.conn, self.cfg, template, live=True, log=self.log.append, sleep=lambda _: None)
        self.assertEqual(s.sent, 0)  # 今日の枠は使い切り
        self.assertEqual(len(self.smtp.messages), 2)  # type: ignore[attr-defined]

    def test_rejected_recipient_is_marked_bounced(self) -> None:
        self.conn.execute(
            "INSERT INTO companies(name, site_url, host, status, created_at) VALUES ('消えた社','https://gone.test/','gone.test','crawled',?)",
            (db.now(),),
        )
        self.conn.execute(
            "INSERT INTO contacts(company_id, email, source_url, status, found_at) VALUES (1,'gone@gone.test','https://gone.test/','approved',?)",
            (db.now(),),
        )
        self.conn.commit()
        template = load_template(self.root / "templates" / "honbu.txt")
        s = send_batch(self.conn, self.cfg, template, live=True, log=self.log.append, sleep=lambda _: None)
        self.assertEqual((s.sent, s.failed), (0, 1))
        self.assertEqual(self.conn.execute("SELECT status FROM contacts").fetchone()[0], "bounced")
        self.assertEqual(db.sendable_contacts(self.conn), [])

    def test_missing_sender_info_blocks_sending(self) -> None:
        env = dict(self.env, SENDER_ADDRESS="")
        cfg = load_config(self.root, env=env)
        template = load_template(self.root / "templates" / "honbu.txt")
        with self.assertRaises(TemplateError):
            send_batch(self.conn, cfg, template, live=False, log=self.log.append)

    def test_unfilled_placeholder_blocks_sending(self) -> None:
        cfg = load_config(self.root, env=dict(self.env, SERVICE_START=""))
        template = load_template(self.root / "templates" / "honbu.txt")
        row = {"name": "A社", "area": "", "category": "", "source_url": "https://a.test/"}
        with self.assertRaises(TemplateError):
            render(template, cfg, row)

    def _reply(self, from_addr: str, body: str, mid: str, subject: str = "Re: ご提案") -> bytes:
        return (
            f"From: {from_addr}\r\nTo: sales@sender.test\r\nSubject: {subject}\r\n"
            f"Message-ID: <{mid}@x.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
        ).encode("utf-8")

    def test_replies(self) -> None:
        self._seed(["ok", "sjis"])
        self._crawl()
        self._approve_all()
        template = load_template(self.root / "templates" / "honbu.txt")
        send_batch(self.conn, self.cfg, template, live=True, log=self.log.append, sleep=lambda _: None)
        own = {"sales@sender.test"}

        self.assertEqual(process_message(self.conn, self._reply("info@ok.localhost", "3", "a"), own), "reply")
        self.assertIsNone(process_message(self.conn, self._reply("info@ok.localhost", "3", "a"), own))  # 二重に数えない
        self.assertEqual(process_message(self.conn, self._reply("info@sjis.localhost", "不要", "b"), own), "optout")
        self.assertTrue(db.is_suppressed(self.conn, "info@sjis.localhost"))
        self.assertIsNone(process_message(self.conn, self._reply("someone@other.test", "こんにちは", "c"), own))
        bounce = self._reply(
            "MAILER-DAEMON@mx.test",
            "Delivery failed: <info@ok.localhost> user unknown",
            "d",
            subject="Undelivered Mail",
        )
        self.assertEqual(process_message(self.conn, bounce, own), "bounce")
        self.assertEqual(
            self.conn.execute("SELECT status FROM contacts WHERE email='info@ok.localhost'").fetchone()[0], "bounced"
        )
        row = self.conn.execute("SELECT choice FROM replies WHERE kind='reply'").fetchone()
        self.assertEqual(row["choice"], 3)


if __name__ == "__main__":
    unittest.main()

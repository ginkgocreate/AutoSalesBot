"""レビューで見つかった不具合の再発防止と、discover の確認。外にはつながない。"""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

from autosales import cli, db
from autosales.config import load_config
from autosales.crawler import Crawler, Robots, crawl_pending
from autosales.discover import DiscoverError, build_overpass_query, discover_osm, discover_search, parse_search_output
from autosales.extract import extract_contacts, extract_emails, find_refusal, parse_html
from autosales.mailer import TemplateError, _send_lock, load_template, plan_count, send_batch
from autosales.replies import _imap_folder, classify_reply, process_message

ROOT = Path(__file__).resolve().parent.parent


def page(html: str):
    return parse_html(html, "https://shop.test/")


class ExtractFixes(unittest.TestCase):
    def test_refusal_across_line_breaks_and_phrasings(self) -> None:
        hits = [
            "※営業・勧誘目的でのお問い合わせは<br>固くお断りいたします。",
            "<h3>営業目的のお問い合わせについて</h3><p>一切お断りしております。</p>",
            "<li>営業・勧誘のご連絡</li><li>はお断りします</li>",
            "営業メールには返信いたしかねます",
            "セールスのご連絡には対応しておりません",
            "営業のお電話・メールはお受けしておりません",
            "営業メールNG",
            "売込のご連絡はご遠慮ください",
            "業者様からのご提案はお断りしています",
            '<img src="a.png" alt="営業メールお断り">',
        ]
        for html in hits:
            self.assertIsNotNone(find_refusal(page(html)), html)

    def test_refusal_does_not_fire_on_ordinary_text(self) -> None:
        for html in [
            "営業時間 19:00〜LAST<br>泥酔の方の入店はお断りします",
            "迷惑メール対策で受信拒否の設定をされている方は解除してください",
            "営業のご相談もお受けしております",
            "DINING BAR 広告 LOUNGE",
        ]:
            self.assertIsNone(find_refusal(page(html)), html)

    def test_declared_obfuscation_is_not_harvested(self) -> None:
        for html in [
            "info＠shop.jp（迷惑メール対策のため＠を全角にしています。半角に変えて送信してください）",
            "&#105;&#110;&#102;&#111;&#64;shop.jp",
            "info(a)shop.jp",
            "info【at】shop.jp",
            "info*shop.jp（*を@に）",
        ]:
            p = page(html)
            self.assertEqual(extract_emails(p), [], html)
            self.assertTrue(p.obfuscated, html)
        # 断り書きのない全角は、ただの書き方なので拾う
        self.assertEqual(extract_emails(page("ｉｎｆｏ＠ｓｈｏｐ.jp")), ["info@shop.jp"])

    def test_glued_labels_are_stripped_and_flagged(self) -> None:
        for html in ["TEL.0742-12-3456 MAIL.info@shop.jp", "E-mail_info@shop.jp", "TEL:0742-12-3456info@shop.jp"]:
            self.assertEqual(extract_contacts(page(html)), [("info@shop.jp", ["label_glued"])], html)
        both = page('MAIL.info@shop.jp <a href="mailto:info@shop.jp">m</a>')
        self.assertEqual(extract_contacts(both), [("info@shop.jp", [])])

    def test_tags_inside_an_address_do_not_split_it(self) -> None:
        self.assertEqual(extract_emails(page("<b>sato</b>.taro@shop.jp")), ["sato.taro@shop.jp"])


class RobotsFixes(unittest.TestCase):
    def test_bom_wildcards_and_longest_match(self) -> None:
        r = Robots("﻿User-agent: *\nDisallow: /private/\nDisallow: /*.php$\nAllow: /private/open/\nCrawl-delay: 3\n")
        self.assertFalse(r.can_fetch("https://a.test/private/x"))
        self.assertTrue(r.can_fetch("https://a.test/private/open/x"))
        self.assertFalse(r.can_fetch("https://a.test/form.php"))
        self.assertTrue(r.can_fetch("https://a.test/form.php?x=1"))
        self.assertEqual(r.crawl_delay, 3.0)

    def test_named_group_wins_over_star(self) -> None:
        r = Robots("User-agent: *\nDisallow:\n\nUser-agent: AutoSalesBot\nDisallow: /\n")
        self.assertFalse(r.can_fetch("https://a.test/"))
        self.assertTrue(Robots("User-agent: *\nDisallow:\n").can_fetch("https://a.test/x"))


class _Sites(http.server.BaseHTTPRequestHandler):
    sites: dict[str, dict[str, object]] = {}
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        site = self.headers.get("Host", "").split(":")[0].replace(".localhost", "")
        path = unquote(self.path)
        type(self).hits.append(f"{site}{path}")
        body = self.sites.get(site, {}).get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
        elif isinstance(body, tuple):
            self.send_response(body[0])
            self.send_header("Location", body[1])
            self.end_headers()
        else:
            data = str(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain" if path == "/robots.txt" else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


class _SMTP(socketserver.StreamRequestHandler):
    """mode: ok / relay_denied（全RCPTに554 5.7.1）/ drop_after_data（受理後、返事の前に切る）"""

    def handle(self) -> None:
        srv = self.server
        self.wfile.write(b"220 fake\r\n")
        rcpt: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250 fake\r\n")
            elif up.startswith("MAIL FROM"):
                rcpt = []
                self.wfile.write(b"250 ok\r\n")
            elif up.startswith("RCPT TO"):
                if srv.mode == "relay_denied":  # type: ignore[attr-defined]
                    self.wfile.write(b"554 5.7.1 Relay access denied\r\n")
                else:
                    rcpt.append(cmd.split(":", 1)[1].strip().strip("<>"))
                    self.wfile.write(b"250 ok\r\n")
            elif up == "DATA":
                self.wfile.write(b"354 go\r\n")
                while self.rfile.readline() not in (b".\r\n", b""):
                    pass
                srv.accepted.extend(rcpt)  # type: ignore[attr-defined]
                if srv.mode == "drop_after_data":  # type: ignore[attr-defined]
                    return
                self.wfile.write(b"250 queued\r\n")
            elif up in ("RSET", "NOOP"):
                self.wfile.write(b"250 ok\r\n")
            elif up == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"502 no\r\n")


class _TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


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
        cls.web = _TCP(("127.0.0.1", 0), _Sites)
        cls.smtp = _TCP(("127.0.0.1", 0), _SMTP)
        cls.smtp.mode, cls.smtp.accepted = "ok", []  # type: ignore[attr-defined]
        for srv in (cls.web, cls.smtp):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.port = cls.web.server_address[1]

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
            "SENDER_EMAIL": "sales@sender.test",
            "SERVICE_START": "2027年春",
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(self.smtp.server_address[1]),
            "SMTP_SECURITY": "none",
            "DAILY_LIMIT": "5",
            "SEND_INTERVAL_SEC": "0",
        }
        self.cfg = load_config(self.root, env=self.env)
        self.conn = db.connect(self.cfg.path("DB_PATH"))
        self.smtp.mode = "ok"  # type: ignore[attr-defined]
        self.smtp.accepted.clear()  # type: ignore[attr-defined]
        _Sites.sites = {}
        _Sites.hits.clear()
        self.template = load_template(self.root / "templates" / "honbu.txt")
        self.out: list[str] = []

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    # ---- 部品 ----
    def company(self, name: str, host: str, segment: str = "honbu", status: str = "new") -> int:
        cur = self.conn.execute(
            "INSERT INTO companies(name, site_url, host, segment, status, created_at) VALUES (?,?,?,?,?,?)",
            (name, f"http://{host}:{self.port}/", host, segment, status, db.now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def contact(self, company_id: int, email: str, status: str = "approved", flags: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO contacts(company_id, email, source_url, flags, status, found_at) VALUES (?,?,?,?,?,?)",
            (company_id, email, "https://x.test/", flags, status, db.now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def ready(self, name: str, email: str, segment: str = "honbu") -> int:
        return self.contact(self.company(name, email.split("@")[1], segment, status="crawled"), email)

    def crawl(self) -> None:
        crawl_pending(self.conn, Crawler(delay=0, allow_private=True), log=self.out.append)

    def status(self, company_id: int) -> str:
        return self.conn.execute("SELECT status FROM companies WHERE id=?", (company_id,)).fetchone()[0]

    def sendable(self) -> list[str]:
        return [r["email"] for r in db.sendable_contacts(self.conn)]

    def send(self, **kw):
        return send_batch(
            self.conn, self.cfg, self.template, live=True, log=self.out.append, sleep=lambda _: None, **kw
        )


class CrawlFixes(Base):
    def test_redirect_into_disallowed_path_is_not_read(self) -> None:
        _Sites.sites = {
            "rd": {
                "/robots.txt": "User-agent: *\nDisallow: /private/\n",
                "/": '<a href="/contact">お問い合わせ</a>',
                "/contact": (302, "/private/contact.html"),
                "/private/contact.html": "secret@rd.localhost",
            }
        }
        cid = self.company("転送社", "rd.localhost")
        self.crawl()
        self.assertNotIn("rd/private/contact.html", _Sites.hits)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)
        self.assertEqual(self.status(cid), "no_email")

    def test_start_url_redirecting_to_another_site_is_not_adopted(self) -> None:
        _Sites.sites = {
            "old": {"/": (301, f"http://other.localhost:{self.port}/")},
            "other": {"/": "info@other.localhost"},
        }
        cid = self.company("移転社", "old.localhost")
        self.crawl()
        self.assertEqual(self.status(cid), "skipped")
        self.assertNotIn("other/", _Sites.hits)

    def test_contact_page_is_read_first_and_refusal_found(self) -> None:
        nav = "".join(f'<a href="/shop-{i}">店舗情報{i}</a>' for i in range(8))
        _Sites.sites = {
            "nav": {
                "/": f'<a href="/company">会社概要</a>{nav}<a href="/お問い合わせ.html">お問い合わせ</a> info@nav.localhost',
                "/company": "会社概要",
                "/お問い合わせ.html": "営業メールは固くお断りいたします",
                **{f"/shop-{i}": "店" for i in range(8)},
            }
        }
        cid = self.company("ナビ社", "nav.localhost")
        self.crawl()
        self.assertEqual(self.status(cid), "refused")
        self.assertEqual(_Sites.hits[2], "nav/お問い合わせ.html")  # robots.txt, トップ の次

    def test_unread_contact_page_blocks_bulk_approval(self) -> None:
        _Sites.sites = {"half": {"/": '<a href="/contact">お問い合わせ</a> info@half.localhost'}}  # /contact は 404
        self.company("半分社", "half.localhost")
        self.crawl()
        flags = self.conn.execute("SELECT flags FROM contacts").fetchone()[0]
        self.assertIn("contact_unread", flags)
        self.assertEqual(cli.main_for_test(self.cfg, self.conn, ["approve", "--unflagged"]), 0)
        self.assertEqual(self.sendable(), [])

    def test_address_on_refused_site_is_blocked_everywhere(self) -> None:
        _Sites.sites = {
            "group": {"/": "honbu@group.localhost 営業メールは固くお断りします。"},
            "clubx.group": {"/": "お問い合わせ: honbu@group.localhost"},
        }
        for order in (("group", "clubx.group"), ("clubx.group", "group")):
            self.conn.executescript(
                "DELETE FROM contacts; DELETE FROM crawl_log; DELETE FROM suppressions; "
                "DELETE FROM site_emails; DELETE FROM companies;"
            )
            for host in order:
                self.company(host, f"{host}.localhost")
            self.crawl()
            cli.main_for_test(self.cfg, self.conn, ["approve", "--unflagged"])
            self.assertEqual(self.sendable(), [], order)
            self.assertTrue(db.is_suppressed(self.conn, "honbu@group.localhost"))

    def test_unrefusing_one_site_does_not_release_an_address_refused_elsewhere(self) -> None:
        _Sites.sites = {
            "a.group": {"/": "店頭での客引き・勧誘行為は一切お断りしております。 honbu@group.localhost"},
            "b.group": {"/": "※営業メールの送信は固くお断りします。 honbu@group.localhost"},
        }
        a = self.company("A店", "a.group.localhost")
        self.company("B店", "b.group.localhost")
        self.crawl()
        self.assertEqual(cli.main_for_test(self.cfg, self.conn, ["unrefuse", str(a), "--yes"]), 0)
        self.crawl()
        cli.main_for_test(self.cfg, self.conn, ["approve", "--unflagged"])
        self.assertEqual(self.status(a), "crawled")
        self.assertEqual(self.sendable(), [])

    def test_ordinary_pages_are_not_mistaken_for_obfuscation(self) -> None:
        for html in [
            'TEL 0742-12-3456 ● MAIL <a href="mailto:info@shop.jp">info@shop.jp</a> ● www.shop.jp',
            "LINE＠でもご予約いただけます ※半角数字でご入力ください info@shop.jp",
            '<a href="https://twitter.com/x">&#64;handle</a> info@shop.jp',
        ]:
            self.assertEqual(extract_contacts(page(html)), [("info@shop.jp", [])], html)

    def test_addresses_are_not_rewritten_into_someone_else(self) -> None:
        self.assertEqual(extract_contacts(page("contact-us@shop.jp")), [("contact-us@shop.jp", [])])
        self.assertEqual(extract_contacts(page("09012345678@docomo.ne.jp")), [("09012345678@docomo.ne.jp", [])])
        hidden = extract_contacts(page('info<span style="display:none">null</span>@shop.jp'))
        self.assertEqual(hidden, [("null@shop.jp", ["tag_joined"])])  # 印が付くので一括承認されない

    def test_robots_percent_case(self) -> None:
        r = Robots("User-agent: *\nDisallow: /お問い合わせ/\n")
        self.assertFalse(r.can_fetch("https://a.test/%e3%81%8a%e5%95%8f%e3%81%84%e5%90%88%e3%82%8f%e3%81%9b/"))

    def test_unrefuse_only_clears_the_snippets_a_person_saw(self) -> None:
        site = {
            "/": '店頭での客引き・勧誘行為は一切お断りしております。<a href="/contact">お問い合わせ</a>',
            "/contact": "info@g.localhost",
        }
        _Sites.sites = {"g": site}
        cid = self.company("G店", "g.localhost")
        self.crawl()
        self.assertEqual(self.status(cid), "refused")
        self.assertEqual(cli.main_for_test(self.cfg, self.conn, ["unrefuse", str(cid), "--yes"]), 0)
        self.crawl()
        self.assertEqual(self.status(cid), "crawled")
        # あとから本物のお断りが増えたら、また除外される
        site["/contact"] = "info@g.localhost ※営業メールの送信は固くお断りします。"
        self.conn.execute("UPDATE companies SET status='new' WHERE id=?", (cid,))
        self.crawl()
        self.assertEqual(self.status(cid), "refused")
        self.assertEqual(self.sendable(), [])

    def test_one_bad_row_does_not_stop_the_crawl(self) -> None:
        _Sites.sites = {"ok2": {"/": "info@ok2.localhost"}}
        self.conn.execute(
            "INSERT INTO companies(name, site_url, host, created_at) VALUES ('壊れ','http://a..b/','a..b',?)",
            (db.now(),),
        )
        cid = self.company("正常", "ok2.localhost")
        self.crawl()
        self.assertEqual(self.status(cid), "crawled")


class GateFixes(Base):
    def test_suppression_accepts_urls_and_covers_subdomains(self) -> None:
        self.ready("B社", "info@mail.b.co.test")
        self.ready("C社", "info@c.test")
        self.ready("E社", "info@e.test")
        self.ready("F社", "info@f.test")
        for target in ("b.co.test", "www.c.test", "https://e.test/contact"):
            db.add_suppression(self.conn, target, "手動")
        self.assertEqual(self.sendable(), ["info@f.test"])
        with self.assertRaises(ValueError):
            db.add_suppression(self.conn, "なにか", "手動")
        self.assertEqual(cli.main_for_test(self.cfg, self.conn, ["suppress", "foo"]), 1)

    def test_optout_blocks_the_whole_company_but_bounce_does_not(self) -> None:
        a = self.company("A社", "a.test", status="crawled")
        self.contact(a, "tantou@a.test")
        self.contact(a, "info@a.test")
        b = self.company("B社", "b.test", status="crawled")
        self.contact(b, "old@b.test")
        self.contact(b, "info@b.test")
        db.add_suppression(self.conn, "tantou@a.test", "本人から停止の連絡")
        db.add_suppression(self.conn, "old@b.test", "不達")
        self.assertEqual(self.sendable(), ["info@b.test"])

    def test_limit_can_never_exceed_todays_room(self) -> None:
        for i in range(8):
            self.ready(f"L{i}社", f"info@l{i}.test")
        self.assertEqual(plan_count(self.conn, self.cfg, 8, -1, True), 0)
        self.assertEqual(plan_count(self.conn, self.cfg, 8, 99, True), 5)
        with self.assertRaises(TemplateError):
            self.send(limit=-1)
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["send", "--limit", "-1"])
        s = self.send()
        self.assertEqual((s.sent, s.skipped_limit), (5, 3))
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(len(self.smtp.accepted), 5)  # type: ignore[attr-defined]

    def test_second_concurrent_send_is_refused(self) -> None:
        self.ready("A社", "info@a.test")
        with _send_lock(self.cfg):
            with self.assertRaises(TemplateError):
                self.send()
        self.assertEqual(self.smtp.accepted, [])  # type: ignore[attr-defined]
        self.assertEqual(self.send().sent, 1)

    def test_relay_denied_does_not_destroy_the_list(self) -> None:
        for i in range(3):
            self.ready(f"R{i}社", f"info@r{i}.test")
        self.smtp.mode = "relay_denied"  # type: ignore[attr-defined]
        s = self.send()
        self.assertEqual((s.sent, s.failed), (0, 2))  # 続けて2社に断られたら止める
        self.assertIn("SMTP", s.stopped)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contacts WHERE status='bounced'").fetchone()[0], 0)
        self.assertEqual(len(self.sendable()), 3)

    def test_disconnect_after_data_is_not_resent(self) -> None:
        self.ready("F社", "info@f.test")
        self.ready("G社", "info@g.test")
        self.smtp.mode = "drop_after_data"  # type: ignore[attr-defined]
        s = self.send()
        self.assertEqual((s.sent, s.unknown), (0, 1))
        self.smtp.mode = "ok"  # type: ignore[attr-defined]
        s = self.send()
        self.assertEqual(s.sent, 1)
        self.assertEqual(sorted(self.smtp.accepted), ["info@f.test", "info@g.test"])  # type: ignore[attr-defined]
        self.assertEqual(db.sent_today(self.conn), 2)

    def test_stop_requests_reach_the_whole_company_in_any_order(self) -> None:
        # 停止が先・巡回が後 / サイトのURLで停止 / 連絡先に無い担当者のアドレスで停止
        _Sites.sites = {"x": {"/": "info@x.localhost tantou@x.localhost clubx.nara@gmail.com"}}
        for target in ("info@x.localhost", f"http://www.x.localhost:{self.port}/contact", "tanaka@x.localhost"):
            self.conn.executescript(
                "DELETE FROM contacts; DELETE FROM crawl_log; DELETE FROM suppressions; "
                "DELETE FROM site_emails; DELETE FROM companies;"
            )
            db.add_suppression(self.conn, target, "電話で停止依頼")
            self.company("X社", "x.localhost")
            self.crawl()
            self.conn.execute("UPDATE contacts SET status='approved'")
            self.conn.commit()
            self.assertEqual(self.sendable(), [], target)

    def test_empty_segment_means_only_companies_without_one(self) -> None:
        self.ready("本部社", "info@h.test", segment="honbu")
        self.ready("層なし店", "info@n.test", segment="")
        self.assertEqual([r["email"] for r in db.sendable_contacts(self.conn, segment="")], ["info@n.test"])

    def test_stop_during_the_wait_is_honoured(self) -> None:
        self.ready("A社", "info@a.test")
        self.ready("X社", "info@x.test")

        def stop_x(_seconds: float) -> None:
            db.add_suppression(self.conn, "info@x.test", "電話で停止依頼")

        s = send_batch(self.conn, self.cfg, self.template, live=True, log=self.out.append, sleep=stop_x)
        self.assertEqual(s.sent, 1)
        self.assertEqual(self.smtp.accepted, ["info@a.test"])  # type: ignore[attr-defined]

    def test_mixed_segments_need_an_explicit_choice(self) -> None:
        self.ready("本部社", "info@h.test", segment="honbu")
        self.ready("ショー店", "info@s.test", segment="show")
        self.assertEqual(cli.main_for_test(self.cfg, self.conn, ["send"]), 1)
        self.assertEqual(list(self.root.glob("outbox/*/*.eml")), [])
        self.assertEqual(cli.main_for_test(self.cfg, self.conn, ["send", "--segment", "show", "--template", "show"]), 0)
        self.assertEqual(len(list(self.root.glob("outbox/*/*.eml"))), 1)

    def test_approve_csv_reads_fullwidth_and_checks_addresses(self) -> None:
        a = self.contact(self.company("A社", "a.test", status="crawled"), "info@a.test", status="pending")
        b = self.contact(self.company("B社", "b.test", status="crawled"), "info@b.test", status="pending")
        c = self.contact(self.company("C社", "c.test", status="refused"), "info@c.test", status="pending")
        path = self.root / "review.csv"
        rows = [
            f"{a},ｏｋ,A社,info@a.test",
            f"{b},〇,B社,info@WRONG.test",
            f"{c},ok,C社,info@c.test",
            f"{a},たぶん,A社,info@a.test",
        ]
        path.write_bytes(("ID,判定,会社名,アドレス\r\n" + "\r\n".join(rows) + "\r\n").encode("cp932"))
        self.assertEqual(cli.main_for_test(self.cfg, self.conn, ["approve", "--csv", str(path)]), 1)
        self.assertEqual(self.sendable(), ["info@a.test"])


class ReplyFixes(Base):
    def setUp(self) -> None:
        super().setUp()
        self.ready("S1社", "info@s1.test")
        self.contact(1, "owner@s1.test", status="pending")
        self.ready("S2社", "info@s2.test")
        self.send()
        self.mids = {
            r["email"]: r["message_id"]
            for r in self.conn.execute(
                "SELECT ct.email, s.message_id FROM sends s JOIN contacts ct ON ct.id=s.contact_id"
            )
        }
        self.n = 0

    def mail(self, from_addr: str, body: str, subject: str = "Re: ご提案", extra: str = "") -> str | None:
        self.n += 1
        raw = (
            f"From: {from_addr}\r\nTo: sales@sender.test\r\nSubject: {subject}\r\n{extra}"
            f"Message-ID: <r{self.n}@x.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
        )
        return process_message(self.conn, raw.encode("utf-8"), {"sales@sender.test"})

    def test_optout_wording_and_position(self) -> None:
        long_mail = (
            "株式会社テスト\n山田様\n\nお世話になっております。\nS1の佐藤です。\nご連絡ありがとうございます。\n"
            "今後のご案内は不要です。\n配信停止をお願いいたします。"
        )
        quoted_first = (
            "> 近いものを、番号だけご返信いただけますか。\n> 4 今は必要ない\n\n今後のメール送信はお控えください。"
        )
        for text in (long_mail, quoted_first, "今後のご連絡はご遠慮ください。", "不要です"):
            self.assertEqual(classify_reply(text).kind, "optout", text)

    def test_interested_replies_are_not_counted_as_optout(self) -> None:
        for text, choice in [
            ("工事不要なら興味があります。1です", 1),
            ("費用は不要ですか?詳しく聞きたいです", None),
            ("打合せは火曜日で結構です。", None),
            ("ワンストップ(one-stop)で頼めるなら1", None),
            ("説明は不要なので資料だけ送ってください", None),
            ("4", 4),
            ("4 今は必要ない", 4),
        ]:
            r = classify_reply(text)
            self.assertEqual((r.kind, r.choice), ("reply", choice), text)

    def test_numbers_that_are_not_choices(self) -> None:
        for text in [
            "3店舗あります",
            "2、3店舗あります。詳しく聞きたいです。",
            "2日後に電話ください",
            "11/4にお電話します",
            "2:00以降なら電話可能です",
            "1、2が当てはまります",
            "1と2です",
        ]:
            self.assertIsNone(classify_reply(text).choice, text)
        self.assertEqual(classify_reply("2店舗あります。1です").choice, 1)

    def test_reply_written_below_the_quote_is_read(self) -> None:
        text = "On 2026/11/14 10:00, 株式会社テスト wrote:\n> 近いものを、番号だけ\n> 4 今は必要ない\n\n不要です。今後送らないでください。"
        self.assertEqual(classify_reply(text).kind, "optout")
        self.assertEqual(classify_reply("On 2026/11/14 10:00, 株式会社テスト wrote:\n> 引用\n\n2").choice, 2)

    def test_subject_only_and_plain_stop_wordings(self) -> None:
        self.assertEqual(self.mail("info@s1.test", "", subject="不要"), "optout")
        for text in ("不要なのでやめてください", "停止してください", "お断りです"):
            self.assertEqual(classify_reply(text).kind, "optout", text)

    def test_ordinary_number_replies_and_signatures(self) -> None:
        for text, want in [
            ("1でお願いします", 1),
            ("２です", 2),
            ("1番", 1),
            ("①", 1),
            ("3 新店の予定があります", 3),
            ("番号は2", 2),
            ("1点だけ質問です", None),
            ("2ヶ月後に改装します", None),
            ("興味あります。\n\n山田\n奈良市大宮町1-2-3\n営業2部\nTEL 0742-33-1234", None),
        ]:
            self.assertEqual(classify_reply(text).choice, want, text)
        self.assertIsNone(classify_reply("3営業日以内に返信します\n1", auto=True).choice)

    def test_head_office_stop_covers_sister_shops(self) -> None:
        self.assertEqual(self.mail("honbu@s1.test", "不要です。今後一切送らないでください"), "optout")
        self.contact(self.company("S1の3号店", "shop3.s1.test", status="crawled"), "shop3@s1.test")
        self.assertEqual(self.sendable(), [])

    def test_iso2022jp_with_vendor_characters(self) -> None:
        body = "①".encode("euc_jis_2004")
        raw = (
            b"From: info@s1.test\r\nSubject: Re\r\nMessage-ID: <iso@x.test>\r\n"
            b"Content-Type: text/plain; charset=iso-2022-jp\r\n\r\n\x1b$B"
            + bytes(b & 0x7F for b in body)
            + b"\x1b(B\r\n"
        )
        self.assertEqual(process_message(self.conn, raw, {"sales@sender.test"}), "reply")
        self.assertEqual(self.conn.execute("SELECT choice FROM replies").fetchone()[0], 1)

    def test_reply_from_another_address_is_matched_by_thread(self) -> None:
        extra = f"In-Reply-To: {self.mids['info@s1.test']}\r\n"
        self.assertEqual(self.mail("someone@gmail.com", "不要です", extra=extra), "optout")
        self.assertTrue(db.is_suppressed(self.conn, "info@s1.test"))

    def test_reply_from_known_colleague_counts_for_the_company(self) -> None:
        self.assertEqual(self.mail("owner@s1.test", "2"), "reply")
        self.assertEqual(self.conn.execute("SELECT choice FROM replies").fetchone()[0], 2)

    def test_unrelated_mail_is_not_stored(self) -> None:
        self.assertIsNone(self.mail("stranger@elsewhere.test", "こんにちは"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 0)

    def _dsn(self, rcpt: str, action: str, status: str) -> bytes:
        self.n += 1
        return (
            "From: MAILER-DAEMON@mx.test\r\nTo: sales@sender.test\r\nSubject: Delivery Status\r\n"
            f"Message-ID: <d{self.n}@mx.test>\r\n"
            'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n\r\n'
            "--B\r\nContent-Type: text/plain\r\n\r\nnotice\r\n"
            "--B\r\nContent-Type: message/delivery-status\r\n\r\n"
            "Reporting-MTA: dns; mx.test\r\n\r\n"
            f"Final-Recipient: rfc822; {rcpt}\r\nAction: {action}\r\nStatus: {status}\r\n\r\n--B--\r\n"
        ).encode()

    def test_bounces_hit_only_the_exact_failed_recipient(self) -> None:
        own = {"sales@sender.test"}
        self.assertIsNone(process_message(self.conn, self._dsn("info@s1.test", "delayed", "4.4.1"), own))
        self.assertIsNone(process_message(self.conn, self._dsn("info@s1.te", "failed", "5.1.1"), own))
        self.assertEqual(process_message(self.conn, self._dsn("info@s1.test", "failed", "5.1.1"), own), "bounce")
        states = dict(self.conn.execute("SELECT email, status FROM contacts").fetchall())
        self.assertEqual(states, {"info@s1.test": "bounced", "owner@s1.test": "pending", "info@s2.test": "approved"})

    def test_imap_folder_names(self) -> None:
        self.assertEqual(_imap_folder("INBOX"), '"INBOX"')
        self.assertEqual(_imap_folder("[Gmail]/All Mail"), '"[Gmail]/All Mail"')
        # RFC 3501 に載っている例
        self.assertEqual(_imap_folder("~peter/mail/台北/日本語"), '"~peter/mail/&U,BTFw-/&ZeVnLIqe-"')


class DiscoverTests(Base):
    def test_osm_groups_by_site_and_skips_portals(self) -> None:
        payload = {
            "elements": [
                {
                    "tags": {
                        "name": "ラウンジA 本店",
                        "amenity": "bar",
                        "website": "https://www.group-a.test/honten",
                        "addr:city": "奈良市",
                    }
                },
                {
                    "tags": {
                        "name": "ラウンジA 2号店",
                        "amenity": "bar",
                        "contact:website": "https://group-a.test/",
                        "addr:city": "橿原市",
                    }
                },
                {"tags": {"name": "バーB", "amenity": "bar", "website": "bar-b.test"}},
                {"tags": {"name": "居酒屋C", "amenity": "bar", "website": "https://r.gnavi.co.jp/abc/"}},
                {"tags": {"amenity": "bar", "website": "https://noname.test/"}},
            ]
        }
        seen: list[str] = []

        def fetch(query: str, ua: str) -> dict:
            seen.append(query)
            return payload

        c = discover_osm(self.conn, '奈良県"];evil', segment="show", fetch=fetch, log=self.out.append)
        self.assertEqual((c["added"], c["non_official"]), (2, 1))
        self.assertNotIn('"];evil', seen[0])
        rows = {r["host"]: r for r in self.conn.execute("SELECT * FROM companies")}
        self.assertEqual(rows["group-a.test"]["store_count"], 2)
        self.assertEqual(rows["group-a.test"]["site_url"], "https://group-a.test/")
        self.assertEqual(rows["bar-b.test"]["segment"], "show")
        self.assertEqual(discover_osm(self.conn, "奈良県", fetch=fetch, log=self.out.append)["duplicate"], 2)
        with self.assertRaises(DiscoverError):
            build_overpass_query("奈良県", ["unknown"])

    def test_osm_chains_shared_hosts_and_truncated_answers(self) -> None:
        payload = {
            "elements": [
                # 同じ屋号の系列店（店名は branch）。www 付き・http のサイト
                {
                    "type": "node",
                    "id": 1,
                    "tags": {
                        "name": "HUB",
                        "branch": "東梅田店",
                        "brand": "HUB",
                        "operator": "株式会社ハブ",
                        "amenity": "bar",
                        "website": "http://www.hub.test/shop/a",
                    },
                },
                {
                    "type": "node",
                    "id": 2,
                    "tags": {
                        "name": "HUB",
                        "branch": "なんば店",
                        "brand": "HUB",
                        "operator": "株式会社ハブ",
                        "amenity": "bar",
                        "website": "http://www.hub.test/shop/b",
                    },
                },
                # ホームページ作成サービスに相乗りしている、無関係な2店
                {
                    "type": "node",
                    "id": 3,
                    "tags": {"name": "スナック花", "amenity": "bar", "website": "https://r.builder.test/hana"},
                },
                {
                    "type": "node",
                    "id": 4,
                    "tags": {"name": "バー月", "amenity": "bar", "website": "https://r.builder.test/tsuki"},
                },
            ]
        }
        discover_osm(self.conn, "大阪市", fetch=lambda q, ua: payload, log=self.out.append)
        rows = {r["host"]: r for r in self.conn.execute("SELECT * FROM companies")}
        self.assertEqual(
            (rows["hub.test"]["name"], rows["hub.test"]["store_count"], rows["hub.test"]["site_url"]),
            ("株式会社ハブ", 2, "http://www.hub.test/"),
        )
        self.assertEqual(
            (rows["r.builder.test"]["name"], rows["r.builder.test"]["store_count"], rows["r.builder.test"]["site_url"]),
            ("スナック花", 1, "https://r.builder.test/hana"),
        )
        self.assertIn("バー月", rows["r.builder.test"]["notes"])
        with self.assertRaises(DiscoverError):
            discover_osm(
                self.conn,
                "大阪府",
                fetch=lambda q, ua: {"elements": [], "remark": "runtime error: Query timed out"},
                log=self.out.append,
            )

    def test_search_limit_and_internal_addresses(self) -> None:
        cands = [{"name": f"会社{i}", "url": f"https://c{i}.test/"} for i in range(8)]
        cands[:0] = [
            {"name": "内部", "url": "http://127.0.0.1/"},
            {"name": "メタ", "url": "http://169.254.169.254/latest/"},
        ]
        out = json.dumps({"structured_output": {"candidates": cands}})
        c = discover_search(self.conn, "x", limit=3, run=lambda p, t: out, log=self.out.append)
        self.assertEqual((c["added"], c["invalid"]), (3, 2))

    def test_search_candidates_go_through_the_same_checks(self) -> None:
        out = {
            "result": "done",
            "structured_output": {
                "candidates": [
                    {
                        "name": "株式会社ナイトグループ",
                        "url": "https://night-group.test/",
                        "area": "大阪市",
                        "category": "キャバクラ",
                        "stores": 5,
                        "note": "x",
                    },
                    {"name": "ポータル", "url": "https://www.instagram.com/abc", "stores": None},
                    {"name": "", "url": "https://x.test"},
                    {"name": "変なURL", "url": "javascript:alert(1)"},
                    "文字列",
                ]
            },
        }
        prompts: list[str] = []

        def run(prompt: str, timeout: int) -> str:
            prompts.append(prompt)
            return json.dumps(out)

        c = discover_search(
            self.conn, "大阪 キャバクラ グループ 運営会社", segment="honbu", run=run, log=self.out.append
        )
        self.assertEqual(c, {"added": 1, "duplicate": 0, "invalid": 2, "non_official": 1})
        row = self.conn.execute("SELECT * FROM companies").fetchone()
        self.assertEqual(
            (row["name"], row["store_count"], row["segment"], row["status"]),
            ("株式会社ナイトグループ", 5, "honbu", "new"),
        )
        self.assertIn("大阪 キャバクラ", prompts[0])

    def test_search_output_parsing(self) -> None:
        text = json.dumps({"result": '候補です。\n```json\n[{"name": "A", "url": "https://a.test"}]\n```'})
        self.assertEqual(parse_search_output(text), [{"name": "A", "url": "https://a.test"}])
        for bad in [
            "not json",
            json.dumps({"result": "見つかりませんでした"}),
            json.dumps({"is_error": True, "result": "x"}),
        ]:
            with self.assertRaises(DiscoverError):
                parse_search_output(bad)


if __name__ == "__main__":
    unittest.main()

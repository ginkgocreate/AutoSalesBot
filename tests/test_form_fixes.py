"""フォーム送信のレビューで見つかった不具合の再発防止。外にはつながない。"""

from __future__ import annotations

import csv
import unittest

from autosales import db
from autosales.assist import Assist
from autosales.config import load_config
from autosales.extract import find_refusal, parse_html
from autosales.forms import fill, find_contact_form
from autosales.formsend import encode_pairs
from autosales.replies import process_message

try:
    from tests.test_forms import CF7, PLAIN, SENDER, URL, Base, _Web, thanks
except ImportError:  # python3 -m unittest discover -s tests で動かしたとき
    from test_forms import CF7, PLAIN, SENDER, URL, Base, _Web, thanks


def plan_of(html: str, url: str = URL):
    return find_contact_form(html, url)


def form(
    rows: str, head: str = "<h2>お問い合わせ</h2>", action: str = "/contact/send.php", button: str = "送信"
) -> str:
    return (
        f'{head}<form action="{action}" method="post"><table>{rows}'
        '<tr><th>お名前</th><td><input name="name"></td></tr>'
        '<tr><th>メールアドレス</th><td><input name="mail"></td></tr>'
        '<tr><th>お問い合わせ内容</th><td><textarea name="body"></textarea></td></tr></table>'
        f'<input type="submit" value="{button}"></form>'
    )


def item(plan, name: str) -> dict:
    return next(i for i in plan.items if i["name"] == name)


class Detection(unittest.TestCase):
    def test_public_comment_forms_are_not_contact_forms(self) -> None:
        comment = (
            '<form action="/wp-comments-post.php" method="post" id="commentform">'
            '<textarea name="comment"></textarea><input name="author"><input name="email">'
            '<input type="hidden" name="comment_post_ID" value="2"><input type="submit" value="コメントを送信"></form>'
        )
        self.assertIsNone(plan_of(comment, "https://shop.test/"))
        review = form("", head="<h2>口コミを投稿する</h2>", action="/review/post.php", button="投稿する")
        self.assertIsNone(plan_of(review, "https://shop.test/shop/"))

    def test_forms_for_other_purposes_are_not_auto(self) -> None:
        # 必須の印が無く、見出しがフォームの外にある予約フォーム
        reserve = form(
            '<tr><th>ご来店日</th><td><input type="date" name="d"></td></tr>'
            '<tr><th>人数</th><td><select name="n"><option>1名</option><option>2名</option></select></td></tr>',
            head="<h2>ご予約フォーム</h2>",
            action="/reserve/send.php",
        )
        self.assertEqual(plan_of(reserve, "https://shop.test/reserve/").mode, "skip")
        for head, action in [
            ("<h2>採用エントリーフォーム</h2>", "/recruit/entry.php"),
            ("<h2>資料請求</h2>", "/request/send.php"),
            ("<h3>お見積りのご依頼</h3>", "/estimate.php"),
        ]:
            self.assertEqual(plan_of(form("", head=head, action=action), "https://shop.test/x/").mode, "skip", head)
        # 問い合わせと予約の共用らしいフォームは、人が確かめる
        mixed = plan_of(form("", head="<h2>お問い合わせ・ご予約</h2>"))
        self.assertEqual(mixed.mode, "manual")
        dated = plan_of(form('<tr><th>ご希望日時</th><td><input name="when"></td></tr>'))
        self.assertEqual(dated.mode, "manual")
        # 問い合わせ用だと分かる手がかりが無いフォームも、人が確かめる
        vague = (
            '<form action="/send.php" method="post"><input name="name"><input name="mail">'
            '<textarea name="memo"></textarea><input type="submit" value="OK"></form>'
        )
        self.assertEqual(plan_of(vague, "https://shop.test/").mode, "manual")

    def test_category_never_ends_up_as_reservation_or_recruiting(self) -> None:
        def select(options: str, head: str = "お問い合わせ種別") -> str:
            return form(f'<tr><th>{head}</th><td><select name="kind">{options}</select></td></tr>')

        # 空の選択肢が無く、先頭が「ご予約」。その他があればそれを選び、無ければ人に回す
        plan = plan_of(select("<option>ご予約について</option><option>採用について</option><option>その他</option>"))
        self.assertEqual((plan.mode, item(plan, "kind")["choice"], plan.flags), ("auto", "その他", ["category"]))
        self.assertEqual(plan_of(select("<option>ご予約について</option><option>採用について</option>")).mode, "manual")
        # 「営業時間」「提携駐車場」「ご宿泊のお問い合わせ」「一般歯科」は、選んでよいものではない
        bad = "".join(
            f"<option>{t}</option>"
            for t in ("営業時間について", "提携駐車場について", "ご宿泊のお問い合わせ", "一般歯科")
        )
        self.assertEqual(
            plan_of(select('<option value="">選択</option>' + bad, head="お問い合わせ種別 必須")).mode, "manual"
        )
        # 意味の分からない選択欄を最初の値のまま送るときは、印を付けて一覧に出す
        plan = plan_of(
            form(
                '<tr><th>年代</th><td><select name="age"><option>20代</option><option>30代</option></select></td></tr>'
            )
        )
        self.assertIn("unknown_fields", plan.flags)
        self.assertIn("年代→「20代」のまま", plan.describe())

    def test_radio_labels_must_be_certain(self) -> None:
        # 文字が入力の前にある作り。直後の文字で名前を決めると1つずれる
        before = form(
            '<tr><th>種別 必須</th><td>ご予約<input type="radio" name="k" value="1">'
            '採用<input type="radio" name="k" value="2">その他<input type="radio" name="k" value="3"></td></tr>'
        )
        self.assertEqual(plan_of(before).mode, "manual")
        labelled = form(
            '<tr><th>種別 必須</th><td><label><input type="radio" name="k" value="1">ご予約</label>'
            '<label><input type="radio" name="k" value="3">その他</label></td></tr>'
        )
        plan = plan_of(labelled)
        self.assertEqual((plan.mode, item(plan, "k")["value"], item(plan, "k")["choice"]), ("auto", "3", "その他"))

    def test_hidden_traps_stay_empty(self) -> None:
        traps = form(
            '<tr class="gform_validation_container"><th>Email</th><td><input name="trap_a"></td></tr>'
            '<tr><th>この欄は空欄のままにしてください</th><td><input name="trap_b"></td></tr>'
            '<tr><th>URL</th><td><input name="trap_c" tabindex="-1" autocomplete="off"></td></tr>'
        )
        plan = plan_of(traps)
        got = dict(fill(plan.items, SENDER, "s", "b"))
        self.assertEqual((got["trap_a"], got["trap_b"], got["trap_c"], got["mail"]), ("", "", "", "sales@sender.test"))

    def test_roles_come_from_the_field_own_label(self) -> None:
        # 前の欄の下の注記が、次の欄の意味を決めてはいけない
        divs = (
            '<h2>お問い合わせ</h2><form action="/s.php" method="post">'
            '<div>メールアドレス</div><input name="f_a"><div>※携帯電話のメールアドレスは受信設定をご確認ください</div>'
            '<div>会社名</div><input name="f_c">'
            '<div>電話番号</div><input name="f_d"><p>日中つながるお電話番号をご記入ください</p>'
            '<div>ご住所</div><input name="f_e">'
            '<div>内容</div><textarea name="f_m"></textarea><input type="submit" value="送信"></form>'
        )
        got = dict(fill(plan_of(divs).items, SENDER, "s", "b"))
        self.assertEqual(
            (got["f_a"], got["f_c"], got["f_d"], got["f_e"]),
            ("sales@sender.test", "株式会社テスト", "06-1234-5678", "大阪府大阪市北区1-1"),
        )
        # name 属性の一部が当たっただけでは決めない
        names = form(
            '<tr><th>ご利用ホテル</th><td><input name="hotel"></td></tr>'
            '<tr><th>ご利用プラン</th><td><input name="plan_name"></td></tr>'
            '<tr><th>会社名</th><td><input name="mailform[company]"></td></tr>'
        )
        got = dict(fill(plan_of(names).items, SENDER, "s", "b"))
        self.assertEqual((got["hotel"], got["plan_name"], got["mailform[company]"]), ("", "", "株式会社テスト"))
        required = form('<tr><th>ご利用ホテル 必須</th><td><input name="hotel"></td></tr>')
        self.assertEqual(plan_of(required).mode, "manual")

    def test_only_real_consent_boxes_are_checked(self) -> None:
        def with_box(text: str, name: str = "c1") -> str:
            return form(
                f'<tr><th>個人情報の取り扱い</th><td><label><input type="checkbox" name="{name}" value="1">{text}</label></td></tr>'
            )

        self.assertEqual(item(plan_of(with_box("個人情報の取り扱いに同意する")), "c1")["role"], "check")
        for text in ("カタログの送付を希望する", "新着情報のメールを受け取る"):
            plan = plan_of(with_box(text))
            self.assertNotIn("c1", [i["name"] for i in plan.items], text)
        refused = plan_of(with_box("営業・勧誘を目的とした連絡ではないことを確認しました"))
        self.assertEqual(refused.mode, "skip")
        self.assertIn("営業目的", refused.reason)

    def test_more_spam_guards_send_the_form_to_a_person(self) -> None:
        for extra in (
            "<altcha-widget></altcha-widget>",
            '<input type="hidden" name="ct_checkjs" value="0">',
            '<p>画像に表示された数字を入力してください</p><input name="code">',
            '<input type="hidden" id="wpa_field" name="x">',
            '<div class="mosparo__container"></div>',
        ):
            self.assertEqual(plan_of(form("") + extra).mode, "manual", extra)

    def test_signature_notices_what_matters(self) -> None:
        def box(text: str) -> str:
            return form(
                f'<tr><th>確認</th><td><label><input type="checkbox" name="agree" value="1">{text}</label></td></tr>'
            )

        base = plan_of(form('<input type="hidden" name="form_type" value="contact">')).signature()
        changes = {
            "任意の日付欄が増えた": form(
                '<input type="hidden" name="form_type" value="contact">'
                '<tr><th>ご希望日</th><td><input type="date" name="d"></td></tr>'
            ),
            "隠し項目の意味が変わった": form('<input type="hidden" name="form_type" value="reserve">'),
            "送信先が変わった": form(
                '<input type="hidden" name="form_type" value="contact">', action="/contact/send.php?mode=reserve"
            ),
            "見出しが変わった": form('<input type="hidden" name="form_type" value="contact">').replace(
                "お問い合わせ内容", "ご要望"
            ),
        }
        for what, html in changes.items():
            self.assertNotEqual(plan_of(html).signature(), base, what)
        self.assertNotEqual(
            plan_of(box("個人情報の取り扱いに同意する")).signature(), plan_of(box("上記の規約に同意する")).signature()
        )
        # 毎回変わるトークンでは変わらない
        token = '<input type="hidden" name="_token" value="{}"><input type="hidden" name="form_type" value="contact">'
        self.assertEqual(plan_of(form(token.format("abc"))).signature(), plan_of(form(token.format("xyz"))).signature())

    def test_refusal_written_inside_a_field_is_read(self) -> None:
        html = form("").replace(
            '<textarea name="body">',
            '<textarea name="body" placeholder="※営業・セールス目的のお問い合わせは固くお断りいたします。">',
        )
        self.assertIsNotNone(find_refusal(parse_html(html, URL)))
        self.assertIsNone(find_refusal(parse_html(form(""), URL)))

    def test_select_without_closing_option_tags(self) -> None:
        old = form(
            '<tr><th>都道府県</th><td><select name="pref"><option value="">--<option value="27">大阪府'
            '<option value="26">京都府</select></td></tr>'
            '<tr><th>ご来店日 必須</th><td><input type="date" name="d"></td></tr>'
        )
        plan = plan_of(old)
        self.assertEqual(plan.mode, "skip")  # select の後ろの見出し（来店日・必須）が読めている
        self.assertEqual(item(plan, "pref")["options"], [("", "--"), ("27", "大阪府"), ("26", "京都府")])

    def test_shift_jis_pages_take_common_characters(self) -> None:
        data, _ = encode_pairs([("a", "㈱テスト～①髙")], "Shift_JIS", False)
        self.assertIn("%87%8A", data.decode("ascii").upper())  # ㈱ は cp932 の字として送る


class Crawl(Base):
    def test_contact_page_form_wins_over_an_auto_form_elsewhere(self) -> None:
        guarded = CF7.format(api="", token="t") + '<script src="https://www.google.com/recaptcha/api.js"></script>'
        cid = self.site("a", guarded, top='<a href="/contact/">お問い合わせ</a>' + form(""))
        self.crawl()
        row = self.form(cid)
        self.assertEqual((row["mode"], row["page_url"].endswith("/contact/")), ("manual", True))

    def test_unread_contact_page_marks_the_form(self) -> None:
        cid = self.site(
            "a",
            PLAIN.format(token="t"),
            top='<a href="/contact/">お問い合わせ</a><a href="/inquiry/">お問い合わせの前に</a>',
        )
        self.crawl()
        self.assertIn("contact_unread", self.form(cid)["flags"])
        self.assertIn("承認 0件", self.cli("form", "approve", "--unflagged")[1])

    def test_frames_do_not_push_out_other_pages(self) -> None:
        links = "".join(
            f'<a href="/{p}/">{t}</a>'
            for p, t in [
                ("contact", "お問い合わせ"),
                ("inquiry", "お問い合わせ窓口"),
                ("toiawase", "問い合わせ先"),
                ("company", "会社概要"),
                ("about", "私たちについて"),
                ("access", "アクセス"),
                ("policy", "サイトポリシー"),
            ]
        )
        cid = self.site("a", "<p>窓口</p>", top='<iframe src="/calendar/"></iframe>' + links)
        for path in ("inquiry", "toiawase", "company", "about", "access", "calendar"):
            _Web.sites["a"][f"/{path}/"] = "<p>ページ</p>"
        _Web.sites["a"]["/policy/"] = "<p>営業メールは固くお断りいたします。</p>"
        self.crawl()
        self.assertEqual(self.conn.execute("SELECT status FROM companies WHERE id=?", (cid,)).fetchone()[0], "refused")

    def test_rejected_and_demoted_forms_survive_recrawls(self) -> None:
        a = self.site("a", PLAIN.format(token="t"))
        b = self.site(
            "b",
            PLAIN.format(token="t"),
            post=lambda p, f, h: (200, "<p>入力内容に誤りがあります</p>" + PLAIN.format(token="t"), {}),
        )
        self.crawl()
        self.cli("form", "approve", "--reject", str(self.form(a)["id"]), "--ids", str(self.form(b)["id"]))
        self.assertEqual(self.send().failed, 1)
        self.assertEqual((self.form(b)["mode"], self.form(b)["demoted"]), ("manual", 1))
        page = _Web.sites["a"]["/contact/"]
        for contact in ("", page, page + '<div class="g-recaptcha"></div>', page):
            # お問い合わせページが一時的に読めない → 戻る → 一時的に CAPTCHA が入る → 外れる
            _Web.sites["a"]["/contact/"] = _Web.sites["b"]["/contact/"] = contact
            if not contact:
                del _Web.sites["a"]["/contact/"], _Web.sites["b"]["/contact/"]
            self.conn.execute("UPDATE companies SET status='no_email'")
            self.crawl(recrawl=True)
        self.assertEqual(self.form(a)["status"], "rejected")
        self.assertEqual((self.form(b)["mode"], self.form(b)["demoted"]), ("manual", 1))
        self.cli("form", "approve", "--unflagged")
        self.assertEqual((self.send().sent, len(self.posted("a")), len(self.posted("b"))), (0, 0, 1))

    def test_same_public_address_is_reached_only_once(self) -> None:
        a = self.site("a", PLAIN.format(token="t") + '<a href="mailto:honbu@group.test">mail</a>')
        b = self.site("b", PLAIN.format(token="t") + '<a href="mailto:honbu@group.test">mail</a>')
        self.crawl()
        self.approve_all()
        # 本部のアドレスは a社の連絡先になった。b社のフォームは同じ窓口に届くので送らない
        self.assertEqual(self.conn.execute("SELECT company_id FROM contacts").fetchall()[0][0], a)
        self.assertEqual(db.sendable_forms(self.conn), [])
        # 逆向き: a社のアドレスを見送り、b社にフォームで送ったあとは、そのアドレスにメールを送らない
        self.conn.execute("UPDATE contacts SET status='rejected'")
        self.assertEqual(self.send(limit=1).sent, 1)
        self.conn.execute("UPDATE contacts SET status='approved'")
        self.assertEqual(db.sendable_contacts(self.conn), [])
        del b


class Sending(Base):
    def one(self, contact: str = "", post=thanks) -> int:  # noqa: ANN001
        cid = self.site("a", contact or PLAIN.format(token="t"), post=post)
        self.crawl()
        self.approve_all()
        return cid

    def test_success_page_that_shows_the_form_again_is_not_a_failure(self) -> None:
        a = self.one(post=lambda p, f, h: (200, "<p>送信が完了しました。</p>" + PLAIN.format(token="t"), {}))
        s = self.send()
        self.assertEqual((s.failed, s.unknown, self.sends(a), self.form(a)["mode"]), (0, 1, ["unknown"], "auto"))
        self.assertTrue(db.company_contacted(self.conn, a))
        self.assertEqual(Assist(self.conn, self.cfg, self.template).targets(), [])
        self.cli("export")
        with open(self.root / "data" / "list.csv", encoding="utf-8-sig") as f:
            self.assertIn(
                "届いたか不明（入力フォームがもう一度表示されました", list(csv.DictReader(f))[0]["フォーム送信の結果"]
            )

    def test_missing_thanks_page_after_redirect_is_not_a_failure(self) -> None:
        a = self.one(post=lambda p, f, h: (302, "", {"Location": "/contact/thanks-missing.html"}))
        s = self.send()
        self.assertEqual((s.failed, s.unknown, self.sends(a)), (0, 1, ["unknown"]))

    def test_polite_error_page_is_not_counted_as_sent(self) -> None:
        a = self.one(
            post=lambda p, f, h: (
                200,
                "<p>お問い合わせありがとうございます。このお問い合わせは送信できませんでした。</p>",
                {},
            )
        )
        s = self.send()
        self.assertEqual((s.sent, s.unknown, self.sends(a)), (0, 1, ["unknown"]))

    def _confirm_page(self, extra: str) -> object:
        def post(path, fields, headers):  # noqa: ANN001
            if "send" in fields:
                return 200, "<p>送信しました。</p>", {}
            hidden = "".join(
                f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items() if k != "confirm"
            )
            return (
                200,
                (
                    f'{extra}<form method="post" action="/contact/send.php">{hidden}'
                    '<input type="submit" name="send" value="送信する"></form>'
                ),
                {},
            )

        return post

    def test_confirm_screen_with_a_refusal_or_captcha_is_not_submitted(self) -> None:
        a = self.one(post=self._confirm_page("<p>※営業・勧誘目的のお問い合わせは固くお断りいたします。</p>"))
        self.assertEqual(self.send().sent, 0)
        self.assertEqual([("send" in f) for f in self.posted("a")], [False])  # 最終の送信は出していない
        self.assertEqual(self.conn.execute("SELECT status FROM companies WHERE id=?", (a,)).fetchone()[0], "refused")
        self.assertFalse(db.company_contacted(self.conn, a))

        b = self.site(
            "b",
            PLAIN.format(token="t"),
            post=self._confirm_page('<script src="https://www.google.com/recaptcha/api.js"></script>'),
        )
        self.crawl()
        self.approve_all()
        s = self.send()
        self.assertEqual((s.sent, s.failed, [("send" in f) for f in self.posted("b")]), (0, 1, [False]))
        self.assertEqual((self.form(b)["mode"], db.company_contacted(self.conn, b)), ("manual", False))
        self.assertIn("reCAPTCHA", self.form(b)["reason"])

    def test_failure_before_anything_is_sent_keeps_the_form_as_it_is(self) -> None:
        # 送信先につながらない（相手には何も届いていない）。人には回さず、送った扱いにもしない
        page = PLAIN.format(token="t").replace(
            'action="/contact/send.php"', 'action="http://a.localhost:1/contact/send.php"'
        )
        a = self.one(page)
        s = self.send()
        self.assertEqual((s.sent, s.failed, s.unknown), (0, 0, 0))
        self.assertEqual(
            (
                self.form(a)["mode"],
                self.form(a)["status"],
                db.company_contacted(self.conn, a),
                db.sent_today(self.conn),
            ),
            ("auto", "approved", False, 0),
        )

    def test_wrong_or_stale_csv_approves_nothing(self) -> None:
        a = self.site("a", PLAIN.format(token="t") + '<a href="mailto:recruit@a.localhost">求人</a>')
        b = self.site("b", PLAIN.format(token="t"))
        self.crawl()
        self.cli("review")
        self.cli("form", "review")
        mail_csv, form_csv = self.root / "data" / "review.csv", self.root / "data" / "form_review.csv"

        def mark(path) -> None:  # noqa: ANN001
            with open(path, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            for r in rows:
                r["判定"] = "ok"
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)

        mark(mail_csv)
        mark(form_csv)
        # メール用とフォーム用のCSVを取り違えても、何も承認されない
        self.assertEqual(self.cli("form", "approve", "--csv", str(mail_csv))[0], 1)
        self.assertEqual(self.cli("approve", "--csv", str(form_csv))[0], 1)
        self.assertEqual(self.form(b)["status"], "pending")
        self.assertEqual(self.conn.execute("SELECT status FROM contacts").fetchone()[0], "pending")
        # 書き出したあとにフォームが変わったら、古いCSVの ok は反映しない
        _Web.sites["b"]["/contact/"] = PLAIN.format(token="t").replace(
            "<tr><th>電話番号</th>", '<tr><th>ご予算</th><td><input name="budget"></td></tr><tr><th>電話番号</th>'
        )
        self.conn.execute("UPDATE companies SET status='no_email' WHERE id=?", (b,))
        self.crawl(recrawl=True)
        code, out = self.cli("form", "approve", "--csv", str(form_csv))
        self.assertEqual((code, self.form(b)["status"]), (1, "pending"))
        self.assertIn("合わない行", out)
        del a


class Replies(Base):
    def setUp(self) -> None:
        super().setUp()
        self.shop = self.site("ginza.group", PLAIN.format(token="t"))
        self.head = self.site("group", PLAIN.format(token="t"))
        self.rental = self.site("a-shokudo.rental", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        self.assertEqual(self.send().sent, 3)

    def mail(self, sender: str, text: str, mid: str, subject: str = "Re", extra: str = "") -> str | None:
        raw = (
            f"From: {sender}\r\nTo: sales@sender.test\r\nSubject: {subject}\r\nMessage-ID: <{mid}@x>\r\n{extra}"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{text}\r\n"
        ).encode("utf-8")
        return process_message(self.conn, raw, {"sales@sender.test"})

    def test_reply_goes_to_the_exact_company(self) -> None:
        self.assertEqual(self.mail("info@group.localhost", "2 でお願いします", "m1"), "reply")
        self.assertEqual(self.conn.execute("SELECT company_id FROM replies").fetchone()[0], self.head)
        # 貸しサーバーの運営元（サイトの親ドメイン）からのメールは、店の返信ではない
        self.assertIsNone(
            self.mail("support@rental.localhost", "配信停止はこちら", "m2", subject="お知らせ（配信停止）")
        )
        self.assertFalse(db.is_suppressed(self.conn, "x@a-shokudo.rental.localhost"))

    def test_receipt_copies_are_neither_replies_nor_stops(self) -> None:
        ours = self.posted("group")[0]["body"]
        receipt = f"お問い合わせありがとうございます。以下の内容で受け付けました。\n※このメールは送信専用です。ご返信はご遠慮ください。\n\n{ours}"
        self.assertEqual(
            self.mail("info@group.localhost", receipt, "m1", subject="【自動返信】お問い合わせを受け付けました"), "auto"
        )
        self.assertEqual(
            self.mail("info@group.localhost", " ".join(ours.split()), "m2", subject="お問い合わせ内容"), "auto"
        )
        self.assertEqual(self.mail("info@group.localhost", "3", "m3", extra="Auto-Submitted: auto-replied\r\n"), "auto")
        self.assertFalse(db.is_suppressed(self.conn, "info@group.localhost"))
        self.assertIn("返信 0社 / 停止 0社", self.cli("stats")[1])
        # 人の返事に、こちらの文面が引用符なしで付いていても、相手が書いた部分だけを読む
        self.assertEqual(
            self.mail("tantou@group.localhost", f"2 です。よろしくお願いします。\n\n{ours}", "m4"), "reply"
        )
        self.assertEqual(self.conn.execute("SELECT choice FROM replies WHERE kind='reply'").fetchone()[0], 2)
        self.assertEqual(self.mail("boss@group.localhost", f"今後は不要です。\n\n{ours}", "m5"), "optout")
        self.assertTrue(db.is_suppressed(self.conn, "anyone@group.localhost"))


class AssistFixes(Base):
    def test_odd_urls_and_broken_drafts_do_not_break_the_screen(self) -> None:
        guarded = CF7.format(api="", token="t") + '<script src="https://www.google.com/recaptcha/api.js"></script>'
        a = self.site("a", guarded)
        self.crawl()
        self.conn.execute("UPDATE forms SET page_url='javascript:alert(1)'")
        cfg = load_config(self.root, env=dict(self.env, SERVICE_START=""))
        app = Assist(self.conn, cfg, self.template)
        app.port = 8123
        status, _, body = app.handle("GET", f"/{app.token}/", "127.0.0.1:8123")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn('href="javascript:', page)
        self.assertIn("開けないURLです", page)
        self.assertIn("この会社の文面を作れません", page)
        self.assertIn("あとで", page)
        del a


if __name__ == "__main__":
    unittest.main()

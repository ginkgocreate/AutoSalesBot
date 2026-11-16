"""Result-page regressions use local fixture servers only."""

from __future__ import annotations

import html
import unittest

from autosales import db
from autosales.assist import Assist
from autosales.formsend import _judge_cf7_rest, judge
from test_forms import Base, PLAIN, URL, _Web


BODY = "照明と音楽を切り替える仕組みについてのご案内です。"


def confirmation(body: str, button: str = "送信する", action: str = "/contact/send.php") -> str:
    return (
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="body" value="{html.escape(body, quote=True)}">'
        f'<input type="submit" name="send" value="{button}"></form>'
    )


class ResultPageRegression(unittest.TestCase):
    def state(self, page: str, **kwargs):
        return judge(page, URL, 200, "body", BODY, ours=("sales@sender.test",), **kwargs)

    def test_empty_returned_form_with_static_instructions_is_unknown(self):
        for success in ("", "<p>送信が完了しました。</p>"):
            for redirected in (False, True):
                with self.subTest(success=success, redirected=redirected):
                    page = success + "<p>※メールアドレスは半角で正しく入力してください。</p>" + PLAIN.format(token="a")
                    self.assertEqual(self.state(page, redirected=redirected)[0], "unknown")

    def test_unrelated_header_form_is_not_the_confirmation(self):
        header = '<form method="post" action="/cart/"><input type="hidden" name="mode" value="view"><button>カートを見る</button></form>'
        state, _, follow = self.state(header + f"<p>{BODY}</p>" + confirmation(BODY))
        self.assertEqual(state, "confirm")
        self.assertEqual(follow["action"], "https://shop.test/contact/send.php")

    def test_completion_never_submits_newsletter_or_navigation(self):
        for form in (
            '<form method="post" action="/newsletter/"><input type="hidden" name="mail" value="sales@sender.test"><button>メルマガにも登録する</button></form>',
            '<form method="get" action="/"><button>トップページへ戻る</button></form>',
            '<form method="get" action="/"><button>トップページへ</button></form>',
        ):
            with self.subTest(form=form):
                state, _, follow = self.state(f"<p>送信が完了しました。</p><p>{BODY}</p>" + form)
                self.assertEqual(state, "sent")
                self.assertIsNone(follow)

    def test_prefilled_newsletter_email_is_not_a_returned_inquiry(self):
        page = (
            '<p>送信が完了しました。</p><form method="post" action="/newsletter/">'
            '<input type="email" name="nl" value="sales@sender.test"><button>登録</button></form>'
        )
        self.assertEqual(self.state(page)[0], "sent")

    def test_explicit_completion_with_preserved_inputs_is_not_retried(self):
        for message in ("送信が完了しました。", "お問い合わせありがとうございました。"):
            with self.subTest(message=message):
                page = f"<p>{message}</p>" + PLAIN.format(token="t").replace(
                    'name="mail"', 'name="mail" value="sales@sender.test"'
                )
                self.assertEqual(self.state(page)[0], "unknown")

    def test_conditional_validation_help_is_not_a_rejection(self):
        page = "<p>入力内容に誤りがある場合は訂正してから送信してください。</p>" + PLAIN.format(token="a")
        self.assertEqual(self.state(page)[0], "unknown")

    def test_back_form_does_not_hide_the_forward_confirmation(self):
        state, _, follow = self.state(confirmation(BODY, "戻る", "/contact/") + confirmation(BODY))
        self.assertEqual(state, "confirm")
        self.assertEqual(follow["action"], "https://shop.test/contact/send.php")

    def test_multiple_forward_confirmations_are_not_submitted(self):
        state, _, follow = self.state(confirmation(BODY) + confirmation(BODY, action="/contact/other.php"))
        self.assertNotEqual(state, "confirm")
        self.assertIsNone(follow)

    def test_partial_body_or_wrong_field_cannot_authorize_a_followup(self):
        for page in (
            confirmation(BODY[:30] + "別の内容"),
            confirmation(BODY).replace('name="body"', 'name="newsletter_text"'),
        ):
            with self.subTest(page=page):
                self.assertEqual(self.state(f"<p>{BODY}</p>" + page)[0], "unknown")

    def test_completion_and_hidden_echo_do_not_authorize_another_post(self):
        for note in ("", "<p>※メールアドレスは正しく入力してください。</p>"):
            with self.subTest(note=note):
                state, _, follow = self.state("<p>送信が完了しました。</p>" + note + confirmation(BODY))
                self.assertEqual(state, "unknown")
                self.assertIsNone(follow)

    def test_completion_with_echoed_values_and_static_help_is_not_retried(self):
        page = "<p>送信が完了しました。</p><p>※メールアドレスは正しく入力してください。</p>"
        page += PLAIN.format(token="t").replace('name="mail"', 'name="mail" value="sales@sender.test"')
        self.assertEqual(self.state(page)[0], "unknown")

    def test_completion_help_text_does_not_cancel_delivery_evidence(self):
        for page in (
            "送信が完了しました。確認メールが届かない場合は、メールアドレスが正しく入力されていない可能性があります。",
            "お問い合わせありがとうございました。届かない場合は、お手数ですがもう一度お問い合わせください。",
        ):
            with self.subTest(page=page):
                self.assertEqual(self.state(f"<p>{page}</p>")[0], "sent")

    def test_explicit_delivery_failure_is_not_reported_as_success(self):
        for page in (
            "お問い合わせありがとうございます。このお問い合わせは送信できませんでした。",
            "送信が完了しませんでした。",
            "送信に失敗しました。もう一度やり直してください。",
        ):
            with self.subTest(page=page):
                self.assertEqual(self.state(f"<p>{page}</p>")[0], "unknown")

    def test_unclassified_cf7_http_errors_remain_unknown(self):
        for code in (400, 415, 429):
            with self.subTest(code=code):
                self.assertEqual(_judge_cf7_rest(code, '{"message":"request rejected"}')[0], "unknown")

    def test_unrelated_status_attribute_cannot_turn_delivery_into_failure(self):
        page = '<div data-status="failed">ウィジェット</div><p>送信が完了しました。</p>'
        self.assertEqual(self.state(page)[0], "sent")

    def test_multiple_cf7_statuses_cannot_make_the_inquiry_retryable(self):
        page = (
            '<form class="wpcf7-form invalid" data-status="invalid"><textarea name="other"></textarea></form>'
            '<form class="wpcf7-form sent" data-status="sent"><textarea name="body"></textarea></form>'
        )
        self.assertEqual(self.state(page)[0], "unknown")

    def test_cf7_status_requires_the_original_body_field(self):
        page = '<form class="wpcf7-form invalid" data-status="invalid"><textarea name="other"></textarea></form>'
        self.assertEqual(self.state(page)[0], "unknown")

    def test_cf7_failure_conflicting_with_completion_or_redirect_is_unknown(self):
        form = '<form class="wpcf7-form invalid" data-status="invalid"><textarea name="body"></textarea></form>'
        self.assertEqual(self.state(form, redirected=True)[0], "unknown")
        self.assertEqual(self.state("<p>送信が完了しました。</p>" + form)[0], "unknown")

    def test_cf7_status_requires_the_original_form_kind_and_id(self):
        form = (
            '<form class="wpcf7-form invalid" data-status="invalid">'
            '<input type="hidden" name="_wpcf7" value="99"><textarea name="body"></textarea></form>'
        )
        self.assertEqual(self.state(form, form_kind="html")[0], "unknown")
        self.assertEqual(self.state(form, form_kind="cf7", cf7_id="12")[0], "unknown")
        self.assertEqual(self.state(form, form_kind="cf7", cf7_id="99")[0], "failed")


class ResultPageDeliveryRegression(Base):
    def prepare(self, post):
        company = self.site("a", PLAIN.format(token="t"), post=post)
        self.crawl()
        self.approve_all()
        return company

    def test_accepted_post_with_instructions_cannot_reenter_manual_queue(self):
        company = self.prepare(
            lambda p, f, h: (
                200,
                "<p>送信が完了しました。</p><p>必須項目は必ず入力してください。</p>" + PLAIN.format(token="t"),
                {},
            )
        )
        summary = self.send()
        self.assertEqual((summary.failed, summary.unknown), (0, 1))
        self.assertTrue(db.company_contacted(self.conn, company))
        self.assertEqual(db.sent_today(self.conn), 1)
        self.assertEqual(Assist(self.conn, self.cfg, self.template).targets(), [])
        self.send()
        self.assertEqual(len(self.posted("a")), 1)

    def test_header_cart_never_receives_a_post(self):
        def post(path, fields, headers):
            if path != "/contact/send.php":
                return 200, "<p>カート</p>", {}
            if fields.get("send"):
                return 200, "<p>送信が完了しました。</p>", {}
            header = '<form method="post" action="/cart/"><input type="hidden" name="mode" value="view"><button>カートを見る</button></form>'
            return 200, header + f"<p>{html.escape(fields['body'])}</p>" + confirmation(fields["body"]), {}

        company = self.prepare(post)
        summary = self.send()
        self.assertEqual((summary.sent, self.sends(company)), (1, ["sent"]))
        self.assertEqual([path for _, path, _, _ in _Web.received], ["/contact/send.php", "/contact/send.php"])

    def test_completion_with_newsletter_never_posts_twice(self):
        def post(path, fields, headers):
            if path != "/contact/send.php":
                return 200, "<p>登録しました。</p>", {}
            page = (
                f"<p>送信が完了しました。</p><p>{html.escape(fields['body'])}</p>"
                '<form method="post" action="/newsletter/"><input type="hidden" name="mail" value="sales@sender.test">'
                "<button>メルマガにも登録する</button></form>"
            )
            return 200, page, {}

        company = self.prepare(post)
        summary = self.send()
        self.assertEqual((summary.sent, summary.failed), (1, 0))
        self.assertTrue(db.company_contacted(self.conn, company))
        self.send()
        self.assertEqual([path for _, path, _, _ in _Web.received], ["/contact/send.php"])

    def test_completion_with_return_button_never_enters_manual_queue(self):
        def post(path, fields, headers):
            return (
                200,
                (
                    f"<p>送信が完了しました。</p><p>{html.escape(fields['body'])}</p>"
                    '<form action="/" method="get"><button>トップへ戻る</button></form>'
                ),
                {},
            )

        company = self.prepare(post)
        summary = self.send()
        self.assertEqual((summary.sent, summary.failed), (1, 0))
        self.assertTrue(db.company_contacted(self.conn, company))
        self.assertEqual(Assist(self.conn, self.cfg, self.template).targets(), [])
        self.send()
        self.assertEqual(len(self.posted("a")), 1)

    def test_plain_delivery_with_unrelated_cf7_failure_is_not_retried(self):
        page = (
            '<p>送信が完了しました。</p><form class="wpcf7-form invalid" data-status="invalid">'
            '<input type="hidden" name="_wpcf7" value="99"><textarea name="body"></textarea></form>'
        )
        company = self.prepare(lambda p, f, h: (200, page, {}))
        summary = self.send()
        self.assertEqual((summary.failed, summary.unknown), (0, 1))
        self.assertTrue(db.company_contacted(self.conn, company))
        self.assertEqual(Assist(self.conn, self.cfg, self.template).targets(), [])
        self.send()
        self.assertEqual(len(self.posted("a")), 1)

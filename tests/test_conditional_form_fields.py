"""Keep conditionally displayed reservation fields out of automatic delivery."""

import unittest

from autosales.forms import fill, find_contact_form


def contact_form(extra):
    return (
        '<h1>お問い合わせ</h1><form action="/contact/send" method="post">'
        '<label>お問い合わせ種別<select name="category" required>'
        '<option value="general">その他のお問い合わせ</option>'
        '<option value="booking">テラス席のご予約</option></select></label>'
        '<label>メールアドレス<input type="email" name="email" required></label>'
        f'{extra}<label>本文<textarea name="body" required></textarea></label>'
        '<button type="submit">送信</button></form>'
    )


class ConditionalFormFields(unittest.TestCase):
    def test_hidden_reservation_controls_require_manual_review(self):
        for attrs in ('style="display:none"', "hidden", 'class="d-none"'):
            with self.subTest(attrs=attrs):
                html = contact_form(
                    f"<div {attrs}>"
                    '<label>ご利用人数<input name="guests" required></label>'
                    '<label>日にち<input type="date" name="date" required></label>'
                    '<label>時間<select name="hour"><option value="22">22</option></select></label>'
                    "</div>"
                )
                plan = find_contact_form(html, "https://venue.test/contact/")
                self.assertEqual(plan.mode, "manual")
                self.assertIn("表示が切り替わる可能性", plan.reason)
                roles = {item["name"]: item["role"] for item in plan.items}
                for name in ("guests", "date", "hour"):
                    self.assertEqual(roles[name], "empty")

    def test_real_honeypots_and_hidden_tokens_keep_existing_behavior(self):
        html = contact_form(
            '<input type="hidden" name="csrf" value="token123">'
            '<div style="display:none"><input name="website"></div>'
            "<div hidden><label>ご利用人数（空欄のまま）"
            '<input name="booking_honeypot" required></label></div>'
        )
        plan = find_contact_form(html, "https://venue.test/contact/")
        self.assertEqual(plan.mode, "auto")
        values = dict(fill(plan.items, {"email": "sender@sender.test"}, "Subject", "Message"))
        self.assertEqual(values["csrf"], "token123")
        self.assertEqual(values["website"], "")
        self.assertEqual(values["booking_honeypot"], "")

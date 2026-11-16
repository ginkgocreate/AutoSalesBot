"""Regression tests for human replies and the evidence used by form approval."""

from __future__ import annotations

import csv

from autosales import db
from autosales.replies import process_message

try:
    from tests.test_forms import Base, PLAIN, _Web
except ImportError:
    from test_forms import Base, PLAIN, _Web


class HumanFormReplies(Base):
    def setUp(self) -> None:
        super().setUp()
        self.company = self.site("a", PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        self.assertEqual(self.send().sent, 1)

    def reply(self, text: str, subject: str = "Re: お問い合わせ", extra: str = ""):
        raw = (
            "From: tantou@a.localhost\r\nTo: sales@sender.test\r\n"
            f"Subject: {subject}\r\nMessage-ID: <handoff@a.localhost>\r\n{extra}"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{text}"
        ).encode("utf-8")
        return process_message(self.conn, raw, {"sales@sender.test"})

    def test_polite_human_optout_is_not_an_automatic_receipt(self) -> None:
        result = self.reply("お問い合わせありがとうございます。ご案内は不要です。今後ご連絡はお控えください。")
        self.assertEqual(result, "optout")
        self.assertTrue(db.is_suppressed(self.conn, "anyone@a.localhost"))

    def test_human_answer_in_a_receipt_thread_is_counted(self) -> None:
        result = self.reply(
            "営業担当です。ご提案ありがとうございます。2番でお願いします。",
            subject="Re: 【自動返信】お問い合わせを受け付けました",
        )
        self.assertEqual(result, "reply")
        self.assertEqual(self.conn.execute("SELECT choice FROM replies").fetchone()[0], 2)

    def test_quoted_receipt_does_not_hide_a_human_optout(self) -> None:
        self.assertEqual(self.reply("今後ご連絡は不要です。\n> このメールは自動返信です。"), "optout")
        self.assertTrue(db.is_suppressed(self.conn, "anyone@a.localhost"))

    def test_explicit_automatic_receipt_with_unneeded_reply_footer_is_ignored(self) -> None:
        self.assertEqual(
            self.reply(
                "お問い合わせありがとうございます。このメールは送信専用です。返信は不要です。",
                subject="お問い合わせ受付",
            ),
            "auto",
        )
        self.assertFalse(db.is_suppressed(self.conn, "anyone@a.localhost"))

    def test_automatic_header_prevents_copy_from_becoming_an_optout(self) -> None:
        self.assertEqual(
            self.reply("お問い合わせを受け付けました。返信不要です。", extra="Auto-Submitted: auto-replied\r\n"), "auto"
        )
        self.assertFalse(db.is_suppressed(self.conn, "anyone@a.localhost"))

    def test_copied_outgoing_body_is_still_not_an_optout(self) -> None:
        self.assertEqual(self.reply(self.posted("a")[0]["body"], subject="お問い合わせ内容"), "auto")
        self.assertFalse(db.is_suppressed(self.conn, "anyone@a.localhost"))


class FormApprovalEvidence(Base):
    def setUp(self) -> None:
        super().setUp()
        self.company = self.site(
            "a",
            PLAIN.format(token="t"),
            top='<a href="/contact/">お問い合わせ</a><a href="/contact/other">お問い合わせの案内</a>',
        )
        _Web.sites["a"]["/contact/other"] = "<p>お問い合わせの案内</p>"
        self.crawl()

    def approved_csv(self):
        self.cli("form", "review")
        path = self.root / "data" / "form_review.csv"
        with path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            row["判定"] = "ok"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def unread_contact_page(self) -> None:
        del _Web.sites["a"]["/contact/other"]
        self.crawl(recrawl=True)
        self.assertIn("contact_unread", self.form(self.company)["flags"])

    def test_new_warning_invalidates_old_csv_without_changing_form_structure(self) -> None:
        path = self.approved_csv()
        self.unread_contact_page()
        self.assertEqual(self.cli("form", "approve", "--csv", str(path))[0], 1)
        self.assertEqual(self.form(self.company)["status"], "pending")
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(_Web.received, [])

    def test_new_warning_revokes_existing_approval(self) -> None:
        self.approve_all()
        self.unread_contact_page()
        self.assertEqual(self.form(self.company)["status"], "pending")
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(_Web.received, [])

    def test_fresh_warning_review_can_be_explicitly_approved(self) -> None:
        self.unread_contact_page()
        path = self.approved_csv()
        self.assertEqual(self.cli("form", "approve", "--csv", str(path))[0], 0)
        self.assertEqual(self.form(self.company)["status"], "approved")

    def test_existing_rejection_survives_changed_warnings(self) -> None:
        self.cli("form", "approve", "--reject", str(self.form(self.company)["id"]))
        self.unread_contact_page()
        self.assertEqual(self.form(self.company)["status"], "rejected")

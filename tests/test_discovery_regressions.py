"""Regression coverage for REST routes and visible no-sales declarations."""

from __future__ import annotations

import unittest
from html import escape
from urllib.parse import parse_qsl, urlsplit

from autosales.extract import extract_emails, find_refusal, parse_html
from autosales.forms import find_contact_form

try:
    from tests.test_forms import Base, CF7, PLAIN, URL, _Web
except ImportError:
    from test_forms import Base, CF7, PLAIN, URL, _Web


class DiscoveryRegressions(unittest.TestCase):
    def test_cf7_rest_root_preserves_pretty_permalink_path(self) -> None:
        route = "/contact-form-7/v1/contact-forms/12/feedback"
        for api in (
            "https://shop.test/wp-json",
            "https://shop.test/wp-json/",
            "https://shop.test/blog/index.php/wp-json",
        ):
            with self.subTest(api=api):
                plan = find_contact_form(CF7.format(api=api, token="t"), URL)
                self.assertEqual(plan.rest_url, api.rstrip("/") + route)

    def test_cf7_rest_root_preserves_query_route_and_other_parameters(self) -> None:
        for api in (
            "https://shop.test/?rest_route=/",
            "https://shop.test/index.php?rest_route=%2F",
            "https://shop.test/blog/index.php?lang=ja&rest_route=/&flag=",
        ):
            with self.subTest(api=api):
                plan = find_contact_form(CF7.format(api=escape(api), token="t"), URL)
                actual = urlsplit(plan.rest_url)
                original = urlsplit(api)
                query = dict(parse_qsl(actual.query, keep_blank_values=True))
                self.assertEqual(
                    (actual.scheme, actual.netloc, actual.path), (original.scheme, original.netloc, original.path)
                )
                self.assertEqual(query.pop("rest_route"), "/contact-form-7/v1/contact-forms/12/feedback")
                self.assertEqual(
                    query, {k: v for k, v in parse_qsl(original.query, keep_blank_values=True) if k != "rest_route"}
                )

    def test_cf7_external_rest_root_remains_unused(self) -> None:
        plan = find_contact_form(CF7.format(api="https://elsewhere.test/?rest_route=/", token="t"), URL)
        self.assertEqual(plan.rest_url, "")

    def test_visible_submit_value_and_button_text_block_sales(self) -> None:
        for button in (
            '<input type="submit" value="営業目的のお問い合わせはお断りします">',
            '<input type="SUBMIT" value="営業目的ではないことを確認して送信">',
            '<input type="button" value="営業目的ではないことを確認して送信">',
            '<button type="submit">営業目的ではないことを確認して送信</button>',
            '<button type="submit" aria-label="営業目的のお問い合わせはお断りします">進む</button>',
        ):
            with self.subTest(button=button):
                self.assertIsNotNone(find_refusal(parse_html(button, URL)))

    def test_button_values_are_not_collected_as_contact_addresses(self) -> None:
        page = parse_html('<input type="submit" value="test@shop.test">', URL)
        self.assertEqual(extract_emails(page), [])

    def test_hidden_values_and_regular_sales_buttons_are_not_refusals(self) -> None:
        for html in (
            '<input type="hidden" value="営業目的ではないことを確認して送信">',
            '<script><input type="submit" value="営業目的ではないことを確認して送信"></script>',
            '<input type="submit" value="営業提案を送信する">',
            "<button>営業時間ではないことを確認して送信</button>",
        ):
            with self.subTest(html=html):
                self.assertIsNone(find_refusal(parse_html(html, URL)))


class DiscoverySendingRegressions(Base):
    def test_cf7_query_route_is_used_for_local_fixture_submission(self) -> None:
        api = f"http://a.localhost:{self.port}/index.php?rest_route=/&lang=ja"
        company = self.site(
            "a", CF7.format(api=escape(api), token="t"), post=lambda p, f, h: (200, '{"status":"mail_sent"}', {})
        )
        self.crawl()
        self.approve_all()
        self.assertEqual(self.send().sent, 1)
        posted = urlsplit(_Web.received[0][1])
        self.assertEqual(posted.path, "/index.php")
        self.assertEqual(
            dict(parse_qsl(posted.query)), {"rest_route": "/contact-form-7/v1/contact-forms/12/feedback", "lang": "ja"}
        )
        self.assertEqual(self.sends(company), ["sent"])

    def test_no_sales_submit_declaration_prevents_crawl_and_send(self) -> None:
        original = PLAIN.format(token="t")
        refusal = original.replace('value="確認画面へ"', 'value="営業目的ではないことを確認して送信"')
        before = self.site("before", refusal)
        after = self.site("after", original)
        self.crawl()
        self.approve_all()
        self.assertIsNone(self.form(before))
        _Web.sites["after"]["/contact/"] = refusal
        self.assertEqual(self.send().sent, 0)
        self.assertEqual(_Web.received, [])
        for company in (before, after):
            self.assertEqual(
                self.conn.execute("SELECT status FROM companies WHERE id=?", (company,)).fetchone()[0], "refused"
            )

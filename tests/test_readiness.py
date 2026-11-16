"""Check configuration readiness without connecting or exposing configured values."""

import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosales.config import Config
from autosales.readiness import inspect_readiness


class Readiness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "templates").mkdir()
        (self.root / "templates" / "pilot.txt").write_text(
            "件名: ご相談\n{{会社名}}\n{{送信者会社名}}の{{担当者名}}です。\n{{デモURL}}", encoding="utf-8"
        )
        self.values = {
            "SENDER_COMPANY": "秘密の送信会社",
            "SENDER_PERSON": "秘密の担当者",
            "SENDER_ADDRESS": "秘密の署名住所",
            "SENDER_EMAIL": "sender@business-mail.co.jp",
            "SMTP_HOST": "smtp.business-mail.co.jp",
            "SMTP_USER": "smtp-private-user",
            "SMTP_PASS": "smtp-private-password",
            "IMAP_HOST": "imap.business-mail.co.jp",
            "IMAP_USER": "imap-private-user",
            "IMAP_PASS": "imap-private-password",
        }

    def report(self, **overrides):
        return inspect_readiness(Config(self.root, dict(self.values, **overrides)), template_names=("pilot",))

    def test_configured_report_is_redacted_and_has_no_external_or_database_io(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        with (
            mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("network not allowed")),
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network not allowed")),
            mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database not allowed")),
        ):
            result = self.report()
        for stage in ("research", "draft", "email", "replies"):
            self.assertTrue(result[f"{stage}_ready"])
            self.assertEqual(result["blockers"][stage], [])
        self.assertFalse(result["delivery_verified"])
        self.assertEqual(result["readiness_state"], "production_configured")
        serialized = json.dumps(result, ensure_ascii=False)
        for value in self.values.values():
            self.assertNotIn(value, serialized)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_empty_sender_and_mail_settings_keep_research_available(self):
        result = inspect_readiness(Config(self.root), template_names=("pilot",))
        self.assertTrue(result["research_ready"])
        self.assertFalse(result["draft_ready"])
        self.assertFalse(result["email_ready"])
        self.assertFalse(result["replies_ready"])
        self.assertEqual(result["readiness_state"], "research_only")
        self.assertIn("SENDER_COMPANY", " ".join(result["blockers"]["draft"]))
        self.assertIn("SMTP_HOST", " ".join(result["blockers"]["email"]))

    def test_dummy_sender_can_make_drafts_but_is_not_production_ready(self):
        for email in ("sender@example.invalid", "sender@example.com", "sender@test.example.org", "sender@fixture.test"):
            with self.subTest(email=email):
                result = self.report(SENDER_EMAIL=email)
                self.assertTrue(result["draft_ready"])
                self.assertFalse(result["email_ready"])
                self.assertEqual(result["readiness_state"], "draft_only")
                self.assertNotIn(email, json.dumps(result))

    def test_invalid_numeric_settings_do_not_echo_the_value(self):
        for key in ("SMTP_PORT", "IMAP_PORT", "DAILY_LIMIT", "SEND_INTERVAL_SEC", "CRAWL_MAX_PAGES", "CRAWL_DELAY_SEC"):
            with self.subTest(key=key):
                value = "secret-value-must-never-appear"
                result = self.report(**{key: value})
                self.assertTrue(any(key in item["message"] and not item["ok"] for item in result["checks"]))
                self.assertNotIn(value, json.dumps(result))
        for port in ("0", "65536", "-1"):
            self.assertFalse(self.report(SMTP_PORT=port)["email_ready"])
        for interval in ("-1", "NaN", "inf"):
            self.assertFalse(self.report(SEND_INTERVAL_SEC=interval)["email_ready"])

    def test_credentials_and_transport_settings_are_consistent(self):
        for changes in (
            {"SMTP_USER": ""},
            {"SMTP_PASS": ""},
            {"SMTP_USER": "", "SMTP_PASS": ""},
            {"SMTP_SECURITY": "none"},
            {"SMTP_SECURITY": "misspelled"},
            {"SMTP_SECURITY": " starttls "},
            {"SMTP_HOST": "https://smtp.business-mail.co.jp/"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.report(**changes)["email_ready"])
        local = self.report(SMTP_HOST="127.0.0.1", SMTP_SECURITY="none", SMTP_USER="", SMTP_PASS="")
        self.assertTrue(local["email_ready"])
        for key in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS"):
            self.assertFalse(self.report(**{key: ""})["replies_ready"])

    def test_email_headers_and_optional_reply_addresses_are_validated(self):
        for key in ("SENDER_EMAIL", "REPLY_TO", "OPTOUT_EMAIL"):
            for value in (
                "wrong-address",
                "a..b@business-mail.co.jp",
                "a@business-mail.co.jp\n",
                "a@business-mail.co.jp\nBcc: hidden@other.co.jp",
            ):
                with self.subTest(key=key, value=value):
                    result = self.report(**{key: value})
                    self.assertFalse(result["draft_ready"])
                    self.assertFalse(result["email_ready"])
                    self.assertNotIn(value, json.dumps(result))

    def test_template_missing_malformed_unknown_or_unfilled_is_not_draft_ready(self):
        path = self.root / "templates" / "pilot.txt"
        for body in (None, "本文だけ", "件名: 件名\n{{未知の項目}}", "件名: 件名\n{{提供開始時期}}"):
            with self.subTest(body=body):
                if body is None:
                    path.unlink()
                else:
                    path.write_text(body, encoding="utf-8")
                result = self.report()
                self.assertFalse(result["draft_ready"])
        path.write_text("件名: 件名\n{{提供開始時期}}", encoding="utf-8")
        self.assertTrue(self.report(SERVICE_START="調整中")["draft_ready"])

    def test_template_paths_cannot_escape_the_template_directory(self):
        private = self.root / "private.txt"
        private.write_text("private-data-do-not-read", encoding="utf-8")
        (self.root / "templates" / "linked.txt").symlink_to(private)
        for name in ("../private", "linked", "/tmp/private"):
            with self.subTest(name=name):
                result = inspect_readiness(Config(self.root, self.values), template_names=(name,))
                self.assertFalse(result["draft_ready"])
                self.assertNotIn("private-data-do-not-read", json.dumps(result))

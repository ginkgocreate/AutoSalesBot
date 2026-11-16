"""Validate the offline operator workflow and its network boundary."""

import json
import socket
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

from autosales import cli
from scripts.dry_run_smoke import network_guard, run_smoke


class SmokeWorkflow(unittest.TestCase):
    def test_encoded_email_body_with_unresolved_placeholder_fails(self):
        original_main = cli.main_for_test

        def corrupt_draft(cfg, conn, argv):
            result = original_main(cfg, conn, argv)
            if argv[0] == "send":
                path = next((cfg.root / "outbox").rglob("*.eml"))
                message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                message.set_content("本文の{{未差し込み}}を検出する", cte="base64")
                path.write_bytes(message.as_bytes())
                self.assertNotIn(b"{{", path.read_bytes())
            return result

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(cli, "main_for_test", corrupt_draft):
                with self.assertRaisesRegex(RuntimeError, "Unresolved draft placeholder"):
                    run_smoke(Path(folder))

    def test_default_run_creates_reviewed_drafts_without_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_smoke(Path(folder))
            self.assertEqual(report["status"], "passed")
            self.assertEqual(
                report["companies"], {"total": 4, "mail": 1, "auto_form": 1, "manual_form": 1, "refused": 1}
            )
            self.assertEqual(
                report["dry_run"],
                {
                    "email_drafts": 1,
                    "form_drafts": 1,
                    "smtp_connections": 0,
                    "smtp_messages": 0,
                    "form_posts": 0,
                    "delivery_records": 0,
                },
            )
            self.assertEqual(report["network"]["blocked_attempts"], [])
            self.assertIsNone(report["local_delivery"])
            artifact = Path(report["artifact_dir"])
            self.assertEqual(json.loads((artifact / "result.json").read_text())["status"], "passed")
            self.assertTrue((artifact / "transcript.log").exists())
            self.assertTrue((artifact / "dry-run.sqlite3").exists())
            self.assertEqual(len(list((artifact / "outbox").rglob("*.eml"))), 1)
            self.assertEqual(len(list((artifact / "outbox").rglob("*.txt"))), 1)
            commands = [s["argv"] for s in report["steps"]]
            self.assertTrue(any(c[:2] == ["approve", "--csv"] for c in commands))
            self.assertTrue(any(c[:3] == ["form", "approve", "--csv"] for c in commands))
            self.assertFalse(any("--live" in c for c in commands))

    def test_local_delivery_is_separate_and_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_smoke(Path(folder), local_delivery=True)
            self.assertEqual(report["dry_run"]["delivery_records"], 0)
            self.assertEqual(
                report["local_delivery"],
                {
                    "smtp_messages": 1,
                    "form_posts": 1,
                    "delivery_records": 2,
                    "duplicate_delivery_count": 0,
                    "reply_choice": 2,
                    "optout_count": 1,
                    "manual_pending": 1,
                },
            )
            self.assertEqual(report["network"]["blocked_attempts"], [])
            self.assertIn("real_smtp_imap_and_external_sites", report["not_verified"])

    def test_network_guard_rejects_external_dns_and_socket_before_io(self):
        with network_guard(set()) as trace:
            with self.assertRaises(OSError):
                socket.getaddrinfo("example.com", 443)
            with socket.socket() as sock:
                with self.assertRaises(OSError):
                    sock.connect(("203.0.113.1", 443))
                with self.assertRaises(OSError):
                    sock.connect_ex(("127.0.0.1", 25))
        self.assertEqual(len(trace["blocked_attempts"]), 3)
        self.assertEqual(trace["connections"], [])

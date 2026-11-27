"""Exercise readiness diagnostics without touching the operational database."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosales import cli


class ReadinessCLI(unittest.TestCase):
    def test_json_diagnosis_does_not_create_or_open_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"ASB_HOME": folder}, clear=True),
                mock.patch.object(cli.db, "connect", side_effect=AssertionError("Unexpected DB access")),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(["doctor", "--json"])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertFalse(report["email_ready"])
            self.assertFalse(report["delivery_verified"])
            self.assertFalse((root / "data").exists())

    def test_required_email_fails_without_exposing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("SMTP_PASS=private-token\nSMTP_PORT=private-invalid-port\n")
            output = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"ASB_HOME": folder}, clear=True),
                mock.patch.object(cli.db, "connect", side_effect=AssertionError("Unexpected DB access")),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(["doctor", "--require", "email"])
            self.assertEqual(code, 1)
            self.assertIn("未確認", output.getvalue())
            self.assertNotIn("private-token", output.getvalue())
            self.assertNotIn("private-invalid-port", output.getvalue())

    def test_template_selection_and_draft_requirement_are_forwarded(self):
        report = {
            "research_ready": True,
            "draft_ready": True,
            "email_ready": False,
            "replies_ready": False,
            "readiness_state": "draft_only",
            "delivery_verified": False,
            "blockers": {"research": [], "draft": [], "email": [], "replies": []},
        }
        with tempfile.TemporaryDirectory() as folder:
            with (
                mock.patch.dict(os.environ, {"ASB_HOME": folder}, clear=True),
                mock.patch("autosales.readiness.inspect_readiness", return_value=report) as inspect,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["doctor", "--template", "pilot", "--require", "draft"]), 0)
            self.assertEqual(inspect.call_args.kwargs["template_names"], ("pilot",))

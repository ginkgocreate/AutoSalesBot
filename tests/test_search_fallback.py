"""Verify explicit fallback without concealing malformed or partial search results."""

import json
import subprocess
import unittest
from unittest import mock

from autosales import db, discover


def osm_payload():
    return {
        "elements": [
            {
                "type": "node",
                "id": index,
                "tags": {"name": f"Venue {index}", "amenity": "nightclub", "website": f"https://venue{index}.test/"},
            }
            for index in range(4)
        ]
    }


class SearchFallback(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.logs = []
        self.fetch = mock.Mock(return_value=osm_payload())

    def call(self, **kwargs):
        options = {
            "fallback_osm_area": "大阪市",
            "fallback_osm_kinds": ["nightclub"],
            "log": self.logs.append,
            "fetch": self.fetch,
        }
        options.update(kwargs)
        return discover.discover_search_with_fallback(self.conn, "大阪の運営会社", **options)

    def test_unavailable_search_uses_explicit_map_source_and_limit(self):
        run = mock.Mock(side_effect=discover.SearchUnavailableError("rate_limit"))
        counts = self.call(run=run, segment="honbu", limit=2)
        rows = self.conn.execute("SELECT * FROM companies ORDER BY id").fetchall()
        self.assertEqual(counts["added"], 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["source"] == "osm:大阪市" for r in rows))
        self.assertTrue(all(r["segment"] == "" and r["status"] == "new" for r in rows))
        self.assertIn("地図登録の店舗候補", "\n".join(self.logs))
        self.fetch.assert_called_once()
        self.assertIn('"name"="大阪市"', self.fetch.call_args.args[0])

    def test_explicit_map_segment_does_not_reuse_search_segment(self):
        run = mock.Mock(side_effect=discover.SearchUnavailableError("authentication"))
        self.call(run=run, segment="honbu", fallback_segment="show", limit=1)
        self.assertEqual(self.conn.execute("SELECT segment FROM companies").fetchone()[0], "show")

    def test_unavailable_search_without_opt_in_does_not_contact_map(self):
        for reason in ("missing_cli", "authentication", "rate_limit", "timeout"):
            with self.subTest(reason=reason):
                run = mock.Mock(side_effect=discover.SearchUnavailableError(reason))
                with self.assertRaises(discover.SearchUnavailableError):
                    self.call(run=run, fallback_osm_area=None, fallback_osm_kinds=None)
        self.fetch.assert_not_called()

    def test_empty_success_is_not_an_availability_failure(self):
        counts = self.call(run=lambda p, t: json.dumps({"structured_output": {"candidates": []}}))
        self.assertEqual(counts["added"], 0)
        self.fetch.assert_not_called()

    def test_success_keeps_the_company_search_provenance(self):
        output = json.dumps({"structured_output": {"candidates": [{"name": "Group", "url": "https://group.test/"}]}})
        counts = self.call(run=lambda p, t: output, segment="honbu")
        row = self.conn.execute("SELECT source,segment FROM companies").fetchone()
        self.assertEqual(counts["added"], 1)
        self.assertEqual(tuple(row), ("search:大阪の運営会社", "honbu"))
        self.fetch.assert_not_called()

    def test_bad_results_are_not_replaced_by_map_candidates(self):
        for output in (
            "{incomplete",
            json.dumps({"is_error": True, "result": "invalid schema secret-fixture"}),
            json.dumps({"result": '{"candidates": ['}),
        ):
            with self.subTest(output=output):
                with self.assertRaises(discover.DiscoverError) as raised:
                    self.call(run=lambda p, t: output)
                self.assertNotIsInstance(raised.exception, discover.SearchUnavailableError)
                self.assertNotIn("secret-fixture", str(raised.exception))
        self.fetch.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 0)

    def test_embedded_candidate_results_and_fragments_prevent_fallback(self):
        fragments = [
            '{"candidates":[{"name":"Verified group","url":"https://group.test/"}]}',
            '[{"name":"Verified group","url":"https://group.test/"}]',
            '{"candidates":[{"name":"Verified group"',
            '```json\n{"candi',
            json.dumps(
                {
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": 'Partial result: {"candidates": ['},
                }
            ),
        ]
        for fragment in fragments:
            for exit_code in (0, 1):
                with self.subTest(fragment=fragment, exit_code=exit_code):
                    payload = json.dumps(
                        {"is_error": True, "result": f"Partial result: {fragment}\nAPI Error: rate_limit_error"}
                    )
                    done = subprocess.CompletedProcess(["claude"], exit_code, payload, "")
                    with mock.patch.object(discover.shutil, "which", return_value="claude"):
                        with mock.patch.object(discover.subprocess, "run", return_value=done):
                            with self.assertRaises(discover.DiscoverError) as raised:
                                self.call(run=discover._run_claude)
                    self.assertNotIsInstance(raised.exception, discover.SearchUnavailableError)
                    self.assertIn("途中", str(raised.exception))
        self.fetch.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 0)

    def test_complete_provider_quota_error_still_allows_fallback(self):
        provider_error = json.dumps(
            {"type": "error", "error": {"type": "rate_limit_error", "message": "Usage limit reached"}}
        )
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                payload = json.dumps({"is_error": True, "result": f"API Error: 429 {provider_error}"})
                done = subprocess.CompletedProcess(["claude"], exit_code, payload, "")
                with mock.patch.object(discover.shutil, "which", return_value="claude"):
                    with mock.patch.object(discover.subprocess, "run", return_value=done):
                        self.call(run=discover._run_claude, limit=1)
        self.assertEqual(self.fetch.call_count, 2)

    def test_invalid_fallback_configuration_fails_before_search(self):
        run = mock.Mock()
        for options in (
            {"fallback_osm_area": None},
            {"fallback_osm_kinds": None},
            {"fallback_osm_area": " "},
            {"fallback_osm_kinds": ["unknown"]},
            {"limit": 0},
        ):
            with self.subTest(options=options):
                with self.assertRaises(discover.DiscoverError):
                    self.call(run=run, **options)
        run.assert_not_called()
        self.fetch.assert_not_called()

    def test_partial_map_response_propagates_without_import(self):
        run = mock.Mock(side_effect=discover.SearchUnavailableError("timeout"))
        self.fetch.return_value = {"elements": osm_payload()["elements"], "remark": "Query timed out"}
        with self.assertRaises(discover.DiscoverError):
            self.call(run=run)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 0)


class ClaudeAvailability(unittest.TestCase):
    def test_missing_command_and_empty_timeout_have_typed_safe_errors(self):
        with mock.patch.object(discover.shutil, "which", return_value=None):
            with self.assertRaises(discover.SearchUnavailableError) as raised:
                discover._run_claude("query", 1)
        self.assertEqual(raised.exception.reason, "missing_cli")
        with mock.patch.object(discover.shutil, "which", return_value="claude"):
            with mock.patch.object(discover.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 1)):
                with self.assertRaises(discover.SearchUnavailableError) as raised:
                    discover._run_claude("query", 1)
        self.assertEqual(raised.exception.reason, "timeout")

    def test_quota_and_authentication_messages_are_classified_without_echoing_secrets(self):
        cases = [
            ("You've hit your 5-hour limit · resets 10:30pm secret-fixture", "rate_limit"),
            ("You've hit your limit · resets tomorrow secret-fixture", "rate_limit"),
            ("API Error: 429 rate_limit_error secret-fixture", "rate_limit"),
            ("OAuth token expired; please log in secret-fixture", "authentication"),
            ("Invalid API key secret-fixture", "authentication"),
        ]
        for detail, reason in cases:
            for exit_code in (0, 1):
                with self.subTest(reason=reason, exit_code=exit_code):
                    payload = json.dumps({"is_error": True, "result": detail})
                    done = subprocess.CompletedProcess(["claude"], exit_code, payload, "")
                    with mock.patch.object(discover.shutil, "which", return_value="claude"):
                        with mock.patch.object(discover.subprocess, "run", return_value=done):
                            with self.assertRaises(discover.SearchUnavailableError) as raised:
                                discover.parse_search_output(discover._run_claude("query", 1))
                    self.assertEqual(raised.exception.reason, reason)
                    self.assertNotIn("secret-fixture", str(raised.exception))

    def test_invalid_cli_arguments_are_not_provider_outages(self):
        done = subprocess.CompletedProcess(["claude"], 1, "", "invalid argument: --login secret-fixture")
        with mock.patch.object(discover.shutil, "which", return_value="claude"):
            with mock.patch.object(discover.subprocess, "run", return_value=done):
                with self.assertRaises(discover.DiscoverError) as raised:
                    discover._run_claude("query", 1)
        self.assertNotIsInstance(raised.exception, discover.SearchUnavailableError)
        self.assertNotIn("secret-fixture", str(raised.exception))

    def test_partial_output_does_not_become_an_availability_failure(self):
        failures = [
            subprocess.CompletedProcess(["claude"], 1, '{"candidates": [', "rate limit secret-fixture"),
            subprocess.CompletedProcess(
                ["claude"],
                1,
                json.dumps({"structured_output": {"candidates": []}, "result": "rate limit"}),
                "",
            ),
            subprocess.TimeoutExpired("claude", 1, output=b'{"candidates": ['),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with mock.patch.object(discover.shutil, "which", return_value="claude"):
                    with mock.patch.object(discover.subprocess, "run", **kwargs):
                        with self.assertRaises(discover.DiscoverError) as raised:
                            discover._run_claude("query", 1)
                self.assertNotIsInstance(raised.exception, discover.SearchUnavailableError)
                self.assertNotIn("secret-fixture", str(raised.exception))

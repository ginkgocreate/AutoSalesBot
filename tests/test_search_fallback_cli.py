"""Keep fallback discovery explicit and separate from headquarters targeting."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosales import cli, db
from autosales.config import Config


class SearchFallbackCLI(unittest.TestCase):
    def test_explicit_osm_options_are_forwarded_without_headquarters_segment(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = Config(Path(folder))
            conn = db.connect(":memory:")
            try:
                with (
                    mock.patch(
                        "autosales.discover.discover_search_with_fallback",
                        create=True,
                        return_value={"added": 0, "duplicate": 0, "invalid": 0, "non_official": 0},
                    ) as search,
                    mock.patch.object(cli, "discover_search", side_effect=AssertionError("Unexpected primary call")),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = cli.main_for_test(
                        cfg,
                        conn,
                        [
                            "discover",
                            "search",
                            "大阪の運営会社",
                            "--segment",
                            "honbu",
                            "--limit",
                            "3",
                            "--fallback-osm-area",
                            "大阪市",
                            "--fallback-osm-kinds",
                            "nightclub,music_venue",
                        ],
                    )
                self.assertEqual(code, 0)
                self.assertEqual(search.call_args.kwargs["limit"], 3)
                self.assertEqual(search.call_args.kwargs["segment"], "honbu")
                self.assertEqual(search.call_args.kwargs["fallback_segment"], "")
                self.assertEqual(search.call_args.kwargs["fallback_osm_kinds"], ["nightclub", "music_venue"])
            finally:
                conn.close()

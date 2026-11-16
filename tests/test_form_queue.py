"""Exercise queue progress without sending to external sites."""

from autosales import db
from autosales.config import load_config
from autosales.formsend import send_forms

try:
    from tests.test_forms import Base, PLAIN, _Web
except ImportError:
    from test_forms import Base, PLAIN, _Web


class FormQueue(Base):
    def prepare_queue(self, missing: int, available: int) -> None:
        names = [f"site{i}" for i in range(missing + available)]
        for name in names:
            self.site(name, PLAIN.format(token="t"))
        self.crawl()
        self.approve_all()
        for name in names[:missing]:
            del _Web.sites[name]["/contact/"]

    def test_unavailable_forms_do_not_starve_later_companies(self) -> None:
        self.prepare_queue(missing=5, available=2)
        summary = self.send()
        self.assertEqual(summary.sent, 2)
        self.assertEqual(summary.skipped, 5)
        self.assertEqual(summary.skipped_limit, 0)
        self.assertEqual(db.sent_today(self.conn), 2)
        self.assertEqual([site for site, *_ in _Web.received], ["site5", "site6"])

    def test_explicit_limit_applies_after_skipping_unavailable_forms(self) -> None:
        self.prepare_queue(missing=2, available=3)
        summary = self.send(limit=1)
        self.assertEqual((summary.sent, summary.skipped, summary.skipped_limit), (1, 2, 2))
        self.assertEqual(len(_Web.received), 1)

    def test_skipped_forms_are_reported_without_exhausting_daily_limit(self) -> None:
        self.prepare_queue(missing=6, available=0)
        summary = self.send()
        self.assertEqual((summary.sent, summary.failed, summary.skipped), (0, 0, 6))
        self.assertEqual(summary.skipped_limit, 0)
        self.assertEqual(db.sent_today(self.conn), 0)
        self.assertEqual(_Web.received, [])

    def test_daily_limit_still_bounds_posts_after_skips(self) -> None:
        self.prepare_queue(missing=3, available=3)
        self.cfg = load_config(self.root, env=dict(self.env, DAILY_LIMIT="1"))
        summary = self.send()
        self.assertEqual((summary.sent, summary.skipped, summary.skipped_limit), (1, 3, 2))
        self.assertEqual(db.sent_today(self.conn), 1)
        self.assertEqual(len(_Web.received), 1)

    def test_draft_limit_does_not_contact_sites(self) -> None:
        self.prepare_queue(missing=2, available=2)
        summary = send_forms(self.conn, self.cfg, self.template, limit=1, log=self.out.append)
        self.assertEqual((summary.dry_run, summary.skipped_limit), (1, 3))
        self.assertEqual(_Web.received, [])

"""Shared published addresses must not receive repeated mail or form outreach."""

from __future__ import annotations

from autosales import db
from autosales.assist import Assist
from test_forms import Base, PLAIN, _Web


class SharedPublishedContacts(Base):
    def candidates(self) -> tuple[int, int, int]:
        shared = '<a href="mailto:honbu@group.test">mail</a>'
        a = self.site("a", PLAIN.format(token="a") + shared)
        b = self.site("b", PLAIN.format(token="b") + shared)
        independent = self.site("independent", PLAIN.format(token="c"))
        self.crawl()
        self.approve_all()
        self.conn.execute("UPDATE contacts SET status='rejected'")
        self.conn.commit()
        return a, b, independent

    def record(self, company_id: int, status: str) -> None:
        self.conn.execute(
            "INSERT INTO form_sends(form_id,company_id,template,status,sent_at) VALUES (?,?,?,?,?)",
            (self.form(company_id)["id"], company_id, "honbu_form", status, db.now()),
        )
        self.conn.commit()

    def test_batch_posts_once_to_a_shared_address_without_limit(self) -> None:
        a, b, independent = self.candidates()
        result = self.send()
        self.assertEqual([site for site, _, _, _ in _Web.received], ["a", "independent"])
        self.assertEqual(
            (result.sent, self.sends(a), self.sends(b), self.sends(independent)), (2, ["sent"], [], ["sent"])
        )

    def test_sent_unknown_and_manual_block_other_company_forms(self) -> None:
        a, b, independent = self.candidates()
        for status in ("sent", "unknown", "manual"):
            with self.subTest(status=status):
                self.conn.execute("DELETE FROM form_sends")
                self.record(a, status)
                self.assertEqual([row["company_id"] for row in db.sendable_forms(self.conn)], [independent])
                self.assertNotIn(b, [row["company_id"] for row in db.sendable_forms(self.conn)])

    def test_shared_company_is_removed_from_manual_assistance(self) -> None:
        a, b, independent = self.candidates()
        self.conn.execute("UPDATE forms SET mode='manual'")
        self.record(a, "manual")
        app = Assist(self.conn, self.cfg, self.template)
        self.assertEqual([row["company_id"] for row in app.targets()], [independent])
        self.assertNotIn(b, [row["company_id"] for row in app.targets()])

    def test_shared_company_other_email_is_also_blocked(self) -> None:
        a, b, independent = self.candidates()
        self.conn.execute(
            "INSERT INTO contacts(company_id,email,source_url,status,found_at) VALUES (?,?,?,'approved',?)",
            (b, "other@b.localhost", "http://b.localhost/contact/", db.now()),
        )
        self.conn.execute(
            "INSERT INTO contacts(company_id,email,source_url,status,found_at) VALUES (?,?,?,'approved',?)",
            (independent, "info@independent.localhost", "http://independent.localhost/contact/", db.now()),
        )
        self.conn.execute("UPDATE companies SET status='crawled'")
        self.record(a, "unknown")
        self.assertEqual([row["company_id"] for row in db.sendable_contacts(self.conn)], [independent])

    def test_mail_delivery_blocks_other_forms_sharing_any_published_address(self) -> None:
        a, b, independent = self.candidates()
        contact = self.conn.execute("SELECT id FROM contacts WHERE company_id=?", (a,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO sends(contact_id,template,subject,status,sent_at) VALUES (?,'honbu','fixture','unknown',?)",
            (contact, db.now()),
        )
        self.conn.commit()
        self.assertEqual([row["company_id"] for row in db.sendable_forms(self.conn)], [independent])
        self.assertNotIn(b, [row["company_id"] for row in db.sendable_forms(self.conn)])

    def test_dry_run_and_failed_records_do_not_claim_a_shared_address(self) -> None:
        a, b, independent = self.candidates()
        for status in ("dry_run", "failed"):
            with self.subTest(status=status):
                self.conn.execute("DELETE FROM form_sends")
                self.record(a, status)
                self.assertEqual([row["company_id"] for row in db.sendable_forms(self.conn)], [a, b, independent])

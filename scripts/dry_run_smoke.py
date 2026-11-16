"""Exercise the real CLI against isolated localhost fixtures, never external services."""

from __future__ import annotations

import argparse
import csv
import http.server
import io
import json
import os
import shutil
import socket
import socketserver
import sqlite3
import sys
import tempfile
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from autosales import cli, db, formsend  # noqa: E402
from autosales.assist import Assist  # noqa: E402
from autosales.config import Config  # noqa: E402
from autosales.crawler import Crawler  # noqa: E402
from autosales.mailer import load_template  # noqa: E402
from autosales.replies import process_message  # noqa: E402

HOSTS = {f"{name}.localhost" for name in ("mail", "form", "captcha", "refused")}
FORM = """<h1>お問い合わせ</h1><form action="/contact/send.php" method="post">
<label>お名前<input name="name" required></label>
<label>メールアドレス<input name="email" type="email" required></label>
<label>お問い合わせ内容<textarea name="body" required></textarea></label>
<button type="submit">送信する</button></form>"""


@contextmanager
def network_guard(allowed_ports: set[int]):
    """Allow only this harness's listeners, including during DNS resolution."""
    trace: dict[str, list] = {"connections": [], "blocked_attempts": []}
    resolve, connect, connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex

    def deny(value):
        trace["blocked_attempts"].append(str(value))
        raise OSError("Smoke test blocked a connection outside its localhost fixtures")

    def checked_address(address):
        if not isinstance(address, tuple) or len(address) < 2:
            return deny(address)
        host, port = address[:2]
        if host not in ("127.0.0.1", "::1") or port not in allowed_ports:
            return deny(address)
        trace["connections"].append({"host": host, "port": port})

    def safe_resolve(host, port, *args, **kwargs):
        if host in HOSTS or host == "localhost":
            host = "127.0.0.1"
        if host not in ("127.0.0.1", "::1") or int(port) not in allowed_ports:
            return deny((host, port))
        return resolve(host, port, *args, **kwargs)

    def safe_connect(sock, address):
        checked_address(address)
        return connect(sock, address)

    def safe_connect_ex(sock, address):
        checked_address(address)
        return connect_ex(sock, address)

    proxies = {key: "" for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    proxies.update({"NO_PROXY": "*", "no_proxy": "*"})
    with (
        mock.patch("socket.getaddrinfo", safe_resolve),
        mock.patch("socket.socket.connect", safe_connect),
        mock.patch("socket.socket.connect_ex", safe_connect_ex),
        mock.patch("socket.getfqdn", return_value="localhost"),
        mock.patch.dict(os.environ, proxies),
    ):
        yield trace


class _Web(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "").split(":", 1)[0]
        self.server.requests.append({"method": "GET", "host": host, "path": self.path})
        if self.path == "/robots.txt":
            return self.reply(200, "User-agent: *\nAllow: /\n", "text/plain")
        if self.path == "/":
            return self.reply(200, '<h1>架空の店舗</h1><a href="/contact/">お問い合わせ</a>')
        if self.path != "/contact/":
            return self.reply(404, "Not found")
        pages = {
            "mail.localhost": '<h1>お問い合わせ</h1><a href="mailto:info@mail.localhost">info@mail.localhost</a>',
            "form.localhost": FORM,
            "captcha.localhost": FORM + '<div class="g-recaptcha"></div>',
            "refused.localhost": FORM + "<p>営業目的のお問い合わせはお断りします。</p>",
        }
        return self.reply(200, pages[host])

    def do_POST(self):
        host = self.headers.get("Host", "").split(":", 1)[0]
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append({"method": "POST", "host": host, "path": self.path})
        return self.reply(200, "<p>送信が完了しました。ありがとうございました。</p>")

    def reply(self, status, text, content_type="text/html; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _SMTP(socketserver.StreamRequestHandler):
    def handle(self):
        self.server.connections += 1
        self.wfile.write(b"220 fixture ESMTP\r\n")
        recipients = []
        while line := self.rfile.readline():
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith(("HELO", "EHLO")):
                self.wfile.write(b"250-fixture\r\n250 8BITMIME\r\n")
            elif upper.startswith("MAIL FROM"):
                recipients = []
                self.wfile.write(b"250 ok\r\n")
            elif upper.startswith("RCPT TO"):
                recipients.append(command.split(":", 1)[1].strip().strip("<>"))
                self.wfile.write(b"250 ok\r\n")
            elif upper == "DATA":
                self.wfile.write(b"354 continue\r\n")
                lines = []
                while (part := self.rfile.readline()) not in (b".\r\n", b""):
                    lines.append(part[1:] if part.startswith(b"..") else part)
                self.server.messages.append({"recipients": recipients, "bytes": len(b"".join(lines))})
                self.wfile.write(b"250 accepted\r\n")
            elif upper == "QUIT":
                self.wfile.write(b"221 goodbye\r\n")
                return
            elif upper == "RSET":
                self.wfile.write(b"250 ok\r\n")
            else:
                self.wfile.write(b"502 unsupported\r\n")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _approve_csv(path: Path, *, forms=False):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields, rows = reader.fieldnames, list(reader)
    for row in rows:
        row["判定"] = "ok" if not forms or row["送り方"] == "自動" else ""
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _snapshot(conn, path):
    target = sqlite3.connect(path)
    try:
        conn.backup(target)
    finally:
        target.close()


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _delivery_records(conn):
    return conn.execute(
        "SELECT (SELECT COUNT(*) FROM sends WHERE status!='dry_run') "
        "+ (SELECT COUNT(*) FROM form_sends WHERE status!='dry_run')"
    ).fetchone()[0]


def run_smoke(output_dir: Path, *, local_delivery: bool = False) -> dict:
    """Save reproducible evidence without loading credentials or changing an existing DB."""
    output_dir = Path(output_dir).resolve()
    if output_dir == REPO or REPO in output_dir.parents:
        raise ValueError("Evidence must be outside the application repository")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = Path(tempfile.mkdtemp(prefix="run-", dir=output_dir))
    web, smtp = _Server(("127.0.0.1", 0), _Web), _Server(("127.0.0.1", 0), _SMTP)
    web.requests, smtp.messages, smtp.connections = [], [], 0
    for server in (web, smtp):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {
        "status": "running",
        "artifact_dir": str(artifact),
        "steps": [],
        "local_delivery": None,
        "not_verified": [
            "external_candidate_discovery",
            "real_smtp_imap_and_external_sites",
            "browser_manual_delivery",
        ],
    }
    transcript = []
    try:
        with tempfile.TemporaryDirectory(prefix="analink-smoke-") as working:
            root = Path(working)
            shutil.copytree(REPO / "templates", root / "templates")
            cfg = Config(
                root,
                {
                    "SENDER_COMPANY": "株式会社サンプル（架空）",
                    "SENDER_PERSON": "テスト 担当",
                    "SENDER_ADDRESS": "架空住所1-1",
                    "SENDER_EMAIL": "sales@sender.test",
                    "SERVICE_START": "調整中",
                    "DEMO_URL": "https://example.invalid/demo",
                    "SMTP_HOST": "127.0.0.1",
                    "SMTP_PORT": str(smtp.server_address[1]),
                    "SMTP_SECURITY": "none",
                    "DAILY_LIMIT": "20",
                    "SEND_INTERVAL_SEC": "0",
                    "CRAWL_DELAY_SEC": "0",
                },
            )
            conn = db.connect(root / "data" / "fixture.sqlite3")

            def command(*argv):
                buffer = io.StringIO()
                with redirect_stdout(buffer), redirect_stderr(buffer):
                    code = cli.main_for_test(cfg, conn, list(argv))
                output = buffer.getvalue()
                report["steps"].append({"argv": list(argv), "exit_code": code, "output": output})
                transcript.append("$ autosales " + " ".join(argv) + "\n" + output)
                _require(code == 0, f"CLI command failed: {argv}")

            try:
                seeds = root / "seeds.csv"
                with seeds.open("w", encoding="utf-8", newline="") as file:
                    writer = csv.writer(file)
                    writer.writerow(["会社名", "サイト", "エリア", "業態", "店舗数", "層"])
                    for kind in ("mail", "form", "captcha", "refused"):
                        writer.writerow(
                            [
                                f"架空{kind}社",
                                f"http://{kind}.localhost:{web.server_address[1]}/",
                                "大阪",
                                "ラウンジ",
                                1,
                                "honbu",
                            ]
                        )
                ports = {web.server_address[1], smtp.server_address[1]}

                def fixture_crawler(**kwargs):
                    return Crawler(**dict(kwargs, allow_private=True))

                original_send_forms = formsend.send_forms

                def fixture_send_forms(*args, **kwargs):
                    return original_send_forms(*args, **dict(kwargs, crawler=Crawler(delay=0, allow_private=True)))

                with (
                    network_guard(ports) as trace,
                    mock.patch.object(cli, "Crawler", fixture_crawler),
                    mock.patch.object(cli, "send_forms", fixture_send_forms),
                ):
                    report["network"] = trace
                    command("import", str(seeds))
                    command("crawl")
                    counts = {r["host"]: r["status"] for r in conn.execute("SELECT host,status FROM companies")}
                    _require(
                        counts
                        == {
                            "mail.localhost": "crawled",
                            "form.localhost": "no_email",
                            "captcha.localhost": "no_email",
                            "refused.localhost": "refused",
                        },
                        "Unexpected crawl classification",
                    )
                    modes = {
                        r["host"]: r["mode"]
                        for r in conn.execute("SELECT c.host,f.mode FROM forms f JOIN companies c ON c.id=f.company_id")
                    }
                    _require(
                        modes == {"form.localhost": "auto", "captcha.localhost": "manual"},
                        "Unexpected form classification",
                    )
                    report["companies"] = {"total": 4, "mail": 1, "auto_form": 1, "manual_form": 1, "refused": 1}
                    _require(
                        not db.sendable_contacts(conn) and not db.sendable_forms(conn), "Unapproved automatic target"
                    )
                    command("review")
                    command("form", "review")
                    _approve_csv(root / "data" / "review.csv")
                    _approve_csv(root / "data" / "form_review.csv", forms=True)
                    command("approve", "--csv", str(root / "data" / "review.csv"))
                    command("form", "approve", "--csv", str(root / "data" / "form_review.csv"))
                    command("send", "--template", "honbu", "--segment", "honbu")
                    command("form", "send", "--template", "honbu_form", "--segment", "honbu")
                    report["dry_run"] = {
                        "email_drafts": len(list((root / "outbox").rglob("*.eml"))),
                        "form_drafts": len(list((root / "outbox").rglob("*.txt"))),
                        "smtp_connections": smtp.connections,
                        "smtp_messages": len(smtp.messages),
                        "form_posts": sum(r["method"] == "POST" for r in web.requests),
                        "delivery_records": _delivery_records(conn),
                    }
                    _require(
                        report["dry_run"]
                        == {
                            "email_drafts": 1,
                            "form_drafts": 1,
                            "smtp_connections": 0,
                            "smtp_messages": 0,
                            "form_posts": 0,
                            "delivery_records": 0,
                        },
                        "Dry-run performed delivery or missed a draft",
                    )
                    for path in (root / "outbox").rglob("*"):
                        if path.is_file():
                            if path.suffix == ".eml":
                                message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                                text = (
                                    str(message.get("Subject", ""))
                                    + "\n"
                                    + "\n".join(
                                        part.get_content()
                                        for part in message.walk()
                                        if part.get_content_maintype() == "text" and not part.is_multipart()
                                    )
                                )
                            else:
                                text = path.read_text(encoding="utf-8")
                            _require("{{" not in text, "Unresolved draft placeholder")
                    _snapshot(conn, artifact / "dry-run.sqlite3")
                    command("stats")
                    if local_delivery:
                        command("send", "--template", "honbu", "--segment", "honbu", "--live", "--yes")
                        command("form", "send", "--template", "honbu_form", "--segment", "honbu", "--live", "--yes")
                        before = len(smtp.messages) + sum(r["method"] == "POST" for r in web.requests)
                        command("send", "--template", "honbu", "--segment", "honbu", "--live", "--yes")
                        command("form", "send", "--template", "honbu_form", "--segment", "honbu", "--live", "--yes")
                        for address, body, ident, expected in [
                            ("info@mail.localhost", "2番でお願いします。", "reply", "reply"),
                            ("owner@form.localhost", "ご案内は不要です。今後連絡しないでください。", "stop", "optout"),
                        ]:
                            msg = EmailMessage()
                            msg["From"], msg["To"], msg["Message-ID"], msg["Subject"] = (
                                address,
                                "sales@sender.test",
                                f"<{ident}@fixture.localhost>",
                                "Re: ご案内",
                            )
                            msg.set_content(body)
                            _require(
                                process_message(conn, msg.as_bytes(), {"sales@sender.test"}) == expected,
                                "Reply classification mismatch",
                            )
                        report["local_delivery"] = {
                            "smtp_messages": len(smtp.messages),
                            "form_posts": sum(r["method"] == "POST" for r in web.requests),
                            "delivery_records": _delivery_records(conn),
                            "duplicate_delivery_count": len(smtp.messages)
                            + sum(r["method"] == "POST" for r in web.requests)
                            - before,
                            "reply_choice": conn.execute("SELECT choice FROM replies WHERE kind='reply'").fetchone()[0],
                            "optout_count": conn.execute("SELECT COUNT(*) FROM replies WHERE kind='optout'").fetchone()[
                                0
                            ],
                            "manual_pending": len(
                                Assist(conn, cfg, load_template(root / "templates" / "honbu_form.txt")).targets()
                            ),
                        }
                        _require(
                            report["local_delivery"]
                            == {
                                "smtp_messages": 1,
                                "form_posts": 1,
                                "delivery_records": 2,
                                "duplicate_delivery_count": 0,
                                "reply_choice": 2,
                                "optout_count": 1,
                                "manual_pending": 1,
                            },
                            "Local delivery verification failed",
                        )
                        _require(
                            db.is_suppressed(conn, "owner@form.localhost"), "Optout was not added to the stop list"
                        )
                        transcript.append(
                            "Reply fixtures: reply choice=2; optout=1. process_message used directly; no IMAP connection.\n"
                        )
                        command("stats")
                        _snapshot(conn, artifact / "local-delivery.sqlite3")
                    command("export", "--out", str(root / "data" / "export.csv"))
                    _require(not trace["blocked_attempts"], "A workflow attempted external I/O")
                shutil.copytree(root / "outbox", artifact / "outbox")
                for path in (root / "data").glob("*.csv"):
                    shutil.copy2(path, artifact / path.name)
                shutil.copy2(seeds, artifact / "seeds.csv")
                report["http_requests"] = web.requests
                report["smtp_messages"] = smtp.messages
                report["status"] = "passed"
            finally:
                conn.close()
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for server in (web, smtp):
            server.shutdown()
            server.server_close()
        (artifact / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (artifact / "transcript.log").write_text("\n".join(transcript), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.gettempdir()) / "analink-dry-run-evidence")
    parser.add_argument(
        "--local-delivery", action="store_true", help="Also test delivery only to temporary localhost servers"
    )
    args = parser.parse_args()
    report = run_smoke(args.output_dir, local_delivery=args.local_delivery)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("status", "artifact_dir", "companies", "dry_run", "local_delivery", "not_verified")
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

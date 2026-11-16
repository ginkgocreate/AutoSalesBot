"""人がブラウザで送るフォームの補助画面。

CAPTCHA のあるフォームなど、自動では送らないものを、人が1件ずつ確かめて送るための画面。
この画面は入力する内容（下書き）を見せるだけで、相手のサイトには何も送らない。
自分のPCの中（127.0.0.1）だけで動き、URL に含まれる合言葉を知らないと開けない。
"""

from __future__ import annotations

import html
import http.server
import json
import secrets
import sqlite3
from typing import Callable
from urllib.parse import parse_qs

from . import db
from .config import Config
from .forms import describe_items
from .formsend import render_for, sender_values
from .mailer import Template, TemplateError, _send_lock

_STYLE = """
:root{color-scheme:light dark;--bg:#f6f6f4;--card:#fff;--ink:#1c1c1a;--sub:#66665f;--line:#d9d9d2;--accent:#1f5fbf;--warn:#9a5b00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--ink:#eeeee8;--sub:#a3a39a;--line:#3a3a36;--accent:#7fb0ff;--warn:#e3a84a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 -apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px;color:var(--sub);font-weight:600}
.meta{color:var(--sub);font-size:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:16px}
.why{color:var(--warn);font-size:14px;margin:6px 0 0}
.row{display:flex;gap:8px;align-items:flex-start;margin:8px 0}.row label{flex:0 0 7.5em;font-size:14px;color:var(--sub);padding-top:8px}
.row input,.row textarea{flex:1;min-width:0;font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:transparent;color:inherit}
.row textarea{height:16em;line-height:1.6}
button,a.btn{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;text-decoration:none;display:inline-block}
a.btn.primary,button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){a.btn.primary,button.primary{color:#0b1b33}}
.copy{flex:0 0 auto}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;align-items:center}
.actions input{font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:transparent;color:inherit;flex:1;min-width:10em}
ol{padding-left:1.3em;margin:8px 0}.url{word-break:break-all}
"""
_SCRIPT = """
document.querySelectorAll('button.copy').forEach(function(b){b.addEventListener('click',function(){
  var el=document.getElementById(b.dataset.for);el.select();
  var done=function(){var t=b.textContent;b.textContent='コピーしました';setTimeout(function(){b.textContent=t},1200)};
  if(navigator.clipboard){navigator.clipboard.writeText(el.value).then(done,function(){document.execCommand('copy');done()})}
  else{document.execCommand('copy');done()}
})});
"""


class Assist:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, template: Template, segment: str | None = None) -> None:
        self.conn, self.cfg, self.template, self.segment = conn, cfg, template, segment
        self.token = secrets.token_urlsafe(18)
        self.later: set[int] = set()  # 「あとで」にしたもの。この起動の間は出さない
        self.done = False
        self.flash = ""  # 直前の操作の結果。次に画面を出すときに1回だけ見せる
        self.sent = 0
        self.port = 0

    # ---- 相手 ----
    def targets(self) -> list[sqlite3.Row]:
        rows = db.sendable_forms(self.conn, segment=self.segment, mode="manual")
        return [r for r in rows if r["form_id"] not in self.later]

    def room(self) -> int:
        return max(0, self.cfg.get_int("DAILY_LIMIT") - db.sent_today(self.conn))

    # ---- 画面 ----
    def _fields(self, row: sqlite3.Row) -> list[tuple[str, str, bool]]:
        sender = sender_values(self.cfg)
        subject, body = render_for(self.template, self.cfg, row)
        out = [("会社名", sender["company"], False), ("お名前", sender["person"], False)]
        if sender["kana"]:
            out.append(("フリガナ", sender["kana"], False))
        out.append(("メール", sender["email"], False))
        if sender["tel"]:
            out.append(("電話番号", sender["tel"], False))
        if sender["zip"]:
            out.append(("郵便番号", sender["zip"], False))
        out += [("住所", sender["address"], False), ("件名", subject, False), ("本文", body, True)]
        return out

    def page(self, message: str = "") -> str:
        e = html.escape
        base = f"/{self.token}/"
        targets = self.targets()
        room = self.room()
        limit = self.cfg.get_int("DAILY_LIMIT")
        head = (
            f"<p class=meta>今日の送信 {limit - room} / 上限 {limit}（メールと合わせた数）　残り {len(targets)}社</p>"
        )
        if message:
            head += f"<p class=why>{e(message)}</p>"
        if room <= 0:
            body = "<div class=card><h1>今日の上限に達しました</h1><p>続きは明日以降に送ってください。</p></div>"
        elif not targets:
            body = (
                "<div class=card><h1>人が送るフォームは、もうありません</h1>"
                "<p>「あとで」にしたものは、次に起動したときに出てきます。</p></div>"
            )
        else:
            row = targets[0]
            try:
                items = json.loads(row["plan"]).get("items", []) if row["plan"] else []
            except ValueError:
                items = []
            hint = describe_items(items)
            rows = []
            try:
                values = self._fields(row)
                problem = ""
            except TemplateError as err:
                # この会社だけ文面を作れない（差し込みが空など）。画面は出して、後回しか見送りにできるようにする
                values, problem = [], f"この会社の文面を作れません: {err}"
            for i, (label, value, multi) in enumerate(values):
                field = (
                    f"<textarea id=v{i} readonly>{e(value)}</textarea>"
                    if multi
                    else f'<input id=v{i} readonly value="{e(value, quote=True)}">'
                )
                rows.append(
                    f"<div class=row><label for=v{i}>{e(label)}</label>{field}"
                    f"<button type=button class=copy data-for=v{i}>コピー</button></div>"
                )
            page_url = row["page_url"]
            link = (
                f'<a class="btn primary" href="{e(page_url, quote=True)}" target="_blank" '
                'rel="noopener noreferrer">フォームを開く（新しいタブ）</a>'
                if page_url.lower().startswith(("http://", "https://"))
                else f"<span class=why>開けないURLです: {e(page_url)}</span>"
            )
            body = f"""
<div class=card>
  <h1>{e(row["name"])}</h1>
  <p class="meta url">{e(row["site_url"])}</p>
  <p class=why>自動で送らない理由: {e(row["reason"] or "人の確認が必要")}</p>
  <h2>手順</h2>
  <ol>
    <li>フォームを開いて、お問い合わせ用のフォームかを確かめる（予約用・求人用なら送らない）</li>
    <li>「営業のご連絡はお断り」の表示がないかを確かめる（あれば「送らない」）</li>
    <li>下の内容をコピーして貼り付け、自分で送信する</li>
    <li>この画面に戻って、結果のボタンを押す</li>
  </ol>
  {f"<p class=why>{e(problem)}</p>" if problem else ""}
  <p>{link}</p>
  {f"<p class=meta>このフォームの欄の見立て: {e(hint)}</p>" if hint else ""}
  <h2>入力する内容</h2>
  {"".join(rows)}
  <h2>結果</h2>
  <form method=post action="{base}mark" class=actions>
    <input type=hidden name=form_id value="{row["form_id"]}">
    <button class=primary name=action value=sent>送った</button>
    <button name=action value=later>あとで</button>
    <button name=action value=reject>送らない</button>
    <input name=note placeholder="送らない理由（任意）" maxlength=100>
  </form>
</div>"""
        return (
            f"<!doctype html><html lang=ja><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=referrer content=no-referrer><title>フォーム送信の補助</title><style>{_STYLE}</style></head>"
            f"<body><main>{head}{body}"
            f"<form method=post action='{base}quit' class=actions><button>終了する</button></form>"
            f"</main><script>{_SCRIPT}</script></body></html>"
        )

    # ---- 操作 ----
    def mark(self, form_id: int, action: str, note: str = "") -> str:
        row = self.conn.execute(
            "SELECT f.id, f.company_id, f.mode, f.status, co.name FROM forms f JOIN companies co ON co.id=f.company_id "
            "WHERE f.id=?",
            (form_id,),
        ).fetchone()
        if row is None:
            return "そのフォームは見つかりません"
        if action == "later":
            self.later.add(form_id)
            return ""
        if action == "reject":
            self.conn.execute(
                "UPDATE forms SET status='rejected', decided_at=?, reason=? WHERE id=?",
                (db.now(), ("人が見送り: " + note.strip())[:200] if note.strip() else "人が見送り", form_id),
            )
            self.conn.commit()
            return f"{row['name']} は送らないことにしました"
        if action == "sent":
            if db.company_contacted(self.conn, row["company_id"]):
                return f"{row['name']} は、すでに連絡済みとして記録されています"
            self.conn.execute(
                "INSERT INTO form_sends(form_id, company_id, template, status, detail, sent_at) "
                "VALUES (?,?,?, 'manual', '人がブラウザで送信', ?)",
                (form_id, row["company_id"], self.template.name, db.now()),
            )
            self.conn.commit()
            self.sent += 1
            return f"{row['name']} を送信済みにしました"
        return "分からない操作です"

    def allowed(self, path: str, host: str) -> bool:
        """別のサイトのページからこの画面を操作されないよう、宛先の名前と合言葉を確かめる。"""
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}") and path.startswith(f"/{self.token}/")

    def handle(self, method: str, path: str, host: str, body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
        """1回のやりとり。 (ステータス, ヘッダー, 中身) を返す。"""
        headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'",
        }
        base = f"/{self.token}/"
        if not self.allowed(path, host):
            return 404, headers, "見つかりません".encode("utf-8")
        route = path[len(base) :].split("?", 1)[0]
        if method == "GET" and route == "":
            message, self.flash = self.flash, ""
            return 200, headers, self.page(message).encode("utf-8")
        if method == "POST" and route == "mark":
            form = parse_qs(body.decode("utf-8", "replace"))
            try:
                form_id = int(form.get("form_id", [""])[0])
            except ValueError:
                return 400, headers, "不正な入力です".encode("utf-8")
            self.flash = self.mark(form_id, form.get("action", [""])[0], form.get("note", [""])[0])
            # 画面を読み込み直しても、同じ操作がもう一度送られないよう、元の画面に戻す
            return 303, dict(headers, Location=base), b""
        if method == "POST" and route == "quit":
            self.done = True
            return (
                200,
                headers,
                (
                    "<!doctype html><meta charset=utf-8><title>終了</title>"
                    "<p>終了しました。このタブは閉じてかまいません。</p>"
                ).encode("utf-8"),
            )
        return 404, headers, "見つかりません".encode("utf-8")


def _handler(app: Assist) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5  # 何も送ってこない接続で、画面が固まらないようにする

        def _serve(self, method: str) -> None:
            host = self.headers.get("Host", "")
            body = b""
            if app.allowed(self.path, host):  # 合言葉を確かめてから、中身を読む
                try:
                    length = max(0, min(int(self.headers.get("Content-Length") or 0), 10_000))
                except ValueError:
                    length = 0
                body = self.rfile.read(length) if length else b""
            try:
                status, headers, data = app.handle(method, self.path, host, body)
            except Exception as err:  # 1回の不備で画面全体を止めない
                status, headers, data = (
                    500,
                    {"Content-Type": "text/plain; charset=utf-8"},
                    f"エラー: {type(err).__name__}: {err}".encode("utf-8"),
                )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

        def log_message(self, *args) -> None:
            pass

    return Handler


def run_assist(
    conn: sqlite3.Connection,
    cfg: Config,
    template: Template,
    *,
    segment: str | None = None,
    port: int = 0,
    open_browser: bool = True,
    log: Callable[[str], None] = print,
) -> int:
    """補助画面を開いて、終了が押されるまで待つ。人が「送った」と記録した数を返す。"""
    missing = cfg.missing_sender_fields()
    if missing:
        raise TemplateError("送信者の表示が未設定です（.env）: " + ", ".join(missing))
    app = Assist(conn, cfg, template, segment)
    if not app.targets():
        log("人が送るフォームはありません。")
        return 0
    app.page()  # 文面の差し込みに不備があれば、画面を開く前に止まる
    # 上限と1社1回を守るため、メールやフォームの自動送信とは同時に動かさない
    with _send_lock(cfg):
        server = http.server.HTTPServer(("127.0.0.1", port), _handler(app))
        app.port = server.server_address[1]
        url = f"http://127.0.0.1:{app.port}/{app.token}/"
        log(f"補助画面: {url}")
        log("終わったら、画面の「終了する」を押すか、ここで Ctrl-C を押してください。")
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        try:
            while not app.done:
                server.handle_request()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return app.sent

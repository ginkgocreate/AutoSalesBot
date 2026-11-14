"""コマンドの入口。 python3 -m autosales <コマンド>"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from . import db
from .config import Config, load_config
from .crawler import Crawler, crawl_pending
from .importer import import_csv
from .mailer import TemplateError, load_template, render, send_batch
from .replies import fetch_replies

REVIEW_HEADERS = ["ID", "判定", "会社名", "アドレス", "注意", "掲載ページ", "層", "エリア", "業態"]
FLAG_LABELS = {
    "recruit": "求人用の可能性",
    "privacy": "個人情報窓口の可能性",
    "free_mail": "フリーメール",
    "offsite_domain": "サイトと別ドメイン（制作会社などの可能性）",
}


def _flags_ja(flags: str) -> str:
    return "・".join(FLAG_LABELS.get(f, f) for f in flags.split(",") if f)


def cmd_init(cfg: Config, conn, args) -> int:
    env = cfg.root / ".env"
    example = cfg.root / ".env.example"
    if not env.exists() and example.exists():
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        env.chmod(0o600)
        print(f"{env} を作りました。送信者の情報とSMTPの設定を書き込んでください。")
    print(f"データベース: {cfg.path('DB_PATH')}")
    missing = cfg.missing_sender_fields()
    if missing:
        print("まだ空の必須項目: " + ", ".join(missing))
    return 0


def cmd_import(cfg: Config, conn, args) -> int:
    path = Path(args.csv)
    if not path.is_file():
        print(f"ファイルがありません: {path}", file=sys.stderr)
        return 1
    c = import_csv(conn, path, source=args.source or "")
    print(f"追加 {c['added']}件 / 重複 {c['duplicate']}件 / 不備 {c['invalid']}件 / "
          f"ポータル・SNSのURLのため除外 {c['non_official']}件")
    return 0


def cmd_crawl(cfg: Config, conn, args) -> int:
    crawler = Crawler(
        user_agent=cfg.get("CRAWL_USER_AGENT"),
        delay=cfg.get_float("CRAWL_DELAY_SEC"),
        max_pages=cfg.get_int("CRAWL_MAX_PAGES"),
    )
    counts = crawl_pending(conn, crawler, limit=args.limit, recrawl=args.recrawl)
    if not counts:
        print("巡回する会社がありません（import で候補を入れてください）。")
        return 0
    labels = {"crawled": "アドレスあり", "no_email": "アドレスなし", "refused": "営業お断り",
              "blocked": "巡回不可", "error": "エラー", "skipped": "対象外"}
    print("---")
    print(" / ".join(f"{labels[k]} {counts[k]}社" for k in labels if counts.get(k)))
    print(f"新しい連絡先 {counts.get('_contacts', 0)}件。次は review で確認用のCSVを出してください。")
    return 0


def cmd_review(cfg: Config, conn, args) -> int:
    rows = conn.execute(
        "SELECT ct.id, co.name, ct.email, ct.flags, ct.source_url, co.segment, co.area, co.category "
        "FROM contacts ct JOIN companies co ON co.id = ct.company_id "
        "WHERE ct.status='pending' AND co.status='crawled' ORDER BY co.id, ct.id"
    ).fetchall()
    out = Path(args.out) if args.out else cfg.root / "data" / "review.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(REVIEW_HEADERS)
        for r in rows:
            w.writerow([r["id"], "", r["name"], r["email"], _flags_ja(r["flags"]),
                        r["source_url"], r["segment"], r["area"], r["category"]])
    print(f"確認待ち {len(rows)}件を {out} に書き出しました。")
    print("掲載ページを開いて確かめ、「判定」列に ok か ng を入れて、approve --csv で取り込んでください。")
    return 0


def _decide(conn, contact_id: int, ok: bool) -> bool:
    cur = conn.execute(
        "UPDATE contacts SET status=?, decided_at=? WHERE id=? AND status IN ('pending','approved','rejected')",
        ("approved" if ok else "rejected", db.now(), contact_id),
    )
    return cur.rowcount > 0


def cmd_approve(cfg: Config, conn, args) -> int:
    approved = rejected = 0
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                verdict = (row.get("判定") or "").strip().lower()
                cid = (row.get("ID") or "").strip()
                if not cid.isdigit() or verdict not in ("ok", "ng", "○", "×", "o", "x"):
                    continue
                ok = verdict in ("ok", "○", "o")
                if _decide(conn, int(cid), ok):
                    approved += ok
                    rejected += not ok
    for cid in args.ids or []:
        approved += _decide(conn, cid, True)
    for cid in args.reject or []:
        rejected += _decide(conn, cid, False)
    if args.unflagged:
        cur = conn.execute(
            "UPDATE contacts SET status='approved', decided_at=? WHERE status='pending' AND flags='' "
            "AND company_id IN (SELECT id FROM companies WHERE status='crawled')",
            (db.now(),),
        )
        approved += cur.rowcount
    conn.commit()
    print(f"承認 {approved}件 / 見送り {rejected}件")
    return 0


def cmd_preview(cfg: Config, conn, args) -> int:
    template = load_template(cfg.root / "templates" / f"{args.template}.txt")
    sample = {"name": args.name, "area": "奈良市", "category": "ラウンジ",
              "source_url": "https://example.co.jp/contact"}
    missing = cfg.missing_sender_fields()
    if missing:
        print("（送信者の表示が未設定のため、署名は仮の表示です: " + ", ".join(missing) + "）\n")
        for key in missing:
            cfg.values[key] = f"［{key}］"
    if not cfg.get("SERVICE_START"):
        cfg.values["SERVICE_START"] = "［提供開始時期］"
    subject, body = render(template, cfg, sample)
    print(f"件名: {subject}\n\n{body}")
    return 0


def cmd_send(cfg: Config, conn, args) -> int:
    template = load_template(cfg.root / "templates" / f"{args.template}.txt")
    targets = db.sendable_contacts(conn, segment=args.segment)
    if not targets:
        print("送れる相手がいません（承認済みで、まだ送っておらず、停止リストに無い連絡先が0件）。")
        return 0
    if args.live:
        missing = cfg.missing_sender_fields() + ([] if cfg.get("SMTP_HOST") else ["SMTP_HOST"])
        if missing:
            raise TemplateError("未設定の項目があります（.env）: " + ", ".join(missing))
        room = max(0, cfg.get_int("DAILY_LIMIT") - db.sent_today(conn))
        n = min(x for x in (room, args.limit, len(targets)) if x is not None)
        print(f"送れる相手 {len(targets)}社。今日の残り枠 {room}通。今回送るのは {n}通です。")
        if n == 0:
            return 0
        if not args.yes:
            try:
                answer = input(f"本当に {n}通 を送信しますか？ (yes と入力): ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "yes":
                print("中止しました。")
                return 1
    s = send_batch(conn, cfg, template, live=args.live, segment=args.segment, limit=args.limit)
    if args.live:
        print(f"送信 {s.sent}通 / 失敗 {s.failed}通 / 上限のため次回へ {s.skipped_limit}社")
    else:
        print(f"下書き {s.dry_run}通を outbox/ に書き出しました（まだ送っていません）。"
              "中身を確かめてから --live を付けて実行してください。")
    return 0


def cmd_replies(cfg: Config, conn, args) -> int:
    c = fetch_replies(conn, cfg, days=args.days)
    print(f"確認 {c['checked']}通 → 返信 {c['reply']}件 / 停止の連絡 {c['optout']}件 / 不達 {c['bounce']}件")
    return 0


def cmd_suppress(cfg: Config, conn, args) -> int:
    for target in args.targets:
        new = db.add_suppression(conn, target, args.reason or "手動")
        print(f"{target}: {'停止リストに入れました' if new else 'すでに停止リストにあります'}")
    return 0


def cmd_unrefuse(cfg: Config, conn, args) -> int:
    row = conn.execute("SELECT name, status, status_note FROM companies WHERE id=?", (args.company_id,)).fetchone()
    if not row or row["status"] != "refused":
        print("「営業お断り」で除外中の会社ではありません。")
        return 1
    print(f"{row['name']}: {row['status_note']}")
    conn.execute("UPDATE companies SET status='new', refusal_checked=1, "
                 "status_note='人が確認して除外を解除' WHERE id=?", (args.company_id,))
    conn.commit()
    print("除外を解除しました。crawl を実行すると、この会社のアドレスが確認待ちに入ります。")
    return 0


def cmd_stats(cfg: Config, conn, args) -> int:
    def one(sql: str, *p) -> int:
        return int(conn.execute(sql, p).fetchone()[0])

    print("■ 会社")
    labels = [("new", "未巡回"), ("crawled", "アドレスあり"), ("no_email", "アドレスなし"),
              ("refused", "営業お断り"), ("blocked", "巡回不可"), ("error", "エラー"), ("skipped", "対象外")]
    total = one("SELECT COUNT(*) FROM companies")
    print(f"  合計 {total}社")
    for key, label in labels:
        n = one("SELECT COUNT(*) FROM companies WHERE status=?", key)
        if n:
            print(f"  {label} {n}社")
    print("■ 連絡先")
    for key, label in [("pending", "確認待ち"), ("approved", "承認済み"), ("rejected", "見送り"), ("bounced", "不達")]:
        print(f"  {label} {one('SELECT COUNT(*) FROM contacts WHERE status=?', key)}件")
    print("■ 送信と反応（層ごと）")
    rows = conn.execute(
        "SELECT co.segment AS seg, "
        " COUNT(DISTINCT CASE WHEN s.status='sent' THEN ct.id END) AS sent "
        "FROM companies co JOIN contacts ct ON ct.company_id=co.id "
        "LEFT JOIN sends s ON s.contact_id=ct.id GROUP BY co.segment ORDER BY co.segment"
    ).fetchall()
    for r in rows:
        seg = r["seg"]
        base = ("FROM replies rp JOIN contacts ct ON ct.id=rp.contact_id "
                "JOIN companies co ON co.id=ct.company_id WHERE co.segment=? ")
        reply = one("SELECT COUNT(*) " + base + "AND rp.kind='reply'", seg)
        optout = one("SELECT COUNT(*) " + base + "AND rp.kind='optout'", seg)
        bounce = one("SELECT COUNT(*) " + base + "AND rp.kind='bounce'", seg)
        choices = [one("SELECT COUNT(*) " + base + "AND rp.choice=?", seg, i) for i in (1, 2, 3, 4)]
        print(f"  [{seg or '層なし'}] 送信 {r['sent']}通 / 返信 {reply}件 / 停止 {optout}件 / 不達 {bounce}件"
              f" / 番号 1:{choices[0]} 2:{choices[1]} 3:{choices[2]} 4:{choices[3]}")
    print(f"■ 停止リスト {one('SELECT COUNT(*) FROM suppressions')}件 / 今日の送信 {db.sent_today(conn)}通"
          f"（上限 {cfg.get('DAILY_LIMIT')}通）")
    return 0


def cmd_export(cfg: Config, conn, args) -> int:
    out = Path(args.out) if args.out else cfg.root / "data" / "list.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT co.id, co.name, co.site_url, co.area, co.category, co.store_count, co.segment, "
        " co.status, co.status_note, ct.email, ct.flags, ct.status AS contact_status, ct.source_url, "
        " (SELECT MAX(sent_at) FROM sends s WHERE s.contact_id=ct.id AND s.status='sent') AS sent_at, "
        " (SELECT kind || COALESCE(':' || choice, '') FROM replies rp WHERE rp.contact_id=ct.id "
        "   ORDER BY rp.id DESC LIMIT 1) AS reply "
        "FROM companies co LEFT JOIN contacts ct ON ct.company_id=co.id ORDER BY co.id, ct.id"
    ).fetchall()
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["会社ID", "会社名", "サイト", "エリア", "業態", "店舗数", "層", "会社の状態", "補足",
                    "アドレス", "注意", "連絡先の状態", "掲載ページ", "送信日時", "反応"])
        for r in rows:
            w.writerow([r["id"], r["name"], r["site_url"], r["area"], r["category"], r["store_count"],
                        r["segment"], r["status"], r["status_note"], r["email"], _flags_ja(r["flags"] or ""),
                        r["contact_status"], r["source_url"], r["sent_at"], r["reply"]])
    print(f"{len(rows)}行を {out} に書き出しました。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autosales", description="公開アドレスの収集と営業メールの送信")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help=".env とデータベースを用意する").set_defaults(func=cmd_init)

    s = sub.add_parser("import", help="候補リスト（CSV）を取り込む")
    s.add_argument("csv")
    s.add_argument("--source", help="リストの出どころのメモ")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("crawl", help="公式サイトを見て、公表アドレスを集める")
    s.add_argument("--limit", type=int)
    s.add_argument("--recrawl", action="store_true", help="アドレスなし・エラーだった会社も見直す")
    s.set_defaults(func=cmd_crawl)

    s = sub.add_parser("review", help="確認待ちの連絡先をCSVに出す")
    s.add_argument("--out")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("approve", help="確認した連絡先を承認・見送りにする")
    s.add_argument("--csv", help="review で出して「判定」を入れたCSV")
    s.add_argument("--ids", type=int, nargs="*", help="承認する連絡先ID")
    s.add_argument("--reject", type=int, nargs="*", help="見送る連絡先ID")
    s.add_argument("--unflagged", action="store_true", help="注意フラグの無い確認待ちをまとめて承認")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("preview", help="文面を画面に出す（送らない）")
    s.add_argument("--template", default="honbu")
    s.add_argument("--name", default="株式会社サンプル")
    s.set_defaults(func=cmd_preview)

    s = sub.add_parser("send", help="承認済みの相手に送る（--live が無ければ下書きを書くだけ）")
    s.add_argument("--template", default="honbu")
    s.add_argument("--segment", help="この層だけに送る（例: honbu / show）")
    s.add_argument("--limit", type=int)
    s.add_argument("--live", action="store_true", help="実際に送信する")
    s.add_argument("--yes", action="store_true", help="確認の入力を省く")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("replies", help="返信を取り込む（不要→停止、番号→集計、不達→除外）")
    s.add_argument("--days", type=int, default=14)
    s.set_defaults(func=cmd_replies)

    s = sub.add_parser("suppress", help="アドレスかドメインを停止リストに入れる")
    s.add_argument("targets", nargs="+")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_suppress)

    s = sub.add_parser("unrefuse", help="「営業お断り」の誤検知を、人が確認して解除する")
    s.add_argument("company_id", type=int)
    s.set_defaults(func=cmd_unrefuse)

    sub.add_parser("stats", help="件数と反応を見る").set_defaults(func=cmd_stats)

    s = sub.add_parser("export", help="リスト全体をCSVに出す")
    s.add_argument("--out")
    s.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    conn = db.connect(cfg.path("DB_PATH"))
    try:
        return args.func(cfg, conn, args)
    except TemplateError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

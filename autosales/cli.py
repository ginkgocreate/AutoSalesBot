"""コマンドの入口。 python3 -m autosales <コマンド>"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

from . import db
from .config import Config, load_config
from .crawler import Crawler, crawl_pending
from .discover import DEFAULT_OSM_KINDS, OSM_KINDS, DiscoverError, discover_osm, discover_search
from .importer import import_csv, open_csv
from .assist import run_assist
from .forms import FLAG_LABELS as FORM_FLAG_LABELS, describe_items
from .formsend import send_forms
from .mailer import TemplateError, load_template, plan_count, render, send_batch
from .replies import fetch_replies

FORM_REVIEW_HEADERS = [
    "ID",
    "判定",
    "会社名",
    "送り方",
    "フォームのページ",
    "入力の内容",
    "注意",
    "人が送る理由",
    "層",
    "照合",
]
FORM_MODES = {"auto": "自動", "manual": "人が送る", "skip": "送らない"}
REVIEW_HEADERS = ["ID", "判定", "会社名", "アドレス", "注意", "掲載ページ", "層", "エリア", "業態"]
FLAG_LABELS = {
    "recruit": "求人用の可能性",
    "privacy": "個人情報窓口の可能性",
    "free_mail": "フリーメール",
    "offsite_domain": "サイトと別ドメイン（制作会社などの可能性）",
    "label_glued": "見出しや電話番号とつながっていた（アドレスを要確認）",
    "tag_joined": "アドレスの途中にタグがあった（アドレスを要確認）",
    "contact_unread": "お問い合わせページを読めていない（お断りの表示を要確認）",
    "page_truncated": "ページが長すぎて最後まで読めていない",
}
OK_WORDS = {"ok", "o", "○", "◯", "〇", "可", "yes", "y"}
NG_WORDS = {"ng", "x", "×", "✕", "✗", "不可", "no", "n"}


def _positive_int(value: str) -> int:
    try:
        n = int(unicodedata.normalize("NFKC", value))
    except ValueError:
        raise argparse.ArgumentTypeError("整数で指定してください")
    if n < 1:
        raise argparse.ArgumentTypeError("1以上で指定してください")
    return n


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


def cmd_doctor(cfg: Config, conn, args) -> int:
    from .readiness import inspect_readiness

    options = {"template_names": tuple(args.template)} if args.template else {}
    report = inspect_readiness(cfg, **options)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("運用前チェック（設定の確認のみ。実配信・到達・返信受信は未確認）")
        stages = {"research": "リサーチ", "draft": "下書き", "email": "メール送信設定", "replies": "返信受信設定"}
        for stage, label in stages.items():
            print(f"{label}: {'準備済み' if report[stage + '_ready'] else '未準備'}")
            for reason in report["blockers"].get(stage, []):
                print(f"  - {reason}")
    return int(bool(args.require and not report[args.require + "_ready"]))


def cmd_import(cfg: Config, conn, args) -> int:
    path = Path(args.csv)
    if not path.is_file():
        print(f"ファイルがありません: {path}", file=sys.stderr)
        return 1
    c = import_csv(conn, path, source=args.source or "")
    print(
        f"追加 {c['added']}件 / 重複 {c['duplicate']}件 / 不備 {c['invalid']}件 / "
        f"ポータル・SNSのURLのため除外 {c['non_official']}件"
    )
    return 0


def cmd_discover(cfg: Config, conn, args) -> int:
    if not args.query.strip():
        raise DiscoverError("地域名か、検索の言葉を指定してください")
    before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    if args.source == "osm":
        kinds = [k.strip() for k in (args.kinds or ",".join(DEFAULT_OSM_KINDS)).split(",") if k.strip()]
        c = discover_osm(
            conn, args.query, kinds=kinds, segment=args.segment or "", user_agent=cfg.get("CRAWL_USER_AGENT")
        )
    else:
        print("Web検索で候補を探しています（数分かかります）…")
        if args.fallback_osm_area or args.fallback_osm_kinds or args.fallback_segment:
            from .discover import discover_search_with_fallback

            c = discover_search_with_fallback(
                conn,
                args.query,
                segment=args.segment or "",
                limit=args.limit or 20,
                fallback_osm_area=args.fallback_osm_area,
                fallback_osm_kinds=(
                    [kind.strip() for kind in args.fallback_osm_kinds.split(",") if kind.strip()]
                    if args.fallback_osm_kinds
                    else None
                ),
                fallback_segment=args.fallback_segment or "",
                user_agent=cfg.get("CRAWL_USER_AGENT"),
            )
        else:
            c = discover_search(conn, args.query, segment=args.segment or "", limit=args.limit or 20)
    after = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(
        f"追加 {c['added']}社 / すでにある {c['duplicate']}社 / 不備 {c['invalid']}件 / "
        f"ポータル・SNSのURLのため除外 {c['non_official']}件（リスト全体 {before}社 → {after}社）"
    )
    print("次は crawl で、公式サイトから公表アドレスを集めてください。")
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
    labels = {
        "crawled": "アドレスあり",
        "no_email": "アドレスなし",
        "refused": "営業お断り",
        "blocked": "巡回不可",
        "error": "エラー",
        "skipped": "対象外",
    }
    print("---")
    print(" / ".join(f"{labels[k]} {counts[k]}社" for k in labels if counts.get(k)))
    print(f"新しい連絡先 {counts.get('_contacts', 0)}件。次は review で確認用のCSVを出してください。")
    forms = [
        (label, counts.get(f"_form_{mode}", 0))
        for mode, label in (("auto", "自動で送れる"), ("manual", "人が送る"), ("skip", "送らない"))
    ]
    if any(n for _, n in forms):
        print(
            "お問い合わせフォーム: "
            + " / ".join(f"{label} {n}社" for label, n in forms if n)
            + "。フォームで連絡するなら、次は form review。"
        )
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
            w.writerow(
                [
                    r["id"],
                    "",
                    r["name"],
                    r["email"],
                    _flags_ja(r["flags"]),
                    r["source_url"],
                    r["segment"],
                    r["area"],
                    r["category"],
                ]
            )
    print(f"確認待ち {len(rows)}件を {out} に書き出しました。")
    print("掲載ページを開いて確かめ、「判定」列に ok か ng を入れて、approve --csv で取り込んでください。")
    return 0


def _require_columns(reader: csv.DictReader, columns: tuple[str, ...], command: str) -> None:
    """別の種類のCSV（メール用とフォーム用の取り違え）を、1行も反映せずに止める。"""
    have = {(name or "").strip() for name in reader.fieldnames or []}
    missing = [c for c in columns if c not in have]
    if missing:
        raise ValueError(
            f"このCSVには「{'」「'.join(missing)}」の列がありません。{command} で書き出したCSVを使ってください"
        )


def _decide(conn, contact_id: int, ok: bool, allow_bounced: bool = False) -> bool:
    states = "('pending','approved','rejected','bounced')" if allow_bounced else "('pending','approved','rejected')"
    if ok:
        row = conn.execute(
            "SELECT co.status FROM contacts ct JOIN companies co ON co.id=ct.company_id WHERE ct.id=?", (contact_id,)
        ).fetchone()
        if row is None or row["status"] != "crawled":
            return False  # 営業お断りなどで外した会社の連絡先は承認しない
    cur = conn.execute(
        f"UPDATE contacts SET status=?, decided_at=? WHERE id=? AND status IN {states}",
        ("approved" if ok else "rejected", db.now(), contact_id),
    )
    return cur.rowcount > 0


def cmd_approve(cfg: Config, conn, args) -> int:
    approved = rejected = 0
    unreadable: list[int] = []
    mismatched: list[int] = []
    if args.csv:
        with open_csv(Path(args.csv)) as f:
            reader = csv.DictReader(f)
            _require_columns(reader, ("ID", "判定", "アドレス"), "review")
            for line_no, row in enumerate(reader, start=2):
                norm = {
                    (k or "").strip(): unicodedata.normalize("NFKC", v if isinstance(v, str) else "").strip()
                    for k, v in row.items()
                }
                verdict = norm.get("判定", "").lower()
                cid = norm.get("ID", "")
                if not verdict:
                    continue
                if not (cid.isascii() and cid.isdigit()) or verdict not in OK_WORDS | NG_WORDS:
                    unreadable.append(line_no)
                    continue
                known = conn.execute("SELECT email FROM contacts WHERE id=?", (int(cid),)).fetchone()
                if known is None or norm.get("アドレス", "").lower() != known["email"]:
                    mismatched.append(line_no)  # 行がずれたCSVで、別の相手を承認しないため
                    continue
                ok = verdict in OK_WORDS
                if _decide(conn, int(cid), ok):
                    approved += ok
                    rejected += not ok
    for cid in args.ids or []:
        approved += _decide(conn, cid, True, allow_bounced=True)
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
    if unreadable:
        print(
            f"読めなかった判定 {len(unreadable)}件（CSVの {', '.join(map(str, unreadable[:20]))} 行目）。"
            "ok か ng で入れてください。"
        )
    if mismatched:
        print(
            f"IDとアドレスが合わない行 {len(mismatched)}件（{', '.join(map(str, mismatched[:20]))} 行目）は"
            "反映していません。review で出し直してください。"
        )
    return 1 if unreadable or mismatched else 0


def cmd_preview(cfg: Config, conn, args) -> int:
    template = load_template(cfg.root / "templates" / f"{args.template}.txt")
    sample = {
        "name": args.name,
        "area": "奈良市",
        "category": "ラウンジ",
        "source_url": "https://example.co.jp/contact",
    }
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


def _segments_mixed(targets, segment: str | None) -> bool:
    """層ごとに文面が違う。1社1回で送り直せないので、混ざったままでは送らない。"""
    if segment is not None:
        return False
    by_segment: dict[str, int] = {}
    for row in targets:
        by_segment[row["segment"]] = by_segment.get(row["segment"], 0) + 1
    if len(by_segment) <= 1:
        return False
    listing = " / ".join(f"{seg or '層なし'} {n}社" for seg, n in sorted(by_segment.items()))
    print(
        f"送れる相手に複数の層が混ざっています（{listing}）。"
        '--segment で層を指定してください（層なしだけに送るなら --segment ""）。'
    )
    return True


def _confirm_live(n: int, what: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        answer = input(f"本当に {n}{what} を送信しますか？ (yes と入力): ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "yes":
        print("中止しました。")
        return False
    return True


def _form_review_token(row) -> str:
    """Bind a review decision to the form and the warnings the reviewer saw."""
    snapshot = [row["signature"], row["page_url"], row["mode"], sorted(set(filter(None, row["flags"].split(","))))]
    return "v2:" + hashlib.sha256(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


def cmd_form_review(cfg: Config, conn, args) -> int:
    rows = conn.execute(
        "SELECT f.id, co.name, f.mode, f.page_url, f.plan, f.flags, f.reason, co.segment, f.signature "
        "FROM forms f JOIN companies co ON co.id = f.company_id "
        "WHERE f.status='pending' AND f.mode IN ('auto','manual') AND co.status IN ('crawled','no_email') "
        "ORDER BY (f.mode != 'auto'), co.id"
    ).fetchall()
    out = Path(args.out) if args.out else cfg.root / "data" / "form_review.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(FORM_REVIEW_HEADERS)
        for r in rows:
            try:
                items = json.loads(r["plan"]).get("items", []) if r["plan"] else []
            except ValueError:
                items = []
            flags = "・".join(FORM_FLAG_LABELS.get(x, x) for x in r["flags"].split(",") if x)
            w.writerow(
                [
                    r["id"],
                    "",
                    r["name"],
                    FORM_MODES[r["mode"]],
                    r["page_url"],
                    describe_items(items),
                    flags,
                    r["reason"] if r["mode"] == "manual" else "",
                    r["segment"],
                    _form_review_token(r),
                ]
            )
    auto = sum(1 for r in rows if r["mode"] == "auto")
    print(
        f"確認待ちのフォーム {len(rows)}件（自動 {auto}件 / 人が送る {len(rows) - auto}件）を {out} に書き出しました。"
    )
    print(
        "自動で送るものは、フォームのページを開いて、問い合わせ用のフォームか、入力の内容が合っているかを確かめ、"
        "「判定」列に ok か ng を入れて form approve --csv で取り込んでください。"
    )
    print("人が送るものは承認がいりません。form assist の画面で、1件ずつ確かめて送ります。")
    return 0


def _decide_form(conn, form_id: int, ok: bool) -> bool:
    if ok:
        row = conn.execute(
            "SELECT co.status, f.mode FROM forms f JOIN companies co ON co.id=f.company_id WHERE f.id=?", (form_id,)
        ).fetchone()
        if row is None or row["status"] not in ("crawled", "no_email") or row["mode"] == "skip":
            return False  # 営業お断りなどで外した会社、送らないと決めたフォームは承認しない
    cur = conn.execute(
        "UPDATE forms SET status=?, decided_at=? WHERE id=?", ("approved" if ok else "rejected", db.now(), form_id)
    )
    return cur.rowcount > 0


def cmd_form_approve(cfg: Config, conn, args) -> int:
    approved = rejected = 0
    unreadable: list[int] = []
    mismatched: list[int] = []
    if args.csv:
        with open_csv(Path(args.csv)) as f:
            reader = csv.DictReader(f)
            _require_columns(reader, ("ID", "判定", "フォームのページ", "照合"), "form review")
            for line_no, row in enumerate(reader, start=2):
                norm = {
                    (k or "").strip(): unicodedata.normalize("NFKC", v if isinstance(v, str) else "").strip()
                    for k, v in row.items()
                }
                verdict = norm.get("判定", "").lower()
                fid = norm.get("ID", "")
                if not verdict:
                    continue
                if not (fid.isascii() and fid.isdigit()) or verdict not in OK_WORDS | NG_WORDS:
                    unreadable.append(line_no)
                    continue
                known = conn.execute(
                    "SELECT page_url, signature, mode, flags FROM forms WHERE id=?", (int(fid),)
                ).fetchone()
                # 行がずれたCSVや、書き出したあとに作りが変わったフォームを、見ていないまま承認しないため
                if (
                    known is None
                    or norm.get("フォームのページ") != known["page_url"]
                    or norm.get("照合") != _form_review_token(known)
                ):
                    mismatched.append(line_no)
                    continue
                ok = verdict in OK_WORDS
                if _decide_form(conn, int(fid), ok):
                    approved += ok
                    rejected += not ok
    for fid in args.ids or []:
        approved += _decide_form(conn, fid, True)
    for fid in args.reject or []:
        rejected += _decide_form(conn, fid, False)
    if args.unflagged:
        cur = conn.execute(
            "UPDATE forms SET status='approved', decided_at=? WHERE status='pending' AND mode='auto' AND flags='' "
            "AND company_id IN (SELECT id FROM companies WHERE status IN ('crawled','no_email'))",
            (db.now(),),
        )
        approved += cur.rowcount
    conn.commit()
    print(f"承認 {approved}件 / 見送り {rejected}件")
    if unreadable:
        print(
            f"読めなかった判定 {len(unreadable)}件（CSVの {', '.join(map(str, unreadable[:20]))} 行目）。"
            "ok か ng で入れてください。"
        )
    if mismatched:
        print(
            f"いまのフォームと合わない行 {len(mismatched)}件（{', '.join(map(str, mismatched[:20]))} 行目）は"
            "反映していません（行のずれか、書き出したあとにフォームが変わっています）。form review で出し直してください。"
        )
    return 1 if unreadable or mismatched else 0


def cmd_form_send(cfg: Config, conn, args) -> int:
    template = load_template(cfg.root / "templates" / f"{args.template}.txt")
    targets = db.sendable_forms(conn, segment=args.segment, mode="auto")
    if not targets:
        print("自動で送れるフォームがありません（承認済みで、まだ連絡しておらず、停止リストに無い会社が0社）。")
        return 0
    if _segments_mixed(targets, args.segment):
        return 1
    n = 0
    if args.live:
        room = max(0, cfg.get_int("DAILY_LIMIT") - db.sent_today(conn))
        n = plan_count(conn, cfg, len(targets), args.limit, True)
        print(f"送れる相手 {len(targets)}社。今日の残り枠 {room}件（メールと共通）。今回送るのは {n}件です。")
        if n <= 0:
            return 0
        if not _confirm_live(n, "件", args.yes):
            return 1
    s = send_forms(conn, cfg, template, live=args.live, segment=args.segment, limit=n if args.live else args.limit)
    if args.live:
        line = f"送信 {s.sent}件 / 失敗 {s.failed}件 / 上限のため次回へ {s.skipped_limit}社"
        if s.skipped:
            line += f" / 今回見送り {s.skipped}社"
        if s.unknown:
            line += f" / 届いたか不明 {s.unknown}件"
        print(line)
        if s.failed:
            print("失敗したフォームは、人が送る方に回しました（form assist）。")
        if s.stopped:
            print(f"途中で止めました: {s.stopped}")
            return 1
    else:
        print(
            f"下書き {s.dry_run}件を outbox/ に書き出しました（まだ送っていません）。"
            "中身を確かめてから --live を付けて実行してください。"
        )
    return 0


def cmd_form_assist(cfg: Config, conn, args) -> int:
    template = load_template(cfg.root / "templates" / f"{args.template}.txt")
    targets = db.sendable_forms(conn, segment=args.segment, mode="manual")
    if _segments_mixed(targets, args.segment):
        return 1
    sent = run_assist(conn, cfg, template, segment=args.segment, port=args.port or 0, open_browser=not args.no_open)
    print(f"人が送ったフォーム {sent}件を記録しました。")
    return 0


def cmd_send(cfg: Config, conn, args) -> int:
    template = load_template(cfg.root / "templates" / f"{args.template}.txt")
    targets = db.sendable_contacts(conn, segment=args.segment)
    n = 0
    if not targets:
        print("送れる相手がいません（承認済みで、まだ送っておらず、停止リストに無い連絡先が0件）。")
        return 0
    if _segments_mixed(targets, args.segment):
        return 1
    if args.live:
        missing = cfg.missing_sender_fields() + ([] if cfg.get("SMTP_HOST") else ["SMTP_HOST"])
        if missing:
            raise TemplateError("未設定の項目があります（.env）: " + ", ".join(missing))
        room = max(0, cfg.get_int("DAILY_LIMIT") - db.sent_today(conn))
        n = plan_count(conn, cfg, len(targets), args.limit, True)
        print(f"送れる相手 {len(targets)}社。今日の残り枠 {room}通。今回送るのは {n}通です。")
        if n <= 0:
            return 0
        if not _confirm_live(n, "通", args.yes):
            return 1
    # 本番では、確認で表示した通数を超えて送らない（入力待ちの間に承認が増えても同じ）
    s = send_batch(conn, cfg, template, live=args.live, segment=args.segment, limit=n if args.live else args.limit)
    if args.live:
        line = f"送信 {s.sent}通 / 失敗 {s.failed}通 / 上限のため次回へ {s.skipped_limit}社"
        if s.unknown:
            line += f" / 届いたか不明 {s.unknown}通"
        print(line)
        if s.stopped:
            print(f"途中で止めました: {s.stopped}")
            return 1
    else:
        print(
            f"下書き {s.dry_run}通を outbox/ に書き出しました（まだ送っていません）。"
            "中身を確かめてから --live を付けて実行してください。"
        )
    return 0


def cmd_replies(cfg: Config, conn, args) -> int:
    c = fetch_replies(conn, cfg, days=args.days)
    print(
        f"確認 {c['checked']}通 → 返信 {c['reply']}件 / 停止の連絡 {c['optout']}件 / 不達 {c['bounce']}件"
        + (f" / フォームの受付の控え {c['auto']}件" if c.get("auto") else "")
    )
    if c["unreadable"]:
        print(f"取得できなかったメール {c['unreadable']}通。もう一度 replies を実行してください。")
        return 1
    return 0


def cmd_suppress(cfg: Config, conn, args) -> int:
    failed = 0
    # 「a@x.jp, b@y.jp」のようにまとめて貼られたものは、1件ずつに分ける
    targets = [t for raw in args.targets for t in re.split(r"[,;、\s]+", raw) if t]
    for target in targets:
        try:
            new = db.add_suppression(conn, target, args.reason or "手動")
        except ValueError:
            print(f"{target}: アドレスかドメインとして読めません（停止リストに入れていません）")
            failed += 1
            continue
        shown = db.normalize_target(target)
        print(f"{shown}: {'停止リストに入れました' if new else 'すでに停止リストにあります'}")
    return 1 if failed else 0


def cmd_unrefuse(cfg: Config, conn, args) -> int:
    row = conn.execute(
        "SELECT name, status, refusal_confirmed, crawled_at FROM companies WHERE id=?", (args.company_id,)
    ).fetchone()
    if not row or row["status"] != "refused":
        print("「営業お断り」で除外中の会社ではありません。")
        return 1
    found = conn.execute(
        "SELECT url, detail FROM crawl_log WHERE company_id=? AND result='refusal' AND fetched_at=? ORDER BY id",
        (args.company_id, row["crawled_at"]),
    ).fetchall()
    print(f"{row['name']} で見つけた表示（{len(found)}件）:")
    for i, r in enumerate(found, 1):
        print(f"  {i}. {r['detail']}\n     {r['url']}")
    if not args.yes:
        try:
            answer = (
                input("上の表示はすべて「営業メールお断り」ではない、でよいですか？ (yes と入力): ").strip().lower()
            )
        except EOFError:
            answer = ""
        if answer != "yes":
            print("そのままにしました。")
            return 1
    confirmed = {line for line in row["refusal_confirmed"].split("\n") if line} | {r["detail"] for r in found}
    conn.execute(
        "UPDATE companies SET status='new', refusal_confirmed=?, status_note='人が確認して除外を解除' WHERE id=?",
        ("\n".join(sorted(confirmed)), args.company_id),
    )
    conn.commit()
    print(
        "除外を解除しました。crawl を実行すると、この会社のアドレスが確認待ちに入ります。"
        "次の巡回で別の表示が見つかった場合は、また除外されます。"
    )
    return 0


def cmd_stats(cfg: Config, conn, args) -> int:
    def one(sql: str, *p) -> int:
        return int(conn.execute(sql, p).fetchone()[0])

    print("■ 会社")
    labels = [
        ("new", "未巡回"),
        ("crawled", "アドレスあり"),
        ("no_email", "アドレスなし"),
        ("refused", "営業お断り"),
        ("blocked", "巡回不可"),
        ("error", "エラー"),
        ("skipped", "対象外"),
    ]
    total = one("SELECT COUNT(*) FROM companies")
    print(f"  合計 {total}社")
    for key, label in labels:
        n = one("SELECT COUNT(*) FROM companies WHERE status=?", key)
        if n:
            print(f"  {label} {n}社")
    print("■ 連絡先")
    for key, label in [
        ("pending", "確認待ち"),
        ("approved", "承認済み"),
        ("rejected", "見送り"),
        ("bounced", "宛先不明"),
    ]:
        print(f"  {label} {one('SELECT COUNT(*) FROM contacts WHERE status=?', key)}件")
    print("■ フォーム（送ってよい会社のもの）")
    live = "company_id IN (SELECT id FROM companies WHERE status IN ('crawled','no_email'))"
    print(
        f"  自動: 確認待ち {one(f'SELECT COUNT(*) FROM forms WHERE mode=? AND status=? AND {live}', 'auto', 'pending')}件"
        f" / 承認済み {one(f'SELECT COUNT(*) FROM forms WHERE mode=? AND status=? AND {live}', 'auto', 'approved')}件"
    )
    print(
        f"  人が送る {one(f'SELECT COUNT(*) FROM forms WHERE mode=? AND status!=? AND {live}', 'manual', 'rejected')}件"
        f" / 送らない {one(f'SELECT COUNT(*) FROM forms WHERE (mode=? OR status=?) AND {live}', 'skip', 'rejected')}件"
    )
    print("■ 送信と反応（層ごと、会社の数）")
    segments = [r[0] for r in conn.execute("SELECT DISTINCT segment FROM companies ORDER BY segment")]
    latest = (
        "(SELECT MAX(r2.id) FROM replies r2 WHERE r2.kind='reply' AND r2.choice IS NOT NULL GROUP BY r2.company_id)"
    )
    base = "FROM replies rp JOIN companies co ON co.id=rp.company_id WHERE co.segment=? "
    by_mail = (
        "EXISTS (SELECT 1 FROM sends s JOIN contacts ct ON ct.id=s.contact_id "
        "WHERE ct.company_id=co.id AND s.status IN ('sent','unknown'))"
    )
    by_form = (
        "EXISTS (SELECT 1 FROM form_sends fs WHERE fs.company_id=co.id AND fs.status IN ('sent','unknown','manual'))"
    )
    for seg in segments:
        sent = one(f"SELECT COUNT(*) FROM companies co WHERE co.segment=? AND ({by_mail} OR {by_form})", seg)
        if not sent:
            continue
        via_form = one(f"SELECT COUNT(*) FROM companies co WHERE co.segment=? AND {by_form}", seg)
        reply = one("SELECT COUNT(DISTINCT co.id) " + base + "AND rp.kind='reply'", seg)
        optout = one("SELECT COUNT(DISTINCT co.id) " + base + "AND rp.kind='optout'", seg)
        bounce = one("SELECT COUNT(DISTINCT co.id) " + base + "AND rp.kind='bounce'", seg)
        choices = [
            one("SELECT COUNT(*) " + base + f"AND rp.choice=? AND rp.id IN {latest}", seg, i) for i in (1, 2, 3, 4)
        ]
        print(
            f"  [{seg or '層なし'}] 送信 {sent}社（うちフォーム {via_form}社）/ 返信 {reply}社 / 停止 {optout}社 / 不達 {bounce}社"
            f" / 番号 1:{choices[0]} 2:{choices[1]} 3:{choices[2]} 4:{choices[3]}"
        )
    print(
        f"■ 停止リスト {one('SELECT COUNT(*) FROM suppressions')}件 / 今日の送信 {db.sent_today(conn)}件"
        f"（メールとフォームの合計。上限 {cfg.get('DAILY_LIMIT')}件）"
    )
    return 0


def _form_result_ja(result: str | None) -> str:
    if not result:
        return ""
    status, _, detail = result.partition("（")
    label = {"sent": "送信済み", "unknown": "届いたか不明", "manual": "人が送信", "failed": "送れなかった"}.get(
        status, status
    )
    return label + ("（" + detail if detail else "")


def cmd_export(cfg: Config, conn, args) -> int:
    out = Path(args.out) if args.out else cfg.root / "data" / "list.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT co.id, co.name, co.site_url, co.area, co.category, co.store_count, co.segment, "
        " co.status, co.status_note, ct.email, ct.flags, ct.status AS contact_status, ct.source_url, "
        " (SELECT MAX(sent_at) FROM sends s WHERE s.contact_id=ct.id AND s.status='sent') AS sent_at, "
        " (SELECT kind || COALESCE(':' || choice, '') FROM replies rp WHERE rp.contact_id=ct.id "
        "   ORDER BY rp.id DESC LIMIT 1) AS reply, "
        " f.mode AS form_mode, f.status AS form_status, f.page_url AS form_url, "
        " (SELECT MAX(sent_at) FROM form_sends fs WHERE fs.company_id=co.id "
        "   AND fs.status IN ('sent','unknown','manual')) AS form_sent_at, "
        " (SELECT fs.status || CASE WHEN fs.detail != '' THEN '（' || fs.detail || '）' ELSE '' END "
        "   FROM form_sends fs WHERE fs.company_id=co.id AND fs.status != 'dry_run' "
        "   ORDER BY fs.id DESC LIMIT 1) AS form_result, "
        " (SELECT kind || COALESCE(':' || choice, '') FROM replies rp WHERE rp.company_id=co.id "
        "   AND rp.contact_id IS NULL ORDER BY rp.id DESC LIMIT 1) AS form_reply "
        "FROM companies co LEFT JOIN contacts ct ON ct.company_id=co.id "
        "LEFT JOIN forms f ON f.company_id=co.id ORDER BY co.id, ct.id"
    ).fetchall()
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "会社ID",
                "会社名",
                "サイト",
                "エリア",
                "業態",
                "店舗数",
                "層",
                "会社の状態",
                "補足",
                "アドレス",
                "注意",
                "連絡先の状態",
                "掲載ページ",
                "送信日時",
                "反応",
                "フォームの送り方",
                "フォームの状態",
                "フォームのページ",
                "フォーム送信日時",
                "フォーム送信の結果",
                "フォームへの反応",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r["id"],
                    r["name"],
                    r["site_url"],
                    r["area"],
                    r["category"],
                    r["store_count"],
                    r["segment"],
                    r["status"],
                    r["status_note"],
                    r["email"],
                    _flags_ja(r["flags"] or ""),
                    r["contact_status"],
                    r["source_url"],
                    r["sent_at"],
                    r["reply"],
                    FORM_MODES.get(r["form_mode"] or "", ""),
                    r["form_status"],
                    r["form_url"],
                    r["form_sent_at"],
                    _form_result_ja(r["form_result"]),
                    r["form_reply"],
                ]
            )
    print(f"{len(rows)}行を {out} に書き出しました。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autosales", description="公開アドレス・フォームの収集と、営業の連絡の送信")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help=".env とデータベースを用意する").set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="送信者・ひな形・送受信設定を、接続やDB更新なしで確認する")
    s.add_argument("--json", action="store_true", help="設定値を含まないJSONで出力する")
    s.add_argument(
        "--require", choices=["research", "draft", "email", "replies"], help="指定段階が未準備なら終了コード1"
    )
    s.add_argument("--template", action="append", help="確認するひな形名（複数指定可、既定は標準4種）")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("import", help="候補リスト（CSV）を取り込む")
    s.add_argument("csv")
    s.add_argument("--source", help="リストの出どころのメモ")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("discover", help="候補（公式サイトのURL）を自動で集める")
    s.add_argument(
        "source", choices=["osm", "search"], help="osm = 地図のオープンデータから / search = Web検索で運営会社を探す"
    )
    s.add_argument("query", help="osm: 地域名（例: 奈良県、大阪市） / search: 検索の言葉")
    s.add_argument("--kinds", help="osm の種類をカンマ区切りで（" + ", ".join(OSM_KINDS) + "）")
    s.add_argument("--segment", help="集めた候補に付ける層（例: honbu / show）")
    s.add_argument("--limit", type=_positive_int, help="search で集める最大件数（既定 20）")
    s.add_argument("--fallback-osm-area", help="searchが利用不可の場合に調べる地図の地域（種類指定と併用）")
    s.add_argument("--fallback-osm-kinds", help="代替地図検索の種類をカンマ区切りで明示する")
    s.add_argument("--fallback-segment", help="代替の店舗候補に付ける層。検索側の層は引き継がない")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("crawl", help="公式サイトを見て、公表アドレスを集める")
    s.add_argument("--limit", type=_positive_int)
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
    s.add_argument("--limit", type=_positive_int)
    s.add_argument("--live", action="store_true", help="実際に送信する")
    s.add_argument("--yes", action="store_true", help="確認の入力を省く")
    s.set_defaults(func=cmd_send)

    f = sub.add_parser("form", help="お問い合わせフォームで連絡する（アドレスが無い会社向け）")
    fsub = f.add_subparsers(dest="form_command", required=True)
    s = fsub.add_parser("review", help="確認待ちのフォームをCSVに出す")
    s.add_argument("--out")
    s.set_defaults(func=cmd_form_review)
    s = fsub.add_parser("approve", help="確認したフォームを承認・見送りにする")
    s.add_argument("--csv", help="form review で出して「判定」を入れたCSV")
    s.add_argument("--ids", type=int, nargs="*", help="承認するフォームID")
    s.add_argument("--reject", type=int, nargs="*", help="見送るフォームID")
    s.add_argument("--unflagged", action="store_true", help="注意の印が無い、自動で送れるフォームをまとめて承認")
    s.set_defaults(func=cmd_form_approve)
    s = fsub.add_parser("send", help="承認済みのフォームに自動で送る（--live が無ければ下書きを書くだけ）")
    s.add_argument("--template", default="honbu_form")
    s.add_argument("--segment", help="この層だけに送る（例: honbu / show）")
    s.add_argument("--limit", type=_positive_int)
    s.add_argument("--live", action="store_true", help="実際に送信する")
    s.add_argument("--yes", action="store_true", help="確認の入力を省く")
    s.set_defaults(func=cmd_form_send)
    s = fsub.add_parser("assist", help="自動で送らないフォームを、人がブラウザで送るための補助画面を開く")
    s.add_argument("--template", default="honbu_form")
    s.add_argument("--segment", help="この層だけを出す（例: honbu / show）")
    s.add_argument("--port", type=_positive_int, help="画面を出すポート（既定は空いている番号）")
    s.add_argument("--no-open", action="store_true", help="ブラウザを自動で開かない")
    s.set_defaults(func=cmd_form_assist)

    s = sub.add_parser("replies", help="返信を取り込む（不要→停止、番号→集計、不達→除外）")
    s.add_argument("--days", type=_positive_int, default=14)
    s.set_defaults(func=cmd_replies)

    s = sub.add_parser("suppress", help="アドレスかドメインを停止リストに入れる")
    s.add_argument("targets", nargs="+")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_suppress)

    s = sub.add_parser("unrefuse", help="「営業お断り」の誤検知を、人が確認して解除する")
    s.add_argument("company_id", type=int)
    s.add_argument("--yes", action="store_true", help="確認の入力を省く")
    s.set_defaults(func=cmd_unrefuse)

    sub.add_parser("stats", help="件数と反応を見る").set_defaults(func=cmd_stats)

    s = sub.add_parser("export", help="リスト全体をCSVに出す")
    s.add_argument("--out")
    s.set_defaults(func=cmd_export)
    return p


def _run(cfg: Config, conn, args) -> int:
    try:
        return args.func(cfg, conn, args)
    except (TemplateError, DiscoverError, ValueError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


def main_for_test(cfg: Config, conn, argv: list[str]) -> int:
    """テスト用の入口。設定とDBを外から渡す。"""
    return _run(cfg, conn, build_parser().parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if args.command == "doctor":
        return _run(cfg, None, args)
    conn = db.connect(cfg.path("DB_PATH"))
    try:
        return _run(cfg, conn, args)
    finally:
        conn.close()

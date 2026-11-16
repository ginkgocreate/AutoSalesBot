# ANALINK 営業支援システム 引き継ぎ

## 現在の作業

- 2026-11-16: Claude Code の「Analink自動営業DM配信システム」から引き継ぎ。
- 実装先はこのリポジトリ `ginkgocreate/AutoSalesBot`。セッションの開始場所 `Tanpl-LP` は実装先ではない。
- 作業ブランチ: `feature/analink-forms-handoff`。引き継ぎ時の基点: `fb4b35d`。
- 元セッションはフォーム機能の再レビュー中に OAuth 認証期限切れで停止。未コミットの変更を引き継いだ。

## 合意済みの範囲

- SNS の基盤は不要。候補URL収集、公式サイトからの連絡先収集、メール送信を先に作る。
- フォームは自動と手送りを併用する。通常フォームは承認後に自動送信、CAPTCHA や特殊なフォームは下書きと手動確認。
- 本送信の既定は無効。1社1回、メール・フォーム共通の日次上限、停止・営業拒否の除外を維持する。
- この引き継ぎでは外部宛先への送信・本番デプロイ・実データ変更は行わない。テストは一時DBとlocalhostの仮サーバーを使う。

## 完了タスク

- [x] 元セッションの依頼・停止理由・未完了レビューを回収。
- [x] 引き継ぎ状態の全121テストを現物で再実行し成功。
- [x] featureブランチを作成し、未コミットのフォーム機能を維持。
- [x] 先頭の送信前見送りで後続候補が処理されない問題を再現テストで修正。
- [x] CF7のquery形式・末尾スラッシュなしのREST URLを修正。
- [x] 送信ボタンにある営業拒否・非営業目的の宣言を検出。
- [x] フォーム結果判定と確認画面の誤操作防止を修正・検証。
- [x] 旧DBからの明示的移行・データ保持・安全なラウンドトリップを検証。
- [x] 人の停止意思・番号回答と自動受付を区別し、引用による誤認を修正。
- [x] 警告変更で承認を解除し、フォームの状態が変わった古い承認CSVを拒否。
- [x] 同じ公開メールを共有する会社について、メール・自動フォーム・手動フォーム間の重複を防止。
- [x] Python 3.9 / 3.14 の全テスト、lint、formatを通過。
- [x] commit・push・PR作成完了: https://github.com/ginkgocreate/AutoSalesBot/pull/1
- [x] 実装コミット `820b253` のCIを確認。push / PR のPython 3.9・3.14・qualityがすべて成功。
- [x] 架空4社で実CLIを通る再実行可能なドライテストと、localhost限定の擬似送信テストを追加。
- [x] base64で保存されたメール本文も差し込み残りを検出する回帰テストを追加。
- [x] 実UIの操作動画を作成。架空データでコピー・保留・見送り・送信済みの記録操作を確認。
- [x] 実データの候補検索・公式サイト巡回・連絡先とフォームの抽出を実行し、21候補の確認状況をCSV化。
- [x] 実フォームで見つけた、条件表示される予約項目をhoneypot扱いする誤分類をTDDで修正。

## 検証証跡

- 基点: `python3 -m unittest discover -s tests` → 121件成功。
- キュー: `python3 -m unittest discover -s tests -p test_form_queue.py` → 修正前は後続への送信0件、修正後5件成功。
- 検出: `test_discovery_regressions.py` → 修正前12 subtest failures、修正後8テスト成功。
- 結果判定24件、DB移行8件、共有窓口6件、停止・承認規則10件も失敗を先に確認して修正。
- 最終全体: Python 3.14.6で182件成功（13.022秒）、Python 3.9.6で182件成功（12.635秒）。引き継ぎ時121件から61件追加。
- Ruff 0.17.5: `ruff check --config ruff.toml autosales tests` 成功、`ruff format --config ruff.toml --check autosales tests` 26ファイル成功。
- `compileall` / `git diff --check` 成功。Ruff設定とPython 3.9 / 3.14のGitHub Actionsを追加。
- 初回CIはPython 3.14とquality成功。Python 3.9はubuntu-latest（26.04）の配布バイナリ不足で準備段階に失敗。公式versions-manifestで3.9/3.14対応を確認し、テストrunnerをUbuntu 22.04に固定。
- 修正後CI: https://github.com/ginkgocreate/AutoSalesBot/actions/runs/43840681664 （PR）と https://github.com/ginkgocreate/AutoSalesBot/actions/runs/43840671865 （push）で全3ジョブ成功。

## 2026-11-16 ドライテストと操作動画

- 再現スクリプト: `scripts/dry_run_smoke.py`。既定は下書きまで。`--local-delivery` を付けた場合だけ、後段にlocalhostの仮SMTP・仮フォームへの配送確認を追加する。READMEに両方の実行コマンドを記載。
- 候補CSV取り込み → 架空公式ページの巡回 → メール1社・通常フォーム1社・CAPTCHA1社・営業拒否1社の分類 → 確認CSVの出力・承認取り込み → 下書き生成を、実CLIのparser/handler経由で確認。
- ドライテスト結果: メール下書き1件、フォーム下書き1件、SMTP接続0件、SMTP受信0件、フォームPOST0件、本送信記録0件。下書きの件名・本文に差し込み残りなし。
- 別段階のlocalhost擬似送信結果: SMTP受信1件、フォームPOST1件、配送記録2件、再実行の重複配送0件、番号「2」の返信1件、停止1件、CAPTCHAの手動待ち1件。返信は架空メールを `process_message` へ直接渡して検証し、IMAPには接続していない。
- `.env`・既存DB・既存連絡先を読み込まず、明示した架空設定と一時DBを使用。DNS・socketは当該テストが立てたlocalhostのポートだけを許可し、外向き通信の試行は0件。ガード単体テストでは外部DNS・外部IP・非許可localhostポートをすべてI/O前に拒否。
- 証跡: `/private/tmp/analink-dry-run-evidence/run-j7wt7a4i/`。`result.json`、`transcript.log`、CSV、下書き、段階別DB（`dry-run.sqlite3` / `local-delivery.sqlite3`）を保存。
- TDD: 追加テストを先に実行し、実装前の `ModuleNotFoundError` を確認。監査指摘のbase64本文については、実CLIが生成したメールの本文へ差し込み残りを埋める回帰テストが `RuntimeError not raised` で失敗することを確認後、MIMEをデコードして検査するよう修正。追加4テストが成功。
- 最終全体: Python 3.14.6で186件成功（16.312秒）、Python 3.9.6で186件成功（15.683秒）。Ruff 0.17.5のcheck・format（`autosales scripts tests`、29ファイル）と `git diff --check` 成功。CIのlint/format対象にもscriptsを追加。
- 動画: `outbox/demo-20261116/analink-ui-demo.mp4`。54秒、H.264、1252×1140、24fps。実UI操作39秒と検証結果を収録。全フレームのデコード正常、コピーと完了のフレームを目視確認。送信済み操作は架空データ上の記録で、外部サイトへの手動送信は行っていない。
- このドライテスト段階では本体コード・モデル・マイグレーションの変更なし。外部検索APIによる候補収集は後述の実リサーチで確認。実SMTP/IMAP、実サイトへの送信は未検証。

## 2026-11-16 実データの候補リサーチ

- 実APIの `discover osm 大阪市 --kinds nightclub,events_venue,music_venue --segment show` が42.012秒で成功。16施設から15サイトを取得し、export・statsまで完走。
- `discover search` も実行したが、Claudeの5時間利用上限でexit 1。認証障害ではない。接続済みWeb検索と公式ページ確認へ切り替え、6グループを確認して既存import・crawl・review・form review・export・statsを実行。
- 対象はINSOU、KAISER、RIVAN、JUNGLE、SPARROW/JACK、Garden Osaka/JUMEIRAH。ブランド・法人名・本部窓口・店舗窓口の違いをCSVに明記し、法人同一性は推測しない。
- OSM候補のうちGIRAFFE Japan、グランドサロン十三、The Pink、Jazz Club Gallonの4サイトも別DBで実巡回。KAISERのブランド公式リンクから法人サイトへ進み、本部問い合わせを追加確認。
- 合計21候補（OSM 15＋Web 6）。10候補を深掘りし、既存crawlerによるHTML取得成功は42ページ（別ドメインのKAISER本部を含む。robots・redirect・拒否記録を除く）。OSMの残り11候補は現況・適合未確認。
- GALLONの公式公開メール1件を取得し、未承認でreview.csvへ出力。入力例メールは取り込まれていない。KAISER本部はCAPTCHA等でmanual、INSOUは取材/協力依頼・採用の用途確認が必要。
- グランドサロン十三は営業目的の問い合わせへの拒否表示で除外。RIVANもスカウトDM禁止文でrefusedになったが、一般営業との範囲差を人が確認するまで解除しない。
- 実データと認証情報をコミットせず、隔離した研究DBを使用。承認0件・メール送信0件・フォーム送信0件。OverpassへのPOSTは検索問い合わせのみ。
- 成果物: `outbox/research-20261116/real-candidates.csv`、`verified-contact-routes.csv`、`research-summary.json`、`research-evidence.zip`。根拠URL・窓口用途・未確認範囲・OSM帰属を同梱。
- 元証跡: `data/research-real-cskoiyfu/` と `data/research-real-osm-9l9uauzb/`。OSM帰属: © OpenStreetMap contributors / ODbL（https://www.openstreetmap.org/copyright）。
- GIRAFFE実フォームの条件表示される予約項目をhoneypotと誤認しautoになっていたため修正。一般化したfixtureでCSS非表示・hidden属性・d-noneの3パターンが修正前に失敗することを確認し、manualへ変更。明示honeypot・hidden website・CSRFの挙動は保持。
- 修正後は実CLIでGIRAFFEを再巡回し、manual/pending・承認0・送信0を確認。更新済みフォームCSVと `after-fix-cli.json` を保存。公開API・モデル変更なし、追加マイグレーション不要。
- 最終検証: Python 3.14.6の188件（16.117秒）・Python 3.9.6の188件（15.683秒）が成功。Ruff check/format全30ファイル・diff-check成功。今回回帰テスト2件追加。

## Stocked Issues

- [BLOCKED] 実サイトへのフォーム送信、実SMTP配信、返信受信の本番E2Eは未実施。送信者設定・確定文面・対象の承認後に少数から確認する。
- [BLOCKED] 組み込みの `discover search` はClaudeの5時間利用上限で停止。今回は接続済みWeb検索＋CSV取り込みで実リサーチを完了。OSMの実取得は成功済み。
- [QUESTION] OSMの残り11候補は現況・事業適合未確認。会議室・結婚式場も含まれるため、取得件数をそのまま有効な営業先数と扱わない。
- [QUESTION] RIVANのスカウトDM禁止を一般営業全般の拒否と同一視できるか未確定。現在のrefusedを保ち、文脈を確認する。
- [DEBT] CF7のHTTP 400/415/429で配送成否を確定できない場合は `unknown` を維持。安易な再送を避けるため、自動で再送可能な状態には戻さない。
- [DEBT] EUC-JPで表現できない人名等は送信前に見送り。文字を欠落させず、人による確認に残す。
- [MIGRATION-REVIEW] `v001_forms.py` の downgrade はフォーム2表と返信の会社列を除去するため、全業務テーブルが空のテストDBに限定。運用データが1件でもあるDBは拒否する。upgradeは追加と返信の会社ID補完のみ。実DBには未適用。
- [DEBT] 更新前のフォーム承認CSVは照合方式が異なるため再出力が必要。`form review` で新しいCSVを作成してから承認する。

## 次のアクション

1. このPRのレビュー・マージ。
2. 送信元の情報と文面、候補一覧、承認状態を確認。
3. dry-runで入力・本文を確認後、明示的に承認された少数の対象で本番E2Eを実施。

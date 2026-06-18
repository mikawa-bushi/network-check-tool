# 改善提案レビュー 第2版（network-check-tool / 現行コード再評価）

- **対象**: リポジトリ全体（`netdiag` パッケージ + `network_check_tool` パッケージ + テンプレート/CLI/CI/設定）
- **作成日**: 2026-06-17
- **観点**: UI / 保守性 / 機能 / 処理速度
- **前提**: 前回レビュー [`docs/improvements.md`](improvements.md)（2026-06-13）の指摘（U1 Plotly ローカル配信・P1 SQL集計・P6 複合index・P3 WAL・P2 プローブ並列化・U3 セキュア既定バインド・M2/M3/M4/M5/M7/E1 等）は**ほぼ実装済み**。本書はそれらを巻き戻さず、**実装後の現行コードで残っている／新たに見える改善余地**だけを、`file:line` 付きで実証ベースに洗い出したもの。
- **手法**: 観点別に複数エージェントで現行コードを並行調査し、各指摘を別エージェントが実ファイルに当てて敵対的に検証（既対応・事実誤認を除外）。性能セクションは検証が外部レート制限で2度失敗したため、レビュアーが該当ファイルを直接精読して作成。検証で却下した指摘は「付録B」に明記。

---

## 重要度サマリ（高い順）

| # | 区分 | 重要度 | 概要 | 主な場所 |
|---|------|--------|------|----------|
| P1 | 処理速度 | ★★ | 描画系APIが時間窓内の全行を上限・間引き無しで返し、30秒ごとに全件再取得＋チャート全再描画。長期間×短間隔でJSON肥大・描画重化 | `views/dashboard.py:103-108,151-156`, `templates/dashboard.html` 自動更新 |
| U1 | UI | ★★ | naive UTC タイムスタンプを TZ 情報なしで送出。チャート軸がブラウザのローカル時刻として解釈され、JST等で数時間ずれて見える | `views/dashboard.py:168,170,205-211` |
| U2 | UI | ★★ | `/api/stats` のエラー応答(400等)を成功として扱い、`undefined.toFixed()` で統計更新が無言で止まる | `templates/dashboard.html:215-247` |
| M1 | 保守性 | ★★ | `DnsProbe` がプロセス全体の `socket.setdefaulttimeout` を書き換え、復元値が `None` 固定。`--jobs>1` 並列で競合 | `netdiag/probes/dns.py:63,70` |
| M2 | 保守性 | ★★ | `SchedulerService.__init__` が `logging.basicConfig` を呼びルートロガーを副作用的に設定 | `services/scheduler_service.py:90` |
| F1 | 機能 | ★★ | 監視でダウン検出しても1行保存するだけ。落ちたホストへ自動 `netdiag` を発火し根本原因を添付する連携が無い | `services/scheduler_service.py:32-43` ほか |
| F2 | 機能 | ★★ | 監視対象が env のカンマ区切りだけ。ホスト別の間隔・閾値を持つ宣言的設定ファイルが無い | `config/config.py:36-39`, `cli.py:62-89` |
| F3 | 機能 | ★★ | ping 監視は「落ちた/落ちてない」の二値のみ。RTT劣化・断続ロスのしきい値アラートが無い | `services/ping_service.py:61-67` |
| F4 | 機能 | ★★ | ダッシュボードが完全読み取り専用。GUI からホスト追加/削除/監視開始停止ができない | `views/dashboard.py` 全GET, `app.py:36-65` |
| U3 | UI | ★ | クエリURLに `host` を `encodeURIComponent` せず直挿入。予約文字を含むホスト名で無言の表示欠落 | `templates/dashboard.html:215,225` |
| U4 | UI | ★ | データ無し(404)を赤いエラー表示にし、空状態と障害を区別できない | `views/dashboard.py:158-159`, `dashboard.html:228-229` |
| U5 | UI | ★ | 30秒自動更新が毎回 `Loading...`→`newPlot` でズーム/パンと表示状態をリセット | `templates/dashboard.html:212,231,269-273` |
| U6 | UI | ★ | ダークモード・レスポンシブ・`aria-live`/`role` が皆無 | `templates/dashboard.html`（CSS/動的領域） |
| U7 | UI | ★ | Success Rate と Packet Loss Rate が常に和100%の冗長タイル。API の `min/max/successful_pings` は未使用のデッドデータ | `dashboard.html:240-247`, `dashboard.py:265-271` |
| U8 | UI | ★ | `netdiag scenario` が `--no-color` を持たず色を強制（`check` と非対称） | `netdiag/cli.py:355-373` |
| U9 | UI | ★ | `netdiag check` が長時間でも実行前/中の進捗を一切出さない（`--jobs` 並列時も無音） | `netdiag/cli.py:161-191` |
| U10 | UI | ★ | `--verbose` が group とサブコマンドで二重定義され意味が異なる | `netdiag/cli.py:101-106,136,354` |
| U11 | UI | ★ | `run-server -h` がヘルプではなく `--host` のエイリアス（標準慣習に反する） | `network_check_tool/cli.py:32-33` |
| M3 | 保守性 | ★ | CI の lint 網羅不足: `netdiag` は Black 未チェック、ruff(F系)/型(mypy)は CI 非実行 | `.github/workflows/black-formatter.yml`, `pyproject.toml:65-70` |
| M4 | 保守性 | ★ | ダッシュボード3エンドポイントで host/hours/since/クエリ構築が重複 | `views/dashboard.py:90-108,139-156,216-231` |
| M5 | 保守性 | ★ | `Monitor` と `Watcher` が周期ループ（スレッド/停止/残り時間 sleep）を丸ごと重複 | `netdiag/scheduler.py:36-66`, `netdiag/watch.py:143-168` |
| M6 | 保守性 | ★ | `report` の端末出力だけ日本語ハードコード、JSON/HTML は英語で i18n 不整合 | `netdiag/report.py:44,53,57,61,64` |
| M7 | 保守性 | ★ | `RUNBOOKS` がプローブ名キーで別モジュールに分離、プラグインプローブに解説が付かない | `netdiag/diagnosis/rules.py:15,126` |
| M8 | 保守性 | ★ | カンマ区切りプローブ名の解析と config 構築が `cli.py` 内で重複・分岐（756行） | `netdiag/cli.py:162,163,385,468` |
| M9 | 保守性 | ★ | ping の OK/WARN/FAIL 判定が3プローブで重複（閾値変更時にドリフト） | `netdiag/probes/icmp.py:79-98,123-137`, `gateway.py:78-97` |
| M10 | 保守性 | ★ | `dualstack` が `icmp.ping` を使わずパース再実装、`count` を無視（部分損失を誤判定する脆い部分文字列マッチ） | `netdiag/probes/dualstack.py:17-33,58-62` |
| M11 | 保守性 | ★ | 2つの CLI 間でコマンド名・オプション・用語が衝突（`ping`/`status` が別意味） | `netdiag/cli.py` ↔ `network_check_tool/cli.py` |
| M12 | 保守性 | ★ | バージョンドリフト: `network_check_tool/__init__.__version__="0.1.1"` vs pyproject `0.2.5` | `network_check_tool/__init__.py:3` |
| F5 | 機能 | ★ | 監視データの取り出し口が無い（CSV / Prometheus `/metrics` エクスポート） | `views/dashboard.py` ルート群 |
| F6 | 機能 | ★ | `AlertSink`/`MultiSink` の拡張点があるのに通知 sink は `FileAlertSink` 1種のみ（webhook/Slack/desktop が無い） | `netdiag/alerting.py:157-222` |
| F7 | 機能 | ★ | `netdiag snapshot diff` が端末テキスト固定。`--format json` もダッシュボード可視化も無い | `netdiag/cli.py:276-305` |
| F8 | 機能 | ★ | `--format`/`--json` の網羅にムラ。機械可読出力を持つコマンドと持たないコマンドが混在 | `netdiag/cli.py`, `network_check_tool/cli.py` |

> 重要度の目安: ★★=実害/誤表示/競合の温床、優先的に対応推奨。★=ポリッシュ・一貫性・将来の拡張余地（致命ではない）。

---

## 1. 処理速度（Performance）

### P1. 描画系APIの全件取得 + 30秒ごとの全再描画【★★】
- **場所**: `views/dashboard.py:103-108`（`get_ping_data`）、`:151-156`（`get_ping_chart`）、`templates/dashboard.html` の自動更新（`setInterval(..., 30000)` → `loadData` → `/api/ping_chart`）。
- **問題**: 両エンドポイントとも `filter(target_host==?, timestamp>=since).order_by(timestamp).all()` で**時間窓内の全行を上限・間引き無し**で取り出す。`hours` の上限は `HOURS_MAX=168`（1週間）。60秒間隔なら1ホストあたり約1万行、短間隔監視ではさらに増える。`get_ping_chart` はその全点から毎リクエスト `go.Figure` を組み直し `PlotlyJSONEncoder` で直列化し、クライアントは30秒ごとに**全データを再取得して `Plotly.newPlot` で全再描画**する。
- **根拠**: 行数に比例して「SQL→JSON直列化→転送→ブラウザ描画」がすべて線形に重くなる。集計値は SQL に寄せた（前回 P1 対応済み）が、**生データ経路は手付かず**で、ダッシュボードを開きっぱなしにする運用ほどコストが積み上がる。複合インデックスは範囲取得を速くするが、返す点数そのものは減らさない。
- **改善案**:
  1. **点数を間引く**: 描画用に最大点数（例 2000点）へ `LIMIT` するか、時間バケットで平均化する。`netdiag/storage.py` の `downsample` と同じ発想で、表示幅に対して過剰な解像度を返さない。
  2. **差分更新**: クライアント側で `Plotly.react`（`newPlot` ではなく）を使い、自動更新時は `since=<最後の点のtimestamp>` で**増分だけ**取得して追記する。ズーム維持（→ U5）も同時に解決する。
  3. **作図責務をクライアントへ**: `/api/ping_chart`（サーバ側作図）を廃し、`/api/ping_data`（生 or バケット集計）からクライアントで一度だけ作図する経路に寄せると、サーバの図再構築コストが消える。

### P2. バッチ ping の1件ごとコミット【★】
- **場所**: `services/ping_service.py:78-79`（`save_ping_result` が毎回 `db.session.commit()`）、呼び元 `cli.py:124-130`（`ping --count N` のループ）。
- **問題**: `ping --count N` は N 回 `ping_and_save` を呼び、**N 個の独立トランザクション**になる（各回 `sleep(1)` もある）。常駐監視は「1ホスト1間隔1ping」なので実害は小さいが、CLI のバッチ確認では無駄なコミットが積み上がる。
- **改善案**: CLI バッチ経路ではループ内で `add` だけ行い、最後に1回 `commit` する（`save_ping_result` に `commit=True` 引数を足し、バッチ側は `False` で呼ぶ）。常駐監視の1件1コミットは現状維持で問題ない。

---

## 2. UI

### U1. naive UTC タイムスタンプでチャート軸がローカル時刻として誤描画【★★】
- **場所**: `views/dashboard.py:168,170`（`get_ping_chart` が `result.timestamp` をそのまま `go.Scatter(x=...)` へ）、`:205-211`（`update_layout` に軸のタイムゾーン明示なし）。`:119`（`get_ping_data` の `isoformat()`）は外部API消費者向けの潜在問題。
- **問題**: `timestamp` は `models/ping_result.py:20` + `utils.utcnow()` で**意図的に naive UTC**（`tzinfo=None`）として保存される。これを TZ 情報（`Z` / `+00:00`）無しで Plotly に渡すため、Plotly/ブラウザは「ローカル時刻」として解釈する。UTC で記録した時刻が JST（+9h）等のロケールでは**軸ラベルが一律にずれて**見える。「UTC か local か」の注記も無い。
- **根拠**: 監視ダッシュボードの一次情報は「いつ落ちたか」。サーバ UTC・閲覧者 JST という構成は一般的で、無音のまま障害時刻の相関分析を誤らせる。窓幅(N時間)は保たれるが、絶対時刻の読みがずれる。
- **改善案**: 送出時に UTC を明示する（`timestamp.replace(tzinfo=timezone.utc)` で aware 化、`isoformat()` が `…+00:00` になる）。さらにクライアントで明示的にローカル変換するか、軸に「Time (UTC)」と注記して**どのタイムゾーンかを必ずユーザに伝える**。

### U2. `/api/stats` のエラー応答を成功として扱い統計更新が無言で停止【★★】
- **場所**: `templates/dashboard.html:215-218`（`fetch().then(r => r.json())` で `response.ok` を見ない）、`:240-247`（`updateStats` が `data.avg_response_time.toFixed(1)` 等を直接参照）。バックエンドの400経路 `views/dashboard.py:36-44`（`_parse_hours`）→ `:220-222`。
- **問題**: `_parse_hours` が不正な `hours` で `{"error": ...}` を 400 で返すと、ボディに `avg_response_time` 等のキーが無く、`updateStats` 内で `undefined.toFixed()` が `TypeError` を投げる。`.catch` はそれを拾うが `console.error` だけで握りつぶし、**ユーザには何のフィードバックも出ず統計タイルが古い値/`-` のまま静かに固まる**。チャート側は `data.error` 表示と可視 `.catch` を持つのに、stats 側だけ可視エラー経路が無い。
- **根拠**: ステータスコードを見ない `fetch` の典型的な堅牢性欠陥。エラー時に「嘘の表示」が残るのは監視ダッシュボードとして最も避けたい。現状 UI 経由で400を踏むのは容易でないが、将来の 5xx・直叩き・改修時に顕在化する。
- **改善案**: `if (!response.ok) return response.json().then(e => Promise.reject(e))` で 4xx/5xx を reject に落とし、stats 側 `.catch` でも統計タイルを `—` にして `showError` で可視通知する。`updateStats` 冒頭に `typeof data.avg_response_time !== 'number'` のガードを足す。チャート側 fetch も同じ `response.ok` 非チェックなので同時に直すとよい。

### U3. クエリURLでの `host` 未エンコード【★】
- **場所**: `templates/dashboard.html:215,225`（`` `/api/stats?host=${currentHost}&hours=...` `` を未エンコードで構築）。
- **問題**: `currentHost` は `target_host`（任意文字列, `String(255)`）そのもの。`&`・`#`・空白・日本語等を含むとパラメータが途中で切れ、サーバ側の完全一致照合で404になり、**その特定ホストだけ無言で表示できなくなる**。
- **根拠**: 通常は IP/FQDN だが、任意ラベルが DB に入る可能性があり再現困難なバグになる。値は自前DB由来で `fetch` URL にしか乗らないため XSS ではなく表示欠落。
- **改善案**: 2箇所を `host=${encodeURIComponent(currentHost)}` にする。共通ヘルパ `buildQuery(host, hours)` に集約すれば将来のエンコード漏れも防げる。

### U4. データ無し(404)を赤エラー表示にし空状態と障害を混同【★】
- **場所**: `views/dashboard.py:158-159`（`ping_chart` がデータ0件で 404 + `{"error":"No data found"}`）、`templates/dashboard.html:228-229`（`data.error` を赤 `.error` で表示）、`:94-100`（赤スタイル）。
- **問題**: 「選択期間にデータがまだ無い」は正常な空状態だが、視覚的には障害時と同じ赤エラーになる。さらに `/api/stats` は0件でもゼロ値の正常JSONを返す（`:235-246`）ため、**統計タイルは 0.0% を表示しつつチャートだけ赤エラー**という不整合が起きる。新規ホストや短い時間レンジで頻発。
- **改善案**: 空状態・読み込み中・エラーの3状態を分離する。サーバは「データ無し」を 404+error ではなく 200 + `{"empty": true}` で返すか、クライアントが `response.status===404` を中立な空状態（グレー）として扱い「この期間のデータはまだありません」を表示。赤 `.error` は真の失敗(5xx/ネットワーク)のみに限定。`total_pings===0` のとき統計タイルも空状態に揃える。

### U5. 30秒自動更新がズーム/パンと表示状態を毎回リセット【★】
- **場所**: `templates/dashboard.html:212`（`chartContainer.innerHTML='Loading...'`）、`:231`（`Plotly.newPlot`）、`:269-273`（`setInterval(..., 30000)` → `loadData`）。
- **問題**: 自動更新のたびにチャートを `Loading...` で全消去し `newPlot` で作り直すため、ユーザが拡大/移動していたビューが30秒ごとに初期状態へ戻る。長期トレンドを観察しづらい。
- **改善案**: P1 の差分更新案と同じく `Plotly.react` を使い、ズーム/パン状態を保ったまま系列だけ更新する。`Loading...` 全消去は初回ロード時のみに限定する。

### U6. ダークモード・レスポンシブ・アクセシビリティの欠落【★】
- **場所**: `templates/dashboard.html`（CSS に `@media`/`prefers-color-scheme` 無し、`.controls` に `flex-wrap` 無し、動的更新領域に `aria-live`/`role` 無し — grep で該当0件を確認）。
- **問題**: 背景/カード色がライト固定でダークモード非対応、ブレークポイントが無く小画面で窮屈、統計タイル・エラー通知が支援技術に伝わらない。
- **根拠**: 監視ツールは夜間/暗所運用が多くダークモード要望が強い。`aria-live` 欠如は「値が変わったこと」が視覚ユーザにしか伝わらない。
- **改善案**: 優先度順に (1) `statsContainer`/`chartContainer` に `aria-live="polite"`、エラー領域に `role="alert"`（小コスト・効果明確、単独でも推奨）、(2) `.controls` に `flex-wrap:wrap` ＋狭幅 `@media` を1点、(3) 色を CSS 変数化し `@media (prefers-color-scheme: dark)` で切替。

### U7. 冗長な統計タイルと未使用の集計値【★】
- **場所**: `templates/dashboard.html:242,244`（Success Rate = `100 - packet_loss_rate`、Packet Loss Rate = `packet_loss_rate`）、`views/dashboard.py:265-271`（API は `successful_pings`/`failed_pings`/`min`/`max` も返す）。
- **問題**: Success Rate と Packet Loss Rate は定義上必ず和100%で、4枚のタイルの1枚を実質ダブり表示に費やしている。一方 API が返す `min_response_time`/`max_response_time`/`successful_pings` はテンプレートが一切表示せず**デッドデータ**になっている。
- **改善案**: 冗長タイルの片方を「最小/最大RTT」や「成功/総数」など API が既に返している有用値に差し替える。表示しない集計はレスポンスから外すか UI で活用するか、どちらかに揃える。

### U8〜U11. CLI のUX小整合【★】
- **U8 色の非対称**: `netdiag scenario`（`cli.py:355-373`）は `render_terminal(color=sys.stdout.isatty())` 固定で `--no-color` を持たない。`check`（`:138,177`）は持つ。`scenario` にも `--no-color` を足し、TTY判定と OR を取る。
- **U9 進捗フィードバック皆無**: `netdiag check`（`cli.py:161-191`、`engine.run` at `:166`）は実行前/中に何も出さず、能動プローブ（ping系）が積み上がる間 TTY 上で固まって見える（`--jobs` 並列時も無音）。進捗は **stderr** に出し **TTY かつ非 JSON/HTML 出力時のみ**に限定すれば stdout の機械可読性を壊さない。
- **U10 `--verbose` の二重定義**: group（`cli.py:101-106`、`_setup_logging` を駆動）と各サブコマンド（`check :136` → `Engine(verbose=...)`）で同名・別意味。group はロギング詳細、command は推論過程の表示で、ユーザを混乱させる。役割を分けて命名する（例 group 側 `--log-level`）か、意味を統一する。
- **U11 `-h` の非慣習**: `network_check_tool/cli.py:32-33` で `run-server -h` が `--host` のエイリアス。`-h`=ヘルプは事実上の標準で最小驚き原則に反する。`-h` は Click 既定のヘルプに戻し、host の短縮は別文字（例 `-H` か無し）にする。

---

## 3. 保守性（Maintainability）

### M1. `DnsProbe` がプロセス全体の socket デフォルトタイムアウトを書き換え【★★】
- **場所**: `netdiag/probes/dns.py:63`（`socket.setdefaulttimeout(timeout)`）、`:70`（finally で `socket.setdefaulttimeout(None)`）。
- **問題**: `setdefaulttimeout` は**プロセス全体のグローバル状態**。(1) `--jobs>1`（前回 P2 で導入された並列実行）で他プローブと同時に走ると相互に干渉し、(2) 復元先が `None` 固定なので、呼び出し前に別コード（Flask 側やプラグイン）が独自のデフォルトを設定していた場合それを**破壊**する。
- **根拠**: `netdiag` は `__init__` の `NullHandler` 方針が示すとおり「ライブラリとしても使える」設計。ライブラリがインポート元プロセスのグローバル socket 設定を恒久変更/消去するのは副作用が大きく、並列化が入った以上グローバル可変状態は競合の温床。
- **改善案**: プロセスグローバルの書き換えをやめる。`getaddrinfo` はリゾルバ/OS のタイムアウトで概ね律速されるので `setdefaulttimeout` を撤去するか、どうしても必要なら**専用スレッド + `join(timeout)`** でタイムアウトを局所化し、グローバル状態に触れない（`socket.setdefaulttimeout` を `socket` モジュール全体に効かせない）。

### M2. `SchedulerService.__init__` が `logging.basicConfig` でルートロガーを設定【★★】
- **場所**: `services/scheduler_service.py:90`（`logging.basicConfig(level=logging.INFO)`）。`netdiag` 側は `__init__` で `NullHandler` を付け「ライブラリはグローバル設定しない」原則を取っている。
- **問題**: サービス生成という副作用的タイミングで `basicConfig` を呼び、アプリ全体のルートロガー設定を上書きしうる。ライブラリ/サービス層がグローバルなロギング設定を握るのはアンチパターン。
- **改善案**: サービス層は `logging.getLogger(__name__)` のみ使う。`basicConfig` は CLI エントリポイント（`network_check_tool/cli.py` の各コマンド先頭）でのみ実行する。

### M3. CI の lint 網羅不足【★】
- **場所**: `.github/workflows/black-formatter.yml`（Black チェックの対象範囲）、`pyproject.toml:65-70`（ruff は `select=["I"]`=import 整列のみ）、`.pre-commit-config.yaml`。
- **問題**: CI は Black（しかも対象が `network_check_tool/` 中心で `netdiag` がフォーマット検査外の可能性）と pytest が中心で、未使用 import（F401）等の検出も型チェック（mypy）も走らない。前回 M3 で消した未使用 import の再発を CI が自動では止められない。
- **改善案**: Black チェック対象に `netdiag/` を含める。ruff の `select` に F系（少なくとも F401/F811）を段階的に追加し、CI ポリシーと同期させる（`pyproject.toml` のコメントが「CI ポリシーと一緒に広げよ」と明記しているとおり）。型チェックは `mypy` を `netdiag` のコア（純関数中心で型が付けやすい）から導入する。

### M4. ダッシュボード3エンドポイントの定型重複【★】
- **場所**: `views/dashboard.py:90-108`（`get_ping_data`）、`:139-156`（`get_ping_chart`）、`:216-231`（`get_stats`）。
- **問題**: (a) `host = request.args.get("host")`、(b) `hours, error = _parse_hours()` の早期 return、(c) `since = utcnow() - timedelta(hours=hours)`、(d) 同じ `filter`/`order_by` の組み立てが3関数で重複。
- **改善案**: `_parse_host_hours(require_host: bool=False) -> (host, since, error)` のような小ヘルパに (a)〜(c) を集約。クエリ基底も `_base_query(host, since)` に寄せると、将来のエンドポイント追加（→ F5 エクスポート）でも再利用できる。

### M5. `Monitor` と `Watcher` の周期ループ重複【★】
- **場所**: `netdiag/scheduler.py:36-66`（`Monitor` の `_stop`/`_thread`/`_loop`/`start`/`stop`）、`netdiag/watch.py:143-168`（`Watcher` 同形）。
- **問題**: スレッド起動・停止イベント・`interval` から経過時間を引いた残り時間 sleep という周期ループの骨格が両クラスでほぼ同一。片方の停止/sleep バグ修正がもう片方に伝播しない。
- **改善案**: `_PeriodicLoop`（`interval`、`start(background)`、`stop()`、`_tick()` フック）を抽出し両者の基底にする。サブクラスは `_tick` のみ実装。停止/残時間計算の一点修正が両方に効く。

### M6. `report` 端末出力の日本語ハードコードと i18n 不整合【★】
- **場所**: `netdiag/report.py:44,53,57,61,64`（端末見出し「診断:」「推定原因:」「根拠:」「確信度:」「次の確認手順:」を日本語リテラル）。一方 `render_json`/`render_html` は英語（`:127` "Next steps:" 等）。
- **問題**: 同じ Diagnosis でも端末は日本語・機械可読/アーカイブは英語と、チャネルで言語が分裂している。ロケール切替もテストもしづらい。
- **改善案**: 文言を定数テーブル（`MESSAGES` 等）に集約し、端末/JSON/HTML で同じソースを参照。将来の言語切替（環境変数 or `--lang`）の土台にする。

### M7. `RUNBOOKS` のプローブ名キー分離とプラグイン非対応【★】
- **場所**: `netdiag/diagnosis/rules.py:15`（`RUNBOOKS` をプローブ名でキー）、`:126`（`runbook_steps`）、`core/probe.py:44-53`（Probe は `what`/`why`/`manual_commands` を持つ）、`plugins.py:21-49`（エントリポイントでプラグインプローブ読込）。
- **問題**: 原因→次手順の対応表がプローブ名キーで別モジュールに固定化されており、`plugins` 経由で追加したプローブには runbook が紐付かず解説が出ない（フォールバックは `root.summary` のみ）。プローブ自身が解説を保持する設計（`core/probe.py`）と二重管理。
- **改善案**: runbook をプローブ定義側（`Probe.runbook_steps` 等のフック）に持たせ、`RUNBOOKS` は組込みプローブの既定として残す。プラグインプローブが自前で手順を提供できるようにする。

### M8. `cli.py` のCSV解析・config構築の重複（756行）【★】
- **場所**: `netdiag/cli.py:162,163,385,468`（カンマ区切り文字列の split が4箇所）。config 構築も `_build_config`（`:83`、呼び出しは `:161` のみ）と `scenario`/`monitor`/`watch` で分岐。
- **問題**: 「`a,b,c` → `["a","b","c"]`（空要素除去）」が手書きで散在し、strip/空要素ポリシーがずれるリスク。`cli.py` が756行で責務過多。
- **改善案**: `_split_csv(value) -> Optional[List[str]]` を抽出して4箇所を置換（低リスク・高効果）。config 構築も共通化し、必要ならサブコマンド群を `cli/` パッケージに分割する。

### M9. ping の OK/WARN/FAIL 判定の3重複【★】
- **場所**: `netdiag/probes/icmp.py:79-98`（`ExternalProbe`）、`:123-137`（`PingProbe`）、`gateway.py:78-97`（`GatewayProbe`）。
- **問題**: `icmp.ping()` の戻り `(rtt, loss, text)` を受けた後、3プローブが「`loss>=100`→fail / `loss>0`→warn / else→ok」という同一分岐を手書きで繰り返す（文言だけ違う）。閾値や方針を変えるとき3箇所同期が要る典型のドリフト源。`GatewayProbe` は ok で `{rtt:.2f}`・warn で `{rtt:.1f}` と書式も微妙に不一致。
- **改善案**: `classify_ping(name, rtt, loss, text, *, fail_msg, warn_msg, ok_msg, ...) -> CheckResult` を `icmp.py` に新設し閾値分岐と metrics 組立を集約。閾値は `FAIL_LOSS_PCT=100.0`/`WARN_LOSS_PCT=0.0` をモジュール定数化。挙動不変の純リファクタなので既存プローブテストで担保。

### M10. `dualstack` の ping パース再実装と `count` 無視【★】
- **場所**: `netdiag/probes/dualstack.py:17-33`（`_ping_family`）、`:58-62`（`check`）。
- **問題**: `icmp.py` にコンパイル済み正規表現と Windows/Linux 分岐を備えた `ping()` があるのに、`_ping_family` は独自に `"0% packet loss" in stdout` の**部分文字列マッチ**で損失判定し、引数組み立ても二重実装。`"0% packet loss"` は `"10% packet loss"` の部分文字列なので**部分損失を誤判定し得る**（正規表現の `icmp` 側より脆い）。さらに `check` は `_ping_family(host, v6=False)` を `count` 無しで呼び、他プローブが尊重する `ctx["count"]` が dualstack だけ効かない。
- **改善案**: `_ping_family` を `icmp.ping` 実行+パースへ委譲する（`icmp.ping` に family/`-4`/`-6` 引数を足すか、引数だけ組んで `icmp.ping` に渡す）。`loss==0.0→True`/`loss>=100.0→False` で判定。`ctx.get("count")` を伝播。v4-ok/v6-down の非対称をスタブで単体テスト（`tests/test_probe_parsers.py` のパターン流用、現状未テスト）。※`_resolver` は dns 以外のプローブが本来読まない設計なので伝播は不要。

### M11. 2つの CLI の用語/オプション衝突【★】
- **場所**: `netdiag/cli.py`（`ping :198`, `status :570`, `monitor :381`）↔ `network_check_tool/cli.py`（`ping :113`, `status :141`, `start-monitoring :65`）。
- **問題**: 両 CLI に `ping`/`status` があるが意味が違う（netdiag `ping`=アドホック ICMP / network-checker `ping`=DB保存付き）。学習コストと取り違えの温床。
- **改善案**: 致命ではないので低優先。少なくとも README とヘルプで「2ツールの役割と同名コマンドの差」を明記。長期的には用語（monitor/watch/start-monitoring 等）を1つの語彙に整理する。

### M12. バージョンドリフト【★】
- **場所**: `network_check_tool/__init__.py:3`（`__version__ = "0.1.1"`）vs `pyproject.toml:3`（`version = "0.2.5"`）。`netdiag/__init__.py` は `0.1.7`（独立管理、`netdiag/CHANGELOG.md` と一致＝こちらは正常）。
- **問題**: 配布版は pyproject から決まるため `0.1.1` は**未使用のまま放置された古い値**。参照されないので実害は無いが、混乱の元。
- **改善案**: `network_check_tool/__init__.__version__` を `importlib.metadata.version("network-check-tool")` から導出する（`netdiag/cli.py:59` の `_resolve_version` と同じ手法）か、単純に削除する。

---

## 4. 機能（Features）

> プロジェクトの思想（オフラインファースト・stdlib中心・依存グラフ診断・自己完結）に沿い、既存の拡張点を活かせる順に。F1〜F4 は「設定→検知→通知→操作」の価値線を作る中核で、相互に噛み合う。

### F1. 監視ダウンを契機に `netdiag` 診断を自動実行【★★】
- **場所**: `services/scheduler_service.py:32-43`（`_run_ping_job`）、`services/ping_service.py:82-84`、`netdiag/diagnosis/engine.py`（`Engine`/`Diagnosis`）、`netdiag/alerting.py:206-222`。
- **問題**: ping 監視はパケットロスを検出しても `PingResult` を1行書くだけで**何も診断しない**。READMEの売り文句は「`network-checker` が *that* something is down、`netdiag` が *why*」なのに、両者が連携していない。
- **根拠**: 落ちた瞬間にこそ根本原因（どのレイヤが切れたか）が知りたい。`netdiag` の `Engine` は既にライブラリとして呼べるので、監視ジョブが失敗を検出したら `netdiag` を1回走らせ、結果をアラート/履歴に添えるのは思想そのものの実現。
- **改善案**: `_run_ping_job` で `packet_loss` が一定回連続したホストに対し `Engine().run()` を発火（過剰実行を避けるためデバウンス/レート制限）。結果の `Diagnosis.primary` を `PingResult` とは別テーブル or ファイルに保存し、ダッシュボード/アラートに「推定原因」を添付する。

### F2. 監視ホストの宣言的設定ファイル（TOML/JSON）【★★】
- **場所**: `config/config.py:36-39`（`DEFAULT_HOSTS` は env を split しただけ）、`cli.py:62-89`（`start-monitoring` は全ホスト共通の単一 `--interval`）。
- **問題**: ホスト別の間隔・閾値・IPファミリを表現できず、再起動のたびにコマンド引数を打ち直す運用は壊れやすい。`toml` は依存にあるのに監視構成には未使用。
- **根拠**: 運用が増えると「このホストは30秒・loss>5%で警告、こっちは5分・rtt>200ms」のような差をつけたくなる。宣言的設定は差分管理・レビュー・再現性に効き、F3 の閾値や IPv6 監視の置き場所にもなる。
- **改善案**: `config` 配下に TOML/JSON ローダー（`[[host]] name interval rtt_warn_ms loss_warn_pct family`）を新設。`start-monitoring --config PATH` で per-host `interval` を `add_ping_job(host, interval)` に流す（`add_ping_job` は既に host 単位 interval 対応＝スケジューラ無改修）。閾値はまずパース・保持のみ。env `DEFAULT_HOSTS` は後方互換で維持、パースエラーは非ゼロ終了。

### F3. しきい値アラート（RTT/パケットロス）【★★】
- **場所**: フック点 `services/scheduler_service.py:32-43`（`ping_and_save` 直後）。再利用先 `netdiag/alerting.py:68`（`StateMachine`、フラップ抑制付き）、`:171/:209`（`FileAlertSink`/`MultiSink`）、`netdiag/diagnosis/baseline.py:35`（`compute_baseline`、median/MAD）。
- **問題**: ping 監視は「落ちた/落ちてない」の二値のみで、**劣化（遅い・たまに落ちる）のアラートが無い**。完全ダウンより見逃されやすく実害が大きい。netdiag 側には劣化検知部品があるのに ping ダッシュボード側は素通り。
- **改善案**: `_run_ping_job` の保存直後にホスト別 `StateMachine.observe(healthy = not packet_loss and rtt<=閾値)` を呼び、DEGRADED/RECOVERED を sink へ emit。閾値は `config` に `PING_RTT_THRESHOLD_MS`/`PING_LOSS_THRESHOLD`（可能ならホスト別、F2 が置き場）。`compute_baseline` で baseline 比 Nσ 超え判定も選択肢。**注意**: `StateMachine` はインメモリなので、再起動をまたぐホスト別ストリークを残すなら永続化を別途設計。まずは `FileAlertSink` + ダッシュボード表示で価値を出し、通知出口は F6 とセットで段階導入。

### F4. ダッシュボードからのホスト管理【★★】
- **場所**: `views/dashboard.py`（全ルート GET のみ、`get_hosts :82-87` は履歴の DISTINCT）、`app.py:36-65`（`create_app` はスケジューラ非同居）、`services/scheduler_service.py:105-145`（`add_ping_job`/`remove_ping_job`/`list_jobs`/`get_job_status` は既に揃う）。
- **問題**: ダッシュボードは完全読み取り専用で、監視対象の追加/削除/開始停止が GUI から不可能（すべて CLI 再実行）。`run-server` と `start-monitoring` は別プロセスのため、サーバを開いたまま対象を増やせない。
- **改善案**: `create_app` で `SchedulerService` を1つ起動・保持し、起動時に F2 の設定ファイルから既存ジョブを再登録。`POST /api/hosts`→`add_ping_job`、`DELETE /api/hosts/<host>`→`remove_ping_job`、`GET /api/jobs`→`list_jobs` を追加し、テンプレートに追加フォーム＋監視中ジョブ一覧+停止ボタン。**前提**: プロセス内スケジューラは単一ワーカ運用前提（multi-worker だと各ワーカが独立スケジューラを持ち重複pingになる）。書き込み API は非ループバックbind時は無効化/警告し、既存のセキュア既定（127.0.0.1・認証なし前提）と整合させる。

### F5. 監視データのエクスポート（CSV / Prometheus `/metrics`）【★】
- **場所**: `views/dashboard.py` のルート群（現状 `/api/hosts`/`/api/ping_data`/`/api/ping_chart`/`/api/stats` のみ、すべて Plotly 描画前提）。
- **問題**: SQLite にデータが貯まる一方、外部の監視基盤(Prometheus/Grafana)や表計算へ持ち出す手段が無い（リポジトリ全体に csv/prometheus 実装なしを grep 確認）。
- **根拠**: `/metrics` は pull 型・CSV はファイル出力でオフラインファースト思想と両立。stdlib(`csv`) + 既存 `func` 集計だけで実装でき**新規依存ゼロ**。
- **改善案**: (1) `GET /api/export?host=&hours=&format=csv`（`csv` + `stream_with_context` で逐次出力、`_parse_hours` の入力検証を再利用）。(2) `GET /metrics`（`get_stats` と同じ集計をホスト単位でループし `network_ping_up{host}`/`network_ping_loss_ratio{host}`/`network_ping_rtt_ms{host}` を text exposition format で）。分位点は SQLite に分位関数が無いので初版は avg/min/max を別系列で。0.0.0.0 オプトイン時にスクレイプ口が開く点は docs 明記。

### F6. アラート通知チャネル（webhook / Slack / desktop）【★】
- **場所**: `netdiag/alerting.py:157-222`（`AlertSink` Protocol / `FileAlertSink` / `MultiSink`）、`netdiag/cli.py:405-516`（`watch`、現状 sink は `FileAlertSink` にハードワイヤ）。
- **問題**: `AlertSink`/`MultiSink` という拡張点が既にあるのに sink 実装は `FileAlertSink`（JSONL+log）の1種だけ。状態変化を能動的に人へ届ける手段が無く、ファイルを tail し続けないと気づけない。
- **改善案**: `WebhookSink(url, timeout=5)`（`urllib.request` で `Transition.to_dict()` を POST、stdlib のみ）、Slack 向けは payload を `{"text": ...}` に整形する薄いサブクラス、`DesktopSink` は `shutil.which('notify-send'/'osascript')` を見て `subprocess.run`。いずれも外部依存ゼロ。`watch` に `--webhook-url/--slack-url/--desktop` を足し `MultiSink([...])` に束ねる。sink 失敗は既存 `MultiSink` の `logger.warning(exc_info=True)` に乗り、`StateMachine` の debounce で通知スパムにならない。各 sink にタイムアウト必須（監視ツールが通知でブロックしないため）。

### F7. `snapshot diff` の JSON 化とダッシュボード可視化【★】
- **場所**: `netdiag/cli.py:276-305`（`snapshot_diff` は `--format` 無し・`click.echo` のみ）。対比 `check` は `--format text/json/html`（`:132-134`）、`report.py:84/106` に `render_json`/`render_html`。`snapshot/diff.py` の `SnapshotDiff`/`SourceDiff` は `added`/`removed` の dataclass。
- **問題**: 「過去の健全時と今で何が変わったか」を出す強力な機能なのに出力が端末テキストに閉じている。`check` が `--format json/html` を持つのに `snapshot diff` には無く非対称。
- **改善案**: `snapshot_diff` に `--format text/json` を追加し、`SnapshotDiff`/`SourceDiff` を dict 化する小シリアライザを新設（※`report.py` の `render_json` は Diagnosis 専用で流用不可、別途必要）。出力は source ごとの `{key,label,added:[],removed:[]}`（※snapshot に ok→fail のステータス概念は無く、設定**行**の増減である点に注意）。ダッシュボードは `~/.netdiag/snapshots` を読む read-only エンドポイントで「どのサブシステム(routes/dns/firewall)の設定行が増減したか」を色分け表示。CLI の `--format json` を先行実装し、ダッシュボード統合は別PRに分割。

### F8. `--format`/`--json` 網羅のムラ【★】
- **場所**: 機械可読出力を持つ: `netdiag check`（`:132-134`）/`scenario`（`:353`）/`status`（`:569`）。持たない: `netdiag ping`/`peer test`/`monitor`/`watch`/`history`/`prune`、および `network_check_tool` の `status`/`ping`（固定幅テキストのみ、`--json` 一切なし）。
- **問題**: 機械可読出力の有無がコマンドごとにバラバラ。診断中核は JSON を出すのに、`network-checker` 側は全コマンドがテキストのみで、スクリプト連携時にパースが必要。
- **改善案**: 出力を持つコマンドに統一的に `--json`（or `--format`）を順次追加。少なくとも `network-checker status`/`ping` の JSON 化は CI/監視スクリプトからの利用価値が高い。共通の「dict→text/json レンダラ」を1つ用意して各コマンドが選ぶ形にする。

---

## 5. 推奨着手順（コスト対効果）

1. **即修正・低コスト**: U3（encodeURIComponent 1行）、U11（`-h` をヘルプへ）、M12（version drift）、U7（冗長タイル差し替え）、U8（scenario `--no-color`）。
2. **誤表示/堅牢性（★★ UI）**: U1（UTC 明示）、U2（`response.ok` チェック + 可視エラー）、U4/U5（空状態分離・`Plotly.react` でズーム維持）。U5 は P1 の差分更新とセットで実装すると効率的。
3. **競合の温床（★★ 保守性）**: M1（socket グローバル撤去/局所化）、M2（`basicConfig` を CLI へ移動）。
4. **性能**: P1（描画系の間引き＋差分更新）。P2 は CLI バッチのみ軽微対応。
5. **機能の価値線（★★）**: F2（設定ファイル）→ F3（閾値アラート）→ F6（通知チャネル）→ F1（自動 netdiag）→ F4（ダッシュボード操作）の順で段階導入すると、各段が次段の土台になる。
6. **中期の保守性**: M3（CI に Black=netdiag/ruff F系/mypy）、M4/M5/M8/M9/M10（重複の共通化リファクタ、いずれも挙動不変で既存テストで担保可能）。

---

## 付録A：良い点（維持したい設計）
- 前回レビューの指摘がほぼ実装され、**セキュア既定（127.0.0.1 / debug 拒否 / CORS オプトイン）**・**Plotly ローカル配信（オフライン整合）**・**SQL集計+複合index**・**WAL+busy_timeout**・**プローブ並列化（既定直列で計測純度維持）**・**`utcnow()` 統一 + AST 回帰ガード**が揃っている。
- `netdiag` の `run()`／`Probe.run` が「ツール不在・権限不足でクラッシュせず SKIP に降格」を一貫（堅牢性の手本）。
- 依存グラフによる原因/結果の分離（`diagnosis/engine.py`）と runbook のデータ化（`diagnosis/rules.py`）。
- 時系列保存の複合インデックスとダウンサンプリング/リテンション（`netdiag/storage.py`）。← P1 の描画系間引きはこの `downsample` の発想を Flask 側にも適用するということ。
- 純関数化されたパーサ（`icmp.ping`、`traceroute._parse_hops`）とテスト容易性。CLI/ダッシュボードAPI/レポートのテストも整備済み。
- `netdiag/__init__` の `NullHandler` 方針（ライブラリはグローバルロギング設定をしない）。← M1/M2 はこの原則を `DnsProbe`/`SchedulerService` にも徹底すべき、という指摘。

## 付録B：検証で却下した指摘（誤検出）
- **「CHANGELOG が netdiag のバージョン履歴を追跡していない」→ 却下（事実誤認）**: 専用の `netdiag/CHANGELOG.md` が既に存在し、`0.1.1→0.1.7` の各版を issue（#35/#20/#14/#12/#8/#6）にマップ済み。`netdiag/__init__.__version__="0.1.7"` も先頭エントリと一致。`_resolve_version`（`netdiag/cli.py:59`）が `importlib.metadata` でdist版を優先し `__version__` をフォールバックする二系統管理も健全。
- このほか、前回レビューで対応済みの項目（CDN Plotly / SQL未集計 / WAL欠如 / 0.0.0.0既定 / `PING_TIMEOUT`未配線 / `datetime.utcnow()` / 未使用import / デッドコード等）に「戻る」指摘は、検証段で現行コードと矛盾するため全て除外している。

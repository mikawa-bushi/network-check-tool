# Plan: issue-4 低コスト品質修正バンドル (U2/U6/M2/M3/E4)

## Goal (verbatim from issue #4)

`network_check_tool` の CLI とダッシュボードに残る低リスクな既知不具合5件をまとめて修正する。
完了後は、ping結果一覧の文言が正しく、0.0ms RTT が "timeout" と誤表示されず、`PING_TIMEOUT`
設定が実際に効き、未使用importが除去され、ダッシュボードAPIの `hours` パラメータが検証される。

## Findings (source locations confirmed)

- **U2** `cli.py:116` — `f"Recent ping results{'for ' + host if host else ''}:"` → ホスト指定時に
  "resultsfor" と連結。スペース欠落。
- **U6** `cli.py:123-125` — `f"{result.response_time:.2f}ms" if result.response_time else "timeout"`。
  `response_time == 0.0` が偽値なので 0.00ms / 極小RTT が "timeout" と誤表示。Status列は
  `result.packet_loss` で判定しており不整合。
- **M2** タイムアウト未配線:
  - `cli.py:76` (`ping` コマンド) — `PingService()` で常に既定4.0秒。`Config.PING_TIMEOUT` 無視。
  - `services/scheduler_service.py:61` — `self.ping_service = PingService()` で同上（定時監視）。
  - `Config.PING_TIMEOUT` は `config/config.py:15` に存在するが誰も渡していない。
- **M3** `views/dashboard.py:3` — `from sqlalchemy import desc, func`。両方ともファイル内で未使用。
- **E4** `views/dashboard.py:29,76,152` — `int(request.args.get("hours", 24))`。`?hours=abc` で
  `ValueError`→500。負値・範囲外も無検証。

## Approach

### U2 (cli.py)
`'for ' + host` → `' for ' + host` に修正（"for" の前にスペース）。

### U6 (cli.py)
偽値判定をやめ、Status列と整合させる:
`response_str = "timeout" if result.packet_loss or result.response_time is None else f"{result.response_time:.2f}ms"`
これで `response_time == 0.0` かつ `packet_loss == False` は "0.00ms"、実際の無応答のみ "timeout"。

### M2 (cli.py + scheduler_service.py)
設定値の注入はエントリポイント(CLI)で行い、サービス層は受け取った値を尊重する（ライブラリが
グローバル設定を読まない原則）。
- `cli.py` の `ping`: `PingService(timeout=Config.PING_TIMEOUT)`（Config は既に import 済み）。
- `SchedulerService.__init__(self, app=None, timeout: Optional[float] = None)` を追加し、
  `timeout` 指定時のみ `PingService(timeout=timeout)`、未指定時は従来通り `PingService()`（既定4.0）。
- `cli.py` の `start_monitoring`: `SchedulerService(app=app, timeout=Config.PING_TIMEOUT)`。

### M3 (views/dashboard.py)
未使用の `desc, func` import 行を削除。

### E4 (views/dashboard.py)
`hours` の解析を小さなヘルパに集約:
```python
HOURS_MIN, HOURS_MAX, HOURS_DEFAULT = 1, 168, 24
def _parse_hours():
    """Return (hours, None) or (None, (json_error, 400)) for an invalid ?hours=."""
    raw = request.args.get("hours", HOURS_DEFAULT)
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "Invalid 'hours'; must be an integer"}), 400)
    return max(HOURS_MIN, min(hours, HOURS_MAX)), None
```
各ルート(`get_ping_data`/`get_ping_chart`/`get_stats`)冒頭で
`hours, error = _parse_hours(); if error: return error`。
`abc` → 400 JSON（既存の 400/404 と整合）、負値/範囲外 → [1,168] にクランプ。

## Alternatives considered / rejected
- **U2/U6 を純関数に抽出してテスト**: CliRunner + create_app monkeypatch で実コマンド出力を
  検証する方が受け入れシナリオ（"一覧表示すると見出しが…"）に忠実。プロダクションの構造を
  変えずに済むため採用。
- **SchedulerService が Config を直接 import**: サービス層→設定層の結合を生む。エントリポイント
  注入の方が E2 の指摘（ライブラリがグローバル設定をしない）と整合するため却下。
- **E4 で Flask の `type=int`**: 不正値が黙って既定値にフォールバックし 400 を返せない。明示
  ヘルパで 400 と範囲クランプを両立させる方が良い。

## Out of scope (このIssueでは触らない)
P1 SQL集計化 / U1 Plotly同梱 / U3 セキュリティ強化(0.0.0.0/CORS/debug) / M4 sqliteスレッド安全性 /
M1 ping・保存コア共通化 / E2 basicConfig / M7 utcnow非推奨。netdiag 側の M6 デッドコードも別パッケージ。

## Test strategy (受け入れシナリオ → テスト)
新規 `tests/test_network_check_tool_fixes.py`:
- **U2**: CliRunner で `status --host google.com`（create_app を temp-DB app に monkeypatch、1行seed）→
  出力に `"Recent ping results for google.com:"` を含む。
- **U6**: 同上で `response_time=0.0, packet_loss=False` を seed → 出力に `0.00ms` を含み `timeout` を含まない。
  併せて `packet_loss=True` 行は `timeout` 表示。
- **M2-a**: `SchedulerService(timeout=2.5).ping_service.timeout == 2.5`。
- **M2-b**: `ping --count 0` で services.PingService を fake に差し替え、`Config.PING_TIMEOUT` を
  monkeypatch した値が PingService に渡ることを確認。
- **M3**: `network_check_tool.views.dashboard` に `desc`/`func` 属性が無い。
- **E4**: test client で `/api/stats?hours=abc` → 400、`/api/stats?hours=-5` → 200（クランプ）。

## Version / changelog
- `pyproject.toml` 0.1.1 → 0.1.2（バグ修正バンドル, semver patch）。
- `CHANGELOG.md` を新規作成し v0.1.2 エントリを追加。

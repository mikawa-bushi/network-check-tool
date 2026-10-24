# network-check-tool — 生産技術者のためのネットワーク点検ツール

![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)
![PyPI](https://img.shields.io/pypi/v/network-check-tool)

ノートPCをラインのスイッチにつないで、機器の一覧（台帳）を渡すだけ。
**このPCのネットワーク**と**ラインの全機器（PLC・HMI・ロボット・センサ）**を数秒で点検し、
異常があれば「どこが・なぜ・次に何を確認するか」を日本語で示します。

```bash
pip install network-check-tool
netdiag check -f 台帳.csv          # このPC + 台帳の全機器を点検
```

- **気軽に** — 管理者権限不要・設定ファイル不要・オフラインで動作。Excelで作ったCSVの台帳をそのまま読めます。
  Windows / Linux / macOS 対応（日本語版Windowsの `ping` 表示もそのまま解析）。
- **高精度** — 応答時間はマイクロ秒精度、損失率は「95%上限」付きで表示（10回中0件のロスでも
  「28%以下」としか言えない、という事実を正直に示します）。遅延は中央値・p95・最大・ジッタで評価。
- **網羅的** — LANリンク品質（速度・全二重/半二重・CRCエラー）、IP設定の矛盾、ゲートウェイ、ARP、
  機器ごとの経路・応答・ARP・サービスポート、ネットワーク上の機器発見、長期監視まで1つのパッケージで。

同梱のコマンドは2つです。

| コマンド | 役割 |
|---|---|
| **`netdiag`** | その場の点検・原因の切り分け（標準ライブラリ + click のみ、オフライン） |
| **`network-checker`** | 長期監視（定期ping・Webダッシュボード・しきい値アラート・劣化時の自動診断） |

---

## インストール

```bash
pip install network-check-tool
# Windows で netdiag にパスが通っていない場合:
py -m pip install network-check-tool
py -m netdiag check
```

インターネットに出られない工場PCには、別のPCで部品をダウンロードして持ち込みます。

```bash
pip download network-check-tool -d wheels          # ネットにつながるPCで
pip install --no-index --find-links wheels network-check-tool   # 工場PCで
```

YAML形式の台帳・シナリオを使う場合のみ `pip install "network-check-tool[yaml]"`（CSV/JSON/TOMLは不要）。

## 使い方

### 1. このPCのネットワークを点検

```bash
netdiag check
```

LANケーブルのリンク速度・全二重/半二重・エラー、IPアドレス（169.254.x.x になっていないか、
同じネットワークが2つのNICにないか、ゲートウェイが複数ないか、IP重複していないか、マスクの誤り）、
ゲートウェイ、ARP表、PCのリソースを確認します。

> 設備ネットワークはインターネットにつながっていないのが普通なので、**インターネット接続の確認は既定では行いません**。
> 必要なときは `--internet` を付けてください（外部到達・DNS・IPv6を確認）。

### 2. 機器を点検

```bash
netdiag check 192.168.3.39                 # ping・ARP・経路
netdiag check 192.168.3.39:502             # + Modbus/TCP のポート
netdiag check PLC1=192.168.3.39:melsoft    # 名前付き・プロトコル名で指定
netdiag ping 192.168.3.39                  # pingの精密統計だけ
```

### 3. 台帳で一括点検（おすすめ）

```bash
netdiag scan --save 台帳.csv      # 同じネットワーク上の機器を一覧して台帳の下書きを作る
#   → Excelで開いて名前・ポートを書き足す
netdiag check -f 台帳.csv          # 全機器を一括点検
netdiag check -f 台帳.csv -o 点検結果.html   # 保存用レポート（.csv / .json も可）
```

台帳はExcelで「CSV（コンマ区切り）」保存したファイルがそのまま使えます（Shift_JIS / UTF-8 / UTF-16 対応）。
例: [`examples/devices.csv`](examples/devices.csv)

```csv
名前,IPアドレス,ポート,グループ,MACアドレス,備考
PLC-1,192.168.3.39,melsoft,1ライン,,メインPLC
HMI-1,192.168.3.40,vnc,1ライン,,
ロボット-1,192.168.3.41,,1ライン,,pingのみ確認
IoTゲートウェイ,192.168.3.50,modbus;opcua,共通,,
```

| 列（見出しは日本語・英語どちらでも） | 内容 |
|---|---|
| 名前 / 機器名 / name | 表示名 |
| IPアドレス / ホスト / host | IPアドレス（推奨）またはホスト名。`192.168.3.39:502` の形も可 |
| ポート / port | 確認するTCPポート。番号か名前を `;` 区切りで：`modbus`(502) `enip`(44818) `opcua`(4840) `s7`(102) `melsoft`(5007) `fins`(9600) `http` `https` `ssh` `telnet` `ftp` `vnc` `rdp` `smb` `mqtt` `iec104` `dnp3` |
| グループ / ライン | まとめ表示用 |
| MACアドレス / mac | 書いておくと、別の機器が応答したとき（機器交換・IP重複）に検出 |
| 遅延上限 / rtt_warn_ms、ロス上限 / loss_warn_pct | 機器ごとの判定しきい値 |
| 監視間隔 / interval | `network-checker` で監視するときの間隔[秒] |
| 備考 / note | メモ |

見出し行が無い単純なIPの一覧も読めます。YAML/JSON/TOML（`devices:` のリスト）も使えます。

### 4. 断続的な不調（ときどき通信エラーが出る）を調べる

```bash
netdiag check -f 台帳.csv --count 300        # 1台あたり300回（約1分）
netdiag check 192.168.3.39 --duration 600    # 10分間測り続ける
```

回数を増やすほど損失率の「95%上限」が下がり、「本当にロスが無い」と言える精度が上がります
（例: ロス0件で 1% 未満と言うには約380回）。測定中にPCのNICエラー（CRC等）が増えた場合は、
ケーブル・コネクタ・ノイズの問題として報告します。

### 表示例

```text
━━━ netdiag ネットワーク診断 ━━━  2026-10-21 14:03:12  PC: LINE1-ENG01
✘ 結論: 異常 — 機器 4台中 1台に異常（ロボット-1）

■ このPCのネットワーク
  ✔ [L2]     ネットワークアダプタ  イーサネット 192.168.3.100/24 (1 Gbps 全二重)
  …

■ 機器 4台
     名前             アドレス      応答   損失  95%上限  RTT中央/p95/最大(ms)  ポート           所見
  ✔  PLC-1            192.168.3.39  10/10  0%    ≤28%     0.40/1.06/1.20        5007 開
  ✔  HMI-1            192.168.3.40  0/10   100%           —                     5900 開          ping無応答・ポート正常
  ✘  ロボット-1       192.168.3.41  0/10   100%           —                     —                ARP応答なし
  ✔  IoTゲートウェイ  192.168.3.50  10/10  0%    ≤28%     0.35/0.56/0.61        502 開, 4840 開

■ 診断と次の確認
  ✘ ロボット-1 (192.168.3.41) — 192.168.3.41 の機器がネットワーク上に見つかりません（ARP応答なし）。  [確信度: 高]
      根拠: ping・ARPとも応答なし（応答 0/10・損失 100%）— 電源・ケーブル・機器のIP設定を確認
      1. 機器の電源とLANポートのリンクランプ、スイッチ側のランプを確認する
      2. LANケーブル・コネクタを挿し直す／交換する
      3. 機器側のIP設定が本当に 192.168.3.41 か確認する（機器のパネル/設定ツール）
      4. 同じスイッチにつながる他の機器に届くか確認し、スイッチ/VLANの問題を切り分ける
```

終了コードは `0` = 正常、`1` = 異常あり、`2` = 注意のみ。バッチやタスクスケジューラからも使えます。
英語で表示するには `--lang en`（または環境変数 `NETDIAG_LANG=en`）。

## 診断の考え方

- **下の層から順に**確認し、最も下で失敗した項目を「原因」、それより上で連動して失敗した項目を
  「結果」としてまとめます。機器ごとには **経路 → 応答（ping/ARP）→ ポート** の順です。
- **PCと機器のサブネット不一致**（例: PCが 192.168.1.x、PLCが 192.168.3.x）は、ケーブルや機器の
  故障ではなく設定の問題としてはっきり示し、設定すべきアドレスの例を出します。
- **ARP（同じLAN内で機器が必ず応答する仕組み）**で、「電源断・配線不良・IP誤り（ARP応答なし）」と
  「機器は存在するがpingに応答しない設定（ARP応答あり）」を区別します。判定には**測定中に得た新しい
  ARP情報だけ**を使い、PCに残っている古い記録で「存在する」と誤判定しないようにしています。
- **IP重複**は、1回のpingに複数の応答が返る・台帳のMACと違う機器が応答する・WindowsがIP競合を
  検出している、のいずれかで報告します。
- ポートは「開」「閉（機器は生きているが待ち受けていない）」「無応答（ファイアウォール/機器停止）」を区別します。
  pingにもTCPにも全く応答が無い場合は、ポートの問題ではなく機器・経路の問題として報告します。

## 設備への配慮

- pingの送信は全体で毎秒200回までに制限しています。
- TCPポートは、台帳や `-p` で**指定したものだけ**に接続し、すぐ切断します（接続数に上限のあるPLCを考慮）。
  `netdiag scan` は ping と ARP だけを使い、指定しない限りポートには触れません。
- 管理者権限は不要です。ping は OS が許可していれば権限不要のICMPソケット（マイクロ秒精度）、
  そうでなければ OS の `ping` コマンドで測定します。

## netdiag コマンド一覧

| コマンド | 内容 |
|---|---|
| `netdiag check [機器...] [-f 台帳] [-p ポート]` | このPCと機器を点検（`--count` `--duration` `--internet` `-o` `--format text/json/html/csv` `--all`） |
| `netdiag scan [ネットワーク]` | 同じネットワーク上の機器を一覧（`--save 台帳.csv`、`-p` でポートも確認） |
| `netdiag ping <host>` | pingの精密統計（損失の95%上限・中央値/p95/最大・ジッタ） |
| `netdiag explain [項目]` | 各チェックの内容と、同じことを手で確かめるコマンド |
| `netdiag scenario <file>` | YAML/JSONのシナリオ（このPCの確認項目 + 機器）で点検。例: [`examples/line1.yaml`](examples/line1.yaml) |
| `netdiag snapshot save/diff` | 正常時のネットワーク設定を保存し、「何が変わったか」を比較 |
| `netdiag peer serve/test` | 2台のPC間でLAN品質（ロス・ジッタ・スループット）を測定 |
| `netdiag watch` | 常時監視し、状態が変わったときだけ通知（閉じたネットワークでは `--no-internet`） |
| `netdiag status / history / prune / monitor / service` | 監視状態・履歴分析・履歴整理・systemdサービス化 |

詳しい設計と全オプションは [docs/netdiag.md](docs/netdiag.md) を参照してください。

---

## network-checker — 長期監視とダッシュボード

夜間だけ通信が切れる、といった問題には常設の監視が有効です。`netdiag` と同じ台帳がそのまま使えます。

```bash
network-checker start-monitoring --config 台帳.csv --diagnose   # 定期ping + 劣化時に自動診断
network-checker run-server                                       # http://localhost:9991
```

- 機器ごとの間隔・しきい値（台帳の `監視間隔` `遅延上限` `ロス上限`）で `DEGRADED`/`RECOVERED` アラートを
  JSONLに記録します。`--diagnose` を付けると、劣化した**その機器**について netdiag が原因
  （このPCのリンク断・サブネット不一致・ARP応答なし など）を調べてアラートに添付します。
- ダッシュボードは応答時間とパケットロスの推移を表示し、CSVエクスポート（`/api/export`）と
  Prometheus 形式（`/metrics`）も提供します。
- Docker での常設例: [docker/README.md](docker/README.md)

| コマンド | 内容 |
|---|---|
| `network-checker start-monitoring [hosts...] [--config FILE] [--diagnose] [--alert-file F]` | 監視を開始 |
| `network-checker run-server [--host H] [--port P]` | ダッシュボード（既定 127.0.0.1:9991） |
| `network-checker ping <host> [--count N] [--json]` | pingして監視DBに記録 |
| `network-checker status [--host H] [--json]` | 監視DBの最近の結果 |
| `network-checker init` / `clear` | DBの初期化 / 全削除 |

> **セキュリティ**: ダッシュボードとAPIには認証がありません。`run-server` は既定でこのPC（127.0.0.1）だけで
> 待ち受けます。ネットワークに公開する（`--host 0.0.0.0`）場合は、認証付きのリバースプロキシを前段に置いてください。
> `--debug` は 127.0.0.1 以外では起動を拒否します。他オリジンからの `/api/*` 利用は `CORS_ORIGINS` で明示的に許可した場合のみです。

主な環境変数: `DATABASE_URL`（既定 `sqlite:///network_check_tool.db`）、`PING_TIMEOUT`（4.0秒）、
`DEFAULT_PING_INTERVAL`（60秒）、`SCHEDULER_MAX_WORKERS`（4）、`SQLITE_BUSY_TIMEOUT_MS`（5000）、`CORS_ORIGINS`。

API: `GET /api/hosts`, `/api/stats?host=&hours=`, `/api/ping_data?host=&hours=`, `/api/export?host=&hours=&format=csv`,
`/metrics`、ホスト管理（ループバック起動時のみ）`GET /api/jobs`, `POST /api/hosts`, `DELETE /api/hosts/<host>`。

---

## English summary

**network-check-tool** helps production engineers check plant networks quickly and precisely:

- `netdiag check -f devices.csv` checks **this PC** (link speed/duplex/errors, IP-setting conflicts,
  gateway, ARP) and **every device on the line** (subnet/route, ping statistics with a 95 % upper
  bound on loss, median/p95/max latency and jitter, fresh ARP evidence that separates "device off"
  from "device ignores ping", IP conflicts, and service ports: open / closed / silent), then names
  the root cause and the next steps.
- Device lists are plain CSV saved from Excel (Japanese or English headers, Shift_JIS/UTF-8/UTF-16),
  YAML, JSON or TOML; ports can be numbers or protocol names (`modbus`, `enip`, `opcua`, `s7` …).
- Works offline and unprivileged on Windows (including Japanese-locale output), Linux and macOS;
  `netdiag` needs only the standard library and `click`. Closed networks are the default — add
  `--internet` to also check external reachability and DNS. Output in Japanese by default;
  `--lang en` or `NETDIAG_LANG=en` for English. Reports: text, JSON, self-contained HTML, CSV.
- `netdiag scan` discovers devices on a subnet (ICMP + ARP only); `network-checker` adds long-term
  monitoring with a dashboard, threshold alerts and automatic per-device diagnosis.

Exit codes: `0` healthy, `1` problem found, `2` warnings only. See [docs/netdiag.md](docs/netdiag.md).

## 開発 / Development

```bash
git clone https://github.com/mikawa-bushi/network-check-tool.git
cd network-check-tool
poetry install --all-extras
poetry run pytest
```

貢献の手順は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。

## License

MIT — see [LICENSE](LICENSE).

## Support

- 🐛 [Issue Tracker](https://github.com/mikawa-bushi/network-check-tool/issues)
- 💬 [Discussions](https://github.com/mikawa-bushi/network-check-tool/discussions)

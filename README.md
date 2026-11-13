# network-check-tool — 生産技術者のためのネットワーク点検ツール

![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)
![PyPI](https://img.shields.io/pypi/v/network-check-tool)

ノートPCをラインのスイッチにつないで、機器の一覧（台帳）を渡すだけ。
**このPCのネットワーク**と**ラインの全機器（PLC・HMI・ロボット・センサ）**を数秒で点検し、
異常があれば「どこが・なぜ・次に何を確認するか」を日本語で示します。

```bash
pip install network-check-tool
netdiag                            # このPCを点検し、次に試すコマンドも表示（まずはこれ）
netdiag check -f 台帳.csv          # このPC + 台帳の全機器を点検
```

- **気軽に** — 管理者権限不要・設定ファイル不要・オフラインで動作。Excelで作ったCSVの台帳をそのまま読めます。
  Windows / Linux / macOS 対応（日本語版Windowsの `ping` 表示もそのまま解析）。
- **高精度** — 応答時間はマイクロ秒精度、損失率は「95%上限」付きで表示（10回中0件のロスでも
  「28%以下」としか言えない、という事実を正直に示します）。遅延は中央値・p95・最大・ジッタで評価。
- **網羅的** — LANリンク品質（速度・全二重/半二重・CRCエラー）、IP設定の矛盾、ゲートウェイ、ARP、
  機器ごとの経路・応答・ARP・サービスポート、ネットワーク上の機器発見、数時間の瞬断記録まで1つのコマンドで。

コマンドは `netdiag` ひとつで、標準ライブラリと click だけで動きます。

> 定期pingのWebダッシュボード `network-checker` は削除しました。最後に含んでいた版は 0.5.0 です
> （`pip install "network-check-tool==0.5.0"`）。

---

## インストール

### Windows: exe 版（Python 不要・インストール不要）

[Releases](https://github.com/mikawa-bushi/network-check-tool/releases) の
`netdiag-windows-x64-<版>.zip` を展開して使います（管理者権限不要）。

- `netdiag.exe` を**ダブルクリック** → このPCを点検し、結果のHTMLレポートを exe の隣に保存してブラウザで開きます。
- 台帳（Excelで保存したCSV）を `netdiag.exe` に**ドラッグ＆ドロップ** → 台帳の全機器を点検し、
  台帳の隣に `<台帳名>_点検結果_日時.html` を保存して開きます。
- コマンドプロンプトからは `netdiag.exe check -f 台帳.csv` など、下記の `netdiag` と同じように使えます。

署名なしの exe のため、初回に SmartScreen の確認が出ることがあります（「詳細情報」→「実行」）。

### pip（Windows / Linux / macOS）

```bash
pip install network-check-tool
# Windows で netdiag にパスが通っていない場合:
py -m pip install network-check-tool
py -m netdiag
```

> パッケージ名は `network-check-tool` です（コマンド名は `netdiag`）。PyPI の `netdiag` は
> 無関係の別プロジェクトで、`pip install netdiag` ではこのツールは入りません。

インターネットに出られない工場PCには、別のPCで部品をダウンロードして持ち込みます。
部品はすべてOS・Pythonのバージョンを問わないファイルなので、ダウンロードするPCのOSや
Pythonのバージョンが工場PCと違ってもかまいません。ただし、どちらのPCも Python 3.11 以上にしてください。
ダウンロードするPCが 3.10 だと、古い 0.5.0 がダウンロードされます。

```bash
pip download network-check-tool -d wheels                       # ネットにつながるPCで
pip install --no-index --find-links wheels network-check-tool   # 工場PCで
```

YAML形式の台帳・シナリオを使う場合のみ `pip install "network-check-tool[yaml]"`（CSV/JSON/TOMLは不要）。

## 使い方

### 1. このPCのネットワークを点検

```bash
netdiag            # netdiag check と同じ点検のあと、次に試すコマンドを表示
netdiag check      # 点検だけ
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
例: [`examples/devices.csv`](https://github.com/mikawa-bushi/network-check-tool/blob/main/examples/devices.csv)

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

### 5. 瞬断を捕まえる（長時間の連続監視）

```bash
netdiag soak -f 台帳.csv --duration 2h -o 結果.html   # 2時間、全機器を 0.2 秒間隔で監視
netdiag soak 192.168.3.39 --duration 8h --format csv -o log.csv
```

「ときどき通信が切れる」を、**いつ・何秒・どの機器が**切れたかの時系列にします。
全機器が同時に切れたら「このPC側（ケーブル・NIC・PC直近のスイッチ）」、一部の機器だけなら
「その機器群が共有するスイッチ・配線」、1台だけなら「その機器・ケーブル」と切り分けます。
PCのNICエラーの増加（Linux ではリンク断の回数も）を同じ時間軸に並べて表示します。
HTMLレポートでは機器×時間のヒートマップで、途切れた時刻がひと目で分かります。
監視中は途切れた／戻った瞬間を画面に表示し、Ctrl+C で止めてもそこまでの結果を保存します。

### 6. 不具合を報告する（生データの収集）

```bash
netdiag collect --target 192.168.3.39     # → netdiag-collect-日時.zip
```

netdiag が使うOSコマンド（ping・arp・route・PowerShell など）の**生の出力**と netdiag の解釈結果を
zip にまとめます。IPアドレス・MAC・PC名・ユーザー名・SSID・ドメインは既定で伏せます
（サブネット構造とMACのメーカー部分は残すので解析に使えます）。送る前に中身を確認してください。

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
- 管理者権限は不要です。ping は Linux/macOS では権限不要のICMPソケット、Windows では
  OS の ICMP API（`IcmpSendEcho`）で送信し、いずれも高分解能タイマーで 1ms 未満まで測ります。
  どちらも使えない環境では OS の `ping` コマンドで測定します。

## netdiag コマンド一覧

| コマンド | 内容 |
|---|---|
| `netdiag` | このPCを点検（`netdiag check` と同じ）し、次に試すコマンドを表示 |
| `netdiag check [機器...] [-f 台帳] [-p ポート]` | このPCと機器を点検（`--count` `--duration` `--internet` `-o` `--format text/json/html/csv` `--all`） |
| `netdiag scan [ネットワーク]` | 同じネットワーク上の機器を一覧（`--save 台帳.csv`、`-p` でポートも確認） |
| `netdiag ping <host>` | pingの精密統計（損失の95%上限・中央値/p95/最大・ジッタ） |
| `netdiag soak [機器...] [-f 台帳] --duration 2h` | 長時間の連続監視。瞬断の時刻・長さ・範囲（全機器/一部/1台）とPC側のリンク断を時系列で（`--format text/json/html/csv` `-o`） |
| `netdiag collect [--target IP]` | 不具合報告用に、OSコマンドの生出力と解釈結果をzipに収集（既定でIP・MAC等を伏せる） |
| `netdiag explain [項目]` | チェックの一覧。項目を指定すると、その内容と同じことを手で確かめるコマンド |
| `netdiag scenario <file>` | YAML/JSONのシナリオ（このPCの確認項目 + 機器）で点検。例: [`examples/line1.yaml`](https://github.com/mikawa-bushi/network-check-tool/blob/main/examples/line1.yaml) |
| `netdiag snapshot save/diff` | 正常時のネットワーク設定を保存し、「何が変わったか」を比較 |
| `netdiag peer serve/test` | 2台のPC間でLAN品質（ロス・ジッタ・スループット）を測定 |
| `netdiag watch` | 常時監視し、状態が変わったときだけ通知。毎回の結果を履歴DBに記録（閉じたネットワークでは `--no-internet`） |
| `netdiag status / history / prune / service` | 監視状態・履歴分析・履歴整理・systemdサービス化 |

詳しい設計と全オプションは [docs/netdiag.md](https://github.com/mikawa-bushi/network-check-tool/blob/main/docs/netdiag.md) を参照してください。

---

## English summary

**network-check-tool** helps production engineers check plant networks quickly and precisely:

- `netdiag` alone checks this PC and then suggests the next commands to try.
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
- `netdiag soak` watches devices for hours and turns "it drops sometimes" into a timeline: when,
  for how long, and whether all devices, some or one dropped together — next to this PC's NIC
  errors (and link drops on Linux) (HTML heatmap, CSV, JSON).
- `netdiag collect` zips the raw output of every OS command netdiag relies on, with IPs, MACs,
  host/user names, SSIDs and domains masked, for bug reports.
- Windows: a single `netdiag.exe` (no Python): double-click to check this PC, or drop a device
  list on it to check the line; an HTML report opens in the browser. Precise pings use the
  Windows ICMP API (no admin rights).
- `netdiag scan` discovers devices on a subnet (ICMP + ARP only).
- Install with `pip install network-check-tool`. The command is `netdiag`, but the
  `netdiag` package on PyPI is an unrelated project.
- Offline install: run `pip download network-check-tool -d wheels` on any PC with
  internet access, then `pip install --no-index --find-links wheels network-check-tool` on the
  plant PC. Every wheel is pure Python, so the two PCs' OS and Python version need not match,
  but both need Python 3.11 or newer (pip on 3.10 downloads 0.5.0).
- The `network-checker` ping dashboard is removed. The last release with it is 0.5.0
  (`pip install "network-check-tool==0.5.0"`).

Exit codes: `0` healthy, `1` problem found, `2` warnings only. See [docs/netdiag.md](https://github.com/mikawa-bushi/network-check-tool/blob/main/docs/netdiag.md).

## 開発 / Development

```bash
git clone https://github.com/mikawa-bushi/network-check-tool.git
cd network-check-tool
poetry install --all-extras
poetry run pytest
```

貢献の手順は [CONTRIBUTING.md](https://github.com/mikawa-bushi/network-check-tool/blob/main/CONTRIBUTING.md) を参照してください。

## License

MIT — see [LICENSE](https://github.com/mikawa-bushi/network-check-tool/blob/main/LICENSE).

## Support

- 🐛 [Issue Tracker](https://github.com/mikawa-bushi/network-check-tool/issues)
- 💬 [Discussions](https://github.com/mikawa-bushi/network-check-tool/discussions)

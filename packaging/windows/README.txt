netdiag（ネットワーク点検ツール）Windows 版
==========================================

インストール不要・管理者権限不要です。

■ このPCのネットワークを点検する
  netdiag.exe をダブルクリックします。
  結果が画面に表示され、同じフォルダーに「このPC_点検結果_日時.html」が
  保存されてブラウザーで開きます。

■ ラインの機器（PLC・HMI・ロボットなど）をまとめて点検する
  1. devices_sample.csv を Excel で開き、名前・IPアドレス・ポートなどを書き換えて
     CSV で保存します（列名は日本語のままで構いません）。
  2. 保存した CSV ファイルを netdiag.exe の上にドラッグ＆ドロップします。
  3. CSV と同じフォルダーに「<CSV名>_点検結果_日時.html」が保存されて開きます。
  ポート欄には番号のほか melsoft / modbus / enip / opcua / s7 / fins / http / vnc
  などの名前を書けます（複数は ; 区切り）。

■ 結果の見方
  OK = 問題なし / 注意 = 様子見・要確認 / 異常 = 対処が必要。
  異常・注意の項目には「原因の見立て」と「次に確認すること」が表示されます。
  レポート（html）はそのままメールやチャットで共有できます。

■ 詳しい使い方（コマンドプロンプト / PowerShell から）
  netdiag.exe check -f 台帳.csv --count 300     … 300回pingして散発的なロスも検出
  netdiag.exe soak -f 台帳.csv --duration 2h    … 2時間の連続監視（瞬断の時刻と範囲）
  netdiag.exe ping 192.168.3.39                 … 高精度ping（RTT分布・ロス率）
  netdiag.exe collect                           … 不具合報告用の生データをzipで収集
  netdiag.exe --help                            … すべてのコマンド

■ 注意
  署名なしの exe のため、初回に「Windows によって PC が保護されました」と
  表示されることがあります。「詳細情報」→「実行」で起動できます。
  会社のウイルス対策ソフトで止められる場合は、情報システム部門に確認してください。

詳しくは https://github.com/mikawa-bushi/network-check-tool をご覧ください。


netdiag (network check) for Windows — English
---------------------------------------------
No install, no administrator rights needed.

* Double-click netdiag.exe: checks this PC; an HTML report is saved next to the
  exe and opened in the browser.
* Drop a device list (CSV saved from Excel; see devices_sample.csv) onto
  netdiag.exe: every device in it is checked; the report is saved next to it.
* From a terminal: "netdiag.exe --lang en --help" lists every command.
* The exe is not code-signed: SmartScreen may ask "More info" -> "Run anyway".

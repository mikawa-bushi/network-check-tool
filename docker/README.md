# Docker で network-checker を動かす

常設の監視用PC（Linux）で、ダッシュボードと定期pingをコンテナとして動かす例です。
ふだんの点検（`netdiag check`）はコンテナ不要で、PCに `pip install` するだけで使えます。

## 使い方

```bash
# 1. 監視する機器の台帳を用意（Excelで編集したCSVでOK）
netdiag scan --save docker/devices.csv     # 同じネットワークの機器を一覧して保存
#    → 名前を書き足す・不要な行を消す

# 2. 起動
docker compose -f docker/docker-compose.yml up -d --build

# 3. ブラウザで http://127.0.0.1:9991 を開く
```

## 注意

- **`network_mode: host` が必須です。** ブリッジネットワークのコンテナ内では、ping や ARP は
  コンテナの仮想ネットワークを見てしまい、工場のネットワークを正しく測れません。
- ダッシュボードには**認証がありません**。既定どおりホストの 127.0.0.1 だけで待ち受けるか、
  認証付きのリバースプロキシを前段に置いてください。
- 履歴は `nct-data` ボリューム（`/data/network_check_tool.db`）に保存されます。
- コンテナ内の非root ping は、ホストの `net.ipv4.ping_group_range` が許可していれば
  ICMPソケット（高精度）、そうでなければ `ping` コマンドで測定します。

## Running with Docker (English)

`docker compose -f docker/docker-compose.yml up -d --build` starts the dashboard
(`http://127.0.0.1:9991`) and a monitor that pings every device listed in
`docker/devices.csv`. Host networking is required for meaningful measurements,
and the dashboard has no authentication — keep it on loopback or behind an
authenticating reverse proxy.

# 【2025年最新版】CSVからWebアプリが作れる！Monitor App完全ガイド

## はじめに

こんにちは！今回は、**手持ちのCSVファイルをWeb UI付きのデータベースアプリに一瞬で変換**できる「Monitor App」の使い方をご紹介します。

**「Excel管理から卒業したい製造業の方」「データベースは難しそうで手が出ない初心者」「プログラミング未経験だけどWebアプリを作りたい方」**に最適です。

### なぜCSVなのか？

多くの製造業や中小企業では、**データ管理といえばExcelやCSV**です。しかし、以下のような課題を抱えていませんか？

- 📊 **複数人で同じファイルを編集して競合が発生**
- 🔍 **データが増えすぎて検索・集計が大変**
- 📱 **スマホやタブレットから確認できない**
- 🚨 **リアルタイムでの状況把握が困難**

Monitor Appは、**既存のCSVデータをそのまま活用**して、これらの問題を一気に解決します。データベースの知識は一切不要！**CSVがあればすぐに本格的なWebアプリが完成**します。

## Monitor Appとは？

Monitor Appは、**製造業やWeb開発初心者向け**に開発されたPythonライブラリです：

- 🚀 **手持ちのCSVファイルを置くだけで、WebアプリとREST APIが自動生成**
- 📊 **SQLite（開発）→MySQL/PostgreSQL（本格運用）への移行も簡単**
- 🔧 **設定ファイル1つで高度なカスタマイズ（SQL知識があれば無限に拡張可能）**
- 📱 **スマホ・タブレット対応のレスポンシブUI**
- 📖 **Swagger UI付きのAPI自動ドキュメント**
- 🎨 **製造現場に最適な条件付きセルスタイリング機能**
- ⏰ **リアルタイム更新機能（2秒間隔）**

### 🏭 製造業での活用メリット

- **在庫管理**: CSVの在庫データが自動更新されるモニター画面に
- **品質管理**: 検査結果をリアルタイムで色分け表示
- **生産管理**: 生産計画と実績を一画面で把握
- **設備管理**: 設備の稼働状況を視覚的にモニタリング

## インストール方法

たった1行でインストール完了！

```bash
pip install monitor-app
```

## 基本的な使い方

### ステップ1: 新しいプロジェクトを作成

```bash
monitor-app startproject my_data_app
cd my_data_app
```

この時点で、以下のような構造のプロジェクトが作成されます：

```
my_data_app/
├── monitor_app/
│   ├── app.py           # メインアプリケーション
│   ├── config/
│   │   └── config.py    # 設定ファイル（カスタマイズはここ）
│   ├── csv/             # CSVデータを配置
│   │   ├── users.csv
│   │   ├── products.csv
│   │   └── orders.csv
│   ├── templates/       # HTMLテンプレート
│   ├── static/          # CSS, JS, 画像
│   └── instances/       # SQLiteデータベース保存先
└── pyproject.toml
```

### ステップ2: 手持ちのCSVファイルを活用しよう！

**重要：サンプルファイルではなく、あなたの実際のCSVデータを使うことを強く推奨します！**

#### 🔄 既存CSVファイルの置き換え方法

1. `csv/` フォルダ内のサンプルファイルを削除
2. あなたのCSVファイルをコピー（ファイル名は自由）
3. 設定ファイルで対応（後述）

#### 📋 製造業でよくあるCSVパターン例

**在庫管理.csv**
```csv
id,part_number,part_name,stock_quantity,reorder_point,supplier
1,A001,ボルト M6,150,50,田中商事
2,A002,ナット M6,120,30,田中商事
3,B001,ワッシャー 6mm,80,100,山田部品
```

**設備稼働.csv**
```csv
id,machine_name,status,temperature,run_time,last_maintenance
1,プレス機1,稼働中,45.2,120,2024-12-15
2,プレス機2,停止中,25.1,0,2024-12-10
3,切断機A,稼働中,38.7,95,2024-12-20
```

**品質検査.csv**
```csv
id,product_code,inspection_date,result,defect_rate,inspector
1,P001,2025-01-10,合格,0.1,佐藤
2,P002,2025-01-10,不合格,5.2,田中
3,P001,2025-01-11,合格,0.3,佐藤
```

**💡 ポイント**: idカラムがあると便利ですが、なくても自動で追加されます！

### ステップ3: アプリケーションを起動

```bash
python monitor_app/app.py runserver --csv
```

これで `http://127.0.0.1:9990` にアクセスすると、WebアプリケーションとAPIが利用できます！

## 高度な機能の活用

### 1. API自動ドキュメント

`http://127.0.0.1:9990/docs` にアクセスすると、**FastAPI風のSwagger UI**でAPI仕様を確認できます。

![](https://i.imgur.com/swagger-example.png)

### 2. 設定ファイルのカスタマイズ

`config/config.py` で様々な設定をカスタマイズできます：

#### 2-1. データベース設定

```python
# SQLite（デフォルト）
DB_TYPE = "sqlite"

# MySQL
DB_TYPE = "mysql"
MYSQL_USER = "root"
MYSQL_PASSWORD = "password"
MYSQL_HOST = "localhost"
MYSQL_PORT = "3306"
MYSQL_DB = "monitor_app"

# PostgreSQL
DB_TYPE = "postgresql"
POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "password"
POSTGRES_HOST = "localhost"
POSTGRES_PORT = "5432"
POSTGRES_DB = "monitor_app"
```

#### 2-2. テーブル設定の分離設計

Monitor Appの最大の特徴は、**CRUD操作用**と**表示用**の設定を分離していることです：

**ALLOWED_TABLES（CRUD操作用）**
```python
ALLOWED_TABLES = {
    "users": {
        "columns": ["id", "name", "email"],
        "primary_key": "id"
    },
    "products": {
        "columns": ["id", "name", "price"], 
        "primary_key": "id"
    },
    "orders": {
        "columns": ["id", "user_id", "product_id", "amount"],
        "primary_key": "id",
        "foreign_keys": {"user_id": "users.id", "product_id": "products.id"}
    }
}
```

**VIEW_TABLES（表示用）**
```python
VIEW_TABLES = {
    "users_view": {
        "query": "SELECT id, name, email FROM users",
        "title": "ユーザー一覧"
    },
    "products_view": {
        "query": "SELECT id, name, price FROM products", 
        "title": "商品一覧"
    },
    "orders_summary": {
        "query": """
            SELECT 
                orders.id, 
                users.name AS user_name, 
                products.name AS product_name, 
                orders.amount
            FROM orders
            JOIN users ON orders.user_id = users.id
            JOIN products ON orders.product_id = products.id
        """,
        "title": "注文サマリー"
    }
}
```

この分離により、**データの整合性を保ちながら柔軟な表示**が実現できます。

### 3. 条件付きスタイリング機能

**TABLE_CELL_STYLES**で、セルの値に応じて動的にスタイルを変更できます：

```python
TABLE_CELL_STYLES = {
    "products_view": {
        "price": {
            # 1000以上は青背景（高価格商品）
            "greater_than": {"value": 1000, "class": "bg-primary text-white"},
            # 500未満は黄背景（低価格商品）
            "less_than": {"value": 500, "class": "bg-warning text-dark"},
            "width": "20%",
            "align": "right",
            "bold": True
        }
    },
    "orders_summary": {
        "amount": {
            # 大量注文は赤でハイライト
            "greater_than": {"value": 10, "class": "bg-danger text-white"},
            # 少量注文は注意喚起
            "less_than": {"value": 3, "class": "bg-info text-dark"},
            "font_size": "24px",
            "align": "center"
        }
    }
}
```

これにより、**データの重要度や状態を視覚的に**表現できます。

### 4. REST API の活用

Monitor AppはフルCRUD対応のREST APIを提供します：

```bash
# ユーザー一覧取得
curl -X GET http://localhost:9990/api/users

# 新しいユーザー作成
curl -X POST http://localhost:9990/api/users \
  -H "Content-Type: application/json" \
  -d '{"name": "新規ユーザー", "email": "new@example.com"}'

# ユーザー情報更新
curl -X PUT http://localhost:9990/api/users/1 \
  -H "Content-Type: application/json" \
  -d '{"name": "更新ユーザー", "email": "updated@example.com"}'

# ユーザー削除
curl -X DELETE http://localhost:9990/api/users/1

# スタイル付きビューデータ取得
curl -X GET http://localhost:9990/api/view/orders_summary
```

## 🏭 製造業での実践的な活用例

### 活用例1: リアルタイム在庫モニターシステム

製造現場でよくある「部品切れによる生産停止」を防ぐシステムです：

```python
# VIEW_TABLESで在庫状況を可視化
VIEW_TABLES = {
    "inventory_monitor": {
        "query": """
            SELECT 
                part_number as '部品番号',
                part_name as '部品名',
                stock_quantity as '現在庫数',
                reorder_point as '発注点',
                supplier as '仕入先',
                CASE 
                    WHEN stock_quantity <= reorder_point * 0.5 THEN '緊急補充'
                    WHEN stock_quantity <= reorder_point THEN '要発注'
                    WHEN stock_quantity <= reorder_point * 2 THEN '注意'
                    ELSE '充分'
                END as status,
                ROUND((stock_quantity * 1.0 / reorder_point * 100), 1) as '在庫率(%)'
            FROM inventory
            ORDER BY 
                CASE status
                    WHEN '緊急補充' THEN 1
                    WHEN '要発注' THEN 2  
                    WHEN '注意' THEN 3
                    ELSE 4
                END,
                stock_quantity ASC
        """,
        "title": "📦 リアルタイム在庫モニター",
        "description": "部品在庫を常時監視し、発注タイミングを見逃しません"
    }
}

# 製造現場に最適な視覚的アラート
TABLE_CELL_STYLES = {
    "inventory_monitor": {
        "status": {
            "equal_to": [
                {"value": "緊急補充", "class": "bg-danger text-white"},
                {"value": "要発注", "class": "bg-warning text-dark"},
                {"value": "注意", "class": "bg-info text-dark"},
                {"value": "充分", "class": "bg-success text-white"}
            ],
            "font_size": "20px",
            "bold": True
        },
        "現在庫数": {
            "less_than": {"value": 10, "class": "bg-danger text-white"},
            "greater_than": {"value": 100, "class": "bg-success text-white"},
            "align": "center",
            "font_size": "18px"
        }
    }
}
```

### 活用例2: 設備稼働状況ダッシュボード

設備の異常を即座に発見できるシステムです：

```python
VIEW_TABLES = {
    "machine_dashboard": {
        "query": """
            SELECT 
                machine_name as '設備名',
                status as 'ステータス',
                temperature as '温度(℃)',
                run_time as '稼働時間(分)',
                CASE 
                    WHEN temperature > 60 THEN '高温注意'
                    WHEN temperature > 50 THEN '要監視'
                    ELSE '正常'
                END as '温度状態',
                CASE
                    WHEN run_time > 480 THEN '長時間稼働'
                    WHEN run_time > 240 THEN '連続稼働中'
                    ELSE '通常'
                END as '稼働状態',
                last_maintenance as '最終メンテ'
            FROM equipment
            ORDER BY 
                CASE status WHEN '異常' THEN 1 WHEN '要点検' THEN 2 WHEN '稼働中' THEN 3 ELSE 4 END,
                temperature DESC
        """,
        "title": "⚙️ 設備稼働状況ダッシュボード", 
        "description": "全設備の稼働状況と異常を一画面で監視"
    }
}

# 製造現場の安全管理に重要な色分け
TABLE_CELL_STYLES = {
    "machine_dashboard": {
        "ステータス": {
            "equal_to": [
                {"value": "異常", "class": "bg-danger text-white"},
                {"value": "要点検", "class": "bg-warning text-dark"},
                {"value": "稼働中", "class": "bg-success text-white"},
                {"value": "停止中", "class": "bg-secondary text-white"}
            ]
        },
        "温度(℃)": {
            "greater_than": {"value": 60, "class": "bg-danger text-white"},
            "greater_than": {"value": 50, "class": "bg-warning text-dark"},
            "align": "center"
        }
    }
}
```

### 活用例3: 品質管理モニター

```python
VIEW_TABLES = {
    "quality_monitor": {
        "query": """
            SELECT 
                product_code as '製品コード',
                inspection_date as '検査日',
                result as '判定',
                defect_rate as '不良率(%)',
                inspector as '検査員',
                CASE 
                    WHEN defect_rate > 3.0 THEN '要改善'
                    WHEN defect_rate > 1.0 THEN '注意'
                    ELSE '良好'
                END as '品質状態'
            FROM quality_inspection
            ORDER BY inspection_date DESC, defect_rate DESC
        """,
        "title": "🔍 品質管理モニター"
    }
}
```

## 🎓 SQL学習で無限にカスタマイズ

**SQLの知識を身につけることで、Monitor Appの可能性は無限大に広がります！**

### 初心者向けSQL学習のメリット

- **複雑な集計が可能**: 月次売上、部門別実績、トレンド分析など
- **多テーブル結合**: 顧客情報と注文履歴を組み合わせた分析
- **条件分岐**: CASE文で業務ロジックを表現
- **関数活用**: 日付操作、文字列処理、数値計算

### SQL学習により実現できる高度な例

```sql
-- 月次売上トレンド（過去12ヶ月）
SELECT 
    strftime('%Y-%m', order_date) as '年月',
    COUNT(*) as '注文件数',
    SUM(amount * price) as '売上金額',
    AVG(amount * price) as '平均単価'
FROM orders o
JOIN products p ON o.product_id = p.id
WHERE order_date >= date('now', '-12 months')
GROUP BY strftime('%Y-%m', order_date)
ORDER BY '年月' DESC

-- 設備効率分析（稼働率・故障頻度）
SELECT 
    machine_name,
    COUNT(*) as '総稼働日数',
    SUM(CASE WHEN status = '稼働中' THEN 1 ELSE 0 END) as '正常稼働日数',
    ROUND(SUM(CASE WHEN status = '稼働中' THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 2) as '稼働率(%)',
    SUM(CASE WHEN status = '異常' THEN 1 ELSE 0 END) as '故障回数'
FROM equipment_log
WHERE log_date >= date('now', '-30 days')
GROUP BY machine_name
ORDER BY '稼働率(%)' DESC
```

## テスト機能

Monitor Appには包括的なテストスイートが含まれています：

```bash
# 全テストの実行
python -m pytest tests/test_api.py -v

# 特定機能のテスト
python -m pytest tests/test_api.py::TestUsersAPI -v
```

## まとめ

Monitor Appを使うことで：

- ✅ **手持ちのCSV→リアルタイムWebアプリ化が数分で完了**
- ✅ **データベース知識不要でREST API自動生成**
- ✅ **SQL学習により無限カスタマイズが可能**
- ✅ **製造現場に最適な条件付きスタイリング**
- ✅ **SQLite→MySQL/PostgreSQLへの本格移行も簡単**
- ✅ **複数人でのリアルタイム情報共有が実現**

**製造業の現場改善**、**Excel管理からの脱却**、**データベース入門**に最適なツールです！

### 🚀 こんな方におすすめ

- **製造業でExcel管理に限界を感じている方**
- **CSVデータは持っているがWebアプリ化したい方**  
- **データベースは難しそうで手が出ない初心者**
- **プログラミング未経験だが業務効率化したい方**
- **SQL学習のきっかけが欲しい方**

## リンク

- 📦 **PyPI**: https://pypi.org/project/monitor-app/
- 🔗 **GitHub**: https://github.com/hardwork9047/monitor-app
- 📖 **詳細ドキュメント**: リポジトリのREADME.md参照

ぜひ試してみて、フィードバックをお聞かせください！🚀

## タグ

#Python #Flask #WebApp #CSV #データベース #REST API #Bootstrap #SQLite #MySQL #PostgreSQL #製造業 #DX #業務効率化 #Excel #在庫管理
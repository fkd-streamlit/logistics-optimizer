# 🏭 物流 × AGV 量子インスパイア最適化アプリ v3

> 複数トラック動的迂回（焼きなまし法 SA） ＋ 工場内 AGV 5台 リアルタイムシミュレーション

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://logistics-optimizer-mgm6atk2cfmmlxibo3xhhb.streamlit.app/)

---

## 概要

本アプリは **量子アニーリングにインスパイアされた焼きなまし法（Simulated Annealing: SA）** を用いて、以下の2つの最適化問題をリアルタイムでシミュレーションします。

| 機能 | 内容 |
|------|------|
| **グローバル物流ルート最適化** | 複数トラックが走行中に突発的な通行止めが発生し、各トラックが SA で個別に最適迂回ルートを再計算 |
| **工場内 AGV シミュレーション** | 5台の AGV が工場フロアを自律走行し、SA ベースの目的地選択と衝突回避を実現 |

---

## 画面構成

```
┌─────────────────────────┬─────────────────────────┐
│  左パネル               │  右パネル               │
│  複数トラック           │  工場内 AGV             │
│  動的迂回シミュレーション│  シミュレーション       │
│  （folium 地図）        │  （Canvas アニメーション）│
└─────────────────────────┴─────────────────────────┘
         ↑ サイドバーで全パラメータを設定
```

---

## 機能詳細

### 左パネル：複数トラック動的迂回

- **3台のトラック**が同一ルートを異なる位置から同時出発
- 設定した tick タイミングで**ルート上にランダムに通行止め（2点）が発生**
- 各トラックが**現在地から目的地**までの迂回ルートを SA で個別計算
- 迂回候補は OSRM/ORS API で取得し、通行止め円を回避するルートを選択
- folium（CartoDB DarkMatter）でルートをリアルタイム表示

### 右パネル：工場内 AGV シミュレーション

- 7つのステーション（入荷・検品・組立・品質検査・出荷・充電）を配置
- AGV 5台が **SA ベースの目的地選択**で次の搬送先を決定
- **衝突回避**：30px 以内に接近した場合、後着 AGV が待機
- Canvas による工場フロア描画（搬送路レーン・棚・壁）
- **純 JavaScript アニメーション**（requestAnimationFrame）で動作、Python 側のメモリを消費しない

---

## 技術スタック

| カテゴリ | 使用技術 |
|---------|---------|
| フレームワーク | Streamlit 1.57 |
| 地図描画 | Folium + CartoDB DarkMatter |
| ルーティング API | OpenRouteService (ORS) / OSRM（フォールバック） |
| 最適化アルゴリズム | 焼きなまし法（Simulated Annealing） |
| AGV アニメーション | HTML Canvas + JavaScript (requestAnimationFrame) |
| ジオコーディング | Geopy (Nominatim) |
| 数値計算 | NumPy / GeoPy |

---

## セットアップ

### 必要環境

- Python 3.10 以上
- インターネット接続（OSRM / ORS API へのアクセス）

### インストール

```bash
git clone https://github.com/fkd-streamlit/logistics-optimizer.git
cd logistics-optimizer
pip install -r requirements.txt
```

### 起動

```bash
streamlit run logistics_app_v2.py
```

---

## 依存ライブラリ

```
streamlit>=1.35.0
folium>=0.16.0
geopy>=2.4.0
requests>=2.31.0
pandas>=2.0.0
numpy>=1.26.0
matplotlib>=3.8.0
streamlit-autorefresh>=1.0.0
urllib3>=2.0.0
```

---

## 使い方

### 1. ルート計算（左パネル）

| 手順 | 操作 |
|------|------|
| ① | サイドバーで輸送元・輸送先を入力 |
| ② | 通行止めの影響半径・発生タイミングを設定 |
| ③ | 「**ルート準備**」ボタンをクリック |
| ④ | 「**開始**」ボタンをクリックしてシミュレーション開始 |
| ⑤ | 設定 tick になると通行止めが発生し、各トラックが迂回ルートを自動計算 |

### 2. AGV シミュレーション（右パネル）

| 手順 | 操作 |
|------|------|
| ① | Canvas 内の「**開始**」ボタンをクリック |
| ② | 5台の AGV が自律走行を開始 |
| ③ | 「**停止**」で一時停止、「**リセット**」で初期状態に戻る |

### ステータス表示

| 色 | 状態 |
|----|------|
| 🟢 緑 | 走行中 |
| 🟡 黄 | ステーションで停車中 |
| 🔴 赤 | 衝突回避待機中 |

---

## サイドバー設定パラメータ

| パラメータ | 説明 | デフォルト |
|-----------|------|-----------|
| ORS API Key | OpenRouteService の API キー（任意） | 空欄（OSRM 使用） |
| 輸送元 | 出発地の住所 | 大阪府堺市 |
| 輸送先 | 目的地の住所 | 山口県下関市 |
| 納期（時間） | コスト計算に使用する納期 | 24時間 |
| 影響半径 (km) | 通行止めの影響範囲 | 5km |
| 2点間の最小間隔 (km) | 通行止め2点の最小距離 | 20km |
| 通行止め発生タイミング | 何 tick 目に通行止めを発生させるか | 8 |
| トラック更新間隔 (ms) | シミュレーション速度 | 1000ms |

---

## API キーについて

ORS API Key は**任意**です。未入力の場合は **OSRM（完全無料・APIキー不要）** で自動的に陸路を計算します。

ORS を使用する場合は [openrouteservice.org](https://openrouteservice.org) で無料登録（1日2,000リクエスト）してください。

---

## Streamlit Cloud へのデプロイ

### 手順

1. このリポジトリを GitHub に Push
2. [share.streamlit.io](https://share.streamlit.io) にアクセス
3. **New app** → リポジトリ・ブランチ・ファイル名を設定

```
Repository : fkd-streamlit/logistics-optimizer
Branch     : main
Main file  : logistics_app_v2.py
```

4. **Deploy!** をクリック

### Secrets 設定（ORS を使う場合）

Streamlit Cloud の **Settings → Secrets** に以下を追加：

```toml
ORS_API_KEY = "your_api_key_here"
```

### 無料枠の制限

| 項目 | 制限 |
|------|------|
| メモリ | 1GB |
| アプリ数 | 1個（Public リポジトリ） |
| 非アクティブ時 | 一定時間後スリープ（アクセスで復帰） |

---

## アーキテクチャ

```
logistics_app_v2.py
│
├── ルーティング層
│   ├── geocode()          住所 → 緯度経度（Nominatim）
│   ├── get_route()        ORS / OSRM でルート取得（@cache_data）
│   └── find_detour()      SA による迂回候補選択
│
├── 最適化層
│   ├── sa_select()        焼きなまし法（コスト最小化）
│   ├── pick_blocks()      通行止め位置のランダム配置
│   └── violates()         ルートが通行止めに抵触するか判定
│
├── 可視化層
│   ├── build_map()        folium 地図生成（変化時のみ）
│   └── factory_html       Canvas + JS 工場アニメーション
│
└── Streamlit UI
    ├── サイドバー         パラメータ設定
    ├── 左カード           トラックシミュレーション
    └── 右カード           AGV シミュレーション
```

---

## メモリ最適化のポイント

Streamlit Cloud 無料枠（1GB）で安定動作させるための工夫：

1. **`map_dirty` フラグ** — 地図はルート変化時のみ再生成（毎 tick 再生成しない）
2. **AGV を純 JS 化** — Python の rerun・session_state を使わず JS の requestAnimationFrame で完結
3. **`@st.cache_data`** — 同じ区間のルートは API を再取得しない
4. **ルート点数を最大 200 点に間引き** — session_state のメモリ使用量を削減
5. **`st_autorefresh` を1つに統合** — 2重 rerun を防止

---

## ライセンス

MIT License

---

## 作者

**fkd-streamlit**
- GitHub: [github.com/fkd-streamlit](https://github.com/fkd-streamlit)
- App: [logistics-optimizer on Streamlit Cloud](https://logistics-optimizer-mgm6atk2cfmmlxibo3xhhb.streamlit.app/)

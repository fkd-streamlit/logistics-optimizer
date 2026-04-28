# 生産ラインシミュレーションアプリ v2

物流ルート最適化（通行止め + SA）と、工場内AGV搬送シミュレーションを1つの画面で確認できる `Streamlit` アプリです。

## 機能

- 物流ルート最適化（焼きなまし法）
  - 通行止め2点をランダム生成
  - 迂回候補を評価して最小コストを選択
  - ORS優先、失敗時はOSRMにフォールバック
- 工場内AGV（5台）シミュレーション
  - 衝突回避の待機ロジック
  - 稼働ステップ可視化

## 必要環境

- Python 3.11 以上推奨

## ローカル実行

```powershell
cd "c:\Users\FMV\Desktop\QUBO_物流最適化"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run logistics_app_v2.py
```

## APIキーについて

- `OpenRouteService API Key` は任意です。
- 未入力時もOSRMフォールバックにより陸路計算を継続します。

## GitHubで配布する手順

```powershell
cd "c:\Users\FMV\Desktop\QUBO_物流最適化"
git init
git add .
git commit -m "Initial release: logistics + AGV streamlit app"
```

その後、GitHubで新規リポジトリを作成して `git remote add origin ...` / `git push -u origin main` を実行してください。

## 推奨エントリーポイント

- `logistics_app_v2.py`

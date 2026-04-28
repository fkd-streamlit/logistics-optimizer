"""
logistics_app_v2.py
グローバル物流ルート + 工場内AGVシミュレーション
── SA（焼きなまし法）による迂回ルート最適化 ──

主な改善点：
  1. ORS avoid_polygons に依存しない「純粋SA選択」方式
     - avoid_polygons を使うと日本の細道で経路なしが頻発するため廃止
     - waypoint候補のルートを全件ORS取得し，violates()でブロック通過チェック
  2. ブロック配置の堅牢化
     - ルート点数が少なくてもフォールバック配置
     - min_sep_km 未満なら間隔を緩めて再試行
  3. Waypoint候補の多様化
     - ブロックを「外側へ迂回」する方向だけでなく
       出発地・目的地の中間帯にも候補を追加
  4. SA の設計改善
     - コスト = 距離コスト + 遅延ペナルティ（距離が等しければ時間短いほど良い）
     - feasible==0 のとき，violates チェックを緩めた最良候補を代替採用
  5. ロバスト性
     - ORS 呼び出し失敗を個別 try/except でスキップ
     - 全候補失敗時は「ルートなし」警告のみ（例外で落ちない）
  6. AGV 衝突回避の追加
     - 同一セルに複数台が入ろうとした場合，後着をその場で待機
"""

import math
import random
import warnings
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import folium
import matplotlib.pyplot as plt
import streamlit as st

from geopy.geocoders import Nominatim
from geopy.distance import geodesic, distance as geo_distance
from urllib3.exceptions import InsecureRequestWarning
from streamlit_autorefresh import st_autorefresh

warnings.simplefilter("ignore", InsecureRequestWarning)

# =========================================================
# UI skin
# =========================================================
st.set_page_config(page_title="生産ラインシミュレーションアプリ v2", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap');
* { font-family: 'Noto Sans JP', sans-serif; }
.stApp { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); }
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1500px; }
.hero { text-align:center; color:#fff; padding: 18px 0 10px 0; margin-bottom: 18px; }
.hero h1 { font-size:40px; font-weight:900; margin:0; letter-spacing:1px;
            background: linear-gradient(90deg,#e94560,#0f3460,#53d8fb);
            -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.hero p { font-size:16px; color:#aac4ff; margin:8px 0 0 0; }
.card {
  background: rgba(255,255,255,0.04);
  border-radius: 16px;
  padding: 18px 18px 16px 18px;
  box-shadow: 0 12px 32px rgba(0,0,0,0.45);
  border: 1px solid rgba(255,255,255,0.10);
  color: #e0e0e0;
}
.card-title {
  display:flex; align-items:center; gap:10px;
  font-size:20px; font-weight:900; color:#53d8fb;
  margin:0 0 10px 0;
}
.hrline { height:2px; background:linear-gradient(90deg,#e94560,#53d8fb);
          opacity:0.7; border-radius:2px; margin:8px 0 14px 0; }
.stButton>button {
  width:100%;
  border-radius: 10px;
  padding: 0.75rem 1rem;
  font-weight: 900;
  border: none;
  background: linear-gradient(90deg, #e94560, #0f3460);
  color: white;
  box-shadow: 0 6px 18px rgba(0,0,0,0.4);
  transition: opacity .2s;
}
.stButton>button:hover { opacity:.85; }
section[data-testid="stSidebar"] > div {
  background: rgba(15,20,40,0.95);
  border-right: 1px solid rgba(255,255,255,0.08);
  color: #ccc;
}
label, .stSelectbox label, .stSlider label, .stCheckbox label { color:#aac4ff !important; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="hero">
  <h1>🏭 生産ラインシミュレーションアプリ v2</h1>
  <p>グローバル物流（通行止め＋SA最適化） ＋ 工場内 AGV 5台衝突回避シミュレーション</p>
</div>
""", unsafe_allow_html=True)

# =========================================================
# session_state 初期化
# =========================================================
_defaults = {
    "map_html": None,
    "route_df": None,
    "best_route": None,
    "geo_info": None,
    "blocks": None,
    "agv_running": False,
    "agv_tick": 0,
    "agv_state": None,
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================================================
# ユーティリティ
# =========================================================
def geocode(addr: str, jp_only: bool = True):
    geo = Nominatim(user_agent="logistics-agv-demo-v2")
    loc = geo.geocode(addr, country_codes="jp" if jp_only else None, exactly_one=True)
    if loc is None:
        raise ValueError(f"住所が見つかりません: {addr}")
    return loc.latitude, loc.longitude, loc.address


def ors_geojson(api_key: str, coords_lonlat, timeout=20):
    """
    avoid_polygons を一切使わず素のルートを取得。
    呼び出し側でviolates()によりブロック通過チェックを行う。
    """
    url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"coordinates": coords_lonlat}
    try:
        r = requests.post(url, json=body, headers=headers,
                          verify=False, timeout=timeout)
        return r.json()
    except Exception:
        return {}

def osrm_geojson(coords_lonlat, timeout=20):
    """
    ORS失敗時のフォールバック用（APIキー不要）。
    """
    try:
        coord_str = ";".join([f"{lon},{lat}" for lon, lat in coords_lonlat])
        url = f"https://router.project-osrm.org/route/v1/driving/{coord_str}"
        params = {"overview": "full", "geometries": "geojson"}
        r = requests.get(url, params=params, timeout=timeout)
        return r.json()
    except Exception:
        return {}

def get_route_with_fallback(api_key: str, coords_lonlat, timeout=20):
    """
    ORSを優先し、失敗時はOSRMにフォールバックして route を返す。
    return: route, dist_km, time_hr, source("ORS"/"OSRM"/None)
    """
    if api_key:
        gj = ors_geojson(api_key, coords_lonlat, timeout=timeout)
        route, dist_km, time_hr = extract_route(gj)
        if route is not None:
            return route, dist_km, time_hr, "ORS"

    gj = osrm_geojson(coords_lonlat, timeout=timeout)
    route, dist_km, time_hr = extract_route(gj)
    if route is not None:
        return route, dist_km, time_hr, "OSRM"
    return None, None, None, None


def extract_route(geojson):
    # ORS
    feats = geojson.get("features")
    if isinstance(feats, list) and len(feats) > 0:
        f0 = feats[0]
        coords = f0.get("geometry", {}).get("coordinates")
        summ   = f0.get("properties", {}).get("summary")
        if isinstance(coords, list) and isinstance(summ, dict):
            route = [[pt[1], pt[0]] for pt in coords
                     if isinstance(pt, list) and len(pt) >= 2]
            if len(route) >= 2:
                return route, summ.get("distance", 0) / 1000, summ.get("duration", 0) / 3600

    # OSRM
    routes = geojson.get("routes")
    if isinstance(routes, list) and len(routes) > 0:
        r0 = routes[0]
        coords = r0.get("geometry", {}).get("coordinates")
        if isinstance(coords, list):
            route = [[pt[1], pt[0]] for pt in coords
                     if isinstance(pt, list) and len(pt) >= 2]
            if len(route) >= 2:
                return route, r0.get("distance", 0) / 1000, r0.get("duration", 0) / 3600

    return None, None, None


# =========================================================
# ブロック配置
# =========================================================
def pick_blocks_on_route(base_route: list, n_blocks: int = 2, min_sep_km: float = 20):
    """
    ルート上にランダムに n_blocks 点を配置。
    ルート点数が少ない・min_sep_km を満たせない場合はフォールバック。
    """
    if base_route is None:
        return None

    n = len(base_route)
    margin = max(5, n // 10)          # 端から除外するインデックス数

    # ルートが短すぎる場合でも動くように端マージンを動的に縮める
    if n < n_blocks * 2 + 4:
        margin = 1

    inner = list(range(margin, max(n - margin, margin + n_blocks)))
    if len(inner) < n_blocks:
        inner = list(range(n))

    # min_sep_km を満たすまで最大60回試行。失敗したら間隔を半分に緩める
    cur_sep = min_sep_km
    while cur_sep >= 1:
        for _ in range(60):
            if len(inner) < n_blocks:
                break
            idxs = sorted(random.sample(inner, k=n_blocks))
            pts = [tuple(base_route[i]) for i in idxs]
            if all(
                geodesic(pts[i], pts[j]).km >= cur_sep
                for i in range(len(pts))
                for j in range(i + 1, len(pts))
            ):
                return pts
        cur_sep /= 2   # 条件を緩める

    # 最終フォールバック：等分割配置
    step = max(1, n // (n_blocks + 1))
    return [tuple(base_route[min(step * (i + 1), n - 1)]) for i in range(n_blocks)]


# =========================================================
# 経路がブロックに入っているか判定
# =========================================================
def violates(route_latlon, centers_latlon, radius_km, margin_km=0.3):
    thr = radius_km + margin_km
    for lat, lon in route_latlon:
        for c_lat, c_lon in centers_latlon:
            if geodesic((lat, lon), (c_lat, c_lon)).km <= thr:
                return True
    return False


# =========================================================
# Waypoint候補生成（改善版）
# =========================================================
def gen_waypoints(
    centers_latlon, base_radius_km, n_angles=12,
    o_latlon=None, d_latlon=None
):
    """
    ブロック周辺の外側候補 ＋ 出発地と目的地の中間帯候補 を混合。
    """
    multipliers = [2.0, 3.0, 5.0, 8.0, 12.0, 20.0]
    wps = []

    # ① ブロック外周の候補
    for c_lat, c_lon in centers_latlon:
        for mul in multipliers:
            for ang in np.linspace(0, 360, n_angles, endpoint=False):
                dest = geo_distance(
                    kilometers=base_radius_km * mul
                ).destination((c_lat, c_lon), ang)
                wps.append((dest.latitude, dest.longitude))

    # ② 出発地～目的地の中間帯（端に偏らないよう t=0.3〜0.7）
    if o_latlon and d_latlon:
        o_lat, o_lon = o_latlon
        d_lat, d_lon = d_latlon
        for t in np.linspace(0.2, 0.8, 7):
            mid_lat = o_lat + t * (d_lat - o_lat)
            mid_lon = o_lon + t * (d_lon - o_lon)
            # 中間点の少しずれた位置を候補に
            for ang in np.linspace(0, 360, 8, endpoint=False):
                dest = geo_distance(
                    kilometers=base_radius_km * 4
                ).destination((mid_lat, mid_lon), ang)
                wps.append((dest.latitude, dest.longitude))

    # 重複除去
    uniq, seen = [], set()
    for lat, lon in wps:
        key = (round(lat, 3), round(lon, 3))
        if key not in seen:
            seen.add(key)
            uniq.append((lat, lon))
    return uniq


# =========================================================
# SA（焼きなまし法）：feasible リストからインデックスを選択
# =========================================================
def simulated_annealing(costs, n_iter=2000, T0=1.0, alpha=0.993, seed=42):
    rnd = random.Random(seed)
    n = len(costs)
    if n == 0:
        return None
    if n == 1:
        return 0
    cur  = rnd.randrange(n)
    best = cur
    T    = T0
    for _ in range(n_iter):
        nxt = rnd.randrange(n)
        if nxt == cur:
            continue
        delta = costs[nxt] - costs[cur]
        if delta < 0 or rnd.random() < math.exp(-delta / max(T, 1e-9)):
            cur = nxt
        if costs[cur] < costs[best]:
            best = cur
        T *= alpha
    return best


# =========================================================
# folium HTML → data URL
# =========================================================
def folium_to_data_url(html: str) -> str:
    return "data:text/html;charset=utf-8," + quote(html)


# =========================================================
# Sidebar
# =========================================================
with st.sidebar:
    st.markdown("## ⚙️ 設定")
    st.markdown("---")
    api_key   = st.text_input("OpenRouteService API Key（任意）", type="password")
    st.caption("未入力時は OSRM フォールバックで陸路を計算します。")
    origin    = st.text_input("輸送元（住所）", "大阪府堺市")
    dest_addr = st.text_input("輸送先（住所）", "山口県下関市")
    due_time  = st.slider("納期（時間）", 5, 72, 24)
    jp_only   = st.checkbox("日本国内モード", value=True)

    st.markdown("### 🚧 通行止め（ランダム2点）")
    enable_blocks = st.checkbox("通行止めを有効化", value=True)
    radius_km     = st.slider("影響半径 (km)", 1, 20, 5)
    min_sep_km    = st.slider("通行止め2点の最小間隔 (km)", 5, 100, 20)
    n_candidates  = st.slider("迂回候補数", 10, 80, 40)
    sa_iters      = st.slider("SA反復回数", 500, 5000, 2000)
    max_resample  = st.slider("再抽選最大回数", 3, 30, 10)

    debug = st.checkbox("デバッグ表示", value=False)
    run   = st.button("🚀 ルート計算（通行止め＋SA）")
    if run:
        st.session_state.agv_running = False

# =========================================================
# レイアウト
# =========================================================
left, right = st.columns([1.2, 1.2], gap="large")

# =========================================================
# 左カード：物流ルート最適化
# =========================================================
with left:
    st.markdown(
        '<div class="card">'
        '<div class="card-title">🌍 グローバル物流ルート（通行止め＋SA最適化）</div>'
        '<div class="hrline"></div>',
        unsafe_allow_html=True
    )

    if run:
        try:
            # ── ジオコーディング ──
            o_lat, o_lon, o_addr = geocode(origin,    jp_only=jp_only)
            d_lat, d_lon, d_addr = geocode(dest_addr, jp_only=jp_only)
            st.write(f"輸送元: {o_addr} ({o_lat:.5f}, {o_lon:.5f})")
            st.write(f"輸送先: {d_addr} ({d_lat:.5f}, {d_lon:.5f})")
            st.session_state.geo_info = {
                "origin": (o_addr, o_lat, o_lon),
                "dest":   (d_addr, d_lat, d_lon),
            }

            coords_od = [[o_lon, o_lat], [d_lon, d_lat]]

            # ── ベースルート（avoid なし）──
            base_route, base_dist, base_time, base_source = get_route_with_fallback(api_key, coords_od)
            if base_route is None:
                raise RuntimeError(
                    "ベースルートを取得できませんでした。"
                    "住所・ネットワーク・API Key（任意）を確認してください。"
                )
            if base_source == "OSRM":
                st.info("ℹ️ ORSが利用できなかったため、OSRMで陸路を計算しています。")

            # ── 通行止めブロック配置ループ ──
            best_truck  = None
            best_blocks = None
            best_base_route = base_route
            used_blocks_for_map = None

            for attempt in range(max_resample):

                blocks = (
                    pick_blocks_on_route(
                        base_route, n_blocks=2, min_sep_km=min_sep_km
                    )
                    if enable_blocks else None
                )

                # ── Waypoint候補生成 ──
                # BASE（直接ルート）は常に候補に含める
                candidates = [("BASE", coords_od)]
                if blocks:
                    wps = gen_waypoints(
                        blocks, radius_km, n_angles=12,
                        o_latlon=(o_lat, o_lon),
                        d_latlon=(d_lat, d_lon),
                    )
                    random.shuffle(wps)
                    wps = wps[:n_candidates]
                    for i, (w_lat, w_lon) in enumerate(wps):
                        coords3 = [[o_lon, o_lat], [w_lon, w_lat], [d_lon, d_lat]]
                        candidates.append((f"WP{i+1}", coords3))

                # ── 各候補をORS取得してviolatesチェック ──
                feasible        = []   # ブロック回避できた候補
                feasible_loose  = []   # ブロック通過するが取得できた候補（最終手段）

                for tag, coords in candidates:
                    route, dist_km, time_hr, source = get_route_with_fallback(api_key, coords)
                    if route is None:
                        continue
                    cost = dist_km * 120 + max(0, time_hr - due_time) * 5000
                    entry = {
                        "tag":     tag,
                        "coords":  coords,
                        "route":   route,
                        "dist_km": dist_km,
                        "time_hr": time_hr,
                        "cost":    cost,
                        "source":  source,
                    }
                    if blocks and violates(route, blocks, radius_km):
                        feasible_loose.append(entry)   # 通過するが存在はする
                    else:
                        feasible.append(entry)         # 回避成功

                if debug:
                    st.write(
                        f"attempt {attempt+1}/{max_resample} | "
                        f"candidates={len(candidates)} | "
                        f"feasible(回避)={len(feasible)} | "
                        f"feasible_loose(通過)={len(feasible_loose)}"
                    )

                # ── SA で最良候補を選択 ──
                if len(feasible) > 0:
                    # 正常系：ブロック回避できた候補から選択
                    costs = [f["cost"] for f in feasible]
                    idx   = simulated_annealing(costs, n_iter=sa_iters, seed=42)
                    best_truck   = feasible[idx]
                    best_blocks  = blocks
                    used_blocks_for_map = blocks
                    if debug:
                        st.success(f"✅ 回避成功 attempt {attempt+1}")
                    break

                # feasible == 0 で最終試行なら loose から最良を使う
                if attempt == max_resample - 1 and len(feasible_loose) > 0:
                    costs = [f["cost"] for f in feasible_loose]
                    idx   = simulated_annealing(costs, n_iter=sa_iters, seed=42)
                    best_truck   = feasible_loose[idx]
                    best_blocks  = blocks
                    used_blocks_for_map = blocks
                    st.warning(
                        "⚠️ 完全な迂回ルートは見つかりませんでしたが、"
                        "最も通行止め影響の少ない候補を表示します。"
                        "影響半径・再抽選回数を増やすと改善することがあります。"
                    )

            # ── 比較表（トラック / 飛行機 / 船）──
            gc_km = geodesic((o_lat, o_lon), (d_lat, d_lon)).km
            rows  = []

            if best_truck is not None:
                rows.append([
                    "トラック(SA)",
                    round(best_truck["dist_km"], 1),
                    round(best_truck["time_hr"], 1),
                    int(best_truck["cost"]),
                ])

            plane_time = gc_km / 800 + 4
            plane_cost = gc_km * 250 + 30000 + max(0, plane_time - due_time) * 5000
            rows.append(["飛行機", round(gc_km, 1), round(plane_time, 1), int(plane_cost)])

            ship_dist = gc_km * 1.6
            ship_time = gc_km / 40
            ship_cost = ship_dist * 72 + 50000 + max(0, ship_time - due_time) * 5000
            rows.append(["船", round(ship_dist, 1), round(ship_time, 1), int(ship_cost)])

            df = pd.DataFrame(rows, columns=["手段", "距離(km)", "時間(h)", "コスト(円)"])
            best_mode = df.loc[df["コスト(円)"].idxmin()]["手段"]

            # ── 地図描画 ──
            m = folium.Map(
                location=[(o_lat + d_lat) / 2, (o_lon + d_lon) / 2],
                zoom_start=6,
                tiles="CartoDB Positron",
            )
            # 通常ルート（グレー破線）
            folium.PolyLine(
                best_base_route, color="gray", weight=4, opacity=0.6,
                dash_array="6,6", tooltip="通常ルート(ORS)"
            ).add_to(m)

            # SA 最適ルート（青）
            if best_truck is not None:
                folium.PolyLine(
                    best_truck["route"], color="#00cfff", weight=6,
                    opacity=0.9, tooltip=f"SA最適ルート({best_truck['tag']})"
                ).add_to(m)

            # 通行止め円（オレンジ）
            if used_blocks_for_map:
                for b_lat, b_lon in used_blocks_for_map:
                    folium.Circle(
                        [b_lat, b_lon], radius=radius_km * 1000,
                        color="#ff7700", fill=True, fill_opacity=0.25,
                        tooltip="🚧 通行止め"
                    ).add_to(m)
                    folium.Marker(
                        [b_lat, b_lon],
                        icon=folium.Icon(icon="ban", prefix="fa", color="red"),
                        tooltip="🚧 通行止め"
                    ).add_to(m)

            # 概念線（飛行機：赤、船：緑）
            folium.PolyLine(
                [[o_lat, o_lon], [d_lat, d_lon]],
                color="red", dash_array="10,10", tooltip="飛行機(概念)"
            ).add_to(m)
            tokyo_port = [35.63, 139.77]
            osaka_port = [34.65, 135.43]
            folium.PolyLine(
                [[o_lat, o_lon], tokyo_port, osaka_port, [d_lat, d_lon]],
                color="green", tooltip="船(概念)"
            ).add_to(m)

            # マーカー
            folium.Marker(
                [o_lat, o_lon],
                icon=folium.Icon(icon="play", prefix="fa", color="blue"),
                tooltip=f"出発: {o_addr}"
            ).add_to(m)
            folium.Marker(
                [d_lat, d_lon],
                icon=folium.Icon(icon="flag", prefix="fa", color="darkred"),
                tooltip=f"到着: {d_addr}"
            ).add_to(m)

            # セッション保存
            st.session_state.map_html  = m.get_root().render()
            st.session_state.route_df  = df
            st.session_state.best_route = best_mode

            if best_truck is None:
                st.warning(
                    "⚠️ 再抽選上限まで試しましたが、ルート候補が見つかりませんでした。"
                    "API Key・住所・パラメータを確認してください。"
                )
            else:
                avoid_msg = (
                    f"✅ SA最適化完了 — ルート: {best_truck['tag']} | "
                    f"候補{len(feasible)}件から選択 | 推奨手段: {best_mode} | "
                    f"経路API: {best_truck['source']}"
                )
                st.success(avoid_msg)

        except Exception as e:
            st.error(f"実行エラー: {e}")
            if debug:
                import traceback
                st.code(traceback.format_exc())

    # 結果表示（ページ維持）
    if st.session_state.map_html:
        st.markdown("**📍 ルートマップ**")
        st.iframe(
            folium_to_data_url(st.session_state.map_html),
            height=400, width="stretch"
        )
        st.dataframe(st.session_state.route_df, use_container_width=True)
        st.success(f"🏆 推奨手段：{st.session_state.best_route}")

    st.markdown("</div>", unsafe_allow_html=True)


# =========================================================
# 右カード：AGV シミュレーション（衝突回避付き）
# =========================================================
with right:
    st.markdown(
        '<div class="card">'
        '<div class="card-title">🏭 工場内 AGV 移動シミュレーション（衝突回避）</div>'
        '<div class="hrline"></div>',
        unsafe_allow_html=True
    )

    GRID     = 20
    machines = {"A": (10, 5), "B": (15, 15), "C": (5, 14)}
    machine_list = list(machines.values())

    # ── AGV 初期化 ──
    def init_agvs():
        used = set(machine_list)
        agvs = []
        for i in range(5):
            while True:
                p = (random.randint(0, GRID - 1), random.randint(0, GRID - 1))
                if p not in used:
                    used.add(p)
                    break
            agvs.append({
                "id":    i + 1,
                "pos":   p,
                "goal":  random.choice(machine_list),
                "dwell": random.randint(2, 5),
                "wait":  0,   # 衝突回避待機カウンタ
            })
        return agvs

    if st.session_state.agv_state is None:
        st.session_state.agv_state = init_agvs()

    c1, c2, c3 = st.columns(3)
    if c1.button("▶ AGV 開始"):
        st.session_state.agv_running = True
    if c2.button("⏹ AGV 停止"):
        st.session_state.agv_running = False
    if c3.button("🔄 リセット"):
        st.session_state.agv_state   = init_agvs()
        st.session_state.agv_tick    = 0
        st.session_state.agv_running = False

    # ── AGV 1ステップ更新（衝突回避付き）──
    if st.session_state.agv_running:
        st_autorefresh(interval=400, key="agv_refresh")

        agvs = st.session_state.agv_state
        occupied = {agv["pos"] for agv in agvs}   # 現在の占有セル

        new_positions = {}   # agv_id -> 移動先
        for agv in agvs:
            if agv["wait"] > 0:
                agv["wait"] -= 1
                new_positions[agv["id"]] = agv["pos"]
                continue

            if agv["pos"] == agv["goal"]:
                agv["dwell"] -= 1
                if agv["dwell"] <= 0:
                    agv["goal"]  = random.choice(machine_list)
                    agv["dwell"] = random.randint(2, 5)
                new_positions[agv["id"]] = agv["pos"]
            else:
                x, y  = agv["pos"]
                gx, gy = agv["goal"]
                # 優先軸を選択
                if abs(gx - x) >= abs(gy - y):
                    nx, ny = x + int(np.sign(gx - x)), y
                else:
                    nx, ny = x, y + int(np.sign(gy - y))
                nx = max(0, min(GRID - 1, nx))
                ny = max(0, min(GRID - 1, ny))
                candidate = (nx, ny)

                # 衝突回避：移動先が他 AGV に占有されていたら待機
                already_claimed = list(new_positions.values())
                if candidate in occupied or candidate in already_claimed:
                    agv["wait"] = random.randint(1, 2)
                    new_positions[agv["id"]] = agv["pos"]   # 現在地を維持
                else:
                    new_positions[agv["id"]] = candidate

        # 位置を更新
        for agv in agvs:
            agv["pos"] = new_positions[agv["id"]]

        st.session_state.agv_tick += 1

    # ── 描画 ──
    fig, ax = plt.subplots(figsize=(7, 5))
    fig.patch.set_facecolor("#0f1826")
    ax.set_facecolor("#0f1826")
    ax.set_xlim(-0.5, GRID - 0.5)
    ax.set_ylim(-0.5, GRID - 0.5)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.15, color="white")
    ax.set_title(
        f"⏱ Time = {st.session_state.agv_tick}",
        color="white", fontsize=14, fontweight="bold"
    )
    ax.tick_params(colors="gray")

    # 機械
    for name, pos in machines.items():
        ax.scatter(pos[0], pos[1], s=300, c="#53d8fb",
                   marker="s", zorder=5, edgecolors="white", linewidths=1.5)
        ax.text(pos[0] + 0.3, pos[1] + 0.3, name,
                fontsize=13, weight="bold", color="white", zorder=6)

    # AGV
    colors = ["#e94560", "#00cfff", "#34c759", "#ff9f0a", "#bf5af2"]
    for i, agv in enumerate(st.session_state.agv_state):
        x, y  = agv["pos"]
        gx, gy = agv["goal"]
        marker = "X" if agv.get("wait", 0) > 0 else "o"   # 待機中は × 表示
        ax.scatter(x,  y,  s=200, c=colors[i], marker=marker,
                   zorder=7, edgecolors="white", linewidths=1,
                   label=f"AGV{agv['id']}")
        ax.scatter(gx, gy, s=80, c=colors[i], marker="*",
                   alpha=0.4, zorder=4)
        ax.annotate(
            "", xy=(gx, gy), xytext=(x, y),
            arrowprops=dict(arrowstyle="->", color=colors[i], alpha=0.3, lw=1)
        )

    ax.legend(
        fontsize=9, loc="upper right",
        facecolor="#1a2540", edgecolor="gray", labelcolor="white"
    )
    st.pyplot(fig, use_container_width=True)

    st.markdown(
        "<small style='color:#aac4ff'>"
        "● 丸 = 走行中 　 × = 衝突回避待機 　 ☆ = 目標地点 　 □ = 機械（A/B/C）"
        "</small>",
        unsafe_allow_html=True
    )

    st.markdown("</div>", unsafe_allow_html=True)

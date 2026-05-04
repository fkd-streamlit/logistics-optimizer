"""
logistics_app_v6.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
■ 左パネル：複数トラック動的迂回シミュレーション
  - 3台のトラックが同時走行
  - 走行中にランダムで通行止め（2点）が発生
  - 各トラックが SA で個別に迂回ルートを再計算
  - folium アニメーション（AntPath）で可視化

■ 右パネル：工場内 AGV ビジュアルシミュレーション
  - HTML Canvas による工場フロア等角投影図
  - 搬送路レーン・棚・機械を描画
  - AGV 5台が滑らかに走行・衝突回避
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import math, random, warnings, json
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests
import folium
from folium.plugins import AntPath
import streamlit as st
import streamlit.components.v1 as components

from geopy.geocoders import Nominatim
from geopy.distance import geodesic, distance as geo_distance
from urllib3.exceptions import InsecureRequestWarning
from streamlit_autorefresh import st_autorefresh

warnings.simplefilter("ignore", InsecureRequestWarning)

# ═══════════════════════════════════════════
# ページ設定 & CSS
# ═══════════════════════════════════════════
st.set_page_config(page_title="物流×AGV最適化 v3", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap');
* { font-family:'Noto Sans JP',sans-serif; box-sizing:border-box; }
.stApp { background:#0b0f1a; }
.block-container { padding-top:1rem; padding-bottom:2rem; max-width:1600px; }

/* ヒーロー */
.hero { text-align:center; padding:16px 0 12px; }
.hero h1 {
  font-size:36px; font-weight:900; margin:0; letter-spacing:.5px;
  background:linear-gradient(90deg,#ff6b6b,#ffd93d,#6bcb77,#4d96ff);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
}
.hero p { color:#8899bb; font-size:14px; margin:6px 0 0; }

/* カード */
.card {
  background:rgba(255,255,255,0.03);
  border:1px solid rgba(255,255,255,0.08);
  border-radius:14px;
  padding:16px;
  box-shadow:0 8px 32px rgba(0,0,0,0.5);
  color:#dde;
}
.card-title {
  font-size:17px; font-weight:900; color:#4d96ff;
  margin:0 0 8px; display:flex; align-items:center; gap:8px;
}
.hrline {
  height:2px; border-radius:2px; margin:0 0 12px;
  background:linear-gradient(90deg,#ff6b6b,#4d96ff);
}

/* ボタン */
.stButton>button {
  border-radius:8px; padding:.6rem .8rem; font-weight:900;
  border:none; width:100%;
  background:linear-gradient(90deg,#ff6b6b,#4d96ff);
  color:#fff; box-shadow:0 4px 14px rgba(0,0,0,0.4);
}
.stButton>button:hover { opacity:.85; }

/* サイドバー */
section[data-testid="stSidebar"]>div {
  background:rgba(10,14,28,0.97);
  border-right:1px solid rgba(255,255,255,0.06);
}
label,.stCheckbox label,.stSlider label { color:#8899bb !important; }

/* ステータスバッジ */
.badge {
  display:inline-block; border-radius:6px; padding:2px 8px;
  font-size:12px; font-weight:700; margin:2px;
}
.badge-run  { background:#1a3a1a; color:#6bcb77; border:1px solid #6bcb77; }
.badge-reroute { background:#3a2a00; color:#ffd93d; border:1px solid #ffd93d; }
.badge-block { background:#3a0a0a; color:#ff6b6b; border:1px solid #ff6b6b; }
.badge-done { background:#0a1a3a; color:#4d96ff; border:1px solid #4d96ff; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="hero">
  <h1>🚛 物流×AGV 量子インスパイア最適化 v3</h1>
  <p>複数トラック動的迂回（SA） ＋ 工場内AGV5台 リアルタイムシミュレーション</p>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════
# Session State
# ═══════════════════════════════════════════
_DEF = {
    # 物流
    "map_html": None, "route_df": None, "best_route": None,
    "trucks": None,          # [{id, route, color, status, dist, time}]
    "blocks": None,          # [(lat,lon), ...]
    "sim_tick": 0,
    "sim_running": False,
    "sim_done": False,
    "base_route": None,
    "o_latlon": None, "d_latlon": None,
    "o_addr": "", "d_addr": "",
    "api_key_cache": "",
    "radius_km_cache": 5,
    # AGV
    "agv_running": False,
    "agv_tick": 0,
    "agv_state": None,
}
for k, v in _DEF.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ═══════════════════════════════════════════
# ── ルーティングユーティリティ ──
# ═══════════════════════════════════════════
def geocode(addr, jp_only=True):
    geo = Nominatim(user_agent="logistics-v3")
    loc = geo.geocode(addr, country_codes="jp" if jp_only else None, exactly_one=True)
    if loc is None:
        raise ValueError(f"住所が見つかりません: {addr}")
    return loc.latitude, loc.longitude, loc.address

def ors_route(api_key, coords_lonlat, timeout=18):
    if not api_key:
        return {}
    try:
        url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
        r = requests.post(url,
            json={"coordinates": coords_lonlat},
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            verify=False, timeout=timeout)
        return r.json()
    except Exception:
        return {}

def osrm_route(coords_lonlat, timeout=18):
    try:
        s = ";".join(f"{lon},{lat}" for lon, lat in coords_lonlat)
        r = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{s}",
            params={"overview":"full","geometries":"geojson"}, timeout=timeout)
        return r.json()
    except Exception:
        return {}

def extract_route(gj):
    # ORS
    feats = gj.get("features")
    if isinstance(feats, list) and feats:
        f0 = feats[0]
        coords = f0.get("geometry", {}).get("coordinates", [])
        summ   = f0.get("properties", {}).get("summary", {})
        if isinstance(coords, list) and len(coords) >= 2:
            route = [[p[1], p[0]] for p in coords if len(p) >= 2]
            return route, summ.get("distance",0)/1000, summ.get("duration",0)/3600
    # OSRM
    routes = gj.get("routes")
    if isinstance(routes, list) and routes:
        r0 = routes[0]
        coords = r0.get("geometry", {}).get("coordinates", [])
        if isinstance(coords, list) and len(coords) >= 2:
            route = [[p[1], p[0]] for p in coords if len(p) >= 2]
            return route, r0.get("distance",0)/1000, r0.get("duration",0)/3600
    return None, None, None

def get_route(api_key, coords_lonlat):
    """ORS優先 → OSRM フォールバック"""
    gj = ors_route(api_key, coords_lonlat)
    route, d, t = extract_route(gj)
    if route:
        return route, d, t, "ORS"
    gj = osrm_route(coords_lonlat)
    route, d, t = extract_route(gj)
    if route:
        return route, d, t, "OSRM"
    return None, None, None, None

def violates(route, centers, radius_km, margin=0.3):
    thr = radius_km + margin
    for lat, lon in route:
        for c in centers:
            if geodesic((lat, lon), c).km <= thr:
                return True
    return False

def pick_blocks(base_route, n=2, min_sep=20):
    if not base_route or len(base_route) < 6:
        return None
    N = len(base_route)
    m = max(3, N // 8)
    inner = list(range(m, N - m))
    sep = min_sep
    while sep >= 1:
        for _ in range(80):
            if len(inner) < n:
                break
            idxs = sorted(random.sample(inner, n))
            pts = [tuple(base_route[i]) for i in idxs]
            if all(geodesic(pts[i], pts[j]).km >= sep
                   for i in range(n) for j in range(i+1, n)):
                return pts
        sep /= 2
    step = max(1, N // (n+1))
    return [tuple(base_route[min(step*(i+1), N-1)]) for i in range(n)]

def gen_waypoints(blocks, radius_km, o_ll, d_ll, n_angles=10, n_candidates=40):
    muls = [2, 3.5, 6, 10, 16]
    wps = []
    for c_lat, c_lon in blocks:
        for mul in muls:
            for ang in np.linspace(0, 360, n_angles, endpoint=False):
                dest = geo_distance(kilometers=radius_km*mul).destination((c_lat,c_lon), ang)
                wps.append((dest.latitude, dest.longitude))
    o_lat, o_lon = o_ll; d_lat, d_lon = d_ll
    for t in np.linspace(0.2, 0.8, 6):
        mlat = o_lat + t*(d_lat-o_lat); mlon = o_lon + t*(d_lon-o_lon)
        for ang in np.linspace(0, 360, 8, endpoint=False):
            dest = geo_distance(kilometers=radius_km*5).destination((mlat, mlon), ang)
            wps.append((dest.latitude, dest.longitude))
    seen, uniq = set(), []
    for lat, lon in wps:
        k = (round(lat,3), round(lon,3))
        if k not in seen:
            seen.add(k); uniq.append((lat, lon))
    random.shuffle(uniq)
    return uniq[:n_candidates]

def sa_select(costs, n_iter=1500, seed=None):
    rnd = random.Random(seed)
    n = len(costs)
    if n == 0: return None
    if n == 1: return 0
    cur = best = rnd.randrange(n)
    T = 1.0
    for _ in range(n_iter):
        nxt = rnd.randrange(n)
        d = costs[nxt] - costs[cur]
        if d < 0 or rnd.random() < math.exp(-d / max(T, 1e-9)):
            cur = nxt
        if costs[cur] < costs[best]:
            best = cur
        T *= 0.993
    return best

TRUCK_COLORS = ["#ff6b6b", "#ffd93d", "#6bcb77"]

# ═══════════════════════════════════════════
# ── SA 迂回ルート探索（1台分）──
# ═══════════════════════════════════════════
def find_detour(api_key, o_ll, d_ll, blocks, radius_km, due_time, max_try=8):
    o_lat, o_lon = o_ll; d_lat, d_lon = d_ll
    coords_od = [[o_lon, o_lat], [d_lon, d_lat]]

    wps = gen_waypoints(blocks, radius_km, o_ll, d_ll, n_candidates=30)

    feasible = []; loose = []
    for tag, coords in [("BASE", coords_od)] + [(f"WP{i+1}", [[o_lon,o_lat],[lon,lat],[d_lon,d_lat]])
                                                 for i,(lat,lon) in enumerate(wps)]:
        route, dist, time_, _ = get_route(api_key, coords)
        if not route: continue
        cost = dist*120 + max(0, time_-due_time)*5000
        entry = {"tag": tag, "route": route, "dist": dist, "time": time_, "cost": cost}
        (feasible if not violates(route, blocks, radius_km) else loose).append(entry)

    pool = feasible if feasible else loose
    if not pool: return None
    idx = sa_select([e["cost"] for e in pool], seed=random.randint(0, 9999))
    return pool[idx]

# ═══════════════════════════════════════════
# ── folium 地図生成 ──
# ═══════════════════════════════════════════
def build_map(o_ll, d_ll, trucks, blocks, radius_km, base_route):
    o_lat, o_lon = o_ll; d_lat, d_lon = d_ll
    m = folium.Map(location=[(o_lat+d_lat)/2, (o_lon+d_lon)/2],
                   zoom_start=6, tiles="CartoDB DarkMatter")

    # ベースルート（グレー破線）
    if base_route:
        folium.PolyLine(base_route, color="#555", weight=3,
                        dash_array="6,6", tooltip="通常ルート").add_to(m)

    # トラックルート
    for tk in (trucks or []):
        if tk.get("route"):
            if tk["status"] in ("reroute", "done"):
                AntPath(tk["route"], color=tk["color"],
                        weight=5, delay=800, tooltip=f"🚛 トラック{tk['id']} 迂回中").add_to(m)
            else:
                folium.PolyLine(tk["route"], color=tk["color"],
                                weight=5, opacity=0.8,
                                tooltip=f"🚛 トラック{tk['id']}").add_to(m)

            # トラック現在位置（進捗で補間）
            prog = min(tk.get("progress", 0), len(tk["route"])-1)
            pos  = tk["route"][prog]
            folium.CircleMarker(pos, radius=10,
                color=tk["color"], fill=True, fill_opacity=1,
                tooltip=f"🚛 {tk['id']} [{tk['status']}]").add_to(m)

    # 通行止め
    if blocks:
        for b_lat, b_lon in blocks:
            folium.Circle([b_lat,b_lon], radius=radius_km*1000,
                color="#ff4444", fill=True, fill_opacity=0.25,
                tooltip="🚧 通行止め").add_to(m)
            folium.Marker([b_lat,b_lon],
                icon=folium.Icon(icon="ban", prefix="fa", color="red"),
                tooltip="🚧 通行止め").add_to(m)

    # 出発・到着
    folium.Marker([o_lat,o_lon],
        icon=folium.Icon(icon="play", prefix="fa", color="blue"),
        tooltip="出発").add_to(m)
    folium.Marker([d_lat,d_lon],
        icon=folium.Icon(icon="flag-checkered", prefix="fa", color="darkgreen"),
        tooltip="到着").add_to(m)

    return m.get_root().render()

# ═══════════════════════════════════════════
# ── サイドバー ──
# ═══════════════════════════════════════════
with st.sidebar:
    st.markdown("## ⚙️ 設定")
    st.markdown("---")
    api_key   = st.text_input("ORS API Key（任意）", type="password")
    st.caption("未入力時は OSRM（無料）で陸路を計算します。")
    origin    = st.text_input("輸送元（住所）", "大阪府堺市")
    dest_addr = st.text_input("輸送先（住所）", "山口県下関市")
    due_time  = st.slider("納期（時間）", 5, 72, 24)
    jp_only   = st.checkbox("日本国内モード", value=True)

    st.markdown("### 🚧 通行止め設定")
    radius_km  = st.slider("影響半径 (km)", 1, 20, 5)
    min_sep_km = st.slider("2点間の最小間隔 (km)", 5, 80, 20)
    block_tick = st.slider("通行止め発生タイミング（tick）", 3, 20, 8)

    st.markdown("### 🚛 シミュレーション")
    sim_speed = st.slider("アニメ速度（ms/tick）", 200, 1500, 500)
    debug     = st.checkbox("デバッグ表示", value=False)
    st.markdown("---")

    col1, col2 = st.columns(2)
    btn_init = col1.button("📍 ルート準備")
    btn_start= col2.button("▶ シミュ開始")
    btn_stop = st.button("⏹ 停止 / リセット")

# ═══════════════════════════════════════════
# ── ボタン処理 ──
# ═══════════════════════════════════════════
if btn_stop:
    st.session_state.sim_running = False
    st.session_state.sim_done    = False
    st.session_state.sim_tick    = 0
    st.session_state.trucks      = None
    st.session_state.blocks      = None
    st.session_state.map_html    = None

if btn_init:
    with st.spinner("ジオコーディング & ベースルート取得中…"):
        try:
            o_lat, o_lon, o_addr = geocode(origin, jp_only=jp_only)
            d_lat, d_lon, d_addr = geocode(dest_addr, jp_only=jp_only)
            base_route, bd, bt, src = get_route(api_key, [[o_lon,o_lat],[d_lon,d_lat]])
            if base_route is None:
                st.error("ベースルートを取得できませんでした。")
            else:
                if src == "OSRM":
                    st.info("ORS 未使用：OSRM でルートを計算しています。")
                # 3台のトラックを初期化（同じルートから出発）
                trucks = []
                for i, color in enumerate(TRUCK_COLORS):
                    trucks.append({
                        "id": i+1, "color": color,
                        "route": base_route,
                        "progress": i * max(1, len(base_route)//6),  # スタート位置をずらす
                        "status": "run",
                        "dist": bd, "time": bt,
                    })
                st.session_state.trucks       = trucks
                st.session_state.base_route   = base_route
                st.session_state.o_latlon     = (o_lat, o_lon)
                st.session_state.d_latlon     = (d_lat, d_lon)
                st.session_state.o_addr       = o_addr
                st.session_state.d_addr       = d_addr
                st.session_state.blocks       = None
                st.session_state.sim_tick     = 0
                st.session_state.sim_running  = False
                st.session_state.sim_done     = False
                st.session_state.api_key_cache    = api_key
                st.session_state.radius_km_cache  = radius_km
                # 初期地図
                st.session_state.map_html = build_map(
                    (o_lat,o_lon),(d_lat,d_lon), trucks, None, radius_km, base_route)
                st.success(f"準備完了 — 出発: {o_addr} → 到着: {d_addr}  ▶ シミュ開始 を押してください")
        except Exception as e:
            st.error(f"エラー: {e}")

if btn_start and st.session_state.trucks:
    st.session_state.sim_running = True
    st.session_state.sim_done    = False

# ═══════════════════════════════════════════
# ── シミュレーション tick ──
# ═══════════════════════════════════════════
if st.session_state.sim_running and not st.session_state.sim_done:
    st_autorefresh(interval=sim_speed, key="sim_refresh")

    tick   = st.session_state.sim_tick + 1
    trucks = st.session_state.trucks
    blocks = st.session_state.blocks
    o_ll   = st.session_state.o_latlon
    d_ll   = st.session_state.d_latlon
    ak     = st.session_state.api_key_cache
    rkm    = st.session_state.radius_km_cache

    # 通行止め発生
    if tick == block_tick and blocks is None and st.session_state.base_route:
        blocks = pick_blocks(st.session_state.base_route, min_sep=min_sep_km)
        st.session_state.blocks = blocks
        # 各トラックをリルート状態に
        if blocks:
            for tk in trucks:
                if tk["status"] == "run":
                    tk["status"] = "reroute"

    # 各トラックの処理
    all_done = True
    for tk in trucks:
        if tk["status"] == "done":
            continue
        all_done = False

        # 迂回ルート計算（reroute 状態で1回だけ）
        if tk["status"] == "reroute" and blocks:
            # 現在位置から目的地への迂回ルートを計算
            prog = min(tk.get("progress", 0), len(tk["route"])-1)
            cur_pos = tk["route"][prog]  # [lat, lon]
            cur_ll  = (cur_pos[0], cur_pos[1])

            result = find_detour(ak, cur_ll, d_ll, blocks, rkm, due_time)
            if result:
                tk["route"]    = result["route"]
                tk["dist"]     = result["dist"]
                tk["time"]     = result["time"]
                tk["progress"] = 0
            tk["status"] = "run"

        # 進捗を進める
        step = max(1, len(tk["route"]) // 40)
        tk["progress"] = min(tk["progress"] + step, len(tk["route"]) - 1)
        if tk["progress"] >= len(tk["route"]) - 1:
            tk["status"] = "done"

    if all_done:
        st.session_state.sim_done    = True
        st.session_state.sim_running = False

    st.session_state.trucks   = trucks
    st.session_state.sim_tick = tick

    # 地図更新
    if o_ll and d_ll:
        st.session_state.map_html = build_map(
            o_ll, d_ll, trucks, blocks, rkm, st.session_state.base_route)

# ═══════════════════════════════════════════
# ── AGV 工場シミュレーション 定義 ──
# ═══════════════════════════════════════════
FACTORY_W, FACTORY_H = 800, 500  # キャンバスサイズ

# 工場レイアウト定義（ピクセル座標）
FACTORY_STATIONS = {
    "入荷ゾーン":   (80,  420),
    "検品台A":      (200, 320),
    "組立ライン1":  (370, 200),
    "組立ライン2":  (370, 350),
    "品質検査":     (540, 260),
    "出荷ゾーン":   (700, 420),
    "充電ステーション": (80, 120),
}
STATION_LIST = list(FACTORY_STATIONS.values())
STATION_NAMES = list(FACTORY_STATIONS.keys())
AGV_COLORS_HEX = ["#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff", "#bf5af2"]

def init_agvs_factory():
    agvs = []
    used = set()
    charge_pos = FACTORY_STATIONS["充電ステーション"]
    for i in range(5):
        # スタートを充電ステーション付近に分散
        sx = charge_pos[0] + (i - 2) * 30
        sy = charge_pos[1] + random.randint(-20, 20)
        sx = max(50, min(FACTORY_W-50, sx))
        sy = max(50, min(FACTORY_H-50, sy))
        goal_idx = random.randint(0, len(STATION_LIST)-1)
        agvs.append({
            "id": i+1,
            "x": float(sx), "y": float(sy),
            "gx": float(STATION_LIST[goal_idx][0]),
            "gy": float(STATION_LIST[goal_idx][1]),
            "goal_name": STATION_NAMES[goal_idx],
            "color": AGV_COLORS_HEX[i],
            "speed": 3.0 + random.uniform(-0.5, 1.0),
            "dwell": 0,
            "wait": 0,
            "status": "run",  # run / dwell / wait
        })
    return agvs

if st.session_state.agv_state is None:
    st.session_state.agv_state = init_agvs_factory()

def agv_step(agvs):
    """AGV 1ステップ更新（衝突回避付き）"""
    # 予定移動先を計算
    proposals = []
    for agv in agvs:
        if agv["dwell"] > 0:
            agv["dwell"] -= 1
            if agv["dwell"] == 0:
                agv["status"] = "run"
                # 次のゴールを SA 的にランダム選択（距離をコストとして重み付け）
                dists = [math.hypot(agv["x"]-s[0], agv["y"]-s[1]) for s in STATION_LIST]
                # SAで選択（近いほどコストが低いが，時々遠くも選ぶ）
                idx = sa_select(dists, n_iter=100, seed=None)
                agv["gx"] = float(STATION_LIST[idx][0])
                agv["gy"] = float(STATION_LIST[idx][1])
                agv["goal_name"] = STATION_NAMES[idx]
            proposals.append((agv["x"], agv["y"]))
            continue

        if agv["wait"] > 0:
            agv["wait"] -= 1
            agv["status"] = "wait" if agv["wait"] > 0 else "run"
            proposals.append((agv["x"], agv["y"]))
            continue

        dx = agv["gx"] - agv["x"]; dy = agv["gy"] - agv["y"]
        dist = math.hypot(dx, dy)
        if dist < agv["speed"] + 1:
            proposals.append((agv["gx"], agv["gy"]))
        else:
            nx = agv["x"] + agv["speed"] * dx / dist
            ny = agv["y"] + agv["speed"] * dy / dist
            proposals.append((nx, ny))

    # 衝突チェック
    claimed = {}
    for i, agv in enumerate(agvs):
        px, py = proposals[i]
        collide = False
        for j, agv2 in enumerate(agvs):
            if i == j: continue
            ex, ey = proposals[j]
            if math.hypot(px-ex, py-ey) < 28:
                collide = True; break
        if collide:
            agv["wait"] = random.randint(1, 3)
            agv["status"] = "wait"
        else:
            agv["x"] = px; agv["y"] = py
            # ゴール到達チェック
            if math.hypot(agv["x"]-agv["gx"], agv["y"]-agv["gy"]) < agv["speed"]+1:
                agv["x"] = agv["gx"]; agv["y"] = agv["gy"]
                agv["dwell"] = random.randint(5, 15)
                agv["status"] = "dwell"
    return agvs

# ═══════════════════════════════════════════
# ── メインレイアウト ──
# ═══════════════════════════════════════════
left, right = st.columns([1.15, 1.15], gap="large")

# ─────────────────────────────────────────
# 左：複数トラック シミュレーション
# ─────────────────────────────────────────
with left:
    st.markdown('<div class="card"><div class="card-title">🌍 複数トラック動的迂回シミュレーション（SA最適化）</div><div class="hrline"></div>', unsafe_allow_html=True)

    # トラックステータス表示
    trucks = st.session_state.trucks
    if trucks:
        cols = st.columns(3)
        for i, tk in enumerate(trucks):
            badge_cls = {"run":"badge-run","reroute":"badge-reroute",
                         "done":"badge-done"}.get(tk["status"], "badge-block")
            label = {"run":"🟢 走行中","reroute":"🟡 迂回計算","done":"🔵 到着"}.get(tk["status"],"⚫")
            cols[i].markdown(
                f'<div class="badge {badge_cls}">🚛 T{tk["id"]} {label}<br>'
                f'<small>{tk["dist"]:.0f}km / {tk["time"]:.1f}h</small></div>',
                unsafe_allow_html=True)

    # 通行止め表示
    blocks = st.session_state.blocks
    tick   = st.session_state.sim_tick
    if blocks:
        st.markdown(f'<div class="badge badge-block">🚧 tick {block_tick} に通行止め発生！ 各トラックが SA 迂回ルートを計算</div>', unsafe_allow_html=True)
    elif trucks:
        remain = max(0, block_tick - tick)
        st.caption(f"⏱ tick {tick} — あと {remain} tick で通行止め発生予定")

    if debug and trucks:
        st.write(f"tick={tick} | blocks={blocks}")

    if st.session_state.sim_done:
        st.success("✅ 全トラック到着！ SA による迂回最適化が完了しました。")

    # 地図
    if st.session_state.map_html:
        components.html(st.session_state.map_html, height=420, scrolling=False)
    else:
        st.info("← サイドバーから「📍 ルート準備」→「▶ シミュ開始」を押してください")

    st.markdown("</div>", unsafe_allow_html=True)

# ─────────────────────────────────────────
# 右：工場 AGV シミュレーション（HTML Canvas）
# ─────────────────────────────────────────
with right:
    st.markdown('<div class="card"><div class="card-title">🏭 工場内 AGV シミュレーション（衝突回避）</div><div class="hrline"></div>', unsafe_allow_html=True)

    ca, cb, cc = st.columns(3)
    agv_start = ca.button("▶ AGV 開始")
    agv_stop  = cb.button("⏹ AGV 停止")
    agv_reset = cc.button("🔄 AGV リセット")

    if agv_start:  st.session_state.agv_running = True
    if agv_stop:   st.session_state.agv_running = False
    if agv_reset:
        st.session_state.agv_state   = init_agvs_factory()
        st.session_state.agv_tick    = 0
        st.session_state.agv_running = False

    if st.session_state.agv_running:
        st_autorefresh(interval=120, key="agv_refresh")
        st.session_state.agv_state = agv_step(st.session_state.agv_state)
        st.session_state.agv_tick += 1

    # AGV 状態を JSON にシリアライズして HTML キャンバスへ渡す
    agvs_json   = json.dumps(st.session_state.agv_state)
    stations_json = json.dumps([
        {"name": n, "x": float(p[0]), "y": float(p[1])}
        for n, p in FACTORY_STATIONS.items()
    ])
    tick_val = st.session_state.agv_tick

    factory_html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin:0; background:#0b0f1a; display:flex; flex-direction:column;
          align-items:center; font-family:'Segoe UI',sans-serif; }}
  canvas {{ border-radius:10px; box-shadow:0 0 30px rgba(77,150,255,0.3); }}
  .legend {{ color:#8899bb; font-size:12px; margin:6px 0 0; }}
</style>
</head>
<body>
<canvas id="c" width="{FACTORY_W}" height="{FACTORY_H}"></canvas>
<div class="legend">⏱ Time = {tick_val} &nbsp;|&nbsp;
  🟢走行 &nbsp; 🟡停車中 &nbsp; 🔴衝突回避待機</div>
<script>
const W={FACTORY_W}, H={FACTORY_H};
const agvs  = {agvs_json};
const stats = {stations_json};
const c = document.getElementById('c');
const ctx = c.getContext('2d');

// ── 背景：工場フロア ──
function drawFactory() {{
  // 床
  const floorGrad = ctx.createLinearGradient(0,0,0,H);
  floorGrad.addColorStop(0,'#111827');
  floorGrad.addColorStop(1,'#0d1520');
  ctx.fillStyle = floorGrad;
  ctx.fillRect(0,0,W,H);

  // グリッドライン（薄）
  ctx.strokeStyle='rgba(77,150,255,0.06)'; ctx.lineWidth=1;
  for(let x=0;x<W;x+=40){{ ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke(); }}
  for(let y=0;y<H;y+=40){{ ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke(); }}

  // 搬送路レーン（黄色破線）
  ctx.setLineDash([14,8]); ctx.strokeStyle='rgba(255,217,61,0.25)'; ctx.lineWidth=18;
  ctx.beginPath();
  // メインレーン：横
  ctx.moveTo(50,420); ctx.lineTo(750,420);
  // 縦レーン
  ctx.moveTo(200,420); ctx.lineTo(200,120);
  ctx.moveTo(540,420); ctx.lineTo(540,120);
  // 中間レーン
  ctx.moveTo(200,260); ctx.lineTo(700,260);
  ctx.stroke();
  ctx.setLineDash([]);

  // 棚（ライトブルーの矩形群）
  const shelves = [
    [280,80,80,80],[380,80,80,80],[480,80,80,80],
    [280,160,80,80],[480,160,80,80],
  ];
  shelves.forEach(([sx,sy,sw,sh])=>{{
    const g=ctx.createLinearGradient(sx,sy,sx,sy+sh);
    g.addColorStop(0,'#1a3a5c'); g.addColorStop(1,'#0f2540');
    ctx.fillStyle=g;
    ctx.strokeStyle='rgba(77,150,255,0.3)'; ctx.lineWidth=1.5;
    ctx.beginPath(); ctx.roundRect(sx,sy,sw,sh,4);
    ctx.fill(); ctx.stroke();
    // 棚板
    for(let r=1;r<4;r++){{
      ctx.strokeStyle='rgba(77,150,255,0.15)'; ctx.lineWidth=1;
      ctx.beginPath(); ctx.moveTo(sx+4,sy+sh*r/4); ctx.lineTo(sx+sw-4,sy+sh*r/4); ctx.stroke();
    }}
    // ラベル
    ctx.fillStyle='rgba(77,150,255,0.5)'; ctx.font='bold 9px monospace';
    ctx.fillText('SHELF',sx+12,sy+sh/2+4);
  }});

  // 壁
  ctx.strokeStyle='rgba(255,255,255,0.12)'; ctx.lineWidth=6;
  ctx.strokeRect(3,3,W-6,H-6);
  ctx.strokeStyle='rgba(255,255,255,0.05)'; ctx.lineWidth=2;
  ctx.strokeRect(10,10,W-20,H-20);
}}

// ── ステーション描画 ──
function drawStations() {{
  stats.forEach(s=>{{
    // 光るリング
    const grd=ctx.createRadialGradient(s.x,s.y,4,s.x,s.y,28);
    grd.addColorStop(0,'rgba(77,150,255,0.25)');
    grd.addColorStop(1,'rgba(77,150,255,0)');
    ctx.fillStyle=grd; ctx.beginPath(); ctx.arc(s.x,s.y,28,0,Math.PI*2); ctx.fill();

    // 四角マーカー
    ctx.fillStyle='#1a3060'; ctx.strokeStyle='#4d96ff'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.roundRect(s.x-20,s.y-14,40,28,5); ctx.fill(); ctx.stroke();

    // テキスト
    ctx.fillStyle='#aac4ff'; ctx.font='bold 9px "Segoe UI"'; ctx.textAlign='center';
    const label = s.name.length > 6 ? s.name.slice(0,6)+'…' : s.name;
    ctx.fillText(label,s.x,s.y+4);
  }});
}}

// ── AGV 描画 ──
function drawAGVs() {{
  agvs.forEach(agv=>{{
    const x=agv.x, y=agv.y;
    const col=agv.color;

    // 影
    ctx.shadowColor=col; ctx.shadowBlur=14;

    // 本体
    const col2 = agv.status==='wait' ? '#ff4444' :
                 agv.status==='dwell' ? '#ffd93d' : col;
    ctx.fillStyle=col2;
    ctx.strokeStyle='#fff'; ctx.lineWidth=1.5;
    ctx.beginPath();
    ctx.roundRect(x-14, y-10, 28, 20, 5);
    ctx.fill(); ctx.stroke();

    ctx.shadowBlur=0;

    // ID テキスト
    ctx.fillStyle='#fff'; ctx.font='bold 11px monospace';
    ctx.textAlign='center'; ctx.textBaseline='middle';
    ctx.fillText('T'+agv.id, x, y);

    // ゴールへの矢印（薄）
    ctx.strokeStyle=col+'55'; ctx.lineWidth=1; ctx.setLineDash([4,6]);
    ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(agv.gx,agv.gy); ctx.stroke();
    ctx.setLineDash([]);

    // ゴールマーカー（小さい星型）
    ctx.fillStyle=col+'88';
    ctx.beginPath(); ctx.arc(agv.gx,agv.gy,4,0,Math.PI*2); ctx.fill();
  }});
}}

drawFactory();
drawStations();
drawAGVs();
</script>
</body>
</html>
"""

    components.html(factory_html, height=FACTORY_H + 60, scrolling=False)

    # ステータス一覧
    agv_cols = st.columns(5)
    for i, agv in enumerate(st.session_state.agv_state):
        status_icon = {"run":"🟢","dwell":"🟡","wait":"🔴"}.get(agv["status"],"⚫")
        agv_cols[i].markdown(
            f"<small style='color:{agv['color']}'><b>AGV{agv['id']}</b> {status_icon}<br>"
            f"{agv['goal_name'][:6]}</small>",
            unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

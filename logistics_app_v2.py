"""
logistics_app_v3_stable.py
Streamlit 1.57 / Python 3.14 対応・安定版
修正:
  1. time.sleep + st.rerun の無限ループ → st_autorefresh に戻す（正しい使い方）
  2. st.components.v1.html → st.iframe に統一
  3. use_container_width → width='stretch' に統一
  4. ⏱ 絵文字をタイトル文字列から除去（フォント欠落警告対策）
  5. st.rerun() はボタン処理後のみ使用
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
st.set_page_config(page_title="物流xAGV最適化 v3", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap');
* { font-family:'Noto Sans JP',sans-serif; box-sizing:border-box; }
.stApp { background:#0b0f1a; }
.block-container { padding-top:1rem; padding-bottom:2rem; max-width:1600px; }
.hero { text-align:center; padding:16px 0 12px; }
.hero h1 {
  font-size:32px; font-weight:900; margin:0;
  background:linear-gradient(90deg,#ff6b6b,#ffd93d,#6bcb77,#4d96ff);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
}
.hero p { color:#8899bb; font-size:13px; margin:6px 0 0; }
.card {
  background:rgba(255,255,255,0.03);
  border:1px solid rgba(255,255,255,0.08);
  border-radius:14px; padding:16px;
  box-shadow:0 8px 32px rgba(0,0,0,0.5); color:#dde;
}
.card-title { font-size:16px; font-weight:900; color:#4d96ff; margin:0 0 8px; }
.hrline { height:2px; border-radius:2px; margin:0 0 12px;
          background:linear-gradient(90deg,#ff6b6b,#4d96ff); opacity:.7; }
.stButton>button {
  border-radius:8px; padding:.55rem .8rem; font-weight:900;
  border:none; width:100%;
  background:linear-gradient(90deg,#ff6b6b,#4d96ff);
  color:#fff; box-shadow:0 4px 14px rgba(0,0,0,0.4);
}
.stButton>button:hover { opacity:.85; }
section[data-testid="stSidebar"]>div {
  background:rgba(10,14,28,0.97);
  border-right:1px solid rgba(255,255,255,0.06);
}
label,.stCheckbox label,.stSlider label { color:#8899bb !important; }
.badge { display:inline-block; border-radius:6px; padding:3px 10px;
         font-size:12px; font-weight:700; margin:2px; }
.badge-run    { background:#1a3a1a; color:#6bcb77; border:1px solid #6bcb77; }
.badge-reroute{ background:#3a2a00; color:#ffd93d; border:1px solid #ffd93d; }
.badge-block  { background:#3a0a0a; color:#ff6b6b; border:1px solid #ff6b6b; }
.badge-done   { background:#0a1a3a; color:#4d96ff; border:1px solid #4d96ff; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="hero">
  <h1>物流 x AGV 量子インスパイア最適化 v3</h1>
  <p>複数トラック動的迂回（焼きなまし法 SA） + 工場内AGV5台 リアルタイムシミュレーション</p>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════
# Session State
# ═══════════════════════════════════════════
_DEF = {
    "map_html": None, "route_df": None,
    "trucks": None, "blocks": None,
    "sim_tick": 0, "sim_running": False, "sim_done": False,
    "base_route": None,
    "o_latlon": None, "d_latlon": None,
    "o_addr": "", "d_addr": "",
    "api_key_cache": "",
    "radius_km_cache": 5,
    "due_time_cache": 24,
    "block_tick_cache": 8,
    "agv_running": False,
    "agv_tick": 0,
    "agv_state": None,
}
for k, v in _DEF.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ═══════════════════════════════════════════
# ルーティング関数
# ═══════════════════════════════════════════
def geocode(addr, jp_only=True):
    geo = Nominatim(user_agent="logistics-v3-stable")
    loc = geo.geocode(addr, country_codes="jp" if jp_only else None, exactly_one=True)
    if loc is None:
        raise ValueError(f"住所が見つかりません: {addr}")
    return loc.latitude, loc.longitude, loc.address

def _ors(api_key, coords, timeout=18):
    if not api_key:
        return {}
    try:
        r = requests.post(
            "https://api.openrouteservice.org/v2/directions/driving-car/geojson",
            json={"coordinates": coords},
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            verify=False, timeout=timeout)
        return r.json()
    except Exception:
        return {}

def _osrm(coords, timeout=18):
    try:
        s = ";".join(f"{lon},{lat}" for lon, lat in coords)
        r = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{s}",
            params={"overview": "full", "geometries": "geojson"}, timeout=timeout)
        return r.json()
    except Exception:
        return {}

def extract_route(gj):
    feats = gj.get("features")
    if isinstance(feats, list) and feats:
        coords = feats[0].get("geometry", {}).get("coordinates", [])
        summ   = feats[0].get("properties", {}).get("summary", {})
        if len(coords) >= 2:
            return [[p[1],p[0]] for p in coords if len(p)>=2], \
                   summ.get("distance",0)/1000, summ.get("duration",0)/3600
    routes = gj.get("routes")
    if isinstance(routes, list) and routes:
        coords = routes[0].get("geometry", {}).get("coordinates", [])
        if len(coords) >= 2:
            return [[p[1],p[0]] for p in coords if len(p)>=2], \
                   routes[0].get("distance",0)/1000, routes[0].get("duration",0)/3600
    return None, None, None

def get_route(api_key, coords):
    route, d, t = extract_route(_ors(api_key, coords))
    if route:
        return route, d, t, "ORS"
    route, d, t = extract_route(_osrm(coords))
    if route:
        return route, d, t, "OSRM"
    return None, None, None, None

def violates(route, centers, radius_km, margin=0.3):
    thr = radius_km + margin
    for lat, lon in route:
        for c in centers:
            if geodesic((lat,lon), c).km <= thr:
                return True
    return False

def pick_blocks(base_route, n=2, min_sep=20):
    if not base_route or len(base_route) < 6:
        return None
    N = len(base_route); m = max(3, N//8)
    inner = list(range(m, N-m))
    sep = float(min_sep)
    while sep >= 1:
        for _ in range(80):
            if len(inner) < n: break
            idxs = sorted(random.sample(inner, n))
            pts  = [tuple(base_route[i]) for i in idxs]
            if all(geodesic(pts[i],pts[j]).km >= sep
                   for i in range(n) for j in range(i+1,n)):
                return pts
        sep /= 2
    step = max(1, N//(n+1))
    return [tuple(base_route[min(step*(i+1),N-1)]) for i in range(n)]

def gen_waypoints(blocks, radius_km, o_ll, d_ll, n_candidates=30):
    muls = [2, 3.5, 6, 10, 16]
    wps  = []
    for c_lat, c_lon in blocks:
        for mul in muls:
            for ang in np.linspace(0, 360, 10, endpoint=False):
                dest = geo_distance(kilometers=radius_km*mul).destination((c_lat,c_lon), ang)
                wps.append((dest.latitude, dest.longitude))
    o_lat,o_lon = o_ll; d_lat,d_lon = d_ll
    for t in np.linspace(0.2, 0.8, 6):
        mlat=o_lat+t*(d_lat-o_lat); mlon=o_lon+t*(d_lon-o_lon)
        for ang in np.linspace(0, 360, 8, endpoint=False):
            dest = geo_distance(kilometers=radius_km*5).destination((mlat,mlon), ang)
            wps.append((dest.latitude, dest.longitude))
    seen,uniq = set(),[]
    for lat,lon in wps:
        k=(round(lat,3),round(lon,3))
        if k not in seen: seen.add(k); uniq.append((lat,lon))
    random.shuffle(uniq)
    return uniq[:n_candidates]

def sa_select(costs, n_iter=1200, seed=None):
    rnd = random.Random(seed)
    n   = len(costs)
    if n == 0: return None
    if n == 1: return 0
    cur = best = rnd.randrange(n); T = 1.0
    for _ in range(n_iter):
        nxt = rnd.randrange(n)
        d   = costs[nxt]-costs[cur]
        if d < 0 or rnd.random() < math.exp(-d/max(T,1e-9)):
            cur = nxt
        if costs[cur] < costs[best]: best = cur
        T *= 0.994
    return best

def find_detour(api_key, o_ll, d_ll, blocks, radius_km, due_time):
    o_lat,o_lon = o_ll; d_lat,d_lon = d_ll
    wps = gen_waypoints(blocks, radius_km, o_ll, d_ll)
    candidates = [("BASE", [[o_lon,o_lat],[d_lon,d_lat]])]
    for i,(lat,lon) in enumerate(wps):
        candidates.append((f"WP{i+1}", [[o_lon,o_lat],[lon,lat],[d_lon,d_lat]]))
    feasible,loose = [],[]
    for tag,coords in candidates:
        route,dist,time_,_ = get_route(api_key, coords)
        if not route: continue
        cost  = dist*120 + max(0,time_-due_time)*5000
        entry = {"tag":tag,"route":route,"dist":dist,"time":time_,"cost":cost}
        (loose if violates(route,blocks,radius_km) else feasible).append(entry)
    pool = feasible if feasible else loose
    if not pool: return None
    idx = sa_select([e["cost"] for e in pool])
    return pool[idx]

TRUCK_COLORS = ["#ff6b6b","#ffd93d","#6bcb77"]

def build_map(o_ll, d_ll, trucks, blocks, radius_km, base_route):
    o_lat,o_lon = o_ll; d_lat,d_lon = d_ll
    m = folium.Map(location=[(o_lat+d_lat)/2,(o_lon+d_lon)/2],
                   zoom_start=6, tiles="CartoDB DarkMatter")
    if base_route:
        folium.PolyLine(base_route, color="#555", weight=3,
                        dash_array="6,6", tooltip="通常ルート").add_to(m)
    for tk in (trucks or []):
        if not tk.get("route"): continue
        if tk["status"] in ("reroute","done"):
            AntPath(tk["route"], color=tk["color"], weight=5,
                    delay=800, tooltip=f"T{tk['id']} 迂回中").add_to(m)
        else:
            folium.PolyLine(tk["route"], color=tk["color"], weight=5,
                            opacity=0.85, tooltip=f"T{tk['id']}").add_to(m)
        prog = min(tk.get("progress",0), len(tk["route"])-1)
        pos  = tk["route"][prog]
        folium.CircleMarker(pos, radius=10, color=tk["color"],
            fill=True, fill_opacity=1,
            tooltip=f"T{tk['id']} [{tk['status']}]").add_to(m)
    if blocks:
        for b_lat,b_lon in blocks:
            folium.Circle([b_lat,b_lon], radius=radius_km*1000,
                color="#ff4444", fill=True, fill_opacity=0.25,
                tooltip="通行止め").add_to(m)
            folium.Marker([b_lat,b_lon],
                icon=folium.Icon(icon="ban", prefix="fa", color="red"),
                tooltip="通行止め").add_to(m)
    folium.Marker([o_lat,o_lon],
        icon=folium.Icon(icon="play", prefix="fa", color="blue"),
        tooltip="出発").add_to(m)
    folium.Marker([d_lat,d_lon],
        icon=folium.Icon(icon="flag", prefix="fa", color="darkgreen"),
        tooltip="到着").add_to(m)
    return m.get_root().render()

# ═══════════════════════════════════════════
# 工場 AGV 定義
# ═══════════════════════════════════════════
FW, FH = 760, 440
FACTORY_STATIONS = {
    "入荷ゾーン":       (70,  400),
    "検品台A":          (190, 310),
    "組立ライン1":      (360, 190),
    "組立ライン2":      (360, 340),
    "品質検査":         (530, 250),
    "出荷ゾーン":       (680, 400),
    "充電ステーション": (70,  110),
}
STATION_LIST  = list(FACTORY_STATIONS.values())
STATION_NAMES = list(FACTORY_STATIONS.keys())
AGV_COLORS    = ["#ff6b6b","#ffd93d","#6bcb77","#4d96ff","#bf5af2"]

def init_agvs():
    agvs = []
    cx,cy = FACTORY_STATIONS["充電ステーション"]
    for i in range(5):
        sx = cx+(i-2)*35; sy = cy+random.randint(-15,15)
        sx = max(40,min(FW-40,sx)); sy = max(40,min(FH-40,sy))
        gi = random.randint(0, len(STATION_LIST)-1)
        agvs.append({"id":i+1,"x":float(sx),"y":float(sy),
                     "gx":float(STATION_LIST[gi][0]),"gy":float(STATION_LIST[gi][1]),
                     "goal_name":STATION_NAMES[gi],"color":AGV_COLORS[i],
                     "speed":3.0+random.uniform(-0.5,1.2),
                     "dwell":0,"wait":0,"status":"run"})
    return agvs

if st.session_state.agv_state is None:
    st.session_state.agv_state = init_agvs()

def agv_step(agvs):
    proposals = []
    for agv in agvs:
        if agv["dwell"] > 0:
            agv["dwell"] -= 1
            if agv["dwell"] == 0:
                agv["status"] = "run"
                dists = [math.hypot(agv["x"]-s[0],agv["y"]-s[1]) for s in STATION_LIST]
                idx   = sa_select(dists, n_iter=80)
                agv["gx"]=float(STATION_LIST[idx][0]); agv["gy"]=float(STATION_LIST[idx][1])
                agv["goal_name"]=STATION_NAMES[idx]
            proposals.append((agv["x"],agv["y"])); continue
        if agv["wait"] > 0:
            agv["wait"] -= 1
            agv["status"]="wait" if agv["wait"]>0 else "run"
            proposals.append((agv["x"],agv["y"])); continue
        dx=agv["gx"]-agv["x"]; dy=agv["gy"]-agv["y"]
        dist=math.hypot(dx,dy)
        if dist < agv["speed"]+1:
            proposals.append((agv["gx"],agv["gy"]))
        else:
            proposals.append((agv["x"]+agv["speed"]*dx/dist,
                               agv["y"]+agv["speed"]*dy/dist))
    for i,agv in enumerate(agvs):
        px,py=proposals[i]
        collide=any(i!=j and math.hypot(px-proposals[j][0],py-proposals[j][1])<30
                    for j in range(len(agvs)))
        if collide:
            agv["wait"]=random.randint(1,3); agv["status"]="wait"
        else:
            agv["x"]=px; agv["y"]=py
            if math.hypot(agv["x"]-agv["gx"],agv["y"]-agv["gy"])<agv["speed"]+1:
                agv["x"]=agv["gx"]; agv["y"]=agv["gy"]
                agv["dwell"]=random.randint(6,16); agv["status"]="dwell"
    return agvs

# ═══════════════════════════════════════════
# サイドバー
# ═══════════════════════════════════════════
with st.sidebar:
    st.markdown("## 設定")
    st.markdown("---")
    api_key   = st.text_input("ORS API Key（任意）", type="password")
    st.caption("未入力時は OSRM（無料）で陸路計算します。")
    origin    = st.text_input("輸送元", "大阪府堺市")
    dest_addr = st.text_input("輸送先", "山口県下関市")
    due_time  = st.slider("納期（時間）", 5, 72, 24)
    jp_only   = st.checkbox("日本国内モード", value=True)
    st.markdown("### 通行止め設定")
    radius_km  = st.slider("影響半径 (km)", 1, 20, 5)
    min_sep_km = st.slider("2点間の最小間隔 (km)", 5, 80, 20)
    block_tick = st.slider("通行止め発生タイミング（tick）", 3, 20, 8)
    st.markdown("### シミュレーション速度")
    sim_speed  = st.slider("速度（ms/tick）", 300, 2000, 800)
    debug      = st.checkbox("デバッグ表示", value=False)
    st.markdown("---")
    c1,c2 = st.columns(2)
    btn_init  = c1.button("ルート準備")
    btn_start = c2.button("開始")
    btn_stop  = st.button("停止 / リセット")

# ═══════════════════════════════════════════
# ボタン処理
# ═══════════════════════════════════════════
if btn_stop:
    st.session_state.sim_running = False
    st.session_state.sim_done    = False
    st.session_state.sim_tick    = 0
    st.session_state.trucks      = None
    st.session_state.blocks      = None
    st.session_state.map_html    = None
    st.session_state.base_route  = None

if btn_init:
    with st.spinner("ルート取得中..."):
        try:
            o_lat,o_lon,o_addr = geocode(origin, jp_only=jp_only)
            d_lat,d_lon,d_addr = geocode(dest_addr, jp_only=jp_only)
            coords_od = [[o_lon,o_lat],[d_lon,d_lat]]
            base_route,bd,bt,src = get_route(api_key, coords_od)
            if base_route is None:
                st.error("ルートを取得できませんでした。住所を確認してください。")
            else:
                if src == "OSRM":
                    st.info("ORS 未使用：OSRM で陸路を計算しています。")
                n = len(base_route)
                trucks = [{"id":i+1,"color":TRUCK_COLORS[i],"route":base_route,
                           "progress":i*max(1,n//6),"status":"run","dist":bd,"time":bt}
                          for i in range(3)]
                st.session_state.update({
                    "trucks":trucks,"base_route":base_route,
                    "o_latlon":(o_lat,o_lon),"d_latlon":(d_lat,d_lon),
                    "o_addr":o_addr,"d_addr":d_addr,
                    "blocks":None,"sim_tick":0,
                    "sim_running":False,"sim_done":False,
                    "api_key_cache":api_key,"radius_km_cache":radius_km,
                    "due_time_cache":due_time,"block_tick_cache":block_tick,
                })
                st.session_state.map_html = build_map(
                    (o_lat,o_lon),(d_lat,d_lon), trucks, None, radius_km, base_route)
                st.success(f"準備完了：{o_addr} -> {d_addr}　'開始' を押してください")
        except Exception as e:
            st.error(f"エラー: {e}")

if btn_start and st.session_state.trucks:
    st.session_state.sim_running = True
    st.session_state.sim_done    = False

# ═══════════════════════════════════════════
# autorefresh（正しい使い方：条件付きで1回だけ呼ぶ）
# ═══════════════════════════════════════════
if st.session_state.sim_running and not st.session_state.sim_done:
    st_autorefresh(interval=sim_speed, key="sim_refresh")

if st.session_state.agv_running:
    st_autorefresh(interval=150, key="agv_refresh")

# ═══════════════════════════════════════════
# シミュレーション tick 処理
# ═══════════════════════════════════════════
if st.session_state.sim_running and not st.session_state.sim_done:
    tick   = st.session_state.sim_tick + 1
    trucks = st.session_state.trucks or []
    blocks = st.session_state.blocks
    o_ll   = st.session_state.o_latlon
    d_ll   = st.session_state.d_latlon
    ak     = st.session_state.api_key_cache
    rkm    = st.session_state.radius_km_cache
    dtm    = st.session_state.due_time_cache
    btk    = st.session_state.block_tick_cache

    # 通行止め発生
    if tick == btk and blocks is None and st.session_state.base_route:
        blocks = pick_blocks(st.session_state.base_route, min_sep=min_sep_km)
        st.session_state.blocks = blocks
        if blocks:
            for tk in trucks:
                if tk["status"] == "run":
                    tk["status"] = "reroute"

    # 各トラック更新
    all_done = True
    for tk in trucks:
        if tk["status"] == "done": continue
        all_done = False
        if tk["status"] == "reroute" and blocks:
            prog    = min(tk.get("progress",0), len(tk["route"])-1)
            cur_pos = tk["route"][prog]
            result  = find_detour(ak,(cur_pos[0],cur_pos[1]),d_ll,blocks,rkm,dtm)
            if result:
                tk.update({"route":result["route"],"dist":result["dist"],
                           "time":result["time"],"progress":0})
            tk["status"] = "run"
        step = max(1, len(tk["route"])//40)
        tk["progress"] = min(tk["progress"]+step, len(tk["route"])-1)
        if tk["progress"] >= len(tk["route"])-1:
            tk["status"] = "done"

    if all_done:
        st.session_state.sim_done    = True
        st.session_state.sim_running = False

    st.session_state.trucks   = trucks
    st.session_state.sim_tick = tick
    if o_ll and d_ll:
        st.session_state.map_html = build_map(
            o_ll, d_ll, trucks, blocks, rkm, st.session_state.base_route)

# AGV tick
if st.session_state.agv_running:
    st.session_state.agv_state = agv_step(st.session_state.agv_state)
    st.session_state.agv_tick += 1

# ═══════════════════════════════════════════
# メインレイアウト
# ═══════════════════════════════════════════
left, right = st.columns([1.15, 1.15], gap="large")

# ── 左：物流 ──
with left:
    st.markdown('<div class="card"><div class="card-title">複数トラック動的迂回シミュレーション（SA最適化）</div><div class="hrline"></div>', unsafe_allow_html=True)

    trucks = st.session_state.trucks
    if trucks:
        cols = st.columns(3)
        for i,tk in enumerate(trucks):
            badge_cls = {"run":"badge-run","reroute":"badge-reroute",
                         "done":"badge-done"}.get(tk["status"],"badge-block")
            label = {"run":"走行中","reroute":"迂回計算中",
                     "done":"到着済み"}.get(tk["status"],"---")
            cols[i].markdown(
                f'<div class="badge {badge_cls}">T{tk["id"]} {label}<br>'
                f'<small>{tk.get("dist",0):.0f}km / {tk.get("time",0):.1f}h</small></div>',
                unsafe_allow_html=True)

    blocks = st.session_state.blocks
    tick   = st.session_state.sim_tick
    btk    = st.session_state.get("block_tick_cache", 8)
    if blocks:
        st.markdown(f'<div class="badge badge-block">通行止め発生（tick {btk}）- 各トラックがSAで迂回ルートを個別計算</div>', unsafe_allow_html=True)
    elif trucks:
        st.caption(f"tick {tick} - あと {max(0, btk-tick)} tick で通行止め発生予定")

    if debug and trucks:
        st.write({"tick":tick,"blocks":blocks,
                  "trucks":[{k:v for k,v in t.items() if k!="route"} for t in trucks]})

    if st.session_state.sim_done:
        st.success("全トラック到着！SA による動的迂回最適化デモ完了")

    if st.session_state.map_html:
        st.iframe(st.session_state.map_html, height=420, width="stretch")
    else:
        st.info("サイドバーから「ルート準備」→「開始」を押してください")

    st.markdown("</div>", unsafe_allow_html=True)

# ── 右：AGV ──
with right:
    st.markdown('<div class="card"><div class="card-title">工場内 AGV シミュレーション（SA衝突回避）</div><div class="hrline"></div>', unsafe_allow_html=True)

    ca,cb,cc = st.columns(3)
    if ca.button("AGV 開始"): st.session_state.agv_running = True
    if cb.button("AGV 停止"): st.session_state.agv_running = False
    if cc.button("AGV リセット"):
        st.session_state.agv_state   = init_agvs()
        st.session_state.agv_tick    = 0
        st.session_state.agv_running = False

    agvs_j  = json.dumps(st.session_state.agv_state)
    stats_j = json.dumps([{"name":n,"x":float(p[0]),"y":float(p[1])}
                           for n,p in FACTORY_STATIONS.items()])
    tick_v  = st.session_state.agv_tick

    factory_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body{{margin:0;background:#0b0f1a;display:flex;flex-direction:column;
        align-items:center;font-family:'Segoe UI',sans-serif;}}
  canvas{{border-radius:10px;box-shadow:0 0 28px rgba(77,150,255,0.3);display:block;}}
  .info{{color:#8899bb;font-size:11px;margin:5px 0 2px;}}
</style></head>
<body>
<canvas id="c" width="{FW}" height="{FH}"></canvas>
<div class="info">Time = {tick_v} | 緑=走行 黄=停車中 赤=衝突回避待機</div>
<script>
const W={FW},H={FH};
const agvs=JSON.parse('{agvs_j.replace("'", "\\'")}');
const stats=JSON.parse('{stats_j.replace("'", "\\'")}');
const cv=document.getElementById('c');
const ctx=cv.getContext('2d');

function rrect(x,y,w,h,r){{
  ctx.beginPath();
  ctx.moveTo(x+r,y);ctx.lineTo(x+w-r,y);
  ctx.quadraticCurveTo(x+w,y,x+w,y+r);
  ctx.lineTo(x+w,y+h-r);
  ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h);
  ctx.lineTo(x+r,y+h);
  ctx.quadraticCurveTo(x,y+h,x,y+h-r);
  ctx.lineTo(x,y+r);
  ctx.quadraticCurveTo(x,y,x+r,y);
  ctx.closePath();
}}

// 床
const g=ctx.createLinearGradient(0,0,0,H);
g.addColorStop(0,'#111827');g.addColorStop(1,'#0d1520');
ctx.fillStyle=g;ctx.fillRect(0,0,W,H);

// グリッド
ctx.strokeStyle='rgba(77,150,255,0.06)';ctx.lineWidth=1;
for(let x=0;x<W;x+=40){{ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}}
for(let y=0;y<H;y+=40){{ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}}

// 搬送路レーン
ctx.setLineDash([14,8]);ctx.strokeStyle='rgba(255,217,61,0.2)';ctx.lineWidth=22;
ctx.beginPath();
ctx.moveTo(50,400);ctx.lineTo(720,400);
ctx.moveTo(190,400);ctx.lineTo(190,110);
ctx.moveTo(530,400);ctx.lineTo(530,110);
ctx.moveTo(190,250);ctx.lineTo(680,250);
ctx.stroke();ctx.setLineDash([]);

// 棚
[[280,70,80,80],[380,70,80,80],[480,70,80,80],
 [280,160,80,80],[480,160,80,80]].forEach(([sx,sy,sw,sh])=>{{
  const sg=ctx.createLinearGradient(sx,sy,sx,sy+sh);
  sg.addColorStop(0,'#1a3a5c');sg.addColorStop(1,'#0f2540');
  ctx.fillStyle=sg;ctx.strokeStyle='rgba(77,150,255,0.3)';ctx.lineWidth=1.5;
  rrect(sx,sy,sw,sh,4);ctx.fill();ctx.stroke();
  ctx.fillStyle='rgba(77,150,255,0.4)';ctx.font='bold 9px monospace';
  ctx.textAlign='center';ctx.fillText('SHELF',sx+sw/2,sy+sh/2+4);
}});

// 壁
ctx.strokeStyle='rgba(255,255,255,0.1)';ctx.lineWidth=5;
ctx.strokeRect(3,3,W-6,H-6);

// ステーション
stats.forEach(s=>{{
  const rg=ctx.createRadialGradient(s.x,s.y,4,s.x,s.y,26);
  rg.addColorStop(0,'rgba(77,150,255,0.3)');
  rg.addColorStop(1,'rgba(77,150,255,0)');
  ctx.fillStyle=rg;ctx.beginPath();ctx.arc(s.x,s.y,26,0,Math.PI*2);ctx.fill();
  ctx.fillStyle='#1a3060';ctx.strokeStyle='#4d96ff';ctx.lineWidth=2;
  rrect(s.x-22,s.y-14,44,28,5);ctx.fill();ctx.stroke();
  ctx.fillStyle='#aac4ff';ctx.font='bold 8px "Segoe UI"';ctx.textAlign='center';
  const lb=s.name.length>6?s.name.slice(0,6)+'...':s.name;
  ctx.fillText(lb,s.x,s.y+4);
}});

// AGV
agvs.forEach(agv=>{{
  const x=agv.x,y=agv.y;
  const bodyCol = agv.status==='wait'?'#ff4444':agv.status==='dwell'?'#ffd93d':agv.color;
  ctx.shadowColor=bodyCol;ctx.shadowBlur=12;
  ctx.fillStyle=bodyCol;ctx.strokeStyle='rgba(255,255,255,0.8)';ctx.lineWidth=1.5;
  rrect(x-14,y-10,28,20,4);ctx.fill();ctx.stroke();
  ctx.shadowBlur=0;
  ctx.fillStyle='#fff';ctx.font='bold 10px monospace';
  ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.fillText('T'+agv.id,x,y);
  ctx.strokeStyle=agv.color+'44';ctx.lineWidth=1;ctx.setLineDash([4,6]);
  ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(agv.gx,agv.gy);ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle=agv.color+'99';
  ctx.beginPath();ctx.arc(agv.gx,agv.gy,4,0,Math.PI*2);ctx.fill();
}});
ctx.textBaseline='alphabetic';
</script></body></html>"""

    st.iframe(factory_html, height=FH+55, width="stretch")

    agv_cols = st.columns(5)
    status_icon = {"run":"●","dwell":"■","wait":"✕"}
    for i,agv in enumerate(st.session_state.agv_state):
        icon = status_icon.get(agv["status"],"?")
        agv_cols[i].markdown(
            f"<small style='color:{agv['color']}'><b>AGV{agv['id']}</b> {icon}<br>"
            f"{agv['goal_name'][:5]}</small>",
            unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

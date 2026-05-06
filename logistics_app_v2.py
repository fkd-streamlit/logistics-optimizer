"""
logistics_app_v2.py  -- メモリ最適化版 --
OOM (Out of Memory) 対策:
  1. folium地図はルート変化時のみ再生成（map_dirty フラグ）
  2. st_autorefresh を1つに統合（2重rerunを防止）
  3. AGV は純JS requestAnimationFrame（Pythonのrerun不要）
  4. @st.cache_data でOSRM/ORSをキャッシュ
  5. ルート点数を最大200点に間引き
"""
import math, random, warnings, json
from urllib.parse import quote
import numpy as np
import requests
import folium
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.distance import geodesic, distance as geo_distance
from urllib3.exceptions import InsecureRequestWarning
from streamlit_autorefresh import st_autorefresh

warnings.simplefilter("ignore", InsecureRequestWarning)

st.set_page_config(page_title="物流xAGV最適化 v3", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap');
* { font-family:'Noto Sans JP',sans-serif; box-sizing:border-box; }
.stApp { background:#0b0f1a; }
.block-container { padding-top:1rem; padding-bottom:2rem; max-width:1600px; }
.hero { text-align:center; padding:14px 0 10px; }
.hero h1 { font-size:28px; font-weight:900; margin:0;
  background:linear-gradient(90deg,#ff6b6b,#ffd93d,#6bcb77,#4d96ff);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.hero p { color:#8899bb; font-size:12px; margin:5px 0 0; }
.card { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.08);
  border-radius:14px; padding:14px; box-shadow:0 6px 24px rgba(0,0,0,0.5); color:#dde; }
.card-title { font-size:14px; font-weight:900; color:#4d96ff; margin:0 0 8px; }
.hrline { height:2px; border-radius:2px; margin:0 0 10px;
  background:linear-gradient(90deg,#ff6b6b,#4d96ff); opacity:.7; }
.stButton>button { border-radius:8px; padding:.5rem .7rem; font-weight:900;
  border:none; width:100%; background:linear-gradient(90deg,#ff6b6b,#4d96ff);
  color:#fff; box-shadow:0 3px 12px rgba(0,0,0,0.4); }
.stButton>button:hover { opacity:.85; }
section[data-testid="stSidebar"]>div {
  background:rgba(10,14,28,0.97); border-right:1px solid rgba(255,255,255,0.06); }
label,.stCheckbox label,.stSlider label { color:#8899bb !important; }
.badge { display:inline-block; border-radius:5px; padding:2px 8px;
  font-size:11px; font-weight:700; margin:1px; }
.badge-run    { background:#1a3a1a; color:#6bcb77; border:1px solid #6bcb77; }
.badge-reroute{ background:#3a2a00; color:#ffd93d; border:1px solid #ffd93d; }
.badge-block  { background:#3a0a0a; color:#ff6b6b; border:1px solid #ff6b6b; }
.badge-done   { background:#0a1a3a; color:#4d96ff; border:1px solid #4d96ff; }
</style>""", unsafe_allow_html=True)

st.markdown('<div class="hero"><h1>物流 x AGV 量子インスパイア最適化 v3</h1>'
            '<p>複数トラック動的迂回（焼きなまし法 SA） + 工場内AGV5台 リアルタイムシミュレーション</p></div>',
            unsafe_allow_html=True)

# Session State
for k,v in {"map_html":None,"map_dirty":False,"trucks":None,"blocks":None,
             "sim_tick":0,"sim_running":False,"sim_done":False,"base_route":None,
             "o_latlon":None,"d_latlon":None,"api_key_cache":"",
             "radius_km_cache":5,"due_time_cache":24,"block_tick_cache":8,
             "agv_seed":42}.items():
    if k not in st.session_state: st.session_state[k]=v

# ── ルーティング ──
def geocode(addr, jp_only=True):
    geo = Nominatim(user_agent="logistics-v3-lite")
    loc = geo.geocode(addr, country_codes="jp" if jp_only else None, exactly_one=True)
    if loc is None: raise ValueError(f"住所が見つかりません: {addr}")
    return loc.latitude, loc.longitude, loc.address

@st.cache_data(ttl=3600, show_spinner=False)
def _osrm(coord_str):
    try:
        r = requests.get(f"https://router.project-osrm.org/route/v1/driving/{coord_str}",
                         params={"overview":"full","geometries":"geojson"}, timeout=18)
        return r.json()
    except: return {}

@st.cache_data(ttl=3600, show_spinner=False)
def _ors(api_key, coord_str):
    try:
        coords = json.loads(coord_str)
        r = requests.post("https://api.openrouteservice.org/v2/directions/driving-car/geojson",
                          json={"coordinates":coords},
                          headers={"Authorization":api_key,"Content-Type":"application/json"},
                          verify=False, timeout=18)
        return r.json()
    except: return {}

def extract_route(gj):
    feats=gj.get("features")
    if isinstance(feats,list) and feats:
        coords=feats[0].get("geometry",{}).get("coordinates",[])
        summ=feats[0].get("properties",{}).get("summary",{})
        if len(coords)>=2:
            return [[p[1],p[0]] for p in coords if len(p)>=2],\
                   summ.get("distance",0)/1000,summ.get("duration",0)/3600
    routes=gj.get("routes")
    if isinstance(routes,list) and routes:
        coords=routes[0].get("geometry",{}).get("coordinates",[])
        if len(coords)>=2:
            return [[p[1],p[0]] for p in coords if len(p)>=2],\
                   routes[0].get("distance",0)/1000,routes[0].get("duration",0)/3600
    return None,None,None

def get_route(api_key, coords):
    cs=json.dumps(coords)
    if api_key:
        r,d,t=extract_route(_ors(api_key,cs))
        if r:
            s=max(1,len(r)//200); return r[::s],d,t,"ORS"
    os_str=";".join(f"{lon},{lat}" for lon,lat in coords)
    r,d,t=extract_route(_osrm(os_str))
    if r:
        s=max(1,len(r)//200); return r[::s],d,t,"OSRM"
    return None,None,None,None

def violates(route,centers,radius_km,margin=0.3):
    thr=radius_km+margin
    for lat,lon in route:
        for c in centers:
            if geodesic((lat,lon),c).km<=thr: return True
    return False

def pick_blocks(base_route,n=2,min_sep=20):
    if not base_route or len(base_route)<6: return None
    N=len(base_route); m=max(2,N//8)
    inner=list(range(m,N-m)); sep=float(min_sep)
    while sep>=1:
        for _ in range(60):
            if len(inner)<n: break
            idxs=sorted(random.sample(inner,n))
            pts=[tuple(base_route[i]) for i in idxs]
            if all(geodesic(pts[i],pts[j]).km>=sep for i in range(n) for j in range(i+1,n)):
                return pts
        sep/=2
    step=max(1,N//(n+1))
    return [tuple(base_route[min(step*(i+1),N-1)]) for i in range(n)]

def gen_wps(blocks,rkm,o_ll,d_ll,nc=20):
    wps=[]
    for c_lat,c_lon in blocks:
        for mul in [2,4,7,12]:
            for ang in np.linspace(0,360,8,endpoint=False):
                dest=geo_distance(kilometers=rkm*mul).destination((c_lat,c_lon),ang)
                wps.append((dest.latitude,dest.longitude))
    o_lat,o_lon=o_ll; d_lat,d_lon=d_ll
    for t in np.linspace(0.25,0.75,4):
        ml=o_lat+t*(d_lat-o_lat); mn=o_lon+t*(d_lon-o_lon)
        for ang in np.linspace(0,360,6,endpoint=False):
            dest=geo_distance(kilometers=rkm*5).destination((ml,mn),ang)
            wps.append((dest.latitude,dest.longitude))
    seen,uniq=set(),[]
    for lat,lon in wps:
        k=(round(lat,3),round(lon,3))
        if k not in seen: seen.add(k); uniq.append((lat,lon))
    random.shuffle(uniq); return uniq[:nc]

def sa_select(costs,n_iter=300):
    n=len(costs)
    if n==0: return None
    if n==1: return 0
    cur=best=random.randrange(n); T=1.0
    for _ in range(n_iter):
        nxt=random.randrange(n); d=costs[nxt]-costs[cur]
        if d<0 or random.random()<math.exp(-d/max(T,1e-9)): cur=nxt
        if costs[cur]<costs[best]: best=cur
        T*=0.994
    return best

def find_detour(ak,o_ll,d_ll,blocks,rkm,dtm):
    """高速化版：候補を絞り、早期終了付きSAで迂回ルートを選択"""
    o_lat,o_lon=o_ll; d_lat,d_lon=d_ll
    # ★ 候補を8個に絞る（20→8で処理時間を60%削減）
    wps=gen_wps(blocks,rkm,o_ll,d_ll,nc=8)
    cands=[("BASE",[[o_lon,o_lat],[d_lon,d_lat]])]+\
          [(f"WP{i+1}",[[o_lon,o_lat],[lon,lat],[d_lon,d_lat]])
           for i,(lat,lon) in enumerate(wps)]
    feasible,loose=[],[]
    for tag,coords in cands:
        route,dist,time_,_=get_route(ak,coords)
        if not route: continue
        cost=dist*120+max(0,time_-dtm)*5000
        e={"tag":tag,"route":route,"dist":dist,"time":time_,"cost":cost}
        if not violates(route,blocks,rkm):
            feasible.append(e)
            # ★ 早期終了：十分良い迂回が3本見つかれば探索終了
            if len(feasible)>=3: break
        else:
            loose.append(e)
    pool=feasible if feasible else loose
    if not pool: return None
    return pool[sa_select([e["cost"] for e in pool])]

TRUCK_COLORS=["#ff6b6b","#ffd93d","#6bcb77"]

def build_map(o_ll,d_ll,trucks,blocks,rkm,base_route):
    o_lat,o_lon=o_ll; d_lat,d_lon=d_ll
    # 全ポイントが入るようにfit_boundsで自動ズーム
    all_lats=[o_lat,d_lat]; all_lons=[o_lon,d_lon]
    if base_route:
        all_lats+=[p[0] for p in base_route]
        all_lons+=[p[1] for p in base_route]
    sw=[min(all_lats)-0.3, min(all_lons)-0.3]
    ne=[max(all_lats)+0.3, max(all_lons)+0.3]
    m=folium.Map(tiles="CartoDB Positron",prefer_canvas=True)
    m.fit_bounds([sw,ne])
    # 通常ルート（灰色破線・太め）
    if base_route:
        folium.PolyLine(base_route,color="#888888",weight=5,
                        dash_array="10,6",opacity=0.8,
                        tooltip="通常ルート（通行止め前）").add_to(m)
    # 各トラックのルート
    for tk in (trucks or []):
        if not tk.get("route"): continue
        col   = tk["color"]
        label = {"run":"走行中","reroute":"迂回計算中","done":"到着済み"}.get(tk["status"],"---")
        # 迂回ルートは太く実線、通常ルートは細め
        weight  = 8 if tk.get("rerouted") else 5
        opacity = 0.95 if tk.get("rerouted") else 0.75
        folium.PolyLine(
            tk["route"], color=col, weight=weight, opacity=opacity,
            tooltip=f"T{tk['id']} {label}{'（迂回）' if tk.get('rerouted') else ''}",
        ).add_to(m)
        # 現在位置マーカー
        prog = min(tk.get("progress",0), len(tk["route"])-1)
        folium.CircleMarker(
            tk["route"][prog], radius=12, color=col,
            fill=True, fill_opacity=1,
            tooltip=f"T{tk['id']} {label}",
        ).add_to(m)
        # 迂回済みラベル
        if tk.get("rerouted") and len(tk["route"]) > 2:
            mid = tk["route"][len(tk["route"])//2]
            folium.Marker(
                mid,
                icon=folium.DivIcon(
                    html=f'<div style="background:{col};color:#fff;padding:2px 6px;'
                         f'border-radius:4px;font-size:11px;font-weight:bold;'
                         f'white-space:nowrap;box-shadow:0 2px 4px rgba(0,0,0,0.3)">'
                         f'T{tk["id"]}迂回中</div>',
                    icon_size=(60,20), icon_anchor=(30,10)
                )
            ).add_to(m)
    # 通行止め
    if blocks:
        for b_lat,b_lon in blocks:
            folium.Circle([b_lat,b_lon],radius=rkm*1000,color="#ff2222",
                          fill=True,fill_opacity=0.25,
                          tooltip=f"🚧 通行止め（半径{rkm}km）").add_to(m)
            folium.Marker(
                [b_lat,b_lon],
                icon=folium.DivIcon(
                    html='<div style="font-size:24px">🚧</div>',
                    icon_size=(30,30), icon_anchor=(15,15)
                )
            ).add_to(m)
    folium.Marker([o_lat,o_lon],icon=folium.Icon(icon="play",prefix="fa",color="blue"),tooltip="出発").add_to(m)
    folium.Marker([d_lat,d_lon],icon=folium.Icon(icon="flag",prefix="fa",color="darkgreen"),tooltip="到着").add_to(m)
    return m.get_root().render()

# ── 工場AGV定義 ──
FW,FH=740,420
STATIONS={"入荷":(65,385),"検品A":(185,300),"組立1":(350,180),
           "組立2":(350,330),"品質検査":(520,240),"出荷":(665,385),"充電":(65,105)}
ST_LIST=list(STATIONS.values())
ST_NAMES=list(STATIONS.keys())
AGV_COLS=["#ff6b6b","#ffd93d","#6bcb77","#4d96ff","#bf5af2"]

def make_agv_json(seed=42):
    rnd=random.Random(seed)
    cx,cy=STATIONS["充電"]
    agvs=[{"id":i+1,"x":float(cx+(i-2)*32),"y":float(cy+rnd.randint(-12,12)),
            "gx":float(ST_LIST[rnd.randint(0,len(ST_LIST)-1)][0]),
            "gy":float(ST_LIST[rnd.randint(0,len(ST_LIST)-1)][1]),
            "color":AGV_COLS[i],"speed":2.8+rnd.uniform(-0.3,0.8),
            "dwell":0,"wait":0,"status":"run"} for i in range(5)]
    stations=[{"name":n,"x":float(p[0]),"y":float(p[1])} for n,p in STATIONS.items()]
    st_list=[[float(p[0]),float(p[1])] for p in ST_LIST]
    return json.dumps({"agvs":agvs,"stations":stations,"stationList":st_list})

# ── サイドバー ──
with st.sidebar:
    st.markdown("## 設定"); st.markdown("---")
    api_key=st.text_input("ORS API Key（任意）",type="password")
    st.caption("未入力時は OSRM（無料）で陸路計算します。")
    origin=st.text_input("輸送元","大阪府堺市")
    dest_addr=st.text_input("輸送先","山口県下関市")
    due_time=st.slider("納期（時間）",5,72,24)
    jp_only=st.checkbox("日本国内モード",value=True)
    st.markdown("### 通行止め設定")
    radius_km=st.slider("影響半径 (km)",1,20,5)
    min_sep_km=st.slider("2点間の最小間隔 (km)",5,80,20)
    block_tick=st.slider("通行止め発生タイミング（tick）",3,20,8)
    st.markdown("### 速度")
    sim_speed=st.slider("トラック更新間隔（ms）",500,3000,1000)
    debug=st.checkbox("デバッグ表示",value=False)
    st.markdown("---")
    c1,c2=st.columns(2)
    btn_init=c1.button("ルート準備"); btn_start=c2.button("開始")
    btn_stop=st.button("停止 / リセット")

# ── ボタン処理 ──
if btn_stop:
    for k in ["sim_running","sim_done","map_dirty"]: st.session_state[k]=False
    for k in ["trucks","blocks","map_html","base_route"]: st.session_state[k]=None
    st.session_state.sim_tick=0
    st.session_state.detour_cache={}

if btn_init:
    with st.spinner("ルート取得中...（初回は20〜30秒かかります）"):
        try:
            o_lat,o_lon,o_addr=geocode(origin,jp_only=jp_only)
            d_lat,d_lon,d_addr=geocode(dest_addr,jp_only=jp_only)
            base_route,bd,bt,src=get_route(api_key,[[o_lon,o_lat],[d_lon,d_lat]])
            if base_route is None:
                st.error("ルートを取得できませんでした。")
            else:
                if src=="OSRM": st.info("OSRM で陸路を計算しています。")
                n=len(base_route)
                trucks=[{"id":i+1,"color":TRUCK_COLORS[i],"route":base_route,
                          "progress":i*max(1,n//6),"status":"run","dist":bd,"time":bt}
                         for i in range(3)]
                st.session_state.update({
                    "trucks":trucks,"base_route":base_route,
                    "o_latlon":(o_lat,o_lon),"d_latlon":(d_lat,d_lon),
                    "blocks":None,"sim_tick":0,"sim_running":False,"sim_done":False,
                    "api_key_cache":api_key,"radius_km_cache":radius_km,
                    "due_time_cache":due_time,"block_tick_cache":block_tick,"map_dirty":True})
                st.success(f"準備完了：{o_addr} → {d_addr}")
        except Exception as e: st.error(f"エラー: {e}")

if btn_start and st.session_state.trucks:
    st.session_state.sim_running=True; st.session_state.sim_done=False

# ── autorefresh（シミュ中のみ1つ）──
if st.session_state.sim_running and not st.session_state.sim_done:
    st_autorefresh(interval=sim_speed, key="sim_refresh")

# ── シミュ tick ──
if st.session_state.sim_running and not st.session_state.sim_done:
    tick=st.session_state.sim_tick+1
    trucks=st.session_state.trucks or []
    blocks=st.session_state.blocks
    o_ll=st.session_state.o_latlon; d_ll=st.session_state.d_latlon
    ak=st.session_state.api_key_cache; rkm=st.session_state.radius_km_cache
    dtm=st.session_state.due_time_cache; btk=st.session_state.block_tick_cache
    changed=False

    if tick==btk and blocks is None and st.session_state.base_route:
        blocks=pick_blocks(st.session_state.base_route,min_sep=min_sep_km)
        st.session_state.blocks=blocks
        if blocks:
            for tk in trucks:
                if tk["status"]=="run": tk["status"]="reroute"
        changed=True

    all_done=True
    for tk in trucks:
        if tk["status"]=="done": continue
        all_done=False
        if tk["status"]=="reroute" and blocks:
            prog=min(tk.get("progress",0),len(tk["route"])-1)
            cp=tk["route"][prog]
            # ★ キャッシュキーで同じ区間の再計算をスキップ
            cache_key=(round(cp[0],2),round(cp[1],2),
                       round(d_ll[0],2),round(d_ll[1],2))
            if cache_key not in st.session_state.get("detour_cache",{}):
                res=find_detour(ak,(cp[0],cp[1]),d_ll,blocks,rkm,dtm)
                if "detour_cache" not in st.session_state:
                    st.session_state.detour_cache={}
                st.session_state.detour_cache[cache_key]=res
            else:
                res=st.session_state.detour_cache[cache_key]
            if res: tk.update({"route":res["route"],"dist":res["dist"],"time":res["time"],"progress":0,"rerouted":True})
            tk["status"]="run"; changed=True
        step=max(1,len(tk["route"])//40)
        tk["progress"]=min(tk["progress"]+step,len(tk["route"])-1)
        if tk["progress"]>=len(tk["route"])-1: tk["status"]="done"; changed=True

    if all_done: st.session_state.sim_done=True; st.session_state.sim_running=False; changed=True
    st.session_state.trucks=trucks; st.session_state.sim_tick=tick
    if changed: st.session_state.map_dirty=True

# ── 地図再生成（変化時のみ）──
if st.session_state.map_dirty and st.session_state.o_latlon:
    st.session_state.map_html=build_map(
        st.session_state.o_latlon,st.session_state.d_latlon,
        st.session_state.trucks,st.session_state.blocks,
        st.session_state.radius_km_cache,st.session_state.base_route)
    st.session_state.map_dirty=False

# ── レイアウト ──
left,right=st.columns([1.15,1.15],gap="large")

with left:
    st.markdown('<div class="card"><div class="card-title">複数トラック動的迂回シミュレーション（SA最適化）</div><div class="hrline"></div>',unsafe_allow_html=True)
    trucks=st.session_state.trucks
    if trucks:
        cols=st.columns(3)
        for i,tk in enumerate(trucks):
            bc={"run":"badge-run","reroute":"badge-reroute","done":"badge-done"}.get(tk["status"],"badge-block")
            lb={"run":"走行中","reroute":"迂回計算中","done":"到着済み"}.get(tk["status"],"---")
            cols[i].markdown(f'<div class="badge {bc}">T{tk["id"]} {lb}<br><small>{tk.get("dist",0):.0f}km/{tk.get("time",0):.1f}h</small></div>',unsafe_allow_html=True)
    blocks=st.session_state.blocks; tick=st.session_state.sim_tick; btk=st.session_state.get("block_tick_cache",8)
    if blocks:
        st.markdown(f'<div class="badge badge-block">通行止め発生（tick {btk}） - 各トラックがSAで迂回ルートを個別計算</div>',unsafe_allow_html=True)
    elif trucks:
        st.caption(f"tick {tick} - あと {max(0,btk-tick)} tick で通行止め発生予定")
    if debug and trucks:
        st.write({"tick":tick,"blocks":str(blocks),"trucks":[{k:v for k,v in t.items() if k!="route"} for t in trucks]})
    if st.session_state.sim_done: st.success("全トラック到着！SA による動的迂回最適化デモ完了")
    if st.session_state.map_html:
        st.iframe(st.session_state.map_html,height=400,width="stretch")
    else:
        st.info("サイドバーから「ルート準備」→「開始」を押してください")
    st.markdown("</div>",unsafe_allow_html=True)

with right:
    st.markdown('<div class="card"><div class="card-title">工場内 AGV シミュレーション（SA最適化・レーン走行）</div><div class="hrline"></div>',unsafe_allow_html=True)

    if st.button("AGV リセット"): st.session_state.agv_seed=random.randint(0,9999)
    init_data=make_agv_json(st.session_state.agv_seed)

    factory_html="""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#eef2f7;font-family:'Segoe UI',sans-serif;}
.wrap{display:flex;flex-direction:column;align-items:stretch;}
.ctrl{display:flex;gap:6px;padding:6px;}
.btn{padding:6px 16px;border:none;border-radius:6px;font-weight:700;
     font-size:12px;cursor:pointer;color:#fff;}
.go{background:#22aa44;} .st{background:#cc4422;} .rs{background:#4466cc;}
canvas{display:block;width:100%;height:auto;}
.log{font-size:10px;color:#446;background:#ddeeff;padding:3px 8px;
     border-radius:4px;margin:3px 6px;min-height:16px;}
.inf{font-size:11px;color:#334;padding:2px 6px;font-weight:600;}
</style></head>
<body><div class="wrap">
<div class="ctrl">
  <button class="btn go" onclick="start()">▶ 開始</button>
  <button class="btn st" onclick="stop()">■ 停止</button>
  <button class="btn rs" onclick="reset()">↺ リセット</button>
</div>
<canvas id="cv" width="720" height="430"></canvas>
<div class="inf" id="inf">Time = 0 | 緑▶走行 黄⚙作業中 赤⏸衝突待機</div>
<div class="log" id="log">▶開始 を押してください</div>
</div>
<script>
const cv=document.getElementById('cv');
const ctx=cv.getContext('2d');
// 実座標系: 720x430 固定、CSSで幅100%に伸縮

// レーンノード
const N=[
  {x:60, y:390},{x:180,y:390},{x:180,y:240},{x:180,y:100},
  {x:350,y:240},{x:510,y:240},{x:510,y:100},{x:510,y:390},
  {x:660,y:390},{x:350,y:390},{x:180,y:295},{x:350,y:175},
  {x:350,y:325},{x:510,y:240}
];
const E=[[0,1],[1,2],[2,3],[2,4],[4,5],[5,6],[5,7],[7,8],[7,9],[1,10],[4,11],[4,12],[1,9],[9,7]];
const ST_N=[0,10,11,12,13,8,3];
const ST_NAME=["入荷","検品A","組立1","組立2","品質検査","出荷","充電"];
const COLORS=["#e53935","#f9a825","#2e7d32","#1565c0","#6a1b9a"];

// 隣接リスト
const ADJ=N.map(()=>[]);
E.forEach(([a,b])=>{ADJ[a].push(b);ADJ[b].push(a);});

// ダイクストラ
function dijkstra(s,t){
  const d=N.map(()=>1e9),p=N.map(()=>-1);
  d[s]=0;const Q=new Set(N.map((_,i)=>i));
  while(Q.size){
    let u=-1;Q.forEach(n=>{if(u<0||d[n]<d[u])u=n;});
    if(u===t||d[u]===1e9)break;Q.delete(u);
    ADJ[u].forEach(v=>{
      const w=Math.hypot(N[u].x-N[v].x,N[u].y-N[v].y);
      if(d[u]+w<d[v]){d[v]=d[u]+w;p[v]=u;}
    });
  }
  const path=[];let c=t;
  while(c>=0){path.unshift(c);c=p[c];}
  return path.length>1?path:[s];
}

// SA目的地選択
function saSelect(agv){
  const n=ST_N.length;
  const costs=ST_N.map((ni,si)=>{
    const dist=Math.hypot(agv.x-N[ni].x,agv.y-N[ni].y);
    const busy=agvs.filter(o=>o.id!==agv.id&&o.goalSt===si).length*150;
    return dist+busy;
  });
  let cur=Math.floor(Math.random()*n),best=cur,T=100;
  for(let i=0;i<200;i++){
    const nx=Math.floor(Math.random()*n),d=costs[nx]-costs[cur];
    if(d<0||Math.random()<Math.exp(-d/Math.max(T,0.1)))cur=nx;
    if(costs[cur]<costs[best])best=cur;T*=0.95;
  }
  document.getElementById('log').textContent=
    'SA：AGV'+agv.id+'→'+ST_NAME[best]+
    ' (コスト:'+Math.round(costs[best])+' 温度終了:'+T.toFixed(1)+')';
  return best;
}

// AGV初期化
""" + f"const INIT={init_data};" + """
let agvs=[],running=false,tick=0;

function initAGV(){
  agvs=INIT.agvs.map((a,i)=>{
    const sn=i%N.length;
    const gs=i%ST_N.length;
    const path=dijkstra(sn,ST_N[gs]);
    return{id:a.id,color:COLORS[i%5],
      x:N[sn].x,y:N[sn].y,
      path,pi:0,goalSt:gs,
      speed:3.2+i*0.15,
      dwell:0,wait:0,status:'run',
      trail:[]};
  });
}

function reset(){initAGV();tick=0;running=false;
  document.getElementById('log').textContent='▶開始 を押してください';
  draw();}
function start(){running=true;}
function stop(){running=false;}
initAGV();

// ── 最近接ノードを返す ──
function nearestNode(x,y){
  let best=0,bd=1e9;
  N.forEach((n,i)=>{const d=Math.hypot(x-n.x,y-n.y);if(d<bd){bd=d;best=i;}});
  return best;
}

// ステップ
function stepAGV(){
  // ── フェーズ1：各AGVの移動計算 ──
  agvs.forEach(a=>{
    a.trail.push({x:a.x,y:a.y});
    if(a.trail.length>30)a.trail.shift();

    // 無限待機防止：待機カウンタが10以上なら強制リセット
    if(a.waitTotal===undefined)a.waitTotal=0;
    if(a.status==='wait'){
      a.waitTotal++;
      if(a.waitTotal>15){
        // 別ノードへ迂回
        const cn=nearestNode(a.x,a.y);
        const altGoal=(a.goalSt+1)%ST_N.length;
        const np=dijkstra(cn,ST_N[altGoal]);
        if(np&&np.length>1){a.path=np;a.pi=0;a.goalSt=altGoal;}
        a.wait=0;a.waitTotal=0;a.status='run';
      }
    }else{
      a.waitTotal=0;
    }

    if(a.dwell>0){
      a.dwell--;a.status='dwell';
      if(a.dwell===0){
        a.goalSt=saSelect(a);
        const cn=a.path[a.path.length-1]||nearestNode(a.x,a.y);
        const np=dijkstra(cn,ST_N[a.goalSt]);
        if(np&&np.length>1){a.path=np;a.pi=0;}
        a.status='run';
      }return;
    }
    if(a.wait>0){a.wait--;a.status=a.wait>0?'wait':'run';return;}
    if(a.pi>=a.path.length-1){
      a.x=N[a.path[a.path.length-1]].x;
      a.y=N[a.path[a.path.length-1]].y;
      a.dwell=15+Math.floor(Math.random()*15);
      a.status='dwell';return;
    }
    const tgt=N[a.path[a.pi+1]];
    const dx=tgt.x-a.x,dy=tgt.y-a.y,d=Math.hypot(dx,dy);
    if(d<a.speed+1){a.x=tgt.x;a.y=tgt.y;a.pi++;}
    else{a.x+=a.speed*dx/d;a.y+=a.speed*dy/d;}
    a.status='run';
  });

  // ── フェーズ2：衝突判定（先着優先）──
  // 各ノード付近に複数AGVがいる場合、IDが小さい方を優先
  for(let i=0;i<agvs.length;i++){
    const a=agvs[i];
    if(a.status!=='run')continue;
    for(let j=i+1;j<agvs.length;j++){
      const b=agvs[j];
      if(b.status!=='run')continue;
      const dist=Math.hypot(a.x-b.x,a.y-b.y);
      if(dist<32){
        // IDが大きい方（後着）を待機
        // 待機が重なりすぎないよう最大5tickに制限
        if(b.wait===0){
          b.wait=2+Math.floor(Math.random()*2);
          b.status='wait';
        }
      }
    }
  }
  tick++;
}

// 角丸矩形
function rr(x,y,w,h,r){
  ctx.beginPath();
  ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);
  ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);
  ctx.arcTo(x,y,x+w,y,r);ctx.closePath();
}

// 描画
function draw(){
  const W=720,H=430;
  // 床
  ctx.fillStyle='#eef2f7';ctx.fillRect(0,0,W,H);
  // グリッド
  ctx.strokeStyle='rgba(100,130,180,0.12)';ctx.lineWidth=1;
  for(let x=0;x<W;x+=40){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}
  for(let y=0;y<H;y+=40){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}

  // 搬送路（太い黄色実線）
  ctx.strokeStyle='rgba(220,175,0,0.4)';ctx.lineWidth=26;ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(60,390);ctx.lineTo(660,390);
  ctx.moveTo(180,390);ctx.lineTo(180,100);
  ctx.moveTo(510,390);ctx.lineTo(510,100);
  ctx.moveTo(180,240);ctx.lineTo(660,240);
  ctx.stroke();
  // 中心白破線
  ctx.strokeStyle='rgba(255,255,255,0.8)';ctx.lineWidth=2;ctx.setLineDash([10,8]);
  ctx.beginPath();
  ctx.moveTo(60,390);ctx.lineTo(660,390);
  ctx.moveTo(180,390);ctx.lineTo(180,100);
  ctx.moveTo(510,390);ctx.lineTo(510,100);
  ctx.moveTo(180,240);ctx.lineTo(660,240);
  ctx.stroke();ctx.setLineDash([]);

  // 棚
  [[285,60,65,65],[355,60,65,65],[425,60,65,65],
   [285,140,65,65],[425,140,65,65]].forEach(([sx,sy,sw,sh])=>{
    const g=ctx.createLinearGradient(sx,sy,sx,sy+sh);
    g.addColorStop(0,'#c5ddf5');g.addColorStop(1,'#9cc0e5');
    ctx.fillStyle=g;ctx.strokeStyle='#4477bb';ctx.lineWidth=1.5;
    rr(sx,sy,sw,sh,5);ctx.fill();ctx.stroke();
    for(let r=1;r<3;r++){
      ctx.strokeStyle='rgba(60,100,180,0.25)';ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(sx+4,sy+sh*r/3);ctx.lineTo(sx+sw-4,sy+sh*r/3);ctx.stroke();
    }
    ctx.fillStyle='#1a4070';ctx.font='bold 8px monospace';
    ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.fillText('SHELF',sx+sw/2,sy+sh/2);
  });

  // 外壁
  ctx.strokeStyle='rgba(50,70,120,0.45)';ctx.lineWidth=5;
  ctx.strokeRect(3,3,W-6,H-6);

  // ステーション
  ST_N.forEach((ni,si)=>{
    const n=N[ni];
    const active=agvs.some(a=>a.goalSt===si&&a.status==='dwell');
    ctx.fillStyle=active?'#fff5cc':'#ddeeff';
    ctx.strokeStyle=active?'#cc8800':'#2266cc';ctx.lineWidth=2.5;
    rr(n.x-26,n.y-16,52,32,7);ctx.fill();ctx.stroke();
    // グロー
    if(active){
      const g2=ctx.createRadialGradient(n.x,n.y,2,n.x,n.y,30);
      g2.addColorStop(0,'rgba(255,200,0,0.3)');
      g2.addColorStop(1,'rgba(255,200,0,0)');
      ctx.fillStyle=g2;ctx.beginPath();ctx.arc(n.x,n.y,30,0,Math.PI*2);ctx.fill();
    }
    ctx.fillStyle='#003388';ctx.font='bold 10px "Segoe UI"';
    ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.fillText(ST_NAME[si],n.x,n.y);
  });

  // AGV軌跡 & 本体
  agvs.forEach(a=>{
    // 軌跡
    if(a.trail.length>1){
      ctx.beginPath();ctx.moveTo(a.trail[0].x,a.trail[0].y);
      a.trail.forEach(p=>ctx.lineTo(p.x,p.y));
      ctx.strokeStyle=a.color+'55';ctx.lineWidth=3;ctx.stroke();
    }
    // ゴール破線
    const gn=N[ST_N[a.goalSt]];
    ctx.strokeStyle=a.color+'66';ctx.lineWidth=1.5;ctx.setLineDash([5,7]);
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(gn.x,gn.y);ctx.stroke();
    ctx.setLineDash([]);
    // ゴール★
    ctx.fillStyle=a.color+'99';ctx.beginPath();ctx.arc(gn.x,gn.y,5,0,Math.PI*2);ctx.fill();

    // AGV本体
    const col=a.status==='wait'?'#dd2222':a.status==='dwell'?'#dd9900':a.color;
    ctx.shadowColor='rgba(0,0,0,0.3)';ctx.shadowBlur=8;ctx.shadowOffsetY=3;
    ctx.fillStyle=col;ctx.strokeStyle='#fff';ctx.lineWidth=2.5;
    rr(a.x-18,a.y-12,36,24,6);ctx.fill();ctx.stroke();
    ctx.shadowBlur=0;ctx.shadowOffsetY=0;
    // ホイール
    ctx.fillStyle='#333';
    [[-9,10],[9,10],[-9,-10],[9,-10]].forEach(([wx,wy])=>{
      ctx.beginPath();ctx.ellipse(a.x+wx,a.y+wy,4,2.5,0,0,Math.PI*2);ctx.fill();
    });
    // ID
    ctx.fillStyle='#fff';ctx.font='bold 11px monospace';
    ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.fillText('A'+a.id,a.x,a.y);
    // ステータスアイコン
    const icon=a.status==='wait'?'⏸':a.status==='dwell'?'⚙':'▶';
    ctx.font='9px serif';ctx.fillText(icon,a.x+16,a.y-14);
  });
  ctx.textBaseline='alphabetic';
  document.getElementById('inf').textContent=
    'Time='+tick+' | 緑▶走行 黄⚙作業中 赤⏸衝突待機';
}

// ループ
let last=0;
function loop(ts){
  if(running&&ts-last>100){stepAGV();last=ts;}
  draw();
  requestAnimationFrame(loop);
}
draw(); // 初回描画
requestAnimationFrame(loop);
</script></body></html>"""

    st.iframe(factory_html, height=530, width="stretch")
    st.markdown(
        '<small style="color:#8899bb">'
        '▶開始でAGVがレーン沿いに走行。SAが次目的地を自動選択しログに表示します。'
        '</small>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

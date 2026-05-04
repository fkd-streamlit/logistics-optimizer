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

def sa_select(costs,n_iter=600):
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
    o_lat,o_lon=o_ll; d_lat,d_lon=d_ll
    wps=gen_wps(blocks,rkm,o_ll,d_ll)
    cands=[("BASE",[[o_lon,o_lat],[d_lon,d_lat]])]+\
          [(f"WP{i+1}",[[o_lon,o_lat],[lon,lat],[d_lon,d_lat]]) for i,(lat,lon) in enumerate(wps)]
    feasible,loose=[],[]
    for tag,coords in cands:
        route,dist,time_,_=get_route(ak,coords)
        if not route: continue
        cost=dist*120+max(0,time_-dtm)*5000
        e={"tag":tag,"route":route,"dist":dist,"time":time_,"cost":cost}
        (loose if violates(route,blocks,rkm) else feasible).append(e)
    pool=feasible if feasible else loose
    if not pool: return None
    return pool[sa_select([e["cost"] for e in pool])]

TRUCK_COLORS=["#ff6b6b","#ffd93d","#6bcb77"]

def build_map(o_ll,d_ll,trucks,blocks,rkm,base_route):
    o_lat,o_lon=o_ll; d_lat,d_lon=d_ll
    m=folium.Map(location=[(o_lat+d_lat)/2,(o_lon+d_lon)/2],
                 zoom_start=6,tiles="CartoDB DarkMatter",prefer_canvas=True)
    if base_route:
        folium.PolyLine(base_route,color="#555",weight=3,dash_array="6,6",tooltip="通常ルート").add_to(m)
    for tk in (trucks or []):
        if not tk.get("route"): continue
        folium.PolyLine(tk["route"],color=tk["color"],weight=5,opacity=0.85,
                        tooltip=f"T{tk['id']} {tk['status']}").add_to(m)
        prog=min(tk.get("progress",0),len(tk["route"])-1)
        folium.CircleMarker(tk["route"][prog],radius=9,color=tk["color"],
                            fill=True,fill_opacity=1,tooltip=f"T{tk['id']}").add_to(m)
    if blocks:
        for b_lat,b_lon in blocks:
            folium.Circle([b_lat,b_lon],radius=rkm*1000,color="#ff4444",
                          fill=True,fill_opacity=0.2,tooltip="通行止め").add_to(m)
            folium.Marker([b_lat,b_lon],icon=folium.Icon(icon="ban",prefix="fa",color="red")).add_to(m)
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

if btn_init:
    with st.spinner("ルート取得中..."):
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
            res=find_detour(ak,(cp[0],cp[1]),d_ll,blocks,rkm,dtm)
            if res: tk.update({"route":res["route"],"dist":res["dist"],"time":res["time"],"progress":0})
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
    st.markdown('<div class="card"><div class="card-title">工場内 AGV シミュレーション（SA衝突回避・純JS）</div><div class="hrline"></div>',unsafe_allow_html=True)
    ca,cb,cc=st.columns(3)
    if ca.button("AGV 開始"): st.session_state.agv_seed=st.session_state.agv_seed  # trigger rerun
    agv_stop_flag=cb.button("AGV 停止")
    if cc.button("AGV リセット"): st.session_state.agv_seed=random.randint(0,9999)

    init_data=make_agv_json(st.session_state.agv_seed)

    factory_html=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body{{margin:0;background:#0b0f1a;display:flex;flex-direction:column;align-items:center;font-family:'Segoe UI',sans-serif;}}
canvas{{border-radius:10px;box-shadow:0 0 24px rgba(77,150,255,0.3);display:block;}}
.info{{color:#8899bb;font-size:11px;margin:4px 0 2px;}}
.ctrl{{display:flex;gap:6px;margin:3px 0;}}
.btn{{background:linear-gradient(90deg,#ff6b6b,#4d96ff);color:#fff;border:none;
      border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;}}
</style></head>
<body>
<div class="ctrl">
  <button class="btn" onclick="running=true">開始</button>
  <button class="btn" onclick="running=false">停止</button>
  <button class="btn" onclick="resetAGV()">リセット</button>
</div>
<canvas id="c" width="{FW}" height="{FH}"></canvas>
<div class="info" id="info">Time = 0 | 緑=走行  黄=停車中  赤=衝突回避待機</div>
<script>
const FW={FW},FH={FH};
const INIT={init_data};
const cv=document.getElementById('c'),ctx=cv.getContext('2d');
let agvs,running=false,tick=0;
function resetAGV(){{agvs=JSON.parse(JSON.stringify(INIT.agvs));tick=0;running=false;}}
resetAGV();
const stats=INIT.stations,stList=INIT.stationList;
function saSelect(costs){{
  const n=costs.length; if(!n) return 0;
  let cur=Math.floor(Math.random()*n),best=cur,T=1.0;
  for(let i=0;i<100;i++){{
    const nxt=Math.floor(Math.random()*n),d=costs[nxt]-costs[cur];
    if(d<0||Math.random()<Math.exp(-d/Math.max(T,1e-9)))cur=nxt;
    if(costs[cur]<costs[best])best=cur; T*=0.98;
  }}
  return best;
}}
function step(){{
  const props=agvs.map(a=>{{
    if(a.dwell>0){{a.dwell--;
      if(a.dwell===0){{a.status='run';const ds=stList.map(s=>Math.hypot(a.x-s[0],a.y-s[1]));
        const idx=saSelect(ds);a.gx=stList[idx][0];a.gy=stList[idx][1];}}
      return{{x:a.x,y:a.y}};}}
    if(a.wait>0){{a.wait--;a.status=a.wait>0?'wait':'run';return{{x:a.x,y:a.y}};}}
    const dx=a.gx-a.x,dy=a.gy-a.y,dist=Math.hypot(dx,dy);
    if(dist<a.speed+1)return{{x:a.gx,y:a.gy}};
    return{{x:a.x+a.speed*dx/dist,y:a.y+a.speed*dy/dist}};
  }});
  agvs.forEach((a,i)=>{{
    const px=props[i].x,py=props[i].y;
    if(props.some((p,j)=>i!==j&&Math.hypot(px-p.x,py-p.y)<30)){{
      a.wait=1+Math.floor(Math.random()*3);a.status='wait';return;}}
    a.x=px;a.y=py;
    if(Math.hypot(a.x-a.gx,a.y-a.gy)<a.speed+1){{
      a.x=a.gx;a.y=a.gy;a.dwell=8+Math.floor(Math.random()*12);a.status='dwell';}}
  }});
  tick++;
}}
function rr(x,y,w,h,r){{
  ctx.beginPath();ctx.moveTo(x+r,y);ctx.lineTo(x+w-r,y);
  ctx.quadraticCurveTo(x+w,y,x+w,y+r);ctx.lineTo(x+w,y+h-r);
  ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h);ctx.lineTo(x+r,y+h);
  ctx.quadraticCurveTo(x,y+h,x,y+h-r);ctx.lineTo(x,y+r);
  ctx.quadraticCurveTo(x,y,x+r,y);ctx.closePath();
}}
function draw(){{
  const g=ctx.createLinearGradient(0,0,0,FH);
  g.addColorStop(0,'#111827');g.addColorStop(1,'#0d1520');
  ctx.fillStyle=g;ctx.fillRect(0,0,FW,FH);
  ctx.strokeStyle='rgba(77,150,255,0.05)';ctx.lineWidth=1;
  for(let x=0;x<FW;x+=40){{ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,FH);ctx.stroke();}}
  for(let y=0;y<FH;y+=40){{ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(FW,y);ctx.stroke();}}
  ctx.setLineDash([14,8]);ctx.strokeStyle='rgba(255,217,61,0.18)';ctx.lineWidth=20;
  ctx.beginPath();
  ctx.moveTo(45,385);ctx.lineTo(700,385);
  ctx.moveTo(185,385);ctx.lineTo(185,105);
  ctx.moveTo(520,385);ctx.lineTo(520,105);
  ctx.moveTo(185,240);ctx.lineTo(665,240);
  ctx.stroke();ctx.setLineDash([]);
  [[275,65,75,75],[360,65,75,75],[445,65,75,75],[275,150,75,75],[445,150,75,75]].forEach(([sx,sy,sw,sh])=>{{
    const sg=ctx.createLinearGradient(sx,sy,sx,sy+sh);
    sg.addColorStop(0,'#1a3a5c');sg.addColorStop(1,'#0f2540');
    ctx.fillStyle=sg;ctx.strokeStyle='rgba(77,150,255,0.25)';ctx.lineWidth=1.5;
    rr(sx,sy,sw,sh,4);ctx.fill();ctx.stroke();
    ctx.fillStyle='rgba(77,150,255,0.35)';ctx.font='bold 8px monospace';
    ctx.textAlign='center';ctx.fillText('SHELF',sx+sw/2,sy+sh/2+3);
  }});
  ctx.strokeStyle='rgba(255,255,255,0.08)';ctx.lineWidth=4;ctx.strokeRect(3,3,FW-6,FH-6);
  stats.forEach(s=>{{
    const rg=ctx.createRadialGradient(s.x,s.y,3,s.x,s.y,24);
    rg.addColorStop(0,'rgba(77,150,255,0.28)');rg.addColorStop(1,'rgba(77,150,255,0)');
    ctx.fillStyle=rg;ctx.beginPath();ctx.arc(s.x,s.y,24,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='#1a3060';ctx.strokeStyle='#4d96ff';ctx.lineWidth=1.8;
    rr(s.x-21,s.y-13,42,26,5);ctx.fill();ctx.stroke();
    ctx.fillStyle='#aac4ff';ctx.font='bold 8px "Segoe UI"';ctx.textAlign='center';ctx.fillText(s.name,s.x,s.y+3);
  }});
  agvs.forEach(a=>{{
    const col=a.status==='wait'?'#ff4444':a.status==='dwell'?'#ffd93d':a.color;
    ctx.strokeStyle=a.color+'33';ctx.lineWidth=1;ctx.setLineDash([4,6]);
    ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(a.gx,a.gy);ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle=a.color+'88';ctx.beginPath();ctx.arc(a.gx,a.gy,4,0,Math.PI*2);ctx.fill();
    ctx.shadowColor=col;ctx.shadowBlur=10;ctx.fillStyle=col;
    ctx.strokeStyle='rgba(255,255,255,0.75)';ctx.lineWidth=1.5;
    rr(a.x-13,a.y-9,26,18,4);ctx.fill();ctx.stroke();ctx.shadowBlur=0;
    ctx.fillStyle='#fff';ctx.font='bold 9px monospace';ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.fillText('T'+a.id,a.x,a.y);
  }});
  ctx.textBaseline='alphabetic';
  document.getElementById('info').textContent='Time='+tick+' | 緑=走行  黄=停車中  赤=衝突回避待機';
}}
let last=0;
function loop(ts){{if(running&&ts-last>120){{step();last=ts;}}draw();requestAnimationFrame(loop);}}
requestAnimationFrame(loop);
</script></body></html>"""

    st.iframe(factory_html, height=FH+80, width="stretch")
    st.markdown('<small style="color:#8899bb">AGVの開始/停止はCanvas内のボタンで操作します</small>',unsafe_allow_html=True)
    st.markdown("</div>",unsafe_allow_html=True)

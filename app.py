#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桃園市參與式高溫監測系統  v0.3
支援 SQLite（本地開發）與 PostgreSQL（Railway 部署）
"""

import os, sqlite3, hashlib, secrets, re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn, bcrypt

# ─── 設定 ───────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
DATABASE   = BASE_DIR / "heat.db"
TOKEN_TTL   = 7 * 86400  # 7天秒數

# 優先使用 Railway 的 DATABASE_URL，否則用本地 SQLite
USE_PG = bool(os.environ.get("DATABASE_URL"))

app = FastAPI()
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir), html=True))

# ─── 資料庫 ───────────────────────────────────────────

import psycopg, re
from urllib.parse import urlparse

def _translate_sql(sql):
    """Translate SQLite ? placeholders to psycopg $n placeholders."""
    idx = [0]
    def replacer(_): idx[0] += 1; return f'${idx[0]}'
    return re.sub(r'\?', replacer, sql)

class DB:
    """SQLite 與 PostgreSQL 統一介面：dict-style 取用"""
    def __init__(self, conn, is_pg):
        self._c = conn
        self._pg = is_pg
    def execute(self, sql, args=()):
        if self._pg:
            sql = _translate_sql(sql)
        cur = self._c.cursor()
        cur.execute(sql, args)
        if self._pg:
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return DCursor(rows)
        else:
            return DCursor([dict(r) for r in cur.fetchall()])
    def commit(self):
        if self._pg: self._c.commit()
    def close(self):
        if self._c:
            self._c.close()
            self._c = None

class DCursor:
    def __init__(self, rows):
        self._rows = rows
        self._pos = 0
    def fetchone(self):
        if self._pos < len(self._rows):
            r = self._rows[self._pos]; self._pos += 1; return r
        return None
    def fetchall(self):
        return self._rows
    def __iter__(self):
        return iter(self._rows)
    def __len__(self):
        return len(self._rows)
    def __bool__(self):
        return bool(self._rows)

def _pg_conn():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    return conn

def get_db():
    if USE_PG:
        return DB(_pg_conn(), True)
    else:
        conn = sqlite3.connect(DATABASE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # autocommit mode
        return DB(conn, False)


def init_db():
    if USE_PG:
        conn = psycopg.connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        cur = conn.cursor()
        # PostgreSQL schema
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            is_admin INTEGER DEFAULT 0,
            li_name VARCHAR(255) NOT NULL,
            created_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token VARCHAR(255) PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL
        );
        CREATE TABLE IF NOT EXISTS temp_reports (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            li_name VARCHAR(255) NOT NULL,
            location VARCHAR(255) NOT NULL,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            temp DOUBLE PRECISION NOT NULL,
            note TEXT,
            status VARCHAR(50) DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL,
            reviewed_at TIMESTAMP,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS layers (
            id SERIAL PRIMARY KEY,
            li_name VARCHAR(255) NOT NULL,
            type VARCHAR(50) NOT NULL,
            name VARCHAR(255) NOT NULL,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            note TEXT
        );
        """)
        for li in ["桃園區-大林里", "中壢區-雙連里", "新屋區-蚵間里"]:
            pw = bcrypt.hashpw((li + "_admin").encode(), bcrypt.gensalt()).decode()
            try:
                cur.execute(
                    "INSERT INTO users (username,password,is_admin,li_name,created_at) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING",
                    (li + "_admin", pw, 1, li, datetime.now())
                )
            except Exception:
                pass
        conn.close()
    else:
        # SQLite: use raw connection for executescript
        conn = sqlite3.connect(DATABASE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            li_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS temp_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            li_name TEXT NOT NULL,
            location TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            temp REAL NOT NULL,
            note TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            li_name TEXT NOT NULL,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            note TEXT
        );
        """)
        for li in ["桃園區-大林里", "中壢區-雙連里", "新屋區-蚵間里"]:
            pw = bcrypt.hashpw((li + "_admin").encode(), bcrypt.gensalt()).decode()
            conn.execute(
                "INSERT OR IGNORE INTO users (username,password,is_admin,li_name,created_at) VALUES (?,?,?,?,?)",
                (li + "_admin", pw, 1, li, datetime.now().isoformat())
            )
        conn.commit()
        conn.close()

# ─── 認證 ───────────────────────────────────────────
def make_token(user_id: int) -> str:
    token = secrets.token_hex(32)
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO tokens (token,user_id,expires_at) VALUES (?,?,?)",
        (token, user_id, datetime.now().timestamp() + TOKEN_TTL)
    )
    db.commit()
    db.close()
    return token

def get_user(request: Request) -> dict:
    token = request.cookies.get("token")
    if not token:
        raise HTTPException(401, "請先登入")
    db = get_db()
    # SQL: both SQLite and PostgreSQL use %s / ? depending on USE_PG
    sql = "SELECT u.id,u.username,u.is_admin,u.li_name FROM tokens t JOIN users u ON t.user_id=u.id WHERE t.token=? AND t.expires_at>?"
    row = db.execute(sql, (token, datetime.now().timestamp())).fetchone()
    db.close()
    if not row:
        raise HTTPException(401, "請先登入")
    return dict(row)

def get_admin(user: dict = Depends(get_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "需要管理者權限")
    return user

# ─── HTML輔助 ───────────────────────────────────────────
def _head():
    return """<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Microsoft JhengHei','PingFang TC',sans-serif;background:#f0f4f8;color:#333}
a{text-decoration:none}
header{background:#1565C0;color:#fff;padding:10px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px}
header h1{font-size:1rem}
header a{color:#fff;margin-left:14px;font-size:.85rem}
.container{max-width:980px;margin:0 auto;padding:12px}
.card{background:#fff;border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:0 2px 8px rgba(0,0,0,.1)}
.card h2{font-size:.95rem;margin-bottom:10px;color:#1565C0;border-bottom:2px solid #E3F2FD;padding-bottom:6px}
.btn{display:inline-block;padding:7px 16px;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;text-decoration:none}
.btn-sm{padding:4px 10px;font-size:.8rem}
.btn-primary{background:#1565C0;color:#fff}.btn-success{background:#2E7D32;color:#fff}
.btn-danger{background:#C62828;color:#fff}.btn-secondary{background:#607D8B;color:#fff}
.form-group{margin-bottom:10px}
.form-group label{display:block;margin-bottom:3px;font-weight:bold;font-size:.8rem;color:#555}
.form-group input,.form-group select,.form-group textarea{width:100%;padding:7px 10px;border:1px solid #ccc;border-radius:6px;font-size:.9rem}
.form-group input[type=submit]{background:#1565C0;color:#fff;border:none;cursor:pointer}
.msg{padding:9px 14px;border-radius:6px;margin-bottom:12px;font-size:.85rem}
.msg-success{background:#E8F5E9;color:#1B5E20}.msg-error{background:#FFEBEE;color:#B71C1C}
.status-pending{color:#E65100;font-weight:bold}.status-approved{color:#2E7D32;font-weight:bold}
.status-rejected{color:#C62828;font-weight:bold}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:left}
th{background:#E3F2FD;color:#1565C0;font-weight:bold}
.empty{color:#999;font-size:.85rem;padding:8px}
footer{text-align:center;font-size:.75rem;color:#999;padding:16px}
.rank-row{display:flex;justify-content:space-between;padding:7px 8px;border-bottom:1px solid #f0}
.rank-label{font-weight:bold}.rank-val{color:#555;font-size:.85rem}
.layer-grid{display:grid;grid-template-columns:repeat(6,1fr) auto;gap:6px;align-items:end}
.layer-form{padding:12px;background:#f8f9fa;border-radius:8px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:640px){.two-col{grid-template-columns:1fr}.layer-grid{grid-template-columns:1fr 1fr}}
#map{height:60vh;border-radius:10px;border:2px solid #ddd}
.legend{display:flex;gap:10px;flex-wrap:wrap;padding:4px 0}
.legend-item{display:flex;align-items:center;gap:4px;font-size:.8rem}
.legend-dot{width:14px;height:14px;border-radius:50%;flex-shrink:0}
.stat-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px}
.stat-box{text-align:center;background:#f0f4f8;border-radius:8px;padding:10px}
.stat-num{font-size:1.6rem;font-weight:bold;color:#1565C0}
.stat-lbl{font-size:.75rem;color:#555}
</style>"""

def _gps_script():
    return """<script>
function getLocation(){
  if(!navigator.geolocation){setStatus("⚠️ 您的瀏覽器不支援 GPS定位");return;}
  setStatus("📡 正在取得 GPS...");
  navigator.geolocation.getCurrentPosition(function(pos){
    var lat=pos.coords.latitude, lng=pos.coords.longitude;
    document.getElementById("lat").value=lat;
    document.getElementById("lng").value=lng;
    setStatus("📡 GPS已取得，反查地址中...");
    reverseGeocode(lat,lng,function(addr){
      if(addr){
        document.getElementById("location").value=addr;
        setStatus("✅ 位置："+addr+"（"+lat.toFixed(5)+","+lng.toFixed(5)+")");
      } else {
        setStatus("✅ GPS已取得（"+lat.toFixed(5)+","+lng.toFixed(5)+")");
      }
    });
  },function(err){
    setStatus("⚠️ 無法取得定位："+err.message);
  });
}
function geocodeAddress(){
  var addr=document.getElementById("addr-input").value.trim();
  if(!addr){setStatus("⚠️ 請先輸入地址");return;}
  setStatus("🔍 查詢地址："+addr+"...");
  var url="https://nominatim.openstreetmap.org/search?format=json&q="+encodeURIComponent(addr)+"&limit=1&addressdetails=1";
  fetch(url,{headers:{"User-Agent":"TaoyuanHeatMap/1.0"}})
    .then(function(r){return r.json();})
    .then(function(data){
      if(data&&data.length>0){
        var lat=parseFloat(data[0].lat), lng=parseFloat(data[0].lon);
        var a=data[0].address||{};
        var city=a.city||a.town||a.village||"";
        var district=a.city_district||a.suburb||a.district||"";
        var road=a.road||"";
        var housenr=a.house_number||"";
        var parts=[];
        if(city){parts.push(city.replace(/[市]$/,"")+"市");}
        if(district&&district!==city){parts.push(district.replace(/[區]$/,"")+"區");}
        if(road){parts.push(road);}
        if(housenr){parts.push(housenr+"號");}
        var clean=parts.length>0?parts.join(""):data[0].display_name||"";
        document.getElementById("lat").value=lat;
        document.getElementById("lng").value=lng;
        document.getElementById("location").value=clean;
        setStatus("✅ 找到："+clean);
        if(document.getElementById("pick-map")) initPickerMap(lat,lng);
      } else {
        setStatus("⚠️ 找不到，請換關鍵字或直接輸入座標");
      }
    })
    .catch(function(){setStatus("⚠️ 查址失敗，請稍後再試");});
}
function reverseGeocode(lat,lng,cb){
  var url="https://nominatim.openstreetmap.org/reverse?format=json&lat="+lat+"&lon="+lng+"&zoom=18&addressdetails=1";
  fetch(url,{headers:{"User-Agent":"TaoyuanHeatMap/1.0"}})
    .then(function(r){return r.json();})
    .then(function(data){
      if(!data||!data.address){cb(null);return;}
      var a=data.address;
      // 幹掉 city/town/village 裡重複的「桃園市」「桃園區」之類
      var city = a.city || a.town || a.village || a.county || "";
      // district: 清潔隊用「區」，e.g. a.town + a.suburb
      var district = a.city_district || a.suburb || a.district || "";
      // 路名
      var road = a.road || "";
      // 門牌
      var housenr = a.house_number || "";
      // 組出乾淨地址：城市+區+路+號
      var parts=[];
      // 合併 city (e.g. "桃園市")
      if(city){
        var c=city.replace(/[市]$/,"");
        parts.push(c+"市");
      }
      if(district && district!==city){
        parts.push(district.replace(/[區]$/,"")+"區");
      }
      if(road){
        parts.push(road);
      }
      if(housenr){
        parts.push(housenr+"號");
      }
      var clean = parts.length>0 ? parts.join("") : (data.display_name||null);
      cb(clean);
    })
    .catch(function(){cb(null);});
}
function setStatus(msg){
  var s=document.getElementById("gps-status");
  if(s)s.innerText=msg;
}
function initPickerMap(defLat,defLng){
  var mapEl=document.getElementById("pick-map");
  if(!mapEl)return;
  var lat=defLat||24.99, lng=defLng||121.30;
  var map=L.map("pick-map").setView([lat,lng],17);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"© OSM",maxZoom:19}).addTo(map);
  var marker=L.marker([lat,lng],{draggable:true}).addTo(map);
  document.getElementById("lat").value=lat;
  document.getElementById("lng").value=lng;
  marker.on("dragend",function(e){
    var p=e.target.getLatLng();
    document.getElementById("lat").value=p.lat;
    document.getElementById("lng").value=p.lng;
    reverseGeocode(p.lat,p.lng,function(addr){
      if(addr)document.getElementById("location").value=addr;
    });
  });
  map.on("click",function(e){
    marker.setLatLng(e.latlng);
    document.getElementById("lat").value=e.latlng.lat;
    document.getElementById("lng").value=e.latlng.lng;
    reverseGeocode(e.latlng.lat,e.latlng.lng,function(addr){
      if(addr)document.getElementById("location").value=addr;
    });
  });
}
</script>"""

def _map_script(reports_json, layers_json):
    return """<script>
var reports=""" + reports_json + """;
var layerData=""" + layers_json + """;
</script>"""

def _map_html():
    return """
  <div class="card" style="padding:10px">
    <div class="legend">
      <div class="legend-item"><div class="legend-dot" style="background:#4CAF50"></div>&lt;30°C（涼爽）</div>
      <div class="legend-item"><div class="legend-dot" style="background:#FFEB3B"></div>30-33°C（適中）</div>
      <div class="legend-item"><div class="legend-dot" style="background:#FF9800"></div>33-36°C（炎熱）</div>
      <div class="legend-item"><div class="legend-dot" style="background:#F44336"></div>≥36°C（危險）</div>
      <div class="legend-item"><div class="legend-dot" style="background:#4CAF50"></div>🌿 綠地</div>
      <div class="legend-item"><div class="legend-dot" style="background:#8D6E63"></div>⛱️ 遮蔭</div>
      <div class="legend-item"><div class="legend-dot" style="background:#03A9F4"></div>💧 飲水機</div>
      <div class="legend-item"><div class="legend-dot" style="background:#9C27B0"></div>🏥 關懷</div>
    </div>
  </div>
  <div id="map"></div>
  <div class="stat-row">
    <div class="stat-box"><div class="stat-num" id="s-total">-</div><div class="stat-lbl">已審核回報</div></div>
    <div class="stat-box"><div class="stat-num" id="s-max">-</div><div class="stat-lbl">最高溫度</div></div>
    <div class="stat-box"><div class="stat-num" id="s-avg">-</div><div class="stat-lbl">平均溫度</div></div>
  </div>
  <script>
  var map=L.map("map").setView([24.99,121.30],12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"© OpenStreetMap",maxZoom:18}).addTo(map);
  function tempColor(t){
    if(t<30)return"#4CAF50";
    if(t<33)return"#FFEB3B";
    if(t<36)return"#FF9800";
    return"#F44336";
  }
  var layerColors={green:"#4CAF50",shade:"#8D6E63",water:"#03A9F4",care_point:"#9C27B0"};
  reports.forEach(function(r){
    if(!r.latitude||!r.longitude)return;
    var c=tempColor(r.temp);
    var icon=L.divIcon({html:'<div style="background:'+c+';width:16px;height:16px;border-radius:50%;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4)"></div>',iconSize:[16,16],iconAnchor:[8,8],className:""});
    var date=new Date(r.created_at).toLocaleString("zh-TW");
    L.marker([r.latitude,r.longitude],{icon:icon}).addTo(map).bindPopup("<b>"+r.location+"</b><br>溫度："+r.temp+"°C<br>里別："+r.li_name+"<br>回報："+date+(r.note?"<br>備註："+r.note:""));
  });
  layerData.forEach(function(p){
    var c=layerColors[p.type]||"#999";
    var icon=L.divIcon({html:'<div style="background:'+c+';width:12px;height:12px;border-radius:2px;border:1px solid #fff"></div>',iconSize:[12,12],iconAnchor:[6,6],className:""});
    L.marker([p.lat,p.lng],{icon:icon}).addTo(map).bindPopup(p.name+(p.note?"<br>"+p.note:""));
  });
  var temps=reports.map(function(r){return r.temp;}).filter(function(t){return t!=null;});
  if(temps.length){
    document.getElementById("s-total").innerText=reports.length;
    document.getElementById("s-max").innerText=Math.max.apply(Math,temps).toFixed(1)+"°C";
    document.getElementById("s-avg").innerText=(temps.reduce(function(a,b){return a+b;},0)/temps.length).toFixed(1)+"°C";
  }
  </script>"""

def _nav(user):
    li = user.get("li_name","") if user else ""
    is_admin = user.get("is_admin") if user else 0
    links = []
    if is_admin:
        links.append(("/admin","🔧 後台"))
    links += [("/dashboard","📊 儀表板"),("/map","🗺️ 地圖"),("/logout","登出")]
    nav = " | ".join('<a href="'+t+'" style="margin-left:0">'+u+'</a>' for t,u in links)
    return ('<span style="font-size:.85rem">里別：<strong>'+li+'</strong></span>' if li else "") + "<span>"+nav+"</span>"

def _page(title, body, user=None, msg=""):
    nav = _nav(user) if user else '<a href="/login">登入</a> | <a href="/logout">登出</a>'
    return """<!DOCTYPE html><html><head><title>"""+title+"""</title>"""+_head()+"""</head><body>
<header><h1>🌡️ 桃園市參與式高溫監測系統</h1><div>"""+nav+"""</div></header>
<div class="container">"""+msg+body+"""</div>
<footer>© 2026 桃園市氣候變遷調適計畫｜國立中央大學太空及遙測研究中心</footer>
</body></html>"""

# ─── 路由 ───────────────────────────────────────────
@app.get("/dbg2")
async def dbg2():
    try:
        db = get_db()
        users = db.execute("SELECT username, li_name, is_admin FROM users LIMIT 10").fetchall()
        db.close()
        return {"users": [dict(u) for u in users], "USE_PG": USE_PG}
    except Exception as e:
        import traceback; return {"error": str(e), "tb": traceback.format_exc()}, 500

@app.get("/")
async def root(request: Request):
    try:
        get_user(request)
    except:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")

# 登入
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    try:
        get_user(request)
        return RedirectResponse(url="/dashboard")
    except:
        pass
    err = request.query_params.get("e")
    logged_out = request.query_params.get("logged_out")
    msg = ""
    if logged_out:
        msg = '<p style="color:#2E7D32;text-align:center;margin-bottom:12px">✅ 已登出</p>'
    elif err:
        msg = '<p style="color:#C62828;text-align:center;margin-bottom:12px">帳號或密碼錯誤</p>'
    return HTMLResponse(_page("🌡️ 登入","""
  <div class="card">
    <h2 style="margin-bottom:16px">會員登入</h2>
    <form method="post" action="/login">
      <div class="form-group"><label>帳號</label><input name="username" required placeholder="輸入帳號"></div>
      <div class="form-group"><label>密碼</label><input name="password" type="password" required placeholder="輸入密碼"></div>
      <div class="form-group"><input type="submit" value="登入" class="btn btn-primary" style="width:100%"></div>
    </form>
    <p style="text-align:center;margin-top:8px;font-size:.85rem">還沒有帳號？<a href="/register" style="color:#1565C0">立即註冊</a></p>
  </div>""", msg=msg))

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    db.close()
    if not user:
        return RedirectResponse(url="/login?e=1", status_code=302)
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), user["password"].encode("utf-8"))
    except:
        ok = False
    if not ok:
        return RedirectResponse(url="/login?e=1", status_code=302)
    token = make_token(user["id"])
    resp = RedirectResponse(url="/admin" if user["is_admin"] else "/dashboard", status_code=302)
    resp.set_cookie("token", token, httponly=True, max_age=TOKEN_TTL, samesite="lax", path="/")
    return resp

# 註冊
@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    try:
        get_user(request)
        return RedirectResponse(url="/dashboard")
    except:
        pass
    db = get_db()
    lis = [r["li_name"] for r in db.execute("SELECT DISTINCT li_name FROM users").fetchall()]
    db.close()
    li_opts = "".join('<option value="'+li+'">'+li+'</option>' for li in lis)
    return HTMLResponse(_page("📝 註冊","""
  <div class="card">
    <h2 style="margin-bottom:16px">新會員註冊</h2>
    <form method="post" action="/register">
      <div class="form-group"><label>帳號</label><input name="username" required placeholder="自行設定" minlength="3"></div>
      <div class="form-group"><label>密碼</label><input name="password" type="password" required placeholder="自行設定" minlength="4"></div>
      <div class="form-group"><label>確認密碼</label><input name="password2" type="password" required placeholder="再輸入一次"></div>
      <div class="form-group"><label>里別（競賽分組，選擇後不可更改）</label><select name="li_name">"""+li_opts+"""</select></div>
      <div class="form-group"><input type="submit" value="註冊" class="btn btn-primary" style="width:100%"></div>
    </form>
    <p style="text-align:center;margin-top:8px;font-size:.85rem">已有帳號？<a href="/login" style="color:#1565C0">直接登入</a></p>
  </div>"""))

@app.post("/register")
async def register(request: Request,
                  username: str = Form(...), password: str = Form(...),
                  password2: str = Form(...), li_name: str = Form(...)):
    if password != password2:
        return RedirectResponse(url="/register?e=2", status_code=302)
    if len(username) < 3:
        return RedirectResponse(url="/register?e=3", status_code=302)
    db = get_db()
    if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
        db.close()
        return RedirectResponse(url="/register?e=4", status_code=302)
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode()
    db.execute("INSERT INTO users (username,password,is_admin,li_name,created_at) VALUES (?,?,0,?,?)",
               (username, hashed, li_name, datetime.now().isoformat()))
    db.commit()
    db.close()
    return RedirectResponse(url="/login?e=0", status_code=302)

# 登出
@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("token")
    if token:
        db = get_db()
        db.execute("DELETE FROM tokens WHERE token=?", (token,))
        db.commit()
        db.close()
    resp = RedirectResponse(url="/login?logged_out=1", status_code=302)
    resp.delete_cookie("token", path="/")
    return resp

# 儀表板
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_user(request)
    db = get_db()
    my_reports = db.execute(
        "SELECT * FROM temp_reports WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (user["id"],)
    ).fetchall()
    lb_rows = db.execute("""
        SELECT li_name, COUNT(*) as cnt, AVG(temp) as avg_temp
        FROM temp_reports WHERE status='approved' GROUP BY li_name ORDER BY cnt DESC
    """).fetchall()
    # 里內個人排名
    user_rank_rows = db.execute("""
        SELECT u.username, r.li_name, COUNT(r.id) as cnt
        FROM temp_reports r
        JOIN users u ON r.user_id = u.id
        WHERE r.status = 'approved'
        GROUP BY u.id, r.li_name
        ORDER BY r.li_name, cnt DESC, u.username
    """).fetchall()
    # 按里分組個人排名
    li_user_ranks = {}
    for row in user_rank_rows:
        li = row["li_name"]
        if li not in li_user_ranks:
            li_user_ranks[li] = []
        li_user_ranks[li].append(row)
    db.close()
    lb_parts = []
    for r in lb_rows:
        li = r["li_name"]
        total = r["cnt"]
        avg_t = str(round(r["avg_temp"],1)) if r["avg_temp"] else "-"
        user_rows = li_user_ranks.get(li, [])
        user_list_html = ""
        if user_rows:
            medals = ["🥇","🥈","🥉"]
            user_list_html = '<div style=margin-top:6px;padding:4px 8px;background:#f8f9fa;border-radius:6px;font-size:.8rem>'
            for i, ur in enumerate(user_rows[:10]):  # 每里最多顯示前10名
                medal = medals[i] if i < 3 else f"#{i+1}"
                user_list_html += "<div style=display:flex;justify-content:space-between><span>" + medal + " " + ur["username"] + "</span><span style=color:#555>" + str(ur["cnt"]) + "次回報</span></div>"
            user_list_html += '</div>'
        lb_parts.append(f'<div class="rank-row" style="flex-direction:column;gap:2px"><span class="rank-label">{li}</span><span class="rank-val">{total} 回報 | 均溫 {avg_t}°C</span>{user_list_html}</div>')
    lb = "".join(lb_parts) or "<p class='empty'>尚無核准資料</p>"
    rep = "".join(
        "<tr><td>"+rr["location"]+"</td><td>"+str(rr["temp"])+"°C</td>"
        "<td class='status-"+rr["status"]+"'>"+rr["status"]+"</td><td>"+str(rr["created_at"][:16])+"</td></tr>"
        for rr in my_reports
    ) or "<tr><td colspan='4' class='empty'>尚無回報</td></tr>"
    return HTMLResponse(_page("📊 儀表板",
        _gps_script() +
        '<div class="card" style="background:linear-gradient(135deg,#1565C0,#1976D2);color:#fff;padding:14px 18px;border-radius:10px;margin-bottom:14px">' +
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
        '<div style="font-size:1.1rem;font-weight:bold">🏘️ 我的里別：' + user["li_name"] + '</div>' +
        '<div style="font-size:.85rem;opacity:.85">競賽分組，回報的溫度將計入該里的總積分</div>' +
        '</div>' +
        '</div>' +
        '<div class="two-col">' +
        '<div class="card">' +
        '<h2>📍 回報測量溫度</h2>' +
        '<form method="post" action="/report">' +
        '<div class="form-group"><label>📍 地點（可輸入地址查座標）</label>' +
        '<div style="display:flex;gap:6px">' +
        '<input id="location" name="location" required placeholder="里辦公處、7-11超商、學校等" style="flex:1">' +
        '<button type="button" class="btn btn-secondary" onclick="geocodeAddress()" style="white-space:nowrap">🔍 查座標</button>' +
        '</div>' +
        '<input id="addr-input" placeholder="或輸入地址，例如：桃園區中山路123號" style="margin-top:4px;width:100%;padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:.9rem">' +
        '</div>' +
        '<div class="form-group"><label>溫度（°C）</label>' +
        '<input name="temp" type="number" step="0.1" min="20" max="50" required placeholder="例如：35.5"></div>' +
        '<div class="form-group"><label>補充說明（選填）</label>' +
        '<input name="note" placeholder="例如：下午三點、太陽直射位置"></div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">' +
        '<div class="form-group"><label>緯度</label>' +
        '<input id="lat" name="latitude" type="number" step="0.000001" min="22" max="26" placeholder="24.xxx"></div>' +
        '<div class="form-group"><label>經度</label>' +
        '<input id="lng" name="longitude" type="number" step="0.000001" min="120" max="122" placeholder="121.xxx"></div>' +
        '</div>' +
        '<div class="form-group"><span id="gps-status" class="gps-hint">📍 手機按鈕自動定位，電腦可輸入地址查座標，或直接在地圖上點選</span><br>' +
        '<button type="button" class="btn btn-secondary" onclick="getLocation()">📍 自動取得 GPS</button>' +
        '<button type="button" class="btn btn-secondary" onclick="initPickerMap()" style="margin-left:6px">🗺️ 地圖選位置</button></div>' +
        '<div id="pick-map" style="display:none;height:300px;border-radius:8px;border:2px solid #1565C0;margin-bottom:8px"></div>' +
        '<div class="form-group"><input type="submit" value="送出回報" class="btn btn-success" style="width:100%"></div>' +
        '</form>' +
        '</div>' +
        '<div class="card">' +
        '<h2>🏆 里別積分排行</h2>' +
        lb +
        '</div>' +
        '</div>' +
        '<div class="card">' +
        '<h2>📋 我的回報紀錄</h2>' +
        '<table><tr><th>地點</th><th>溫度</th><th>狀態</th><th>時間</th></tr>' +
        rep +
        '</table>' +
        '<p style="margin-top:8px"><a href="/map" class="btn btn-primary">🗺️ 開啟熱點地圖</a></p>' +
        '</div>' +
        '</div>', user=user))

@app.post("/report")
async def report(request: Request,
                location: str = Form(...), temp: float = Form(...),
                note: str = Form(""), latitude: float = Form(None), longitude: float = Form(None)):
    user = get_user(request)
    db = get_db()
    db.execute("""
        INSERT INTO temp_reports (user_id,li_name,location,latitude,longitude,temp,note,status,created_at)
        VALUES (?,?,?,?,?,?,?,'pending',?)
    """, (user["id"], user["li_name"], location, latitude, longitude, temp, note, datetime.now().isoformat()))
    db.commit()
    db.close()
    return RedirectResponse(url="/dashboard?msg=回報已送出，等待管理者審核", status_code=302)

# 地圖
@app.get("/map", response_class=HTMLResponse)
async def map_view(request: Request):
    user = get_user(request)
    db = get_db()
    reports = db.execute(
        "SELECT * FROM temp_reports WHERE status='approved' ORDER BY created_at DESC LIMIT 500"
    ).fetchall()
    layers = db.execute("SELECT * FROM layers").fetchall()
    db.close()
    import json
    r_json = json.dumps([{**dict(r)} for r in reports], default=str)
    l_json = json.dumps([dict(l) for l in layers], default=str)
    return HTMLResponse(_page("🗺️ 熱點地圖", _gps_script()+_map_script(r_json,l_json)+_map_html(), user=user))

# 管理者後台
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # 直接在這裡做管理員檢查
    token = request.cookies.get("token")
    if not token:
        return RedirectResponse(url="/login", status_code=302)
    db = get_db()
    row = db.execute(
        "SELECT u.id,u.username,u.is_admin,u.li_name FROM tokens t JOIN users u ON t.user_id=u.id WHERE t.token=? AND t.expires_at>?",
        (token, datetime.now().timestamp())
    ).fetchone()
    db.close()
    if not row or not row["is_admin"]:
        raise HTTPException(403, "需要管理者權限")
    user = dict(row)
    db = get_db()
    pending = db.execute(
        "SELECT r.*, u.username FROM temp_reports r JOIN users u ON r.user_id=u.id "
        "WHERE r.status='pending' AND r.li_name=? ORDER BY r.created_at DESC",
        (user["li_name"],)
    ).fetchall()
    layers = db.execute("SELECT * FROM layers ORDER BY li_name, type").fetchall()
    all_lis = [r["li_name"] for r in db.execute("SELECT DISTINCT li_name FROM users").fetchall()]
    # 管理員只看到自己的里
    lb_rows = db.execute("""
        SELECT li_name, COUNT(*) as cnt, AVG(temp) as avg_temp
        FROM temp_reports WHERE status='approved' AND li_name=? GROUP BY li_name ORDER BY cnt DESC
    """, (user["li_name"],)).fetchall()
    db.close()
    p = "".join(
        "<tr><td>"+x["username"]+"</td><td>"+x["li_name"]+"</td><td>"+x["location"]+"</td>"
        "<td>"+str(x["temp"])+"°C</td><td>"+str(x["created_at"][:16])+"</td>"
        "<td>"+("("+str(round(x["latitude"],5))+","+str(round(x["longitude"],5))+")" if x["latitude"] else "-")+"</td>"
        "<td><a href='/admin/approve/"+str(x["id"])+"' class='btn btn-success btn-sm'>✅核准</a> "
        "<a href='/admin/reject/"+str(x["id"])+"' class='btn btn-danger btn-sm'>❌駁回</a></td></tr>"
        for x in pending
    ) or "<tr><td colspan='7' class='empty'>沒有待審核項目</td></tr>"
    l = "".join(
        "<tr><td>"+y["li_name"]+"</td><td>"+y["type"]+"</td><td>"+y["name"]+"</td>"
        "<td>"+str(round(y["lat"],5))+","+str(round(y["lng"],5))+"</td><td>"+(y["note"] or "-")+"</td></tr>"
        for y in layers
    ) or "<tr><td colspan='5' class='empty'>尚無圖層資料</td></tr>"
    lb = "".join(
        "<tr><td>"+r["li_name"]+"</td><td>"+str(r["cnt"])+"</td><td>"+(str(round(r["avg_temp"],1)) if r["avg_temp"] else "-")+"°C</td></tr>"
        for r in lb_rows
    )
    li_opts = "".join('<option value="'+li+'">'+li+'</option>' for li in all_lis)
    type_opts = "\n".join([
        '<option value="green">🌿 綠地</option>',
        '<option value="shade">⛱️ 遮蔭處</option>',
        '<option value="water">💧 飲水機</option>',
        '<option value="care_point">🏥 關懷據點</option>',
    ])
    return HTMLResponse(_page("🔧 管理者後台","""
  <div class="card">
    <h2>📋 待審核回渡（"""+str(len(pending))+""" 筆）</h2>
    <div style="margin-bottom:10px">
      <a href="/admin/export?fmt=csv" class="btn btn-secondary" style="background:#1565C0">📥 匯出 CSV（推薦）</a>
      <a href="/admin/export?fmt=json" class="btn btn-secondary" style="background:#2E7D32">📦 匯出 JSON</a>
    </div>
    <div style="overflow-x:auto"><table>
      <tr><th>用戶</th><th>里別</th><th>地點</th><th>溫度</th><th>時間</th><th>座標</th><th>操作</th></tr>"""+p+"""
    </table></div>
  </div>
  <div class="card">
    <h2>🗺️ 圖層資料管理</h2>
    <form method="post" action="/admin/layer" class="layer-form">
      <div class="layer-grid">
        <div class="form-group"><label>里別</label><select name="li_name">"""+li_opts+"""</select></div>
        <div class="form-group"><label>類型</label><select name="type">"""+type_opts+"""</select></div>
        <div class="form-group"><label>名稱</label><input name="name" required placeholder="例：龍岡森林公園"></div>
        <div class="form-group"><label>緯度</label><input name="lat" type="number" step="0.0001" required placeholder="24.xxx"></div>
        <div class="form-group"><label>經度</label><input name="lng" type="number" step="0.0001" required placeholder="121.xxx"></div>
        <div class="form-group"><label>說明</label><input name="note" placeholder="選填"></div>
        <div class="form-group" style="display:flex;align-items:flex-end"><input type="submit" value="新增圖層" class="btn btn-success"></div>
      </div>
    </form>
    <details style="margin-top:10px"><summary style="cursor:pointer;color:#1565C0">已新增圖層（"""+str(len(layers))+""" 筆）</summary>
      <table style="margin-top:8px"><tr><th>里別</th><th>類型</th><th>名稱</th><th>座標</th><th>說明</th></tr>"""+l+"""</table>
    </details>
  </div>
  <div class="card">
    <h2>🏆 里別積分排行</h2>
    <table><tr><th>里別</th><th>已核准回報數</th><th>平均溫度</th></tr>"""+lb+"""</table>
  </div>""", user=user))

@app.post("/admin/layer")
async def admin_layer(request: Request,
                      li_name: str = Form(...), type: str = Form(...),
                      name: str = Form(...), lat: float = Form(...),
                      lng: float = Form(...), note: str = Form(""),
                      user: dict = Depends(get_admin)):
    db = get_db()
    db.execute("INSERT INTO layers (li_name,type,name,lat,lng,note) VALUES (?,?,?,?,?,?)",
               (li_name, type, name, lat, lng, note))
    db.commit()
    db.close()
    return RedirectResponse(url="/admin?msg=圖層已新增", status_code=302)

@app.get("/admin/approve/{rid}")
async def approve(request: Request, rid: int, user: dict = Depends(get_admin)):
    db = get_db()
    db.execute("UPDATE temp_reports SET status='approved', reviewed_at=? WHERE id=?",
                (datetime.now().isoformat(), rid))
    db.commit()
    db.close()
    return RedirectResponse(url="/admin?msg=已核准", status_code=302)

@app.get("/admin/reject/{rid}")
async def reject(request: Request, rid: int, user: dict = Depends(get_admin)):
    db = get_db()
    db.execute("UPDATE temp_reports SET status='rejected', reviewed_at=? WHERE id=?",
                (datetime.now().isoformat(), rid))
    db.commit()
    db.close()
    return RedirectResponse(url="/admin?msg=已駁回", status_code=302)

# ─── 匯出備份 ────────────────────────────────────────
@app.get("/admin/export")
async def admin_export(request: Request, fmt: str = "csv", user: dict = Depends(get_admin)):
    """管理員一鍵匯出所有資料（CSV 或 JSON）"""
    db = get_db()
    reports = db.execute("SELECT * FROM temp_reports ORDER BY created_at DESC").fetchall()
    layers  = db.execute("SELECT * FROM layers ORDER BY li_name, type").fetchall()
    users   = db.execute(
        "SELECT id,username,is_admin,li_name,created_at FROM users ORDER BY li_name"
    ).fetchall()
    db.close()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_data = [dict(r) for r in reports]
    layers_data  = [dict(r) for r in layers]
    users_data   = [dict(r) for r in users]

    if fmt == "json":
        import json
        content = json.dumps({
            "exported_at": timestamp,
            "temp_reports": reports_data,
            "layers": layers_data,
            "users": users_data
        }, ensure_ascii=False, indent=2)
        from fastapi.responses import Response
        return Response(
            content.encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=heatmap_backup_{timestamp}.json"}
        )

    # CSV
    import csv, io
    output = io.StringIO()
    w = csv.writer(output)

    w.writerow(["=== 溫度回報 temp_reports ==="])
    w.writerow(["id","user_id","li_name","location","latitude","longitude","temp","note","status","created_at","reviewed_at","reviewed_by"])
    for r in reports_data:
        w.writerow([r.get(c,"") for c in ["id","user_id","li_name","location","latitude","longitude","temp","note","status","created_at","reviewed_at","reviewed_by"]])

    w.writerow([])
    w.writerow(["=== 圖層資料 layers ==="])
    w.writerow(["id","li_name","type","name","lat","lng","note"])
    for r in layers_data:
        w.writerow([r.get(c,"") for c in ["id","li_name","type","name","lat","lng","note"]])

    w.writerow([])
    w.writerow(["=== 里民帳號 users ==="])
    w.writerow(["id","username","is_admin","li_name","created_at"])
    for r in users_data:
        w.writerow([r.get(c,"") for c in ["id","username","is_admin","li_name","created_at"]])

    from fastapi.responses import Response
    return Response(
        output.getvalue().encode("utf-8-sig"),  # BOM: Excel needs this for UTF-8 CSV
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=heatmap_backup_{timestamp}.csv"}
    )


# ─── 啟動 ───────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    init_db()

if __name__ == "__main__":
    init_db()
    print("""
╔══════════════════════════════════════════════╗
║  桃園市參與式高溫監測系統  v0.2           ║
║  本機：http://127.0.0.1:8000               ║
║  管理者帳號：<里名>_admin                  ║
║  管理者預設密碼：<里名>_admin               ║
║  里：                                      ║
║  - 桃園區-大林里_admin / 桃園區-大林里_admin ║
║  - 中壢區-雙連里_admin / 中壢區-雙連里_admin ║
║  - 新屋區-蚵間里_admin / 新屋區-蚵間里_admin ║
╚══════════════════════════════════════════════╝
    """)
    uvicorn.run(app, host="0.0.0.0", port=8000)

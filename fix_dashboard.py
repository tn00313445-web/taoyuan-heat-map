#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fix the dashboard HTML string bug in app.py"""

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# ── Fix 1: get_user SQL - already done, verify ──────────────────────────────
if 'if USE_PG' in content and 'WHERE t.token=%s' in content:
    content = content.replace(
        '"SELECT u.id,u.username,u.is_admin,u.li_name FROM tokens t JOIN users u ON t.user_id=u.id WHERE t.token=%s AND t.expires_at>%s" if USE_PG',
        '"SELECT u.id,u.username,u.is_admin,u.li_name FROM tokens t JOIN users u ON t.user_id=u.id WHERE t.token=? AND t.expires_at>?"'
    )
    print("Fix 1: SQL placeholders fixed")
else:
    print("Fix 1: already fixed or not found")

# ── Fix 2: dashboard triple-quote HTML ──────────────────────────────────────
# Find the exact start and end of the broken return block
start_marker = 'return HTMLResponse(_page("📊 儀表板", _gps_script()+"""'
start_idx = content.find(start_marker)
if start_idx < 0:
    print("Fix 2: start marker not found")
else:
    print(f"Fix 2: found broken block at index {start_idx}")
    # Find the end of this statement: '", user=user))'
    # It ends with: ...+rep+"""</table></p></div>""", user=user))
    # Let's find ")  user=user))" at the end
    # Actually look for the full closing
    # The string ends with: +rep+"""</table></p>\n    <p style="margin-top:8px"><a href="/map"...</div>""", user=user))
    
    # Find the next occurrence of user=user)) after start_idx
    end_search_start = start_idx + 100
    # The string ends right before the @app.post decorator
    end_marker = '\n@app.post("/report")'
    end_idx = content.find(end_marker, start_idx)
    if end_idx < 0:
        print("Fix 2: end marker not found")
    else:
        print(f"Fix 2: block ends at index {end_idx}")
        old_block = content[start_idx:end_idx]
        print(f"Fix 2: old block length = {len(old_block)}")
        
        # Build clean replacement
        new_block = '''return HTMLResponse(_page("📊 儀表板",
        _gps_script() +
        \'<div class="card" style="background:linear-gradient(135deg,#1565C0,#1976D2);color:#fff;padding:14px 18px;border-radius:10px;margin-bottom:14px">\' +
        \'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">\' +
        \'<div style="font-size:1.1rem;font-weight:bold">🏘️ 我的里別：\' + user["li_name"] + \'</div>\' +
        \'<div style="font-size:.85rem;opacity:.85">競賽分組，回報的溫度將計入該里的總積分</div>\' +
        \'</div>\' +
        \'</div>\' +
        \'<div class="two-col">\' +
        \'<div class="card">\' +
        \'<h2>📍 回報測量溫度</h2>\' +
        \'<form method="post" action="/report">\' +
        \'<div class="form-group"><label>📍 地點（可輸入地址查座標）</label>\' +
        \'<div style="display:flex;gap:6px">\' +
        \'<input id="location" name="location" required placeholder="里辦公處、7-11超商、學校等" style="flex:1">\' +
        \'<button type="button" class="btn btn-secondary" onclick="geocodeAddress()" style="white-space:nowrap">🔍 查座標</button>\' +
        \'</div>\' +
        \'<input id="addr-input" placeholder="或輸入地址，例如：桃園區中山路123號" style="margin-top:4px;width:100%;padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:.9rem">\' +
        \'</div>\' +
        \'<div class="form-group"><label>溫度（°C）</label>\' +
        \'<input name="temp" type="number" step="0.1" min="20" max="50" required placeholder="例如：35.5"></div>\' +
        \'<div class="form-group"><label>補充說明（選填）</label>\' +
        \'<input name="note" placeholder="例如：下午三點、太陽直射位置"></div>\' +
        \'<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">\' +
        \'<div class="form-group"><label>緯度</label>\' +
        \'<input id="lat" name="latitude" type="number" step="0.000001" min="22" max="26" placeholder="24.xxx"></div>\' +
        \'<div class="form-group"><label>經度</label>\' +
        \'<input id="lng" name="longitude" type="number" step="0.000001" min="120" max="122" placeholder="121.xxx"></div>\' +
        \'</div>\' +
        \'<div class="form-group"><span id="gps-status" class="gps-hint">📍 手機按鈕自動定位，電腦可輸入地址查座標，或直接在地圖上點選</span><br>\' +
        \'<button type="button" class="btn btn-secondary" onclick="getLocation()">📍 自動取得 GPS</button>\' +
        \'<button type="button" class="btn btn-secondary" onclick="initPickerMap()" style="margin-left:6px">🗺️ 地圖選位置</button></div>\' +
        \'<div id="pick-map" style="display:none;height:300px;border-radius:8px;border:2px solid #1565C0;margin-bottom:8px"></div>\' +
        \'<div class="form-group"><input type="submit" value="送出回報" class="btn btn-success" style="width:100%"></div>\' +
        \'</form>\' +
        \'</div>\' +
        \'<div class="card">\' +
        \'<h2>🏆 里別積分排行</h2>\' +
        lb +
        \'</div>\' +
        \'</div>\' +
        \'<div class="card">\' +
        \'<h2>📋 我的回報紀錄</h2>\' +
        \'<table><tr><th>地點</th><th>溫度</th><th>狀態</th><th>時間</th></tr>\' +
        rep +
        \'</table>\' +
        \'<p style="margin-top:8px"><a href="/map" class="btn btn-primary">🗺️ 開啟熱點地圖</a></p>\' +
        \'</div>\' +
        \'</div>\', user=user))
'''
        content = content[:start_idx] + new_block + content[end_idx:]
        print("Fix 2: dashboard HTML string replaced")

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

# Verify syntax
import ast
try:
    ast.parse(content)
    print("✓ Syntax OK")
except SyntaxError as e:
    print(f"✗ SyntaxError at line {e.lineno}: {e.msg}")

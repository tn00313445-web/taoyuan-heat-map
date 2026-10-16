#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fix bugs in app.py"""

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

original = content

# ── Fix 1: get_user SQL ─────────────────────────────────────────────────────
# The USE_PG branch uses %s which is NOT translated by _translate_sql
# (only ? is translated). Replace with uniform ? everywhere.
old1 = (
    '    sql = "SELECT u.id,u.username,u.is_admin,u.li_name FROM tokens t '
    'JOIN users u ON t.user_id=u.id WHERE t.token=%s AND t.expires_at>%s" '
    'if USE_PG           else "SELECT u.id,u.username,u.is_admin,u.li_name '
    'FROM tokens t JOIN users u ON t.user_id=u.id WHERE t.token=? AND t.expires_at>?"'
)
new1 = (
    '    sql = "SELECT u.id,u.username,u.is_admin,u.li_name FROM tokens t '
    'JOIN users u ON t.user_id=u.id WHERE t.token=? AND t.expires_at>?"'
)
if old1 in content:
    content = content.replace(old1, new1)
    print("Fix 1 OK: get_user SQL unified to ? placeholders")
else:
    print("Fix 1: pattern not found (may already be fixed)")
    # Try to find it
    idx = content.find('if USE_PG')
    if idx >= 0:
        print("  Found 'if USE_PG' at index", idx)
        print("  Context:", repr(content[idx-200:idx+200]))

# ── Fix 2: dashboard triple-quote HTML string ───────────────────────────────
# The multi-line string has embedded ''' which prematurely closes the outer """.
# Replace with proper single-quoted string concatenation.
# Find the exact block
old2_start = '    return HTMLResponse(_page("📊 儀表板", _gps_script()+"""'
old2_block = '''    return HTMLResponse(_page("📊 儀表板", _gps_script()+"""
    \'\'\'
  <div class="card" style="background:linear-gradient(135deg,#1565C0,#1976D2);color:#fff;padding:14px 18px;border-radius:10px;margin-bottom:14px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div style="font-size:1.1rem;font-weight:bold">🏘️ 我的里別：\'\'\' + user[\'li_name\'] + \'\'\'</div>
      <div style="font-size:.85rem;opacity:.85">競賽分組，回報的溫度將計入該里的總積分</div>
    </div>
  </div>
  \'\'\' + _gps_script() + \'\'\'
  <div class="two-col">
  <div class="two-col">'''

if old2_start in content:
    print("Found start of dashboard return block")
    # Show what follows
    idx = content.find(old2_start)
    print("Context after start:", repr(content[idx:idx+500]))
else:
    print("old2_start not found")

# Use a safer approach: find the return line and patch the specific portion
idx_dashboard = content.find('return HTMLResponse(_page("📊 儀表板"')
if idx_dashboard >= 0:
    print("dashboard return found at index", idx_dashboard)
    # The problematic section ends at the first '<div class="two-col">' after the _gps_script
    # Let's find the closing part
    idx_end_marker = content.find("'\"\"', user=user))", idx_dashboard)
    if idx_end_marker < 0:
        idx_end_marker = content.find('"\'"', idx_dashboard)
    print("End marker search result:", idx_end_marker)
    print("Content near end:", repr(content[idx_dashboard:idx_dashboard+100]))

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("\nFile written. Size:", len(content), "(was", len(original), ")")

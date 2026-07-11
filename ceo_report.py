#!/usr/bin/env python3
"""
ceo_report.py — the owner's worry list.

Builds dashboard_ceo.html: at most 15 accounts that need a business owner's
eyes RIGHT NOW — churn wording, concerning messages, badly breached urgent
threads. Nothing else.

Each row has a "handled" button: dismissing stores gid|timestamp-of-latest-
worrying-message in the browser (localStorage). The row stays hidden until a
NEW worrying message arrives in that group (new timestamp -> new key ->
reappears automatically).

Runs after report_engine.py in the same workflow; reuses its analysis.
"""
import json, os, html
from datetime import datetime, timezone

import report_engine as eng   # reuse config, classification, analyze()

here = os.path.dirname(os.path.abspath(__file__))

def esc(x): return html.escape(str(x))

def build():
    recs = json.load(open(eng.latest_messages_file()))
    as_of = datetime.now(timezone.utc)
    R = eng.analyze(recs, as_of)            # master view (all teams)
    try: AI = json.load(open(os.path.join(here, "ai_summaries.json")))
    except Exception: AI = {}
    AI_BY_GID = {k.split("|", 1)[0]: v for k, v in AI.items()}

    # ---------- pick the worry list ----------
    items = {}   # gid -> item dict (keep highest severity per group)

    def put(gid, sev, badge, badge_col, group, msg, ts, waiting=None,
            team_reply=None, status=None):
        cur = items.get(gid)
        if cur and cur["sev"] >= sev: return
        items[gid] = {"gid": gid, "sev": sev, "badge": badge, "bcol": badge_col,
                      "group": group, "msg": (msg or "")[:170], "ts": ts,
                      "waiting": waiting, "team_reply": team_reply,
                      "status": status,
                      "ai": (AI_BY_GID.get(gid) or {}).get("summary", "")}

    BAD = "#b42318"; WARN = "#b54708"; PUR = "#5b21b6"

    # 1) churn wording — the loudest alarm
    for r in R["at_risk"]:
        if r.get("n", 0) > 0:
            put(r["gid"], 100 + (10 if r["open"] else 0) + r["n"],
                "churn wording", BAD, r["group"], eng.disp(r.get("latest") or ""),
                r.get("ts"), team_reply=r.get("team_reply"),
                status=("awaiting reply" if r["open"] else "replied"))

    # 2) concerning open threads
    for a in R["attention"]:
        if a.get("concerning"):
            put(a["gid"], 90 + min(int(a["waiting_min"] // 60), 20),
                "concerning · unanswered", BAD, a["group"],
                eng.disp(a.get("last_text") or ""), a.get("last_ts"),
                waiting=a["waiting_min"], status="awaiting reply")

    # 3) badly breached urgent threads (waiting 6h+ of business concern)
    for a in R["attention"]:
        if a.get("breached") and not a.get("concerning") and a["waiting_min"] >= 360:
            put(a["gid"], 50 + min(int(a["waiting_min"] // 120), 30),
                f"breached · waiting {eng.fmt(a['waiting_min'])}", WARN, a["group"],
                eng.disp(a.get("last_text") or ""), a.get("last_ts"),
                waiting=a["waiting_min"], status="awaiting reply")

    # 4) repeated-concern watchlist groups (score-heavy, not already listed)
    for w in R["watchlist"]:
        if w.get("concern", 0) >= 2 and w.get("gid"):
            put(w["gid"], 40 + w.get("score", 0),
                f"{w['concern']} concerns this week", WARN, w["group"], "",
                None, status=w.get("status"))

    worry = sorted(items.values(), key=lambda x: -x["sev"])[:15]

    # ---------- render ----------
    rows = ""
    for it in worry:
        key = f'{it["gid"]}|{(it["ts"].isoformat() if it["ts"] else "na")}'
        ai = (f'<div class=ai>&#129302; {esc(it["ai"])}</div>' if it["ai"] else "")
        tr = it.get("team_reply")
        trh = (f'<div class=trep>&#8618; {esc((tr.get("who") or "") + ": " if tr.get("who") else "")}'
               f'{esc(tr.get("text",""))}'
               f'{(" · " + eng.when(tr["ts"]) + " IST") if tr.get("ts") else ""}</div>') if tr else ""
        sent = f'<span class=meta>sent {eng.when(it["ts"])} IST</span>' if it["ts"] else ""
        wait = f'<span class=meta>waiting {eng.fmt(it["waiting"])}</span>' if it.get("waiting") else ""
        st = ""
        if it.get("status") == "awaiting reply":
            st = f'<span class=pill style="background:#fef3f2;color:{BAD}">awaiting reply</span>'
        elif it.get("status"):
            st = f'<span class=pill style="background:#ecfdf3;color:#067647">{esc(it["status"])}</span>'
        msg = f'<div class=msg>&ldquo;{esc(it["msg"])}&rdquo; {sent}</div>' if it["msg"] else ""
        rows += (f'<div class=item data-key="{esc(key)}">'
                 f'<div class=itop><span class=badge style="color:{it["bcol"]};border-color:{it["bcol"]}">{esc(it["badge"])}</span>'
                 f'<span class=gname>{esc(it["group"])}</span>{st}{wait}'
                 f'<button class=done title="Hide until a new worrying message arrives">handled &#10003;</button></div>'
                 f'{msg}{ai}{trh}</div>')
    if not rows:
        rows = '<div style="color:#067647;font-size:16px;padding:30px;text-align:center">Nothing needs your attention right now &#10003;</div>'

    IST = eng.IST
    H = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content="300">
<meta name="robots" content="noindex,nofollow">
<title>AP Guru — Owner's worry list</title>
<style>
*{{box-sizing:border-box}} body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#15171c;margin:0;background:#eef1f6;line-height:1.5}}
.brand{{max-width:860px;margin:22px auto 0;padding:0 6px}} .brand img{{height:40px}}
.sheet{{max-width:860px;margin:14px auto 40px;background:#fff;border:1px solid #e7e9ee;border-radius:18px;padding:26px 28px;box-shadow:0 2px 8px rgba(16,24,40,.06)}}
h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#6b7280;font-size:12.5px;margin-bottom:18px}}
.item{{border:1px solid #e7e9ee;border-radius:12px;padding:13px 15px;margin-bottom:10px;background:#fff}}
.itop{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.badge{{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;border:1px solid;border-radius:20px;padding:2px 9px;white-space:nowrap}}
.gname{{font-weight:600;font-size:15px}}
.pill{{font-size:11px;padding:2px 9px;border-radius:20px;font-weight:600;white-space:nowrap}}
.meta{{font-size:11px;color:#98a2b3;white-space:nowrap}}
.done{{margin-left:auto;font-size:11px;color:#067647;background:#ecfdf3;border:1px solid #bfe8d2;border-radius:20px;padding:3px 11px;cursor:pointer}}
.done:hover{{background:#d5f2e3}}
.msg{{font-size:13px;color:#374151;margin-top:7px;font-style:italic}}
.ai{{font-size:12.5px;color:#5b21b6;margin-top:5px}}
.trep{{font-size:12px;color:#067647;margin-top:5px}}
.note{{font-size:11.5px;color:#98a2b3;margin-top:16px}}
@media(max-width:700px){{.sheet{{margin:0;border-radius:0;padding:16px 12px}}}}
</style></head><body>
<div class=brand><img src="logo.png" alt="" onerror="this.style.display='none'"></div>
<div class=sheet>
<h1>Owner's worry list</h1>
<div class=sub>Top {len(worry)} accounts needing your attention · updated {(as_of+IST).strftime('%d %b %Y, %H:%M')} IST · auto-refreshes every 5 min</div>
{rows}
<div class=note>"handled &#10003;" hides an account on this device until a NEW worrying message arrives in that group. Full dashboards: <a href="/">master view</a>.</div>
</div>
<script>
var KEY='ceo_dismissed';
function load(){{try{{return JSON.parse(localStorage.getItem(KEY))||{{}};}}catch(e){{return {{}};}}}}
function save(d){{localStorage.setItem(KEY,JSON.stringify(d));}}
var dism=load();
document.querySelectorAll('.item').forEach(function(it){{
  if(dism[it.dataset.key]) it.style.display='none';
}});
document.addEventListener('click',function(e){{
  var b=e.target.closest('.done'); if(!b) return;
  var it=b.closest('.item');
  dism[it.dataset.key]=Date.now(); save(dism);
  it.style.display='none';
}});
// prune dismissals older than 60 days so storage never bloats
(function(){{var cut=Date.now()-60*86400000,ch=false;
for(var k in dism){{if(dism[k]<cut){{delete dism[k];ch=true;}}}} if(ch) save(dism);}})();
</script>
</body></html>"""
    open(os.path.join(here, "dashboard_ceo.html"), "w").write(H)
    print(f"CEO worry list: {len(worry)} items")

if __name__ == "__main__":
    build()

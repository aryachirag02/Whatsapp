#!/usr/bin/env python3
"""
inbox_report.py — the owner's reply inbox.

Reads dms_latest.json (the owner's personal WhatsApp 1:1s, pulled read-only),
finds chats where the LAST message is from the other person (unanswered,
within 72h), classifies each as parent/lead vs personal, and drafts a reply in
the owner's own voice — using the owner's real past replies as style examples.

Output: dashboard_inbox.html — editable draft per chat + a wa.me button that
opens WhatsApp with the (possibly edited) text prefilled. Sending is always a
human tap in WhatsApp; nothing is sent via API.

Caching: one Claude call per chat per new-message (inbox_ai.json), same
pattern as ai_summaries. No key -> page still builds, drafts blank.
"""
import json, os, re, html, time, urllib.request
from datetime import datetime, timezone, timedelta

import report_engine as eng   # IST, when(), disp(), fmt_phone

here = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-haiku-4-5-20251001"
MAX_NEW = 25
STALE_H = 72

def esc(x): return html.escape(str(x))

def load_dms():
    try: return json.load(open(os.path.join(here, "dms_latest.json")))
    except Exception: return []

def build_threads(recs):
    """chat_id -> ordered list of messages (with parsed ts)."""
    th = {}
    for r in recs:
        ts = eng.parse_ts(r["timestamp"]) if hasattr(eng, "parse_ts") else None
        if ts is None:
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            except Exception: continue
        r = dict(r); r["ts"] = ts
        th.setdefault(r["group_id"], []).append(r)
    for v in th.values(): v.sort(key=lambda x: x["ts"])
    return th

def partner_of(msgs):
    """Best display name + phone digits for the other person in a 1:1."""
    name, phone = "", None
    for m in msgs:
        if not m.get("is_self"):
            nm = (m.get("sender_name") or m.get("push_name") or "").strip()
            if nm and not nm.isdigit(): name = nm
            ph = re.sub(r"\D", "", m.get("sender") or "")
            if 10 <= len(ph) <= 13: phone = ph
    if not name and phone: name = eng.fmt_phone(phone)
    return (name or "Unknown"), phone

def style_examples(threads, limit=8):
    """Recent (their message -> my reply) pairs across all chats, short ones."""
    pairs = []
    for msgs in threads.values():
        for i in range(len(msgs) - 1):
            a, b = msgs[i], msgs[i + 1]
            if (not a.get("is_self")) and b.get("is_self"):
                ta, tb = (a.get("text") or "").strip(), (b.get("text") or "").strip()
                if 5 < len(ta) < 220 and 5 < len(tb) < 300 and "cannot display" not in tb.lower():
                    pairs.append((b["ts"], ta, tb))
    pairs.sort(key=lambda x: x[0], reverse=True)
    return [(a, b) for _, a, b in pairs[:limit]]

SYSTEM = (
 "You draft WhatsApp replies for Chirag, who runs AP Guru, an online tutoring "
 "company. You will see his REAL past replies as style examples — match his "
 "tone, brevity, and phrasing exactly. Reply in his voice, first person. "
 "Classify the chat first. Respond ONLY with JSON, no markdown fences: "
 '{"kind":"parent"|"lead"|"personal"|"other","draft":"..."} '
 "kind=parent for existing students' parents; lead for prospective customers; "
 "personal for friends/family/vendors. For personal/other, draft must be an "
 "empty string. Keep drafts short and natural like the examples. If a call is "
 "the right move, suggest one the way Chirag does."
)

def call_claude(api_key, examples, convo, partner):
    ex = "\n\n".join(f"Them: {a}\nChirag: {b}" for a, b in examples) or "(no examples yet)"
    body = json.dumps({
        "model": MODEL, "max_tokens": 300, "system": SYSTEM,
        "messages": [{"role": "user", "content":
            f"STYLE EXAMPLES (Chirag's real replies):\n{ex}\n\n"
            f"CHAT with {partner} (oldest first):\n{convo}\n\nJSON:"}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    txt = " ".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.M).strip()
    return json.loads(txt)

def build():
    recs = load_dms()
    as_of = datetime.now(timezone.utc)
    threads = build_threads(recs)
    examples = style_examples(threads)

    # unanswered = last non-filler message is theirs, within 72h
    cutoff = as_of - timedelta(hours=STALE_H)
    open_chats = []
    for cid, msgs in threads.items():
        real = [m for m in msgs if (m.get("text") or "").strip() and not m.get("is_filler")]
        if not real: continue
        last = real[-1]
        if last.get("is_self"): continue
        if last["ts"] < cutoff: continue
        open_chats.append((cid, msgs, last))
    open_chats.sort(key=lambda x: -x[2]["ts"].timestamp())

    # AI classify+draft, cached per chat|last_ts
    cpath = os.path.join(here, "inbox_ai.json")
    try: cache = json.load(open(cpath))
    except Exception: cache = {}
    live_keys = {f'{cid}|{last["ts"].isoformat()}' for cid, _, last in open_chats}
    cache = {k: v for k, v in cache.items() if k in live_keys}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    new = 0
    for cid, msgs, last in open_chats:
        key = f'{cid}|{last["ts"].isoformat()}'
        if key in cache or not api_key or new >= MAX_NEW: continue
        partner, _ = partner_of(msgs)
        convo = "\n".join(
            f'{"Chirag" if m.get("is_self") else partner}: {eng.disp(m.get("text") or "")[:220]}'
            for m in msgs[-10:] if (m.get("text") or "").strip())
        try:
            out = call_claude(api_key, examples, convo, partner)
            if isinstance(out, dict) and out.get("kind"):
                cache[key] = {"kind": out["kind"], "draft": (out.get("draft") or "")[:600],
                              "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                new += 1
        except Exception as e:
            print(f"inbox ai: skipped one chat ({type(e).__name__})", flush=True)
        time.sleep(0.3)
    json.dump(cache, open(cpath, "w"))
    if new: print(f"inbox ai: {new} new drafts ({len(cache)} cached)", flush=True)

    # render (parents + leads only)
    rows = ""
    shown = 0
    for cid, msgs, last in open_chats:
        key = f'{cid}|{last["ts"].isoformat()}'
        v = cache.get(key) or {}
        kind = v.get("kind", "")
        if kind in ("personal", "other"): continue
        partner, phone = partner_of(msgs)
        badge = {"parent": ("parent", "#5b21b6"), "lead": ("lead", "#0891b2")}.get(kind, ("new", "#6b7280"))
        recent = "".join(
            f'<div class="dmsg {("me" if m.get("is_self") else "them")}">'
            f'{esc(eng.disp(m.get("text") or "")[:240])}'
            f'<span class=meta> · {eng.when(m["ts"])} IST</span></div>'
            for m in msgs[-4:] if (m.get("text") or "").strip())
        draft = esc(v.get("draft", ""))
        wa_attr = f' data-wa="{phone}"' if phone else ""
        btn = ('<button class=send' + wa_attr + '>Open in WhatsApp &#8599;</button>' if phone
               else '<span class=meta>no phone number found — open WhatsApp manually</span>')
        rows += (f'<div class=item data-key="{esc(key)}">'
                 f'<div class=itop><span class=badge style="color:{badge[1]};border-color:{badge[1]}">{badge[0]}</span>'
                 f'<span class=gname>{esc(partner)}</span>'
                 f'<span class=meta>last msg {eng.when(last["ts"])} IST</span>'
                 f'<button class=skip title="Hide until they message again">skip</button></div>'
                 f'{recent}'
                 f'<textarea class=draft rows=3 placeholder="(no draft — write your reply)">{draft}</textarea>'
                 f'<div class=actions>{btn}</div></div>')
        shown += 1
    if not rows:
        rows = '<div style="color:#067647;font-size:16px;padding:30px;text-align:center">Inbox zero — nothing awaiting your reply &#10003;</div>'

    IST = eng.IST
    H = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content="300">
<meta name="robots" content="noindex,nofollow">
<title>AP Guru — Reply inbox</title>
<style>
*{{box-sizing:border-box}} body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#15171c;margin:0;background:#eef1f6;line-height:1.5}}
.brand{{max-width:860px;margin:22px auto 0;padding:0 6px}} .brand img{{height:40px}}
.sheet{{max-width:860px;margin:14px auto 40px;background:#fff;border:1px solid #e7e9ee;border-radius:18px;padding:26px 28px;box-shadow:0 2px 8px rgba(16,24,40,.06)}}
h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#6b7280;font-size:12.5px;margin-bottom:18px}}
.item{{border:1px solid #e7e9ee;border-radius:12px;padding:13px 15px;margin-bottom:12px}}
.itop{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}}
.badge{{font-size:10.5px;font-weight:700;text-transform:uppercase;border:1px solid;border-radius:20px;padding:2px 9px}}
.gname{{font-weight:600;font-size:15px}} .meta{{font-size:11px;color:#98a2b3}}
.skip{{margin-left:auto;font-size:11px;color:#6b7280;background:#f3f4f6;border:1px solid #e7e9ee;border-radius:20px;padding:3px 11px;cursor:pointer}}
.dmsg{{font-size:13px;margin:4px 0;padding:7px 11px;border-radius:10px;max-width:92%}}
.dmsg.them{{background:#f4f5f7}} .dmsg.me{{background:#e7f8ef;margin-left:auto}}
.draft{{width:100%;margin-top:9px;font:13.5px/1.5 inherit;padding:9px 11px;border:1px solid #d7dbe3;border-radius:10px;resize:vertical}}
.actions{{margin-top:8px;display:flex;justify-content:flex-end}}
.send{{font-size:13px;font-weight:600;color:#fff;background:#128c4b;border:none;border-radius:22px;padding:8px 18px;cursor:pointer}}
.send:hover{{background:#0e7a40}}
.note{{font-size:11.5px;color:#98a2b3;margin-top:16px}}
@media(max-width:700px){{.sheet{{margin:0;border-radius:0;padding:16px 12px}}}}
</style></head><body>
<div class=brand><img src="logo.png" alt="" onerror="this.style.display='none'"></div>
<div class=sheet>
<h1>Reply inbox</h1>
<div class=sub>{shown} unanswered parent/lead chats · last 72h, newest first · updated {(as_of+IST).strftime('%d %b %Y, %H:%M')} IST · drafts are in your voice — edit, then Open in WhatsApp and tap send</div>
{rows}
<div class=note>"skip" hides a chat on this device until they message again. Nothing is ever sent automatically. <a href="/ceo">Worry list</a> · <a href="/">Master view</a></div>
</div>
<script>
var KEY='inbox_skipped';
function load(){{try{{return JSON.parse(localStorage.getItem(KEY))||{{}};}}catch(e){{return {{}};}}}}
function save(d){{localStorage.setItem(KEY,JSON.stringify(d));}}
var dism=load(); var hidden=0;
document.querySelectorAll('.item').forEach(function(it){{
  if(dism[it.dataset.key]){{it.style.display='none';hidden++;}}
}});
if(hidden){{
  var note=document.querySelector('.note');
  var a=document.createElement('a'); a.href='#';
  a.textContent=' Show '+hidden+' skipped';
  a.style.cssText='margin-left:8px;color:#5b21b6;font-weight:600';
  a.onclick=function(ev){{ev.preventDefault();localStorage.removeItem(KEY);location.reload();}};
  note.appendChild(a);
}}
document.addEventListener('click',function(e){{
  var sk=e.target.closest('.skip');
  if(sk){{var it=sk.closest('.item');dism[it.dataset.key]=Date.now();save(dism);it.style.display='none';return;}}
  var b=e.target.closest('.send');
  if(b){{
    var it=b.closest('.item');
    var txt=it.querySelector('.draft').value;
    window.open('https://wa.me/+'+b.dataset.wa+'?text='+encodeURIComponent(txt),'_blank');
  }}
}});
// prune old skips
(function(){{var cut=Date.now()-60*86400000,ch=false;
for(var k in dism){{if(dism[k]<cut){{delete dism[k];ch=true;}}}} if(ch) save(dism);}})();
</script>
</body></html>"""
    open(os.path.join(here, "dashboard_inbox.html"), "w").write(H)
    print(f"Reply inbox: {shown} chats shown ({len(open_chats)} unanswered incl. personal)")

if __name__ == "__main__":
    build()

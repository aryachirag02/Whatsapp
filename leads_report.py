#!/usr/bin/env python3
"""
leads_report.py — website leads, cross-checked against the owner's WhatsApp.

Pipeline each run:
  1. Pull recent form submissions from Webflow (WEBFLOW_TOKEN; site/forms
     auto-discovered). Merged into webflow_leads.json (cached between runs).
  2. For each lead, normalize the phone and look it up in the owner's DM store
     (dms_latest.json):
        - no chat found            -> NEW: draft the first-touch message
        - owner messaged, silent
          for FOLLOWUP_HOURS+      -> FOLLOW-UP DUE: WhatsApp follow-up draft
                                       + prefilled email (mailto)
        - lead replied             -> REPLIED (they're in the reply inbox)
        - owner messaged recently  -> WAITING (no action yet)
     Indian numbers (+91) are excluded per policy.
  3. AI (cached per lead) extracts the program, works out the lead's US
     timezone window, and writes the messages in the owner's template/voice.

Output: dashboard_leads.html. All sending is human: wa.me / mailto prefills.
"""
import json, os, re, html, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

import report_engine as eng

here = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-haiku-4-5-20251001"
MAX_NEW_AI = 60
FOLLOWUP_HOURS = 48
LEAD_WINDOW_DAYS = 14
FROM_EMAIL = "aryachirag@apguru.com"

def esc(x): return html.escape(str(x))
def _iso(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

# ---------------- Webflow fetch ----------------
def wf_get(path, token, params=None):
    url = "https://api.webflow.com/v2/" + path.lstrip("/")
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def _store_sub(leads, sub, form_name=""):
    resp = sub.get("formResponse") or sub.get("data") or sub.get("response") or {}
    leads[sub.get("id")] = {"id": sub.get("id"), "form": form_name or sub.get("displayName") or "",
                            "submitted": sub.get("dateSubmitted") or sub.get("createdOn") or "",
                            "fields": resp}

def fetch_webflow_leads(token):
    """All submissions across all sites — site-level endpoint first (covers
    every form in one sweep), per-form endpoint as fallback."""
    leads = {}
    sites = wf_get("sites", token).get("sites", [])
    for s in sites:
        # preferred: site-wide submissions (avoids per-form 404s)
        try:
            offset = 0
            while True:
                data = wf_get(f"sites/{s['id']}/form_submissions", token,
                              {"limit": 100, "offset": offset})
                subs = data.get("formSubmissions") or data.get("submissions") or []
                for sub in subs: _store_sub(leads, sub)
                total = (data.get("pagination") or {}).get("total", 0)
                offset += 100
                if offset >= total or not subs: break
                time.sleep(0.2)
            print(f"leads: site '{s.get('displayName')}': {len(leads)} submissions collected (site-wide)", flush=True)
            continue   # site-level worked; skip per-form for this site
        except Exception as e:
            print(f"leads: site-wide fetch unavailable for '{s.get('displayName')}' ({type(e).__name__}) — trying per-form", flush=True)
        try: forms = wf_get(f"sites/{s['id']}/forms", token).get("forms", [])
        except Exception as e:
            print(f"leads: forms fetch failed for site {s.get('displayName')}: {e}", flush=True); continue
        for f in forms:
            got = 0
            offset = 0
            while True:
                try:
                    data = wf_get(f"forms/{f['id']}/submissions", token,
                                  {"limit": 100, "offset": offset})
                except Exception as e:
                    print(f"leads: submissions fetch failed ({f.get('displayName')}): {e}", flush=True); break
                subs = data.get("formSubmissions") or data.get("submissions") or []
                got += len(subs)
                for sub in subs: _store_sub(leads, sub, f.get("displayName") or "")
                total = (data.get("pagination") or {}).get("total", 0)
                offset += 100
                if offset >= total or not subs: break
                time.sleep(0.2)
            print(f"leads: form '{f.get('displayName')}' ({s.get('displayName')}): {got} submissions", flush=True)
    return leads

# ---------------- lead parsing ----------------
def _field(fields, *cands):
    """Field lookup: exact (normalized) key first, then fuzzy contains —
    preferring the longest value so e.g. a 'Program field' dropdown never
    shadows the real 'field' message box."""
    low = {re.sub(r"[^a-z]", "", k.lower()): v for k, v in fields.items()}
    for c in cands:                      # pass 1: exact
        cc = re.sub(r"[^a-z]", "", c.lower())
        if low.get(cc): return str(low[cc]).strip()
    best = ""
    for c in cands:                      # pass 2: fuzzy, longest value wins
        cc = re.sub(r"[^a-z]", "", c.lower())
        for k, v in low.items():
            if (cc in k or k in cc) and v and len(str(v)) > len(best):
                best = str(v).strip()
    return best

_DIAL_HINTS=[  # (keywords in location/school, dial code)
 (("united states","usa"," us","florida","california","texas","new york","jersey","illinois","georgia",
   "virginia","carolina","washington","massachusetts","pennsylvania","ohio","michigan","arizona",
   "colorado","seattle","boston","chicago","houston","dallas","austin","atlanta","miami","jacksonville",
   "san francisco","los angeles","san jose","san diego","denver","phoenix","charlotte","nashville",
   "minneapolis","portland","connecticut","maryland","tennessee","missouri","indiana","wisconsin"),"1"),
 (("canada","toronto","vancouver","ontario","calgary","montreal","ottawa"),"1"),
 (("united kingdom"," uk","london","manchester","birmingham","england","scotland","surrey","kent"),"44"),
 (("uae","dubai","abu dhabi","sharjah","emirates"),"971"),
 (("singapore",),"65"), (("hong kong",),"852"), (("qatar","doha"),"974"),
 (("saudi","riyadh","jeddah","dammam"),"966"), (("kuwait",),"965"), (("bahrain",),"973"),
 (("oman","muscat"),"968"), (("australia","sydney","melbourne","perth","brisbane"),"61"),
 (("nigeria","lagos","abuja"),"234"), (("kenya","nairobi"),"254"), (("tanzania","dar es salaam"),"255"),
 (("switzerland","zurich","geneva"),"41"), (("germany","berlin","munich","frankfurt"),"49"),
 (("netherlands","amsterdam"),"31"), (("japan","tokyo"),"81"), (("thailand","bangkok"),"66"),
 (("indonesia","jakarta"),"62"), (("malaysia","kuala lumpur"),"60"),
 (("india","mumbai","delhi","bangalore","bengaluru","chennai","hyderabad","pune","kolkata","gurgaon","gurugram","noida","ahmedabad","jaipur","lucknow","chandigarh","indore","surat","nagpur","bhopal","patna","kochi","cochin","coimbatore","vadodara","thane","goa","dehradun","bhubaneswar","visakhapatnam","mysore","mysuru"),"91"),
]
def infer_dial(loc, school):
    t=f"{loc} {school}".lower()
    for keys,dial in _DIAL_HINTS:
        if any(k in t for k in keys): return dial
    return None

_GULF=("uae","dubai","abu dhabi","sharjah","emirates","qatar","doha","saudi","riyadh","jeddah",
       "kuwait","bahrain","oman","muscat")
def attach_files(program, loc, school, phone):
    """Marketing files to attach, based on program + region (USD default, AED for Gulf)."""
    p=(program or "").lower(); t=f"{loc} {school}".lower()
    aed = any(k in t for k in _GULF) or (phone or "").startswith(("971","966","965","973","968","974"))
    cur = "AED" if aed else "USD"
    f=[]
    if "sat" in p or "psat" in p:
        f=["SAT Brochure.pdf","SAT Scores 2025-26.pdf"]
        f.append("SAT (AED).jpeg" if aed else "SAT (USD).jpeg")
        if "psat" in p: f.append("PSAT Prep.png")
    elif "act" in p: f=["SAT Brochure.pdf","SAT Scores 2025-26.pdf"]
    elif p.startswith("ap") or " ap " in f" {p} ":
        f=["AP Guru_s 2025 AP Scores.pdf", f"AP {cur}.png"]
    elif "myp" in p: f=[f"MYP {cur}.png"]
    elif "ib" in p: f=[f"IB {cur}.png"]
    elif "igcse" in p or "gcse" in p: f=[f"IGCSE {cur}.png"]
    elif "a-level" in p or "alevel" in p or "a level" in p: f=[f"ALEVEL {cur}.png"]
    else:
        for k,fn in (("gmat","GMAT.png"),("gre","GRE.png"),("ucat","UCAT.png"),("tmua","TMUA.png"),
                     ("lnat","LNAT.png"),("esat","ESAT.png"),("step","STEP.png"),("amc","AMC.png")):
            if k in p: f=[fn]; break
    return ["marketing/"+x for x in f]

def parse_lead(raw):
    f = raw["fields"]
    first = _field(f, "first name", "firstname", "name")
    last  = _field(f, "last name", "lastname")
    email = _field(f, "email")
    dial  = re.sub(r"\D", "", _field(f, "dial code", "dialcode", "country code"))
    phone = re.sub(r"\D", "", _field(f, "phone number", "phone", "mobile", "whatsapp"))
    loc   = _field(f, "location", "city", "state", "country")
    school= _field(f, "school name", "school")
    msg   = _field(f, "field", "message", "requirement", "details", "comments")
    # some leads put the FULL number in the dial-code field
    if not phone and len(dial) >= 10:
        phone, dial = dial, ""
    # phone may already embed the dial code, or appear inside the message
    if not phone:
        m = re.search(r"\+?(\d[\d\s\-()]{8,})", msg or "")
        if m: phone = re.sub(r"\D", "", m.group(1))
    full = phone
    if dial and phone and not phone.startswith(dial): full = dial + phone
    return {"id": raw["id"], "first": first or "there", "last": last, "email": email,
            "phone": full, "loc": loc, "school": school, "msg": msg,
            "submitted": _iso(raw.get("submitted") or "")}

# ---------------- DM cross-check ----------------
_WA_CACHE_P = os.path.join(here, "wa_check.json")
def wa_checker():
    """check(phone)->True/False/None(unknown). Cached forever per number."""
    try: cache = json.load(open(_WA_CACHE_P))
    except Exception: cache = {}
    key = os.environ.get("UNIPILE_API_KEY", "").strip()
    try: cfg = json.load(open(os.path.join(here, "config.json")))
    except Exception: cfg = {}
    acct = cfg.get("dm_account"); dsn = cfg.get("dsn", "https://api55.unipile.com:18582")
    calls = {"n": 0}
    def check(phone):
        if not phone: return None
        if phone in cache: return cache[phone]
        if not (key and acct) or calls["n"] >= 30: return None
        calls["n"] += 1
        try:
            req = urllib.request.Request(f"{dsn}/api/v1/users/{phone}?account_id={acct}",
                headers={"X-API-KEY": key, "accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ok = (r.status == 200)
        except urllib.error.HTTPError as e:
            ok = False if e.code == 404 else None
            if ok is None: print(f"leads wa-check: HTTP {e.code} for a lookup (treating as unknown)", flush=True)
        except Exception as e:
            ok = None
            print(f"leads wa-check: {type(e).__name__} (treating as unknown)", flush=True)
        if ok is not None: cache[phone] = ok
        return ok
    stats={"yes":0,"no":0,"unk":0,"err":""}
    _check=check
    def check2(phone):
        r=_check(phone)
        if r is True: stats["yes"]+=1
        elif r is False: stats["no"]+=1
        else: stats["unk"]+=1
        return r
    def save():
        json.dump(cache, open(_WA_CACHE_P, "w"))
        print(f"leads wa-check: {stats['yes']} on WhatsApp, {stats['no']} not, {stats['unk']} unknown", flush=True)
    return check2, save

def dm_index():
    """last-10-digits of partner phone -> chat status."""
    try: recs = json.load(open(os.path.join(here, "dms_latest.json")))
    except Exception: recs = []
    chats = {}; raw_by_chat = {}
    for r in recs:
        ts = _iso(r["timestamp"])
        if not ts: continue
        chats.setdefault(r["group_id"], []).append((ts, bool(r.get("is_self")),
                                                    r.get("sender") or ""))
        raw_by_chat.setdefault(r["group_id"], []).append(r)
    idx = {}
    for cid, msgs in chats.items():
        msgs.sort()
        ph = None
        selfd = set()
        for _, self_, sender in msgs:
            d = re.sub(r"\D", "", sender)
            if self_: selfd.add(d)
            elif not ph and 10 <= len(d) <= 13: ph = d
        if not ph:
            # outbound-only chat (sent first touch, no reply yet):
            # take the attendee number that isn't the owner's
            for m in raw_by_chat.get(cid, []):
                for a in (m.get("att") or []):
                    if a not in selfd: ph = a; break
                if ph: break
        if not ph: continue
        last_ts, last_self, _ = msgs[-1]
        first_out = next((t for t, s, _ in msgs if s), None)
        idx[ph[-10:]] = {"last_ts": last_ts, "last_self": last_self,
                         "contacted": first_out is not None,
                         "they_replied_after": any((not s) and first_out and t > first_out
                                                   for t, s, _ in msgs)}
    return idx

# ---------------- AI drafting ----------------
SYSTEM = (
 "You write WhatsApp outreach for Chirag, founder of AP Guru (online 1-to-1 "
 "tutoring). Respond ONLY with JSON, no fences: "
 '{"program":"...","first_touch":"...","follow_up":"...","email_subject":"...","email_body":"..."} '
 "Rules: program = what they asked about (SAT, ACT, IB, AP, IGCSE, A-Level, GMAT, GRE...). "
 "first_touch must follow Chirag's template exactly:\n"
 "Hi {first name},\\n\\nThis is Chirag from AP Guru. This is regarding {program} prep{for_whom} - "
 "you sent a message on our website.\\n\\nIt would be easier to discuss it over a call. "
 "{for_whom}: if their message says who it's for (my daughter/son/child), add "
 "' for your daughter' / ' for your son' / ' for your child'; otherwise omit entirely. "
 "Will you be available anytime between {window} this week?\\n"
 "The window: Chirag's slots are 7 am - 2 pm OR post 10 pm, EASTERN. To pick the zone: "
 "FIRST identify the US state from the location and school (e.g. 'Central Jersey College Prep' "
 "-> New Jersey -> eastern). IGNORE words like Central/Pacific/Mountain appearing inside school "
 "or city NAMES — they are not timezones. Map the state to its zone, then convert BOTH slots and "
 "phrase as 'X am - Y pm or post Z pm {zone} time': "
 "eastern '7 am - 2 pm or post 10 pm eastern time'; central '6 am - 1 pm or post 9 pm central time'; "
 "mountain '5 am - 12 pm or post 8 pm mountain time'; pacific '4 am - 11 am or post 7 pm pacific time'. "
 "If location is non-US or unclear, use '7 am - 2 pm or post 10 pm eastern time'. "
 "follow_up: one short friendly nudge in the same voice referencing the earlier message. "
 "email_subject/email_body: a brief email version of the follow-up, signed 'Chirag | AP Guru'. "
 "Also return \"country_dial\": the international dial code digits for the lead's country inferred from Location AND School Name (a school name often pins the city/state - use it). For the window, the school/city determines the US timezone; be precise."
)

def draft(api_key, lead):
    body = json.dumps({"model": MODEL, "max_tokens": 500, "system": SYSTEM,
        "messages": [{"role": "user", "content":
            f"Lead: {lead['first']} {lead['last']}\nLocation: {lead['loc']}\n"
            f"School: {lead['school']}\nTheir message: {lead['msg']}\n\nJSON:"}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    txt = " ".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.M).strip()
    return json.loads(txt)

# ---------------- main ----------------
def build():
    as_of = datetime.now(timezone.utc)
    token = os.environ.get("WEBFLOW_TOKEN", "").strip()

    lpath = os.path.join(here, "webflow_leads.json")
    try: store = json.load(open(lpath))
    except Exception: store = {}
    fetch_note = ""
    if token:
        try:
            store.update(fetch_webflow_leads(token))
            print(f"leads: {len(store)} submissions in store", flush=True)
        except urllib.error.HTTPError as e:
            hint = {401: "token invalid — re-check the WEBFLOW_TOKEN secret",
                    403: "token missing a scope — needs Sites: Read AND Forms: Read",
                    404: "endpoint not found — token may be for the wrong workspace"}.get(e.code, "")
            fetch_note = f"Webflow fetch failed: HTTP {e.code}" + (f" ({hint})" if hint else "")
            try: print("leads: " + fetch_note + " | body: " + e.read()[:200].decode("utf-8","ignore"), flush=True)
            except Exception: print("leads: " + fetch_note, flush=True)
        except Exception as e:
            fetch_note = f"Webflow fetch failed: {type(e).__name__}"
            print("leads: " + fetch_note, flush=True)
    else:
        fetch_note = "WEBFLOW_TOKEN not set — showing cached leads only"
    json.dump(store, open(lpath, "w"))

    cutoff = as_of - timedelta(days=LEAD_WINDOW_DAYS)
    leads = []
    skipped_india = 0; skipped_nodate = 0; skipped_old = 0
    for raw in store.values():
        L = parse_lead(raw)
        if not L["submitted"]: skipped_nodate += 1; continue
        if L["submitted"] < cutoff: skipped_old += 1; continue
        dial = infer_dial(L["loc"], L["school"])
        # add the country code when the number came in bare (<=10 digits)
        if L["phone"] and len(L["phone"]) <= 10 and dial:
            L["phone"] = dial + L["phone"]
        L["dial_guess"] = dial
        # India exclusion (layered): +91 phone, inferred Indian dial, or Indian location text
        _is_india = (
            (L["phone"].startswith("91") and len(L["phone"]) == 12) or
            (dial == "91") or
            ("india" in f'{L["loc"]} {L["school"]}'.lower())
        )
        if _is_india:
            skipped_india += 1
            if skipped_india <= 5:
                print(f"leads: excluded (India): {L['first']} {L['last']} · {L['loc']}", flush=True)
            continue
        leads.append(L)
    leads.sort(key=lambda x: -x["submitted"].timestamp())
    print(f"leads: window={len(leads)} | outside {LEAD_WINDOW_DAYS}d={skipped_old} | no-date={skipped_nodate} | india={skipped_india}", flush=True)
    # dedupe repeat submissions from the same phone: keep newest card, but
    # merge in the richer message/school/location from older submissions
    _by={} ; _order=[]
    for L in leads:                      # leads are newest-first
        k=L["phone"][-10:] if L["phone"] else L["id"]
        if k not in _by:
            _by[k]=L; _order.append(k)
        else:
            base=_by[k]
            if len(L.get("msg") or "") > len(base.get("msg") or ""): base["msg"]=L["msg"]
            for fld in ("school","loc","email","first","last"):
                if not base.get(fld) and L.get(fld): base[fld]=L[fld]
    leads=[_by[k] for k in _order]

    try: LFLAGS=json.load(open(os.path.join(here,"lead_flags.json")))
    except Exception: LFLAGS={}
    MANUAL=set(LFLAGS.keys())
    n_manual=sum(1 for L in leads if L["id"] in MANUAL)
    leads=[L for L in leads if L["id"] not in MANUAL]

    idx = dm_index()
    for L in leads:
        st = idx.get(L["phone"][-10:]) if L["phone"] else None
        L["prior"] = st   # any existing chat with this number = history worth seeing
        if not st or not st["contacted"]:
            L["status"] = "new"
        elif st["they_replied_after"] and not st["last_self"]:
            L["status"] = "replied"
        elif st["last_self"] and (as_of - st["last_ts"]) > timedelta(hours=FOLLOWUP_HOURS):
            L["status"] = "followup"
        else:
            L["status"] = "waiting"

    # WhatsApp presence for actionable leads (cached; capped per run)
    try: WAFLAGS=set(json.load(open(os.path.join(here,"wa_flags.json"))).keys())
    except Exception: WAFLAGS=set()
    wa_check, wa_save = wa_checker()
    for L in leads:
        if L["status"] in ("new", "followup") and L["phone"]:
            if L["phone"] in WAFLAGS or L["phone"][-10:] in {p[-10:] for p in WAFLAGS}:
                L["wa"] = False          # you marked it: not on WhatsApp
            else:
                L["wa"] = wa_check(L["phone"])
    wa_save()

    # AI drafts (cached per lead id)
    apath = os.path.join(here, "leads_ai.json")
    try: acache = json.load(open(apath))
    except Exception: acache = {}
    acache = {k: v for k, v in acache.items() if v.get("v") == 4}   # template v4 only
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    new = 0
    for L in leads:
        if L["status"] in ("replied", "waiting"): continue
        if L["id"] in acache or not api_key or new >= MAX_NEW_AI: continue
        try:
            d = draft(api_key, L); d["v"] = 4
            acache[L["id"]] = d; new += 1
        except Exception as e:
            print(f"leads ai: skipped one ({type(e).__name__})", flush=True)
        time.sleep(0.3)
    json.dump(acache, open(apath, "w"))
    if new: print(f"leads ai: {new} new drafts", flush=True)
    # AI fallback: bare numbers where the keyword map couldn't infer a dial code
    for L in leads:
        if L["phone"] and len(L["phone"]) <= 10 and not L.get("dial_guess"):
            cd = re.sub(r"\D", "", str((acache.get(L["id"]) or {}).get("country_dial") or ""))
            if 1 <= len(cd) <= 3:
                L["phone"] = cd + L["phone"]
    # final net: AI judged the lead Indian (dial 91) -> exclude
    _keep = []
    for L in leads:
        if re.sub(r"\D", "", str((acache.get(L["id"]) or {}).get("country_dial") or "")) == "91" \
           or (L["phone"].startswith("91") and len(L["phone"]) == 12):
            skipped_india += 1
            print(f"leads: excluded (India, AI net): {L['first']} {L['last']}", flush=True)
            continue
        _keep.append(L)
    leads = _keep

    # ---------------- render ----------------
    def _prior_tag(L):
        p = L.get("prior")
        if not p: return ""
        who = "they messaged you" if not p["last_self"] else "prior chat"
        return (f'<span class=hist title="A WhatsApp conversation with this number already exists">'
                f'&#128172; {who} · last {eng.when(p["last_ts"])} IST</span>')

    def card(L, mode):
        d = acache.get(L["id"]) or {}
        prog = d.get("program", "")
        txt = d.get("first_touch" if mode == "new" else "follow_up", "")
        sub = f'{esc(L["loc"])}{" · " + esc(L["school"]) if L["school"] else ""}'
        badge = ("new lead", "#0891b2") if mode == "new" else ("follow-up due", "#b54708")
        from urllib.parse import quote
        mail = ""
        if L["email"]:
            if mode == "followup":
                msub, mbody = d.get("email_subject","Following up - AP Guru"), d.get("email_body","")
            else:
                msub, mbody = f'{(prog + " prep - ") if prog else ""}AP Guru', txt
            _files = attach_files(prog, L["loc"], L["school"], L["phone"])
            _links = [("https://chirag.apguru.com/" + "/".join(quote(seg) for seg in f.split("/")),
                       f.split("/")[-1].rsplit(".",1)[0]) for f in _files]
            mail = (f'<button class=sendmail data-to="{esc(L["email"])}" '
                    f'data-sub="{esc(msub)}" data-mode="{mode}" '
                    f'data-links="{esc(json.dumps(_links))}" '
                    f'data-ebody="{esc(mbody if mode=="followup" else "")}">'
                    f'Open in Gmail &#9993;{(" (+" + str(len(_links)) + " links)") if _links else ""}</button>')
        if L["phone"] and L.get("wa") is False:
            wa = '<span class=meta>not on WhatsApp — email them</span>'
        elif L["phone"]:
            wa = (f'<button class=send data-wa="{L["phone"]}">Open in WhatsApp &#8599;</button>'
                  f'<button class=nowa data-ph="{L["phone"]}" title="This number is not on WhatsApp — switch this lead to email">no WhatsApp?</button>')
        else:
            wa = '<span class=meta>no phone parsed</span>'

        details = ""
        if L["loc"]:    details += f'<div class=frow><b>Location:</b> {esc(L["loc"])}</div>'
        if L["school"]: details += f'<div class=frow><b>School:</b> {esc(L["school"])}</div>'
        if L["msg"]:    details += f'<div class=frow><b>Message:</b> &ldquo;{esc(L["msg"][:400])}&rdquo;</div>'
        return (f'<div class=item data-key="{esc(L["id"])}-{mode}">'
                f'<div class=itop><span class=badge style="color:{badge[1]};border-color:{badge[1]}">{badge[0]}</span>'
                f'<span class=gname>{esc(L["first"])} {esc(L["last"])}</span>'
                f'{f"<span class=pill>{esc(prog)}</span>" if prog else ""}'
                f'{_prior_tag(L)}'
                f'<span class=meta>submitted {eng.when(L["submitted"])} IST</span>'
                f'<button class=already data-lid="{esc(L["id"])}" data-name="{esc(L["first"])} {esc(L["last"])}" '
                f'data-kind="{"no_followup" if mode == "followup" else "already_messaged"}" '
                f'title="Remove permanently — the system records why">'
                f'{"no follow-up" if mode == "followup" else "already messaged"}</button>'
                f'<button class=already data-lid="{esc(L["id"])}" data-name="{esc(L["first"])} {esc(L["last"])}" '
                f'data-kind="not_relevant" data-ask="1" '
                f'title="Not a real lead — remove permanently with a reason">not relevant</button></div>'
                f'{details}'
                f'<textarea class=draft rows=4 data-ebody="{esc(d.get("email_body","") if mode=="followup" else "")}">{esc(txt)}</textarea>'
                f'<div class=actions>{mail}{wa}</div></div>')

    new_rows = "".join(card(L, "new") for L in leads if L["status"] == "new")
    fu_rows = "".join(card(L, "followup") for L in leads if L["status"] == "followup")
    n_wait = sum(1 for L in leads if L["status"] == "waiting")
    n_rep = sum(1 for L in leads if L["status"] == "replied")
    if not new_rows: new_rows = '<div class=empty>No uncontacted leads &#10003;</div>'
    if not fu_rows: fu_rows = '<div class=empty>No follow-ups due &#10003;</div>'
    note_extra = f' · {esc(fetch_note)}' if fetch_note else ''

    _kl={"already_messaged":"already messaged","no_followup":"no follow-up","not_relevant":"not relevant"}
    _mrows=""
    for lid,meta in sorted(LFLAGS.items(), key=lambda kv:(kv[1] or {}).get("ts",""), reverse=True)[:60]:
        meta=meta or {}
        _mrows+=(f'<div class=mrow><b>{esc(meta.get("name") or lid)}</b>'
                 f' · {esc(_kl.get(meta.get("kind"),"muted"))}'
                 f'{(" · &ldquo;"+esc(meta.get("reason",""))+"&rdquo;") if meta.get("reason") else ""}'
                 f'<span class=meta> · {esc((meta.get("ts") or "")[:10])}</span></div>')
    muted_html=(f'<details style="margin-top:14px"><summary>Muted leads — {len(LFLAGS)} '
                f'(already messaged / no follow-up / not relevant)</summary>{_mrows}</details>'
                if LFLAGS else "")

    IST = eng.IST
    H = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content="300">
<meta name="robots" content="noindex,nofollow">
<title>AP Guru — Leads</title>
<style>
*{{box-sizing:border-box}} body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#15171c;margin:0;background:#eef1f6;line-height:1.5}}
.brand{{max-width:860px;margin:22px auto 0;padding:0 6px}} .brand img{{height:40px}}
.sheet{{max-width:860px;margin:14px auto 40px;background:#fff;border:1px solid #e7e9ee;border-radius:18px;padding:26px 28px;box-shadow:0 2px 8px rgba(16,24,40,.06)}}
h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#6b7280;font-size:12.5px;margin-bottom:14px}}
h2{{font-size:14px;background:#f5f7fb;border-left:3px solid #16243f;padding:8px 12px;border-radius:8px}}
.item{{border:1px solid #e7e9ee;border-radius:12px;padding:13px 15px;margin-bottom:12px}}
.itop{{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:6px}}
.badge{{font-size:10.5px;font-weight:700;text-transform:uppercase;border:1px solid;border-radius:20px;padding:2px 9px}}
.gname{{font-weight:600;font-size:15px}} .meta{{font-size:11px;color:#98a2b3}}
.pill{{font-size:11px;background:#eef2ff;color:#5b21b6;border-radius:20px;padding:2px 9px;font-weight:600}}
.hist{{font-size:11px;background:#fff7e6;color:#b54708;border:1px solid #fde3b8;border-radius:20px;padding:2px 9px;font-weight:600;white-space:nowrap}}
.already{{margin-left:auto;font-size:11px;color:#98a2b3;background:none;border:none;text-decoration:underline;cursor:pointer;padding:2px 4px}}
.already:hover{{color:#0891b2}}
.nowa{{font-size:11px;color:#98a2b3;background:none;border:none;text-decoration:underline;cursor:pointer;align-self:center}}
.nowa:hover{{color:#b54708}}
.skip{{font-size:11px;color:#067647;background:#ecfdf3;border:1px solid #bfe8d2;border-radius:20px;padding:3px 11px;cursor:pointer}}
.msg{{font-size:13px;color:#374151;font-style:italic;margin:4px 0}}
.frow{{font-size:12.5px;color:#374151;margin:2px 0}} .frow b{{color:#6b7280;font-weight:600}}
.draft{{width:100%;margin-top:8px;font:13.5px/1.5 inherit;padding:9px 11px;border:1px solid #d7dbe3;border-radius:10px;resize:vertical}}
.actions{{margin-top:8px;display:flex;gap:8px;justify-content:flex-end}}
.send{{font-size:13px;font-weight:600;color:#fff;background:#128c4b;border:none;border-radius:22px;padding:8px 18px;cursor:pointer}}
.sendmail{{font-size:13px;font-weight:600;color:#fff;background:#16243f;border:none;border-radius:22px;padding:8px 16px;cursor:pointer}}
.sendmail:disabled{{background:#94a3b8}}
.mailbtn{{font-size:13px;font-weight:600;color:#16243f;background:#eef2ff;border:1px solid #d7dbe3;border-radius:22px;padding:8px 16px;text-decoration:none}}
.empty{{color:#067647;padding:12px 4px;font-size:13.5px}}
.mrow{{font-size:12.5px;color:#374151;padding:5px 2px;border-bottom:1px solid #f1f3f7}}
.note{{font-size:11.5px;color:#98a2b3;margin-top:16px}}
@media(max-width:700px){{.sheet{{margin:0;border-radius:0;padding:16px 12px}}}}
</style></head><body>
<div class=brand><img src="logo.png" alt="" onerror="this.style.display='none'"></div>
<div class=sheet>
<h1>Website leads</h1>
<div class=sub>last {LEAD_WINDOW_DAYS} days, newest first · {n_wait} awaiting their reply · {n_rep} replied (see <a href="/inbox">inbox</a>) · {skipped_india} Indian numbers excluded · {n_manual} marked already messaged · updated {(as_of+IST).strftime('%d %b %Y, %H:%M')} IST{note_extra}</div>
<h2>New — send first touch</h2>
{new_rows}
<h2>Follow-up due (no reply for {FOLLOWUP_HOURS}h+)</h2>
{fu_rows}
{muted_html}
<div class=note>Edit the draft, then Open in WhatsApp / Email and tap send yourself. "done" hides a card on this device. <a href="/inbox">Reply inbox</a> · <a href="/ceo">Worry list</a></div>
</div>
<script>
var KEY='leads_done';
function load(){{try{{return JSON.parse(localStorage.getItem(KEY))||{{}};}}catch(e){{return {{}};}}}}
function save(d){{localStorage.setItem(KEY,JSON.stringify(d));}}
var dism=load(); var hidden=0;
document.querySelectorAll('.item').forEach(function(it){{
  if(dism[it.dataset.key]){{it.style.display='none';hidden++;}}
}});
if(hidden){{
  var note=document.querySelector('.note');
  var a=document.createElement('a'); a.href='#';
  a.textContent=' Show '+hidden+' done';
  a.style.cssText='margin-left:8px;color:#5b21b6;font-weight:600';
  a.onclick=function(ev){{ev.preventDefault();localStorage.removeItem(KEY);location.reload();}};
  note.appendChild(a);
}}
document.addEventListener('click',async function(e){{
  var sm=e.target.closest('.sendmail');
  if(sm){{
    var it=sm.closest('.item');
    var body=(sm.dataset.mode==='followup' && sm.dataset.ebody)
             ? sm.dataset.ebody
             : it.querySelector('.draft').value;
    if(!body){{alert('Draft is empty — write the message first.');return;}}
    var links=[];
    try{{links=JSON.parse(sm.dataset.links||'[]');}}catch(e2){{}}
    if(links.length){{
      body+='\n\nUseful resources:';
      links.forEach(function(l){{ body+='\n- '+l[1]+': '+l[0]; }});
    }}
    var u='https://mail.google.com/mail/?view=cm&fs=1&to='+encodeURIComponent(sm.dataset.to)
         +'&su='+encodeURIComponent(sm.dataset.sub)
         +'&body='+encodeURIComponent(body);
    window.open(u,'_blank');
    dism[it.dataset.key]=Date.now(); save(dism);   // acted -> gone on next reload
    return;
  }}
  var am=e.target.closest('.already');
  if(am){{
    var it=am.closest('.item');
    var why='';
    if(am.dataset.ask){{
      why=prompt('Why is this lead not relevant? (e.g. spam, test entry, duplicate, existing student)');
      if(why===null) return;
    }}
    am.disabled=true; am.textContent='saving…';
    try{{
      var r=await fetch('/api/leadflag',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{lead_id:am.dataset.lid,name:am.dataset.name,kind:am.dataset.kind,reason:why}})}});
      if(!r.ok) throw 0;
      dism[it.dataset.key]=Date.now(); save(dism);
      it.style.display='none';
    }}catch(err){{
      am.disabled=false; am.textContent='already messaged';
      alert('Could not save — check the FLAGS KV binding.');
    }}
    return;
  }}
  var sk=e.target.closest('.skip');
  if(sk){{var it=sk.closest('.item');dism[it.dataset.key]=Date.now();save(dism);it.style.display='none';return;}}
  var nw=e.target.closest('.nowa');
  if(nw){{
    var it=nw.closest('.item');
    nw.disabled=true; nw.textContent='saving…';
    try{{
      var r=await fetch('/api/waflag',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{phone:nw.dataset.ph}})}});
      if(!r.ok) throw 0;
      var act=it.querySelector('.actions');
      it.querySelector('.send').remove(); nw.remove();
      var sp=document.createElement('span'); sp.className='meta';
      sp.textContent='not on WhatsApp — email them';
      act.insertBefore(sp, act.firstChild);
    }}catch(err){{ nw.disabled=false; nw.textContent='no WhatsApp?'; alert('Could not save.'); }}
    return;
  }}
  var b=e.target.closest('.send');
  if(b){{
    var it=b.closest('.item');
    var txt=it.querySelector('.draft').value;
    window.open('https://wa.me/+'+b.dataset.wa+'?text='+encodeURIComponent(txt),'_blank');
    dism[it.dataset.key]=Date.now(); save(dism);   // acted -> gone on next reload
  }}
}});
(function(){{var cut=Date.now()-60*86400000,ch=false;
for(var k in dism){{if(dism[k]<cut){{delete dism[k];ch=true;}}}} if(ch) save(dism);}})();
</script>
</body></html>"""
    open(os.path.join(here, "dashboard_leads.html"), "w").write(H)
    print(f"Leads: {sum(1 for L in leads if L['status']=='new')} new, "
          f"{sum(1 for L in leads if L['status']=='followup')} follow-ups, "
          f"{n_wait} waiting, {n_rep} replied")

if __name__ == "__main__":
    build()

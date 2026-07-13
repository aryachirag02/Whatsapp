#!/usr/bin/env python3
"""
AP Guru WhatsApp response + content engine (READ-ONLY analytics; no sending).

Reads messages_latest.json (from unipile_pull.py) and attendees.json (the
global lid->phone+name directory) and writes:
  - dashboard.html   (self-refreshing report)
  - summary.json     (machine-readable metrics)

Highlights for a business owner:
  * What parents are messaging about + every concerning message (top of page).
  * Accounts at risk (churn) — groups where someone is unhappy / asked to stop.
  * 7-day trend vs the previous 7 days (are we getting better or worse?).
  * How fast we reply (distribution) and response rate BY NAMED team member.

Corrections (read automatically if present, next to this script):
  staff_overrides.json  {"force_staff": ["9199..."], "force_student": ["91..."]}
  staff_names.json      {"919XXXXXXXXX": "Riya (SAT)", "lid:1234@lid": "Owner"}
"""
import json, os, glob, re, statistics, html, time, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

# ---------- CONFIG KNOBS ----------
STAFF_MIN_GROUPS = 3
COLD_HOURS       = 12
STALE_HOURS      = 72   # open threads older than this drop off the dashboards
IST              = timedelta(hours=5, minutes=30)
REFRESH_SECONDS  = 300
MEDIA_MARKERS    = ("cannot display this type of message",)
try:
    _cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
except Exception:
    _cfg = {}
LOOKBACK_DAYS = _cfg.get("lookback_days", 30)
# groups whose NAME contains any of these are treated as internal and hidden
try:
    FLAGGED_INFO=json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"flagged_groups.json")))
except Exception:
    FLAGGED_INFO={}
FLAGGED=set(FLAGGED_INFO.keys())
EXCLUDE_GROUP_WORDS = [w.lower() for w in _cfg.get("exclude_group_words",
    _cfg.get("exclude_keywords", ["coordination","content","grading","internal","team","staff",
             "accounts","tracking","tech issues","payglocal","non sat"]))]

FILLER = {"thanks","thank you","thankyou","thanku","thx","ty","ok","okay","okk","okie",
          "noted","great","sure","perfect","done","got it","alright","cool","fine",
          "yes","yep","yeah","no","welcome","good","nice","super","🙏","👍","👍🏻","❤️","🙂"}

STAFF_NAME_WORDS = ["coordinator","coordinater","manager","mgr","rm","qc","hr",
    "ap guru","apguru","work","team","tutor","teacher","faculty","trainer","mentor",
    "counsel","counsellor","counselor","support","admin","relationship","grading",
    "ops","sales","accountant","tech team","program manager"]
PARENT_NAME_WORDS = ["parent","mom","mum","dad","mother","father","papa","mummy"]

CATS = {
 "payment": [
    "fee","fees","payment","pay the","invoice","refund","installment","instalment","emi",
    "pending payment","balance payment","amount due","charge","charged","transaction",
    "receipt","gst","paisa","paise","bhugtan","raseed","not paid","kindly pay","outstanding"],
 "scheduling": [
    "reschedule","re-schedule","reschedul","postpone","prepone","cancel","cancelled",
    "cancellation","shift the class","change the time","change the timing","move the class",
    "another time","not available","won't be able","wont be able","unable to attend",
    "can't attend","cant attend","miss the class","missing the class","skip the class",
    "no class today","holiday","chutti","time slot","time change","new timing","on leave"],
 "academic": [
    "exam","exams","test","tests","quiz","score","scores","marks","grade","grades",
    "result","results","doubt","doubts","syllabus","homework","assignment","mock test",
    "not understanding","didn't understand","didnt understand","struggling","weak in",
    "falling behind","revision","deadline","submission"],
 "complaint": [
    "not happy","unhappy","disappointed","disappoint","disappointing","worst","useless",
    "waste of","bad experience","complaint","complain","unprofessional","rude",
    "not responding","no response","still waiting","again and again","repeatedly",
    "frustrated","frustrating","unacceptable","escalate","escalation","not satisfied",
    "dissatisfied","bekar","kharab","ghatiya","disgusting","pathetic","horrible",
    "discontinue","stop the class","stop the classes","stop classes","want refund",
    "need refund","cancel the subscription","cancel subscription","not interested anymore"],
}
URGENT = ["urgent","urgently","asap","emergency","immediately","tomorrow exam",
          "exam tomorrow","exam is tomorrow","test tomorrow","panic","stressed","stress"]
CONCERN_HARD = ["refund","discontinue","stop the class","stop classes","stop the classes",
                "cancel the subscription","cancel subscription","escalate","not satisfied",
                "dissatisfied","complaint","worst","unprofessional","not happy","disappointed",
                "no response","not responding","still waiting","unacceptable","bekar","ghatiya",
                "pathetic","horrible","want refund","need refund","repeatedly"]
# the subset that signals real churn risk (account may leave)
CHURN_HARD = ["refund","discontinue","stop the class","stop classes","stop the classes",
              "cancel the subscription","cancel subscription","escalate","not satisfied",
              "dissatisfied","not interested anymore","worst","unprofessional","pathetic",
              "horrible","ghatiya","bekar","want refund","need refund"]

# Words/phrases that signal the parent is asking for something => we owe a reply
REQUEST_WORDS = ["please","pls","plz","kindly","can you","could you","can we","could we",
    "would you","would it","will you","let me know","need to know","want to know","share",
    "send","provide","confirm","update me","should we","do we","may i","requesting",
    "request you","call me","when","what time","how many","how much","how do","which",
    "any update","waiting for","get back","revert","is it possible","possible to"," asap"]

def _matcher(words):
    pat = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:" + pat + r")(?![a-z0-9])", re.I)
_REQUEST_RE = _matcher(REQUEST_WORDS)

def need_type(text):
    """How much does this last parent message look like it needs a reply?"""
    if not text: return "fyi"
    if is_media(text): return "attachment"
    if "?" in text: return "question"
    if _REQUEST_RE.search(_clean(text)): return "request"
    return "fyi"

_CAT_RE   = {c: _matcher(ws) for c, ws in CATS.items()}
_URGENT_RE= _matcher(URGENT)
_HARD_RE  = _matcher(CONCERN_HARD)
_CHURN_RE = _matcher(CHURN_HARD)
STAFF_NAME_RE  = _matcher(STAFF_NAME_WORDS)
PARENT_NAME_RE = _matcher(PARENT_NAME_WORDS)

def _clean(t): return (t or "").replace("’","'").replace("‘","'").lower()
def is_media(t): return bool(t) and any(m in t.lower() for m in MEDIA_MARKERS)
def biz_minutes(a,b):
    """Minutes between two UTC datetimes, excluding 22:00-06:00 IST (team off-hours)."""
    if not a or not b or b<=a: return 0
    total=0.0; cur=a
    while cur<b:
        l=cur+IST
        if 6<=l.hour<22:
            end_biz=l.replace(hour=22,minute=0,second=0,microsecond=0)
            seg_end=min(b+IST,end_biz)
            total+=(seg_end-l).total_seconds()/60
            cur=seg_end-IST
        else:
            nxt=(l+timedelta(days=1)).replace(hour=6,minute=0,second=0,microsecond=0) if l.hour>=22                 else l.replace(hour=6,minute=0,second=0,microsecond=0)
            cur=min(b,nxt-IST)
    return round(total)

def when(ts):
    """Short IST timestamp for display, e.g. '09 Jul, 21:14'."""
    return (ts+IST).strftime("%d %b, %H:%M") if ts else ""

def disp(t):
    """Text as shown on the dashboard: media/attachment placeholders -> clean tag."""
    return "[media / attachment]" if is_media(t) else (t or "")
def normalize(t):
    if not t: return ""
    return "".join(c for c in t.lower() if c.isalnum() or c.isspace() or c in "🙏👍❤️🙂").strip()
def is_filler(text):
    if is_media(text): return False
    n=normalize(text)
    if n=="": return False
    if n in FILLER: return True
    w=n.split(); return len(w)<=2 and all(x in FILLER for x in w)

def classify(text):
    """Return (tags:set, concerning:bool, churn:bool, complaint_cat:bool)."""
    if not text or is_media(text): return set(), False, False, False
    low=_clean(text)
    tags={c for c,rx in _CAT_RE.items() if rx.search(low)}
    urgent=bool(_URGENT_RE.search(low))
    if urgent and ("academic" in tags or "scheduling" in tags): tags.add("urgent")
    complaint = "complaint" in tags
    concerning = complaint or bool(_HARD_RE.search(low)) or (urgent and ("academic" in tags or "scheduling" in tags))
    churn = complaint or bool(_CHURN_RE.search(low))
    return tags, concerning, churn, complaint

def fmt(mins):
    if mins is None: return "—"
    m=int(round(mins)); h,mm=divmod(m,60)
    if h>=24: d,hh=divmod(h,24); return f"{d}d {hh}h"
    return f"{h}h {mm:02d}m" if h else f"{mm}m"

def fmt_phone(canon):
    if not canon or str(canon).startswith("lid:"): return "—"
    d="".join(c for c in str(canon) if c.isdigit())
    if len(d)==12 and d.startswith("91"): return f"+91 {d[2:7]} {d[7:]}"
    if len(d)==10: return f"+91 {d[:5]} {d[5:]}"
    return "+"+d if d else "—"

# ---------- LOAD ----------
here=os.path.dirname(os.path.abspath(__file__))
def latest_messages_file():
    fixed=os.path.join(here,"messages_latest.json")
    if os.path.exists(fixed): return fixed
    cand=sorted(glob.glob(os.path.join(here,"messages_*.json")))
    if not cand: raise SystemExit("No messages_*.json found. Run unipile_pull.py first.")
    return cand[-1]

# ---------- AI THREAD SUMMARIES (inline, cached) ----------
AI_MODEL="claude-haiku-4-5-20251001"; AI_MAX_NEW=40
AI_SYSTEM=("You summarize WhatsApp threads for AP Guru, an online tutoring company. "
 "Each thread is between AP Guru's team and a student's parent. In ONE sentence "
 "(max 22 words), plain English, state what the parent needs right now and any "
 "deadline/urgency. If they reference something earlier ('this also please'), "
 "resolve WHAT they mean from context. No preamble, no quotes, just the sentence.")

def ai_summaries(need_ctx):
    """need_ctx: {key:{'group','context'}} -> {key:{'summary','ts'}}.
    Key embeds the last-message timestamp, so each thread is summarized once
    until a new message arrives (ai_summaries.json is just that cache).
    No ANTHROPIC_API_KEY -> silently returns whatever is cached."""
    cpath=os.path.join(here,"ai_summaries.json")
    try: cache=json.load(open(cpath))
    except Exception: cache={}
    cache={k:v for k,v in cache.items() if k in need_ctx}      # prune stale
    key=os.environ.get("ANTHROPIC_API_KEY","").strip()
    if key:
        new=0
        for k,t in need_ctx.items():
            if k in cache or not t.get("context"): continue
            if new>=AI_MAX_NEW: break
            convo="\n".join(f'{m["role"]}{(" ("+m["who"]+")") if m.get("who") else ""}: {m["text"]}'
                            for m in t["context"])
            body=json.dumps({"model":AI_MODEL,"max_tokens":80,"system":AI_SYSTEM,
                "messages":[{"role":"user","content":
                    f"Group: {t.get('group','')}\nRecent thread (oldest first):\n{convo}\n\nSummary:"}]}).encode()
            req=urllib.request.Request("https://api.anthropic.com/v1/messages",data=body,
                headers={"Content-Type":"application/json","x-api-key":key,
                         "anthropic-version":"2023-06-01"})
            try:
                with urllib.request.urlopen(req,timeout=30) as r: data=json.loads(r.read())
                summ=" ".join(b.get("text","") for b in data.get("content",[])
                              if b.get("type")=="text").strip()
                if summ:
                    cache[k]={"summary":summ[:200],
                              "ts":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
                    new+=1
            except Exception:
                pass
            time.sleep(0.3)
        if new: print(f"ai: {new} new summaries ({len(cache)} cached)")
    json.dump(cache,open(cpath,"w"),indent=1)
    return cache

def load_directory():
    p=os.path.join(here,"attendees.json")
    if not os.path.exists(p): return {"lid2phone":{}, "names":{}, "self_phones":[]}
    return json.load(open(p))

def analyze(recs, as_of, keep_gids=None):
    cutoff=as_of-timedelta(days=LOOKBACK_DAYS)
    d=load_directory()
    lid2phone=d.get("lid2phone",{}); dir_names=d.get("names",{})
    self_phones=set(d.get("self_phones",[]))

    name_over={}
    npath=os.path.join(here,"staff_names.json")
    if os.path.exists(npath):
        name_over={k:v for k,v in json.load(open(npath)).items() if not k.startswith("_")}

    def canon(sid):
        """Unify identity: prefer phone (via lid directory) else raw id."""
        if not sid: return None
        if str(sid).startswith("lid:"):
            ph=lid2phone.get(sid[4:])
            return ph or sid
        return sid

    groups=defaultdict(list); group_name={}
    sender_groups=defaultdict(set); sender_names=defaultdict(Counter); self_ids=set()
    for r in recs:
        gid=r["group_id"]; group_name[gid]=r.get("group_name") or gid
        ts=datetime.fromisoformat(r["timestamp"].replace("Z","+00:00"))
        sid=canon(r.get("sender"))
        text=r.get("text") or ""
        filler=bool(r.get("is_filler")) or is_filler(text)
        tags,conc,churn,compl=classify(text)
        groups[gid].append({"ts":ts,"sid":sid,"filler":filler,"text":text,"tags":tags,
                            "concerning":conc,"churn":churn,"is_self":bool(r.get("is_self"))})
        if sid:
            sender_groups[sid].add(gid)
            nm=(r.get("sender_name") or "").strip() or (r.get("push_name") or "").strip()
            if nm: sender_names[sid][nm]+=1
            if r.get("is_self") or sid in self_phones: self_ids.add(sid)

    def best_name(sid):
        if sid in name_over: return name_over[sid]
        if sid in dir_names: return dir_names[sid]
        if sender_names[sid]: return sender_names[sid].most_common(1)[0][0]
        return None

    # ----- team detection -----
    overrides={"force_staff":[],"force_student":[]}
    opath=os.path.join(here,"staff_overrides.json")
    if os.path.exists(opath): overrides.update(json.load(open(opath)))
    team=set()
    for sid,gs in sender_groups.items():
        nm=best_name(sid) or ""
        if sid in self_ids: team.add(sid)
        elif PARENT_NAME_RE.search(nm) and len(gs)<STAFF_MIN_GROUPS: continue
        elif len(gs)>=STAFF_MIN_GROUPS or STAFF_NAME_RE.search(nm): team.add(sid)
    team|=set(overrides["force_staff"]); team-=set(overrides["force_student"])
    def is_team(sid): return sid in team

    win7=as_of-timedelta(days=7)
    def half(ts): return "cur" if ts>=win7 else "prev"

    per_group=[]; all_resp=[]; staff_stats=defaultdict(list); staff_concern=Counter()
    gowner=defaultdict(Counter)   # gid -> Counter of team responders (to name a thread "owner")
    awaiting=[]; cat_counts=Counter(); inbound_total=0; concerning_msgs=[]
    vol={"cur":0,"prev":0}; conc_period={"cur":0,"prev":0}
    resp_period={"cur":[],"prev":[]}; churn_groups=defaultdict(lambda:{"n":0,"latest":None,"latest_ts":None})
    heat=[[0]*24 for _ in range(7)]          # [weekday][hour-IST] inbound volume
    concern_by_gid=Counter(); gstart={}; gfirst_resp={}; gmsgs=Counter()

    hidden_internal=0
    for gid,evs in groups.items():
        gname=(group_name.get(gid) or "").lower()
        senders={e["sid"] for e in evs if e["sid"]}
        # internal = only team/self ever spoke, or name matches exclude words
        if (senders and senders<=team) or any(w in gname for w in EXCLUDE_GROUP_WORDS) or gid in FLAGGED:
            hidden_internal+=1; continue
        if keep_gids is not None and gid not in keep_gids:
            continue
        evs.sort(key=lambda x:x["ts"])
        if evs: gstart[gid]=evs[0]["ts"]
        waiting=False; start=None; start_concern=False; bursts=0; resp_here=[]
        last_concern=None; last_inbound=None
        for e in evs:
            inbound=(not e["is_self"]) and (not is_team(e["sid"]))
            if inbound:
                if not e["filler"]:
                    inbound_total+=1; vol[half(e["ts"])]+=1; last_inbound=e
                    gmsgs[gid]+=1
                    tist=e["ts"]+IST; heat[tist.weekday()][tist.hour]+=1
                    if e["concerning"]: concern_by_gid[gid]+=1
                    for t in e["tags"]:
                        if t!="urgent": cat_counts[t]+=1
                    if e["concerning"]:
                        cat_counts["concerning"]+=1; conc_period[half(e["ts"])]+=1
                        rec={"group":group_name[gid],"gid":gid,"who":best_name(e["sid"]) or fmt_phone(e["sid"]),
                             "ts":e["ts"],"tags":sorted(t for t in e["tags"] if t!="urgent") or ["urgent"],
                             "text":e["text"][:300],"churn":e["churn"]}
                        concerning_msgs.append(rec); last_concern=rec
                    if e["churn"]:
                        cg=churn_groups[gid]; cg["n"]+=1
                        if not cg["latest_ts"] or e["ts"]>cg["latest_ts"]:
                            cg["latest_ts"]=e["ts"]; cg["latest"]=e["text"][:200]
                if e["filler"]: continue
                if not waiting: waiting,start=True,e["ts"]; start_concern=e["concerning"]
                elif e["concerning"]: start_concern=True
            else:
                if e["sid"]: gowner[gid][e["sid"]]+=1
                if waiting:
                    mins=biz_minutes(start,e["ts"])   # excludes 10pm-6am IST
                    met=mins<=sla_hours_for(start)*60
                    all_resp.append((mins,met,e["sid"])); resp_here.append((mins,met))
                    resp_period[half(start)].append((mins,met))
                    staff_stats[e["sid"]].append((mins,met))
                    if start_concern: staff_concern[e["sid"]]+=1
                    bursts+=1; waiting=False; start_concern=False
        open_mins=(as_of-start).total_seconds()/60 if waiting else None
        if waiting and last_inbound and (as_of-last_inbound["ts"]).total_seconds()>STALE_HOURS*3600:
            waiting=False; open_mins=None   # stale (>72h) — age out of the dashboards
        if waiting:
            met_open=open_mins<=sla_hours_for(start)*60
            last_text=(last_inbound["text"] if last_inbound else "")
            ntype=need_type(last_text)
            # attachments alone are usually homework/screenshots — acknowledge, not "act now"
            needs_reply = ntype in ("question","request") or start_concern
            own=None
            if gowner[gid]:
                osid=gowner[gid].most_common(1)[0][0]
                own=best_name(osid) or fmt_phone(osid)
            remaining=sla_hours_for(start)*60-open_mins
            awaiting.append({"group":group_name[gid],"gid":gid,"waiting_min":open_mins,
                             "last_ts":(last_inbound["ts"] if last_inbound else start),
                             "owner":own,"breach_soon":(met_open and remaining<=30),
                             "ctx_key":f"{gid}|{evs[-1]['ts'].isoformat()}",
                             "sla_h":sla_hours_for(start),"breached":not met_open,
                             "cold":open_mins>COLD_HOURS*60,"concerning":start_concern,
                             "need_type":ntype,"needs_reply":needs_reply,
                             "last_text":last_text,
                             "snippet":(last_concern["text"] if (start_concern and last_concern) else "")})
        med=statistics.median([m for m,_ in resp_here]) if resp_here else None
        within=(sum(1 for _,ok in resp_here if ok)/len(resp_here)*100) if resp_here else None
        if resp_here: gfirst_resp[gid]=resp_here[0][0]
        per_group.append({"gid":gid,"group":group_name[gid],"bursts":bursts,"median_min":med,"within_pct":within})

    n_resp=len(all_resp)
    overall_med=statistics.median([m for m,_,_ in all_resp]) if all_resp else None
    overall_within=(sum(1 for _,ok,_ in all_resp if ok)/n_resp*100) if n_resp else None
    awaiting.sort(key=lambda x:(not x["concerning"],not x["breached"],-x["waiting_min"]))
    n_breach_open=sum(1 for a in awaiting if a["breached"]); n_cold=sum(1 for a in awaiting if a["cold"])
    concerning_msgs.sort(key=lambda x:-x["ts"].timestamp())
    # only threads that actually look like they need a reply (question/request/attachment/concerning)
    need_order={"concerning":0,"question":1,"request":2,"attachment":3}
    attention=[a for a in awaiting if a["needs_reply"]]
    attention.sort(key=lambda x:-x["last_ts"].timestamp())   # newest first
    attach_open=[a for a in awaiting if not a["needs_reply"] and a["need_type"]=="attachment"]
    attach_open.sort(key=lambda x:-x["waiting_min"])
    fyi_open=[a for a in awaiting if not a["needs_reply"] and a["need_type"]!="attachment"]
    fyi_open.sort(key=lambda x:-x["waiting_min"])

    # trend (last 7d vs prev 7d)
    def med_of(lst): return statistics.median([m for m,_ in lst]) if lst else None
    def within_of(lst): return (sum(1 for _,ok in lst if ok)/len(lst)*100) if lst else None
    trend={"vol":vol,"conc":conc_period,
           "med":{"cur":med_of(resp_period["cur"]),"prev":med_of(resp_period["prev"])},
           "within":{"cur":within_of(resp_period["cur"]),"prev":within_of(resp_period["prev"])}}

    # reply-speed distribution
    buckets=[("≤ 5 min",0,5),("5–30 min",5,30),("30 min–2 h",30,120),("2–6 h",120,360),("> 6 h",360,1e9)]
    dist=[]
    for lab,lo,hi in buckets:
        c=sum(1 for m,_,_ in all_resp if lo<=m<hi)
        dist.append((lab,c))

    # accounts at risk
    at_risk=[]
    awaiting_gids={a["gid"] for a in awaiting}
    for gid,info in churn_groups.items():
        # what did the team say back after the latest concern? (oversight view)
        treply=None
        if info["latest_ts"] and groups.get(gid):
            for e in groups[gid]:
                if e["ts"]>info["latest_ts"] and (e["is_self"] or is_team(e["sid"])) and (e["text"] or "").strip():
                    who=best_name(e["sid"]) or ""
                    treply={"who":who,"text":disp(e["text"])[:160],"ts":e["ts"]}
        at_risk.append({"gid":gid,"group":group_name[gid],"n":info["n"],"latest":info["latest"],
                        "team_reply":treply,
                        "ts":info["latest_ts"],"open":gid in awaiting_gids})
    at_risk=[r for r in at_risk if r["ts"] and (as_of-r["ts"]).total_seconds()<=STALE_HOURS*3600]
    at_risk.sort(key=lambda x:-x["ts"].timestamp())   # newest first

    # group watchlist (worst groups by a problem score)
    awaiting_by_gid={a["gid"]:a for a in awaiting}
    watchlist=[]
    for pg in per_group:
        gid=pg["gid"]; aw=awaiting_by_gid.get(gid)
        concern=concern_by_gid.get(gid,0); churn=churn_groups.get(gid,{}).get("n",0)
        open_needs=bool(aw and aw["needs_reply"]); breached=bool(aw and aw["breached"]); cold=bool(aw and aw["cold"])
        slow=(pg["median_min"] or 0)>120
        flagged = (concern>0 or churn>0)
        # No flagged wording? Only list on a genuinely bad service PATTERN
        # (median reply > 2h). A currently-open/cold thread is NOT enough —
        # "Act now" already surfaces those; re-listing them here is noise.
        if not flagged and not slow: continue
        score=concern + 2*churn + (3 if open_needs else 0) + (2 if cold else (1 if breached else 0)) + (1 if slow else 0)
        if score<=0: continue
        st=("cold" if cold else ("breached" if breached else ("open" if open_needs else "ok")))
        watchlist.append({"gid":pg["gid"],"group":pg["group"],"concern":concern,"churn":churn,
                          "median_min":pg["median_min"],"within_pct":pg["within_pct"],
                          "status":st,"score":score})
    watchlist.sort(key=lambda x:-x["score"]); watchlist=watchlist[:15]

    # new students (groups whose first activity is recent => likely created in-window)
    new_cut=as_of-timedelta(days=7)
    new_students=[]
    for gid,st in gstart.items():
        if st and st > cutoff+timedelta(hours=18) and st >= new_cut and gmsgs.get(gid,0)>=1:
            aw=awaiting_by_gid.get(gid)
            new_students.append({"group":group_name[gid],"started":st,"msgs":gmsgs.get(gid,0),
                                 "first_resp":gfirst_resp.get(gid),
                                 "open":bool(aw and aw["needs_reply"])})
    new_students.sort(key=lambda x:-x["started"].timestamp())

    # per-team-member
    staff_rows=[]
    for sid,lst in staff_stats.items():
        med=statistics.median([m for m,_ in lst]); within=sum(1 for _,ok in lst if ok)/len(lst)*100
        staff_rows.append({"id":sid,"name":best_name(sid) or fmt_phone(sid),"phone":fmt_phone(sid),
                           "handled":len(lst),"median_min":med,"within_pct":within,
                           "concerning":staff_concern.get(sid,0)})
    staff_rows.sort(key=lambda x:-x["handled"])
    loads=[r["handled"] for r in staff_rows]; med_load=statistics.median(loads) if loads else 0
    for r in staff_rows:
        slow=r["median_min"]>120; busy=r["handled"]>med_load
        r["flag"]="look here" if (slow and not busy) else ("heavy load" if (slow and busy) else "ok")

    # ---- context for AI thread summaries (open threads only) ----
    def _ctx(gid):
        out=[]
        for e in groups[gid][-14:]:
            if e["filler"] or not (e["text"] or "").strip(): continue
            role="Team" if (e["is_self"] or is_team(e["sid"])) else "Parent"
            out.append({"role":role,"who":best_name(e["sid"]) or "","text":e["text"][:220]})
        return out[-8:]
    need_ctx={}
    for a in attention:
        need_ctx[a["ctx_key"]]={"group":a["group"],"context":_ctx(a["gid"])}
    for r in at_risk:
        if r["open"] and groups.get(r["gid"]):
            k=f"{r['gid']}|{groups[r['gid']][-1]['ts'].isoformat()}"
            need_ctx.setdefault(k,{"group":r["group"],"context":_ctx(r["gid"])})

    summary={"generated":as_of.isoformat(),"messages":len(recs),"groups":len(groups),
             "hidden_internal":hidden_internal,"attach_open":len(attach_open),
             "inbound_messages":inbound_total,"responses_measured":n_resp,
             "median_first_response_min":overall_med,"within_sla_pct":overall_within,
             "awaiting_now":len(awaiting),"breached_open":n_breach_open,"cold_threads":n_cold,
             "breach_soon":sum(1 for a in awaiting if a.get("breach_soon")),
             "fyi_open":len(fyi_open),
             "concerning_total":cat_counts.get("concerning",0),"needs_attention":len(attention),
             "accounts_at_risk":len(at_risk),"new_students":len(new_students),
             "watchlist_size":len(watchlist),
             "cat_payment":cat_counts.get("payment",0),"cat_scheduling":cat_counts.get("scheduling",0),
             "cat_academic":cat_counts.get("academic",0),"cat_complaint":cat_counts.get("complaint",0),
             "team_detected":len(team),"staff_rows":staff_rows}

    return {"summary":summary,"need_ctx":need_ctx,
            "sender_groups":sender_groups,"team":team,"best_name":best_name,
            "attention":attention,"fyi_open":fyi_open,"attach_open":attach_open,
            "concerning_msgs":concerning_msgs,"cat_counts":cat_counts,
            "inbound_total":inbound_total,"at_risk":at_risk,"trend":trend,"dist":dist,
            "heat":heat,"watchlist":watchlist,"new_students":new_students}

# ---------- TEAM CLASSIFICATION ----------
# Ordered: a group is assigned to the FIRST team whose pattern matches its name.
# Override any group by adding its exact name under "team_overrides" in config.json:
#   {"team_overrides": {"Aarushi Shah GRE": "else", "Some Group": "ib"}}
TEAMS=[("myp",  "MYP / UK admissions tests", r"\bmyp\b|\bucat\b|\btmua\b|\blnat\b|\besat\b"),
       ("sat",  "SAT / ACT",                 r"\bsat\b|\bact\b|digital sat|dsat|psat"),
       ("ap",   "AP",                        r"\bap\b|advanced placement|apush"),
       ("ib",   "IB Diploma (IBDP)",         r"\bibdp\b|\bib\b|\btok\b|extended essay|\bdp[12]\b"),
       ("igcse","IGCSE / A-Level / GCSE",    r"igcse|\bgcse\b|a-?level|as-?level|o-?level|cambridge|edexcel|\bcaie\b"),
       ("else", "Everything else",           r".*")]
_TEAM_OVER={k:v for k,v in _cfg.get("team_overrides",{}).items()}
_ACCT_TEAM=_cfg.get("accounts",{}) if isinstance(_cfg.get("accounts"),dict) else {}
_TEAM_RE=[(slug,label,re.compile(pat,re.I)) for slug,label,pat in TEAMS]
def team_of(name):
    if name in _TEAM_OVER: return _TEAM_OVER[name]
    # remove the company name so "AP Guru <> Payglocal" doesn't match the AP curriculum
    clean=re.sub(r"ap\s*guru","",name or "",flags=re.I)
    for slug,label,rx in _TEAM_RE:
        if rx.search(clean): return slug
    return "else"

def build_report(messages_path=None, as_of=None):
    """Master + one dashboard per team. AI summaries computed ONCE (shared)."""
    messages_path=messages_path or latest_messages_file()
    recs=json.load(open(messages_path))
    as_of=as_of or datetime.now(timezone.utc)

    # classify every group: by which program-head account it belongs to,
    # falling back to name keywords for any group with no account tag
    gid_name={}; gid_acct={}
    for r in recs:
        gid_name[r["group_id"]]=r.get("group_name") or r["group_id"]
        a=r.get("account_id")
        if a: gid_acct[r["group_id"]]=a
    team_gids=defaultdict(set)
    for gid,nm in gid_name.items():
        t=_ACCT_TEAM.get(gid_acct.get(gid)) or team_of(nm)
        team_gids[t].add(gid)

    # master analysis = everything; its need_ctx is the union of all open threads
    master=analyze(recs, as_of, keep_gids=None)
    AI=ai_summaries(master["need_ctx"])                       # single API pass
    json.dump(master["summary"],open(os.path.join(here,"summary.json"),"w"),indent=2,default=str)

    views=[("dashboard.html","All courses — master", None)]
    for slug,label,_ in TEAMS:
        views.append((f"dashboard_{slug}.html", label, team_gids.get(slug,set())))

    # analyze each team once (shared AI cache covers all subsets)
    team_R={slug: analyze(recs, as_of, keep_gids=team_gids.get(slug,set())) for slug,_,_ in TEAMS}
    team_tally=[(slug,label,team_R[slug]["summary"]) for slug,label,_ in TEAMS]

    counts={}
    for outfile,label,gids in views:
        if gids is None:
            R=master; tally=team_tally
        else:
            slug=outfile[len("dashboard_"):-len(".html")]
            R=team_R[slug]; tally=None
        write_dashboard(R, AI, as_of, outfile, label, tally)
        counts[label]=R["summary"]["needs_attention"]
    return master["summary"], counts

def write_dashboard(R, AI, as_of, outfile, label, team_tally=None):
    _write_html(R["summary"],R["sender_groups"],R["team"],R["best_name"],fmt_phone,
                R["attention"],R["fyi_open"],R["attach_open"],R["concerning_msgs"],
                R["cat_counts"],R["inbound_total"],R["at_risk"],R["trend"],R["dist"],
                R["heat"],R["watchlist"],R["new_students"],as_of,AI,outfile,label,team_tally)

def sla_hours_for(ts_utc):
    h=(ts_utc+IST).hour
    return 2 if 12<=h<23 else 6

def _write_html(s,sender_groups,team,best_name,fmt_phone,attention,fyi_open,attach_open,concerning_msgs,
                cat_counts,inbound_total,at_risk,trend,dist,heat,watchlist,new_students,as_of,AI=None,
                outfile="dashboard.html",label="All courses — master",team_tally=None):
    NAVY="#16243f"; INK="#15171c"; MUT="#6b7280"; LINE="#e7e9ee"
    OK="#067647"; BAD="#b42318"; WARN="#b54708"; PUR="#5b21b6"
    AI=AI or {}
    AI_KEYS={k.split("|",1)[0]: k for k in AI}   # gid -> full cache key
    def ai_line(key):
        v=AI.get(key)
        if not v or not v.get("summary"): return ""
        return f'<div class=snip style="color:{PUR};font-style:normal">🤖 {html.escape(v["summary"])}</div>'
    def esc(x): return html.escape(str(x))
    def card(lbl,val,col=INK,sub="",target=""):
        sb=f'<div class=csub>{sub}</div>' if sub else ""
        t=f' data-t="{target}" style="cursor:pointer"' if target else ""
        hint='<div class=csub style="color:#b6bcc8">&#9662; tap for detail</div>' if target else ""
        return f'<div class=c{t}><div class=lbl>{lbl}</div><div class=val style="color:{col}">{val}</div>{sb}{hint}</div>'

    # ---- trend helpers (arrows shown ON the glance tiles) ----
    def delta_reply(cur,prev):
        if cur is None or prev is None: return ""
        diff=cur-prev
        col=OK if diff<=0 else BAD; arr="▼" if diff<=0 else "▲"
        return f'<span style="color:{col}"> {arr} {fmt(abs(diff))} vs prev 7d</span>'
    def delta_pct(cur,prev,higher_good=True):
        if cur is None or prev is None: return ""
        diff=round(cur-prev); good=(diff>=0)==higher_good
        col=OK if good else BAD; arr="▲" if diff>=0 else "▼"
        return f'<span style="color:{col}"> {arr} {abs(diff)} pts vs prev 7d</span>'
    def delta_n(cur,prev,higher_good=False):
        diff=cur-prev
        if diff==0: return ""
        good=(diff>=0)==higher_good
        col=OK if good else BAD; arr="▲" if diff>0 else "▼"
        return f'<span style="color:{col}"> {arr} {abs(diff)} vs prev 7d</span>'

    breached=[a for a in attention if a["breached"]]
    soon=[a for a in attention if a.get("breach_soon")]
    at_risk_open=[a for a in at_risk if a["open"]]

    # ---- TOP 5 TODAY (auto-written, priority ordered, with AI context) ----
    top=[]
    seen=set()
    def add(icon,text,group=None,aikey=None):
        key=group or text
        if key in seen or len(top)>=5: return
        summ=""
        v=AI.get(aikey) if aikey else None
        if v and v.get("summary"):
            summ=f'<div class=t5s>🤖 {html.escape(v["summary"])}</div>'
        seen.add(key); top.append((icon,text,summ))
    for a in breached:
        if a["concerning"]:
            ow=f" · usually {esc(a['owner'])}" if a.get("owner") else ""
            add("🔴",f"<b>{esc(a['group'])}</b> — concerning message waiting <b>{fmt(a['waiting_min'])}</b>, SLA breached{ow}",a["group"],a.get("ctx_key"))
    for r in at_risk_open:
        snip=esc((r["latest"] or "")[:90])
        add("🔴",f"Churn risk unanswered: <b>{esc(r['group'])}</b> — “{snip}…”",r["group"],AI_KEYS.get(r.get("gid")))
    for a in breached:
        ow=f" · usually {esc(a['owner'])}" if a.get("owner") else ""
        add("🔴",f"<b>{esc(a['group'])}</b> — {esc(a['need_type'])} waiting <b>{fmt(a['waiting_min'])}</b>, SLA breached{ow}",a["group"],a.get("ctx_key"))
    if soon:
        add("🟠",f"<b>{len(soon)} thread{'s' if len(soon)!=1 else ''}</b> breach SLA within 30 min — save these first")
    if trend["conc"]["cur"]>trend["conc"]["prev"]:
        add("🟠",f"Concerning messages rising: <b>{trend['conc']['cur']}</b> this week vs {trend['conc']['prev']} last week")
    slow=[r for r in s["staff_rows"] if r["flag"]=="look here" and r["handled"]>=10]
    if slow:
        w=min(slow,key=lambda r:r["within_pct"])
        add("🟠",f"Check in with <b>{esc(w['name'])}</b> — median reply {fmt(w['median_min'])}, {round(w['within_pct'])}% in SLA")
    ns_open=[n for n in new_students if n["open"]]
    if ns_open:
        _nsl="".join(f'<div class=mrow><b>{esc(n["group"])}</b> · started {(n["started"]+IST).strftime("%d %b")} · {n["msgs"]} msgs</div>' for n in ns_open[:15])
        add("🟠",f"<details style=\"display:inline\"><summary style=\"cursor:pointer;display:inline\"><b>{len(ns_open)} new student group{'s' if len(ns_open)!=1 else ''}</b> awaiting a reply — first impressions at stake <span style=\"color:{MUT};font-size:12px\">(click to see)</span></summary>{_nsl}</details>")
    if not top:
        top.append(("🟢","All clear — no breaches, no churn risks, nothing urgent",""))
    top5="".join(f'<div class=t5><span class=t5i>{i}</span><span>{t}{su}</span></div>' for i,t,su in top)

    # ---- ACT NOW table (breached → breaching soon → rest, with owner) ----
    TYPE_COL={"concerning":BAD,"question":"#0891b2","request":PUR,"attachment":WARN}
    def sla_pill(a):
        if a["cold"]:    return f'<span class=pill style="background:#fef3f2;color:{BAD}">cold &gt;{COLD_HOURS}h</span>'
        if a["breached"]:return f'<span class=pill style="background:#fef3f2;color:{BAD}">breached</span>'
        if a.get("breach_soon"): return f'<span class=pill style="background:#fffaeb;color:{WARN}">&lt;30m left</span>'
        return f'<span class=pill style="background:#ecfdf3;color:{OK}">in SLA</span>'
    ordered=breached+soon+[a for a in attention if not a["breached"] and not a.get("breach_soon")]
    at_rows=""
    for a in ordered[:60]:
        typ="concerning" if a["concerning"] else a["need_type"]
        tcol=TYPE_COL.get(typ,MUT)
        ttag=f'<span class=tg style="background:#f3f4f6;color:{tcol}">{esc(typ)}</span>'
        msg=esc((a["last_text"] or "")[:150]) or "<span style='color:%s'>(attachment / media)</span>"%MUT
        ow=esc(a["owner"]) if a.get("owner") else "—"
        nrb=f' <button class=nr data-gid="{esc(a["gid"])}" data-group="{esc(a["group"])}">not relevant?</button>'
        sent=f'<div class=when>sent {when(a.get("last_ts"))} IST</div>' if a.get("last_ts") else ""
        cpy=f'<button class=cpy data-g="{esc(a["group"])}" title="Copy the group name — paste into WhatsApp search to open it">&#128203; copy name</button>'
        owner_html=f'<span class=meta>usually {ow}</span>' if a.get("owner") else ""
        at_rows+=(f'<div class=acard>'
                  f'<div class=ctop>{ttag}<span class=cname>{esc(a["group"])}</span>{cpy}{nrb}'
                  f'<span class=cright>{owner_html}<span class=cwait>waiting {fmt(a["waiting_min"])}</span>{sla_pill(a)}</span></div>'
                  f'<div class=cmsg>&ldquo;{msg}&rdquo; <span class=when>{("sent " + when(a["last_ts"]) + " IST") if a.get("last_ts") else ""}</span></div>'
                  f'{ai_line(a.get("ctx_key",""))}'
                  f'</div>')
    if not at_rows: at_rows=f'<div style="color:{OK};padding:14px">Nothing needs a reply ✓</div>'

    fyi_rows=""
    for a in fyi_open:
        _nr=f' <button class=nr data-gid="{esc(a["gid"])}" data-group="{esc(a["group"])}">not relevant?</button>'
        fyi_rows+=(f'<tr><td>{esc(a["group"])}{_nr}</td><td><div class=snip>{esc((a["last_text"] or "")[:150])}</div>'
                   f'<div class=when>sent {when(a.get("last_ts"))} IST</div></td>'
                   f'<td style="white-space:nowrap">{fmt(a["waiting_min"])}</td></tr>')
    if not fyi_rows: fyi_rows=f'<tr><td colspan=3 style="color:{MUT};padding:10px">None</td></tr>'

    # ---- AT RISK (churn watch merged with problem-group watchlist) ----
    latest_conc={}   # group name -> most recent concerning msg (text+ts fallback)
    for m in concerning_msgs:                       # already sorted newest first
        latest_conc.setdefault(m["group"],m)
    ai_key_by_group={r["group"]:AI_KEYS.get(r["gid"]) for r in at_risk if r.get("gid")}
    churn_by_group={r["group"]:r for r in at_risk}
    wl_by_group={w["group"]:w for w in watchlist}
    merged={}
    for g in list(churn_by_group)+list(wl_by_group):
        if g in merged: continue
        cr=churn_by_group.get(g); wl=wl_by_group.get(g)
        merged[g]={"group":g,"gid":(cr.get("gid") if cr else (wl.get("gid") if wl else None)),
                   "team_reply":(cr.get("team_reply") if cr else None),
                   "churn":(cr["n"] if cr else (wl["churn"] if wl else 0)),
                   "concern":(wl["concern"] if wl else (cr["n"] if cr else 0)),
                   "latest":((cr["latest"] if cr else "") or (latest_conc.get(g,{}) or {}).get("text","")),
                   "latest_ts":((cr.get("ts") if cr else None) or (latest_conc.get(g,{}) or {}).get("ts")),
                   "median":(wl["median_min"] if wl else None),
                   "open":(cr["open"] if cr else (wl["status"] in ("open","breached","cold") if wl else False)),
                   "score":((wl["score"] if wl else 0)+(cr["n"]*2 if cr else 0))}
    risk_rows=""; slow_rows=""
    for m in sorted(merged.values(),key=lambda x:(-(x.get("latest_ts").timestamp() if x.get("latest_ts") else 0),-x["score"]))[:20]:
        st=(f'<span class=pill style="background:#fef3f2;color:{BAD}">awaiting reply</span>' if m["open"]
            else f'<span class=pill style="background:#ecfdf3;color:{OK}">replied</span>')
        lat=esc((m["latest"] or "")[:130]) or f'<span style="color:{MUT}">— no flagged wording; listed for slow-service pattern (median reply &gt;2h)</span>'
        if m.get("latest") and m.get("latest_ts"):
            lat+=f'<div class=when>sent {when(m["latest_ts"])} IST</div>'
        aik=ai_key_by_group.get(m["group"])
        tr=m.get("team_reply")
        treply_html=(f'<div class=snip style="color:{OK};font-style:normal">&#8618; '
                     f'{(esc(tr["who"]) + ": ") if tr and tr.get("who") else ""}'
                     f'{esc(tr["text"])}'
                     f'{(" <span class=when>· " + when(tr["ts"]) + " IST</span>") if tr.get("ts") else ""}</div>') if tr else ""
        nrb=(f' <button class=nr data-gid="{esc(m["gid"])}" data-group="{esc(m["group"])}">not relevant?</button>'
             if m.get("gid") else "")
        cpy=f'<button class=cpy data-g="{esc(m["group"])}" title="Copy the group name — paste into WhatsApp search to open it">&#128203;</button>'
        if (m["churn"] or 0) > 0 or (m["concern"] or 0) > 0:
            risk_rows+=(f'<div class=acard>'
                        f'<div class=ctop><span class=tg style="background:#fef3f2;color:{BAD}">flagged</span>'
                        f'<span class=cname>{esc(m["group"])}</span>{cpy}{nrb}'
                        f'<span class=cright><span class=meta>median {fmt(m["median"])}</span>{st}</span></div>'
                        f'<div class=cmsg>{lat}</div>{treply_html}'
                        f'{ai_line(aik) if aik else ""}'
                        f'</div>')
        else:
            slow_rows+=(f'<div class=mrow><b>{esc(m["group"])}</b> {cpy}{nrb}'
                        f' · median reply {fmt(m["median"])} · {st}</div>')
    if not risk_rows: risk_rows=f'<div style="color:{OK};padding:14px">No flagged accounts — nothing worrying in the last 72h ✓</div>'
    n_slow=slow_rows.count("mrow")
    slow_watch=(f'<details><summary>Slow-service watch — {n_slow} groups with median reply &gt;2h · already replied, just slow</summary>{slow_rows}</details>'
                if slow_rows else "")

    # ---- concerning messages (collapsible) ----
    def _nrb(gid,name):
        return (f' <button class=nr data-gid="{esc(gid)}" data-group="{esc(name)}">not relevant?</button>'
                if gid else "")
    cm_rows=""
    for m in concerning_msgs:
        tags=" ".join(f'<span class=tg>{esc(t)}</span>' for t in m["tags"])
        risk=' <span class=pill style="background:#fef3f2;color:%s">churn</span>'%BAD if m["churn"] else ""
        cm_rows+=(f'<tr><td style="white-space:nowrap">{(m["ts"]+IST).strftime("%d %b %H:%M")}</td>'
                  f'<td>{esc(m["group"])}'
                  f'{_nrb(m.get("gid"), m["group"])}'
                  f'</td><td>{esc(m["who"])}</td><td>{tags}{risk}</td>'
                  f'<td>{esc(m["text"][:200])}</td></tr>')
    if not cm_rows: cm_rows=f'<tr><td colspan=5 style="color:{OK};padding:14px">No concerning messages ✓</td></tr>'

    # ---- topics (collapsible, small) ----
    total_inb=max(inbound_total,1)
    def bar(label,val,color):
        pct=round(val/total_inb*100)
        return (f'<div class=brow><div class=blab>{label}</div>'
                f'<div class=btrack><div class=bfill style="width:{max(pct,2)}%;background:{color}"></div></div>'
                f'<div class=bval>{val} <span style="color:{MUT}">({pct}%)</span></div></div>')
    bars=(bar("Payment / billing",cat_counts.get("payment",0),"#2563eb")+
          bar("Scheduling / cancellations",cat_counts.get("scheduling",0),"#7c3aed")+
          bar("Academic / urgent",cat_counts.get("academic",0),"#0891b2")+
          bar("Complaints / unhappy",cat_counts.get("complaint",0),BAD))

    # ---- new students (tile + collapsible top 10) ----
    ns_rows=""
    for n in new_students[:10]:
        st=(f'<span class=pill style="background:#fffaeb;color:{WARN}">awaiting</span>' if n["open"]
            else f'<span class=pill style="background:#ecfdf3;color:{OK}">engaged</span>')
        fr=fmt(n["first_resp"]) if n["first_resp"] is not None else "—"
        ns_rows+=(f'<tr><td>{esc(n["group"])}</td><td style="white-space:nowrap">{(n["started"]+IST).strftime("%d %b")}</td>'
                  f'<td style="text-align:center">{n["msgs"]}</td><td>{fr}</td>'
                  f'<td style="text-align:right">{st}</td></tr>')
    if not ns_rows: ns_rows=f'<tr><td colspan=5 style="color:{MUT};padding:12px">No new groups in the last 7 days</td></tr>'

    # ---- weekly accountability: bottom 5 responders only ----
    eligible=[r for r in s["staff_rows"] if r["handled"]>=10]
    bottom5=sorted(eligible,key=lambda r:(r["within_pct"],-r["median_min"]))[:5]
    st_rows=""
    for r in bottom5:
        col=OK if r["flag"]=="ok" else (BAD if r["flag"]=="look here" else WARN)
        st_rows+=(f'<tr><td>{esc(r["name"])}</td>'
                  f'<td>{r["handled"]}</td><td>{fmt(r["median_min"])}</td><td>{round(r["within_pct"])}%</td>'
                  f'<td>{r["concerning"]}</td><td style="text-align:right;color:{col}">{r["flag"]}</td></tr>')
    if not st_rows: st_rows=f'<tr><td colspan=6 style="color:{MUT};padding:10px">Not enough reply volume yet</td></tr>'
    full_rows=""
    for r in s["staff_rows"]:
        col=OK if r["flag"]=="ok" else (BAD if r["flag"]=="look here" else WARN)
        full_rows+=(f'<tr><td>{esc(r["name"])}</td><td style="color:{MUT}">{esc(r["phone"])}</td>'
                    f'<td>{r["handled"]}</td><td>{fmt(r["median_min"])}</td><td>{round(r["within_pct"])}%</td>'
                    f'<td>{r["concerning"]}</td><td style="text-align:right;color:{col}">{r["flag"]}</td></tr>')

    review=sorted(((sid,len(g)) for sid,g in sender_groups.items() if len(g)>=2),key=lambda x:-x[1])
    rv_rows="".join(
        f'<tr><td>{esc(best_name(sid) or "—")}</td><td style="color:{MUT}">{esc(fmt_phone(sid))}</td>'
        f'<td style="font-size:11px;color:{MUT}">{esc(sid)}</td><td>{c}</td>'
        f'<td style="text-align:right">{"team" if sid in team else "—"}</td></tr>'
        for sid,c in review[:80])

    # ---- compact footer: reply speed + busiest hours side by side ----
    maxd=max((c for _,c in dist),default=1) or 1
    dist_rows=""
    for lab,c in dist:
        w=round(c/maxd*100)
        col=OK if "min" in lab and "6" not in lab else (WARN if "2–6" in lab else (BAD if ">" in lab else "#0891b2"))
        dist_rows+=(f'<div class=brow><div class=blab style="width:90px">{lab}</div>'
                    f'<div class=btrack><div class=bfill style="width:{max(w,2)}%;background:{col}"></div></div>'
                    f'<div class=bval style="width:52px">{c}</div></div>')
    days=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    maxh=max((max(row) for row in heat),default=0) or 1
    pk_d=pk_h=pk_v=0
    for di,row in enumerate(heat):
        for hh,v in enumerate(row):
            if v>pk_v: pk_v,pk_d,pk_h=v,di,hh
    hm="<div class=hmwrap><table class=hm><tr><th></th>"
    for hh in range(24): hm+=f'<th>{hh if hh%6==0 else ""}</th>'
    hm+="</tr>"
    for di,row in enumerate(heat):
        hm+=f"<tr><td class=hd>{days[di]}</td>"
        for v in row:
            a=v/maxh
            bg=f"rgba(22,36,63,{a:.2f})" if v else "#f4f5f7"
            hm+=f'<td class=hc style="background:{bg}"></td>'
        hm+="</tr>"
    hm+="</table></div>"
    peak_note=f"Peak: {days[pk_d]} {pk_h:02d}:00 IST ({pk_v} msgs/hr)" if pk_v else ""

    # drill-down lists behind the glance tiles
    def _ai_txt(key):
        v=AI.get(key) if key else None
        return f' <span style="color:{PUR}">🤖 {html.escape(v["summary"])}</span>' if v and v.get("summary") else ""
    def _mini(rows):
        if not rows: return f'<div style="color:{MUT};padding:8px">None</div>'
        return "".join(f'<div class=mrow>{r}</div>' for r in rows[:40])
    l_needs=_mini([f'<b>{esc(a["group"])}</b>{_nrb(a.get("gid"),a["group"])} · waiting {fmt(a["waiting_min"])}{_ai_txt(a.get("ctx_key"))}' for a in attention])
    _all_open=attention+fyi_open+attach_open
    l_breach=_mini([f'<b>{esc(a["group"])}</b>{_nrb(a.get("gid"),a["group"])} · {esc(a["need_type"])} · waiting {fmt(a["waiting_min"])}{_ai_txt(a.get("ctx_key"))}'
                    for a in sorted([x for x in _all_open if x["breached"]],key=lambda x:-x["waiting_min"])])
    l_risk=_mini([f'<b>{esc(m["group"])}</b>{_nrb(m.get("gid"),m["group"])} · {esc((m["latest"] or "")[:110]) or "slow-service pattern"}{_ai_txt(ai_key_by_group.get(m["group"]))}'
                  for m in sorted(merged.values(),key=lambda x:(-(x.get("latest_ts").timestamp() if x.get("latest_ts") else 0),-x["score"]))[:40]])
    _wk=as_of-timedelta(days=7)
    l_conc=_mini([f'<b>{esc(m["group"])}</b>{_nrb(m.get("gid"),m["group"])} · {esc(m["text"][:120])}' for m in concerning_msgs if m["ts"]>=_wk])
    tile_lists=(f'<div class=tlist id=tl-needs>{l_needs}</div>'
                f'<div class=tlist id=tl-breach>{l_breach}</div>'
                f'<div class=tlist id=tl-risk>{l_risk}</div>'
                f'<div class=tlist id=tl-conc>{l_conc}</div>')

    medcol=OK if (s["within_sla_pct"] or 0)>=90 else (WARN if (s["within_sla_pct"] or 0)>=75 else BAD)

    flags_panel=""
    if team_tally is not None and True:   # master view only
        if FLAGGED_INFO:
            rows="".join(
                f'<tr><td>{esc(v.get("group") or gid)}</td><td>{esc(v.get("reason",""))}</td>'
                f'<td style="white-space:nowrap;color:{MUT}">{esc(v.get("by",""))}</td>'
                f'<td style="white-space:nowrap;color:{MUT}">{esc((v.get("ts") or "")[:16].replace("T"," "))} UTC</td></tr>'
                for gid,v in sorted(FLAGGED_INFO.items(), key=lambda kv:kv[1].get("ts",""), reverse=True))
            body=f'<table><thead><tr><th>Group</th><th>Reason given</th><th>Flagged by</th><th>When</th></tr></thead><tbody>{rows}</tbody></table>'
        else:
            body=f'<div style="color:{MUT};padding:6px 2px">No groups flagged yet. The team can flag noise with the "not relevant?" buttons.</div>'
        flags_panel=(f'<details class=panel><summary>Flagged as not relevant <span class=cap>{len(FLAGGED_INFO)} groups excluded from monitoring · review weekly</span></summary>'
                     f'<div class=panelbody>{body}'
                     f'<p class=note>To un-flag a group, delete its entry in the Cloudflare KV namespace (apguru-flags) — it returns on the next run.</p></div></details>')

    team_strip=""
    if team_tally:
        cards=""
        for slug,tl,ts in team_tally:
            nc=BAD if ts["needs_attention"] else MUT
            bc=BAD if ts["breached_open"] else MUT
            rc=BAD if ts["accounts_at_risk"] else MUT
            cards+=(f'<a class=teamcard href="{slug}"><div class=tt>{esc(tl)}</div>'
                    f'<div class=tm><b style="color:{nc}">{ts["needs_attention"]}</b> to reply · '
                    f'<b style="color:{bc}">{ts["breached_open"]}</b> breached · '
                    f'<b style="color:{rc}">{ts["accounts_at_risk"]}</b> at risk</div></a>')
        team_strip=f'<details class=panel open><summary>By team — where to look</summary><div class=panelbody><div class=teamgrid>{cards}</div></div></details>'

    H=f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content="{REFRESH_SECONDS}">
<title>AP Guru — WhatsApp monitor</title>
<style>
*{{box-sizing:border-box}} body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:{INK};margin:0;background:#eef1f6;line-height:1.55}}
.sheet{{max-width:1010px;margin:26px auto;background:#fff;border:1px solid {LINE};border-radius:18px;padding:30px 34px;box-shadow:0 2px 8px rgba(16,24,40,.06)}}
.brand{{max-width:1010px;margin:22px auto 0;padding:0 6px}} .brand img{{height:40px;width:auto}}
.hdr{{margin-bottom:20px;padding-bottom:18px;border-bottom:1px solid {LINE}}}
h1{{font-size:21px;margin:0 0 2px}} .sub{{color:{MUT};font-size:12.5px}}
h2{{background:#f5f7fb;border-left:3px solid {NAVY};padding:8px 12px;border-radius:8px}}
tbody tr:nth-child(even) td{{background:#fafbfd}}
.cardslist{{display:flex;flex-direction:column;gap:10px;margin:8px 0}}
.acard{{border:1px solid {LINE};border-radius:12px;padding:11px 14px;background:#fff}}
.ctop{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.cname{{font-weight:600;font-size:14.5px}}
.cright{{margin-left:auto;display:flex;align-items:center;gap:10px;white-space:nowrap}}
.cwait{{font-size:12px;font-weight:600}}
.cmsg{{font-size:13px;color:#374151;margin-top:6px;font-style:italic}}
.cmsg .when{{font-style:normal}}

details.panel{{border:1px solid {LINE};border-radius:12px;margin:12px 0;background:#fff;overflow:hidden}}
details.panel>summary{{list-style:none;cursor:pointer;padding:12px 15px;font-size:15px;font-weight:600;
  background:#f5f7fb;border-left:3px solid {NAVY};display:flex;align-items:center;justify-content:space-between}}
details.panel>summary::-webkit-details-marker{{display:none}}
details.panel>summary::after{{content:"▸";color:{MUT};font-weight:400;transition:transform .15s}}
details.panel[open]>summary::after{{transform:rotate(90deg)}}
details.panel>summary .cap{{font-weight:400;color:{MUT};font-size:12px;margin-left:8px}}
.panelbody{{padding:14px 15px 4px}}
.cpy{{font-size:10px;color:#6b7280;background:#f8f9fb;border:1px solid #e7e9ee;border-radius:14px;padding:1px 7px;cursor:pointer;vertical-align:middle}}
.cpy:hover{{background:#eef1f6}}
.nr{{font-size:10.5px;color:{MUT};background:#f3f4f6;border:1px solid {LINE};border-radius:20px;padding:1px 8px;margin-top:5px;cursor:pointer}}
.nr:hover{{background:#e9eaee}}
.tlist{{display:none;margin-top:12px;border-top:1px solid {LINE};padding-top:10px}}
.mrow{{font-size:12.5px;padding:5px 2px;border-bottom:1px solid #f1f2f5}}
.when{{font-size:10.5px;color:#98a2b3;margin-top:2px}}
@media(max-width:760px){{.sheet{{margin:0;border-radius:0;padding:18px 14px}} table{{display:block;overflow-x:auto}} h1{{font-size:18px}}}}
.t5{{display:flex;gap:10px;align-items:flex-start;background:#f7f8fa;border:1px solid {LINE};border-radius:11px;padding:11px 14px;margin-bottom:7px;font-size:14px}}
.teamgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:6px}}
.teamcard{{display:block;background:#fff;border:1px solid {LINE};border-radius:11px;padding:12px 14px;text-decoration:none;color:{INK}}}
.teamcard:hover{{border-color:{NAVY}}} .tt{{font-weight:600;font-size:14px;margin-bottom:3px}} .tm{{font-size:12px;color:{MUT}}}
.t5i{{flex:none}} .t5s{{font-size:12.5px;color:{PUR};margin-top:3px;font-weight:400}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:11px;margin-bottom:10px}}
.c{{background:#f7f8fa;border:1px solid {LINE};border-radius:11px;padding:13px}}
.lbl{{font-size:12px;color:{MUT}}} .val{{font-size:23px;font-weight:600;margin-top:2px}} .csub{{font-size:11.5px;margin-top:3px}}
h2{{font-size:15px;margin:30px 0 8px}} .sec{{font-size:12px;color:{MUT};margin:2px 0 14px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th{{text-align:left;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:{MUT};padding:7px 8px;border-bottom:1px solid {LINE}}}
td{{padding:8px;border-bottom:1px solid {LINE};vertical-align:top}}
.pill{{font-size:11px;padding:2px 9px;border-radius:20px;font-weight:600;white-space:nowrap}}
.tg{{font-size:10.5px;background:#eef2ff;color:{PUR};padding:1px 7px;border-radius:20px;margin-right:3px;white-space:nowrap}}
.snip{{font-size:11.5px;color:{MUT};margin-top:3px;font-style:italic}}
.note{{font-size:12px;color:{MUT};margin-top:8px}} details{{margin-top:8px}} summary{{cursor:pointer;font-size:13px;color:{NAVY}}}
.duo{{display:grid;grid-template-columns:1fr 1fr;gap:26px}} @media(max-width:760px){{.duo{{grid-template-columns:1fr}}}}
.hmwrap{{overflow-x:auto}} table.hm{{border-collapse:separate;border-spacing:1.5px;font-size:9.5px;width:auto}}
table.hm th{{border:none;padding:0;text-align:center;width:15px;color:{MUT};font-size:9px;letter-spacing:0}}
table.hm td.hc{{width:15px;height:13px;border:none;border-radius:2px;padding:0}}
table.hm td.hd{{border:none;padding:0 7px 0 0;color:{MUT};font-size:10.5px;white-space:nowrap}}
.brow{{display:flex;align-items:center;gap:9px;margin:5px 0}} .blab{{width:210px;font-size:12.5px}}
.btrack{{flex:1;background:#f0f1f4;border-radius:6px;height:12px;overflow:hidden}} .bfill{{height:100%}}
.bval{{width:96px;text-align:right;font-size:12px}}
</style></head><body>
<div class=brand><img src="logo.png" alt="AP Guru" onerror="this.style.display='none'"></div>
<div class=sheet>
<div class=hdr>
<h1>WhatsApp monitor · {html.escape(label)}</h1>
<div class=sub>Updated {(as_of+IST).strftime('%d %b %Y, %H:%M')} IST · refreshes ~every 15 min during peak (9:30am–8pm IST), hourly mornings &amp; evenings · SLA 2h (12pm–11pm IST) / 6h overnight · reply times exclude 10pm–6am IST</div>
</div>
{team_strip}

<details class=panel open><summary>Top 5 today</summary><div class=panelbody>
{top5}
</div></details>

<details class=panel open><summary>At a glance</summary><div class=panelbody>
<div class=cards>
{card("Needs reply now", s["needs_attention"], BAD if s["needs_attention"] else OK, target="tl-needs")}
{card("SLA breached", s["breached_open"], BAD if s["breached_open"] else OK, f'<span style="color:{WARN}">{s["breach_soon"]} breaching &lt;30m</span>' if s["breach_soon"] else "", target="tl-breach")}
{card("Accounts at risk", s["accounts_at_risk"], BAD if s["accounts_at_risk"] else OK, target="tl-risk")}
{card("Concerning (7d)", trend["conc"]["cur"], BAD if trend["conc"]["cur"]>trend["conc"]["prev"] else OK, delta_n(trend["conc"]["cur"],trend["conc"]["prev"]), target="tl-conc")}
{card("Median reply (7d)", fmt(trend["med"]["cur"]), INK, delta_reply(trend["med"]["cur"],trend["med"]["prev"]))}
{card("Within SLA (7d)", (str(round(trend["within"]["cur"]))+"%") if trend["within"]["cur"] is not None else "—", medcol, delta_pct(trend["within"]["cur"],trend["within"]["prev"]))}
</div>
{tile_lists}
</div></details>

<details class=panel open><summary>Act now — needs your reply <span class=cap>{s["needs_attention"]} threads · newest first · last 72h</span></summary><div class=panelbody>
<div class=sec>Only open threads whose last parent message needs a response. "usually X" = the team member who replies most in that group.</div>
<div class=cardslist>{at_rows}</div>
<details><summary>Attachments awaiting acknowledgement — {len(attach_open)} (homework, screenshots, docs)</summary>
<div class=sec>Last parent message is a file/media with no question. Worth a quick 👍 or "received", but not urgent.</div>
<table><thead><tr><th>Student group</th><th>Waiting</th></tr></thead><tbody>{"".join(f'<tr><td>{esc(a["group"])}</td><td style="white-space:nowrap">{fmt(a["waiting_min"])}</td></tr>' for a in attach_open) or f'<tr><td colspan=2 style="color:{MUT};padding:10px">None</td></tr>'}</tbody></table>
</details>
<details><summary>Open but likely no reply needed — {s["fyi_open"]} FYI/statements</summary>
<table><thead><tr><th>Student group</th><th>Last message</th><th>Waiting</th></tr></thead><tbody>{fyi_rows}</tbody></table>
</details>
</div></details>

<details class=panel open><summary>Accounts at risk <span class=cap>churn wording + problem groups · newest first · last 72h</span></summary><div class=panelbody>
<div class=sec>Groups where a parent used refund/stop/complaint wording. Call these. Slow-reply groups sit in the watch list below.</div>
<div class=cardslist>{risk_rows}</div>
{slow_watch}

<details><summary>All concerning messages — {len(concerning_msgs)} in last {LOOKBACK_DAYS} days</summary>
<table><thead><tr><th>When</th><th>Group</th><th>From</th><th>Type</th><th>Message</th></tr></thead><tbody>{cm_rows}</tbody></table>
</details>

<details><summary>What parents message about — last {LOOKBACK_DAYS} days ({inbound_total} messages)</summary>
{bars}
</details>
</div></details>

<details class=panel><summary>Team accountability <span class=cap>bottom 5 responders, min 10 replies</span></summary><div class=panelbody>
<table><thead><tr><th>Team member</th><th>Replies</th><th>Median</th><th>Within SLA</th><th>Concerning handled</th><th style="text-align:right">Flag</th></tr></thead><tbody>{st_rows}</tbody></table>
<details><summary>Full team table ({len(s["staff_rows"])} members)</summary>
<table><thead><tr><th>Team member</th><th>Phone</th><th>Replies</th><th>Median</th><th>Within SLA</th><th>Concerning</th><th style="text-align:right">Flag</th></tr></thead><tbody>{full_rows}</tbody></table>
<p class=note>"look here" = slow but not busy · "heavy load" = slow but above-median volume. Fix names in <b>staff_names.json</b>.</p>
</details>
<details><summary>Auto-detected team list — review once</summary>
<table><thead><tr><th>Name</th><th>Phone</th><th>ID</th><th>Groups</th><th style="text-align:right">Treated as</th></tr></thead><tbody>{rv_rows}</tbody></table>
</details>
</div></details>

<details class=panel><summary>Operations snapshot</summary><div class=panelbody>
<div class=duo>
<div>
<div class=sec style="margin-bottom:6px">Reply speed ({s["responses_measured"]} first replies)</div>
{dist_rows}
</div>
<div>
<div class=sec style="margin-bottom:6px">Busiest hours (IST) · {peak_note}</div>
{hm}
</div>
</div>
</div></details>
{flags_panel}
</div>
<script>
// Hide already-flagged rows immediately on every page load (before the next
// rebuild makes it permanent). Counts/tiles update on the next data refresh.
fetch('/api/flags').then(function(r){{return r.ok?r.json():{{}};}}).then(function(f){{
  var n=0;
  document.querySelectorAll('.nr').forEach(function(b){{
    if(f && f[b.dataset.gid]){{
      var tr=b.closest('tr'); if(tr){{tr.style.display='none'; n++;}}
    }}
  }});
  if(n){{
    var sub=document.querySelector('.sub');
    if(sub) sub.innerHTML+=' · <span style="color:#98a2b3">'+n+' flagged row'+(n>1?'s':'')+' hidden (counts update next refresh)</span>';
  }}
}}).catch(function(){{}});
document.addEventListener('click',async function(e){{
  var c=e.target.closest('.cpy');
  if(c){{
    try{{await navigator.clipboard.writeText(c.dataset.g);
      var t=c.textContent; c.textContent='copied \u2713';
      setTimeout(function(){{c.textContent=t;}},1400);
    }}catch(err){{ prompt('Copy the group name:', c.dataset.g); }}
    return;
  }}
  var b=e.target.closest('.nr');
  if(b){{
    var reason=prompt("Why is this not relevant? (sent to Chirag to improve the system)");
    if(reason===null) return;
    b.disabled=true;b.textContent='saving…';
    try{{
      var r=await fetch('/api/flag',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{gid:b.dataset.gid,group:b.dataset.group,reason:reason}})}});
      if(!r.ok) throw 0;
      b.textContent='flagged ✓';
      var tr=b.closest('tr'); if(tr) tr.style.opacity=.35;
    }}catch(err){{
      b.disabled=false;b.textContent='not relevant?';
      alert('Could not save the flag — is the FLAGS KV binding set up in Cloudflare?');
    }}
    return;
  }}
  var c=e.target.closest('[data-t]');
  if(c){{
    var el=document.getElementById(c.dataset.t);
    if(el){{
      var show=el.style.display!=='block';
      document.querySelectorAll('.tlist').forEach(function(x){{x.style.display='none';}});
      el.style.display=show?'block':'none';
    }}
  }}
}});
</script>
</body></html>"""
    open(os.path.join(here,outfile),"w").write(H)

if __name__=="__main__":
    s,counts=build_report()
    print(f"MASTER groups={s['groups']} inbound={s['inbound_messages']} needs={s['needs_attention']} "
          f"at_risk={s['accounts_at_risk']} within={round(s['within_sla_pct']) if s['within_sla_pct'] is not None else '—'}%")
    for label,n in counts.items():
        if label!="All courses — master": print(f"  {label:20} needs_reply={n}")

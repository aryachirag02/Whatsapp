#!/usr/bin/env python3
"""
AP Guru WhatsApp pull (READ ONLY — no sending, no write scope).

INCREMENTAL: keeps a rolling 14-day store in messages_latest.json. Each run it
only re-reads groups whose WhatsApp last-activity timestamp changed since the
previous run, fetches just the new messages, and merges them (deduped by
message id). Groups with no new activity are skipped entirely — so an hourly
run usually touches only a handful of the ~380 groups instead of all of them.

Set "incremental": false in config.json to force a full 14-day re-pull.

Stored per message: id, group, time, sender phone, sender name, push name,
is_self flag, attachment flag, message text, and a derived is_filler flag.
Also maintains attendees.json (lid -> phone + name directory) and state.json.
"""
import json, os, time, zlib, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

here = os.path.dirname(os.path.abspath(__file__))
cfg  = json.load(open(os.path.join(here, "config.json")))
API = (os.environ.get("UNIPILE_API_KEY") or cfg.get("api_key", "")).strip()
DSN = cfg["dsn"].rstrip("/")
print(f"auth check: key length={len(API)} | dsn={DSN}")
# Multi-account: config "accounts" = {account_id: team}. Back-compat: single account_id.
_accts = cfg.get("accounts")
if isinstance(_accts, dict):   ACCOUNTS = list(_accts.keys())
elif isinstance(_accts, list): ACCOUNTS = list(_accts)
else:                          ACCOUNTS = [cfg["account_id"]]
LOOKBACK_DAYS = cfg.get("lookback_days", 14)
STORE_TEXT    = cfg.get("store_text", True)
INCREMENTAL   = cfg.get("incremental", True)
OVERLAP_MIN   = cfg.get("overlap_minutes", 20)   # safety re-fetch window
EXCLUDE_KEYWORDS = [k.lower() for k in cfg.get("exclude_keywords",
                    ["coordination", "content", "grading", "internal", "team", "staff"])]
now    = datetime.now(timezone.utc)
cutoff = now - timedelta(days=LOOKBACK_DAYS)

STORE = os.path.join(here, "messages_latest.json")
STATE = os.path.join(here, "state.json")
DIRP  = os.path.join(here, "attendees.json")

FILLER = {"thanks","thank you","thankyou","thanku","thx","ty","ok","okay","okk","okie",
          "noted","great","sure","perfect","done","got it","alright","cool","fine",
          "yes","yep","yeah","no","welcome","good","nice","super","🙏","👍","❤️","🙂"}
def is_filler(t):
    if not t: return False
    keep = "".join(c for c in t.lower() if c.isalnum() or c.isspace() or c in "🙏👍❤️🙂").strip()
    if keep == "": return False
    if keep in FILLER: return True
    w = keep.split(); return len(w) <= 2 and all(x in FILLER for x in w)

def digits(s): return "".join(ch for ch in (s or "") if ch.isdigit())
def parse_ts(s): return datetime.fromisoformat(s.replace("Z","+00:00")) if s else None

def api_get(path, params):
    qs = urllib.parse.urlencode(params)
    url = f"{DSN}/api/v1/{path}?{qs}"
    req = urllib.request.Request(url, headers={"X-API-KEY": API, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def list_group_chats(acct):
    """Starting list = named groups (type 1), not internal, with activity in the
    last LOOKBACK_DAYS. Returns [(id, name, last_activity_dt)]."""
    chats, cursor, total, named, stale, excl = [], None, 0, 0, 0, 0
    while True:
        p = {"account_id": acct, "limit": 100}
        if cursor: p["cursor"] = cursor
        data = api_get("chats", p)
        items = data.get("items", [])
        total += len(items)
        for c in items:
            if c.get("type") != 1 or not c.get("name"): continue
            named += 1
            nm = c["name"].lower()
            if any(k in nm for k in EXCLUDE_KEYWORDS): excl += 1; continue
            ts = parse_ts(c.get("timestamp"))
            if ts and ts < cutoff: stale += 1; continue     # no activity in window
            chats.append((c["id"], c["name"], ts))
        cursor = data.get("cursor")
        if not cursor: break
        time.sleep(0.3)
    print(f"  chats scanned={total} named groups={named} "
          f"(excluded internal={excl}, stale>{LOOKBACK_DAYS}d={stale}) -> starting list={len(chats)}")
    return chats

def attendee_map(chat_id, directory=None):
    amap, cursor = {}, None
    while True:
        p = {"limit": 100}
        if cursor: p["cursor"] = cursor
        data = api_get(f"chats/{chat_id}/attendees", p)
        for a in data.get("items", []):
            phone = digits(a.get("specifics", {}).get("phone_number"))
            name  = (a.get("name") or "").strip()
            isself = int(a.get("is_self") or 0)
            rec = {"name": name, "phone": phone, "is_self": isself}
            keys = [a.get("provider_id"), a.get("public_identifier"),
                    a.get("specifics", {}).get("lid")]
            for key in keys:
                if key: amap[key] = rec
            if directory is not None:
                for key in keys:
                    if key and phone: directory["lid2phone"][key] = phone
                canon = phone or (keys[0] or keys[1] or keys[2])
                if canon and name: directory["names"][canon] = name
                if isself and phone: directory["self_phones"].add(phone)
        cursor = data.get("cursor")
        if not cursor: break
    return amap

def resolve_sender(m, amap):
    sid = m.get("sender_id") or ""
    a = amap.get(sid)
    push = ""
    orig = m.get("original")
    if isinstance(orig, str):
        try: push = (json.loads(orig).get("pushName") or "").strip()
        except Exception: push = ""
    if a:
        sender = a["phone"] or ("lid:" + sid)
        name = a["name"] or push
        is_self = a["is_self"] or int(m.get("is_sender") or 0)
    else:
        phone = digits(sid) if "@s.whatsapp.net" in sid else ""
        sender = phone or ("lid:" + sid if sid else None)
        name = push
        is_self = int(m.get("is_sender") or 0)
    return sender, name, push, is_self

def pull_messages(chat_id, name, amap, since, acct):
    """Fetch messages newest-first; stop once older than `since`."""
    out, cursor = [], None
    while True:
        p = {"limit": 100}
        if cursor: p["cursor"] = cursor
        data = api_get(f"chats/{chat_id}/messages", p)
        items = data.get("items", [])
        if not items: break
        stop = False
        for m in items:
            t = parse_ts(m.get("timestamp"))
            if not t: continue
            if t < since: stop = True; continue
            if m.get("is_event") or m.get("hidden") or m.get("deleted"): continue
            sender, sname, push, is_self = resolve_sender(m, amap)
            text = m.get("text") or ""
            mid = m.get("id") or m.get("provider_id") or f"{chat_id}:{m.get('timestamp')}:{sender}"
            out.append({"mid": mid, "group_id": chat_id, "group_name": name,
                        "account_id": acct,
                        "timestamp": m.get("timestamp"), "sender": sender,
                        "sender_name": sname, "push_name": push, "is_self": is_self,
                        "has_attachment": bool(m.get("attachments")),
                        "is_filler": is_filler(text),
                        "text": text if STORE_TEXT else ""})
        cursor = data.get("cursor")
        if stop or not cursor: break
        time.sleep(0.3)
    return out

def ckey(r):
    """Stable identity for a message, independent of whether an id was stored.
    Avoids duplicates when overlapping windows are re-fetched."""
    t = (r.get("text") or "")[:24]
    return f"{r.get('group_id')}|{r.get('timestamp')}|{r.get('sender')}|{zlib.crc32(t.encode())}"

def load_store():
    if not os.path.exists(STORE): return {}
    try:
        recs = json.load(open(STORE))
    except Exception:
        return {}
    return {ckey(r): r for r in recs}

def load_directory():
    if os.path.exists(DIRP):
        d = json.load(open(DIRP))
        return {"lid2phone": d.get("lid2phone", {}), "names": d.get("names", {}),
                "self_phones": set(d.get("self_phones", []))}
    return {"lid2phone": {}, "names": {}, "self_phones": set()}

def main():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    last_run = parse_ts(state.get("last_run")) if INCREMENTAL else None
    store = load_store()
    directory = load_directory()
    have_store = len(store) > 0

    incr = bool(last_run and have_store)
    overlap = (last_run - timedelta(minutes=OVERLAP_MIN)) if incr else cutoff
    print(f"Pull mode: {'incremental' if incr else 'full backfill'} "
          f"({LOOKBACK_DAYS}d window); store has {len(store)} messages; "
          f"{len(ACCOUNTS)} account(s)")

    # gather chats across every connected account (program head = team)
    all_chats = []                      # (acct, gid, name, ts)
    for acct in ACCOUNTS:
        print(f"Account {acct}:")
        for gid, name, ts in list_group_chats(acct):
            all_chats.append((acct, gid, name, ts))
    known = {r["group_id"] for r in store.values()}

    # Decide per group: backfill brand-new groups fully; otherwise only fetch new.
    todo = []   # (acct, gid, name, since_for_this_group)
    for acct, gid, name, ts in all_chats:
        if gid not in known:
            todo.append((acct, gid, name, cutoff))           # new group -> full backfill
        elif (not incr) or (ts is None) or (ts >= overlap):
            todo.append((acct, gid, name, overlap))          # has new activity -> increment
        # else: in store and no new activity -> skip
    new_groups = sum(1 for g in todo if g[3] is cutoff)
    print(f"  {len(all_chats)} groups across accounts; fetching {len(todo)} "
          f"({new_groups} new backfills, {len(todo)-new_groups} updates)")

    fetched = 0
    for i, (acct, gid, name, gsince) in enumerate(todo, 1):
        try:
            amap = attendee_map(gid, directory)
            for r in pull_messages(gid, name, amap, gsince, acct):
                store[ckey(r)] = r; fetched += 1
        except Exception as e:
            print(f"  ! {name}: {e}")
        if i % 25 == 0: print(f"  {i}/{len(todo)} groups...")
        time.sleep(0.2)

    # prune to rolling window
    recs = [r for r in store.values() if (parse_ts(r.get("timestamp")) or now) >= cutoff]
    recs.sort(key=lambda r: r.get("timestamp") or "")
    json.dump(recs, open(STORE, "w"), ensure_ascii=False, indent=1)
    json.dump(recs, open(os.path.join(here, f"messages_{now:%Y-%m-%d}.json"), "w"),
              ensure_ascii=False, indent=1)
    directory["self_phones"] = sorted(directory["self_phones"])
    json.dump(directory, open(DIRP, "w"), ensure_ascii=False, indent=1)
    json.dump({"last_run": now.isoformat()}, open(STATE, "w"), indent=1)
    print(f"Fetched {fetched} new/updated; store now {len(recs)} messages (14d) "
          f"from {len({r['group_id'] for r in recs})} groups -> messages_latest.json")

if __name__ == "__main__":
    main()

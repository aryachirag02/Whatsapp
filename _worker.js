// Cloudflare Pages — advanced-mode Worker.
// ONE dashboard URL for everyone; content is filtered by WHO logs in.
//   - team "all"  -> master, and can drill into any team via the path
//   - a team slug -> that team's dashboard only, at EVERY path
//
// Credentials live in a Cloudflare Pages env var named USERS (never in repo).
// USERS is JSON: email -> { "pass": "...", "team": "..." }
// team is one of: all | sat | ap | ib | igcse | myp | else
//
// NOTE: paths are extensionless ("/sat", not "/sat.html") because Pages
// auto-redirects "*.html" to the pretty URL, which would loop.

// ---- email sending (owner only): Unipile -> Chirag's Gmail ----
const UNIPILE_DSN = "https://api55.unipile.com:18582";
const GMAIL_ACCOUNT_ID = "aN0iYRJjTEq3hEjtMPH-Yw";
const INSTAGRAM_URL = "https://www.instagram.com/apguru";  // TODO(Chirag): confirm handle
const SIGNATURE_HTML = '<br><br>' +
 '<table cellpadding="0" cellspacing="0" style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">' +
 '<tr><td style="padding-right:16px;vertical-align:top">' +
 '<img src="https://chirag.apguru.com/sig-logo.png" alt="AP Guru" width="90" style="display:block"></td>' +
 '<td style="border-left:2px solid #333;padding-left:16px;line-height:1.6">' +
 '<b>AP Guru</b><br>' +
 'SAT | ACT | IB | AP | A-levels | IGCSE | GMAT<br>' +
 '+91 9920200350<br>' +
 '<a href="https://www.apguru.com" style="color:#222">www.apguru.com</a><br>' +
 'Connect with us on <a href="' + INSTAGRAM_URL + '" style="color:#c13584">Instagram</a>' +
 '</td></tr></table>';
function escapeHtml(t) {
  return String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
// attachments come per-request from the leads page (repo /marketing folder only)
const ATTACH_RE = /^marketing\/[\w\-. ()']+\.(pdf|png|jpe?g)$/;

async function sendLeadEmail(request, env, origin) {
  if (!env.UNIPILE_API_KEY)
    return new Response('{"error":"UNIPILE_API_KEY not configured in Cloudflare Pages secrets"}',
      { status: 503, headers: { "Content-Type": "application/json" } });
  let d = {};
  try { d = await request.json(); } catch {}
  if (!d.to || !d.subject || !d.body)
    return new Response('{"error":"to, subject, body required"}',
      { status: 400, headers: { "Content-Type": "application/json" } });
  const fd = new FormData();
  fd.append("account_id", GMAIL_ACCOUNT_ID);
  fd.append("subject", String(d.subject).slice(0, 300));
  const bodyHtml = escapeHtml(String(d.body).slice(0, 8000)).replace(/\n/g, "<br>") + SIGNATURE_HTML;
  fd.append("body", bodyHtml);
  fd.append("to", JSON.stringify([{ display_name: String(d.name || "").slice(0, 100),
                                    identifier: String(d.to).slice(0, 200) }]));
  const files = Array.isArray(d.files) ? d.files.slice(0, 5) : [];
  for (const f of files) {
    if (!ATTACH_RE.test(f)) continue;
    try {
      const url = origin + "/" + f.split("/").map(encodeURIComponent).join("/");
      const res = await env.ASSETS.fetch(new Request(url));
      if (res.ok) fd.append("attachments", await res.blob(), f.split("/").pop());
    } catch {}
  }
  const r = await fetch(UNIPILE_DSN + "/api/v1/emails",
    { method: "POST", headers: { "X-API-KEY": env.UNIPILE_API_KEY }, body: fd });
  const txt = await r.text();
  return new Response(txt, { status: r.status, headers: { "Content-Type": "application/json" } });
}

function checkOwnerBasic(request, env) {
  let users; try { users = JSON.parse(env.USERS || "{}"); } catch { return false; }
  const hdr = request.headers.get("Authorization") || "";
  const [sch, enc] = hdr.split(" ");
  if (sch !== "Basic" || !enc) return false;
  try {
    const dec = atob(enc); const i = dec.indexOf(":");
    const em = dec.slice(0, i).trim().toLowerCase(); const pw = dec.slice(i + 1);
    const u = users[em];
    return !!(u && pw === u.pass && (u.team || "") === "all");
  } catch { return false; }
}

const TEAM_PATH = {
  all: "/", sat: "/sat", ap: "/ap",
  ib: "/ib", igcse: "/igcse", myp: "/myp", else: "/else",
};
const OWNER_PATHS = new Set(["/", "/sat", "/ap", "/ib", "/igcse", "/myp", "/else", "/ceo", "/inbox", "/leads"]);

function unauthorized() {
  return new Response("Authentication required.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="AP Guru dashboard", charset="UTF-8"' },
  });
}

export default {
  async fetch(request, env) {
    // Owner's worry list: ungated, protected only by an unguessable URL.
    // Also served ungated on the anita.* hostname (owner's custom domain).
    {
      const u0 = new URL(request.url);
      if (u0.hostname.startsWith("anita.")) {
        if (u0.pathname !== "/logo.png" && u0.pathname !== "/inbox") u0.pathname = "/ceo";
        return env.ASSETS.fetch(new Request(u0.toString(), request));
      }
      // chirag.* -> tabbed shell (Inbox + Leads), ungated; iframes load /inbox and /leads
      if (u0.hostname.startsWith("chirag.")) {
        if (u0.pathname === "/api/sendmail" && request.method === "POST") {
          if (!checkOwnerBasic(request, env))
            return new Response('{"error":"login required"}',
              { status: 401, headers: { "Content-Type": "application/json" } });
          return sendLeadEmail(request, env, u0.origin);
        }
        if (u0.pathname === "/api/leadflag" && request.method === "POST" && env.FLAGS) {
          let d = {};
          try { d = await request.json(); } catch {}
          if (d.lead_id) {
            await env.FLAGS.put("lead:" + d.lead_id, JSON.stringify({
              name: (d.name || "").slice(0, 120), kind: (d.kind || "already_messaged").slice(0, 40),
            reason: (d.reason || "").slice(0, 200),
              by: "chirag-host", ts: new Date().toISOString(),
            }));
            return new Response('{"ok":true}', { headers: { "Content-Type": "application/json" } });
          }
          return new Response('{"error":"lead_id required"}', { status: 400, headers: { "Content-Type": "application/json" } });
        }
        if (u0.pathname === "/api/leadflags" && env.FLAGS) {
          const out = {};
          const list = await env.FLAGS.list({ prefix: "lead:" });
          for (const k of list.keys) {
            const v = await env.FLAGS.get(k.name);
            if (v) out[k.name.slice(5)] = JSON.parse(v);
          }
          return new Response(JSON.stringify(out), { headers: { "Content-Type": "application/json" } });
        }
        if (u0.pathname === "/api/waflag" && request.method === "POST" && env.FLAGS) {
          let d = {};
          try { d = await request.json(); } catch {}
          const ph = String(d.phone || "").replace(/\D/g, "");
          if (ph.length >= 10 && ph.length <= 15) {
            await env.FLAGS.put("wa:" + ph, "0");
            return new Response('{"ok":true}', { headers: { "Content-Type": "application/json" } });
          }
          return new Response('{"error":"phone required"}', { status: 400, headers: { "Content-Type": "application/json" } });
        }
        if (u0.pathname === "/api/waflags" && env.FLAGS) {
          const out = {};
          const list = await env.FLAGS.list({ prefix: "wa:" });
          for (const k of list.keys) out[k.name.slice(3)] = false;
          return new Response(JSON.stringify(out), { headers: { "Content-Type": "application/json" } });
        }
        const pass = new Set(["/inbox", "/leads", "/ceo", "/logo.png", "/sig-logo.png"]);
        if (!pass.has(u0.pathname) && !u0.pathname.startsWith("/marketing/")) u0.pathname = "/leads";
        return env.ASSETS.fetch(new Request(u0.toString(), request));
      }
      if (u0.pathname === "/ceo-a1e14bac51" || u0.pathname === "/ceo-a1e14bac51/") {
        u0.pathname = "/ceo";
        return env.ASSETS.fetch(new Request(u0.toString(), request));
      }
    }
    let users;
    try { users = JSON.parse(env.USERS || "{}"); }
    catch { return new Response("Auth config error (USERS not valid JSON).", { status: 503 }); }
    if (!Object.keys(users).length) return new Response("Auth not configured.", { status: 503 });

    const header = request.headers.get("Authorization") || "";
    const [scheme, encoded] = header.split(" ");
    if (scheme === "Basic" && encoded) {
      let decoded = "";
      try { decoded = atob(encoded); } catch { return unauthorized(); }
      const i = decoded.indexOf(":");
      const email = decoded.slice(0, i).trim().toLowerCase();
      const pass = decoded.slice(i + 1);
      const u = users[email];
      if (u && pass === u.pass) {
        const url0 = new URL(request.url);

        // ---- feedback API: "Not relevant" flags (stored in KV binding FLAGS) ----
        if (url0.pathname === "/api/flag" && request.method === "POST") {
          if (!env.FLAGS) return new Response(JSON.stringify({ error: "KV binding FLAGS not configured" }),
            { status: 503, headers: { "Content-Type": "application/json" } });
          let d = {};
          try { d = await request.json(); } catch {}
          if (!d.gid) return new Response(JSON.stringify({ error: "gid required" }),
            { status: 400, headers: { "Content-Type": "application/json" } });
          await env.FLAGS.put("flag:" + d.gid, JSON.stringify({
            group: (d.group || "").slice(0, 200),
            reason: (d.reason || "").slice(0, 500),
            by: email, ts: new Date().toISOString(),
          }));
          return new Response(JSON.stringify({ ok: true }),
            { headers: { "Content-Type": "application/json" } });
        }
        if (url0.pathname === "/api/sendmail" && request.method === "POST") {
          if ((u.team || "") !== "all")
            return new Response('{"error":"owner only"}',
              { status: 403, headers: { "Content-Type": "application/json" } });
          return sendLeadEmail(request, env, url0.origin);
        }
        if (url0.pathname === "/api/leadflag" && request.method === "POST") {
          if (!env.FLAGS) return new Response(JSON.stringify({ error: "KV binding FLAGS not configured" }),
            { status: 503, headers: { "Content-Type": "application/json" } });
          let d = {};
          try { d = await request.json(); } catch {}
          if (!d.lead_id) return new Response(JSON.stringify({ error: "lead_id required" }),
            { status: 400, headers: { "Content-Type": "application/json" } });
          await env.FLAGS.put("lead:" + d.lead_id, JSON.stringify({
            name: (d.name || "").slice(0, 120), kind: (d.kind || "already_messaged").slice(0, 40),
            reason: (d.reason || "").slice(0, 200),
            by: email, ts: new Date().toISOString(),
          }));
          return new Response(JSON.stringify({ ok: true }),
            { headers: { "Content-Type": "application/json" } });
        }
        if (url0.pathname === "/api/leadflags" && request.method === "GET") {
          if (!env.FLAGS) return new Response("{}", { headers: { "Content-Type": "application/json" } });
          const out = {};
          const list = await env.FLAGS.list({ prefix: "lead:" });
          for (const k of list.keys) {
            const v = await env.FLAGS.get(k.name);
            if (v) out[k.name.slice(5)] = JSON.parse(v);
          }
          return new Response(JSON.stringify(out, null, 1),
            { headers: { "Content-Type": "application/json" } });
        }
        if (url0.pathname === "/api/waflag" && request.method === "POST") {
          if (!env.FLAGS) return new Response("{}", { headers: { "Content-Type": "application/json" } });
          let d = {};
          try { d = await request.json(); } catch {}
          const ph = String(d.phone || "").replace(/\D/g, "");
          if (ph.length >= 10 && ph.length <= 15) {
            await env.FLAGS.put("wa:" + ph, "0");
            return new Response('{"ok":true}', { headers: { "Content-Type": "application/json" } });
          }
          return new Response('{"error":"phone required"}', { status: 400, headers: { "Content-Type": "application/json" } });
        }
        if (url0.pathname === "/api/waflags" && request.method === "GET") {
          if (!env.FLAGS) return new Response("{}", { headers: { "Content-Type": "application/json" } });
          const out = {};
          const list = await env.FLAGS.list({ prefix: "wa:" });
          for (const k of list.keys) out[k.name.slice(3)] = false;
          return new Response(JSON.stringify(out), { headers: { "Content-Type": "application/json" } });
        }
        if (url0.pathname === "/api/flags" && request.method === "GET") {
          if (!env.FLAGS) return new Response("{}", { headers: { "Content-Type": "application/json" } });
          const out = {};
          const list = await env.FLAGS.list({ prefix: "flag:" });
          for (const k of list.keys) {
            const v = await env.FLAGS.get(k.name);
            if (v) out[k.name.slice(5)] = JSON.parse(v);
          }
          return new Response(JSON.stringify(out, null, 1),
            { headers: { "Content-Type": "application/json" } });
        }

        const team = (u.team || "else").toLowerCase();
        const url = new URL(request.url);
        let p = url.pathname.replace(/\/+$/, "") || "/";
        if (p.endsWith(".html")) p = p.slice(0, -5) || "/";
        if (p === "/index") p = "/";

        let target;
        if (team === "all") {
          target = OWNER_PATHS.has(p) ? p : "/";
        } else {
          target = TEAM_PATH[team] || "/else";
        }
        if (url.pathname !== target) {
          url.pathname = target;
          return env.ASSETS.fetch(new Request(url.toString(), request));
        }
        return env.ASSETS.fetch(request);
      }
    }
    return unauthorized();
  },
};

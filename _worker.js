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
        const pass = new Set(["/inbox", "/leads", "/ceo", "/logo.png"]);
        if (!pass.has(u0.pathname)) u0.pathname = "/chirag";
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

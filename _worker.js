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
const OWNER_PATHS = new Set(["/", "/sat", "/ap", "/ib", "/igcse", "/myp", "/else"]);

function unauthorized() {
  return new Response("Authentication required.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="AP Guru dashboard", charset="UTF-8"' },
  });
}

export default {
  async fetch(request, env) {
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

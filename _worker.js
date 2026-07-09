// Cloudflare Pages — advanced-mode Worker.
// ONE dashboard URL for everyone; content is filtered by WHO logs in.
//   - team "all"  -> master, and can drill into any team via the path
//   - a team slug -> that team's dashboard only, at EVERY path
//
// Each person's browser only ever receives their own team's HTML, so a team
// member cannot view-source their way into another team's data.
//
// Credentials live in a Cloudflare Pages environment variable named USERS
// (NOT in this repo). USERS is JSON: email -> { "pass": "...", "team": "..." }
// team is one of: all | sat | ap | ib | igcse | myp | else
// Example:
// {
//   "chirag@apguru.com":  {"pass":"...", "team":"all"},
//   "ipshita@apguru.com": {"pass":"...", "team":"sat"}
// }

const TEAM_FILE = {
  all: "/index.html", sat: "/sat.html", ap: "/ap.html",
  ib: "/ib.html", igcse: "/igcse.html", myp: "/myp.html", else: "/else.html",
};
// owner (team "all") can navigate by path:
const OWNER_PATHS = {
  "/": "/index.html", "/index.html": "/index.html",
  "/sat": "/sat.html", "/ap": "/ap.html", "/ib": "/ib.html",
  "/igcse": "/igcse.html", "/myp": "/myp.html", "/else": "/else.html",
};

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
      const decoded = atob(encoded);
      const i = decoded.indexOf(":");
      const email = decoded.slice(0, i).trim().toLowerCase();
      const pass = decoded.slice(i + 1);
      const u = users[email];
      if (u && pass === u.pass) {
        const team = (u.team || "else").toLowerCase();
        let assetPath;
        if (team === "all") {
          let p = new URL(request.url).pathname.replace(/\/+$/, "");
          if (p === "") p = "/";
          assetPath = OWNER_PATHS[p] || "/index.html";
        } else {
          assetPath = TEAM_FILE[team] || "/else.html";
        }
        const url = new URL(request.url);
        url.pathname = assetPath;
        return env.ASSETS.fetch(new Request(url.toString(), request));
      }
    }
    return unauthorized();
  },
};

// PIN gate for solar.pascalhaag.de.
//
// Shows a PIN page, sets a signed cookie and proxies every request to the
// Access-protected origin (solar-origin.*) using an Access service token. The
// origin itself stays locked: cloudflared still requires a valid Access JWT.
//
// Secrets: PIN, COOKIE_KEY, CF_ACCESS_CLIENT_ID, CF_ACCESS_CLIENT_SECRET
// Vars:    ORIGIN_HOST, SESSION_DAYS
// Binding: LOGIN_LIMITER (rate limit for PIN attempts per IP)

const COOKIE = "solar_pin";
const LOGIN_PATH = "/__pin";
const LOGOUT_PATH = "/__pin/logout";

const enc = new TextEncoder();

async function hmac(key, msg) {
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, enc.encode(msg));
  return btoa(String.fromCharCode(...new Uint8Array(sig))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Constant-time string comparison.
function safeEqual(a, b) {
  const x = enc.encode(a), y = enc.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

// The PIN is part of the signed message, so changing the PIN ends all sessions.
async function sign(env, exp) {
  return `${exp}.${await hmac(env.COOKIE_KEY, `${exp}|${env.PIN}`)}`;
}

async function hasSession(request, env) {
  const m = (request.headers.get("Cookie") || "").match(/(?:^|;\s*)solar_pin=([^;]+)/);
  if (!m) return false;
  const [exp] = m[1].split(".");
  if (!/^\d+$/.test(exp) || Number(exp) < Date.now() / 1000) return false;
  return safeEqual(m[1], await sign(env, exp));
}

function safeNext(next) {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith(LOGIN_PATH) ? next : "/";
}

function loginPage(next, error, status = 200) {
  const esc = (s) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const html = `<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Solar</title>
<style>
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--muted:#898781;--ring:rgba(11,11,11,.10);--bad:#d03b3b;--accent:#eb6834;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--muted:#8f8d86;--ring:rgba(240,239,236,.12)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--page);color:var(--ink);font:16px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
form{width:100%;max-width:320px;background:var(--surface);border:1px solid var(--ring);border-radius:14px;padding:28px 24px;display:grid;gap:14px}
h1{margin:0;font-size:1.25rem;display:flex;align-items:center;gap:8px}
h1 span{width:12px;height:12px;border-radius:50%;background:var(--accent)}
p{margin:0;color:var(--muted);font-size:.9rem}
input{font:inherit;font-size:1.5rem;letter-spacing:.3em;text-align:center;padding:10px;border-radius:10px;border:1px solid var(--ring);background:var(--page);color:var(--ink);width:100%}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{font:inherit;font-weight:600;padding:10px;border:0;border-radius:10px;background:var(--ink);color:var(--page);cursor:pointer}
.err{color:var(--bad)}
</style></head><body>
<form method="post" action="${LOGIN_PATH}">
<h1><span></span>Solar</h1>
<p>Bitte PIN eingeben.</p>
<input name="pin" type="password" inputmode="numeric" autocomplete="current-password" autofocus required aria-label="PIN">
<input type="hidden" name="next" value="${esc(next)}">
${error ? `<p class="err">${esc(error)}</p>` : ""}
<button type="submit">Öffnen</button>
</form></body></html>`;
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Frame-Options": "DENY" },
  });
}

async function handleLogin(request, env) {
  const url = new URL(request.url);
  if (request.method !== "POST") return loginPage(safeNext(url.searchParams.get("next")), "");

  const form = await request.formData();
  const next = safeNext(String(form.get("next") || "/"));
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const { success } = await env.LOGIN_LIMITER.limit({ key: ip });
  if (!success) return loginPage(next, "Zu viele Versuche – bitte eine Minute warten.", 429);

  if (!safeEqual(String(form.get("pin") || ""), env.PIN)) return loginPage(next, "Falsche PIN.", 401);

  const maxAge = Number(env.SESSION_DAYS || 30) * 86400;
  const value = await sign(env, Math.floor(Date.now() / 1000) + maxAge);
  return new Response(null, {
    status: 303,
    headers: {
      Location: next,
      "Set-Cookie": `${COOKIE}=${value}; Max-Age=${maxAge}; Path=/; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}

async function proxy(request, env) {
  const url = new URL(request.url);
  const publicHost = url.host;
  url.host = env.ORIGIN_HOST;

  const headers = new Headers(request.headers);
  headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
  headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);
  // Don't forward our own cookie to the origin.
  const cookies = (headers.get("Cookie") || "").split(/;\s*/).filter((c) => c && !c.startsWith(`${COOKIE}=`));
  cookies.length ? headers.set("Cookie", cookies.join("; ")) : headers.delete("Cookie");

  const upstream = await fetch(url, { method: request.method, headers, body: request.body, redirect: "manual" });

  const out = new Response(upstream.body, upstream);
  const loc = out.headers.get("Location");
  if (loc) out.headers.set("Location", loc.replace(`//${env.ORIGIN_HOST}`, `//${publicHost}`));
  // Access may hand out its own session cookie for the origin host; keep it off the public host.
  out.headers.delete("Set-Cookie");
  return out;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === LOGOUT_PATH) {
      return new Response(null, {
        status: 303,
        headers: { Location: LOGIN_PATH, "Set-Cookie": `${COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax` },
      });
    }
    if (url.pathname === LOGIN_PATH) return handleLogin(request, env);

    if (!(await hasSession(request, env))) {
      // API calls get a plain 401 so the dashboard JS doesn't parse an HTML page.
      if (url.pathname.startsWith("/api/")) return new Response("PIN required", { status: 401 });
      return Response.redirect(`${url.origin}${LOGIN_PATH}?next=${encodeURIComponent(url.pathname + url.search)}`, 303);
    }
    return proxy(request, env);
  },
};

# PIN gate (Cloudflare Worker)

`solar.pascalhaag.de` is served by this Worker. It asks for a shared PIN, sets a
signed `solar_pin` cookie (30 days) and proxies every request to the origin.

```
browser ──► solar.pascalhaag.de (Worker: PIN + cookie)
              │  CF-Access-Client-Id / -Secret (service token)
              ▼
            solar-origin.pascalhaag.de (Access app "Solar (Origin)")
              │  Access JWT, checked again by cloudflared (audTag)
              ▼
            tunnel solar-web ──► web:8080 in CT 132
```

- The origin stays behind Cloudflare Access. Policies: e-mail one-time PIN for
  Pascal (fallback login directly on `solar-origin.*`) and the service token
  `solar-pin-worker` for this Worker.
- PIN attempts are rate limited to 5 per minute per IP (`LOGIN_LIMITER`).
- `/__pin/logout` clears the cookie.

## Change the PIN

```sh
cd worker
npx wrangler secret put PIN
```

The PIN is part of the cookie signature, so changing it logs everyone out.

## Deploy

```sh
npx wrangler deploy
```

Secrets: `PIN`, `COOKIE_KEY` (random, signs cookies), `CF_ACCESS_CLIENT_ID`,
`CF_ACCESS_CLIENT_SECRET` (service token `solar-pin-worker`).

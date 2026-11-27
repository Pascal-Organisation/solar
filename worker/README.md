# Passphrase gate (Cloudflare Worker)

`solar.pascalhaag.de` is served by this Worker. It asks for a shared passphrase, sets
a signed `solar_session` cookie (30 days) and proxies every request to the origin.

```
browser ──► solar.pascalhaag.de (Worker: passphrase + cookie)
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
- Login attempts are rate limited to 5 per minute per IP (`LOGIN_LIMITER`).
- `/__login/logout` clears the cookie.
- Fails closed: if `PASSPHRASE` is shorter than 12 characters or `COOKIE_KEY` is
  shorter than 32, nobody gets in (login answers 503).
- Leading/trailing spaces are ignored and Unicode is NFC-normalised, so umlauts
  typed on different devices match.

## Set or change the passphrase

```sh
cd worker
npx wrangler secret put PASSPHRASE   # prompts for the value; nothing lands in the repo
```

Use at least 12 characters; 4+ random words are easy to type on a phone. The
passphrase is part of the cookie signature, so changing it logs everyone out.

The Worker keeps its name `solar-pin` so the existing deployment and route stay in
place. After switching from the old PIN, remove the old secret:
`npx wrangler secret delete PIN`.

## Deploy

```sh
npx wrangler deploy
```

Secrets: `PASSPHRASE`, `COOKIE_KEY` (random, signs cookies), `CF_ACCESS_CLIENT_ID`,
`CF_ACCESS_CLIENT_SECRET` (service token `solar-pin-worker`).

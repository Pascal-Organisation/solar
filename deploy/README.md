# Deployment

Target: an unprivileged Debian 13 LXC on Proxmox with Docker, exposed only through
a Cloudflare Tunnel behind Cloudflare Access.

## 1. Container (on the Proxmox host)

```bash
pct create <id> local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname solar --unprivileged 1 --features nesting=1,keyctl=1 \
  --cores 1 --memory 768 --swap 256 --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=<ip>/24,gw=<gw>,firewall=1 \
  --onboot 1 --tags solar
```

Proxmox firewall for the CT (`/etc/pve/firewall/<id>.fw`): default-deny inbound,
allow only Prometheus to `:9108`. Outbound stays open (inverter UDP, Cloudflare,
GHCR, Sigstore).

## 2. Host setup (inside the CT)

```bash
apt install docker.io docker-compose cosign   # or Docker's apt repo
install -d -m 0755 /srv/solar
install -d -o 10001 -g 10001 -m 0750 /srv/solar/data
install -d -m 0700 /etc/solar
install -m 0600 env.example /etc/solar/env                 # then edit
install -m 0644 cloudflared.example.yml /etc/solar/cloudflared.yml   # then edit
# cloudflared runs as 65532 and must read its token
install -o 65532 -g 65532 -m 0400 /dev/stdin /etc/solar/tunnel_token <<< "<token>"
install -m 0755 solar-deploy.sh solar-backup.sh /usr/local/bin/
install -m 0644 systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now solar-deploy.timer solar-backup.timer
systemctl start solar-deploy.service      # first deploy
```

## 3. Cloudflare

- Tunnel (token-based) with local ingress `/etc/solar/cloudflared.yml`.
- DNS: `CNAME solar → <tunnel-id>.cfargotunnel.com` (proxied).
- Access application for the hostname with an e-mail one-time-PIN policy; put its
  AUD tag into the ingress `audTag`.

## 4. History import from SEMS (one-off)

Credentials are passed only to this run and never stored:

```bash
cd /srv/solar
read -r -p "SEMS account: " SEMS_ACCOUNT; read -r -s -p "SEMS password: " SEMS_PASSWORD; echo
export SEMS_ACCOUNT SEMS_PASSWORD
RUN=(docker compose --project-name solar --env-file image.env run --rm -e SEMS_ACCOUNT -e SEMS_PASSWORD collector)

# 1) compare one SEMS day with a locally measured day (units and sign conventions)
"${RUN[@]}" sems-probe --date "$(date -d yesterday +%F)"
# 2) full backfill: daily totals, then 5-minute curves (resumable, ~5 s per request)
"${RUN[@]}" sems-import
unset SEMS_ACCOUNT SEMS_PASSWORD
```

If the probe shows inverted battery or grid signs, set `SEMS_BATTERY_SIGN` /
`SEMS_GRID_SIGN` (`1` or `-1`) for the import run. `SEMS_POWER_SCALE` overrides
the automatic kW/W detection.

## Operations

```bash
journalctl -u solar-deploy -n 50           # deploy history
docker compose --project-name solar -f /srv/solar/docker-compose.yml --env-file /srv/solar/image.env ps
curl -s localhost:9108/metrics | grep goodwe_ppv
```

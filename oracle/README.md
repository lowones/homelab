# Cloudflare DDNS (oracle)

Keeps `home.low-ones.com` pointed at this site's dynamic Frontier WAN IP.
Runs on **oracle** (`192.168.0.240`, Debian 13) under a systemd timer.

`service.low-ones.com` stays pinned to the static VPS (`45.4.172.124`);
this job only maintains the separate `home` record.

## Components

| File | Installed path | Purpose |
|------|----------------|---------|
| `ddns-cloudflare.sh` | `/usr/local/bin/ddns-cloudflare.sh` | Update logic |
| `ddns-cloudflare.service` | `/etc/systemd/system/` | oneshot runner |
| `ddns-cloudflare.timer` | `/etc/systemd/system/` | every 5 min |
| *(env file)* | `/etc/ddns-cloudflare.env` | **not in git** — see below |

## How it works

1. Logs in to Vault via AppRole (`role_id` + `secret_id`) -> short-lived token.
2. Reads `homelab/cloudflare` (read-only for this role).
3. Gets current WAN IP from `cloudflare.com/cdn-cgi/trace`.
4. Compares against the live Cloudflare record; PATCHes only on change.
5. Pings healthchecks.io on success (dead-man's switch).

No secrets live in the script or the repo. The only on-disk secret is the
AppRole pair in the root-owned `0600` env file.

## Vault setup (run once, from batcomputer)

    vault auth enable approle   # if not already enabled

    vault policy write ddns-cloudflare - <<'POLICY'
    path "homelab/data/cloudflare" {
      capabilities = ["read"]
    }
    POLICY

    vault write auth/approle/role/oracle-ddns \
      token_policies="ddns-cloudflare" \
      token_ttl=15m token_max_ttl=30m \
      secret_id_ttl=0 secret_id_num_uses=0

    vault read auth/approle/role/oracle-ddns/role-id
    vault write -f auth/approle/role/oracle-ddns/secret-id

Vault secret `homelab/cloudflare` holds: `api_token`, `zone`, `zone_id`,
`record`, `record_id`.

## Env file shape (`/etc/ddns-cloudflare.env`, root:root, 0600)

    VAULT_ADDR=http://192.168.0.251:8200
    VAULT_ROLE_ID=<from role-id>
    VAULT_SECRET_ID=<from secret-id>
    HEALTHCHECK_URL=https://hc-ping.com/<uuid>

Regenerate a fresh `secret_id` any time:
`vault write -f auth/approle/role/oracle-ddns/secret-id`

## Install

    sudo apt install -y jq
    # copy the three files to their paths (see table), then:
    sudo chmod 755 /usr/local/bin/ddns-cloudflare.sh
    sudo chmod 600 /etc/ddns-cloudflare.env
    sudo systemctl daemon-reload
    sudo systemctl enable --now ddns-cloudflare.timer

## Verify

    systemctl list-timers ddns-cloudflare.timer
    sudo systemctl start ddns-cloudflare.service
    sudo journalctl -u ddns-cloudflare.service -n 5

Force the update path with a throwaway IP (192.0.2.1 = reserved doc range):
PATCH the record to 192.0.2.1 via the CF API, then start the service.
Expect: `updated: home.low-ones.com 192.0.2.1 -> <wan ip>`

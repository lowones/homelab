#!/usr/bin/env bash
#
# ddns-cloudflare.sh
# Updates home.low-ones.com to the host's current WAN IP via the Cloudflare API.
# Secrets are fetched at runtime from Vault using an AppRole scoped to
# read-only access on homelab/cloudflare. No secrets are stored in this file.
#
# Runs on oracle (192.168.0.240) under a systemd timer.
# Repo: lowones/homelab under oracle/

set -euo pipefail

: "${VAULT_ADDR:?VAULT_ADDR not set}"
: "${VAULT_ROLE_ID:?VAULT_ROLE_ID not set}"
: "${VAULT_SECRET_ID:?VAULT_SECRET_ID not set}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"

VAULT_SECRET_PATH="homelab/data/cloudflare"
LOG_PREFIX="ddns-cloudflare"

log() { echo "[$LOG_PREFIX] $*"; }
die() { echo "[$LOG_PREFIX] ERROR: $*" >&2; exit 1; }

vault_token=$(curl -sf -X POST \
  --data "{\"role_id\":\"${VAULT_ROLE_ID}\",\"secret_id\":\"${VAULT_SECRET_ID}\"}" \
  "${VAULT_ADDR}/v1/auth/approle/login" \
  | jq -r '.auth.client_token') \
  || die "Vault AppRole login failed"
[ -n "$vault_token" ] && [ "$vault_token" != "null" ] || die "Vault returned no token"

secret_json=$(curl -sf \
  -H "X-Vault-Token: ${vault_token}" \
  "${VAULT_ADDR}/v1/${VAULT_SECRET_PATH}") \
  || die "Vault secret read failed"

cf_token=$(echo "$secret_json"  | jq -r '.data.data.api_token')
zone_id=$(echo "$secret_json"   | jq -r '.data.data.zone_id')
record_id=$(echo "$secret_json" | jq -r '.data.data.record_id')
record=$(echo "$secret_json"    | jq -r '.data.data.record')

for v in cf_token zone_id record_id record; do
  val="${!v}"
  [ -n "$val" ] && [ "$val" != "null" ] || die "missing '$v' in Vault secret"
done

current_ip=$(curl -sf4 https://cloudflare.com/cdn-cgi/trace | awk -F= '/^ip=/{print $2}') \
  || die "could not determine WAN IP"
[[ "$current_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "WAN IP looks invalid: '$current_ip'"

cf_get=$(curl -sf \
  -H "Authorization: Bearer ${cf_token}" \
  "https://api.cloudflare.com/client/v4/zones/${zone_id}/dns_records/${record_id}") \
  || die "Cloudflare record read failed"
dns_ip=$(echo "$cf_get" | jq -r '.result.content')

if [ "$current_ip" = "$dns_ip" ]; then
  log "no change: ${record} = ${current_ip}"
else
  update=$(curl -sf -X PATCH \
    -H "Authorization: Bearer ${cf_token}" \
    -H "Content-Type: application/json" \
    --data "{\"content\":\"${current_ip}\"}" \
    "https://api.cloudflare.com/client/v4/zones/${zone_id}/dns_records/${record_id}") \
    || die "Cloudflare PATCH failed"
  ok=$(echo "$update" | jq -r '.success')
  [ "$ok" = "true" ] || die "Cloudflare PATCH returned success=false: $(echo "$update" | jq -c '.errors')"
  log "updated: ${record} ${dns_ip} -> ${current_ip}"
fi

if [ -n "$HEALTHCHECK_URL" ]; then
  curl -sf -m 10 "$HEALTHCHECK_URL" >/dev/null 2>&1 || log "warning: healthcheck ping failed"
fi

exit 0

#!/bin/sh
set -eu
# Only the preauth proxy and this ingress hold this deployment secret.
# It is not a user credential and must never reach browser code or logs.
if ! printf '%s' "${HUB_PROXY_SECRET:-}" | grep -Eq '^[A-Za-z0-9_-]{43,128}$'; then
  echo 'A generated HUB_PROXY_SECRET (43-128 URL-safe characters) is required.' >&2
  exit 1
fi
envsubst '${HUB_PROXY_SECRET}' < /etc/hub/nginx.conf.template > /tmp/nginx.conf
nginx -t -q -c /tmp/nginx.conf
exec nginx -c /tmp/nginx.conf -g 'daemon off;'

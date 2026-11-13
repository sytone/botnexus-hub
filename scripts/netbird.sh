#!/bin/sh
set -eu
: "${NB_MANAGEMENT_URL:?Set your NetBird management URL}"
if [ ! -f /var/lib/netbird/state.json ] && [ ! -f /var/lib/netbird/default.json ]; then
  if [ -z "${NB_SETUP_KEY:-}" ]; then
    echo 'Hub mesh: no setup key or identity; local service remains available.'
    exec tail -f /dev/null
  fi
  echo 'Hub mesh: registering new dedicated peer.'
  exec netbird up --setup-key "$NB_SETUP_KEY" --hostname "$NB_HOSTNAME" --management-url "$NB_MANAGEMENT_URL" --foreground-mode --log-level info
fi
echo 'Hub mesh: reconnecting persisted identity, without setup key.'
exec netbird up --hostname "$NB_HOSTNAME" --management-url "$NB_MANAGEMENT_URL" --foreground-mode --log-level info

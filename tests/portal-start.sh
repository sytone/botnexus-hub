#!/bin/sh
set -eu
resolver=$(awk '/^nameserver / { print $2; exit }' /etc/resolv.conf)
case "$resolver" in ''|*[!0-9a-fA-F.:]*) echo 'Invalid container resolver' >&2; exit 1 ;; esac
sed "s/__DNS__/$resolver/g" /etc/nginx/nginx.template > /tmp/nginx.conf
exec nginx -c /tmp/nginx.conf -g 'daemon off;'

#!/bin/sh
set -eu
mkdir -p /world/home /world/data
if [ ! -f /world/home/config.json ]; then
  cp /seed/config.json /world/home/config.json
fi
exec dotnet /app/BotNexus.Gateway.Api.dll --BotNexus:ConfigPath=/world/home/config.json

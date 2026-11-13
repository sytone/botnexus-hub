# Isolated test worlds only. Never references a host world or any host credential.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
ARG BOTNEXUS_REF=04052bbe283ac9106277a841b8d4c60a5c3ce095
WORKDIR /src
ADD https://codeload.github.com/Sytone/botnexus/tar.gz/${BOTNEXUS_REF} /tmp/platform.tgz
RUN echo '1ecc92bd870241c14c53ed4b4291d2aa113ee0c072e56615273915ba1b0343d4  /tmp/platform.tgz' | sha256sum -c - && tar -xzf /tmp/platform.tgz --strip-components=1 -C /src && rm /tmp/platform.tgz
RUN dotnet publish src/gateway/BotNexus.Gateway.Api/BotNexus.Gateway.Api.csproj -c Release -o /opt/gateway -p:BotNexusSourceRevisionId=${BOTNEXUS_REF} --nologo
# Include the real portal so handoff links can be exercised end-to-end.
RUN dotnet publish src/extensions/BotNexus.Extensions.Channels.SignalR/BotNexus.Extensions.Channels.SignalR.csproj -c Release -o /opt/extensions/botnexus-signalr --nologo
FROM mcr.microsoft.com/dotnet/aspnet:10.0
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /opt/gateway/ ./
COPY --from=build /opt/extensions/ /app/extensions/
ENV BOTNEXUS_HOME=/world/home BOTNEXUS_DATA_DIR=/world/data BOTNEXUS_EXTENSIONS_PATH=/app/extensions ASPNETCORE_ENVIRONMENT=Production ASPNETCORE_URLS=http://+:5000
EXPOSE 5000
ENTRYPOINT ["dotnet", "/app/BotNexus.Gateway.Api.dll", "--BotNexus:ConfigPath=/world/home/config.json"]

FROM localhost/botnexushub-world-base:04052bbe AS world
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS connector
WORKDIR /build
COPY --from=world /app/ /sdk/
COPY VERSION ./VERSION
COPY src/ ./src/
RUN dotnet publish src/BotNexus.Extensions.HubConnector/BotNexus.Extensions.HubConnector.csproj -c Release -o /connector -p:BotNexusBinaryDir=/sdk --nologo
FROM world
COPY --from=connector /connector/ /app/extensions/botnexus-hub-connector/

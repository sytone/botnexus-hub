[CmdletBinding()]
param([Parameter(Mandatory)][string]$BotNexusBinaryDir,[string]$Python='python')
$ErrorActionPreference='Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
 & $Python -m pytest tests -q
 if($LASTEXITCODE){throw 'Python tests failed'}
 $suites=@(Get-ChildItem tests -Filter '*.test.cjs' -File |ForEach-Object FullName)
 if(-not $suites.Count){throw 'No JavaScript suites found'}
 & node --test @suites
 if($LASTEXITCODE){throw 'JavaScript tests failed'}
 & dotnet run --project tests/Connector.Tests/Connector.Tests.csproj -c Release "-p:BotNexusBinaryDir=$BotNexusBinaryDir"
 if($LASTEXITCODE){throw 'Connector tests failed'}
 & git diff --check
 if($LASTEXITCODE){throw 'Whitespace errors'}
} finally {Pop-Location}

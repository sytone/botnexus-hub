<#
.SYNOPSIS
Deploy this project's clean canonical main checkout with Docker or Podman Compose.
.DESCRIPTION
This is a portable project helper, not a host-policy bypass. On a managed host,
call it through that host's separately approved deployment procedure.

The project is always botnexushub. -TestWorlds enables synthetic fixtures;
-NetBird enables the optional mesh sidecar. Existing NetBird deployments require
-NetBird on each run. -Build builds only hub and, with -TestWorlds, world-a.
The separately prepared, pinned test-world base image must already be available.

Both ingress (nginx) and NetBird share the Hub network namespace. Only the exact
project's verified sidecars are removed before replacement, without --volumes.
Persistent botnexushub_hub-data, botnexushub_world-a-state,
botnexushub_world-b-state and botnexushub_netbird-state volumes are never deleted.
.LINK
https://github.com/sytone/botnexus-hub
#>
[CmdletBinding()]
param(
    [ValidateSet('Auto', 'Docker', 'Podman')]
    [string]$Engine = 'Auto',
    [switch]$Build,
    [switch]$TestWorlds,
    [switch]$NetBird
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Get-Item -LiteralPath (Split-Path $PSScriptRoot -Parent)).FullName
$project = 'botnexushub'

function Invoke-SourceGit {
    param([string[]]$Arguments)
    $output = @(& git -C $root @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the deployment checkout with Git' }
    return ($output -join "`n").Trim()
}

# A branch called main in a linked worktree is not the canonical checkout.
$top = Invoke-SourceGit @('rev-parse', '--show-toplevel')
$gitDirectory = Invoke-SourceGit @('rev-parse', '--path-format=absolute', '--git-dir')
$commonDirectory = Invoke-SourceGit @('rev-parse', '--path-format=absolute', '--git-common-dir')
$dotGit = Join-Path $root '.git'
if (-not (Test-Path -LiteralPath $dotGit -PathType Container)) {
    throw 'Deploy only from the canonical checkout, never an alternate worktree'
}
if ((Get-Item -LiteralPath $top).FullName -ne $root -or
    (Get-Item -LiteralPath $gitDirectory).FullName -ne (Get-Item -LiteralPath $commonDirectory).FullName -or
    (Get-Item -LiteralPath $commonDirectory).FullName -ne (Get-Item -LiteralPath $dotGit).FullName) {
    throw 'Git common directory must be this canonical checkout''s .git directory'
}
if ((Invoke-SourceGit @('branch', '--show-current')) -cne 'main' -or
    (Invoke-SourceGit @('status', '--porcelain', '--untracked-files=normal'))) {
    throw 'Deploy only clean canonical main; commit and review changes first'
}
$commit = Invoke-SourceGit @('rev-parse', 'HEAD')
if ($commit -notmatch '^[a-f0-9]{40,64}$') { throw 'Cannot determine the deployment revision' }
if (-not (Test-Path -LiteralPath (Join-Path $root '.env') -PathType Leaf)) {
    throw 'Create the private canonical .env with scripts/configure.py before deployment'
}

$candidates = if ($Engine -eq 'Auto') { @('docker', 'podman') } else { @($Engine.ToLowerInvariant()) }
$engineCommand = $null
foreach ($candidate in $candidates) {
    $engineCommand = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($engineCommand) { break }
}
if (-not $engineCommand) { throw 'Install the selected Docker or Podman engine and its Compose provider' }
$enginePath = $engineCommand.Source
$composeArguments = @('compose', '--project-name', $project, '--project-directory', $root,
                      '--file', (Join-Path $root 'compose.yaml'), '--env-file', (Join-Path $root '.env'))
$services = @('hub', 'ingress')
if ($TestWorlds) {
    $composeArguments += @('--profile', 'test')
    $services += @('mock-provider', 'world-a', 'world-b', 'test-portals')
}
if ($NetBird) {
    $composeArguments += @('--profile', 'netbird')
    $services += 'netbird'
}

function Invoke-ProjectCompose {
    param([string[]]$Arguments)
    # Capture diagnostics: provider errors can include interpolated environment
    # values. In particular, never render a resolved Compose config to the host.
    $null = & $enginePath @composeArguments @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw 'Project Compose operation failed; inspect it privately without dumping resolved secrets'
    }
}

function Get-ProjectContainer {
    param([string]$Name)
    # List only an exact fixed project name, then inspect without merging stderr
    # into JSON. A failed engine query is not treated as an absent container.
    $listed = @(& $enginePath container ls --all --filter ('name=^' + $Name + '$') --format '{{.Names}}' 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot list project containers' }
    if ($listed.Count -eq 0) { return $null }
    if ($listed.Count -ne 1 -or $listed[0] -cne $Name) { throw 'Ambiguous project container name' }
    $raw = @(& $enginePath container inspect $Name 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the project container' }
    try { $items = @(($raw -join "`n") | ConvertFrom-Json) }
    catch { throw 'Invalid project container inspection' }
    if ($items.Count -ne 1 -or $items[0].Name.TrimStart('/') -cne $Name -or
        $items[0].Id -notmatch '^[a-f0-9]{64}$') { throw 'Unexpected project container identity' }
    return $items[0]
}

function Assert-ComposeLabel {
    param($Item, [string]$Key, [string]$Expected)
    $found = $false
    foreach ($prefix in @('com.docker.compose.', 'io.podman.compose.')) {
        $property = $Item.Config.Labels.PSObject.Properties[$prefix + $Key]
        if ($property) {
            $found = $true
            if ($property.Value -cne $Expected) { throw 'Foreign Compose container; refusing deployment' }
        }
    }
    if (-not $found) { throw 'Missing Compose ownership label; refusing deployment' }
}

function Assert-StateVolume {
    param($Item, [string]$Destination, [string]$Name)
    $mounts = @($Item.Mounts | Where-Object { $_.Destination -ceq $Destination })
    if ($mounts.Count -ne 1 -or $mounts[0].Type -cne 'volume' -or
        $mounts[0].Name -cne $Name -or $mounts[0].RW -ne $true) {
        throw 'Unexpected project state volume; refusing deployment'
    }
}

function Assert-ReadOnlyBind {
    param($Mount, [string]$Suffix)
    if ($Mount.Type -cne 'bind' -or $Mount.RW -ne $false -or
        -not $Mount.Source.Replace('\', '/').EndsWith($Suffix, [StringComparison]::Ordinal)) {
        throw 'Unexpected sidecar configuration mount; refusing deployment'
    }
}

function Assert-NamespaceSidecar {
    param($Item, $Hub, [string]$Service)
    Assert-ComposeLabel $Item 'project' 'botnexushub'
    Assert-ComposeLabel $Item 'service' $Service
    if (-not $Hub -or $Item.HostConfig.NetworkMode -cnotin @('container:' + $Hub.Id, 'container:botnexushub')) {
        throw 'Sidecar does not share the approved Hub network namespace'
    }
    if ($Service -eq 'netbird') {
        Assert-StateVolume $Item '/var/lib/netbird' 'botnexushub_netbird-state'
        foreach ($mount in $Item.Mounts) {
            if ($mount.Destination -ceq '/var/lib/netbird') { continue }
            if ($mount.Destination -cne '/hub-netbird.sh') { throw 'Unexpected NetBird sidecar mount' }
            Assert-ReadOnlyBind $mount '/scripts/netbird.sh'
        }
    } else {
        $expected = @{'/etc/hub/nginx.conf.template' = '/ingress/nginx.conf.template';
                      '/etc/hub/start.sh' = '/ingress/start.sh'}
        if (@($Item.Mounts).Count -ne $expected.Count) { throw 'Unexpected ingress mount count' }
        foreach ($mount in $Item.Mounts) {
            if (-not $expected.ContainsKey($mount.Destination)) { throw 'Unexpected ingress sidecar mount' }
            Assert-ReadOnlyBind $mount $expected[$mount.Destination]
            $expected.Remove($mount.Destination)
        }
    }
}

$previousCommit = [Environment]::GetEnvironmentVariable('HUB_COMMIT', 'Process')
try {
    $env:HUB_COMMIT = $commit
    Invoke-ProjectCompose @('config', '--quiet')
    $hub = Get-ProjectContainer 'botnexushub'
    if ($hub) {
        Assert-ComposeLabel $hub 'project' 'botnexushub'
        Assert-ComposeLabel $hub 'service' 'hub'
        Assert-StateVolume $hub '/data' 'botnexushub_hub-data'
        if (@($hub.Mounts).Count -ne 1) { throw 'Unexpected Hub mounts; refusing deployment' }
    }
    $sidecars = @()
    foreach ($service in @('ingress', 'netbird')) {
        $item = Get-ProjectContainer ('botnexushub-' + $service)
        if ($item) {
            Assert-NamespaceSidecar $item $hub $service
            if ($service -eq 'netbird' -and -not $NetBird) {
                throw 'An existing NetBird sidecar requires -NetBird so it is recreated after deployment'
            }
            $sidecars += $item
        }
    }
    # Build first so a build failure leaves the existing namespace intact.
    if ($Build) {
        $buildServices = @('hub')
        if ($TestWorlds) { $buildServices += 'world-a' }
        Invoke-ProjectCompose (@('build') + $buildServices)
    }
    foreach ($sidecar in $sidecars) {
        $fresh = Get-ProjectContainer $sidecar.Name.TrimStart('/')
        if (-not $fresh -or $fresh.Id -cne $sidecar.Id) { throw 'Sidecar identity changed during deployment' }
        $service = if ($fresh.Name.TrimStart('/') -eq 'botnexushub-netbird') { 'netbird' } else { 'ingress' }
        Assert-NamespaceSidecar $fresh $hub $service
        $null = & $enginePath stop --time 10 $sidecar.Id 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Project sidecar stop failed' }
        $null = & $enginePath rm $sidecar.Id 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Project sidecar removal failed; persistent volumes were not removed' }
    }
    # Explicit services prevent ambient profiles from starting unrelated work.
    # Do not use down, --remove-orphans, --volumes or a global image prune.
    Invoke-ProjectCompose (@('up', '--detach', '--no-build') + $services)
    Write-Output 'Project deployment completed. Verify ingress health and externally authenticated access.'
} finally {
    [Environment]::SetEnvironmentVariable('HUB_COMMIT', $previousCommit, 'Process')
}

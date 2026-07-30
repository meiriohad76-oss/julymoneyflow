<#
.SYNOPSIS
    Push the dashboard to a Raspberry Pi and install it, in one command.

.DESCRIPTION
    Uses only ssh/scp/tar, all of which ship with Windows 10 1803+. No rsync,
    no WSL, nothing to install.

    The price cache is copied by DEFAULT. That matters twice over: it saves the
    Pi a 15-40 minute cold fetch, and it saves the Polygon API quota that fetch
    would spend. Skip it with -NoCache only if the Pi already has a cache.

    THIS FILE IS DELIBERATELY PURE ASCII. Windows PowerShell 5.1 (the default on
    Windows) reads .ps1 files as Windows-1252 unless they carry a UTF-8 BOM, so
    a stray em-dash becomes three bytes of mojibake. When that lands inside a
    quoted string it breaks the parser and cascades through the whole file --
    which is exactly what happened. Keep this file 7-bit.

.EXAMPLE
    .\deploy\push-to-pi.ps1 -Pi pi@raspberrypi.local

.EXAMPLE
    .\deploy\push-to-pi.ps1 -Pi pi@192.168.1.50 -NoCache
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Pi,                       # user@host

    [switch]$NoCache,                  # skip the ~19 MB price cache
    [switch]$SkipInstall               # copy only, run the installer yourself
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Say($m)  { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  ! $m"  -ForegroundColor Yellow }

# StrictHostKeyChecking=accept-new trusts a host key the FIRST time it is seen,
# exactly as typing "yes" at the prompt does, but still refuses if a known key
# later changes. That keeps the protection that actually matters (detecting a
# swapped host) while not dead-ending on a first connection. Plain `no` would
# discard both, so it is not used here.
$SSH = @('-o', 'StrictHostKeyChecking=accept-new')

# PowerShell 5.1 turns ANY stderr output from a native command into a
# terminating error when $ErrorActionPreference is 'Stop' -- even when the
# command succeeded. ssh writes ordinary notices to stderr, so every call has to
# run with that relaxed and be judged on its exit code instead.
# NOTE the parameter name. `$Args` is a PowerShell AUTOMATIC variable holding a
# function's unbound arguments; declaring a parameter with that name collides
# with it and the splat silently expands to nothing. That produced an ssh usage
# dump with no destination, which reads like a connection failure but is not.
# Never name a parameter $Args.
function Invoke-Native {
    param([string]$Exe, [string[]]$ArgList)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @ArgList 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{
            Code   = $LASTEXITCODE
            Output = ($out -join "`n").Trim()
        }
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Invoke-Ssh {
    param([string[]]$ArgList)
    Invoke-Native -Exe 'ssh' -ArgList ($SSH + $ArgList)
}

# --- preflight ---------------------------------------------------------------
Say "Checking local tools"
foreach ($t in 'ssh', 'scp', 'tar') {
    if (-not (Get-Command $t -ErrorAction SilentlyContinue)) {
        throw "$t not found. Windows 10 1803+ ships all three; check Settings > Apps > Optional Features > OpenSSH Client."
    }
    Write-Host "  $t OK"
}

Say "Testing the connection to $Pi"
# First try without a password. BatchMode fails fast rather than hanging on a
# prompt, which tells us whether key auth is set up.
$probe = Invoke-Ssh @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $Pi, 'uname -m')
if ($probe.Code -eq 0) {
    $arch = ($probe.Output -split "`n" | Select-Object -Last 1)
    Write-Host "  connected with a key"
} else {
    if ($probe.Output -match 'Host key verification failed|REMOTE HOST IDENTIFICATION') {
        Warn "The Pi's host key changed since it was last seen. If you reinstalled"
        Warn "the OS that is expected; otherwise stop and investigate. To clear it:"
        Warn "  ssh-keygen -R $($Pi.Split('@')[-1])"
        throw "Refusing to continue with a changed host key."
    }
    Warn "No key-based login. You will be asked for the Pi password at each step."
    Warn "To avoid that: ssh-keygen -t ed25519, then append your public key to"
    Warn "  ~/.ssh/authorized_keys on the Pi."
    $r = Invoke-Ssh @($Pi, 'uname -m')
    if ($r.Code -ne 0) {
        # Distinguish "cannot reach it" from "reached it, credentials rejected".
        # Reporting an auth failure as a connectivity problem sends people to
        # check cables and firewalls when the real answer is the username.
        $u = $Pi.Split('@')[0]
        $h = $Pi.Split('@')[-1]
        if ($r.Output -match 'Permission denied|Authentication failed') {
            Warn "SSH reached the Pi. The credentials were refused."
            Warn ""
            Warn "Most likely the username is not '$u'. Raspberry Pi OS stopped"
            Warn "creating a default 'pi' account in 2022 -- you chose your own"
            Warn "during setup. Run 'whoami' on the Pi (the Raspberry Pi Connect"
            Warn "shell works) and re-run with that name:"
            Warn "    .\deploy\push-to-pi.ps1 -Pi <name>@$h"
            throw "Authentication failed for $Pi."
        }
        if ($r.Output -match 'Connection refused') {
            throw "$h refused the connection on port 22. Enable SSH on the Pi: sudo systemctl enable --now ssh"
        }
        if ($r.Output -match 'No route to host|timed out|Network is unreachable') {
            throw "$h is not reachable. Check the address (hostname -I on the Pi) and that both machines are on the same network."
        }
        Warn $r.Output
        throw "SSH to $Pi failed. See the message above."
    }
    $arch = ($r.Output -split "`n" | Select-Object -Last 1)
}
$arch = "$arch".Trim()
Write-Host "  architecture: $arch"
if ($arch -notmatch 'aarch64|x86_64') {
    Warn "32-bit OS detected ($arch). numpy and pandas have no prebuilt wheels for"
    Warn "it and will compile from source, which takes over an hour on a Pi."
    Warn "64-bit Raspberry Pi OS is strongly recommended."
    if ((Read-Host "  continue anyway? [y/N]") -ne 'y') { exit 1 }
}

# --- copy --------------------------------------------------------------------
$exclude = @(
    '--exclude=.venv', '--exclude=__pycache__', '--exclude=.git',
    '--exclude=output', '--exclude=.env', '--exclude=node_modules'
)
if ($NoCache) { $exclude += '--exclude=data' }

$what = if ($NoCache) { 'without cache' } else { 'with price cache' }
Say "Packing project ($what)"

# Deliberately NOT `tar czf - | ssh`. That idiom is right in bash but wrong here:
# the PowerShell pipeline carries strings, not raw bytes, so it re-encodes the
# gzip stream and delivers a corrupt archive. Write a temp file and scp it.
$tmp = Join-Path $env:TEMP ("smf-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".tgz")
Push-Location $root
try {
    & tar czf $tmp @exclude .
    if ($LASTEXITCODE -ne 0) { throw "tar failed" }
} finally {
    Pop-Location
}
$mb = [math]::Round((Get-Item $tmp).Length / 1MB, 1)
Write-Host "  archive $mb MB"

Say "Copying to the Pi"
$r = Invoke-Ssh @($Pi, 'rm -rf /tmp/smf && mkdir -p /tmp/smf')
if ($r.Code -ne 0) {
    Remove-Item $tmp -Force -EA SilentlyContinue
    Warn $r.Output
    throw "could not prepare /tmp/smf on the Pi"
}

$r = Invoke-Native 'scp' ($SSH + @('-q', $tmp, "${Pi}:/tmp/smf.tgz"))
Remove-Item $tmp -Force -EA SilentlyContinue
if ($r.Code -ne 0) { Warn $r.Output; throw "scp failed" }

$r = Invoke-Ssh @($Pi, 'tar xzf /tmp/smf.tgz -C /tmp/smf && rm -f /tmp/smf.tgz')
if ($r.Code -ne 0) { Warn $r.Output; throw "extract failed on the Pi" }

$r = Invoke-Ssh @($Pi, 'find /tmp/smf -type f | wc -l')
Write-Host "  $($r.Output) files staged in /tmp/smf"

if ($SkipInstall) {
    Say "Copied. Finish with:"
    Write-Host "  ssh $Pi 'sudo bash /tmp/smf/deploy/install-pi.sh'"
    exit 0
}

# --- install -----------------------------------------------------------------
# These two run with -t so sudo can prompt, and are NOT wrapped by
# Invoke-Native: their output must stream to the console live, since the install
# takes minutes and a silent window looks like a hang.
Say "Running the installer (asks for sudo on the Pi)"
$prevEA = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& ssh @SSH '-t' $Pi 'sudo bash /tmp/smf/deploy/install-pi.sh'
$installCode = $LASTEXITCODE
$ErrorActionPreference = $prevEA
if ($installCode -ne 0) { throw "installer failed, see the output above" }

# The installer excludes data/ so it never clobbers an existing cache, which
# means the seed has to be copied in separately.
if (-not $NoCache) {
    Say "Seeding the price cache into /opt/smf/data"
    $seed = 'sudo mkdir -p /opt/smf/data && sudo cp -rn /tmp/smf/data/. /opt/smf/data/ 2>/dev/null; sudo chown -R smf:smf /opt/smf/data; find /opt/smf/data -name "*.csv" | wc -l'
    $ErrorActionPreference = 'Continue'
    $out = & ssh @SSH '-t' $Pi $seed
    $ErrorActionPreference = $prevEA
    Write-Host "  cached CSVs on the Pi: $(($out | Select-Object -Last 1))"
}

Say "Done"
Write-Host ""
Write-Host "Next, on the Pi:"
Write-Host ""
# -t matters on the first two: nano needs a terminal, and sudo needs one to
# prompt for a password. Without it nano opens on a dead input and sudo either
# fails or hangs silently.
Write-Host "  1. Add your Polygon key (-t gives nano a terminal)"
Write-Host "       ssh -t $Pi 'sudo nano /opt/smf/.env'"
Write-Host ""
Write-Host "  2. Build the dashboard. With the cache seeded this makes ZERO API calls:"
Write-Host "       ssh -t $Pi 'sudo -u smf /opt/smf/.venv/bin/python /opt/smf/run.py --offline'"
Write-Host ""
Write-Host "  3. Confirm it serves locally (expect HTTP/1.1 200 OK)"
Write-Host "       ssh $Pi 'curl -sI http://127.0.0.1:8080/ | head -1'"
Write-Host ""
Write-Host "  4. Cloudflare Tunnel and Access: deploy/README.md step 4."
Write-Host "     Nothing is reachable from the internet until you finish it, and the"
Write-Host "     Access policy must exist BEFORE you browse to the hostname."
Write-Host ""

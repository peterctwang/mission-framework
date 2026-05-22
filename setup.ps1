# mission-framework one-click setup (Windows / PowerShell).
#
# Idempotent — safe to re-run. Each step checks before acting and prints
# OK / SKIP / ADD / WARN so you can see what changed.
#
# Usage:
#   .\setup.ps1                  # full setup
#   .\setup.ps1 -SkipNpm         # don't try to install Node CLIs
#   .\setup.ps1 -NoPathUpdate    # don't touch User PATH

[CmdletBinding()]
param(
    [switch]$SkipNpm,
    [switch]$NoPathUpdate
)

# Continue past pip/npm stderr warnings (they wrap into NativeCommandError in PS 5.1).
# We check $LASTEXITCODE explicitly where it matters.
$ErrorActionPreference = "Continue"
$repo = $PSScriptRoot

function Section($msg) { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function OK($msg)      { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function SKIP($msg)    { Write-Host "  [SKIP] $msg" -ForegroundColor DarkGray }
function ADD($msg)     { Write-Host "  [ADD]  $msg" -ForegroundColor Yellow }
function WARN($msg)    { Write-Host "  [WARN] $msg" -ForegroundColor Magenta }
function FAIL($msg)    { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }

# --- 1. Prerequisites -------------------------------------------------------
Section "Checking prerequisites"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { FAIL "python not found on PATH. Install Python 3.10+ first." }
$pyver = & python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
if ([version]$pyver -lt [version]"3.10") { FAIL "Python $pyver too old (need 3.10+)." }
OK "Python $pyver"

if (-not $SkipNpm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) { WARN "npm not found — Node CLIs (claude/codex/gemini) will be skipped" }
    else { OK "npm $(npm -v)" }
}

# --- 2. Python deps ---------------------------------------------------------
Section "Installing mission-framework (pip install -e .)"

Push-Location $repo
try {
    # Don't merge stderr — PS 5.1 turns pip's "not on PATH" stderr warning into a fatal error.
    & python -m pip install -e . --quiet --no-warn-script-location | Out-Null
    if ($LASTEXITCODE -ne 0) { FAIL "pip install failed (exit $LASTEXITCODE)" }
    OK "mission-framework installed"
} finally {
    Pop-Location
}

# --- 3. Find the Scripts dir and add to PATH if needed ----------------------
Section "Wiring the `mission` command onto PATH"

# Try several candidate dirs — Microsoft Store Python installs into user
# scope, not the install-prefix Scripts. We probe known locations and pick
# whichever actually contains mission.exe.
$scriptsDir = & python -c @"
import sysconfig, site, os
candidates = []
candidates.append(sysconfig.get_path('scripts', scheme='nt_user') if 'nt_user' in sysconfig.get_scheme_names() else None)
candidates.append(os.path.join(site.getuserbase(), 'Scripts'))
candidates.append(sysconfig.get_path('scripts'))
for c in candidates:
    if c and os.path.exists(os.path.join(c, 'mission.exe')):
        print(c); break
else:
    # fallback: first existing candidate even without mission.exe yet
    for c in candidates:
        if c and os.path.isdir(c):
            print(c); break
"@
$scriptsDir = $scriptsDir.Trim()

if (-not $scriptsDir) {
    WARN "could not locate any Python Scripts dir — install may be broken"
} elseif (-not (Test-Path "$scriptsDir\mission.exe")) {
    WARN "mission.exe not found in $scriptsDir (pip should have created it). Try re-running."
} else {
    OK "Found mission.exe at $scriptsDir"
}

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $segments = $userPath -split ';' | Where-Object { $_ }
    if ($segments -contains $scriptsDir) {
        SKIP "$scriptsDir already on User PATH"
    } else {
        $newPath = ($segments + $scriptsDir) -join ';'
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        # Also reflect into the current session so the next line works
        $env:Path = "$env:Path;$scriptsDir"
        ADD "added $scriptsDir to User PATH (open a new terminal to pick it up)"
    }
}

# --- 4. UTF-8 stdout for the current session --------------------------------
Section "Setting PYTHONIOENCODING=utf-8 (so colored glyphs render)"

$current = [Environment]::GetEnvironmentVariable("PYTHONIOENCODING", "User")
if ($current -eq "utf-8") {
    SKIP "PYTHONIOENCODING=utf-8 already in User env"
} else {
    [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "User")
    $env:PYTHONIOENCODING = "utf-8"
    ADD "set PYTHONIOENCODING=utf-8 (User scope)"
}

# --- 5. CLI providers (check, optionally install) ---------------------------
Section "Checking the four LLM provider CLIs"

$cliMap = @{
    "claude" = "@anthropic-ai/claude-code"
    "codex"  = "@openai/codex"
    "gemini" = "@google/gemini-cli"
}

$missing = @()
foreach ($name in $cliMap.Keys) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { OK "$name -> $($cmd.Source)" }
    else      { $missing += $name }
}

if ($missing.Count -gt 0) {
    if ($SkipNpm) {
        WARN "missing: $($missing -join ', ') (re-run without -SkipNpm to install)"
    } elseif (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        WARN "missing: $($missing -join ', ') — npm not available, install manually"
    } else {
        $pkgs = $missing | ForEach-Object { $cliMap[$_] }
        ADD "installing missing CLIs via npm: $($pkgs -join ' ')"
        & npm install -g @pkgs 2>&1 | ForEach-Object { Write-Host "       $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -ne 0) { WARN "npm install returned $LASTEXITCODE" }
        else                     { OK "npm install done" }
    }
}

# --- 6. Login status hints --------------------------------------------------
Section "OAuth login reminders (run each once if you haven't)"

Write-Host "  claude login            # Anthropic subscription, opens browser"
Write-Host "  codex login             # ChatGPT subscription, opens browser"
Write-Host "  gemini                  # interactive; sign in then /quit"

# Minimax key check
if ($env:MINIMAX_API_KEY -or [Environment]::GetEnvironmentVariable("MINIMAX_API_KEY", "User")) {
    OK "MINIMAX_API_KEY found in environment"
} else {
    $envFile = Join-Path $repo ".env"
    if ((Test-Path $envFile) -and (Select-String -Path $envFile -Pattern "^MINIMAX_API_KEY=" -Quiet)) {
        OK "MINIMAX_API_KEY found in .env"
    } else {
        WARN "MINIMAX_API_KEY missing — copy .env.example to .env and paste your sk-cp-... key"
    }
}

# --- 7. Smoke test ----------------------------------------------------------
Section "Smoke test: rendering dashboard"

& python -m harness.cli dashboard $repo 2>&1 | Select-Object -First 6 | ForEach-Object {
    Write-Host "  $_" -ForegroundColor DarkGray
}
Write-Host "  (truncated)" -ForegroundColor DarkGray

# --- 8. Summary -------------------------------------------------------------
Section "Done. Next steps"
Write-Host ""
Write-Host "  Interactive TUI:    " -NoNewline; Write-Host "mission console" -ForegroundColor Green
Write-Host "  Snapshot (Claude):  " -NoNewline; Write-Host "mission dashboard" -ForegroundColor Green
Write-Host "  Live full-screen:   " -NoNewline; Write-Host "mission watch" -ForegroundColor Green
Write-Host "  Run a manifest:     " -NoNewline; Write-Host "mission run path\to\manifest.json" -ForegroundColor Green
Write-Host "  Quota state:        " -NoNewline; Write-Host "mission status" -ForegroundColor Green
Write-Host ""
Write-Host "  If 'mission' isn't found, open a NEW terminal (PATH was just updated)."
Write-Host "  Or use:  python -m harness.cli <subcommand>"
Write-Host ""

<#
.SYNOPSIS
    Interactive release publishing script for proxy-to-codex.
.DESCRIPTION
    Checks for uncommitted changes, commits them, then bumps version
    (patch/minor/major), updates pyproject.toml, tags, and pushes.
#>

$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSCommandPath)
Push-Location ..

$repo = "fadeawaylove/proxy-to-codex"

# ── Check for uncommitted changes ──────────────────────────
$status = git status --porcelain 2>$null
if ($status) {
    Write-Host "`nUncommitted changes detected:" -ForegroundColor Yellow
    git status --short
    Write-Host ""

    $commit_msg = Read-Host "Enter commit message [chore: update]"
    if (-not $commit_msg) { $commit_msg = "chore: update" }
    Write-Host "`nStaging all changes..." -ForegroundColor Cyan
    git add -A

    Write-Host "Committing..." -ForegroundColor Cyan
    git commit -m $commit_msg

    Write-Host "`nPushing to origin..." -ForegroundColor Cyan
    git push origin master

    Write-Host "Changes committed and pushed." -ForegroundColor Green
} else {
    Write-Host "Working tree clean." -ForegroundColor Green
}

# ── Get current version ────────────────────────────────────
$tags = git tag -l "v*" --sort=-v:refname 2>$null
if (-not $tags) {
    Write-Host "No existing version tags found. Starting at v0.1.0."
    $current = "v0.1.0"
} else {
    $current = ($tags -split "\n")[0].Trim()
}

Write-Host "`nCurrent version: $current" -ForegroundColor Cyan

# ── Choose bump level ──────────────────────────────────────
Write-Host "`nSelect bump level:" -ForegroundColor Yellow
Write-Host "  [1] patch   (bug fixes)"
Write-Host "  [2] minor   (new features, backward-compatible)"
Write-Host "  [3] major   (breaking changes)"

$choice = Read-Host "`nChoice (1/2/3) [1]"
if (-not $choice) { $choice = "1" }
switch ($choice) {
    "1" { $level = "patch" }
    "2" { $level = "minor" }
    "3" { $level = "major" }
    default {
        Write-Host "Invalid choice '$choice'. Aborting." -ForegroundColor Red
        Pop-Location; Pop-Location; exit 1
    }
}

# ── Calculate new version ──────────────────────────────────
Write-Host "`nBump level: $level" -ForegroundColor Cyan
uv run python -c "import sys; v=sys.argv[1].lstrip('v'); mj,mn,p=v.split('.'); mj,mn,p=int(mj),int(mn),int(p); level=sys.argv[2]; exec({'patch':'p+=1','minor':'mn+=1;p=0','major':'mj+=1;mn=0;p=0'}[level]); print(f'v{mj}.{mn}.{p}')" $current $level | ForEach-Object { $new_tag = $_ }

Write-Host "Bumping ${current} -> ${new_tag}" -ForegroundColor Green

# ── Release notes ──────────────────────────────────────────
Write-Host "`nEnter release notes (end a line with `$ to finish, or leave blank to skip):" -ForegroundColor Yellow
$lines = @()
while ($true) {
    $line = Read-Host
    if ($line -eq "") {
        # empty first line means skip
        if ($lines.Count -eq 0) { break }
    }
    $lines += $line
    if ($line.EndsWith('$')) {
        # remove the trailing $ from the last line
        $lines[-1] = $line.Substring(0, $line.Length - 1)
        if ($lines[-1] -eq "") { $lines = $lines[0..($lines.Count - 2)] }
        break
    }
}
$notes = $lines -join "`n"

if ($notes) {
    Write-Host "`nRelease notes preview:" -ForegroundColor Cyan
    Write-Host ("-" * 40)
    Write-Host $notes
    Write-Host ("-" * 40)
} else {
    Write-Host "`n(no release notes — GitHub will auto-generate from commits)" -ForegroundColor DarkGray
}

# ── Update pyproject.toml version ──────────────────────────
$new_ver = $new_tag -replace '^v', ''
$pyprojectPath = (Resolve-Path "pyproject.toml").Path
uv run python -c "import sys,tomlkit; path=sys.argv[1]; ver=sys.argv[2]; f=open(path,'r',encoding='utf-8'); doc=tomlkit.parse(f.read()); f.close(); doc['project']['version']=ver; open(path,'w',encoding='utf-8').write(tomlkit.dumps(doc)); print(f'Updated pyproject.toml to {ver}')" $pyprojectPath $new_ver
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to update pyproject.toml" -ForegroundColor Red
    Pop-Location; Pop-Location; exit 1
}

# ── Git operations ─────────────────────────────────────────
Write-Host "`nCommitting version bump and tagging..." -ForegroundColor Yellow

git add pyproject.toml
$commit_msg = "Bump version to ${new_tag}"
git commit -m $commit_msg

if ($notes) {
    git tag -a $new_tag -m $notes
} else {
    git tag -a $new_tag -m $new_tag
}

Write-Host "`nPushing to origin..." -ForegroundColor Yellow
git push origin master --follow-tags

# ── Done ───────────────────────────────────────────────────
$release_url = "https://github.com/${repo}/releases/tag/${new_tag}"
$actions_url = "https://github.com/${repo}/actions"

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host "  Published: ${new_tag}" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host ""
Write-Host "  Release:  ${release_url}" -ForegroundColor Cyan
Write-Host "  Actions:  ${actions_url}" -ForegroundColor Cyan
Write-Host ""
Write-Host "  The Windows installer (.exe) will be built by GitHub Actions and attached to the release."

Pop-Location
Pop-Location

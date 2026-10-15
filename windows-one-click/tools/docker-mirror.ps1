# ---------------------------------------------------------------
# Configure Docker registry mirrors (for faster pulls in China).
# Called by "2-一键启动.bat". No Chinese characters on purpose:
# Windows PowerShell 5.1 mis-decodes UTF-8 scripts without BOM.
#
# Exit code 0 = nothing changed
# Exit code 1 = config was written (takes effect on next Docker start)
# ---------------------------------------------------------------

$ErrorActionPreference = 'Stop'

$mirrors = @(
    'https://docker.1ms.run',
    'https://docker.m.daocloud.io',
    'https://docker.1panel.live',
    'https://dockerpull.org'
)

$dir  = Join-Path $env:USERPROFILE '.docker'
$file = Join-Path $dir 'daemon.json'

$existing = $null
if (Test-Path $file) {
    try {
        $raw = Get-Content $file -Raw -Encoding UTF8
        if ($raw -and $raw.Trim().Length -gt 0) {
            $existing = $raw | ConvertFrom-Json
        }
    } catch {
        $existing = $null
    }
}

# If the user already has registry-mirrors set, respect it and do nothing.
if ($null -ne $existing) {
    $cur = $null
    try { $cur = $existing.'registry-mirrors' } catch { $cur = $null }
    if ($cur -and @($cur).Count -gt 0) {
        Write-Output 'registry-mirrors already set, keeping user config.'
        exit 0
    }
}

if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# Back up the original file once before touching it.
if (Test-Path $file) {
    $bak = "$file.bak"
    if (-not (Test-Path $bak)) {
        Copy-Item $file $bak -Force
    }
}

# Merge: keep every existing key, only add/replace registry-mirrors.
$cfg = @{}
if ($null -ne $existing) {
    foreach ($p in $existing.PSObject.Properties) {
        $cfg[$p.Name] = $p.Value
    }
}
$cfg['registry-mirrors'] = $mirrors

$json = $cfg | ConvertTo-Json -Depth 10
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($file, $json, $utf8NoBom)

Write-Output "registry-mirrors written to $file"
exit 1

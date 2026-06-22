<#
wuhan_status.ps1 — show progress of the 4-stage union runs (PowerShell version)

Mirrors scripts/wuhan_status.sh for native Windows users (no WSL / Git Bash needed).

Usage (from repo root):
    powershell -ExecutionPolicy Bypass -File scripts\wuhan_status.ps1
#>

$ErrorActionPreference = 'Stop'

# Paths — adjust here if your wuhan project lives elsewhere
$wuhanPdf   = "$HOME\Documents\work\nengzhong\wuhan\pdf"
$stageChiengNone       = Join-Path $wuhanPdf '.stage_chieng_none'
$stageChiengGauss      = Join-Path $wuhanPdf '.stage_chieng_gauss'
$stageChisimNone       = Join-Path $wuhanPdf '.stage_chisim_none'
$stageChisimGauss      = Join-Path $wuhanPdf '.stage_chisim_gauss'

$stages = @(
    @{ Name = 'chieng+none    '; Dir = $stageChiengNone },
    @{ Name = 'chieng+gauss   '; Dir = $stageChiengGauss },
    @{ Name = 'chisim+none    '; Dir = $stageChisimNone },
    @{ Name = 'chisim+gauss   '; Dir = $stageChisimGauss },
)

function Get-StageStats {
    param($Dir, $Name)
    Write-Host "=== $Name ($Dir) ==="
    $state  = Join-Path $Dir '.cable_match_state.json'
    $cache  = Join-Path $Dir '.cable_match_cache.db'
    $csv    = Join-Path $Dir '_matches.csv'
    $log    = Join-Path $Dir '..\wuhan_4stage_'$($Name.Trim())'.log'

    if (Test-Path $state) {
        $s = Get-Content $state -Raw | ConvertFrom-Json
        $total = if ($s.total) { $s.total } else { 0 }
        $done  = @($s.processed).Count
        $matches = 0
        if ($s.matches) { $matches = ($s.matches.PSObject.Properties.Value | ForEach-Object { @($_).Count } | Measure-Object -Sum).Sum }
        $pct = if ($total -gt 0) { [math]::Round(100 * $done / $total, 1) } else { 0 }
        Write-Host ("  state: {0}/{1} ({2}%), {3} matches" -f $done, $total, $pct, $matches)
        if (Test-Path $cache) {
            $con = New-Object System.Data.SQLite.SQLiteConnection("Data Source=$cache;Version=3;Read Only=True;")
            # Fallback: just count via python if sqlite cli not available
            try {
                $count = & python -c "import sqlite3; print(sqlite3.connect(r'$cache').execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0])" 2>$null
                Write-Host ("  cache: {0} OCR rows (expected ~{1} for both mode)" -f $count, ($done * 2))
            } catch {
                Write-Host "  cache: (could not read)"
            }
        }
        if (Test-Path $csv) {
            $rows = (Get-Content $csv).Count - 1
            Write-Host "  _matches.csv: $rows rows"
        }
    } else {
        Write-Host '  state.json not yet created (still starting up)'
    }
    if (Test-Path $log) {
        $last = Get-Content $log -Tail 1
        if ($last.Length -gt 100) { $last = $last.Substring(0, 100) + '...' }
        Write-Host "  last log: $last"
    }
    Write-Host ''
}

foreach ($stage in $stages) {
    Get-StageStats -Dir $stage.Dir -Name $stage.Name
}

Write-Host '=== running processes ==='
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*cable_match*' -or $_.CommandLine -like '*cable_match*' } |
    ForEach-Object { Write-Host ("  PID {0}  CPU {1}%  MEM {2}MB  started {3}" -f $_.Id, $_.CPU, [int]($_.WorkingSet64/1MB), $_.StartTime) }
if (-not (Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*cable_match*' })) {
    Write-Host '  (no cable_match processes running)'
}
Write-Host ''

Write-Host '=== free disk space ==='
$wuhanDrive = (Get-Item $wuhanPdf).PSDrive
if ($wuhanDrive) {
    Write-Host ("  {0} free: {1:N1} GB" -f $wuhanDrive.Name, ($wuhanDrive.Free / 1GB))
}

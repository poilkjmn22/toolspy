<#
run_union.ps1 — launch selected OCR stages in parallel on native Windows

Mirrors scripts/run_union.sh for Windows users without WSL.

Stages (2 lang × 3 OCR recipes — total 6, indexed 1-6):
  Tesseract (default engine):
    1. chieng_tess         — chi_sim+eng, no preprocess
    2. chieng_tess_gauss   — chi_sim+eng, gauss_otsu
    3. chisim_tess         — chi_sim only, no preprocess
    4. chisim_tess_gauss   — chi_sim only, gauss_otsu
  PaddleOCR (optional, `pip install -r requirements-paddleocr.txt`):
    5. chieng_paddle       — PaddleOCR ch model, no preprocess
    6. chisim_paddle       — PaddleOCR en model, no preprocess

Output dirs (under <WUHAN>/):
  .stage_chieng_tess/         .stage_chieng_tess_gauss/
  .stage_chisim_tess/         .stage_chisim_tess_gauss/
  .stage_chieng_paddle/       .stage_chisim_paddle/

Usage (from repo root):
    # Run all 6 stages (default)
    powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4

    # Run only Tesseract 4 stages
    powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "1-4"

    # Run only PaddleOCR 2 stages (use after Win11 GPU is ready)
    powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "5-6"

    # Mixed subset (stage 1, 3, and 6)
    powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "1,3,6"

    # Range + list (stages 2-4 and 6)
    powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 -WorkersPerStage 4 -Stages "2-4,6"

-Stages syntax:
    "all"        run all available stages (default)
    "1-4"        range, inclusive
    "1,3,5"      comma list
    "1-3,6"      mixed range + list

After each batch finishes:
    python scripts\merge_5stage_matches.py "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf"

The merge is idempotent — you can run it any time after any subset of stages
has completed. Missing stage _matches.csv files are silently skipped, so
partial batches produce partial unions, and the final run after all batches
produces the complete union.
#>

param(
    [int]$WorkersPerStage = 4,
    [string]$WuhanDir = "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf",
    [ValidateSet('mixed','tesseract')]
    [string]$Engine = 'mixed',
    [string]$Stages = 'all'
)

$ErrorActionPreference = 'Stop'
$csv = Join-Path $WuhanDir 'filter_result_3hzb_ff.csv'
$repo = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repo 'myenv\Scripts\python.exe'
$logDir = Join-Path (Split-Path $WuhanDir -Parent) 'logs'

if (-not (Test-Path $pythonExe)) {
    Write-Error "Python venv not found at $pythonExe. Run: py -3.11 -m venv myenv; myenv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path $csv)) {
    Write-Error "Cable CSV not found at $csv"
}

if ($Engine -in 'mixed','tesseract') {
    if (-not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
        Write-Warning "tesseract not on PATH. Install from https://github.com/UB-Mannheim/tesseract/wiki"
    }
}

$paddleNeeded = ($Engine -eq 'mixed')
if ($paddleNeeded) {
    $paddleCheck = & $pythonExe -c "import paddleocr" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "paddleocr not installed. Run: myenv\Scripts\pip install -r requirements-paddleocr.txt"
    }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Build the full stages list (before -Stages filter applies).
# Each stage is a hashtable with Name, Args, OutDir.
# PaddleOCR stages always include --engine paddleocr.
$allStages = @(
    @{ Name = 'chieng_tess';         Args = @('--preprocess', 'none');            OutDir = '.stage_chieng_tess' },
    @{ Name = 'chieng_tess_gauss';   Args = @('--preprocess', 'gauss_otsu');      OutDir = '.stage_chieng_tess_gauss' },
    @{ Name = 'chisim_tess';         Args = @('--lang', 'chi_sim', '--preprocess', 'none');            OutDir = '.stage_chisim_tess' },
    @{ Name = 'chisim_tess_gauss';   Args = @('--lang', 'chi_sim', '--preprocess', 'gauss_otsu');      OutDir = '.stage_chisim_tess_gauss' },
)

if ($paddleNeeded) {
    $allStages += @(
        @{ Name = 'chieng_paddle';   Args = @('--engine', 'paddleocr', '--preprocess', 'none');       OutDir = '.stage_chieng_paddle' },
        @{ Name = 'chisim_paddle';   Args = @('--engine', 'paddleocr', '--lang', 'chi_sim', '--preprocess', 'none'); OutDir = '.stage_chisim_paddle' },
    )
}

$nAllStages = $allStages.Count

# === Parse -Stages filter into 1-based stage indices ===
function Parse-Stages {
    param([string]$Filter, [int]$Max)
    if ([string]::IsNullOrEmpty($Filter) -or $Filter -eq 'all') {
        return 1..$Max
    }
    $result = @()
    foreach ($token in $Filter.Split(',')) {
        $token = $token.Trim()
        if ($token -match '^(\d+)-(\d+)$') {
            $start = [int]$Matches[1]
            $end = [int]$Matches[2]
            if ($start -gt $end) {
                Write-Error "Invalid range '$token' (start > end)"
                return $null
            }
            foreach ($i in $start..$end) { $result += $i }
        } elseif ($token -match '^\d+$') {
            $result += [int]$token
        } else {
            Write-Error "Invalid stage token: '$token' (expected integer or N-M range)"
            return $null
        }
    }
    foreach ($i in $result) {
        if ($i -lt 1 -or $i -gt $Max) {
            Write-Error "Stage index $i out of range (1-$Max)"
            return $null
        }
    }
    return $result
}

$selectedIdx = Parse-Stages -Filter $Stages -Max $nAllStages
if ($null -eq $selectedIdx) { exit 1 }
$nSelected = $selectedIdx.Count
$totalWorkers = $WorkersPerStage * $nSelected

Write-Host '=== 6-stage union launcher (PowerShell, filtered) ==='
Write-Host "  WuhanDir:         $WuhanDir"
Write-Host "  CSV:               $csv"
Write-Host "  Engine:            $Engine  (override: -Engine tesseract)"
Write-Host "  WorkersPerStage:   $WorkersPerStage"
Write-Host "  Stages filter:     $Stages  ($nSelected / $nAllStages stages)"
Write-Host "  Total workers:     $totalWorkers"
Write-Host "  Logs:              $logDir"
Write-Host ''
Write-Host '  all stages (index -> name):'
for ($i = 0; $i -lt $nAllStages; $i++) {
    $idx = $i + 1
    $marker = ''
    if ($selectedIdx -contains $idx) { $marker = '  <- will run' }
    Write-Host ("    {0}. {1}{2}" -f $idx, $allStages[$i].Name, $marker)
}
Write-Host ''

foreach ($idx in $selectedIdx) {
    $stage = $allStages[$idx - 1]  # 1-based -> 0-based
    $outDir = Join-Path $WuhanDir $stage.OutDir
    $log = Join-Path $logDir ("stage_{0}.log" -f $stage.Name)
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    $argList = @(
        'scripts\cable_match.py',
        '--csv', $csv,
        '--input', $WuhanDir,
        '--output', $outDir,
        '--workers', "$WorkersPerStage"
    ) + $stage.Args

    Write-Host ("  launching: {0,-22}  ->  {1}" -f $stage.Name, $outDir)
    Write-Host ("    log: {0}" -f $log)
    $proc = Start-Process -FilePath $pythonExe -ArgumentList $argList `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" `
        -WorkingDirectory $repo -PassThru -WindowStyle Hidden
    Write-Host ("    PID: {0}" -f $proc.Id)
    Set-Content -Path (Join-Path $logDir "$($stage.Name).pid") -Value $proc.Id
}

Write-Host ''
Write-Host "=== $nSelected stage(s) launched ==="
Write-Host '  status: powershell -ExecutionPolicy Bypass -File scripts\wuhan_status.ps1'
Write-Host '  stop:   Get-Process python | Where-Object {$_.CommandLine -like "*cable_match*"} | Stop-Process'
Write-Host '          (state.json auto-saved every 30s; safe to interrupt)'
Write-Host "  merge:  python scripts\merge_5stage_matches.py '$WuhanDir'  (idempotent; run any time)"
Write-Host ''
Write-Host '=== running processes ==='
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  PID {0,-6}  started {1}" -f $_.Id, $_.StartTime)
}
<#
run_union.ps1 — launch 6 OCR stages in parallel on native Windows

Mirrors scripts/run_union.sh for Windows users without WSL.

Stages (2 lang × 3 OCR recipes — total 6):
  Tesseract (default engine):
    1. chieng+none      — chi_sim+eng, no preprocess
    2. chieng+gauss      — chi_sim+eng, gauss_otsu
    3. chisim+none       — chi_sim only, no preprocess
    4. chisim+gauss      — chi_sim only, gauss_otsu
  PaddleOCR (optional, `pip install -r requirements-paddleocr.txt`):
    5. chieng+paddle     — PaddleOCR ch model, no preprocess
    6. chisim+paddle     — PaddleOCR en model, no preprocess

Output dirs (under <WUHAN>/):
  .stage_chieng_tess/         .stage_chieng_tess_gauss/
  .stage_chisim_tess/         .stage_chisim_tess_gauss/
  .stage_chieng_paddle/       .stage_chisim_paddle/

Usage (from repo root):
    powershell -ExecutionPolicy Bypass -File scripts\run_union.ps1 [-WorkersPerStage 4] [-Engine mixed|tesseract]

After all stages finish:
    python scripts\merge_5stage_matches.py "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf"
#>

param(
    [int]$WorkersPerStage = 4,
    [string]$WuhanDir = "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf",
    [ValidateSet('mixed','tesseract')]
    [string]$Engine = 'mixed'
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

# Build the stages list. Each stage is a hashtable with Name, Args, OutDir.
# PaddleOCR stages always include --engine paddleocr.
$stages = @(
    @{ Name = 'chieng_tess';         Args = @('--preprocess', 'none');            OutDir = '.stage_chieng_tess' },
    @{ Name = 'chieng_tess_gauss';   Args = @('--preprocess', 'gauss_otsu');      OutDir = '.stage_chieng_tess_gauss' },
    @{ Name = 'chisim_tess';         Args = @('--lang', 'chi_sim', '--preprocess', 'none');            OutDir = '.stage_chisim_tess' },
    @{ Name = 'chisim_tess_gauss';   Args = @('--lang', 'chi_sim', '--preprocess', 'gauss_otsu');      OutDir = '.stage_chisim_tess_gauss' },
)

if ($paddleNeeded) {
    $stages += @(
        @{ Name = 'chieng_paddle';   Args = @('--engine', 'paddleocr', '--preprocess', 'none');       OutDir = '.stage_chieng_paddle' },
        @{ Name = 'chisim_paddle';   Args = @('--engine', 'paddleocr', '--lang', 'chi_sim', '--preprocess', 'none'); OutDir = '.stage_chisim_paddle' },
    )
}

$nStages = $stages.Count
$totalWorkers = $WorkersPerStage * $nStages

Write-Host '=== 6-stage union launcher (PowerShell) ==='
Write-Host "  WuhanDir:         $WuhanDir"
Write-Host "  CSV:               $csv"
Write-Host "  Engine:            $Engine  (override: -Engine tesseract)"
Write-Host "  WorkersPerStage:   $WorkersPerStage"
Write-Host "  Total stages:       $nStages"
Write-Host "  Total workers:      $totalWorkers"
Write-Host "  Logs:               $logDir"
Write-Host ''

foreach ($stage in $stages) {
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
Write-Host "=== all $nStages stages launched ==="
Write-Host '  status: powershell -ExecutionPolicy Bypass -File scripts\wuhan_status.ps1'
Write-Host '  stop:   Get-Process python | Where-Object {$_.CommandLine -like "*cable_match*"} | Stop-Process'
Write-Host '          (state.json auto-saved every 30s; safe to interrupt)'
Write-Host "  merge:  python scripts\merge_5stage_matches.py '$WuhanDir'  (after all finish)"
Write-Host ''
Write-Host '=== running processes ==='
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  PID {0,-6}  started {1}" -f $_.Id, $_.StartTime)
}

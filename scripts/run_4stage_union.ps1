<#
run_4stage_union.ps1 — launch 4 OCR stages in parallel on native Windows

Mirrors scripts/run_4stage_union.sh for Windows users without WSL.

Usage (from repo root):
    powershell -ExecutionPolicy Bypass -File scripts\run_4stage_union.ps1 [-WorkersPerStage 4]

After all stages finish:
    python scripts\merge_4stage_matches.py "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf"
#>

param(
    [int]$WorkersPerStage = 4,
    [string]$WuhanDir = "$env:USERPROFILE\Documents\work\nengzhong\wuhan\pdf",
    [ValidateSet('tesseract','paddleocr')]
    [string]$Engine = 'tesseract'
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
if ($Engine -eq 'tesseract') {
    if (-not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
        Write-Warning "tesseract not on PATH. Install from https://github.com/UB-Mannheim/tesseract/wiki"
    }
} elseif ($Engine -eq 'paddleocr') {
    $paddleCheck = & $pythonExe -c "import paddleocr" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "paddleocr not installed. Run: myenv\Scripts\pip install -r requirements-paddleocr.txt"
    }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$stages = @(
    @{ Name = 'chieng_none';  Args = @('--preprocess', 'none');            OutDir = '.stage_chieng_none' },
    @{ Name = 'chieng_gauss'; Args = @('--preprocess', 'gauss_otsu');      OutDir = '.stage_chieng_gauss' },
    @{ Name = 'chisim_none';  Args = @('--lang', 'chi_sim', '--preprocess', 'none');       OutDir = '.stage_chisim_none' },
    @{ Name = 'chisim_gauss'; Args = @('--lang', 'chi_sim', '--preprocess', 'gauss_otsu'); OutDir = '.stage_chisim_gauss' },
)

Write-Host '=== 4-stage union launcher (PowerShell) ==='
Write-Host "  WuhanDir:         $WuhanDir"
Write-Host "  CSV:               $csv"
Write-Host "  Engine:            $Engine  (override: -Engine paddleocr)"
Write-Host "  WorkersPerStage:   $WorkersPerStage"
Write-Host "  Total workers:     $($WorkersPerStage * 4)"
Write-Host "  Logs:              $logDir"
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
        '--workers', "$WorkersPerStage",
        '--engine', $Engine
    ) + $stage.Args

    Write-Host ("  launching: {0,-14}  ->  {1}" -f $stage.Name, $outDir)
    Write-Host ("    log: {0}" -f $log)
    $proc = Start-Process -FilePath $pythonExe -ArgumentList $argList `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" `
        -WorkingDirectory $repo -PassThru -WindowStyle Hidden
    Write-Host ("    PID: {0}" -f $proc.Id)
    Set-Content -Path (Join-Path $logDir "$($stage.Name).pid") -Value $proc.Id
}

Write-Host ''
Write-Host '=== all 4 stages launched ==='
Write-Host '  status: powershell -ExecutionPolicy Bypass -File scripts\wuhan_status.ps1'
Write-Host '  stop:   Get-Process python | Where-Object {$_.CommandLine -like "*cable_match*"} | Stop-Process'
Write-Host '          (state.json auto-saved every 30s; safe to interrupt)'
Write-Host '  merge:  python scripts\merge_4stage_matches.py "<wuhan_pdf_dir>"  (after all 4 finish)'
Write-Host ''
Write-Host '=== running processes ==='
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  PID {0,-6}  started {1}" -f $_.Id, $_.StartTime)
}

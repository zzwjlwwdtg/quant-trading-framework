# Safe WebUI restart (2026-09-24).
# Stops ONLY the process listening on 127.0.0.1:8080 whose command line contains webui.py,
# relaunches via _webui_watchdog.py (same path as Task Scheduler), then verifies.
# Does not touch orchestrator / OpenD / other python processes.
$ErrorActionPreference = 'Stop'
$Agents = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py     = 'C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe'
$Port   = 8080
$Health = "http://127.0.0.1:$Port/api/health"

function Get-WebuiProc {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
        if ($p -and $p.CommandLine -match 'webui\.py') { return $p }
    }
    return $null
}

$old = Get-WebuiProc
if ($old) {
    $oldStart = (Get-Process -Id $old.ProcessId).StartTime
    Write-Host "[old] WebUI PID $($old.ProcessId) started $oldStart"
    Stop-Process -Id $old.ProcessId -Force
    for ($i = 0; $i -lt 30 -and (Get-WebuiProc); $i++) { Start-Sleep -Seconds 1 }
    if (Get-WebuiProc) { throw "old WebUI still listening on $Port - aborting" }
    Write-Host "[old] stopped"
} else {
    Write-Host "[old] no WebUI listening on $Port"
}

Write-Host "[new] launching via _webui_watchdog.py ..."
& $Py -X utf8 -u (Join-Path $Agents '_webui_watchdog.py')

$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $Health -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
$new = Get-WebuiProc
$runtime = 'webui.py','paper_trader.py','cohort_tracker.py','fill_ledger.py','decision_agent.py'
$codeTime = ($runtime | ForEach-Object { Get-Item (Join-Path $Agents $_) } | Measure-Object LastWriteTime -Maximum).Maximum
if ($new) {
    $newStart = (Get-Process -Id $new.ProcessId).StartTime
    Write-Host "[new] WebUI PID $($new.ProcessId) started $newStart  health_ok=$ok"
    Write-Host "[new] latest runtime code write time $codeTime  loaded_latest=$($newStart -gt $codeTime)"
} else {
    Write-Host "[new] WebUI NOT running (health_ok=$ok) - check agents\signals\webui_watchdog.jsonl"
}

$lock = Join-Path $Agents '.orchestrator.lock'
if (Test-Path $lock) {
    $opid = [int](Get-Content $lock -Raw).Trim()
    $op = Get-Process -Id $opid -ErrorAction SilentlyContinue
    if ($op) {
        $orchFiles = 'paper_trader.py','cohort_tracker.py','fill_ledger.py','decision_agent.py','orchestrator.py','config.py'
        $orchTime = ($orchFiles | ForEach-Object { Get-Item (Join-Path $Agents $_) } | Measure-Object LastWriteTime -Maximum).Maximum
        Write-Host "[orchestrator] PID $opid started $($op.StartTime)  loaded_latest=$($op.StartTime -gt $orchTime) (vs its own files, latest $orchTime)"
    } else { Write-Host "[orchestrator] PID $opid not running" }
}
Push-Location (Split-Path -Parent $Agents)
Write-Host "[git] HEAD $(git rev-parse --short HEAD)"
Pop-Location

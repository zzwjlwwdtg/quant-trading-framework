# Kill duplicate WebUI instances (2026-09-29). Keeps exactly one webui.py: the first 8080
# listener reported by Windows. Others (started >10 min ago) and their webui.bat cmd parents are stopped.
# Never touches the 8080 owner, orchestrator, OpenD, or any other process.
$ErrorActionPreference = 'Continue'
$Agents = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $Agents 'logs\kill_webui_zombies_last.txt'
$log = New-Object System.Collections.Generic.List[string]
$log.Add("generated $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$owner = (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
$log.Add("8080 owner PID = $owner (kept)")
if (-not $owner) { $log.Add("no 8080 owner - aborting, nothing killed"); $log | Out-File $out -Encoding utf8; exit 1 }
$lockPid = [int](Get-Content (Join-Path $Agents '.orchestrator.lock') -Raw).Trim()
$cut = (Get-Date).AddMinutes(-10)
$all = Get-CimInstance Win32_Process
foreach ($p in $all | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'webui\.py' }) {
    $procId = [int]$p.ProcessId
    if ($procId -eq [int]$owner -or $procId -eq $lockPid) { continue }
    if ($p.CreationDate -gt $cut) { $log.Add("skip PID $procId (too new)"); continue }
    # Windows SO_REUSEADDR lets several webui.py bind 8080 at once; keep only $owner.
    $parent = $all | Where-Object { $_.ProcessId -eq $p.ParentProcessId }
    Stop-Process -Id $procId -Force -ErrorAction Continue
    $log.Add("killed python PID $procId started $($p.CreationDate)")
    if ($parent -and $parent.Name -eq 'cmd.exe' -and $parent.CommandLine -match 'webui\.bat') {
        Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
        $log.Add("killed parent cmd PID $($parent.ProcessId)")
    }
}
Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'webui\.(py|bat)' }
$log.Add("remaining webui processes:")
foreach ($r in $left) { $log.Add("  $($r.Name) PID=$($r.ProcessId) start=$($r.CreationDate)") }
try { $h = Invoke-WebRequest -Uri 'http://127.0.0.1:8080/api/health' -UseBasicParsing -TimeoutSec 10; $log.Add("health=$($h.StatusCode)") } catch { $log.Add("health=FAIL $_") }
$log | Out-File $out -Encoding utf8

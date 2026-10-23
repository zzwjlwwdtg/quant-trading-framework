# READ-ONLY process report (2026-09-29). Lists python/cmd/OpenD processes; kills nothing.
$ErrorActionPreference = 'Continue'
$Agents = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $Agents 'logs\proc_report_last.txt'
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("generated $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$all = Get-CimInstance Win32_Process
$byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$listen = @{}
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $listen[[int]$_.OwningProcess] += @($_.LocalPort) }
$lock = Join-Path $Agents '.orchestrator.lock'
$orchPid = if (Test-Path $lock) { [int](Get-Content $lock -Raw).Trim() } else { -1 }
$lines.Add("orchestrator.lock PID = $orchPid")
$targets = $all | Where-Object { $_.Name -match '^(python|pythonw|cmd|OpenD|moomoo_OpenD|FTNN|Futu)' -or $_.Name -match 'OpenD' }
foreach ($p in ($targets | Sort-Object CreationDate)) {
    $gp = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
    $title = if ($gp) { $gp.MainWindowTitle } else { '' }
    $parent = $byId[[int]$p.ParentProcessId]
    $pname = if ($parent) { "$($parent.Name)#$($parent.ProcessId)" } else { "gone#$($p.ParentProcessId)" }
    $kids = ($all | Where-Object { $_.ParentProcessId -eq $p.ProcessId } | ForEach-Object { "$($_.Name)#$($_.ProcessId)" }) -join ','
    $ports = ($listen[[int]$p.ProcessId] | Sort-Object -Unique) -join ','
    $cmd = ($p.CommandLine -replace '\s+', ' ')
    if ($cmd.Length -gt 220) { $cmd = $cmd.Substring(0, 220) + '...' }
    $lines.Add("---")
    $lines.Add("$($p.Name) PID=$($p.ProcessId) start=$($p.CreationDate.ToString('MM-dd HH:mm:ss')) parent=$pname ports=[$ports] window='$title'")
    $lines.Add("  children=[$kids]")
    $lines.Add("  cmd=$cmd")
}
$lines.Add("=== scheduled tasks referencing fsi-skills (read-only) ===")
try {
    foreach ($t in Get-ScheduledTask -ErrorAction Stop) {
        $acts = ($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' | '
        if ($acts -match 'fsi-skills') {
            $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue
            $lines.Add("task=$($t.TaskPath)$($t.TaskName) state=$($t.State) last=$($info.LastRunTime) result=$($info.LastTaskResult) next=$($info.NextRunTime)")
            $lines.Add("  action=$acts")
        }
    }
} catch { $lines.Add("Get-ScheduledTask failed: $_") }
$lines | Out-File -FilePath $out -Encoding utf8

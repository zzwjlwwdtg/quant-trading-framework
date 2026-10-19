' _run_hidden.vbs — 隐藏窗口执行 .bat / .exe
' Task Scheduler 直接跑 .bat 会弹 cmd 窗口, 用它包一层就无窗口了.
'
' 用法:
'   wscript.exe "F:\fsi-skills\agents\_run_hidden.vbs" "F:\fsi-skills\agents\weekly.bat"
'   wscript.exe "F:\fsi-skills\agents\_run_hidden.vbs" "path\to\any.bat" arg1 arg2
'
' Task Scheduler action:
'   Program: wscript.exe
'   Args   : "F:\fsi-skills\agents\_run_hidden.vbs" "F:\fsi-skills\agents\weekly.bat"

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

Dim shell, target, args, cmd, i
Set shell = CreateObject("WScript.Shell")

target = WScript.Arguments(0)

' 后续参数拼接为字符串
args = ""
For i = 1 To WScript.Arguments.Count - 1
    args = args & " """ & WScript.Arguments(i) & """"
Next

' 用 cmd.exe /c 包一层, 避免直接 Run .bat 时的 quoting 边缘问题.
' /c 让 cmd.exe 执行完就退, 双引号包住目标路径处理空格.
' 外层再用一对双引号包整串, 是 cmd.exe /c 的标准做法.
cmd = "cmd.exe /c """"" & target & """" & args & """"

' Run(command, windowStyle, waitOnReturn)
'   0     = 隐藏窗口
'   True  = 等待结束, 让 Task Scheduler 拿到正确的 exit code
On Error Resume Next
shell.Run cmd, 0, True
If Err.Number <> 0 Then
    ' 记 event log 供事后排查 (不弹窗)
    Dim logfile, fso
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set logfile = fso.OpenTextFile("F:\fsi-skills\agents\logs\vbs_run_hidden.log", 8, True)
    logfile.WriteLine Now & " ERR " & Err.Number & " " & Err.Description & " | cmd=" & cmd
    logfile.Close
    WScript.Quit Err.Number
End If

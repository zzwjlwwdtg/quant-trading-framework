@echo off
REM One-off: open a VISIBLE console that starts Claude CLI login (OAuth in browser). User clicks Authorize in the browser.
chcp 65001 > nul
cd /d "%~dp0"
start "Claude 登录 (完成后可关闭此窗口)" cmd /k "echo 正在启动 Claude 登录, 浏览器会弹出授权页, 请点 Authorize/授权 ... & echo. & claude auth login & echo. & echo ---- 当前登录状态 ---- & claude auth status & echo. & echo 如果上面失败: 在本窗口输入 claude 回车, 再输入 /login 回车"
del "%~dp0signals\job_claude_login.running" 2>nul

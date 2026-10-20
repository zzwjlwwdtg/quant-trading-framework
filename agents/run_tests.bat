@echo off
setlocal
cd /d "%~dp0"
rem Tests print Chinese/emoji; UTF-8 mode avoids cp932 UnicodeEncodeError noise.
set PYTHONUTF8=1
python -m unittest discover -s tests -t . -v %*
exit /b %ERRORLEVEL%

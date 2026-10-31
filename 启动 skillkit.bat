@echo off
rem ============================================================
rem  skillkit desktop launcher
rem  double-click to start. Extra args are forwarded, e.g.
rem      launcher.bat --debug
rem  Prefers the pinned pythonw.exe; falls back to whatever
rem  pythonw is on PATH so the script survives being copied to
rem  another machine. Uses pythonw: no console window. Startup
rem  errors are written to kit\skillkit.log (rotating) and shown
rem  in a message box.
rem ============================================================
setlocal
set "KIT=%~dp0kit"
set "LOG=%KIT%\skillkit.log"
set "PYW=D:\develop\python\3.12\pythonw.exe"

if exist "%PYW%" goto run
rem 固定路径不存在（换了机器）：退回 PATH 里的 pythonw
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
  set "PYW=pythonw.exe"
  goto run
)
>>"%LOG%" echo [%DATE% %TIME%] Missing Python: %PYW% and no pythonw.exe in PATH
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('未找到 Python：D:\develop\python\3.12\pythonw.exe，且 PATH 里没有 pythonw.exe','skillkit')"
exit /b 1

:run
if not exist "%KIT%\desktop.py" goto nofile
if not exist "%~dp0ui\index.html" goto nofile

cd /d "%KIT%"
start "" "%PYW%" "%KIT%\desktop.py" %*
exit /b 0

:nofile
>>"%LOG%" echo [%DATE% %TIME%] Missing ui\index.html or kit\desktop.py under %~dp0
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Missing ui\index.html or kit\desktop.py','skillkit')"
exit /b 1

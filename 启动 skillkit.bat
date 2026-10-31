@echo off
rem ============================================================
rem  skillkit desktop launcher
rem  double-click to start. Extra args are forwarded, e.g.
rem      launcher.bat --debug
rem  Uses pythonw.exe: no console window. Startup errors are
rem  written to kit\launch.log and shown in a message box.
rem ============================================================
setlocal
set "KIT=%~dp0kit"
set "PYW=D:\develop\python\3.12\pythonw.exe"
set "LOG=%KIT%\launch.log"

if not exist "%PYW%" goto nopython
if not exist "%KIT%\desktop.py" goto nofile
if not exist "%~dp0ui\index.html" goto nofile

cd /d "%KIT%"
start "" "%PYW%" "%KIT%\desktop.py" %*
exit /b 0

:nopython
>>"%LOG%" echo [%DATE% %TIME%] Missing Python: %PYW%
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Missing Python: D:\develop\python\3.12\pythonw.exe','skillkit')"
exit /b 1

:nofile
>>"%LOG%" echo [%DATE% %TIME%] Missing ui\index.html or kit\desktop.py under %~dp0
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Missing ui\index.html or kit\desktop.py','skillkit')"
exit /b 1

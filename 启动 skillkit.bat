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
rem Pinned path missing (new machine): fall back to pythonw on PATH, but skip
rem the WindowsApps store stub -- it is not a real Python: start() returns
rem immediately, writes no log and shows no dialog.
set "PYWC="
for /f "usebackq delims=" %%i in (`where pythonw.exe 2^>nul`) do (
  echo %%i | "%SystemRoot%\System32\find.exe" /i "WindowsApps" >nul
  if errorlevel 1 if not defined PYWC set "PYWC=%%i"
)
if not defined PYWC goto nopython
set "PYW=%PYWC%"
rem Prove it actually runs (stub or broken install surfaces here, not later)
"%PYW%" -VV >nul 2>nul
if errorlevel 1 goto nopython
goto run

:nopython
>>"%LOG%" echo [%DATE% %TIME%] Missing Python: %PYW% and no usable pythonw.exe in PATH
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('未找到可用的 Python：需要 D:\develop\python\3.12\pythonw.exe，或 PATH 里一个非 WindowsApps 的 pythonw.exe','skillkit')"
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

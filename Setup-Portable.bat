@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM   YT Portable - Setup (run ONCE)
REM   Downloads the portable engine into this folder.
REM   Installs nothing on Windows. No admin rights required.
REM ============================================================

set "PYVER=3.12.7"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"
set "YTURL=https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
set "FFURL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

echo.
echo ============================================================
echo   Setting up YT Portable... (this may take a few minutes)
echo ============================================================
echo.

if not exist "app.py" (
    echo [ERROR] Can't find app.py. Place this .bat next to app.py.
    pause & exit /b 1
)

if not exist "web\index.html" (
    echo [ERROR] Can't find web\index.html. The web folder must sit next to app.py.
    pause & exit /b 1
)

if not exist "_tmp" mkdir "_tmp"
if not exist "bin" mkdir "bin"

REM ---- 1) Embedded Python ------------------------------------
if exist "runtime\python.exe" (
    echo [1/3] Python already present, skipping.
) else (
    echo [1/3] Downloading embedded Python...
    curl -L --fail -o "_tmp\python.zip" "%PYURL%"
    if errorlevel 1 ( echo [ERROR] Failed to download Python. & goto :fail )
    echo       Extracting...
    powershell -NoProfile -Command "Expand-Archive -Force '_tmp\python.zip' 'runtime'"
    if not exist "runtime\python.exe" ( echo [ERROR] Python extraction failed. & goto :fail )
)

REM ---- 2) yt-dlp.exe -----------------------------------------
echo [2/3] Downloading yt-dlp...
curl -L --fail -o "bin\yt-dlp.exe" "%YTURL%"
if not exist "bin\yt-dlp.exe" ( echo [ERROR] Failed to download yt-dlp. & goto :fail )

REM ---- 3) ffmpeg ---------------------------------------------
if exist "bin\ffmpeg.exe" (
    echo [3/3] ffmpeg already present, skipping.
) else (
    echo [3/3] Downloading ffmpeg... ^(large, please be patient^)
    curl -L --fail -o "_tmp\ffmpeg.zip" "%FFURL%"
    if errorlevel 1 ( echo [ERROR] Failed to download ffmpeg. & goto :fail )
    echo       Extracting ffmpeg.exe and ffprobe.exe...
    powershell -NoProfile -Command "Expand-Archive -Force '_tmp\ffmpeg.zip' '_tmp\ff'; Get-ChildItem -Path '_tmp\ff' -Recurse -Include ffmpeg.exe,ffprobe.exe | ForEach-Object { Copy-Item $_.FullName -Destination 'bin' -Force }"
    if not exist "bin\ffmpeg.exe" ( echo [ERROR] Couldn't extract ffmpeg. & goto :fail )
)

REM ---- Create launcher (no console, uses pythonw) -----------
> "Start-Portable.bat" echo @echo off
>> "Start-Portable.bat" echo cd /d "%%~dp0"
>> "Start-Portable.bat" echo start "" "%%~dp0runtime\pythonw.exe" "%%~dp0app.py"

REM ---- Extract the app icon (embedded in app.py) to a .ico file ----
if not exist "yt-portable.ico" (
    "runtime\python.exe" app.py --write-icon "yt-portable.ico" >nul 2>&1
)
set "ICON_REL=runtime\pythonw.exe"
if exist "yt-portable.ico" set "ICON_REL=yt-portable.ico"

REM ---- Create .lnk shortcut (launches pythonw with NO window)
REM    Uses PowerShell's COM object; does NOT run any .vbs.
echo [*] Creating windowless shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=(Get-Location).Path; $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut((Join-Path $d 'YT Portable.lnk')); $s.TargetPath=(Join-Path $d 'runtime\pythonw.exe'); $s.Arguments='\"'+(Join-Path $d 'app.py')+'\"'; $s.WorkingDirectory=$d; $s.IconLocation=(Join-Path $d '%ICON_REL%'); $s.Save()" 2>nul
if exist "YT Portable.lnk" (
    echo       Shortcut created: "YT Portable.lnk"
) else (
    echo       [notice] Couldn't create the .lnk; use "Start-Portable.bat" instead.
)

REM ---- Cleanup -----------------------------------------------
rmdir /s /q "_tmp" 2>nul

echo.
echo ============================================================
echo   DONE. Everything stays inside this folder.
echo   To use it (no console window):
echo       double-click  "YT Portable.lnk"
echo   Alternative:  "Start-Portable.bat"
echo   (You can now copy this folder to a USB stick.)
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo   Setup interrupted. Check your connection and try again.
echo.
pause
exit /b 1

@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM   YT Portable - Constructor (ejecutar UNA sola vez)
REM   Descarga el motor portable dentro de esta carpeta.
REM   No instala nada en Windows. No requiere admin.
REM ============================================================

set "PYVER=3.12.7"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"
set "YTURL=https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
set "FFURL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

echo.
echo ============================================================
echo   Montando YT Portable... (puede tardar unos minutos)
echo ============================================================
echo.

if not exist "app.py" (
    echo [ERROR] No encuentro app.py. Deja este .bat junto a app.py.
    pause & exit /b 1
)

if not exist "_tmp" mkdir "_tmp"
if not exist "bin" mkdir "bin"

REM ---- 1) Python embebido ------------------------------------
if exist "runtime\python.exe" (
    echo [1/3] Python ya presente, omitido.
) else (
    echo [1/3] Descargando Python embebido...
    curl -L --fail -o "_tmp\python.zip" "%PYURL%"
    if errorlevel 1 ( echo [ERROR] Fallo al descargar Python. & goto :fail )
    echo       Extrayendo...
    powershell -NoProfile -Command "Expand-Archive -Force '_tmp\python.zip' 'runtime'"
    if not exist "runtime\python.exe" ( echo [ERROR] Extraccion de Python fallida. & goto :fail )
)

REM ---- 2) yt-dlp.exe -----------------------------------------
echo [2/3] Descargando yt-dlp...
curl -L --fail -o "bin\yt-dlp.exe" "%YTURL%"
if not exist "bin\yt-dlp.exe" ( echo [ERROR] Fallo al descargar yt-dlp. & goto :fail )

REM ---- 3) ffmpeg ---------------------------------------------
if exist "bin\ffmpeg.exe" (
    echo [3/3] ffmpeg ya presente, omitido.
) else (
    echo [3/3] Descargando ffmpeg... ^(grande, paciencia^)
    curl -L --fail -o "_tmp\ffmpeg.zip" "%FFURL%"
    if errorlevel 1 ( echo [ERROR] Fallo al descargar ffmpeg. & goto :fail )
    echo       Extrayendo ffmpeg.exe y ffprobe.exe...
    powershell -NoProfile -Command "Expand-Archive -Force '_tmp\ffmpeg.zip' '_tmp\ff'; Get-ChildItem -Path '_tmp\ff' -Recurse -Include ffmpeg.exe,ffprobe.exe | ForEach-Object { Copy-Item $_.FullName -Destination 'bin' -Force }"
    if not exist "bin\ffmpeg.exe" ( echo [ERROR] No se pudo extraer ffmpeg. & goto :fail )
)

REM ---- Crear lanzador (sin consola, usa pythonw) ------------
> "Iniciar YT Portable.bat" echo @echo off
>> "Iniciar YT Portable.bat" echo cd /d "%%~dp0"
>> "Iniciar YT Portable.bat" echo start "" "%%~dp0runtime\pythonw.exe" "%%~dp0app.py"

REM ---- Crear acceso directo .lnk (lanza pythonw SIN ventana) -
REM    Usa el objeto COM de PowerShell; NO ejecuta ningun .vbs.
echo [*] Creando acceso directo sin ventana...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=(Get-Location).Path; $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut((Join-Path $d 'YT Portable.lnk')); $s.TargetPath=(Join-Path $d 'runtime\pythonw.exe'); $s.Arguments='\"'+(Join-Path $d 'app.py')+'\"'; $s.WorkingDirectory=$d; $s.IconLocation=(Join-Path $d 'runtime\pythonw.exe'); $s.Save()" 2>nul
if exist "YT Portable.lnk" (
    echo       Acceso directo creado: "YT Portable.lnk"
) else (
    echo       [aviso] No se pudo crear el .lnk; usa "Iniciar YT Portable.bat".
)

REM ---- Limpieza ----------------------------------------------
rmdir /s /q "_tmp" 2>nul

echo.
echo ============================================================
echo   LISTO. Todo queda dentro de esta carpeta.
echo   Para usarlo (sin ventana de consola):
echo       doble clic en  "YT Portable.lnk"
echo   Alternativa:  "Iniciar YT Portable.bat"
echo   (Ya puedes copiar esta carpeta a un USB.)
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo   Construccion interrumpida. Revisa tu conexion e intenta de nuevo.
echo.
pause
exit /b 1

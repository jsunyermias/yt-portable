# YT Portable (Windows, Linux y macOS — autocontenido)

Descargador de **vídeo / audio de YouTube** con interfaz web local, para
**Windows, Linux y macOS**. **No requiere instalación**: en Windows no se
toca Python, ni ffmpeg, ni pip; en Linux/macOS se apoya en el `python3` que
ya trae el sistema. Todo el motor (los binarios de `yt-dlp` y `ffmpeg`/
`ffprobe`) vive dentro de esta misma carpeta y se puede llevar en un USB.

---

## Puesta en marcha (solo la primera vez)

### Windows

1. Pon `app.py` y `Setup-Portable.bat` en una carpeta vacía.
2. Doble clic en **`Setup-Portable.bat`**.
   - Descargará dentro de la carpeta: Python embebido (`runtime\`), `yt-dlp.exe` y `ffmpeg` (`bin\`).
   - Tarda unos minutos (ffmpeg es grande). No instala nada en el sistema ni pide permisos de administrador.
3. Al terminar tendrás un nuevo archivo: **`Start-Portable.bat`**, además del
   acceso directo **`YT Portable.lnk`**.

### Linux / macOS

1. Pon `app.py` y `Setup-Portable.sh` en una carpeta vacía.
2. Ejecuta **`./Setup-Portable.sh`** (la primera vez, dale permisos de
   ejecución si hace falta: `chmod +x Setup-Portable.sh`).
   - Descargará dentro de la carpeta `bin/` los binarios `yt-dlp` y
     `ffmpeg`/`ffprobe` (usando tu `python3` ya instalado, sin Python
     embebido — prácticamente toda distro de Linux y macOS ya lo trae).
3. Al terminar tendrás **`Start-Portable.sh`** y un lanzador de doble clic:
   **`YT Portable.desktop`** en Linux o **`YT Portable.command`** en macOS.

## Uso diario

- **Windows:** doble clic en **`YT Portable.lnk`** (se ejecuta **sin ventana
  de consola**); si no se pudo crear el acceso directo, usa
  **`Start-Portable.bat`**.
- **Linux / macOS:** doble clic en **`YT Portable.desktop`** /
  **`YT Portable.command`** (o ejecuta `./Start-Portable.sh`).
- Se abre solo en el navegador (`http://127.0.0.1:8765`).
- Pega el enlace, elige **Vídeo** o **Solo audio**, la calidad/formato, y pulsa **Descargar**.
- Puedes **Cancelar** una descarga en curso con el botón que aparece bajo la barra de progreso.
- Los archivos van a tu carpeta de **Descargas**: `%USERPROFILE%\Downloads`
  en Windows (o donde la hayas reubicado mediante *Propiedades → Ubicación*),
  `~/Downloads` en Linux/macOS.
- Para cerrar el programa: botón **⏻ Salir** en la interfaz (como no hay consola, esta es la forma de pararlo).

> A partir de aquí ya **no hace falta volver a ejecutar el constructor**.
> Puedes copiar toda la carpeta a otro equipo (con el mismo sistema/arquitectura)
> o a un pendrive y funcionará igual.

---

## Estructura tras construir

Windows:
```
YT Portable\
├── app.py                    # Programa (servidor + interfaz)
├── Setup-Portable.bat        # Constructor (solo 1 vez)
├── YT Portable.lnk           # Lanzador SIN ventana (lo crea el constructor)
├── Start-Portable.bat        # Lanzador alternativo
├── yt-portable.ico           # Icono de la app, extraído de app.py para el acceso directo
├── runtime\                  # Python embebido portable (incl. pythonw.exe)
└── bin\                      # yt-dlp.exe + ffmpeg.exe + ffprobe.exe + versiones
```

Linux / macOS:
```
YT Portable/
├── app.py                    # Programa (servidor + interfaz)
├── Setup-Portable.sh         # Constructor (solo 1 vez)
├── YT Portable.desktop       # Lanzador de doble clic (Linux; lo crea el constructor)
├── YT Portable.command       # Lanzador de doble clic (macOS; lo crea el constructor)
├── Start-Portable.sh         # Lanzador alternativo (./Start-Portable.sh)
├── yt-portable.ico           # Icono de la app, extraído de app.py para el lanzador
└── bin/                      # yt-dlp + ffmpeg + ffprobe + versiones
```
(Las descargas van directamente a tu carpeta de Descargas, no a esta carpeta.)

---

## Idiomas
La interfaz está disponible en 21 idiomas: español, inglés, francés, portugués, italiano, alemán, ruso, chino, japonés, coreano, hindi, bengalí, árabe, indonesio, urdu, checo, polaco, catalán, esperanto, turco y vietnamita. Se detecta automáticamente el del navegador y hay un selector arriba a la derecha que muestra el nombre nativo de cada idioma con su bandera (en SVG, para que se vean igual en cualquier navegador de Windows), con diseño de derecha a izquierda para árabe y urdu. La elección se recuerda. La documentación está solo en español e inglés.

## Opciones
- **Vídeo:** Mejor disponible / 1080p / 720p / 480p / 360p → `.mp4`
- **Audio:** MP3 / M4A / Opus / WAV / FLAC (máxima calidad)
- **Playlist completa:** activa el interruptor «Playlist completa» para descargar todos los vídeos de una lista de reproducción en lugar de solo el enlazado; verás el progreso por elemento (p. ej. `3/12`).

## Actualización automática de dependencias
El programa se mantiene al día solo, sin tocar nada en uso:

1. **Al arrancar** aplica primero cualquier actualización que se hubiera descargado la vez anterior (mueve los binarios nuevos de `_staging/` a `bin/` y **borra los viejos**), y luego inicia.
2. **Ya arrancado**, en segundo plano comprueba (como mucho **una vez al día**) si hay versión nueva de **yt-dlp** (GitHub) y de **ffmpeg** — desde gyan.dev en Windows, johnvansickle.com en Linux o evermeet.cx en macOS. Si la hay, la descarga a `_staging/`.
3. **En el siguiente arranque** esos binarios nuevos se aplican antes de iniciar.

La comprobación de red se hace solo una vez por día (se guarda la fecha en `bin/update_check.json`); el *aplicar* lo pendiente, en cambio, ocurre en cada arranque porque es instantáneo y local. La descarga de **yt-dlp se verifica siempre con su checksum SHA-256 oficial** (`SHA2-256SUMS`, el binario de cada sistema: `yt-dlp.exe` / `yt-dlp_linux` / `yt-dlp_macos`) antes de aplicarse, y se descarta si no coincide; **ffmpeg** se verifica por SHA-256 (Windows/macOS) o MD5 (Linux, el único checksum que publica johnvansickle) cuando está disponible, y en Windows además usa una segunda fuente de respaldo en GitHub si gyan.dev no responde. En la parte inferior de la interfaz verás el estado: *Buscando…*, *Actualización lista (se aplicará al reiniciar)*, *Dependencias al día* o *Comprobación ya realizada hoy*. Este sistema en dos fases evita conflictos al no poder sobrescribir un binario mientras está en uso (sobre todo en Windows). Si no hay internet o falla la comprobación, el programa sigue funcionando con lo que ya tiene.

> Sin ventana de consola (Windows): el lanzador usa `pythonw.exe`, por eso no aparece ninguna ventana negra; en Linux/macOS el lanzador arranca el proceso en segundo plano (`nohup ... &`). Cualquier mensaje o error se guarda en `app.log` para diagnóstico.
>
> El intérprete de Python (`runtime\` embebido en Windows, o el `python3` del sistema en Linux/macOS) **no** se autoactualiza (es estable y reemplazarlo en caliente no es seguro). Solo se actualizan yt-dlp y ffmpeg, que es lo que importa.

## Notas
- Solo escucha en `127.0.0.1`; no se expone a tu red.
- Por defecto descarga solo el vídeo enlazado, no toda la lista; activa «Playlist completa» en la interfaz si quieres descargarla entera.
- Si algún día YouTube cambia y yt-dlp falla, basta con sustituir `bin/yt-dlp` (o `bin\yt-dlp.exe` en Windows) por la última versión desde
  https://github.com/yt-dlp/yt-dlp/releases/latest
- Windows requiere 10/11 (usa `curl` y PowerShell, ya incluidos en el sistema). Linux/macOS requieren `python3`, `curl` o `wget`, y `tar`/`unzip` (ya presentes en prácticamente cualquier distro y en macOS).

## Solución de problemas
- **No abre nada o aparece una pestaña de error** → mira `app.log` (junto a `app.py`). Si ya hay otra instancia abierta, el programa la detecta automáticamente y abre esa misma interfaz en lugar de arrancar otra.
- **"No se pudo descargar..."** → revisa tu conexión y vuelve a ejecutar `Setup-Portable.bat` / `Setup-Portable.sh` (reanuda lo que falte).
- **El antivirus bloquea `yt-dlp.exe`** (Windows) → es un falso positivo habitual; permítelo o añádelo a excepciones.
- **No se pudo crear el `.lnk`** (Windows) → usa `Start-Portable.bat`.
- **`Setup-Portable.sh`: «Permission denied»** (Linux/macOS) → dale permisos de ejecución primero: `chmod +x Setup-Portable.sh`, y vuelve a ejecutar `./Setup-Portable.sh`.
- **`Setup-Portable.sh` dice que no encuentra python3** → instálalo con tu gestor de paquetes (`apt`, `dnf`, `pacman`, Homebrew…) y vuelve a ejecutar el script.
- **macOS dice que la app/lanzador es de un «desarrollador no identificado»** → los binarios `yt-dlp`/`ffmpeg` descargados y el `.command` generado no están firmados/notarizados; haz clic derecho → *Abrir* una vez para aprobarlos (o permítelo en *Ajustes del Sistema → Privacidad y seguridad*).
- **Las actualizaciones no se aplican** → asegúrate de cerrar el programa con el botón **Cerrar el programa** para que los binarios no estén en uso (bloqueados en Windows, o en uso en Linux/macOS).

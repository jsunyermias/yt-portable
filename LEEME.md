# YT Portable (Windows, autocontenido)

Descargador de **vídeo / audio de YouTube** con interfaz web local.
**No requiere instalar nada** en Windows: ni Python, ni ffmpeg, ni pip.
Todo el motor vive dentro de esta misma carpeta y se puede llevar en un USB.

---

## Puesta en marcha (solo la primera vez)

1. Pon `app.py` y `Setup-Portable.bat` en una carpeta vacía.
2. Doble clic en **`Setup-Portable.bat`**.
   - Descargará dentro de la carpeta: Python embebido (`runtime\`), `yt-dlp.exe` y `ffmpeg` (`bin\`).
   - Tarda unos minutos (ffmpeg es grande). No instala nada en el sistema ni pide permisos de administrador.
3. Al terminar tendrás un nuevo archivo: **`Start-Portable.bat`**.

## Uso diario

- Doble clic en **`YT Portable.lnk`** (se ejecuta **sin ventana de consola**).
  - Si tu sistema no permitiera crear el acceso directo, usa **`Start-Portable.bat`** (igual de válido).
- Se abre solo en el navegador (`http://127.0.0.1:8765`).
- Pega el enlace, elige **Vídeo** o **Solo audio**, la calidad/formato, y pulsa **Descargar**.
- Puedes **Cancelar** una descarga en curso con el botón que aparece bajo la barra de progreso.
- Los archivos aparecen en la subcarpeta `downloads\`.
- Para cerrar el programa: botón **⏻ Salir** en la interfaz (como no hay consola, esta es la forma de pararlo).

> A partir de aquí ya **no hace falta volver a ejecutar el constructor**.
> Puedes copiar toda la carpeta a otro PC o a un pendrive y funcionará igual.

---

## Estructura tras construir
```
YT Portable\
├── app.py                    # Programa (servidor + interfaz)
├── Setup-Portable.bat        # Constructor (solo 1 vez)
├── YT Portable.lnk           # Lanzador SIN ventana (lo crea el constructor)
├── Start-Portable.bat        # Lanzador alternativo
├── yt-portable.ico           # Icono de la app, extraído de app.py para el acceso directo
├── runtime\                  # Python embebido portable (incl. pythonw.exe)
├── bin\                      # yt-dlp.exe + ffmpeg.exe + ffprobe.exe + versiones
└── downloads\                # Tus descargas
```

---

## Idiomas
La interfaz está disponible en 21 idiomas: español, inglés, francés, portugués, italiano, alemán, ruso, chino, japonés, coreano, hindi, bengalí, árabe, indonesio, urdu, checo, polaco, catalán, esperanto, turco y vietnamita. Se detecta automáticamente el del navegador y hay un selector arriba a la derecha que muestra el nombre nativo de cada idioma con su bandera (en SVG, para que se vean igual en cualquier navegador de Windows), con diseño de derecha a izquierda para árabe y urdu. La elección se recuerda. La documentación está solo en español e inglés.

## Opciones
- **Vídeo:** Mejor disponible / 1080p / 720p / 480p / 360p → `.mp4`
- **Audio:** MP3 / M4A / Opus / WAV / FLAC (máxima calidad)
- **Playlist completa:** activa el interruptor «Playlist completa» para descargar todos los vídeos de una lista de reproducción en lugar de solo el enlazado; verás el progreso por elemento (p. ej. `3/12`).

## Actualización automática de dependencias
El programa se mantiene al día solo, sin tocar nada en uso:

1. **Al arrancar** aplica primero cualquier actualización que se hubiera descargado la vez anterior (mueve los binarios nuevos de `_staging\` a `bin\` y **borra los viejos**), y luego inicia.
2. **Ya arrancado**, en segundo plano comprueba (como mucho **una vez al día**) si hay versión nueva de **yt-dlp** (GitHub) y de **ffmpeg** (gyan.dev). Si la hay, la descarga a `_staging\`.
3. **En el siguiente arranque** esos binarios nuevos se aplican antes de iniciar.

La comprobación de red se hace solo una vez por día (se guarda la fecha en `bin\update_check.json`); el *aplicar* lo pendiente, en cambio, ocurre en cada arranque porque es instantáneo y local. La actualización de **yt-dlp se verifica con su checksum SHA-256 oficial** antes de aplicarse (si no coincide, se descarta); **ffmpeg** usa una segunda fuente de respaldo en GitHub si gyan.dev no responde. En la parte inferior de la interfaz verás el estado: *Buscando…*, *Actualización lista (se aplicará al reiniciar)*, *Dependencias al día* o *Comprobación ya realizada hoy*. Como en Windows no se puede sobrescribir un .exe mientras se usa, este sistema en dos fases evita conflictos. Si no hay internet o falla la comprobación, el programa sigue funcionando con lo que ya tiene.

> Sin ventana de consola: el lanzador usa `pythonw.exe`, por eso no aparece ninguna ventana negra. Cualquier mensaje o error se guarda en `app.log` para diagnóstico.
>
> El intérprete de Python (`runtime\`) **no** se autoactualiza (es estable y reemplazarlo en caliente no es seguro). Solo se actualizan yt-dlp y ffmpeg, que es lo que importa.

## Notas
- Solo escucha en `127.0.0.1`; no se expone a tu red.
- Por defecto descarga solo el vídeo enlazado, no toda la lista; activa «Playlist completa» en la interfaz si quieres descargarla entera.
- Si algún día YouTube cambia y yt-dlp falla, basta con sustituir `bin\yt-dlp.exe` por la última versión desde
  https://github.com/yt-dlp/yt-dlp/releases/latest
- Requiere Windows 10/11 (usa `curl` y PowerShell, ya incluidos en el sistema).

## Solución de problemas
- **No abre nada o aparece una pestaña de error** → mira `app.log` (junto a `app.py`). Si ya hay otra instancia abierta, el programa la detecta automáticamente y abre esa misma interfaz en lugar de arrancar otra.
- **"No se pudo descargar..."** → revisa tu conexión y vuelve a ejecutar `Setup-Portable.bat` (reanuda lo que falte).
- **El antivirus bloquea `yt-dlp.exe`** → es un falso positivo habitual; permítelo o añádelo a excepciones.
- **No se pudo crear el `.lnk`** → usa `Start-Portable.bat`.
- **Las actualizaciones no se aplican** → asegúrate de cerrar el programa con el botón **Cerrar el programa** para que los binarios no estén en uso.

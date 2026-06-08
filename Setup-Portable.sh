#!/usr/bin/env bash
# ============================================================
#   YT Portable - Setup for Linux / macOS (run ONCE)
#   Downloads yt-dlp + ffmpeg into ./bin and creates a launcher.
#   Installs nothing system-wide and needs no admin/sudo rights.
#   Uses the system's python3 (present on virtually every
#   Linux distro and on macOS).
# ============================================================
set -u
cd "$(dirname "$0")"

echo
echo "============================================================"
echo "  Setting up YT Portable... (this may take a few minutes)"
echo "============================================================"
echo

if [ ! -f "app.py" ]; then
    echo "[ERROR] Can't find app.py. Place this script next to app.py."
    exit 1
fi

# ---- 0) OS / architecture detection -------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux)  PLATFORM="linux" ;;
    Darwin) PLATFORM="macos" ;;
    *)
        echo "[ERROR] Unsupported OS: $OS (this script targets Linux and macOS)."
        exit 1
        ;;
esac

mkdir -p bin _tmp

fail() {
    echo
    echo "  Setup interrupted: $1"
    echo "  Check your connection and run this script again (it resumes what's missing)."
    echo
    exit 1
}

download() {
    # download URL DEST
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$2" "$1"
    else
        fail "neither curl nor wget is available"
    fi
}

# ---- 1) Check for python3 ------------------------------------
echo "[1/3] Checking for python3..."
if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "  [ERROR] python3 wasn't found on your system."
    echo "  Almost every Linux distro and macOS ships it already; if yours"
    echo "  doesn't, install it with your package manager, e.g.:"
    echo "    Debian/Ubuntu : sudo apt install python3"
    echo "    Fedora        : sudo dnf install python3"
    echo "    Arch          : sudo pacman -S python"
    echo "    macOS         : brew install python3   (https://brew.sh)"
    echo "  Then run this script again."
    exit 1
fi
echo "      Found: $(python3 --version)"

# ---- 2) yt-dlp ------------------------------------------------
echo "[2/3] Downloading yt-dlp..."
if [ "$PLATFORM" = "macos" ]; then
    YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
else
    YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux"
fi
download "$YTDLP_URL" "bin/yt-dlp" || fail "couldn't download yt-dlp"
chmod +x "bin/yt-dlp"
[ -x "bin/yt-dlp" ] || fail "yt-dlp download failed"

# ---- 3) ffmpeg + ffprobe --------------------------------------
if [ -x "bin/ffmpeg" ] && [ -x "bin/ffprobe" ]; then
    echo "[3/3] ffmpeg already present, skipping."
else
    echo "[3/3] Downloading ffmpeg... (please be patient)"
    if [ "$PLATFORM" = "macos" ]; then
        # evermeet.cx ships ffmpeg and ffprobe as separate signed zips
        for name in ffmpeg ffprobe; do
            info_url="https://evermeet.cx/ffmpeg/info/${name}/release"
            zip_url="$(python3 -c "
import json, urllib.request
req = urllib.request.Request('$info_url', headers={'User-Agent': 'YT-Portable-Setup'})
with urllib.request.urlopen(req, timeout=30) as r:
    print(json.load(r)['download']['zip']['url'])
" 2>/dev/null)"
            [ -n "$zip_url" ] || fail "couldn't resolve the download URL for $name"
            download "$zip_url" "_tmp/${name}.zip" || fail "couldn't download $name"
            unzip -o -q "_tmp/${name}.zip" -d "_tmp/${name}" || fail "couldn't extract $name"
            find "_tmp/${name}" -type f -name "$name" -exec cp {} "bin/$name" \;
            chmod +x "bin/$name" 2>/dev/null
        done
    else
        # johnvansickle.com static builds (single tarball with both binaries)
        case "$ARCH" in
            x86_64|amd64)   FF_ARCH="amd64" ;;
            aarch64|arm64)  FF_ARCH="arm64" ;;
            armv7l)         FF_ARCH="armhf" ;;
            armv6l)         FF_ARCH="armel" ;;
            *)              FF_ARCH="amd64" ;;
        esac
        FF_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FF_ARCH}-static.tar.xz"
        download "$FF_URL" "_tmp/ffmpeg.tar.xz" || fail "couldn't download ffmpeg"
        tar -xf "_tmp/ffmpeg.tar.xz" -C "_tmp" || fail "couldn't extract ffmpeg"
        find "_tmp" -maxdepth 2 -type f \( -name ffmpeg -o -name ffprobe \) -exec cp {} bin/ \;
        chmod +x bin/ffmpeg bin/ffprobe 2>/dev/null
    fi
    [ -x "bin/ffmpeg" ] && [ -x "bin/ffprobe" ] || fail "couldn't extract ffmpeg/ffprobe"
fi

# ---- Create the launcher (detached: keeps running if the
#      terminal is closed; logs to app.log) -------------------
cat > "Start-Portable.sh" <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
nohup python3 app.py >> app.log 2>&1 &
disown
echo "YT Portable is starting... it will open in your browser shortly."
echo "(Log: app.log — to stop it, use the 'Close the program' button in the UI.)"
EOF
chmod +x "Start-Portable.sh"

# ---- Optional desktop integration ----------------------------
if [ "$PLATFORM" = "linux" ]; then
    APP_DIR="$(pwd)"
    if [ ! -f "yt-portable.ico" ]; then
        python3 app.py --write-icon "yt-portable.ico" >/dev/null 2>&1
    fi
    cat > "YT Portable.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=YT Portable
Comment=Portable YouTube video/audio downloader
Exec=$APP_DIR/Start-Portable.sh
Path=$APP_DIR
Icon=$APP_DIR/yt-portable.ico
Terminal=false
Categories=AudioVideo;Network;
EOF
    chmod +x "YT Portable.desktop"
    # Best-effort: let the file manager treat it as trusted/launchable
    command -v gio >/dev/null 2>&1 && gio set "YT Portable.desktop" "metadata::trusted" true 2>/dev/null
    echo "      Created \"YT Portable.desktop\" (double-click to launch, or copy it to ~/Desktop / ~/.local/share/applications)."
elif [ "$PLATFORM" = "macos" ]; then
    if [ ! -f "yt-portable.ico" ]; then
        python3 app.py --write-icon "yt-portable.ico" >/dev/null 2>&1
    fi
    cat > "YT Portable.command" <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
nohup python3 app.py >> app.log 2>&1 &
disown
sleep 1
osascript -e 'tell application "Terminal" to close (every window whose name contains "YT Portable.command")' >/dev/null 2>&1 &
EOF
    chmod +x "YT Portable.command"
    echo "      Created \"YT Portable.command\" (double-click in Finder to launch)."
fi

# ---- Cleanup --------------------------------------------------
rm -rf "_tmp"

echo
echo "============================================================"
echo "  DONE. Everything stays inside this folder."
echo "  To use it:"
if [ "$PLATFORM" = "linux" ]; then
echo "      double-click  \"YT Portable.desktop\"   (or run ./Start-Portable.sh)"
else
echo "      double-click  \"YT Portable.command\"   (or run ./Start-Portable.sh)"
fi
echo "  (You can copy this whole folder to a USB stick or another machine"
echo "   with the same OS/architecture and it will keep working.)"
echo "============================================================"
echo

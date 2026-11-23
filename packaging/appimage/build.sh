#!/bin/bash
# Build dist/DriveLoader-<version>-x86_64.AppImage.
# Runs as root in an ubuntu:24.04 container (see .github/workflows/packages.yml).
# Ubuntu 24.04 sets the floor: the AppImage needs glibc 2.39 or newer on the host.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$PWD
HERE=$ROOT/packaging/appimage
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' driveloader.py)
RCLONE_VERSION=1.76.0
RCLONE_SHA256=b00f00b3266c3764413afe599d3ec316f6f43b3e0ea8a0bc1d186d2133e46df7
OUT=${OUT:-$ROOT/dist}
WORK=$(mktemp -d)
APPDIR=$WORK/AppDir
SYS=/usr/lib/x86_64-linux-gnu
APP_ID=io.github.teiwazik.DriveLoader

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    python3 python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-poppler-0.18 \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-pulseaudio \
    gstreamer1.0-libav libsoup-3.0-0 \
    librsvg2-common libgdk-pixbuf2.0-bin libglib2.0-bin adwaita-icon-theme \
    dconf-gsettings-backend shared-mime-info \
    ca-certificates curl unzip file zsync desktop-file-utils
# video preview needs the GTK 4 video sink, packaged only in some Ubuntu releases
apt-get install -y -q --no-install-recommends gstreamer1.0-gtk4 || echo "no gstreamer1.0-gtk4: video preview off"

mkdir -p "$APPDIR"/usr/{bin,lib,share}

# ---- Python with PyGObject and pycairo
PY=$(readlink -f /usr/bin/python3)          # /usr/bin/python3.12
PYVER=$(basename "$PY")
cp "$PY" "$APPDIR/usr/bin/python3"
cp -a "/usr/lib/$PYVER" "$APPDIR/usr/lib/"
( cd "$APPDIR/usr/lib/$PYVER"
  rm -rf test idlelib tkinter turtledemo ensurepip lib2to3 pydoc_data venv \
         config-*-linux-gnu site-packages dist-packages
  find . -name __pycache__ -prune -exec rm -rf {} + )
mkdir -p "$APPDIR/usr/lib/python3/dist-packages"
cp -a /usr/lib/python3/dist-packages/{gi,cairo} "$APPDIR/usr/lib/python3/dist-packages/"
find "$APPDIR/usr/lib/python3" -name __pycache__ -prune -exec rm -rf {} +

# ---- GObject introspection data the app uses
mkdir -p "$APPDIR/usr/lib/girepository-1.0"
for t in GLib GObject Gio GModule Gtk-4.0 Gdk-4.0 GdkWayland-4.0 GdkX11-4.0 Gsk-4.0 \
         Graphene Pango PangoCairo PangoFT2 HarfBuzz cairo freetype2 fontconfig xlib \
         GdkPixbuf Adw Gst GstBase GstAudio GstVideo GstPbutils GstTag GstPlay Poppler; do
    case $t in *-*) pat=$t.typelib ;; *) pat="$t-[0-9]*.typelib" ;; esac
    # shellcheck disable=SC2086
    cp "$SYS"/girepository-1.0/$pat "$APPDIR/usr/lib/girepository-1.0/" 2>/dev/null \
        || echo "no typelib $t"
done
ls "$APPDIR/usr/lib/girepository-1.0"

# ---- GStreamer plugins (audio preview; video only with the GTK 4 sink)
mkdir -p "$APPDIR/usr/lib/gstreamer-1.0"
cp "$SYS"/gstreamer-1.0/*.so "$APPDIR/usr/lib/gstreamer-1.0/"
cp "$SYS"/gstreamer1.0/gstreamer-1.0/gst-plugin-scanner "$APPDIR/usr/lib/gstreamer-1.0/"
( cd "$APPDIR/usr/lib/gstreamer-1.0"
  # capture devices, old sound systems and text-mode sinks: not needed for previews
  rm -f libgstvideo4linux2.so libgstdv.so libgst1394.so libgstcacasink.so libgstaasink.so \
        libgstshout2.so libgstjack.so libgstossaudio.so libgstoss4.so libgstximagesrc.so \
        libgstpulseaudio-device.so libgstdtsdec.so )

# ---- gdk-pixbuf loaders (SVG for the tray icon, etc.); the cache gets real paths at run time
mkdir -p "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders"
cp "$SYS"/gdk-pixbuf-2.0/2.10.0/loaders/*.so "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders/"
"$SYS"/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders "$APPDIR"/usr/lib/gdk-pixbuf-2.0/loaders/*.so \
    | sed "s|$APPDIR|@APPDIR@|g" > "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders.cache.in"
grep -q svg "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders.cache.in"

# ---- MIME database: gdk-pixbuf picks a loader by GIO's content type, so without
# it SVG icons fail ("Couldn't recognize the image file format"). XDG_DATA_DIRS
# lists the bundled copy last, so the host's own database still wins.
mkdir -p "$APPDIR/usr/share/mime"
for f in mime.cache globs globs2 magic aliases subclasses types generic-icons icons \
         XMLnamespaces treemagic version; do
    cp "/usr/share/mime/$f" "$APPDIR/usr/share/mime/" 2>/dev/null || echo "no mime/$f"
done
test -s "$APPDIR/usr/share/mime/mime.cache"

# ---- GIO modules (dconf settings backend only) and GTK settings schemas
mkdir -p "$APPDIR/usr/lib/gio/modules" "$APPDIR/usr/share/glib-2.0/schemas"
cp "$SYS"/gio/modules/libdconfsettings.so "$APPDIR/usr/lib/gio/modules/" || true
cp /usr/share/glib-2.0/schemas/org.gtk.gtk4.*.gschema.xml "$APPDIR/usr/share/glib-2.0/schemas/"
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"

# ---- icons: Adwaita (no cursors) for hosts without it, and the app's own
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a /usr/share/icons/Adwaita "$APPDIR/usr/share/icons/"
rm -rf "$APPDIR/usr/share/icons/Adwaita/cursors" "$APPDIR/usr/share/icons/Adwaita/icon-theme.cache"
cp /usr/share/icons/hicolor/index.theme "$APPDIR/usr/share/icons/hicolor/"
cp data/icons/*.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/"

# ---- the app and rclone
install -Dm644 driveloader.py "$APPDIR/usr/share/driveloader/driveloader.py"
install -d "$APPDIR/usr/share/driveloader/i18n"
install -m644 i18n/*.json "$APPDIR/usr/share/driveloader/i18n/"
curl -fsSL -o "$WORK/rclone.zip" \
    "https://downloads.rclone.org/v$RCLONE_VERSION/rclone-v$RCLONE_VERSION-linux-amd64.zip"
echo "$RCLONE_SHA256  $WORK/rclone.zip" | sha256sum -c -
unzip -q -j "$WORK/rclone.zip" "*/rclone" -d "$APPDIR/usr/bin"
chmod 755 "$APPDIR/usr/bin/rclone"

# ---- shared libraries: seeds from the typelibs and dlopen()ed ones, then all dependencies
python3 - "$APPDIR" "$HERE/excludelist" "$SYS" <<'EOF'
import os, re, shutil, subprocess, sys
appdir, exclfile, sysdir = sys.argv[1:4]
lib = os.path.join(appdir, "usr/lib")
excl = {l.split("#")[0].strip() for l in open(exclfile)} - {""}

def find(name):
    for d in (sysdir, "/lib/x86_64-linux-gnu", "/usr/lib", "/lib"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p

def copy(name, src):
    if name in excl or name.startswith(("ld-linux", "libc.so", "libm.so", "libpthread", "libdl.so", "librt.so")):
        return False
    dst = os.path.join(lib, name)
    if os.path.exists(dst):
        return False
    shutil.copy2(os.path.realpath(src), dst)
    return True

seeds = {"libsoup-3.0.so.0"}
tdir = os.path.join(lib, "girepository-1.0")
for t in os.listdir(tdir):
    data = open(os.path.join(tdir, t), "rb").read()
    for m in re.findall(rb"lib[\w.+-]+\.so(?:\.\d+)*(?:,lib[\w.+-]+\.so(?:\.\d+)*)*", data):
        seeds.update(m.decode().split(","))
for name in sorted(seeds):
    src = find(name)
    if src:
        copy(name, src)
    else:
        print("typelib library not found:", name)

def elves():
    for root, dirs, files in os.walk(appdir):
        for f in files:
            p = os.path.join(root, f)
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as fh:
                    if fh.read(4) == b"\x7fELF":
                        yield p

done, missing = set(), set()
while True:
    new = 0
    for e in list(elves()):
        if e in done or e.endswith("/usr/bin/rclone"):
            continue
        done.add(e)
        env = dict(os.environ, LD_LIBRARY_PATH=lib)
        for line in subprocess.run(["ldd", e], capture_output=True, text=True, env=env).stdout.splitlines():
            parts = line.split()
            if "=>" not in parts:
                continue
            name, target = parts[0], parts[parts.index("=>") + 1]
            if target == "not":
                missing.add(f"{name} (for {os.path.relpath(e, appdir)})")
            elif not target.startswith(lib) and copy(name, target):
                new += 1
    if not new:
        break
if missing:
    sys.exit("missing libraries:\n  " + "\n  ".join(sorted(missing)))
print(len(os.listdir(lib)), "entries in usr/lib")
EOF

# ---- AppDir root
install -m755 "$HERE/AppRun" "$APPDIR/AppRun"
sed "s|@BINDIR@/driveloader|driveloader|" data/$APP_ID.desktop.in > "$APPDIR/$APP_ID.desktop"
cp data/icons/$APP_ID.svg "$APPDIR/"
ln -s $APP_ID.svg "$APPDIR/.DirIcon"
du -sh "$APPDIR"; du -sh "$APPDIR"/usr/lib/* 2>/dev/null | sort -h | tail -5

# ---- pack
curl -fsSL -o "$WORK/appimagetool" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x "$WORK/appimagetool"
sha256sum "$WORK/appimagetool"
mkdir -p "$OUT"
cd "$OUT"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$WORK/appimagetool" --no-appstream \
    -u "gh-releases-zsync|Teiwazik|drive-loader|latest|DriveLoader-*x86_64.AppImage.zsync" \
    "$APPDIR" "DriveLoader-$VERSION-x86_64.AppImage"
ls -l "$OUT"

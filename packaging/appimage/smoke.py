"""CI smoke test for the AppImage: run with `<AppImage> --bundled-python smoke.py`
under a display. Checks the bundled libraries, plugins and certificates."""
import os
import ssl
import sys
import urllib.request

import gi

for ns, ver in [("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0"), ("GstPlay", "1.0"),
                ("Poppler", "0.18"), ("GdkPixbuf", "2.0")]:
    gi.require_version(ns, ver)
gi.require_foreign("cairo")
from gi.repository import Adw, GdkPixbuf, GLib, Gst, GstPlay, Gtk, Poppler  # noqa: E402

failed = []
print("python", sys.version.split()[0], "prefix", sys.prefix)
print("gtk", Gtk.get_major_version(), Gtk.get_minor_version(), "adw",
      Adw.get_major_version(), Adw.get_minor_version(), "poppler", Poppler.get_version())

Gst.init(None)
for name in ["playbin3", "souphttpsrc", "pulsesink", "autoaudiosink", "decodebin3",
             "mpg123audiodec", "flacdec", "vorbisdec", "opusdec", "wavparse",
             "avdec_aac", "id3demux", "qtdemux", "matroskademux"]:
    ok = Gst.ElementFactory.find(name) is not None
    print("gst", name, "ok" if ok else "MISSING")
    if not ok:
        failed.append(name)
print("gst gtk4paintablesink", Gst.ElementFactory.find("gtk4paintablesink") is not None)

icon = os.path.join(os.environ["APPDIR"], "io.github.teiwazik.DriveLoader.svg")
pb = GdkPixbuf.Pixbuf.new_from_file_at_size(icon, 32, 32)
print("svg pixbuf", pb.get_width(), "x", pb.get_height())

ctx = ssl.create_default_context()
with urllib.request.urlopen("https://www.googleapis.com/discovery/v1/apis?name=drive",
                            context=ctx, timeout=30) as r:
    print("https", r.status)


def activate(app):
    win = Adw.ApplicationWindow(application=app, title="smoke")
    win.set_content(Adw.StatusPage(icon_name="folder-download-symbolic", title="ok"))
    win.present()
    GLib.timeout_add(1500, app.quit)


app = Adw.Application(application_id="io.github.teiwazik.DriveLoader.Smoke")
app.connect("activate", activate)
rc = app.run([])
print("window rc", rc)
if failed or rc:
    sys.exit("missing: " + ", ".join(failed))
print("SMOKE OK")

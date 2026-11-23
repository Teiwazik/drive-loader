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
    # video preview: play a few test frames into the GTK 4 sink shown in the window
    sink = Gst.ElementFactory.make("gtk4paintablesink", None)
    if sink is None:
        print("gst gtk4paintablesink MISSING")
        failed.append("gtk4paintablesink")
        GLib.timeout_add(1500, app.quit)
        return
    win.set_content(Gtk.Picture(paintable=sink.get_property("paintable")))
    pipe = Gst.parse_launch("videotestsrc num-buffers=30 ! videoconvert name=c")
    pipe.add(sink)
    pipe.get_by_name("c").link(sink)
    bus = pipe.get_bus()

    def poll():
        msg = bus.pop_filtered(Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if msg is None:
            return True
        if msg.type == Gst.MessageType.ERROR:
            print("video", msg.parse_error()[0].message)
            failed.append("video playback")
        else:
            print("video frames ok")
        pipe.set_state(Gst.State.NULL)
        app.quit()
        return False

    pipe.set_state(Gst.State.PLAYING)
    GLib.timeout_add(100, poll)

    def timeout():
        failed.append("video timeout")
        app.quit()
        return False

    GLib.timeout_add_seconds(20, timeout)


app = Adw.Application(application_id="io.github.teiwazik.DriveLoader.Smoke")
app.connect("activate", activate)
rc = app.run([])
print("window rc", rc)
if failed or rc:
    sys.exit("missing: " + ", ".join(failed))
print("SMOKE OK")

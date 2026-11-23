#!/usr/bin/env python3
"""Drive Loader: a GTK4/libadwaita front-end for rclone Google Drive transfers.

Every download or upload runs as its own transient systemd user unit, so it
keeps going after the window is closed. Unfinished transfers are restarted at
login by driveloader-resume.service (or when the app starts). Several Google
accounts are supported: each one is an rclone remote of type "drive".
"""
import fcntl
import hashlib
import http.server
import json
import os
import posixpath
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

APP_ID = "io.github.teiwazik.DriveLoader"
APP_NAME = "Drive Loader"
VERSION = "1.3"
REMOTE = "gdrive"  # default / legacy remote name
HOME = Path.home()
CONF_DIR = HOME / ".config/driveloader"
STATE_DIR = HOME / ".local/state/driveloader"
THUMB_DIR = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "driveloader/thumbs"
JOBS_FILE = CONF_DIR / "jobs.json"
SETTINGS_FILE = CONF_DIR / "settings.json"
SERVICE_NAME = "driveloader-resume.service"
SERVICE_FILE = HOME / ".config/systemd/user" / SERVICE_NAME
AUTOSTART_FILE = HOME / ".config/autostart" / f"{APP_ID}.desktop"
SCRIPT = Path(__file__).resolve()
# <prefix>/share/driveloader/driveloader.py -> <prefix>/share/icons (~/.local, /usr, AppImage)
ICON_ROOT = next((d for d in (SCRIPT.parent.parent / "icons", HOME / ".local/share/icons")
                  if (d / "hicolor/scalable/apps" / f"{APP_ID}.svg").exists()),
                 HOME / ".local/share/icons")
# Inside the AppImage the mount point goes away with the process, so systemd units
# and autostart entries must call the .AppImage file itself.
APPIMAGE = os.environ.get("APPIMAGE") if os.environ.get("DRIVELOADER_BUNDLE") else None
SELF_CMD = [APPIMAGE] if APPIMAGE else [sys.executable, str(SCRIPT)]
RCLONE = (APPIMAGE and os.environ.get("DRIVELOADER_RCLONE")) or shutil.which("rclone") or "/usr/bin/rclone"
RCLONE_UNIT = [APPIMAGE, "--rclone"] if APPIMAGE else [RCLONE]

if APPIMAGE:
    # The AppImage sets library and plugin paths for its own Python. Host programs
    # (xdg-open, systemctl, soffice, ...) get the user's original environment.
    HOST_ENV = dict(os.environ)
    for _n in HOST_ENV.pop("DRIVELOADER_BUNDLE").split():
        _orig = HOST_ENV.pop("DRIVELOADER_ORIG_" + _n, None)
        if _orig is None:
            HOST_ENV.pop(_n, None)
        else:
            HOST_ENV[_n] = _orig
    for _n in ("DRIVELOADER_RCLONE", "APPIMAGE", "APPDIR", "ARGV0", "OWD"):
        HOST_ENV.pop(_n, None)
    _popen_init = subprocess.Popen.__init__

    def _host_popen(self, *args, **kw):
        if kw.get("env") is None:
            kw["env"] = HOST_ENV
        _popen_init(self, *args, **kw)
    subprocess.Popen.__init__ = _host_popen


def exec_line(args):
    """A command line for systemd ExecStart= and desktop-entry Exec=."""
    out = []
    for a in args:
        a = a.replace("%", "%%")
        out.append(a if re.fullmatch(r"[\w@%+=:,./-]+", a)
                   else '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return " ".join(out)
ICON_DOWN = "folder-download-symbolic"
ICON_UP = "document-send-symbolic"

# ---------------------------------------------------------------- i18n
# The Russian UI strings are the message ids. i18n/<code>.json next to the
# script maps them to another language; "_language" is its native name.
I18N_DIR = SCRIPT.parent / "i18n"


def system_lang():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for v in os.environ.get(var, "").split(":"):
            code = re.split(r"[_.@]", v)[0].lower()
            if code in ("c", "posix"):
                return "en"
            if code:
                return code
    return "en"


def available_langs():
    """{code: native name}; Russian is built in."""
    langs = {"ru": "Русский"}
    for f in sorted(I18N_DIR.glob("*.json")):
        try:
            langs[f.stem] = json.loads(f.read_text(encoding="utf-8")).get("_language", f.stem)
        except Exception:
            pass
    return langs


def pick_lang(choice):
    langs = available_langs()
    if choice in langs:
        return choice
    code = system_lang()
    return code if code in langs else "en" if "en" in langs else "ru"


try:
    LANG_CHOICE = json.loads(SETTINGS_FILE.read_text()).get("lang", "system")
except Exception:
    LANG_CHOICE = "system"
LANG = pick_lang(LANG_CHOICE)
try:
    _TR = {} if LANG == "ru" else json.loads((I18N_DIR / f"{LANG}.json").read_text(encoding="utf-8"))
except Exception:
    LANG, _TR = "ru", {}
if LANG != system_lang():
    os.environ["LANGUAGE"] = LANG  # GTK's own strings follow the chosen language
DECIMAL = _TR.get("_decimal", ",")


def _(msg):
    return _TR.get(msg, msg)


EXIT_CODES = {
    1: _("ошибка синтаксиса или настроек"),
    2: _("неизвестная ошибка"),
    3: _("папка не найдена (нет доступа по ссылке?)"),
    4: _("файл не найден"),
    5: _("временная ошибка — попробуйте продолжить позже"),
    6: _("часть файлов не передалась"),
    7: _("критическая ошибка (квота или лимит Google?)"),
    8: _("превышен лимит передачи"),
}

BW_OPTIONS = [(_("Без ограничений"), ""), (_("1 МБ/с"), "1M"), (_("5 МБ/с"), "5M"),
              (_("10 МБ/с"), "10M"), (_("25 МБ/с"), "25M"), (_("50 МБ/с"), "50M")]

# ---------------------------------------------------------------- storage

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def setting(key, default=None):
    return load_json(SETTINGS_FILE, {}).get(key, default)


def set_setting(key, value):
    s = load_json(SETTINGS_FILE, {})
    s[key] = value
    save_json(SETTINGS_FILE, s)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = load_json(JOBS_FILE, [])
        for j in self.jobs:  # jobs from v1.0
            j.setdefault("direction", "down")
            j.setdefault("remote", REMOTE)

    def save(self):
        with self.lock:
            save_json(JOBS_FILE, self.jobs)

    def get(self, jid):
        with self.lock:
            return next((j for j in self.jobs if j["id"] == jid), None)

    def add(self, job):
        with self.lock:
            self.jobs.append(job)
            self.save()

    def remove(self, jid):
        with self.lock:
            self.jobs = [j for j in self.jobs if j["id"] != jid]
            self.save()

# ---------------------------------------------------------------- accounts

def drive_remotes():
    """Names of all rclone remotes of type drive, i.e. the Google accounts."""
    try:
        out = subprocess.run([RCLONE, "listremotes", "--long"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "drive":
            names.append(parts[0].rstrip(":"))
    return names


def remote_config(name):
    """Config section of a remote. Contains the OAuth token: never print it."""
    try:
        out = subprocess.run([RCLONE, "config", "dump"], capture_output=True, text=True,
                             timeout=20).stdout
        return json.loads(out).get(name, {})
    except Exception:
        return {}


def fetch_account_info(name):
    """Quota via rclone (which also refreshes the token) and the account e-mail
    via the Drive API, using the account's own token."""
    info = {}
    try:
        r = subprocess.run([RCLONE, "about", f"{name}:", "--json"], capture_output=True,
                           text=True, timeout=60)
        about = json.loads(r.stdout)
        info.update(used=about.get("used"), total=about.get("total"))
    except Exception:
        pass
    cfg = remote_config(name)
    info["scope"] = cfg.get("scope") or "drive"
    try:
        token = json.loads(cfg.get("token") or "{}").get("access_token")
        if token:
            req = urllib.request.Request(
                "https://www.googleapis.com/drive/v3/about?fields=user(displayName,emailAddress)",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                user = json.loads(resp.read()).get("user", {})
            info.update(email=user.get("emailAddress"), display=user.get("displayName"))
    except Exception:
        pass
    return info


def next_remote_name(existing):
    if REMOTE not in existing:
        return REMOTE
    n = 2
    while f"{REMOTE}{n}" in existing:
        n += 1
    return f"{REMOTE}{n}"

# ---------------------------------------------------------------- jobs

def parse_link(text):
    t = text.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", t)
    if m:
        return "folder", m.group(1)
    m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", t)
    if m:
        return "file", m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", t)
    if m:
        return "folder", m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", t):
        return "folder", t
    return None, None


def unit_of(job):
    return f"driveloader-{job['id']}.service"


def state_file(job, ext):
    return STATE_DIR / f"{job['id']}.{ext}"


def log_of(job):
    return state_file(job, "log")


def exit_file_of(job):
    return state_file(job, "exit")


def unit_active(job):
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit_of(job)]).returncode == 0


def read_int(path):
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def read_exit(job):
    return read_int(exit_file_of(job))


def read_port(job):
    return read_int(state_file(job, "port")) or job.get("port")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rc(port, method, params=None, timeout=1.5):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{method}",
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def is_up(job):
    return job.get("direction") == "up"


def build_cmd(job, port):
    remote = job.get("remote", REMOTE)
    common = [
        "--rc", "--rc-addr", f"127.0.0.1:{port}", "--rc-no-auth",
        "--transfers", str(job.get("transfers", 4)), "--checkers", "8",
        "--retries", "20", "--retries-sleep", "15s", "--low-level-retries", "20",
        "--log-file", str(log_of(job)), "-v",
        "--stats", "1m", "--stats-one-line",
    ]
    if job.get("bwlimit"):
        common += ["--bwlimit", job["bwlimit"]]
    if is_up(job):
        cmd = RCLONE_UNIT + ["copy", job["src"], f"{remote}:{job['dest'].strip('/')}",
               "--check-first", "--drive-chunk-size", "32M"]
        if job.get("dest_id"):
            cmd += ["--drive-root-folder-id", job["dest_id"]]
        if job.get("files"):
            cmd += ["--files-from-raw", str(state_file(job, "files"))]
        return cmd + common
    # one stream per file, written in order and not preallocated: a paused big
    # file can then be continued from where it stopped (see prep_download)
    common += ["--multi-thread-streams", "0", "--local-no-preallocate"]
    if job["kind"] == "file":
        return RCLONE_UNIT + ["backend", "copyid", f"{remote}:", job["src"], job["dest"].rstrip("/") + "/"] + common
    return RCLONE_UNIT + ["copy", f"{remote}:", job["dest"], "--drive-root-folder-id", job["src"],
                          "--check-first", "--fast-list"] + common


def start_job(job):
    """Start (or resume) a job as a transient systemd user unit. rclone skips
    files that are already complete, so resuming continues where it stopped.
    A per-job lock keeps the GUI and the resume service from racing."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(state_file(job, "lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if unit_active(job):
            job.update(status="running", port=read_port(job))
            return True
        if is_up(job):
            if not Path(job["src"]).exists():
                job["status"] = "error"
                job["error"] = _("Не найдено: {0}").format(job['src'])
                return False
            if job.get("files"):
                state_file(job, "files").write_text("\n".join(job["files"]) + "\n")
        else:
            # leftover "<name>.<8 hex>.partial" files are continued or removed
            # by prep_download() inside the unit, before rclone starts
            Path(job["dest"]).mkdir(parents=True, exist_ok=True)
        exit_file_of(job).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit_of(job)], capture_output=True)
        port = free_port()
        verb = "Upload" if is_up(job) else "Download"
        wrapper = [
            "systemd-run", "--user", "--quiet", "--collect", f"--unit={unit_of(job)}",
            f"--description=Drive Loader {verb}: {job['name']}",
            "/bin/bash", "-c", 'f="$1"; shift; "$@"; echo $? > "$f"', "driveloader",
            str(exit_file_of(job)),
        ] if is_up(job) else [
            "systemd-run", "--user", "--quiet", "--collect", f"--unit={unit_of(job)}",
            f"--description=Drive Loader {verb}: {job['name']}",
            # $1 = exit file, then the prep command up to "--", then rclone
            "/bin/bash", "-c", 'f="$1"; shift; p=(); while [ "$1" != -- ]; do p+=("$1"); shift; done; '
            'shift; "${p[@]}"; "$@"; echo $? > "$f"',
            "driveloader", str(exit_file_of(job)), *SELF_CMD, "--prep", job["id"], str(port), "--",
        ]
        wrapper += build_cmd(job, port)
        r = subprocess.run(wrapper, capture_output=True, text=True)
        if r.returncode != 0:
            if unit_active(job):
                job.update(status="running", port=read_port(job))
                return True
            job["status"] = "error"
            job["error"] = r.stderr.strip() or _("не удалось запустить rclone")
            return False
        state_file(job, "port").write_text(str(port))
        job.update(status="running", port=port, started=time.time())
        job.pop("error", None)
        return True


def stop_job(job):
    subprocess.run(["systemctl", "--user", "stop", unit_of(job)], capture_output=True)


def cleanup_job_files(job):
    for ext in ("log", "exit", "port", "lock", "files"):
        state_file(job, ext).unlink(missing_ok=True)


def last_errors(job, n=2):
    try:
        lines = log_of(job).read_text(errors="replace").splitlines()[-500:]
    except Exception:
        return ""
    errs = [l for l in lines if " ERROR " in l or " CRITICAL " in l]
    return "\n".join(l[20:].strip()[:220] for l in errs[-n:])


def finish_job(job, code):
    """Turn an exit code into a final status. Returns True if done."""
    if code == 0:
        job["status"] = "done"
        job.pop("error", None)
        job["finished"] = time.time()
        return True
    job["status"] = "error"
    msg = EXIT_CODES.get(code, _("rclone завершился с кодом {0}").format(code))
    tail = last_errors(job)
    if is_up(job) and "insufficient" in tail.lower() and "scope" in tail.lower():
        msg = _("у аккаунта нет права записи — войдите заново без режима «только чтение»")
    if auth_expired(tail):
        msg = AUTH_EXPIRED
    job["error"] = msg + (f"\n{tail}" if tail else "")
    return False


def resume_unfinished(store):
    """Restart jobs that were running when the session ended. Returns names."""
    resumed = []
    with store.lock:
        for job in store.jobs:
            # "interrupted" = the unit died without our pause (shutdown, crash, kill)
            if job["status"] not in ("running", "interrupted") or unit_active(job):
                continue
            code = read_exit(job)
            if code is not None:
                finish_job(job, code)
            elif start_job(job):
                resumed.append(job["name"])
        store.save()
    return resumed


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total

# ---------------------------------------------------------------- partial resume

PARTIAL_RE = re.compile(r"^(.+)\.[0-9a-f]{8}\.partial$")
RESUME_MIN = 32 << 20     # smaller leftovers are cheaper to fetch again
MiB = 1 << 20
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"


def parse_time_ns(s):
    """RFC 3339 time (any fraction length) -> ns since epoch."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", s or "")
    if not m:
        return None
    base = datetime.fromisoformat(m.group(1) + (m.group(3) or "Z").replace("Z", "+00:00"))
    frac = (m.group(2) or ".")[1:]
    return int(base.timestamp()) * 10**9 + int((frac + "000000000")[:9])


def parse_rate(v):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG]?)i?B?\s*", str(v or ""), re.I)
    if not m:
        return 0
    return int(float(m.group(1)) * 1024 ** "_KMG".index((m.group(2) or "_").upper()))


class DriveAPI:
    """Tiny Drive v3 client on rclone's OAuth token. The token is read in-process
    from `rclone config dump` and is only ever sent to googleapis.com."""

    def __init__(self, remote):
        self.remote = remote
        self.token = None
        self.expiry = 0
        self.lock = threading.Lock()

    def _load(self):
        out = subprocess.run([RCLONE, "config", "dump"], capture_output=True, text=True,
                             timeout=60).stdout
        tok = json.loads(json.loads(out)[self.remote]["token"])
        self.token = tok["access_token"]
        self.expiry = (parse_time_ns(tok.get("expiry")) or 0) / 1e9

    def _headers(self, force=False):
        with self.lock:
            if self.token is None:
                self._load()
            if force or time.time() > self.expiry - 120:
                # any rclone call refreshes an expired token and saves it to the config
                subprocess.run([RCLONE, "about", f"{self.remote}:", "--json"],
                               capture_output=True, timeout=180)
                self._load()
            return {"Authorization": f"Bearer {self.token}"}

    def open(self, url, headers=None, timeout=60, tries=8):
        """GET with backoff on rate limits and server errors; returns the response."""
        force = False
        for attempt in range(tries):
            req = urllib.request.Request(url, headers={**self._headers(force), **(headers or {})})
            force = False
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                body = e.read(2000).decode(errors="replace")
                if e.code == 401 and attempt == 0:
                    force = True
                    continue
                again = e.code in (429, 500, 502, 503, 504) or (
                    e.code == 403 and "ateLimitExceeded" in body)
                if not again or attempt == tries - 1:
                    raise RuntimeError(f"HTTP {e.code}: {body[:300]}") from None
            except OSError:
                if attempt == tries - 1:
                    raise
            time.sleep(min(2 ** attempt, 60) + random.random())

    def json(self, url):
        with self.open(url) as r:
            return json.loads(r.read())

    def media(self, fid):
        return f"{DRIVE_FILES}/{fid}?alt=media&supportsAllDrives=true"


class PrepStats:
    """Answers the GUI's rclone rc polls (core/stats, core/bwlimit) on the job's
    port while partial files are being continued, before rclone itself starts."""

    def __init__(self, port, rate=0):
        self.lock = threading.Lock()
        self.total = self.done = 0
        self.files = 0
        self.cur = None               # [name, size, bytes]
        self.samples = deque(maxlen=12)
        self.rate = rate
        stats = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b"{}"
                out = {}
                if self.path == "/core/stats":
                    out = stats.snapshot()
                elif self.path == "/core/bwlimit":
                    try:
                        stats.rate = parse_rate(json.loads(body).get("rate"))
                    except Exception:
                        pass
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        try:
            self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        except (OSError, ValueError):
            self.srv = None

    def close(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    def begin(self, name, size, done=0, download=True):
        with self.lock:
            self.cur = [name, size, done]
            self.samples.clear()
            if download:
                self.files += 1
                self.total += size - done

    def add(self, n, download=True):
        with self.lock:
            self.cur[2] += n
            if download:
                self.done += n
            self.samples.append((time.monotonic(), self.cur[2]))

    def snapshot(self):
        with self.lock:
            speed = 0
            if len(self.samples) > 1:
                (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
                speed = (b1 - b0) / max(t1 - t0, 0.001)
            if self.samples and time.monotonic() - self.samples[-1][0] > 3:
                speed = 0
            tr = []
            if self.cur:
                name, size, got = self.cur
                tr = [{"name": name, "size": size, "bytes": got, "speed": speed,
                       "percentage": int(got * 100 / size) if size else 0,
                       "eta": (size - got) / speed if speed > 0 else None}]
            return {"bytes": self.done, "totalBytes": self.total, "speed": speed,
                    "transferring": tr, "transfers": 0, "totalTransfers": self.files,
                    "checks": 0, "errors": 0}


def plog(job, level, msg):
    try:
        with open(log_of(job), "a") as f:
            f.write(f"{time.strftime('%Y/%m/%d %H:%M:%S')} {level:<6}: {msg}\n")
    except OSError:
        pass


def remote_meta(job, api, final):
    """(id, size, md5, modtime_ns) of the Drive file a local path stands for."""
    if job["kind"] == "file":
        m = api.json(f"{DRIVE_FILES}/{job['src']}?supportsAllDrives=true"
                      "&fields=id,name,size,md5Checksum,modifiedTime")
        if m.get("name") != final.name:
            return None
        return m["id"], int(m.get("size", -1)), m.get("md5Checksum"), parse_time_ns(m.get("modifiedTime"))
    rel = final.relative_to(job["dest"]).as_posix()
    r = subprocess.run([RCLONE, "lsjson", f"{job.get('remote', REMOTE)}:{rel}",
                        "--drive-root-folder-id", job["src"], "--stat", "--hash", "--hash-type", "md5"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return None
    m = json.loads(r.stdout)
    return m["ID"], int(m.get("Size", -1)), (m.get("Hashes") or {}).get("md5"), parse_time_ns(m.get("ModTime"))


def same_bytes(api, fid, f, offsets, n=65536):
    for off in offsets:
        with api.open(api.media(fid), {"Range": f"bytes={off}-{off + n - 1}"}) as r:
            if r.status != 206:
                return False
            remote = r.read()
        f.seek(off)
        if f.read(n) != remote:
            return False
    return True


def resume_partial(job, api, stats, part, final):
    size = part.stat().st_size
    meta = remote_meta(job, api, final)
    if not meta:
        plog(job, "NOTICE", _("{0}: файл на Диске не найден — частичная копия удалена").format(final.name))
        part.unlink(missing_ok=True)
        return
    fid, total, md5, mtime = meta
    # A multi-thread rclone run (before v1.2) preallocated the whole file and wrote
    # four chunks at once, so the last ~256 MiB may have holes: step back past them.
    legacy = part.stat().st_blocks * 512 > size + 64 * MiB
    keep = (size - (320 * MiB if legacy else 4 * MiB)) // MiB * MiB
    if not md5 or total <= keep or keep < RESUME_MIN:
        part.unlink(missing_ok=True)
        return
    with open(part, "r+b") as f:
        offs = [keep * k // 5 for k in range(1, 5)] + [keep - 65536]
        if not same_bytes(api, fid, f, offs):
            plog(job, "NOTICE", _("{0}: частичная копия не совпала с Диском — качаем заново").format(final.name))
            f.close()
            part.unlink(missing_ok=True)
            return
        f.truncate(keep)
        plog(job, "INFO", _("{0}: докачка с {1} из {2}").format(final.name, human(keep), human(total)))
        h = hashlib.md5()
        stats.begin(_("{0} — проверка начала").format(final.name), keep, download=False)
        f.seek(0)
        while chunk := f.read(8 * MiB):
            h.update(chunk)
            stats.add(len(chunk), download=False)
        stats.begin(final.name, total, keep)
        pos, fails, t0, b0 = keep, 0, time.monotonic(), 0
        while pos < total:
            try:
                with api.open(api.media(fid), {"Range": f"bytes={pos}-"}, timeout=60) as r:
                    if r.status != 206:
                        raise RuntimeError(_("сервер не поддержал докачку (HTTP {0})").format(r.status))
                    while chunk := r.read(MiB):
                        f.write(chunk)
                        h.update(chunk)
                        pos += len(chunk)
                        stats.add(len(chunk))
                        b0 += len(chunk)
                        if stats.rate:
                            ahead = t0 + b0 / stats.rate - time.monotonic()
                            if ahead > 0:
                                time.sleep(ahead)
                        else:
                            t0, b0 = time.monotonic(), 0
                        fails = 0
            except Exception as e:
                fails += 1
                plog(job, "NOTICE", _("{0}: обрыв на {1} ({2}), повтор {3}").format(final.name, human(pos), e, fails))
                if fails > 20:
                    return
                time.sleep(min(5 * fails, 60))
        f.flush()
    if pos != total or h.hexdigest() != md5:
        plog(job, "NOTICE", _("{0}: MD5 не совпал — файл будет скачан заново").format(final.name))
        part.unlink(missing_ok=True)
        return
    os.replace(part, final)
    if mtime:
        os.utime(final, ns=(mtime, mtime))   # so rclone sees it as up to date
    plog(job, "INFO", _("{0}: докачан, MD5 совпал").format(final.name))


def prep_download(jid, port):
    """Runs inside the job's unit before rclone: continue big partial files left
    by a pause or reboot instead of downloading them again from zero."""
    job = Store().get(jid)
    if not job or is_up(job):
        return
    dest = Path(job["dest"])
    found = {}
    for p in (dest.rglob("*.partial") if job["kind"] == "folder" else dest.glob("*.partial")):
        m = PARTIAL_RE.match(p.name)
        if not m or not p.is_file():
            continue
        final = p.with_name(m.group(1))
        size = p.stat().st_size
        old = found.get(final)
        if final.exists() or size < RESUME_MIN or (old and old[1] >= size):
            p.unlink(missing_ok=True)
            continue
        if old:
            old[0].unlink(missing_ok=True)
        found[final] = (p, size)
    if not found:
        return
    api = DriveAPI(job.get("remote", REMOTE))
    stats = PrepStats(port, parse_rate(job.get("bwlimit")))
    try:
        for final, (p, _size) in sorted(found.items(), key=lambda kv: -kv[1][1]):
            try:
                resume_partial(job, api, stats, p, final)
            except Exception as e:
                plog(job, "NOTICE", _("{0}: докачка не удалась ({1})").format(final.name, e))
    finally:
        stats.close()


# ---------------------------------------------------------------- autostart

SERVICE_TEXT = f"""[Unit]
Description=Drive Loader: resume unfinished transfers

[Service]
Type=oneshot
ExecStartPre=/bin/sh -c 'for i in $$(seq 1 90); do getent hosts www.googleapis.com >/dev/null 2>&1 && exit 0; sleep 2; done; exit 0'
ExecStart={exec_line(SELF_CMD + ["--resume"])}

[Install]
WantedBy=default.target
"""


def autostart_enabled():
    return subprocess.run(["systemctl", "--user", "is-enabled", "--quiet", SERVICE_NAME],
                          capture_output=True).returncode == 0


def set_autostart(on):
    if on:
        SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SERVICE_FILE.write_text(SERVICE_TEXT)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", SERVICE_NAME], capture_output=True)


TRAY_AUTOSTART_TEXT = (
    "[Desktop Entry]\nType=Application\nName=Drive Loader (tray)\n"
    f"Exec={exec_line(SELF_CMD + ['--hidden'])}\nIcon={APP_ID}\n"
    "Terminal=false\nX-GNOME-Autostart-enabled=true\n")


def refresh_autostart():
    """Point the login entries at this copy of the app (e.g. a moved or updated AppImage)."""
    try:
        if SERVICE_FILE.exists() and SERVICE_FILE.read_text() != SERVICE_TEXT and autostart_enabled():
            set_autostart(True)
        if AUTOSTART_FILE.exists() and AUTOSTART_FILE.read_text() != TRAY_AUTOSTART_TEXT:
            set_tray_autostart(True)
    except OSError:
        pass


def tray_autostart_enabled():
    return AUTOSTART_FILE.exists()


def set_tray_autostart(on):
    if on:
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(
            TRAY_AUTOSTART_TEXT)
    else:
        AUTOSTART_FILE.unlink(missing_ok=True)

# ---------------------------------------------------------------- formatting

def human(n):
    n = float(n or 0)
    units = (_("Б"), _("КБ"), _("МБ"), _("ГБ"), _("ТБ"))
    for i, unit in enumerate(units):
        if abs(n) < 1024 or i == len(units) - 1:
            return f"{n:.0f} {unit}" if i == 0 else f"{n:.1f} {unit}".replace(".", DECIMAL)
        n /= 1024


def eta_str(sec):
    if sec is None or sec < 0:
        return "—"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h >= 24:
        return _("{0} д {1} ч").format(h // 24, h % 24)
    if h:
        return _("{0} ч {1:02d} мин").format(h, m)
    if m:
        return _("{0} мин {1:02d} с").format(m, s)
    return _("{0} с").format(s)


def plural(n, one, few, many):
    if LANG != "ru":
        return one if n == 1 else many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def status_text(job):
    st = job["status"]
    if st == "running":
        return _("Выгружается") if is_up(job) else _("Загружается")
    return {"paused": _("Пауза"), "done": _("Готово"), "error": _("Ошибка"),
            "interrupted": _("Прервано"), "queued": _("В очереди")}.get(st, st)

AUTH_EXPIRED_RU = "вход в Google истёк — войдите заново"
AUTH_EXPIRED = _(AUTH_EXPIRED_RU)
AUTH_PREFIXES = (AUTH_EXPIRED, AUTH_EXPIRED_RU)  # errors saved by another language


def auth_expired(text):
    """Google revoked the refresh token (test-mode apps: every 7 days)."""
    t = (text or "").lower()
    return ("invalid_grant" in t or "token has been expired or revoked" in t
            or "unauthorized_client" in t)  # token from another client_id than the config's


def rclone_error(err):
    """The most useful line of rclone's stderr, without the timestamp."""
    lines = [l for l in (err or "").splitlines() if l.strip()]
    pick = [l for l in lines if "ERROR" in l or "Failed" in l or "error" in l.lower()]
    line = (pick or lines or [_("неизвестная ошибка")])[-1]
    line = re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+\w+\s*:\s*", "", line)
    return line[:240]


# ---------------------------------------------------------------- tray (D-Bus)

SNI_XML = """<node><interface name="org.kde.StatusNotifierItem">
 <property name="Category" type="s" access="read"/>
 <property name="Id" type="s" access="read"/>
 <property name="Title" type="s" access="read"/>
 <property name="Status" type="s" access="read"/>
 <property name="WindowId" type="i" access="read"/>
 <property name="IconName" type="s" access="read"/>
 <property name="IconThemePath" type="s" access="read"/>
 <property name="OverlayIconName" type="s" access="read"/>
 <property name="AttentionIconName" type="s" access="read"/>
 <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
 <property name="ItemIsMenu" type="b" access="read"/>
 <property name="Menu" type="o" access="read"/>
 <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
 <signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewAttentionIcon"/>
 <signal name="NewOverlayIcon"/><signal name="NewToolTip"/>
 <signal name="NewStatus"><arg type="s"/></signal>
</interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
 <property name="Version" type="u" access="read"/>
 <property name="TextDirection" type="s" access="read"/>
 <property name="Status" type="s" access="read"/>
 <property name="IconThemePath" type="as" access="read"/>
 <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
  <arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
 <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
  <arg type="a(ia{sv})" direction="out"/></method>
 <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/>
  <arg type="v" direction="out"/></method>
 <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/>
  <arg type="v" direction="in"/><arg type="u" direction="in"/></method>
 <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
 <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
 <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/>
  <arg type="ai" direction="out"/></method>
 <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
 <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
 <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
</interface></node>"""

# ---------------------------------------------------------------- GUI

def run_gui(start_hidden=False):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

    # libadwaita 1.5 (Ubuntu 24.04, the AppImage base) has no Adw.Spinner
    if hasattr(Adw, "Spinner"):
        Spinner = Adw.Spinner
    else:
        def Spinner(**kw):
            return Gtk.Spinner(spinning=True, **kw)

    def accent_rgba(sm):
        if hasattr(sm, "get_accent_color_rgba"):  # libadwaita 1.6+
            return sm.get_accent_color_rgba()
        c = Gdk.RGBA()
        c.parse("#78aeed" if sm.get_dark() else "#3584e4")
        return c

    CSS = """
    .job-card { padding: 18px 20px; }
    .job-icon { background: alpha(@accent_bg_color, 0.16); color: @accent_color;
                border-radius: 14px; padding: 10px; }
    .job-icon.up { background: alpha(@success_bg_color, 0.18); color: @success_color; }
    progressbar.big > trough, progressbar.big > trough > progress { min-height: 10px; border-radius: 99px; }
    progressbar.thin > trough, progressbar.thin > trough > progress { min-height: 4px; border-radius: 99px; }
    .pill-status { border-radius: 99px; padding: 2px 10px; font-size: 0.82em; font-weight: 700; }
    .st-running { background: alpha(@accent_bg_color, 0.18); color: @accent_color; }
    .st-paused, .st-interrupted, .st-queued { background: alpha(@warning_bg_color, 0.22); color: @warning_color; }
    .st-done { background: alpha(@success_bg_color, 0.22); color: @success_color; }
    .st-error { background: alpha(@error_bg_color, 0.22); color: @error_color; }
    .speed { font-weight: 800; font-size: 1.12em; }
    .transfer-row { padding: 8px 12px; }
    .activity { font-size: 0.85em; }
    .file-icon { color: alpha(currentColor, 0.75); }
    .file-icon.folder { color: @accent_color; }
    .crumbs button { padding: 2px 8px; min-height: 28px; }
    .browser-footer { font-size: 0.85em; }
    .thumb { border-radius: 6px; }
    flowbox.grid > flowboxchild { border-radius: 14px; padding: 6px; }
    flowbox.grid > flowboxchild:hover { background: alpha(currentColor, 0.06); }
    .tile-pic { border-radius: 12px; background: alpha(currentColor, 0.05); color: alpha(currentColor, 0.6); }
    .tile-pic.folder { color: @accent_color; background: alpha(@accent_bg_color, 0.10); }
    .tile-name { font-weight: 600; font-size: 0.92em; }
    .tile-sub { font-size: 0.8em; }
    .drawer { background: alpha(@window_fg_color, 0.025); }
    .drawer-head { border-radius: 0; padding: 8px 16px; }
    .drawer-head .heading { font-size: 0.95em; }
    .preview-pic { background: alpha(@window_fg_color, 0.04); }
    .preview-text { font-size: 0.9em; }
    .drop-hint { border: 2px dashed alpha(@accent_color, 0.6); border-radius: 18px;
                 background: alpha(@accent_bg_color, 0.08); }
    """

    STATUSES = ("running", "paused", "done", "error", "interrupted", "queued")

    # ------------------------------------------------------------ tray

    class Tray:
        """StatusNotifierItem + com.canonical.dbusmenu, served from this process."""

        def __init__(self, app):
            self.app = app
            self.conn = app.get_dbus_connection()
            self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            self.registered = False
            self.have_name = False
            self.watcher = False
            self.rev = 1
            self.order, self.props, self.cbs = [], {}, {}
            self.icon, self.tip = APP_ID, ("", "")
            self.app_png = self._render_png(APP_ID)
            sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self.conn.register_object("/StatusNotifierItem", sni, self._sni_call, self._sni_prop, None)
            self.conn.register_object("/MenuBar", menu, self._menu_call, self._menu_prop, None)
            self._rebuild(emit=False)
            Gio.bus_own_name_on_connection(self.conn, self.bus_name, Gio.BusNameOwnerFlags.NONE,
                                           self._name_acquired, None)
            Gio.bus_watch_name_on_connection(self.conn, "org.kde.StatusNotifierWatcher",
                                             Gio.BusNameWatcherFlags.NONE,
                                             self._watcher_up, self._watcher_down)

        @staticmethod
        def _render_png(name, size=32):
            """Menu icons are looked up by the bar's own (Qt) icon theme, which does
            not see ~/.local/share/icons; send the picture itself as icon-data."""
            try:
                gi.require_version("GdkPixbuf", "2.0")
                from gi.repository import GdkPixbuf
                path = ICON_ROOT / "hicolor/scalable/apps" / f"{name}.svg"
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
                ok, data = pb.save_to_bufferv("png", [], [])
                return bytes(data) if ok else None
            except Exception:
                return None

        # registration -------------------------------------------------
        def _name_acquired(self, *_a):
            self.have_name = True
            self._register()

        def _watcher_up(self, *_a):
            self.watcher = True
            self._register()

        def _watcher_down(self, *_a):
            self.watcher = False
            self.registered = False

        def _register(self):
            if not (self.have_name and self.watcher):
                return

            def done(conn, res):
                try:
                    conn.call_finish(res)
                    self.registered = True
                except GLib.Error as e:
                    print("tray: register failed:", e.message, file=sys.stderr)

            self.conn.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                           "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                           GLib.Variant("(s)", (self.bus_name,)), None,
                           Gio.DBusCallFlags.NONE, -1, None, done)

        # StatusNotifierItem -------------------------------------------
        def _sni_prop(self, _c, _s, _p, _i, name):
            vals = {
                "Category": ("s", "ApplicationStatus"), "Id": ("s", "driveloader"),
                "Title": ("s", APP_NAME), "Status": ("s", "Active"), "WindowId": ("i", 0),
                "IconName": ("s", self.icon), "IconThemePath": ("s", str(ICON_ROOT)),
                "OverlayIconName": ("s", ""), "AttentionIconName": ("s", ""),
                "ToolTip": ("(sa(iiay)ss)", (self.icon, [], self.tip[0], self.tip[1])),
                "ItemIsMenu": ("b", False), "Menu": ("o", "/MenuBar"),
            }
            sig, val = vals.get(name, ("s", ""))
            return GLib.Variant(sig, val)

        def _sni_call(self, _c, _s, _p, _i, method, _params, inv):
            if method in ("Activate", "SecondaryActivate"):
                GLib.idle_add(self.app.toggle_window)
            inv.return_value(None)

        # dbusmenu -----------------------------------------------------
        def _menu_prop(self, _c, _s, _p, _i, name):
            return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                    "Status": GLib.Variant("s", "normal"),
                    "IconThemePath": GLib.Variant("as", [str(ICON_ROOT)])}.get(name, GLib.Variant("s", ""))

        def _vprops(self, iid, names=None):
            return {k: GLib.Variant(sig, v) for k, (sig, v) in self.props.get(iid, {}).items()
                    if not names or k in names}

        def _menu_call(self, _c, _s, _p, _i, method, params, inv):
            args = params.unpack()
            if method == "GetLayout":
                parent = args[0]
                kids = [] if parent else [GLib.Variant("(ia{sv}av)", (i, self._vprops(i), []))
                                          for i in self.order]
                props = {"children-display": GLib.Variant("s", "submenu")} if not parent else self._vprops(parent)
                inv.return_value(GLib.Variant("(u(ia{sv}av))", (self.rev, (parent, props, kids))))
            elif method == "GetGroupProperties":
                ids = args[0] or self.order
                inv.return_value(GLib.Variant("(a(ia{sv}))",
                                              ([(i, self._vprops(i, args[1])) for i in ids if i in self.props],)))
            elif method == "GetProperty":
                v = self._vprops(args[0]).get(args[1], GLib.Variant("s", ""))
                inv.return_value(GLib.Variant("(v)", (v,)))
            elif method == "Event":
                self._event(args[0], args[1])
                inv.return_value(None)
            elif method == "EventGroup":
                for iid, ev, _d, _t in args[0]:
                    self._event(iid, ev)
                inv.return_value(GLib.Variant("(ai)", ([],)))
            elif method == "AboutToShow":
                self._rebuild()
                inv.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                self._rebuild()
                inv.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                inv.return_value(None)

        def _event(self, iid, ev):
            cb = self.cbs.get(iid)
            if ev == "clicked" and cb:
                GLib.idle_add(lambda: (cb(), self._rebuild(), False)[-1])

        # updates ------------------------------------------------------
        def _rebuild(self, emit=True):
            entries = self.app.tray_menu()
            order = [e["id"] for e in entries]
            props, cbs = {}, {}
            for e in entries:
                p = {}
                if e.get("separator"):
                    p["type"] = ("s", "separator")
                else:
                    p["label"] = ("s", e["label"])
                    p["enabled"] = ("b", e.get("enabled", True))
                    if e.get("app_icon") and self.app_png:
                        p["icon-data"] = ("ay", self.app_png)
                    elif e.get("icon") or e.get("app_icon"):
                        p["icon-name"] = ("s", e.get("icon") or APP_ID)
                    if "radio" in e:
                        p["toggle-type"] = ("s", "radio")
                        p["toggle-state"] = ("i", 1 if e["radio"] else 0)
                props[e["id"]] = p
                cbs[e["id"]] = e.get("cb")
            structure = [(i, props[i].get("type"), props[i].get("toggle-type")) for i in order]
            old = [(i, self.props.get(i, {}).get("type"), self.props.get(i, {}).get("toggle-type"))
                   for i in self.order]
            changed = [i for i in order if props[i] != self.props.get(i)]
            self.order, self.props, self.cbs = order, props, cbs
            if not emit:
                return
            if structure != old:
                self.rev += 1
                self._emit("/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated",
                           GLib.Variant("(ui)", (self.rev, 0)))
            elif changed:
                self._emit("/MenuBar", "com.canonical.dbusmenu", "ItemsPropertiesUpdated",
                           GLib.Variant("(a(ia{sv})a(ias))",
                                        ([(i, self._vprops(i)) for i in changed], [])))

        def update(self):
            self._rebuild()
            icon, tip = self.app.tray_status()
            if icon != self.icon:
                self.icon = icon
                self._emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewIcon", None)
            if tip != self.tip:
                self.tip = tip
                self._emit("/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewToolTip", None)

        def _emit(self, path, iface, name, params):
            try:
                self.conn.emit_signal(None, path, iface, name, params)
            except GLib.Error:
                pass

    # ------------------------------------------------------------ widgets

    class TransferRow(Gtk.ListBoxRow):
        def __init__(self, name):
            super().__init__(activatable=False, selectable=False)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.add_css_class("transfer-row")
            self.name = Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            self.name.set_tooltip_text(name)
            hb = Gtk.Box(spacing=10)
            self.bar = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
            self.bar.add_css_class("thin")
            self.info = Gtk.Label(xalign=1)
            for c in ("caption", "numeric", "dim-label"):
                self.info.add_css_class(c)
            hb.append(self.bar)
            hb.append(self.info)
            box.append(self.name)
            box.append(hb)
            self.set_child(box)

        def update(self, t):
            size, done = t.get("size") or 0, t.get("bytes") or 0
            self.bar.set_fraction(done / size if size > 0 else 0)
            eta = t.get("eta")
            self.info.set_text(_("{0}% · {1} из {2} · {3}/с").format(t.get('percentage', 0), human(done), human(size), human(t.get('speed', 0))) + (f" · {eta_str(eta)}" if eta else ""))

    class JobCard(Gtk.Box):
        def __init__(self, win, job):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            self.win, self.jid = win, job["id"]
            self.up = is_up(job)
            self.history = deque(maxlen=120)
            self.trows = {}
            self.last_stats = None
            self.disk = None
            self.add_css_class("card")
            self.add_css_class("job-card")

            top = Gtk.Box(spacing=12)
            if self.up:
                icon_name = ICON_UP
            else:
                icon_name = ICON_DOWN if job["kind"] == "folder" else "document-save-symbolic"
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(26)
            icon.set_valign(Gtk.Align.CENTER)
            icon.add_css_class("job-icon")
            if self.up:
                icon.add_css_class("up")
            tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         valign=Gtk.Align.CENTER)
            self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            self.title.add_css_class("title-4")
            self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            self.sub.add_css_class("dim-label")
            self.sub.add_css_class("caption")
            tb.append(self.title)
            tb.append(self.sub)
            self.pill = Gtk.Label(valign=Gtk.Align.CENTER)
            self.pill.add_css_class("pill-status")

            self.btn_toggle = Gtk.Button(valign=Gtk.Align.CENTER)
            self.btn_toggle.add_css_class("circular")
            self.btn_toggle.connect("clicked", lambda *_a: win.toggle_job(self.jid))
            btn_open = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text=_("Открыть исходную папку") if self.up else _("Открыть папку"))
            btn_open.add_css_class("circular")
            btn_open.add_css_class("flat")
            btn_open.connect("clicked", lambda *_a: win.open_folder(self.jid))
            menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text=_("Ещё"))
            menu.add_css_class("circular")
            menu.add_css_class("flat")
            menu.set_popover(self._build_popover(job))

            for w in (icon, tb, self.pill, self.btn_toggle, btn_open, menu):
                top.append(w)
            self.append(top)

            self.bar = Gtk.ProgressBar()
            self.bar.add_css_class("big")
            self.append(self.bar)

            line = Gtk.Box(spacing=12)
            self.lbl_bytes = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            self.lbl_bytes.add_css_class("numeric")
            self.lbl_speed = Gtk.Label(xalign=1)
            self.lbl_speed.add_css_class("speed")
            self.lbl_speed.add_css_class("numeric")
            line.append(self.lbl_bytes)
            line.append(self.lbl_speed)
            self.append(line)

            self.lbl_files = Gtk.Label(xalign=0, wrap=True)
            self.lbl_files.add_css_class("caption")
            self.lbl_files.add_css_class("dim-label")
            self.append(self.lbl_files)

            self.err = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
            self.err.add_css_class("error")
            self.err.add_css_class("caption")
            self.append(self.err)

            self.spark = Gtk.DrawingArea(content_height=48, hexpand=True)
            self.spark.set_draw_func(self._draw)
            self.append(self.spark)

            self.exp = Gtk.Expander(label=_("Сейчас передаются"), visible=False)
            self.tlist = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=8)
            self.tlist.add_css_class("boxed-list")
            self.exp.set_child(self.tlist)
            self.append(self.exp)
            self.refresh(job)

        def _build_popover(self, job):
            pop = Gtk.Popover()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8,
                          margin_bottom=8, margin_start=8, margin_end=8)
            lbl = Gtk.Label(label=_("Ограничение скорости"), xalign=0)
            lbl.add_css_class("caption-heading")
            box.append(lbl)
            dd = Gtk.DropDown.new_from_strings([o[0] for o in BW_OPTIONS])
            vals = [o[1] for o in BW_OPTIONS]
            dd.set_selected(vals.index(job.get("bwlimit", "")) if job.get("bwlimit", "") in vals else 0)
            dd.connect("notify::selected", lambda d, _p: self.win.set_bwlimit(self.jid, vals[d.get_selected()]))
            box.append(dd)
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))

            def item(label, cb, destructive=False):
                b = Gtk.Button(label=label)
                b.add_css_class("flat")
                if destructive:
                    b.add_css_class("destructive-action")
                b.get_child().set_xalign(0)
                b.connect("clicked", lambda *_a: (pop.popdown(), cb(self.jid)))
                box.append(b)

            item(_("Перепроверить и дозалить") if self.up else _("Перепроверить и докачать"),
                 self.win.restart_job)
            item(_("Открыть на Google Диске"), self.win.open_on_drive)
            item(_("Открыть лог"), self.win.open_log)
            item(_("Убрать из списка…"), self.win.ask_remove, destructive=True)
            pop.set_child(box)
            return pop

        def _draw(self, _area, cr, w, h):
            data = list(self.history)
            if len(data) < 2:
                return
            peak = max(max(data), 1)
            sm = Adw.StyleManager.get_default()
            c = accent_rgba(sm)
            if self.up:
                c = Gdk.RGBA()
                c.parse("#26a269" if not sm.get_dark() else "#8ff0a4")
            step = w / (self.history.maxlen - 1)
            x0 = w - (len(data) - 1) * step
            pts = [(x0 + i * step, h - 2 - (h - 6) * v / peak) for i, v in enumerate(data)]
            cr.move_to(pts[0][0], h)
            for x, y in pts:
                cr.line_to(x, y)
            cr.line_to(pts[-1][0], h)
            cr.close_path()
            cr.set_source_rgba(c.red, c.green, c.blue, 0.16)
            cr.fill()
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, y)
            cr.set_source_rgba(c.red, c.green, c.blue, 0.95)
            cr.set_line_width(1.8)
            cr.stroke()

        def subtitle(self, job):
            acct = self.win.app.account_label(job.get("remote", REMOTE))
            if self.up:
                d = job["dest"].strip("/")
                where = f"{acct}: /{d}" if not job.get("dest_id") else \
                    _("{0}: папка по ссылке").format(acct) + (f" /{d}" if d else "")
                return f"{job['src']}  →  {where}"
            multi = len(self.win.app.accounts) > 1
            return job["dest"] + (f"  ·  {acct}" if multi else "")

        def refresh(self, job, stats=None):
            st = job["status"]
            self.title.set_text(job["name"])
            self.sub.set_text(self.subtitle(job))
            self.sub.set_tooltip_text(self.sub.get_text())
            for k in STATUSES:
                self.pill.remove_css_class(f"st-{k}")
            self.pill.add_css_class(f"st-{st}")
            self.pill.set_text(status_text(job))
            if st == "running":
                self.btn_toggle.set_icon_name("media-playback-pause-symbolic")
                self.btn_toggle.set_tooltip_text(_("Пауза"))
                self.btn_toggle.remove_css_class("suggested-action")
            else:
                self.btn_toggle.set_icon_name("media-playback-start-symbolic")
                self.btn_toggle.set_tooltip_text(_("Продолжить"))
                self.btn_toggle.add_css_class("suggested-action")
            self.btn_toggle.set_visible(st != "done")
            self.err.set_visible(st == "error" and bool(job.get("error")))
            self.err.set_text(job.get("error", ""))
            disk = _("на диске {0}").format(human(self.disk)) if self.disk is not None and not self.up else ""

            if stats is not None and st == "running":
                self.last_stats = stats
                self._show_stats(stats, disk)
                return
            if st == "running":
                self.history.append(0)
            s = self.last_stats or {}
            if st == "done":
                self.bar.set_fraction(1)
                self.lbl_bytes.set_text((_("Выгрузка завершена") if self.up else _("Загрузка завершена"))
                                        + (f" · {disk}" if disk else ""))
            elif st == "running":
                self.bar.pulse()
                self.lbl_bytes.set_text(_("Запуск rclone…"))
            else:
                tot = s.get("totalBytes") or 0
                self.bar.set_fraction((s.get("bytes", 0) / tot) if tot else 0)
                hint = {"paused": _("На паузе"), "interrupted": _("Прервано — нажмите ▶, чтобы продолжить"),
                        "error": _("Остановлено с ошибкой"), "queued": _("Ожидает запуска")}.get(st, "")
                self.lbl_bytes.set_text(hint + (f" · {disk}" if disk else ""))
            self.lbl_speed.set_text("")
            if st == "done":
                self.lbl_files.set_text(_("Файлы на Google Диске") if self.up else _("Сохранено в {0}").format(job['dest']))
            else:
                self.lbl_files.set_text(_("Уже выгруженные файлы при продолжении пропускаются.") if self.up
                                        else _("Уже скачанные файлы при продолжении пропускаются."))
            self.exp.set_visible(False)
            self.spark.queue_draw()

        def _show_stats(self, s, disk):
            done, total = s.get("bytes", 0), s.get("totalBytes", 0)
            speed = s.get("speed", 0) or 0
            transferring = s.get("transferring") or []
            self.history.append(speed)
            if total > 0:
                self.bar.set_fraction(min(done / total, 1))
                self.lbl_bytes.set_text(_("{0} из {1} · {2:.1f}%").format(human(done), human(total), done / total * 100).replace(".", DECIMAL))
            else:
                self.bar.pulse()
                self.lbl_bytes.set_text(_("Подготовка… проверено файлов: {0}").format(s.get('checks', 0)))
            eta = s.get("eta")
            if eta is None and speed > 0 and total > done:
                eta = (total - done) / speed
            self.lbl_speed.set_text(_("{0}/с · {1}").format(human(speed), eta_str(eta)) if transferring else "")
            parts = [_("Файлы: {0} из {1}").format(s.get('transfers', 0), s.get('totalTransfers', 0)),
                     _("проверено: {0}").format(s.get('checks', 0))]
            if disk:
                parts.append(disk)
            if self.history:
                parts.append(_("пик {0}/с").format(human(max(self.history))))
            if s.get("errors"):
                parts.append(_("ошибок: {0}").format(s['errors']))
            self.lbl_files.set_text(" · ".join(parts))
            self.lbl_files.set_tooltip_text(s.get("lastError") or None)
            self._update_transfers(transferring)
            self.spark.queue_draw()

        def _update_transfers(self, transferring):
            names = set()
            for t in transferring:
                name = t.get("name", "?")
                names.add(name)
                row = self.trows.get(name)
                if row is None:
                    row = TransferRow(name)
                    self.trows[name] = row
                    self.tlist.append(row)
                row.update(t)
            for name in list(self.trows):
                if name not in names:
                    self.tlist.remove(self.trows.pop(name))
            self.exp.set_visible(bool(transferring))
            self.exp.set_label(_("Сейчас передаются ({0})").format(len(transferring)))

    def account_row(app):
        """ComboRow to pick one of the Google accounts (defaults to the active one)."""
        names = [a["name"] for a in app.accounts]
        row = Adw.ComboRow(title=_("Аккаунт"), model=Gtk.StringList.new([app.account_label(n) for n in names]))
        if app.active in names:
            row.set_selected(names.index(app.active))
        row.names = names
        return row

    class AddDialog(Adw.Dialog):
        """New download from a Google Drive link."""

        def __init__(self, win):
            super().__init__(title=_("Новая загрузка"), content_width=540)
            self.win = win
            self.custom_dest = None
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin_top=12,
                          margin_bottom=24, margin_start=18, margin_end=18)
            grp = Adw.PreferencesGroup(description=_("Ссылка на папку или файл. "
                                                   "Уже скачанные файлы при повторе пропускаются."))
            self.link = Adw.EntryRow(title=_("Ссылка Google Drive"))
            self.link.connect("changed", self._changed)
            self.name = Adw.EntryRow(title=_("Название (необязательно)"))
            self.name.connect("changed", self._changed)
            self.dest = Adw.ActionRow(title=_("Сохранить в"))
            self.dest.add_css_class("property")
            pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text=_("Выбрать папку"))
            pick.add_css_class("flat")
            pick.connect("clicked", self._pick)
            self.dest.add_suffix(pick)
            self.dest.set_activatable_widget(pick)
            self.acct = account_row(win.app)
            self.threads = Adw.SpinRow.new_with_range(1, 16, 1)
            self.threads.set_title(_("Файлов одновременно"))
            self.threads.set_subtitle(_("Больше — быстрее на множестве мелких файлов"))
            self.threads.set_value(4)
            for r in (self.link, self.name, self.dest, self.acct, self.threads):
                grp.add(r)
            self.start = Gtk.Button(label=_("Начать загрузку"), halign=Gtk.Align.CENTER, sensitive=False)
            self.start.add_css_class("pill")
            self.start.add_css_class("suggested-action")
            self.start.connect("clicked", self._start)
            box.append(grp)
            box.append(self.start)
            tv.set_content(box)
            self.set_child(tv)
            self._changed()
            win.get_clipboard().read_text_async(None, self._clip)

        def _clip(self, clip, res):
            try:
                text = clip.read_text_finish(res) or ""
            except GLib.Error:
                return
            if "drive.google.com" in text and parse_link(text)[0] and not self.link.get_text():
                self.link.set_text(text.strip())

        def _default_dest(self):
            _kind, src = parse_link(self.link.get_text())
            name = self.name.get_text().strip()
            base = re.sub(r'[/\\:*?"<>|]', "_", name) if name else (f"drive-{src[:8]}" if src else "drive")
            return str(HOME / "Downloads" / base)

        def _changed(self, *_a):
            kind, _src = parse_link(self.link.get_text())
            ok = kind is not None
            if self.link.get_text() and not ok:
                self.link.add_css_class("error")
            else:
                self.link.remove_css_class("error")
            self.dest.set_subtitle(GLib.markup_escape_text(self.custom_dest or self._default_dest()))
            self.start.set_sensitive(ok and bool(self.acct.names))

        def _pick(self, *_a):
            fd = Gtk.FileDialog(title=_("Куда сохранить"),
                                initial_folder=Gio.File.new_for_path(str(HOME / "Downloads")))
            fd.select_folder(self.win, None, self._picked)

        def _picked(self, fd, res):
            try:
                f = fd.select_folder_finish(res)
            except GLib.Error:
                return
            if f:
                self.custom_dest = f.get_path()
                self._changed()

        def _start(self, *_a):
            kind, src = parse_link(self.link.get_text())
            dest = self.custom_dest or self._default_dest()
            name = self.name.get_text().strip() or Path(dest).name
            job = {"id": uuid.uuid4().hex[:8], "direction": "down", "name": name, "kind": kind,
                   "src": src, "dest": dest, "remote": self.acct.names[self.acct.get_selected()],
                   "transfers": int(self.threads.get_value()), "bwlimit": "",
                   "status": "queued", "created": time.time()}
            self.win.add_job(job)
            self.close()

    class RemoteBrowser(Adw.Dialog):
        """Pick a folder in "My Drive" of an account."""

        def __init__(self, win, remote, path, on_pick):
            super().__init__(title=_("Папка на Google Диске"), content_width=460, content_height=560)
            self.win, self.remote, self.on_pick = win, remote, on_pick
            self.path = path.strip("/")
            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            self.btn_up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("На уровень выше"))
            self.btn_up.connect("clicked", lambda *_a: self.go(posixpath.dirname(self.path)))
            hb.pack_start(self.btn_up)
            self.wt = Adw.WindowTitle(title=_("Мой диск"))
            hb.set_title_widget(self.wt)
            tv.add_top_bar(hb)
            self.stack = Gtk.Stack()
            self.stack.add_named(Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                             width_request=32, height_request=32), "load")
            sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            self.lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=12,
                                  margin_bottom=12, margin_start=12, margin_end=12,
                                  valign=Gtk.Align.START)
            self.lb.add_css_class("boxed-list")
            sc.set_child(self.lb)
            self.stack.add_named(sc, "list")
            self.msg = Adw.StatusPage(icon_name="folder-symbolic")
            self.stack.add_named(self.msg, "msg")
            tv.set_content(self.stack)
            bottom = Gtk.Box(spacing=8, margin_top=8, margin_bottom=12, margin_start=12, margin_end=12)
            self.newname = Gtk.Entry(placeholder_text=_("Новая папка (создастся при выгрузке)"), hexpand=True)
            choose = Gtk.Button(label=_("Выбрать"))
            choose.add_css_class("suggested-action")
            choose.connect("clicked", self._choose)
            bottom.append(self.newname)
            bottom.append(choose)
            tv.add_bottom_bar(bottom)
            self.set_child(tv)
            self.go(self.path)

        def go(self, path):
            self.path = path.strip("/")
            self.wt.set_subtitle("/" + self.path)
            self.btn_up.set_sensitive(bool(self.path))
            self.stack.set_visible_child_name("load")
            target = self.path

            def work():
                try:
                    r = subprocess.run([RCLONE, "lsjson", "--dirs-only", f"{self.remote}:{target}"],
                                       capture_output=True, text=True, timeout=60)
                    items = json.loads(r.stdout) if r.returncode == 0 else None
                except Exception:
                    items = None
                GLib.idle_add(self._show, target, items)

            threading.Thread(target=work, daemon=True).start()

        def _show(self, path, items):
            if path != self.path:
                return False
            while (row := self.lb.get_first_child()) is not None:
                self.lb.remove(row)
            if items is None:
                self.msg.set_title(_("Не удалось открыть папку"))
                self.msg.set_description(None)
                self.stack.set_visible_child_name("msg")
                return False
            if not items:
                self.msg.set_title(_("Вложенных папок нет"))
                self.msg.set_description(_("Нажмите «Выбрать», чтобы выгрузить сюда"))
                self.stack.set_visible_child_name("msg")
                return False
            for it in sorted(items, key=lambda i: i["Name"].lower()):
                row = Adw.ActionRow(title=GLib.markup_escape_text(it["Name"]), activatable=True)
                row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, n=it["Name"]: self.go(posixpath.join(self.path, n)))
                self.lb.append(row)
            self.stack.set_visible_child_name("list")
            return False

        def _choose(self, *_a):
            new = self.newname.get_text().strip().strip("/")
            self.on_pick(posixpath.join(self.path, new) if new else self.path)
            self.close()

    class UploadDialog(Adw.Dialog):
        """Upload local files or folders to an account's Google Drive."""

        def __init__(self, win, paths=None, dest=None):
            super().__init__(title=_("Выгрузка на Google Диск"), content_width=560)
            self.win, self.app = win, win.app
            self.paths = []
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin_top=12,
                          margin_bottom=24, margin_start=18, margin_end=18)

            src = Adw.PreferencesGroup(title=_("Что выгрузить"),
                                       description=_("Можно также перетащить файлы прямо в окно."))
            self.src_row = Adw.ActionRow(title=_("Ничего не выбрано"))
            self.src_row.add_css_class("property")
            b1 = Gtk.Button(label=_("Файлы…"), valign=Gtk.Align.CENTER)
            b1.connect("clicked", self._pick_files)
            b2 = Gtk.Button(label=_("Папки…"), valign=Gtk.Align.CENTER)
            b2.connect("clicked", self._pick_dirs)
            clear = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text=_("Очистить выбор"))
            clear.add_css_class("flat")
            clear.connect("clicked", lambda *_a: self.set_paths([]))
            for b in (b1, b2, clear):
                self.src_row.add_suffix(b)
            src.add(self.src_row)

            dst = Adw.PreferencesGroup(title=_("Куда"))
            self.acct = account_row(self.app)
            self.acct.connect("notify::selected", self._changed)
            self.dest = Adw.EntryRow(title=_("Папка на Диске (путь или ссылка на папку)"),
                                     text=dest if dest is not None else setting("upload_dest", "Drive Loader"))
            self.dest.connect("changed", self._changed)
            browse = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Выбрать папку на Диске"))
            browse.add_css_class("flat")
            browse.connect("clicked", self._browse)
            self.dest.add_suffix(browse)
            self.threads = Adw.SpinRow.new_with_range(1, 16, 1)
            self.threads.set_title(_("Файлов одновременно"))
            self.threads.set_value(4)
            for r in (self.acct, self.dest, self.threads):
                dst.add(r)
            self.warn = Adw.ActionRow(title=_("Аккаунт подключён только на чтение"),
                                      subtitle=_("Для выгрузки войдите заново с правом записи"),
                                      visible=False)
            self.warn.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
            relog = Gtk.Button(label=_("Войти заново"), valign=Gtk.Align.CENTER)
            relog.add_css_class("suggested-action")
            relog.connect("clicked", self._relogin)
            self.warn.add_suffix(relog)
            dst.add(self.warn)

            self.start = Gtk.Button(label=_("Начать выгрузку"), halign=Gtk.Align.CENTER, sensitive=False)
            self.start.add_css_class("pill")
            self.start.add_css_class("suggested-action")
            self.start.connect("clicked", self._start)
            for w in (src, dst, self.start):
                box.append(w)
            tv.set_content(box)
            self.set_child(tv)
            self.set_paths(paths or [])

        def remote(self):
            names = self.acct.names
            return names[self.acct.get_selected()] if names else None

        def set_paths(self, paths):
            seen = []
            for p in paths:
                if p and os.path.exists(p) and p not in seen:
                    seen.append(p)
            self.paths = seen
            n = len(self.paths)
            if not n:
                self.src_row.set_title(_("Ничего не выбрано"))
                self.src_row.set_subtitle("")
            elif n == 1:
                self.src_row.set_title(GLib.markup_escape_text(os.path.basename(self.paths[0].rstrip("/"))) +
                                       (_(" (папка)") if os.path.isdir(self.paths[0]) else ""))
                self.src_row.set_subtitle(GLib.markup_escape_text(self.paths[0]))
            else:
                dirs = sum(os.path.isdir(p) for p in self.paths)
                files = n - dirs
                parts = []
                if files:
                    parts.append(f"{files} {plural(files, _('файл'), _('файла'), _('файлов'))}")
                if dirs:
                    parts.append(f"{dirs} {plural(dirs, _('папка'), _('папки'), _('папок'))}")
                self.src_row.set_title(_(" и ").join(parts))
                self.src_row.set_subtitle(GLib.markup_escape_text(os.path.commonpath(self.paths)))
            self._changed()

        def _changed(self, *_a):
            r = self.remote()
            ro = bool(r) and "readonly" in self.app.account_scope(r)
            self.warn.set_visible(ro)
            self.start.set_sensitive(bool(self.paths) and bool(r) and not ro)

        def _pick_files(self, *_a):
            Gtk.FileDialog(title=_("Файлы для выгрузки")).open_multiple(self.win, None, self._picked, False)

        def _pick_dirs(self, *_a):
            Gtk.FileDialog(title=_("Папки для выгрузки")).select_multiple_folders(self.win, None, self._picked, True)

        def _picked(self, fd, res, dirs):
            try:
                files = fd.select_multiple_folders_finish(res) if dirs else fd.open_multiple_finish(res)
            except GLib.Error:
                return
            self.set_paths(self.paths + [f.get_path() for f in files if f.get_path()])

        def _browse(self, *_a):
            if self.remote():
                cur = self.dest.get_text().strip()
                start = "" if "google.com" in cur else cur
                RemoteBrowser(self.win, self.remote(), start, self.dest.set_text).present(self)

        def _relogin(self, *_a):
            self.app.login(self.remote(), readonly=False)
            self.close()

        def _start(self, *_a):
            remote = self.remote()
            text = self.dest.get_text().strip()
            kind, fid = parse_link(text) if "google.com" in text else (None, None)
            dest_id = fid if kind == "folder" else None
            base = "" if dest_id else text.strip("/")
            if not dest_id:
                set_setting("upload_dest", base)
            common = {"direction": "up", "remote": remote, "dest_id": dest_id,
                      "transfers": int(self.threads.get_value()), "bwlimit": "", "kind": "folder",
                      "status": "queued", "created": time.time()}
            dirs = [p.rstrip("/") or "/" for p in self.paths if os.path.isdir(p)]
            files = [p for p in self.paths if not os.path.isdir(p)]
            jobs = []
            for d in dirs:
                name = os.path.basename(d) or d
                jobs.append({**common, "id": uuid.uuid4().hex[:8], "name": name, "src": d,
                             "dest": posixpath.join(base, name)})
            if files:
                root = os.path.dirname(files[0]) if len(files) == 1 else os.path.commonpath(files)
                rel = [os.path.relpath(f, root) for f in files]
                name = os.path.basename(files[0]) + (_(" и ещё {0}").format(len(files) - 1) if len(files) > 1 else "")
                jobs.append({**common, "id": uuid.uuid4().hex[:8], "name": name, "src": root,
                             "dest": base, "files": rel})
            for job in jobs:
                self.win.add_job(job)
            self.close()

    GAPPS_ICONS = {"document": "x-office-document-symbolic", "spreadsheet": "x-office-spreadsheet-symbolic",
                   "presentation": "x-office-presentation-symbolic", "form": "x-office-document-symbolic",
                   "drawing": "image-x-generic-symbolic"}

    def item_icon(it):
        if it["IsDir"]:
            return Gio.ThemedIcon.new("folder-symbolic")
        mt = it.get("MimeType") or ""
        if mt.startswith("application/vnd.google-apps."):
            return Gio.ThemedIcon.new(GAPPS_ICONS.get(mt.rsplit(".", 1)[1], "text-x-generic-symbolic"))
        return Gio.content_type_get_symbolic_icon(Gio.content_type_from_mime_type(mt) or mt or
                                                  "application/octet-stream")

    def fmt_date(s):
        try:
            dt = GLib.DateTime.new_from_iso8601(s, None)
            return dt.to_local().format(_("%d.%m.%Y %H:%M")) if dt else ""
        except Exception:
            return ""

    def web_url(it):
        return (f"https://drive.google.com/drive/folders/{it['ID']}" if it["IsDir"]
                else f"https://drive.google.com/open?id={it['ID']}")

    TEXT_EXT = {".txt", ".md", ".log", ".ini", ".cfg", ".conf", ".json", ".xml", ".yml", ".yaml",
                ".toml", ".csv", ".tsv", ".py", ".sh", ".bat", ".ps1", ".js", ".ts", ".c", ".h", ".cpp",
                ".hpp", ".cs", ".java", ".kt", ".lua", ".rs", ".go", ".html", ".css", ".srt", ".nfo",
                ".diff", ".patch", ".sql", ".properties", ".gradle", ".glsl", ".vsh", ".fsh", ".mcmeta"}
    TEXT_MIME = {"application/json", "application/xml", "application/javascript", "application/x-sh",
                 "application/toml", "application/x-yaml", "application/sql"}

    def is_text(it):
        mt = it.get("MimeType") or ""
        return (mt.startswith("text/") or mt in TEXT_MIME or
                os.path.splitext(it["Name"])[1].lower() in TEXT_EXT)

    def item_info(it):
        size = it.get("Size", -1)
        if it["IsDir"]:
            return _("Папка")
        if size is None or size < 0:
            return _("Документ Google")
        return human(size)

    class Thumbs:
        """Drive thumbnails. One files.list call per folder gives the links (the
        OAuth token goes to googleapis.com only); the images themselves come from
        lh3.googleusercontent.com without credentials, through a disk cache."""

        LINK_TTL = 3000  # thumbnail links are short-lived signed URLs

        def __init__(self):
            self.apis = {}
            self.links = {}  # (remote, shared, path) -> (time, {fid: link})
            self.mem = OrderedDict()  # (fid, mtime, spec) -> Gdk.Texture
            self.q = queue.LifoQueue()  # newest folder first
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            for _a in range(4):
                threading.Thread(target=self._worker, daemon=True).start()
            threading.Thread(target=self._prune, daemon=True).start()

        def api(self, remote):
            if remote not in self.apis:
                self.apis[remote] = DriveAPI(remote)
            return self.apis[remote]

        def folder_links(self, remote, shared, path, fid, cb):
            """cb({file id: thumbnail link}) on the main loop, maybe from cache."""
            key = (remote, shared, path)
            hit = self.links.get(key)
            if hit and time.time() - hit[0] < self.LINK_TTL:
                cb(hit[1])
                return
            if shared and not path:
                q = "sharedWithMe=true and trashed=false"
            elif fid:
                q = f"'{fid}' in parents and trashed=false"
            else:
                return

            def work():
                links, token = {}, None
                try:
                    for _a in range(20):
                        params = {"q": q, "pageSize": "1000", "supportsAllDrives": "true",
                                  "includeItemsFromAllDrives": "true",
                                  "fields": "nextPageToken,files(id,thumbnailLink)"}
                        if token:
                            params["pageToken"] = token
                        r = self.api(remote).json(f"{DRIVE_FILES}?{urllib.parse.urlencode(params)}")
                        links.update({f["id"]: f["thumbnailLink"] for f in r.get("files", [])
                                      if f.get("thumbnailLink")})
                        token = r.get("nextPageToken")
                        if not token:
                            break
                except Exception as e:
                    print(f"thumbnail links: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    if not links:
                        return
                    token = "partial"
                if not token:
                    self.links[key] = (time.time(), links)
                GLib.idle_add(lambda: cb(links) and False)

            threading.Thread(target=work, daemon=True).start()

        def forget(self, remote=None):
            self.links = {k: v for k, v in self.links.items() if remote and k[0] != remote}

        def get(self, link, it, spec, cb, alive=lambda: True):
            """cb(texture) on the main loop. spec is an lh3 size like 's80-c' or 's1600'."""
            key = (it["ID"], it.get("ModTime", ""), spec)
            if key in self.mem:
                self.mem.move_to_end(key)
                cb(self.mem[key])
                return
            self.q.put((key, re.sub(r"=s\d+[^=/]*$", "=" + spec, link), cb, alive))

        def _worker(self):
            while True:
                key, url, cb, alive = self.q.get()
                if not alive():
                    continue
                tag = hashlib.md5(f"{key[1]}|{key[2]}".encode()).hexdigest()[:12]
                f = THUMB_DIR / f"{key[0]}-{tag}"
                tex = None
                try:
                    data = f.read_bytes() if f.exists() else None
                    if data is None and url.startswith("https://lh3.googleusercontent.com/"):
                        with urllib.request.urlopen(url, timeout=30) as r:
                            data = r.read(20 << 20)
                        tmp = f.with_suffix(".tmp")
                        tmp.write_bytes(data)
                        tmp.replace(f)
                    if data:
                        tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                except Exception:
                    tex = None
                if tex is not None:
                    def back(tex=tex, key=key, cb=cb, alive=alive):
                        self.mem[key] = tex
                        while len(self.mem) > 800:
                            self.mem.popitem(last=False)
                        if alive():
                            cb(tex)
                        return False

                    GLib.idle_add(back)

        @staticmethod
        def _prune(limit=300 << 20):
            try:
                files = sorted((f.stat().st_atime, f.stat().st_size, f) for f in THUMB_DIR.iterdir())
                total = sum(s for _a, s, _a in files)
                for _a, size, f in files:
                    if total <= limit:
                        break
                    f.unlink(missing_ok=True)
                    total -= size
            except OSError:
                pass

    class MediaProxy:
        """Lets GStreamer stream a Drive file without seeing the OAuth token: a
        127.0.0.1-only HTTP server on a random port; each URL carries a random
        per-session secret; requests (with Range) are forwarded to googleapis.com."""

        _inst = None

        @classmethod
        def get(cls, thumbs):
            if cls._inst is None:
                cls._inst = cls(thumbs)
            return cls._inst

        def __init__(self, thumbs):
            secret = uuid.uuid4().hex
            proxy = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *a):
                    pass

                def do_GET(self):
                    parts = self.path.split("/")
                    if len(parts) != 4 or parts[1] != secret:
                        self.send_error(404)
                        return
                    remote = urllib.parse.unquote(parts[2])
                    api = thumbs.api(remote)
                    hdr = {k: v for k in ("Range",) if (v := self.headers.get(k))}
                    try:
                        r = api.open(api.media(parts[3]), headers=hdr, tries=4)
                    except Exception as e:
                        code = 416 if "HTTP 416" in str(e) else 502
                        self.send_response(code)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    with r:
                        self.send_response(r.status)
                        for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                            if r.headers.get(k):
                                self.send_header(k, r.headers[k])
                        if not r.headers.get("Accept-Ranges"):
                            self.send_header("Accept-Ranges", "bytes")
                        self.end_headers()
                        try:
                            while chunk := r.read(256 << 10):
                                self.wfile.write(chunk)
                        except OSError:
                            pass  # player seeked or stopped

            self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.srv.daemon_threads = True
            self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/{secret}"
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        def url(self, remote, fid):
            return f"{self.base}/{urllib.parse.quote(remote, safe='')}/{fid}"

    _gst = {}

    def gst():
        """(Gst, GstPlay) or None when GStreamer isn't usable."""
        if "mods" not in _gst:
            try:
                gi.require_version("Gst", "1.0")
                gi.require_version("GstPlay", "1.0")
                from gi.repository import Gst, GstPlay
                Gst.init(None)
                _gst["mods"] = (Gst, GstPlay)
            except Exception as e:
                print(f"gstreamer: {e}", file=sys.stderr)
                _gst["mods"] = None
        return _gst["mods"]

    def can_play_video():
        g = gst()
        return bool(g and g[0].ElementFactory.find("gtk4paintablesink"))

    def poppler():
        if "pop" not in _gst:
            try:
                gi.require_version("Poppler", "0.18")
                gi.require_foreign("cairo")
                from gi.repository import Poppler
                _gst["pop"] = Poppler
            except Exception as e:
                print(f"poppler: {e}", file=sys.stderr)
                _gst["pop"] = None
        return _gst["pop"]

    GAPPS_PDF = {"application/vnd.google-apps." + k for k in
                 ("presentation", "document", "spreadsheet", "drawing")}
    OFFICE_EXT = {".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".key", ".doc", ".docx", ".odt", ".rtf",
                  ".xls", ".xlsx", ".ods"}

    def is_office(it):
        return os.path.splitext(it["Name"])[1].lower() in OFFICE_EXT

    def is_gapp(it):
        """Google Docs/Sheets/Slides: rclone lists them as .docx/.xlsx/.pptx with
        size -1; they can only be exported, not downloaded as they are."""
        mt = it.get("MimeType") or ""
        return mt in GAPPS_PDF or (it.get("Size") or 0) < 0 and is_office(it)

    def fmt_clock(ns):
        s = max(0, int(ns // 1_000_000_000))
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    class Player:
        """GstPlay wrapper; signals arrive on the GTK main loop."""

        def __init__(self, uri, video_sink=None, on_event=None):
            Gst, GstPlay = gst()
            self.Gst, self.on = Gst, on_event or (lambda *a: None)
            self.play = GstPlay.Play.new(None)
            pipe = self.play.get_pipeline()
            pipe.set_property("video-sink", video_sink or Gst.ElementFactory.make("fakesink", None))
            cfg = self.play.get_config()
            GstPlay.Play.config_set_position_update_interval(cfg, 250)
            self.play.set_config(cfg)
            self.ad = GstPlay.PlaySignalAdapter.new(self.play)
            for sig in ("position-updated", "duration-changed", "state-changed", "end-of-stream",
                        "error", "media-info-updated"):
                self.ad.connect(sig, lambda _a, *args, s=sig: self._emit(s, *args))
            self.play.set_uri(uri)
            self.state = None

        def _emit(self, sig, *args):
            if sig == "state-changed":
                self.state = args[0]
            self.on(sig, *args)

        def toggle(self):
            from gi.repository import GstPlay
            if self.state == GstPlay.PlayState.PLAYING:
                self.play.pause()
            else:
                self.play.play()

        def stop(self):
            self.on = lambda *a: None
            self.play.stop()

    class PreviewDialog(Adw.Dialog):
        """Look through the files of the current folder: thumbnails in big size
        (images, video frames, Google Docs), text files as text, audio/video with
        a player, PDFs and Google Slides/Docs/Sheets page by page."""

        def __init__(self, br, items, idx):
            super().__init__(content_width=940, content_height=740)
            self.br, self.items, self.idx, self.gen = br, items, idx, 0
            self.toasts = Adw.ToastOverlay()
            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            self.wtitle = Adw.WindowTitle()
            hb.set_title_widget(self.wtitle)
            nav = Gtk.Box()
            nav.add_css_class("linked")
            self.prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Предыдущий (←)"))
            self.next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Следующий (→)"))
            self.prev.connect("clicked", lambda *_a: self.step(-1))
            self.next.connect("clicked", lambda *_a: self.step(1))
            nav.append(self.prev)
            nav.append(self.next)
            hb.pack_start(nav)
            more = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text=_("Ещё"))
            m = Gio.Menu()
            m.append(_("Копировать ссылку"), "pv.link")
            m.append(_("Скачать в…"), "pv.download-to")
            more.set_menu_model(m)
            web = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text=_("Открыть в браузере"),
                             action_name="pv.web")
            dl = Gtk.Button(icon_name=ICON_DOWN, tooltip_text=_("Скачать в «Загрузки»"), action_name="pv.download")
            for w in (more, web, dl):
                hb.pack_end(w)
            tv.add_top_bar(hb)
            ag = Gio.SimpleActionGroup()
            for name, f in (("link", lambda it: self._copy(web_url(it))),
                            ("web", lambda it: self.br._open(web_url(it))),
                            ("download", self._download),
                            ("download-to", lambda it: self.br._download_to(it, None))):
                a = Gio.SimpleAction.new(name, None)
                a.connect("activate", lambda *_a, f=f: f(self.items[self.idx]))
                ag.add_action(a)
            self.insert_action_group("pv", ag)

            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
            self.stack.add_named(Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                             width_request=32, height_request=32), "load")
            self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
            self.pic.add_css_class("preview-pic")
            self.stack.add_named(self.pic, "pic")
            self.text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12,
                                     left_margin=14, right_margin=14)
            self.text.add_css_class("preview-text")
            self.stack.add_named(Gtk.ScrolledWindow(child=self.text), "text")
            self.none = Adw.StatusPage(description=_("Для этого файла предпросмотра нет"))
            nb = Gtk.Button(label=_("Открыть в браузере"), halign=Gtk.Align.CENTER, action_name="pv.web")
            nb.add_css_class("pill")
            self.none.set_child(nb)
            self.stack.add_named(self.none, "none")
            self.stack.add_named(self._media_page(), "media")
            tv.set_content(self.stack)
            self.caption = Gtk.Label(margin_top=6, margin_bottom=8, margin_start=12, margin_end=12,
                                     ellipsize=Pango.EllipsizeMode.END, hexpand=True)
            self.caption.add_css_class("dim-label")
            self.caption.add_css_class("caption")
            self.pager = Gtk.Box(visible=False, margin_end=8, valign=Gtk.Align.CENTER)
            self.pager.add_css_class("linked")
            self.pg_prev = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Предыдущая страница (↑)"))
            self.pg_next = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Следующая страница (↓)"))
            self.pg_label = Gtk.Label(width_chars=9)
            self.pg_label.add_css_class("numeric")
            self.pg_prev.connect("clicked", lambda *_a: self.page_step(-1))
            self.pg_next.connect("clicked", lambda *_a: self.page_step(1))
            for w in (self.pg_prev, self.pg_label, self.pg_next):
                w.add_css_class("flat")
                self.pager.append(w)
            bottom = Gtk.CenterBox()
            bottom.set_center_widget(self.caption)
            bottom.set_end_widget(self.pager)
            tv.add_bottom_bar(bottom)
            self.player, self.doc, self.page, self.pages = None, None, 0, {}
            self.render_lock = threading.Lock()
            self.toasts.set_child(tv)
            self.set_child(self.toasts)

            sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
            for accel, d in (("Left", -1), ("Right", 1)):
                sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(accel),
                                                 Gtk.CallbackAction.new(lambda *_a, d=d: self.step(d) or True)))
            for accel, d in (("Up", -1), ("Down", 1), ("Page_Up", -1), ("Page_Down", 1)):
                sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(accel),
                                                 Gtk.CallbackAction.new(lambda *_a, d=d: self.page_step(d))))
            self.add_controller(sc)
            # Space plays/pauses before a focused button can take it
            sp = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED,
                                        propagation_phase=Gtk.PropagationPhase.CAPTURE)
            sp.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("space"),
                                             Gtk.CallbackAction.new(lambda *_a: self._toggle_play())))
            self.add_controller(sp)
            self.connect("closed", lambda *_a: self._reset(-1))
            self.show_item()

        def _media_page(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=18,
                          margin_bottom=14, margin_start=24, margin_end=24)
            self.mview = Gtk.Stack(vexpand=True)
            self.micon = Gtk.Image(pixel_size=160, valign=Gtk.Align.CENTER)
            self.micon.add_css_class("dim-label")
            self.mcover = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
            self.mvideo = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
            self.mvideo.add_css_class("preview-pic")
            for w, n in ((self.micon, "icon"), (self.mcover, "cover"), (self.mvideo, "video")):
                self.mview.add_named(w, n)
            box.append(self.mview)
            self.mtitle = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, wrap=False)
            self.mtitle.add_css_class("title-3")
            self.martist = Gtk.Label(ellipsize=Pango.EllipsizeMode.END)
            self.martist.add_css_class("dim-label")
            box.append(self.mtitle)
            box.append(self.martist)
            row = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
            self.mplay = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text=_("Играть / пауза (Пробел)"),
                                    valign=Gtk.Align.CENTER)
            self.mplay.add_css_class("circular")
            self.mplay.add_css_class("suggested-action")
            self.mplay.connect("clicked", lambda *_a: self._toggle_play())
            self.mpos, self.mdur = Gtk.Label(label="0:00"), Gtk.Label(label="0:00")
            for l in (self.mpos, self.mdur):
                l.add_css_class("numeric")
                l.add_css_class("dim-label")
            self.mseek = Gtk.Scale(hexpand=True, draw_value=False,
                                   adjustment=Gtk.Adjustment(lower=0, upper=1, step_increment=5))
            self.mseek.connect("change-value", self._seek)
            self.mvol = Gtk.ScaleButton.new(0, 1, 0.05, ["audio-volume-muted-symbolic",
                                                         "audio-volume-high-symbolic",
                                                         "audio-volume-low-symbolic",
                                                         "audio-volume-medium-symbolic"])
            self.mvol.set_valign(Gtk.Align.CENTER)
            self.mvol.set_value(float(setting("preview_volume", 0.8)))
            self.mvol.connect("value-changed", self._volume)
            for w in (self.mplay, self.mpos, self.mseek, self.mdur, self.mvol):
                row.append(w)
            box.append(row)
            return box

        def _toast(self, text):
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

        def _copy(self, url):
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(url))
            self._toast(_("Ссылка скопирована"))

        def _download(self, it):
            self.br._download(it, HOME / "Downloads")
            self._toast(_("Скачивание «{0}» начато").format(it['Name']))

        def step(self, d):
            i = self.idx + d
            if 0 <= i < len(self.items):
                self.idx = i
                self.show_item()

        def _reset(self, gen=None):
            """Stop what the previous file left running."""
            if self.player:
                self.player.stop()
                self.player = None
            self.doc, self.page, self.pages = None, 0, {}
            self.pager.set_visible(False)
            if gen is not None:
                self.gen = gen

        def show_item(self):
            self._reset()
            self.gen += 1
            gen, it = self.gen, self.items[self.idx]
            self.prev.set_sensitive(self.idx > 0)
            self.next.set_sensitive(self.idx < len(self.items) - 1)
            date = fmt_date(it.get("ModTime", ""))
            info = item_info(it)
            self.wtitle.set_title(it["Name"])
            self.wtitle.set_subtitle(f"{info} · {date}" if date else info)
            self.caption.set_text(_("{0} из {1}").format(self.idx + 1, len(self.items)))
            alive = lambda: gen == self.gen
            mt = it.get("MimeType") or ""
            size = it.get("Size") or 0
            link = self.br.tlinks.get(it["ID"])
            self.stack.set_visible_child_name("load")
            office = is_office(it) and shutil.which("soffice") and 0 < size <= (100 << 20)
            if is_text(it) and 0 <= size <= (8 << 20):
                self._load_text(it, alive)
            elif gst() and (mt.startswith("audio/") and "midi" not in mt or mt.startswith("video/") and can_play_video()):
                self._load_media(it, link, alive)
            elif poppler() and (is_gapp(it) or mt == "application/pdf" and 0 < size <= (100 << 20)
                                or office):
                self._load_pdf(it, alive, link)
            elif link:
                note = (_("Кадр из видео — для просмотра установите gst-plugin-gtk4") if mt.startswith("video/") else
                        _("Первый слайд — полный просмотр с LibreOffice") if is_office(it) else
                        _("Первая страница") if not mt.startswith("image/") else "")
                self.br.thumbs.get(link, it, "s1600", lambda t: self._show_pic(t, note), alive)
            elif mt.startswith("image/") and 0 < size <= (30 << 20):
                self._load_image(it, alive)
            else:
                self._show_none(it)

        def _show_pic(self, tex, note=""):
            self.pic.set_paintable(tex)
            self.stack.set_visible_child_name("pic")
            if note:
                self.caption.set_text(_("{0} из {1} · {2}").format(self.idx + 1, len(self.items), note))

        def _show_none(self, it, why=None):
            theme = Gtk.IconTheme.get_for_display(self.get_display())
            self.none.set_paintable(theme.lookup_by_gicon(item_icon(it), 128, self.get_scale_factor(),
                                                          Gtk.TextDirection.NONE, 0))
            self.none.set_title(it["Name"])
            self.none.set_description(why or _("Для этого файла предпросмотра нет"))
            self.stack.set_visible_child_name("none")

        def _fetch(self, it, alive, rng, done):
            api = self.br.thumbs.api(self.br.remote)

            def work():
                try:
                    hdr = {"Range": f"bytes={rng}"} if rng else None
                    with api.open(api.media(it["ID"]), headers=hdr, tries=4) as r:
                        data, err = r.read(32 << 20), None
                except Exception as e:
                    data, err = None, str(e)
                GLib.idle_add(lambda: alive() and done(data, err) and False)

            threading.Thread(target=work, daemon=True).start()

        def _load_text(self, it, alive):
            if not it.get("Size"):
                self.text.get_buffer().set_text("")
                self.stack.set_visible_child_name("text")
                self.caption.set_text(_("{0} из {1} · файл пуст").format(self.idx + 1, len(self.items)))
                return
            limit = 256 << 10

            def done(data, err):
                if data is None:
                    self._show_none(it, _("Не удалось загрузить: {0}").format(err[:200]))
                elif b"\0" in data[:8192]:
                    self._show_none(it, _("Похоже, это не текстовый файл"))
                else:
                    self.text.get_buffer().set_text(data[:limit].decode("utf-8", errors="replace"))
                    self.stack.set_visible_child_name("text")
                    if it["Size"] > limit:
                        self.caption.set_text(_("{0} из {1} · показаны первые {2}").format(self.idx + 1, len(self.items), human(limit)))

            self._fetch(it, alive, f"0-{limit - 1}", done)

        def _load_image(self, it, alive):
            def done(data, err):
                try:
                    self._show_pic(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
                except Exception:
                    self._show_none(it, _("Не удалось показать изображение") if data else
                                    _("Не удалось загрузить: {0}").format((err or '')[:200]))

            self._fetch(it, alive, None, done)

        # ---- audio / video

        def _load_media(self, it, link, alive):
            mt = it.get("MimeType") or ""
            video = mt.startswith("video/")
            sink = None
            if video:
                Gst = gst()[0]
                sink = Gst.ElementFactory.make("gtk4paintablesink", None)
                self.mvideo.set_paintable(sink.get_property("paintable"))
                self.mview.set_visible_child_name("video")
            self.has_cover = False
            if not video:
                self.mcover.set_paintable(None)
                self.micon.set_from_gicon(item_icon(it))
                self.mview.set_visible_child_name("icon")
                if link:  # Drive sometimes has album art as the thumbnail
                    self.br.thumbs.get(link, it, "s1600", self._cover, alive)
            self.mtitle.set_text(os.path.splitext(it["Name"])[0])
            self.martist.set_text("")
            self.martist.set_visible(not video)
            self.mtitle.set_visible(not video)
            self.mseek.set_range(0, 1)
            self.mseek.set_value(0)
            self.mpos.set_text("0:00")
            self.mdur.set_text("…")
            uri = MediaProxy.get(self.br.thumbs).url(self.br.remote, it["ID"])
            self.player = Player(uri, sink, lambda sig, *a: alive() and self._media_event(it, sig, *a))
            self.player.play.set_volume(self.mvol.get_value())
            self.stack.set_visible_child_name("media")
            self.player.play.play()

        def _cover(self, tex):
            if tex and not self.has_cover:
                self.mcover.set_paintable(tex)
                self.mview.set_visible_child_name("cover")

        def _media_event(self, it, sig, *a):
            from gi.repository import GstPlay
            if sig == "position-updated":
                self.mseek.set_value(a[0] / 1e9)
                self.mpos.set_text(fmt_clock(a[0]))
            elif sig == "duration-changed" and a[0] < (1 << 63):
                self.mseek.set_range(0, max(a[0] / 1e9, 1))
                self.mdur.set_text(fmt_clock(a[0]))
            elif sig == "state-changed":
                playing = a[0] in (GstPlay.PlayState.PLAYING, GstPlay.PlayState.BUFFERING)
                self.mplay.set_icon_name("media-playback-pause-symbolic" if playing
                                         else "media-playback-start-symbolic")
            elif sig == "end-of-stream":
                self.player.play.pause()
                self.player.play.seek(0)
                self.mplay.set_icon_name("media-playback-start-symbolic")
            elif sig == "error":
                msg = a[0].message if a and hasattr(a[0], "message") else str(a[:1])
                self._reset()
                self._show_none(it, _("Не удалось воспроизвести: {0}").format(msg[:200]))
            elif sig == "media-info-updated":
                self._media_info(a[0])

        def _media_info(self, info):
            tags = info.get_tags()
            if tags:
                for tag, label in (("title", self.mtitle), ("artist", self.martist)):
                    ok, v = tags.get_string(tag)
                    if ok and v:
                        label.set_text(v)
                ok, album = tags.get_string("album")
                if ok and album and self.martist.get_text():
                    self.martist.set_text(f"{self.martist.get_text()} — {album}")
            sample = info.get_image_sample() if not self.has_cover else None
            if sample:
                buf = sample.get_buffer()
                ok, m = buf.map(gst()[0].MapFlags.READ)
                if ok:
                    try:
                        tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(bytes(m.data)))
                        self.has_cover = True
                        self.mcover.set_paintable(tex)
                        self.mview.set_visible_child_name("cover")
                    except Exception:
                        pass
                    finally:
                        buf.unmap(m)

        def _toggle_play(self):
            if not self.player:
                return False
            self.player.toggle()
            return True

        def _seek(self, _scale, _scroll, value):
            if self.player:
                self.player.play.seek(int(max(0, value) * 1e9))
            return False

        def _volume(self, _b, v):
            if self.player:
                self.player.play.set_volume(v)
            set_setting("preview_volume", round(v, 2))

        # ---- PDF, Google Slides / Docs / Sheets, office files via LibreOffice

        def _load_pdf(self, it, alive, link):
            api = self.br.thumbs.api(self.br.remote)
            mt = it.get("MimeType") or ""

            def get():
                url = (f"{DRIVE_FILES}/{it['ID']}/export?mimeType=application%2Fpdf" if is_gapp(it)
                       else api.media(it["ID"]))
                with api.open(url, tries=4) as r:
                    data = r.read((100 << 20) + 1)
                if len(data) > (100 << 20):
                    raise RuntimeError(_("файл больше 100 МБ"))
                if mt == "application/pdf" or is_gapp(it):
                    return data
                return office_to_pdf(it["Name"], data)

            def work():
                try:
                    data, err = get(), None
                    doc = poppler().Document.new_from_bytes(GLib.Bytes.new(data), None)
                except Exception as e:
                    doc, err = None, str(e)
                GLib.idle_add(lambda: alive() and self._pdf_ready(it, doc, err, link, alive) and False)

            threading.Thread(target=work, daemon=True).start()

        def _pdf_ready(self, it, doc, err, link, alive):
            if doc is None or doc.get_n_pages() == 0:
                if link:
                    self.br.thumbs.get(link, it, "s1600",
                                       lambda t: self._show_pic(t, _("Первая страница")), alive)
                else:
                    self._show_none(it, _("Не удалось открыть: {0}").format((err or _('пустой документ'))[:200]))
                return
            self.doc, self.page = doc, 0
            self.pager.set_visible(doc.get_n_pages() > 1)
            self._render(0)

        def page_step(self, d):
            if not self.doc:
                self.step(d)
                return True
            p = self.page + d
            if 0 <= p < self.doc.get_n_pages():
                self._render(p)
            return True

        def _render(self, p):
            doc, n, gen = self.doc, self.doc.get_n_pages(), self.gen
            self.page = p
            self.pg_label.set_text(f"{p + 1} / {n}")
            self.pg_prev.set_sensitive(p > 0)
            self.pg_next.set_sensitive(p < n - 1)
            self.caption.set_text(_("{0} из {1} · страница {2} из {3}").format(self.idx + 1, len(self.items), p + 1, n))
            if p in self.pages:
                self.pic.set_paintable(self.pages[p])
                self.stack.set_visible_child_name("pic")
                return

            def work():
                import cairo
                with self.render_lock:
                    if gen != self.gen:
                        return
                    page = doc.get_page(p)
                    w, h = page.get_size()
                    k = min(1800 / max(w, 1), 2000 / max(h, 1))
                    W, H = max(1, int(w * k)), max(1, int(h * k))
                    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
                    ctx = cairo.Context(surf)
                    ctx.set_source_rgb(1, 1, 1)
                    ctx.paint()
                    ctx.scale(k, k)
                    page.render(ctx)
                    surf.flush()
                    data, stride = bytes(surf.get_data()), surf.get_stride()

                def show():
                    if gen != self.gen:
                        return False
                    tex = Gdk.MemoryTexture.new(W, H, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                                GLib.Bytes.new(data), stride)
                    self.pages[p] = tex
                    while len(self.pages) > 8:
                        self.pages.pop(next(k for k in self.pages if k != p))
                    if self.page == p:
                        self.pic.set_paintable(tex)
                        self.stack.set_visible_child_name("pic")
                    return False

                GLib.idle_add(show)

            threading.Thread(target=work, daemon=True).start()

    def office_to_pdf(name, data):
        """Convert an office file with LibreOffice in a private temp dir."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="driveloader-") as tmp:
            src = Path(tmp) / ("in" + os.path.splitext(name)[1].lower())
            src.write_bytes(data)
            r = subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless",
                                "--convert-to", "pdf", "--outdir", tmp, str(src)],
                               capture_output=True, text=True, timeout=180)
            out = Path(tmp) / "in.pdf"
            if not out.exists():
                raise RuntimeError(_("LibreOffice не смог конвертировать: ") + (r.stderr or r.stdout)[-200:])
            return out.read_bytes()

    class DriveBrowser(Gtk.Box):
        """Browse an account's Google Drive: folders, files, and the usual file
        operations (new folder, rename, move, delete to trash, download, upload)."""

        def __init__(self, win):
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self.win, self.app = win, win.app
            self.remote, self.shared, self.path = None, False, ""
            self.cache, self.ids, self.by_id = {}, {}, {}
            self.items, self.gen, self.busy, self.pending = [], 0, 0, True
            self.thumbs, self.tlinks, self.slots, self.fill_gen = Thumbs(), {}, {}, 0
            self.view = setting("browser_view", "list")

            self.banner = Adw.Banner(title=_("Аккаунт подключён только на чтение: создание папок, "
                                           "выгрузка и удаление недоступны"),
                                     button_label=_("Войти с правом записи"))
            self.banner.connect("button-clicked", lambda *_a: self.remote and
                                self.app.login(self.remote, readonly=False))
            self.append(self.banner)

            bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
            self.btn_up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("На уровень выше (Alt+↑)"))
            self.btn_up.add_css_class("flat")
            self.btn_up.connect("clicked", lambda *_a: self.go_up())
            self.root = Gtk.DropDown.new_from_strings([_("Мой диск"), _("Доступные мне")])
            self.root.set_tooltip_text(_("Корень"))
            self.root.connect("notify::selected", self._root_changed)
            self.crumbs = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
            self.crumbs.add_css_class("crumbs")
            self.crumb_sc = Gtk.ScrolledWindow(hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                               hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                               child=self.crumbs)
            self.spinner = Spinner(visible=False, width_request=16, height_request=16)
            self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                               tooltip_text=_("Поиск в папке (Ctrl+F)"))
            self.search_btn.add_css_class("flat")
            self.view_btn = Gtk.Button(icon_name="view-grid-symbolic" if self.view == "list"
                                       else "view-list-symbolic", tooltip_text=_("Сетка / список (Ctrl+G)"))
            self.view_btn.add_css_class("flat")
            self.view_btn.connect("clicked", lambda *_a: self.toggle_view())
            mkdir = Gtk.Button(icon_name="folder-new-symbolic", action_name="br.new-folder",
                               tooltip_text=_("Новая папка (Ctrl+Shift+N)"))
            mkdir.add_css_class("flat")
            upload = Gtk.Button(action_name="br.upload", tooltip_text=_("Выгрузить файлы в эту папку"))
            upload.set_child(Adw.ButtonContent(icon_name=ICON_UP, label=_("Выгрузить сюда")))
            upload.add_css_class("suggested-action")
            more = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text=_("Ещё"),
                                  menu_model=self._folder_menu())
            more.add_css_class("flat")
            for w in (self.btn_up, self.root, self.crumb_sc, self.spinner, self.search_btn, self.view_btn,
                      mkdir, upload, more):
                bar.append(w)
            self.append(bar)

            self.search = Gtk.SearchEntry(placeholder_text=_("Имя файла или папки"), hexpand=True)
            self.search.connect("search-changed", lambda *_a: (self.lb.invalidate_filter(),
                                                              self.grid.invalidate_filter(), self._footer()))
            self.search.connect("stop-search", lambda *_a: self.search_btn.set_active(False))
            sclamp = Adw.Clamp(maximum_size=500, child=self.search)
            self.sbar = Gtk.SearchBar(child=sclamp, show_close_button=False)
            self.sbar.connect_entry(self.search)
            self.sbar.set_key_capture_widget(self)
            self.search_btn.bind_property("active", self.sbar, "search-mode-enabled",
                                          GObject.BindingFlags.BIDIRECTIONAL)
            self.sbar.connect("notify::search-mode-enabled",
                              lambda b, _p: b.get_search_mode() or self.search.set_text(""))
            self.append(self.sbar)

            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
            lbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            lbox.append(Spinner(halign=Gtk.Align.CENTER, width_request=32, height_request=32))
            self.load_hint = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, visible=False)
            hint = self.hint_lbl = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=48)
            hint.add_css_class("dim-label")
            self.key_btn = Gtk.Button(label=_("Настроить свой ключ — без ожидания"), halign=Gtk.Align.CENTER)
            self.key_btn.add_css_class("pill")
            self.key_btn.connect("clicked", lambda *_a: KeyDialog(self.app).present(self.win))
            self.load_hint.append(hint)
            self.load_hint.append(self.key_btn)
            lbox.append(self.load_hint)
            self.stack.add_named(lbox, "load")
            self.sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            self.lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                                  margin_top=6, margin_bottom=18, margin_start=12, margin_end=12)
            self.lb.add_css_class("boxed-list")
            self.lb.set_filter_func(self._filter)
            self.lb.connect("row-activated", lambda _l, row: self._activate(row.item))
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", self._context_menu)
            self.lb.add_controller(click)
            self.sc.set_child(Adw.Clamp(maximum_size=860, tightening_threshold=640, child=self.lb))
            self.stack.add_named(self.sc, "list")
            self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                    activate_on_single_click=True, valign=Gtk.Align.START,
                                    min_children_per_line=2, max_children_per_line=16,
                                    column_spacing=6, row_spacing=6, margin_top=8, margin_bottom=18,
                                    margin_start=12, margin_end=12)
            self.grid.add_css_class("grid")
            self.grid.set_filter_func(lambda ch: self._filter(ch))
            self.grid.connect("child-activated", lambda _g, ch: self._activate(ch.item))
            gclick = Gtk.GestureClick(button=3)
            gclick.connect("pressed", self._context_menu)
            self.grid.add_controller(gclick)
            self.gsc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.grid)
            self.stack.add_named(self.gsc, "grid")
            self.msg = Adw.StatusPage()
            self.msg_btn = Gtk.Button(halign=Gtk.Align.CENTER)
            self.msg_btn.add_css_class("pill")
            self.msg_btn.connect("clicked", lambda *_a: self._msg_cb and self._msg_cb())
            self.msg.set_child(self.msg_btn)
            self._msg_cb = None
            self.stack.add_named(self.msg, "msg")
            self.append(self.stack)

            foot = Gtk.Box(spacing=12, margin_top=4, margin_bottom=8, margin_start=16, margin_end=16)
            foot.add_css_class("browser-footer")
            self.count_lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            self.quota_lbl = Gtk.Label(xalign=1)
            for w in (self.count_lbl, self.quota_lbl):
                w.add_css_class("dim-label")
                w.add_css_class("numeric")
                foot.append(w)
            self.append(foot)

            self._setup_actions()
            sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
            for accel, cb in (("<Alt>Up", self.go_up), ("F5", lambda: self.load(force=True)),
                              ("<Ctrl>r", lambda: self.load(force=True)),
                              ("<Ctrl>f", lambda: self.search_btn.set_active(not self.search_btn.get_active())),
                              ("<Ctrl>g", self.toggle_view),
                              ("<Ctrl><Shift>n", lambda: self.actions.activate_action("new-folder", None))):
                sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(accel),
                                                 Gtk.CallbackAction.new(self._shortcut, cb)))
            self.add_controller(sc)
            self.connect("map", lambda *_a: self.pending and self.load())

        # ---------------------------------------------------------- helpers
        def _shortcut(self, _w, _args, cb):
            if not self.get_mapped():
                return False
            cb()
            return True

        def writable(self):
            return bool(self.remote) and "readonly" not in self.app.account_scope(self.remote)

        def can_upload(self):
            return self.writable() and not (self.shared and not self.path)

        def upload_dest(self):
            """Destination text for UploadDialog: a path in My Drive, a folder link otherwise."""
            if not self.shared:
                return "/" + self.path
            fid = self.ids.get((True, self.path))
            return f"https://drive.google.com/drive/folders/{fid}" if fid else None

        def rpath(self, path):
            return f"{self.remote}:{path}"

        def _run(self, args, done, timeout=600):
            cmd = [RCLONE] + args + (["--drive-shared-with-me"] if self.shared else [])
            self._busy(1)

            def work():
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                    res = (r.returncode, r.stdout, r.stderr)
                except Exception as e:
                    res = (-1, "", str(e))

                def back():
                    self._busy(-1)
                    done(*res)
                    return False

                GLib.idle_add(back)

            threading.Thread(target=work, daemon=True).start()

        def _busy(self, d):
            self.busy += d
            self.spinner.set_visible(self.busy > 0)

        def _toast(self, text):
            self.win.toast(text)

        def _copy(self, text):
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
            self._toast(_("Ссылка скопирована"))

        def _open(self, url):
            subprocess.Popen(["xdg-open", url])

        def _item(self, fid):
            return self.by_id.get(fid, (None, None))

        # ---------------------------------------------------------- state
        def set_remote(self, name):
            if name != self.remote:
                self.remote, self.path = name, ""
                self.thumbs.forget(name)
                self.cache.clear()
                self.ids.clear()
                self.by_id.clear()
                if self.get_mapped():
                    self.load()
                else:
                    self.pending = True
            self._update_perm()
            self._footer()

        def _update_perm(self):
            w = self.writable()
            self.banner.set_revealed(bool(self.remote) and not w)
            for name in ("rename", "delete", "new-folder"):
                self.actions.lookup_action(name).set_enabled(w)
            self.actions.lookup_action("move").set_enabled(w and not self.shared)
            self.actions.lookup_action("upload").set_enabled(self.can_upload())
            self.actions.lookup_action("size-here").set_enabled(bool(self.path) or not self.shared)

        def _root_changed(self, *_a):
            shared = self.root.get_selected() == 1
            if shared != self.shared:
                self.shared = shared
                self.load("")

        def changed(self):
            """Something on Drive changed (an upload finished, a write op ran)."""
            self.cache.clear()
            self.thumbs.forget()
            if self.get_mapped():
                self.load(force=True, quiet=True)
            else:
                self.pending = True

        def go_up(self):
            if self.path:
                self.load(posixpath.dirname(self.path))

        def load(self, path=None, force=False, quiet=False):
            self.pending = False
            if path is not None:
                if path.strip("/") != self.path:
                    self.search_btn.set_active(False)
                self.path = path.strip("/")
            self._crumbs()
            self._update_perm()
            if not self.remote:
                self._show_msg("avatar-default-symbolic", _("Нет аккаунта Google"),
                               _("Войдите, чтобы видеть свои файлы на Диске"), _("Войти"),
                               lambda: self.app.login(None))
                return
            key = (self.remote, self.shared, self.path)
            if force:
                self.thumbs.links.pop(key, None)
            if not force and key in self.cache:
                self._fill(self.cache[key])
                return
            self.gen += 1
            gen, path = self.gen, self.path
            if not quiet:
                self.load_hint.set_visible(False)
                self.stack.set_visible_child_name("load")
                GLib.timeout_add_seconds(6, lambda: (gen == self.gen and self.busy > 0
                                                     and self._slow_hint()) and False)

            def done(code, out, err):
                if gen != self.gen:
                    return
                try:
                    items = json.loads(out) if code == 0 else None
                except ValueError:
                    items = None
                if items is None:
                    if auth_expired(err):
                        self._show_msg("dialog-password-symbolic", _("Вход в Google истёк"),
                                       _("Google сбрасывает вход раз в 7 дней, пока приложение в режиме "
                                       "теста. Войдите заново — это полминуты."), _("Войти заново"),
                                       lambda: self.app.relogin(self.remote))
                    elif path:
                        self._show_msg("folder-visit-symbolic", _("Не удалось открыть папку"),
                                       rclone_error(err), _("В корень"), lambda: self.load(""))
                    else:
                        self._show_msg("network-error-symbolic", _("Не удалось получить список файлов"),
                                       rclone_error(err), _("Повторить"), lambda: self.load(force=True))
                    return
                self.cache[(self.remote, self.shared, path)] = items
                self._fill(items)

            self._run(["lsjson", self.rpath(self.path)], done, timeout=180)

        def _show_msg(self, icon, title, desc, btn, cb):
            self.msg.set_icon_name(icon)
            self.msg.set_title(title)
            self.msg.set_description(GLib.markup_escape_text(desc) if desc else None)
            self.msg_btn.set_label(btn or "")
            self.msg_btn.set_visible(bool(btn))
            self._msg_cb = cb
            self.stack.set_visible_child_name("msg")
            self.items = []
            self._footer()

        def _crumbs(self):
            while (c := self.crumbs.get_first_child()) is not None:
                self.crumbs.remove(c)
            home = Gtk.Button(icon_name="go-home-symbolic", tooltip_text=_("Корень"))
            home.connect("clicked", lambda *_a: self.load(""))
            home.add_css_class("flat")
            self.crumbs.append(home)
            acc = ""
            parts = self.path.split("/") if self.path else []
            for i, part in enumerate(parts):
                acc = posixpath.join(acc, part)
                sep = Gtk.Image.new_from_icon_name("go-next-symbolic")
                sep.add_css_class("dim-label")
                self.crumbs.append(sep)
                b = Gtk.Button(label=part, tooltip_text=part)
                b.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                b.get_child().set_max_width_chars(24)
                b.add_css_class("flat")
                if i == len(parts) - 1:
                    b.add_css_class("heading")
                b.connect("clicked", lambda _b, p=acc: self.load(p, force=p == self.path))
                self.crumbs.append(b)
            self.btn_up.set_sensitive(bool(self.path))

            def to_end():
                adj = self.crumb_sc.get_hadjustment()
                adj.set_value(adj.get_upper())
                return False

            GLib.timeout_add(50, to_end)

        # ---------------------------------------------------------- list
        def _sort_key(self):
            mode = self.actions.get_action_state("sort").get_string()
            if mode == "date":
                return lambda it: (not it["IsDir"], [-ord(c) for c in it.get("ModTime", "")])
            if mode == "size":
                return lambda it: (not it["IsDir"], -(it.get("Size") or 0), it["Name"].lower())
            return lambda it: (not it["IsDir"], it["Name"].lower())

        def _fill(self, items):
            self.items = items
            for it in items:
                full = posixpath.join(self.path, it["Name"])
                self.by_id[it["ID"]] = (it, full)
                if it["IsDir"]:
                    self.ids[(self.shared, full)] = it["ID"]
            self.lb.remove_all()
            self.grid.remove_all()
            self.fill_gen += 1
            self.slots, self.tlinks = {}, {}
            if not items:
                if self.can_upload():
                    self._show_msg("folder-symbolic", _("Папка пуста"),
                                   _("Перетащите сюда файлы или нажмите «Выгрузить сюда»"),
                                   _("Выгрузить сюда"), lambda: self.actions.activate_action("upload", None))
                else:
                    self._show_msg("folder-symbolic", _("Папка пуста"), None, None, None)
                return
            grid = self.view == "grid"
            for it in sorted(items, key=self._sort_key()):
                if grid:
                    self.grid.append(self._tile(it))
                else:
                    self.lb.append(self._row(it))
            self.stack.set_visible_child_name(self.view)
            (self.gsc if grid else self.sc).get_vadjustment().set_value(0)
            self._footer()
            self._load_thumbs()

        def _slow_hint(self):
            """Explain a slow listing; the cause depends on whose OAuth key the account uses."""
            own = bool(remote_config(self.remote).get("client_id"))
            self.hint_lbl.set_label(
                _("Google Диск долго отвечает — rclone ждёт и повторяет. "
                "У аккаунта свой ключ, так что это не общий лимит; большие папки бывают до минуты…")
                if own else
                _("Google Диск ограничивает частоту запросов (общий ключ rclone), "
                "rclone ждёт и повторяет. Обычно это до минуты…"))
            self.key_btn.set_visible(not own)
            self.load_hint.set_visible(True)

        def toggle_view(self):
            self.view = "grid" if self.view == "list" else "list"
            set_setting("browser_view", self.view)
            self.view_btn.set_icon_name("view-grid-symbolic" if self.view == "list" else "view-list-symbolic")
            if self.stack.get_visible_child_name() in ("list", "grid") and self.items:
                self._fill(self.items)

        def _folder_id(self):
            if not self.path:
                return None if self.shared else "root"
            return self.ids.get((self.shared, self.path))

        def _load_thumbs(self):
            if not self.remote or not any(not i["IsDir"] for i in self.items):
                return
            gen, grid = self.fill_gen, self.view == "grid"
            alive = lambda: gen == self.fill_gen

            def got(links):
                if not alive():
                    return
                self.tlinks = links
                spec = "s320-c" if grid else "s96-c"
                for fid, img in reversed(list(self.slots.items())):  # LIFO queue: top rows first
                    it, _p = self._item(fid)
                    if it and fid in links:
                        self.thumbs.get(links[fid], it, spec,
                                        lambda t, img=img: self._set_thumb(img, t, grid), alive)

            self.thumbs.folder_links(self.remote, self.shared, self.path, self._folder_id(), got)

        @staticmethod
        def _set_thumb(img, tex, grid):
            img.set_from_paintable(tex)
            img.remove_css_class("file-icon")
            img.add_css_class("thumb")
            img.set_overflow(Gtk.Overflow.HIDDEN)
            if grid:
                img.set_pixel_size(132)

        def _tile(self, it):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            pic = Gtk.Image.new_from_gicon(item_icon(it))
            pic.set_pixel_size(56)
            pic.set_size_request(132, 132)
            pic.set_halign(Gtk.Align.CENTER)
            pic.add_css_class("tile-pic")
            if it["IsDir"]:
                pic.add_css_class("folder")
            else:
                self.slots[it["ID"]] = pic
            box.append(pic)
            name = Gtk.Label(label=it["Name"], ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=16,
                             width_chars=16, margin_top=4)
            name.add_css_class("tile-name")
            sub = Gtk.Label(label=item_info(it))
            sub.add_css_class("tile-sub")
            sub.add_css_class("dim-label")
            box.append(name)
            box.append(sub)
            ch = Gtk.FlowBoxChild(child=box, tooltip_text=it["Name"])
            ch.item = it
            return ch

        def _row(self, it):
            size = it.get("Size", -1)
            if it["IsDir"]:
                info = _("Папка")
            elif size is None or size < 0:
                info = _("Документ Google")
            else:
                info = human(size)
            date = fmt_date(it.get("ModTime", ""))
            row = Adw.ActionRow(title=it["Name"], subtitle=f"{info} · {date}" if date else info,
                                use_markup=False, activatable=True, title_lines=1)
            row.item = it
            icon = Gtk.Image.new_from_gicon(item_icon(it))
            icon.set_pixel_size(32)
            icon.add_css_class("file-icon")
            if it["IsDir"]:
                icon.add_css_class("folder")
            else:
                self.slots[it["ID"]] = icon
            row.add_prefix(icon)
            mb = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Действия"))
            mb.add_css_class("flat")
            mb.set_create_popup_func(lambda b, i=it: b.set_menu_model(self._item_menu(i)))
            row.add_suffix(mb)
            if it["IsDir"]:
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            return row

        def _filter(self, row):
            q = self.search.get_text().strip().lower()
            return not q or q in row.item["Name"].lower()

        def _footer(self):
            q = self.search.get_text().strip().lower()
            items = [i for i in self.items if not q or q in i["Name"].lower()]
            dirs = sum(1 for i in items if i["IsDir"])
            files = len(items) - dirs
            size = sum(max(i.get("Size") or 0, 0) for i in items if not i["IsDir"])
            parts = []
            if q:
                parts.append(_("Найдено: {0}").format(len(items)))
            if dirs:
                parts.append(f"{dirs} {plural(dirs, _('папка'), _('папки'), _('папок'))}")
            if files:
                parts.append(f"{files} {plural(files, _('файл'), _('файла'), _('файлов'))} · {human(size)}")
            self.count_lbl.set_text(", ".join(parts))
            a = self.app.account(self.remote) if self.remote else {}
            used, total = a.get("used"), a.get("total")
            if used is not None and total:
                self.quota_lbl.set_text(_("Занято {0} из {1} ({2:.0f}%)").format(human(used), human(total), used / total * 100))
            elif used is not None:
                self.quota_lbl.set_text(_("Занято {0}").format(human(used)))
            else:
                self.quota_lbl.set_text("")

        def _activate(self, it):
            if it["IsDir"]:
                self.load(posixpath.join(self.path, it["Name"]))
            else:
                self._preview(it)

        def _preview(self, it):
            q = self.search.get_text().strip().lower()
            files = [i for i in sorted(self.items, key=self._sort_key())
                     if not i["IsDir"] and (not q or q in i["Name"].lower())]
            idx = next((n for n, i in enumerate(files) if i["ID"] == it["ID"]), None)
            if idx is None:
                files, idx = [it], 0
            PreviewDialog(self, files, idx).present(self.win)

        def _context_menu(self, gesture, _n, x, y):
            w = gesture.get_widget()
            row = w.get_row_at_y(int(y)) if w is self.lb else w.get_child_at_pos(int(x), int(y))
            if row is None:
                return
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            pop = Gtk.PopoverMenu.new_from_model(self._item_menu(row.item))
            pop.set_parent(w)
            pop.set_has_arrow(False)
            pop.set_halign(Gtk.Align.START)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            pop.set_pointing_to(rect)
            pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
            pop.popup()

        # ---------------------------------------------------------- menus
        @staticmethod
        def _mi(menu, label, action, target=None):
            item = Gio.MenuItem.new(label, None)
            if target is None:
                item.set_detailed_action(action)
            else:
                item.set_action_and_target_value(action, GLib.Variant("s", target))
            menu.append_item(item)

        def _item_menu(self, it):
            fid, m = it["ID"], Gio.Menu()
            s1, s2, s3 = Gio.Menu(), Gio.Menu(), Gio.Menu()
            if it["IsDir"]:
                self._mi(s1, _("Открыть"), "br.enter", fid)
            else:
                self._mi(s1, _("Просмотр"), "br.preview", fid)
            self._mi(s1, _("Открыть в браузере"), "br.web", fid)
            self._mi(s1, _("Копировать ссылку"), "br.link", fid)
            self._mi(s2, _("Скачать в «Загрузки»"), "br.download", fid)
            self._mi(s2, _("Скачать в…"), "br.download-to", fid)
            if it["IsDir"]:
                self._mi(s2, _("Размер папки"), "br.size", fid)
            self._mi(s3, _("Переименовать…"), "br.rename", fid)
            self._mi(s3, _("Переместить в…"), "br.move", fid)
            self._mi(s3, _("Удалить"), "br.delete", fid)
            for s in (s1, s2, s3):
                m.append_section(None, s)
            return m

        def _folder_menu(self):
            m = Gio.Menu()
            s0 = Gio.Menu()
            for label, mode in ((_("По имени"), "name"), (_("Сначала новые"), "date"), (_("Сначала большие"), "size")):
                self._mi(s0, label, "br.sort", mode)
            m.append_section(_("Сортировка"), s0)
            s1 = Gio.Menu()
            self._mi(s1, _("Обновить"), "br.refresh")
            self._mi(s1, _("Новая папка…"), "br.new-folder")
            self._mi(s1, _("Размер этой папки"), "br.size-here")
            m.append_section(None, s1)
            s2 = Gio.Menu()
            self._mi(s2, _("Открыть папку в браузере"), "br.web-here")
            self._mi(s2, _("Копировать ссылку на папку"), "br.link-here")
            self._mi(s2, _("Корзина Google Диска"), "br.trash")
            m.append_section(None, s2)
            return m

        def _setup_actions(self):
            self.actions = Gio.SimpleActionGroup()
            for name, cb in (("enter", lambda it, p: self.load(p)), ("preview", lambda it, p: self._preview(it)),
                             ("web", lambda it, p: self._open(web_url(it))),
                             ("link", lambda it, p: self._copy(web_url(it))),
                             ("download", lambda it, p: self._download(it, HOME / "Downloads")),
                             ("download-to", self._download_to), ("size", self._size),
                             ("rename", self._rename), ("move", self._move), ("delete", self._delete)):
                a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
                a.connect("activate", lambda _a, v, f=cb: self._with_item(v.get_string(), f))
                self.actions.add_action(a)
            for name, cb in (("refresh", lambda: self.load(force=True)), ("new-folder", self._new_folder),
                             ("upload", self._upload_here), ("size-here", lambda: self._size(None, self.path)),
                             ("web-here", lambda: self._open(self._here_url())),
                             ("link-here", lambda: self._copy(self._here_url())),
                             ("trash", lambda: self._open("https://drive.google.com/drive/trash"))):
                a = Gio.SimpleAction.new(name, None)
                a.connect("activate", lambda *_a, f=cb: f())
                self.actions.add_action(a)
            sort = Gio.SimpleAction.new_stateful("sort", GLib.VariantType.new("s"),
                                                 GLib.Variant("s", setting("browser_sort", "name")))
            sort.connect("change-state", self._sort_changed)
            self.actions.add_action(sort)
            self.insert_action_group("br", self.actions)

        def _with_item(self, fid, f):
            it, path = self._item(fid)
            if it is None:
                self._toast(_("Элемент уже не найден — обновите папку"))
                return
            f(it, path)

        def _sort_changed(self, action, value):
            action.set_state(value)
            set_setting("browser_sort", value.get_string())
            if self.items:
                self._fill(self.items)

        def _here_url(self):
            if not self.path:
                return ("https://drive.google.com/drive/shared-with-me" if self.shared
                        else "https://drive.google.com/drive/my-drive")
            fid = self.ids.get((self.shared, self.path))
            return f"https://drive.google.com/drive/folders/{fid}" if fid else "https://drive.google.com/drive/my-drive"

        # ---------------------------------------------------------- operations
        def _after_write(self, ok_text, err_text):
            def done(code, _out, err):
                if code == 0:
                    if ok_text:
                        self._toast(ok_text)
                else:
                    self._toast(f"{err_text}: {rclone_error(err)}")
                self.cache.clear()
                self.thumbs.forget()
                self.load(force=True, quiet=True)
            return done

        def _ask_name(self, heading, initial, ok_label, cb, body=None):
            d = Adw.AlertDialog(heading=heading, body=body or "")
            e = Gtk.Entry(text=initial, activates_default=True)
            d.set_extra_child(e)
            d.add_response("cancel", _("Отмена"))
            d.add_response("ok", ok_label)
            d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            d.set_default_response("ok")
            d.set_close_response("cancel")

            def changed(*_a):
                n = e.get_text().strip()
                d.set_response_enabled("ok", bool(n) and "/" not in n and n not in (".", "..") and n != initial)

            e.connect("changed", changed)
            changed()
            d.connect("response", lambda _d, r: r == "ok" and cb(e.get_text().strip()))
            d.present(self.win)

            def focus():
                e.grab_focus()
                stem, ext = os.path.splitext(initial)
                e.select_region(0, len(stem) if ext and len(ext) <= 6 else -1)
                return False

            GLib.idle_add(focus)

        def _exists(self, name):
            return any(i["Name"] == name for i in self.items)

        def _new_folder(self):
            def create(name):
                if self._exists(name):
                    self._toast(_("«{0}» здесь уже есть").format(name))
                    return
                self._run(["mkdir", self.rpath(posixpath.join(self.path, name))],
                          self._after_write(_("Папка «{0}» создана").format(name), _("Не удалось создать папку")))

            self._ask_name(_("Новая папка"), "", _("Создать"), create)

        def _rename(self, it, path):
            def rename(name):
                if self._exists(name):
                    self._toast(_("«{0}» здесь уже есть").format(name))
                    return
                new = posixpath.join(posixpath.dirname(path), name)
                self._run(["moveto", self.rpath(path), self.rpath(new)],
                          self._after_write(_("Переименовано"), _("Не удалось переименовать")))

            self._ask_name(_("Переименовать"), it["Name"], _("Переименовать"), rename)

        def _move(self, it, path):
            def picked(dest):
                dest = dest.strip("/")
                if dest == posixpath.dirname(path):
                    self._toast(_("Уже лежит в этой папке"))
                    return
                if it["IsDir"] and (dest == path or dest.startswith(path + "/")):
                    self._toast(_("Нельзя переместить папку в саму себя"))
                    return
                self._run(["moveto", self.rpath(path), self.rpath(posixpath.join(dest, it["Name"]))],
                          self._after_write(_("«{0}» перемещено в /{1}").format(it['Name'], dest), _("Не удалось переместить")))

            RemoteBrowser(self.win, self.remote, self.path, picked).present(self.win)

        def _delete(self, it, path):
            what = _("папку") if it["IsDir"] else _("файл")
            d = Adw.AlertDialog(heading=_("Удалить {0} «{1}»?").format(what, it['Name']),
                                body=(_("Папка со всем содержимым") if it["IsDir"] else _("Файл")) +
                                     _(" попадёт в корзину Google Диска, оттуда его можно восстановить "
                                     "в течение 30 дней."))
            d.add_response("cancel", _("Отмена"))
            d.add_response("delete", _("Удалить"))
            d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            d.set_default_response("cancel")
            d.set_close_response("cancel")

            def go(_d, r):
                if r != "delete":
                    return
                verb = "purge" if it["IsDir"] else "deletefile"
                self._run([verb, self.rpath(path)],
                          self._after_write(_("«{0}» перемещено в корзину").format(it['Name']), _("Не удалось удалить")))

            d.connect("response", go)
            d.present(self.win)

        def _size(self, it, path):
            name = it["Name"] if it else (posixpath.basename(path) or
                                          (_("Доступные мне") if self.shared else _("Мой диск")))
            self._toast(_("Считаю размер «{0}»…").format(name))

            def done(code, out, err):
                try:
                    s = json.loads(out) if code == 0 else None
                except ValueError:
                    s = None
                if not s:
                    self._toast(_("Не удалось посчитать: {0}").format(rclone_error(err)))
                    return
                n = s.get("count", 0)
                self._toast(_("«{0}»: {1} в {2} {3}").format(name, human(s.get('bytes', 0)), n, plural(n, _('файле'), _('файлах'), _('файлах'))))

            self._run(["size", "--json", self.rpath(path)], done, timeout=1800)

        def _download(self, it, base):
            base = Path(base)
            if it["IsDir"]:
                safe = re.sub(r'[/\\:*?"<>|]', "_", it["Name"]).strip() or f"drive-{it['ID'][:8]}"
                kind, dest = "folder", str(base / safe)
            else:
                kind, dest = "file", str(base)
            job = {"id": uuid.uuid4().hex[:8], "direction": "down", "name": it["Name"], "kind": kind,
                   "src": it["ID"], "dest": dest, "remote": self.remote, "transfers": 4,
                   "bwlimit": "", "status": "queued", "created": time.time()}
            self.win.add_job(job)

        def _download_to(self, it, _path):
            fd = Gtk.FileDialog(title=_("Куда скачать «{0}»").format(it['Name']),
                                initial_folder=Gio.File.new_for_path(str(HOME / "Downloads")))

            def picked(fd, res):
                try:
                    f = fd.select_folder_finish(res)
                except GLib.Error:
                    return
                if f and f.get_path():
                    self._download(it, f.get_path())

            fd.select_folder(self.win, None, picked)

        def _upload_here(self, paths=None):
            dest = self.upload_dest()
            if dest is None:
                self._toast(_("Не удалось определить папку — обновите её"))
                return
            self.win.show_upload(paths, dest=dest)

    # ------------------------------------------------------------ window

    class Page:
        """One tab: downloads or uploads."""

        def __init__(self, win, direction):
            up = direction == "up"
            self.count = 0
            self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18,
                                margin_bottom=24, margin_start=16, margin_end=16)
            clamp = Adw.Clamp(maximum_size=860, tightening_threshold=640, child=self.list)
            if up:
                # Upload jobs live in a drawer under the Drive browser.
                self.list.set_margin_top(6)
                self.list.set_margin_bottom(12)
                self.list.set_spacing(10)
                self.stack = None
                self.sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp,
                                             propagate_natural_height=True, max_content_height=340)
                return
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            empty = Adw.StatusPage(
                icon_name=ICON_UP if up else ICON_DOWN,
                title=_("Нет выгрузок") if up else _("Нет загрузок"),
                description=(_("Выберите файлы или папки — или перетащите их в окно.\n"
                             "Выгрузка идёт в фоне и продолжится после перезагрузки.")) if up else
                            (_("Вставьте ссылку на папку Google Drive. Загрузка идёт в фоне,\n"
                             "не зависит от окна и продолжится после перезагрузки.")))
            b = Gtk.Button(label=_("Выгрузить файлы") if up else _("Новая загрузка"), halign=Gtk.Align.CENTER)
            b.add_css_class("pill")
            b.add_css_class("suggested-action")
            b.connect("clicked", lambda *_a: win.show_upload() if up else win.show_add())
            empty.set_child(b)
            self.stack.add_named(empty, "empty")
            sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)
            self.stack.add_named(sc, "list")

    class Window(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title=APP_NAME, default_width=800, default_height=780,
                             icon_name=APP_ID)
            self.app, self.store = app, app.store
            self.cards = {}
            self.toasts = Adw.ToastOverlay()
            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            add = Gtk.Button(icon_name="list-add-symbolic",
                             tooltip_text=_("Новая загрузка или выгрузка (Ctrl+N)"))
            add.connect("clicked", lambda *_a: self.show_new())
            hb.pack_start(add)

            self.acct_btn = Gtk.MenuButton(tooltip_text=_("Аккаунт Google"))
            ab = Gtk.Box(spacing=6)
            ab.append(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))
            self.acct_lbl = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=22)
            ab.append(self.acct_lbl)
            self.acct_btn.set_child(ab)
            hb.pack_start(self.acct_btn)

            self.views = Adw.ViewStack()
            self.pages = {"down": Page(self, "down"), "up": Page(self, "up")}
            self.browser = DriveBrowser(self)
            self.browser.set_vexpand(True)
            upbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            upbox.append(self.browser)
            self.drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=False)
            self.drawer.add_css_class("drawer")
            self.drawer.append(Gtk.Separator())
            head = Gtk.Button()
            head.add_css_class("flat")
            head.add_css_class("drawer-head")
            hbx = Gtk.Box(spacing=10)
            hbx.append(Gtk.Image.new_from_icon_name(ICON_UP))
            self.drawer_title = Gtk.Label(label=_("Выгрузки"))
            self.drawer_title.add_css_class("heading")
            hbx.append(self.drawer_title)
            self.drawer_info = Gtk.Label(hexpand=True, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            self.drawer_info.add_css_class("dim-label")
            self.drawer_info.add_css_class("numeric")
            hbx.append(self.drawer_info)
            self.drawer_arrow = Gtk.Image()
            hbx.append(self.drawer_arrow)
            head.set_child(hbx)
            head.connect("clicked", lambda *_a: self.open_drawer(not self.drawer_rev.get_reveal_child()))
            self.drawer.append(head)
            self.drawer_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                           child=self.pages["up"].sc)
            self.drawer.append(self.drawer_rev)
            upbox.append(self.drawer)
            self.open_drawer(setting("up_drawer", True), save=False)
            self.vpages = {
                "down": self.views.add_titled_with_icon(self.pages["down"].stack, "down", _("Скачивание"), ICON_DOWN),
                "up": self.views.add_titled_with_icon(upbox, "up", _("Мой Диск"), "folder-remote-symbolic"),
            }
            self.views.set_visible_child_name(setting("tab", "down"))
            self.views.connect("notify::visible-child-name",
                               lambda v, _p: set_setting("tab", v.get_visible_child_name()))
            sw = Adw.ViewSwitcher(stack=self.views, policy=Adw.ViewSwitcherPolicy.WIDE)
            hb.set_title_widget(sw)

            menu = Gio.Menu()
            menu.append(_("Настройки"), "app.settings")
            menu.append(_("О программе"), "app.about")
            menu.append(_("Выход"), "app.quit")
            hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text=_("Меню")))
            self.activity = Gtk.Label()
            self.activity.add_css_class("activity")
            self.activity.add_css_class("dim-label")
            self.activity.add_css_class("numeric")
            hb.pack_end(self.activity)
            tv.add_top_bar(hb)
            tv.set_content(self.views)
            self.toasts.set_child(tv)
            self.set_content(self.toasts)

            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("enter", self._drop_enter)
            drop.connect("leave", lambda *_a: self.toasts.remove_css_class("drop-hint"))
            drop.connect("drop", self._on_drop)
            self.add_controller(drop)
            self.connect("close-request", self._on_close)

            for job in sorted(self.store.jobs, key=lambda j: j.get("created", 0)):
                self._add_card(job)
            self.update_accounts()
            self._update_view()

        def toast(self, text):
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))
            return False

        def _on_close(self, *_a):
            tray = self.app.tray
            if tray and tray.registered and setting("close_to_tray", True):
                self.set_visible(False)
                tray.update()
                if not setting("tray_hint_shown"):
                    set_setting("tray_hint_shown", True)
                    n = Gio.Notification.new(_("Drive Loader свёрнут в трей"))
                    n.set_body(_("Передачи продолжаются. Выход — через меню значка в трее."))
                    self.app.send_notification("tray-hint", n)
                return True
            return False

        def _drop_enter(self, *_a):
            self.toasts.add_css_class("drop-hint")
            return Gdk.DragAction.COPY

        def _on_drop(self, _t, value, _x, _y):
            self.toasts.remove_css_class("drop-hint")
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if not paths:
                return True
            if self._on_browser() and self.browser.can_upload():
                self.browser._upload_here(paths)
            else:
                self.show_upload(paths)
            return True

        def _on_browser(self):
            return self.views.get_visible_child_name() == "up"

        def open_drawer(self, show=True, save=True):
            self.drawer_rev.set_reveal_child(show)
            self.drawer_arrow.set_from_icon_name("pan-down-symbolic" if show else "pan-up-symbolic")
            if save:
                set_setting("up_drawer", show)

        def _add_card(self, job):
            card = JobCard(self, job)
            self.cards[job["id"]] = card
            page = self.pages["up" if is_up(job) else "down"]
            page.list.prepend(card)
            page.count += 1

        def update_accounts(self):
            app = self.app
            self.acct_lbl.set_text(app.account_label(app.active) if app.active else _("Войти в Google"))
            m = Gio.Menu()
            sec = Gio.Menu()
            for a in app.accounts:
                it = Gio.MenuItem.new(app.account_label(a["name"]), None)
                it.set_action_and_target_value("app.account", GLib.Variant("s", a["name"]))
                sec.append_item(it)
            m.append_section(_("Аккаунт для новых передач") if app.accounts else None, sec)
            sec2 = Gio.Menu()
            sec2.append(_("Добавить аккаунт…"), "app.add-account")
            sec2.append(_("Управление аккаунтами…"), "app.settings")
            m.append_section(None, sec2)
            self.acct_btn.set_menu_model(m)
            self.browser.set_remote(app.active)
            for jid, card in self.cards.items():
                if (job := self.store.get(jid)):
                    card.sub.set_text(card.subtitle(job))

        def _update_view(self):
            for key, page in self.pages.items():
                if page.stack:
                    page.stack.set_visible_child_name("list" if page.count else "empty")
                n = sum(1 for j in self.store.jobs
                        if j["status"] == "running" and (is_up(j) == (key == "up")))
                self.vpages[key].set_badge_number(n)
                self.vpages[key].set_needs_attention(any(
                    j["status"] == "error" and is_up(j) == (key == "up") for j in self.store.jobs))
            n_up = self.pages["up"].count
            self.drawer.set_visible(n_up > 0)
            self.drawer_title.set_text(_("Выгрузки · {0}").format(n_up))
            running = [c for jid, c in self.cards.items()
                       if (j := self.store.get(jid)) and j["status"] == "running"]
            speed = sum((c.last_stats or {}).get("speed", 0) or 0 for c in running)
            up_run = [c for c in running if c.up]
            n_err = sum(1 for j in self.store.jobs if j["status"] == "error" and is_up(j))
            bits = []
            if up_run:
                up_speed = sum((c.last_stats or {}).get("speed", 0) or 0 for c in up_run)
                bits.append(_("идёт {0} · {1}/с").format(len(up_run), human(up_speed)))
            if n_err:
                bits.append(_("ошибок: {0}").format(n_err))
            self.drawer_info.set_text("   ".join(bits))
            self.activity.set_text(_("{0}/с").format(human(speed)) if running else "")
            if self.app.tray:
                self.app.tray.update()

        def show_new(self):
            if self._on_browser() and self.browser.can_upload():
                self.browser._upload_here()
            elif self.views.get_visible_child_name() == "up":
                self.show_upload()
            else:
                self.show_add()

        def _need_account(self):
            if not self.app.accounts:
                self.toast(_("Сначала войдите в аккаунт Google"))
                self.app.show_settings()
                return True
            return False

        def show_add(self):
            self.views.set_visible_child_name("down")
            if not self._need_account():
                AddDialog(self).present(self)

        def show_upload(self, paths=None, dest=None):
            self.views.set_visible_child_name("up")
            if not self._need_account():
                UploadDialog(self, paths, dest).present(self)

        def show_jobs(self, up):
            self.views.set_visible_child_name("up" if up else "down")
            if up:
                self.open_drawer(True)

        def add_job(self, job):
            self.store.add(job)
            start_job(job)
            self.store.save()
            self._add_card(job)
            self._update_view()
            t = Adw.Toast(title=GLib.markup_escape_text(
                _("{0} «{1}» начата").format(_('Выгрузка') if is_up(job) else _('Загрузка'), job['name'])), timeout=5)
            if is_up(job):
                self.open_drawer(True)
            visible = self.views.get_visible_child_name() == ("up" if is_up(job) else "down")
            if not visible:
                t.set_button_label(_("Показать"))
                t.connect("button-clicked", lambda *_a, up=is_up(job): self.show_jobs(up))
            self.toasts.add_toast(t)

        def toggle_job(self, jid):
            job = self.store.get(jid)
            if not job:
                return
            if job["status"] == "running":
                self.pause_job(jid)
            else:
                self.restart_job(jid)
            self._update_view()

        def pause_job(self, jid):
            job = self.store.get(jid)
            if not job or job["status"] != "running":
                return
            job["status"] = "paused"
            self.store.save()
            self.cards[jid].refresh(job)
            threading.Thread(target=stop_job, args=(dict(job),), daemon=True).start()

        def restart_job(self, jid):
            job = self.store.get(jid)
            if not job:
                return
            if not start_job(job):
                self.toast(_("Не удалось запустить передачу"))
            self.store.save()
            self.cards[jid].history.clear()
            self.cards[jid].refresh(job)
            self._update_view()

        def pause_all(self):
            for j in list(self.store.jobs):
                if j["status"] == "running":
                    self.pause_job(j["id"])
            self._update_view()

        def resume_all(self):
            for j in list(self.store.jobs):
                if j["status"] in ("paused", "interrupted", "error", "queued"):
                    self.restart_job(j["id"])

        def set_bwlimit(self, jid, val):
            job = self.store.get(jid)
            if not job:
                return
            job["bwlimit"] = val
            self.store.save()
            port = read_port(job)
            if job["status"] == "running" and port:
                threading.Thread(target=rc, args=(port, "core/bwlimit", {"rate": val or "off"}),
                                 daemon=True).start()
            self.toast(_("Ограничение снято") if not val else _("Ограничение: {0}").format(val.replace('M', _(' МБ/с'))))

        def open_folder(self, jid):
            job = self.store.get(jid)
            if not job:
                return
            path = job["src"] if is_up(job) else job["dest"]
            if not is_up(job):
                Path(path).mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", path])

        def open_on_drive(self, jid):
            job = self.store.get(jid)
            if not job:
                return
            if not is_up(job):
                kind = "file/d" if job["kind"] == "file" else "drive/folders"
                subprocess.Popen(["xdg-open", f"https://drive.google.com/{kind}/{job['src']}"])
                return
            dest = job["dest"].strip("/")
            if job.get("dest_id") and not dest:
                subprocess.Popen(["xdg-open", f"https://drive.google.com/drive/folders/{job['dest_id']}"])
                return
            if not dest:
                subprocess.Popen(["xdg-open", "https://drive.google.com/drive/my-drive"])
                return
            self.toast(_("Ищу папку на Диске…"))

            def work():
                cmd = [RCLONE, "lsjson", "--stat", f"{job['remote']}:{dest}"]
                if job.get("dest_id"):
                    cmd += ["--drive-root-folder-id", job["dest_id"]]
                try:
                    fid = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout)["ID"]
                except Exception:
                    fid = None
                if fid:
                    subprocess.Popen(["xdg-open", f"https://drive.google.com/drive/folders/{fid}"])
                else:
                    GLib.idle_add(self.toast, _("Папка на Диске пока не создана"))

            threading.Thread(target=work, daemon=True).start()

        def open_log(self, jid):
            job = self.store.get(jid)
            if job and log_of(job).exists():
                subprocess.Popen(["xdg-open", str(log_of(job))])
            else:
                self.toast(_("Лог пока пуст"))

        def ask_remove(self, jid):
            job = self.store.get(jid)
            if not job:
                return
            body = (_("Выгрузка остановится. Уже выгруженные файлы останутся на Диске.") if is_up(job)
                    else _("Загрузка остановится. Скачанные файлы останутся в папке."))
            d = Adw.AlertDialog(heading=_("Убрать «{0}»?").format(job['name']), body=body)
            d.add_response("cancel", _("Отмена"))
            d.add_response("remove", _("Убрать"))
            d.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
            d.set_default_response("cancel")
            d.connect("response", lambda _d, r: r == "remove" and self._remove(jid))
            d.present(self)

        def _remove(self, jid):
            job = self.store.get(jid)
            if job:
                j = dict(job)
                threading.Thread(target=lambda: (stop_job(j), cleanup_job_files(j)), daemon=True).start()
            self.store.remove(jid)
            card = self.cards.pop(jid, None)
            if card:
                page = self.pages["up" if card.up else "down"]
                page.list.remove(card)
                page.count -= 1
            self._update_view()

        def apply_poll(self, results):
            changed = False
            for jid, (active, stats, code, disk) in results.items():
                job, card = self.store.get(jid), self.cards.get(jid)
                if not job or not card or job["status"] != "running":
                    continue
                if disk is not None:
                    card.disk = disk
                if active:
                    card.refresh(job, stats)
                    continue
                what = _("Выгрузка") if is_up(job) else _("Загрузка")
                if code is None:
                    job["status"] = "interrupted"
                elif finish_job(job, code):
                    if is_up(job):
                        self.browser.changed()
                        self.app.refresh_account(job.get("remote", REMOTE))
                    n = Gio.Notification.new(_("{0} завершена").format(what))
                    n.set_body(_("«{0}» выгружено на Google Диск").format(job['name']) if is_up(job)
                               else _("«{0}» скачано в {1}").format(job['name'], job['dest']))
                    self.app.send_notification(f"done-{jid}", n)
                else:
                    n = Gio.Notification.new(_("{0} остановилась с ошибкой").format(what))
                    n.set_body(_("«{0}»: {1}").format(job['name'], job['error'].splitlines()[0]))
                    if job["error"].startswith(AUTH_PREFIXES):
                        n.add_button_with_target(_("Войти заново"), "app.relogin",
                                                 GLib.Variant("s", job.get("remote", REMOTE)))
                        self.toast(_("«{0}»: {1}").format(job['name'], AUTH_EXPIRED))
                    self.app.send_notification(f"err-{jid}", n)
                changed = True
                card.refresh(job)
            if changed:
                self.store.save()
            self._update_view()
            return False

    # ------------------------------------------------------------ app

    KEY_STEPS = [
        (_("Создайте проект"), _("Любое имя, например «Drive Loader»"),
         "https://console.cloud.google.com/projectcreate"),
        (_("Включите Google Drive API"), _("Кнопка «Включить» (проверьте, что выбран новый проект)"),
         "https://console.cloud.google.com/apis/library/drive.googleapis.com"),
        (_("Настройте экран входа"), _("«Начать»: название, ваш e-mail, аудитория «Внешняя»"),
         "https://console.cloud.google.com/auth/overview"),
        (_("Добавьте себя в тестировщики"), _("«Тестовые пользователи» → «Add users» → ваш e-mail → «Сохранить». "
         "Без этого вход даёт «Ошибка 403: access_denied»"),
         "https://console.cloud.google.com/auth/audience"),
        (_("Создайте клиент OAuth"), _("Тип «Приложение для ПК» → «Создать» → «Скачать JSON»"),
         "https://console.cloud.google.com/auth/clients/create"),
    ]

    def read_client_json(path):
        """(client_id, client_secret, kind) from a Google client_secret_*.json."""
        try:
            d = json.loads(Path(path).read_text())
        except Exception:
            return None
        kind = "installed" if "installed" in d else ("web" if "web" in d else None)
        c = d.get(kind or "", {})
        if c.get("client_id", "").endswith(".apps.googleusercontent.com") and c.get("client_secret"):
            return c["client_id"], c["client_secret"], kind
        return None

    class KeyDialog(Adw.Dialog):
        """Guided setup of the user's own Google OAuth client, so rclone stops
        sharing its rate-limited (and retiring) client_id with everyone."""

        def __init__(self, app):
            super().__init__(title=_("Свой ключ Google"), content_width=580, content_height=760)
            self.app = app
            self.opened = time.time()
            s = load_json(SETTINGS_FILE, {})
            self.cid, self.sec = s.get("client_id", ""), s.get("client_secret", "")
            self.queue = []
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            page = Adw.PreferencesPage()

            intro = Adw.PreferencesGroup(description=(
                _("Сейчас все запросы идут через общий ключ rclone, который делят тысячи людей: "
                "Google притормаживает его (отсюда долгие «Загрузка…»), а в 2026 году его отключат. "
                "Свой ключ бесплатный и настраивается один раз, минуты за три. "
                "Создать его за вас Google программам не даёт, поэтому нужны четыре шага в браузере.")))
            page.add(intro)

            steps = Adw.PreferencesGroup(title=_("Шаги в Google Cloud"))
            for i, (title, sub, url) in enumerate(KEY_STEPS, 1):
                r = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
                n = Gtk.Label(label=str(i), width_chars=2)
                n.add_css_class("title-3")
                r.add_prefix(n)
                b = Gtk.Button(icon_name="adw-external-link-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text=_("Открыть в браузере"))
                b.add_css_class("flat")
                b.connect("clicked", lambda _b, u=url: subprocess.Popen(["xdg-open", u]))
                r.add_suffix(b)
                r.set_activatable_widget(b)
                steps.add(r)
            page.add(steps)

            key = Adw.PreferencesGroup(title=_("Ключ"))
            self.key_row = Adw.ActionRow()
            self.key_spin = Spinner(valign=Gtk.Align.CENTER)
            self.key_icon = Gtk.Image(icon_name="emblem-ok-symbolic", valign=Gtk.Align.CENTER)
            self.key_icon.add_css_class("success")
            self.key_row.add_prefix(self.key_spin)
            self.key_row.add_prefix(self.key_icon)
            pick = Gtk.Button(label=_("Файл…"), valign=Gtk.Align.CENTER,
                              tooltip_text=_("Выбрать скачанный client_secret_….json"))
            pick.add_css_class("flat")
            pick.connect("clicked", self._pick)
            self.key_row.add_suffix(pick)
            key.add(self.key_row)
            man = Adw.ExpanderRow(title=_("Ввести вручную"))
            self.e_cid = Adw.EntryRow(title="client_id", text=self.cid)
            self.e_sec = Adw.PasswordEntryRow(title="client_secret", text=self.sec)
            for e in (self.e_cid, self.e_sec):
                e.connect("changed", self._manual)
                man.add_row(e)
            key.add(man)
            page.add(key)

            self.acc = Adw.PreferencesGroup(title=_("Аккаунты"), description=(
                _("Ключ привязывается ко входу, поэтому каждый аккаунт войдёт заново: "
                "откроется браузер, на предупреждении «Приложение не проверено» нажмите "
                "«Дополнительно» → «Перейти».")))
            self.acc_rows = []
            page.add(self.acc)

            self.apply_btn = Gtk.Button(label=_("Применить и войти заново"), halign=Gtk.Align.CENTER,
                                        margin_top=6)
            self.apply_btn.add_css_class("suggested-action")
            self.apply_btn.add_css_class("pill")
            self.apply_btn.connect("clicked", self._apply)
            g = Adw.PreferencesGroup()
            g.add(self.apply_btn)
            page.add(g)

            tv.set_content(page)
            self.set_child(tv)
            self.confs = {}
            threading.Thread(target=self._load_confs, daemon=True).start()
            self._show_key()
            self.watch = GLib.timeout_add_seconds(2, self._scan)
            self.connect("closed", lambda *_a: GLib.source_remove(self.watch))

        # key ----------------------------------------------------------
        def _set_key(self, cid, sec, save=True):
            self.cid, self.sec = cid.strip(), sec.strip()
            if save:
                set_setting("client_id", self.cid)
                set_setting("client_secret", self.sec)
            self._show_key()
            self._accounts()

        def _show_key(self):
            have = bool(self.cid and self.sec)
            self.key_spin.set_visible(not have)
            self.key_icon.set_visible(have)
            if have:
                short = self.cid.split("-")[0] + "-…" + ".apps.googleusercontent.com"
                self.key_row.set_title(_("Ключ есть"))
                self.key_row.set_subtitle(short)
            else:
                self.key_row.set_title(_("Жду файл ключа в «Загрузках»…"))
                self.key_row.set_subtitle(_("client_secret_….json подхватится сам после шага 4"))
            self.apply_btn.set_sensitive(have and not self.queue)

        def _scan(self):
            if self.cid and self.sec:
                return True
            dl = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or HOME / "Downloads")
            files = sorted(dl.glob("client_secret*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[:3]:
                c = read_client_json(p)
                if c and c[2] == "installed":
                    self._fill(c)
                    self.app.win.toast(_("Ключ найден в «Загрузках»"))
                    break
                if c:
                    self.key_row.set_subtitle(_("Найден ключ типа «Веб-приложение» — нужен «Приложение для ПК»"))
            return True

        def _fill(self, c):
            for e, v in ((self.e_cid, c[0]), (self.e_sec, c[1])):
                e.handler_block_by_func(self._manual)
                e.set_text(v)
                e.handler_unblock_by_func(self._manual)
            self._set_key(c[0], c[1])

        def _pick(self, *_a):
            f = Gtk.FileFilter(name=_("Ключ Google (JSON)"))
            f.add_pattern("*.json")
            fl = Gio.ListStore.new(Gtk.FileFilter)
            fl.append(f)
            Gtk.FileDialog(title=_("Файл client_secret_….json"), filters=fl).open(
                self.app.win, None, self._picked)

        def _picked(self, fd, res):
            try:
                path = fd.open_finish(res).get_path()
            except GLib.Error:
                return
            c = read_client_json(path)
            if not c:
                self.app.win.toast(_("Это не файл ключа Google"))
            elif c[2] != "installed":
                self.app.win.toast(_("Нужен клиент типа «Приложение для ПК»"))
            else:
                self._fill(c)

        def _manual(self, *_a):
            self._set_key(self.e_cid.get_text(), self.e_sec.get_text())

        # accounts -----------------------------------------------------
        def _load_confs(self):
            try:
                out = subprocess.run([RCLONE, "config", "dump"], capture_output=True, text=True,
                                     timeout=20).stdout
                confs = {n: {"client_id": c.get("client_id", "")} for n, c in json.loads(out).items()}
            except Exception:
                confs = {}
            GLib.idle_add(lambda: (setattr(self, "confs", confs), self._accounts()) and False)

        def _busy(self, n):
            return any(j["status"] == "running" and j.get("remote") == n for j in self.app.store.jobs)

        def _accounts(self):
            for r in self.acc_rows:
                self.acc.remove(r)
            self.acc_rows.clear()
            for a in self.app.accounts:
                n = a["name"]
                cur = self.confs.get(n, {}).get("client_id", "")
                if n in self.queue:
                    sub = _("вход в браузере…") if self.queue[0] == n else _("ждёт входа")
                elif cur and cur == self.cid:
                    sub = _("свой ключ ✓")
                else:
                    sub = _("общий ключ rclone") if not cur else _("другой ключ")
                if self._busy(n) and n not in self.queue:
                    sub += _(" · передачи встанут на паузу на время входа")
                r = Adw.ActionRow(title=GLib.markup_escape_text(self.app.account_label(n)),
                                  subtitle=sub)
                r.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
                if self.cid and not self.queue:
                    # the config may say "own key" while the token is still from the old
                    # key (failed login) — allow a forced re-login per account
                    b = Gtk.Button(label=_("Войти заново"), valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, n=n: self._start([n]))
                    r.add_suffix(b)
                self.acc.add(r)
                self.acc_rows.append(r)

        def _apply(self, *_a):
            todo = [a["name"] for a in self.app.accounts
                    if self.confs.get(a["name"], {}).get("client_id", "") != self.cid]
            # all on the key already: log in again anyway — a token left from the old key
            # (after a failed login) only gets fixed by a fresh login
            self._start(todo or [a["name"] for a in self.app.accounts])

        def _start(self, todo):
            if not todo:
                return
            self.queue = list(todo)
            self._show_key()
            self._next()

        def _next(self):
            self._accounts()
            if not self.queue:
                self._show_key()
                threading.Thread(target=self._load_confs, daemon=True).start()
                return
            n = self.queue[0]

            def done(ok):
                self.queue.pop(0)
                if not ok:
                    self.queue.clear()
                self._next()

            self.app.login(n, readonly="readonly" in self.app.account_scope(n), done_cb=done)

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
            self.store = Store()
            self.win = None
            self.tray = None
            self.logging_in = False
            self.login_proc = None
            self.login_retry = None
            self.login_paused = []
            self.settings_hooks = []
            self.accounts = []
            self.active = None
            self.job_menu_ids = {}
            for name, cb, accels in (("settings", lambda *_a: self.show_settings(), ["<Ctrl>comma"]),
                                     ("about", lambda *_a: self.show_about(), []),
                                     ("new", lambda *_a: self.win and self.win.show_new(), ["<Ctrl>n"]),
                                     ("add-account", lambda *_a: self.login(None), []),
                                     ("quit", lambda *_a: self.quit(), ["<Ctrl>q"])):
                a = Gio.SimpleAction.new(name, None)
                a.connect("activate", cb)
                self.add_action(a)
                if accels:
                    self.set_accels_for_action(f"app.{name}", accels)
            self.acct_action = Gio.SimpleAction.new_stateful("account", GLib.VariantType.new("s"),
                                                              GLib.Variant("s", ""))
            self.acct_action.connect("change-state", lambda a, v: self.set_active(v.get_string()))
            self.add_action(self.acct_action)
            a = Gio.SimpleAction.new("relogin", GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, v: self.relogin(v.get_string()))
            self.add_action(a)
            self.set_accels_for_action("window.close", ["<Ctrl>w"])

        def relogin(self, name):
            """Log in again keeping the account's current scope."""
            if self.win:
                self.win.present()
            self.login(name, readonly="readonly" in self.account_scope(name))

        # accounts -----------------------------------------------------
        def reload_accounts(self, fetch=False):
            cache = setting("accounts", {})
            self.accounts = [{"name": n, **cache.get(n, {})} for n in drive_remotes()]
            names = [a["name"] for a in self.accounts]
            active = setting("active")
            self.active = active if active in names else (names[0] if names else None)
            self.acct_action.set_state(GLib.Variant("s", self.active or ""))
            if fetch:
                for n in names:
                    self.refresh_account(n)

        def refresh_account(self, name):
            def work():
                info = fetch_account_info(name)
                GLib.idle_add(self._store_info, name, info)

            threading.Thread(target=work, daemon=True).start()

        def _store_info(self, name, info):
            cache = setting("accounts", {})
            cache[name] = {**cache.get(name, {}), **{k: v for k, v in info.items() if v is not None}}
            set_setting("accounts", cache)
            self.accounts_changed()
            return False

        def accounts_changed(self):
            self.reload_accounts()
            if self.win:
                self.win.update_accounts()
                self.win._update_view()
            for hook in list(self.settings_hooks):
                hook()

        def account(self, name):
            return next((a for a in self.accounts if a["name"] == name), {})

        def account_label(self, name):
            a = self.account(name)
            return a.get("email") or name or "—"

        def account_scope(self, name):
            return self.account(name).get("scope") or "drive"

        def set_active(self, name):
            if name not in [a["name"] for a in self.accounts]:
                return
            set_setting("active", name)
            self.accounts_changed()
            if self.win:
                self.win.toast(_("Аккаунт: {0}").format(self.account_label(name)))

        def login(self, name, readonly=False, done_cb=None, paused=()):
            """Add a new account (name=None) or log in again to an existing one."""
            if self.logging_in:
                # a login whose browser page failed (e.g. 403) never returns to rclone:
                # pressing again cancels it and starts over
                proc = getattr(self, "login_proc", None)
                if proc and proc.poll() is None:
                    self.login_retry = (name, readonly, done_cb, paused or self.login_paused)
                    proc.terminate()
                    return
                if self.win:
                    self.win.toast(_("Вход уже идёт — закончите его в браузере"))
                return
            # transfers of this account are paused for the login and resumed after it
            paused = list(paused) + [j["id"] for j in self.store.jobs
                                     if name and j["status"] == "running" and j.get("remote") == name]
            for jid in paused:
                self.win.pause_job(jid)
            self.login_paused = paused
            existing = drive_remotes()
            target = name or next_remote_name(existing)
            s = load_json(SETTINGS_FILE, {})
            cid, sec = s.get("client_id", ""), s.get("client_secret", "")
            self.logging_in = True
            if self.win:
                self.win.toast(_("Войдите в Google в открывшемся браузере"))

            def work():
                args = [RCLONE, "config", "create", target, "drive",
                        "scope=" + ("drive.readonly" if readonly else "drive")]
                if cid and sec:
                    args += [f"client_id={cid}", f"client_secret={sec}"]
                if target in existing:
                    # re-login: rclone re-runs the OAuth flow and overwrites the token
                    args = [RCLONE, "config", "update", target,
                            "scope=" + ("drive.readonly" if readonly else "drive"),
                            f"client_id={cid if cid and sec else ''}",
                            f"client_secret={sec if cid and sec else ''}", "config_refresh_token=true"]
                before = remote_config(target) if target in existing else {}
                try:
                    self.login_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.DEVNULL)
                    try:
                        code = self.login_proc.wait(timeout=600)
                    except subprocess.TimeoutExpired:
                        self.login_proc.kill()
                        code = -1
                    ok = code == 0 and target in drive_remotes()
                    after = remote_config(target) if ok else {}
                    ok = ok and after.get("token") and after.get("token") != before.get("token")
                except OSError:
                    ok = False
                if not ok and before:
                    # rclone writes the new client_id before the browser step; a failed
                    # login must not leave the old token paired with a foreign client_id
                    subprocess.run([RCLONE, "config", "update", target, "--non-interactive",
                                    f"client_id={before.get('client_id', '')}",
                                    f"client_secret={before.get('client_secret', '')}",
                                    f"scope={before.get('scope', 'drive.readonly')}",
                                    "config_refresh_token=false"],
                                   capture_output=True, timeout=60)
                info = fetch_account_info(target) if ok else {}

                def finish():
                    self.logging_in = False
                    self.login_proc = None
                    retry = getattr(self, "login_retry", None)
                    self.login_retry = None
                    if retry:  # the paused transfers wait for the new attempt
                        self.login(*retry)
                        return False
                    for j in list(self.store.jobs):
                        # resume what we paused, and after a good login also what failed
                        # because the old login had expired
                        if (j["id"] in paused and j["status"] == "paused") or (
                                ok and j.get("remote") == target and j["status"] == "error"
                                and j.get("error", "").startswith(AUTH_PREFIXES)):
                            self.win.restart_job(j["id"])
                    cache = setting("accounts", {})
                    if ok:
                        cache[target] = {k: v for k, v in info.items() if v is not None}
                        set_setting("accounts", cache)
                        if not name:
                            set_setting("active", target)
                    self.accounts_changed()
                    if self.win:
                        self.win.toast(_("Вход выполнен: {0}").format(self.account_label(target)) if ok else _("Вход не удался"))
                    if done_cb:
                        done_cb(ok)
                    return False

                GLib.idle_add(finish)

            threading.Thread(target=work, daemon=True).start()

        def remove_account(self, name):
            if any(j["status"] == "running" and j.get("remote") == name for j in self.store.jobs):
                self.win.toast(_("У этого аккаунта есть активные передачи"))
                return
            subprocess.run([RCLONE, "config", "delete", name], capture_output=True)
            cache = setting("accounts", {})
            cache.pop(name, None)
            set_setting("accounts", cache)
            self.accounts_changed()

        # lifecycle ----------------------------------------------------
        def do_startup(self):
            Adw.Application.do_startup(self)
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(ICON_ROOT))
            prov = Gtk.CssProvider()
            prov.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def do_activate(self):
            if self.win is not None:
                self.show_window()
                return
            self.reload_accounts(fetch=True)
            resumed = resume_unfinished(self.store)
            self.win = Window(self)
            for job in self.store.jobs:
                self.win.cards[job["id"]].refresh(job)
            if resumed:
                self.win.toast(_("Продолжены: ") + ", ".join(resumed))
            try:
                self.tray = Tray(self)
            except Exception as e:  # no session bus features: just run without a tray
                print("tray unavailable:", e, file=sys.stderr)
            threading.Thread(target=self._poll_loop, daemon=True).start()
            if start_hidden and self.tray:
                self.hold()  # keep running with no visible window
                # the bar's tray watcher may start after us at login: give it time
                GLib.timeout_add_seconds(15, self._hidden_check)
            else:
                self.win.present()

        def _hidden_check(self):
            if not self.tray.registered and not self.win.get_visible():
                self.show_window()
            return False

        def show_window(self):
            self.win.set_visible(True)
            self.win.present()
            if self.tray:
                self.tray.update()
            return False

        def toggle_window(self):
            if self.win.get_visible():
                self.win.close()
                if self.win.get_visible():  # no tray: close() didn't hide it
                    self.win.present()
            else:
                self.show_window()
            return False

        def _poll_loop(self):
            tick = 0
            while True:
                results = {}
                with self.store.lock:
                    jobs = [dict(j) for j in self.store.jobs if j["status"] == "running"]
                for job in jobs:
                    active = unit_active(job)
                    port = read_port(job)
                    stats = rc(port, "core/stats") if active and port else None
                    code = None if active else read_exit(job)
                    disk = (dir_size(job["dest"]) if not is_up(job) and (tick % 10 == 0 or not active)
                            else None)
                    results[job["id"]] = (active, stats, code, disk)
                GLib.idle_add(self.win.apply_poll, results)
                tick += 1
                time.sleep(1)

        # tray content -------------------------------------------------
        def _job_stats(self, jid):
            card = self.win.cards.get(jid) if self.win else None
            return (card.last_stats or {}) if card else {}

        def tray_status(self):
            jobs = self.store.jobs
            running = [j for j in jobs if j["status"] == "running"]
            errors = [j for j in jobs if j["status"] == "error"]
            speed = sum(self._job_stats(j["id"]).get("speed", 0) or 0 for j in running)
            lines = []
            for j in running[:5]:
                s = self._job_stats(j["id"])
                tot = s.get("totalBytes") or 0
                pct = f"{s.get('bytes', 0) / tot * 100:.0f}%" if tot else "…"
                lines.append(f"{'⬆' if is_up(j) else '⬇'} {j['name']} — {pct}")
            if running:
                title = _("Активных: {0} · {1}/с").format(len(running), human(speed))
            elif errors:
                title = _("Ошибок: {0}").format(len(errors))
            else:
                title = _("Нет активных передач")
            icon = f"{APP_ID}-alert" if errors and not running else (
                f"{APP_ID}-active" if running else APP_ID)
            return icon, (f"{APP_NAME}: {title}", "\n".join(lines))

        def _goto(self, page):
            self.show_window()
            self.win.show_jobs(page == "up")

        def tray_menu(self):
            win = self.win
            visible = bool(win and win.get_visible())
            E = [{"id": 1, "label": _("Скрыть окно") if visible else _("Показать окно"),
                  "app_icon": True, "cb": self.toggle_window},
                 {"id": 2, "separator": True}]
            jobs = sorted(self.store.jobs, key=lambda j: (j["status"] != "running", -j.get("created", 0)))[:7]
            for j in jobs:
                mid = self.job_menu_ids.setdefault(j["id"], 1000 + len(self.job_menu_ids))
                arrow = "⬆" if is_up(j) else "⬇"
                name = j["name"] if len(j["name"]) <= 28 else j["name"][:27] + "…"
                if j["status"] == "running":
                    s = self._job_stats(j["id"])
                    tot = s.get("totalBytes") or 0
                    pct = f"{s.get('bytes', 0) / tot * 100:.0f}%" if tot else _("подготовка")
                    info = _("{0} · {1}/с").format(pct, human(s.get('speed', 0)))
                else:
                    info = status_text(j).lower()
                page = "up" if is_up(j) else "down"
                E.append({"id": mid, "label": f"{arrow}  {name} — {info}",
                          "cb": lambda p=page: self._goto(p)})
            if not jobs:
                E.append({"id": 3, "label": _("Передач пока нет"), "enabled": False})
            E.append({"id": 4, "separator": True})
            if any(j["status"] == "running" for j in self.store.jobs):
                E.append({"id": 5, "label": _("Приостановить все"), "icon": "media-playback-pause-symbolic",
                          "cb": lambda: win.pause_all()})
            else:
                can = any(j["status"] in ("paused", "interrupted", "error") for j in self.store.jobs)
                E.append({"id": 5, "label": _("Продолжить все"), "icon": "media-playback-start-symbolic",
                          "enabled": can, "cb": lambda: win.resume_all()})
            E.append({"id": 6, "label": _("Новая загрузка…"), "icon": ICON_DOWN,
                      "cb": lambda: (self.show_window(), win.show_add())})
            E.append({"id": 7, "label": _("Выгрузить на Диск…"), "icon": ICON_UP,
                      "cb": lambda: (self.show_window(), win.show_upload())})
            E.append({"id": 8, "label": _("Открыть папку «Загрузки»"), "icon": "folder-open-symbolic",
                      "cb": lambda: subprocess.Popen(["xdg-open", str(HOME / "Downloads")])})
            if self.accounts:
                E.append({"id": 9, "separator": True})
                for i, a in enumerate(self.accounts):
                    E.append({"id": 2000 + i, "label": self.account_label(a["name"]),
                              "radio": a["name"] == self.active,
                              "cb": lambda n=a["name"]: self.set_active(n)})
            E.append({"id": 10, "separator": True})
            E.append({"id": 11, "label": _("Выход"), "icon": "application-exit-symbolic", "cb": self.quit})
            return E

        # dialogs ------------------------------------------------------
        def show_about(self):
            Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID,
                            version=VERSION, developer_name="rclone + GTK4/libadwaita",
                            comments=_("Потоковая загрузка и выгрузка Google Drive через rclone "
                                     "с продолжением после перезагрузки.")).present(self.win)

        def show_settings(self):
            self.show_window()
            dlg = Adw.PreferencesDialog(title=_("Настройки"))
            page = Adw.PreferencesPage()

            acc = Adw.PreferencesGroup(title=_("Аккаунты Google"),
                                       description=_("Активный аккаунт используется для новых передач. "
                                                   "Переключать можно и в заголовке окна, и в трее."))
            add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text=_("Добавить аккаунт"))
            add_btn.add_css_class("flat")
            acc.set_header_suffix(add_btn)
            rows = []
            ro = Adw.SwitchRow(title=_("Только чтение"),
                               subtitle=_("Без права выгрузки; применяется при следующем входе"))

            def rebuild():
                for r in rows:
                    acc.remove(r)
                rows.clear()
                if not self.accounts:
                    r = Adw.ActionRow(title=_("Нет аккаунтов"), subtitle=_("Нажмите +, чтобы войти"))
                    acc.add(r)
                    rows.append(r)
                for a in self.accounts:
                    n = a["name"]
                    sub = [f"rclone: {n}",
                           _("только чтение") if "readonly" in self.account_scope(n) else _("чтение и запись")]
                    if a.get("total"):
                        sub.append(_("занято {0} из {1}").format(human(a.get('used')), human(a['total'])))
                    r = Adw.ActionRow(title=GLib.markup_escape_text(self.account_label(n)),
                                      subtitle=GLib.markup_escape_text(" · ".join(sub)), activatable=True)
                    r.add_prefix(Gtk.Image.new_from_icon_name(
                        "object-select-symbolic" if n == self.active else "avatar-default-symbolic"))
                    r.connect("activated", lambda _r, n=n: self.set_active(n))
                    relog = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                                       tooltip_text=_("Войти заново (с правом записи, если «Только чтение» выключено)"))
                    relog.add_css_class("flat")
                    relog.connect("clicked", lambda *_a, n=n: self.login(n, readonly=ro.get_active()))
                    rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text=_("Удалить аккаунт из приложения"))
                    rm.add_css_class("flat")
                    rm.connect("clicked", lambda *_a, n=n: confirm_remove(n))
                    r.add_suffix(relog)
                    r.add_suffix(rm)
                    acc.add(r)
                    rows.append(r)

            def confirm_remove(n):
                d = Adw.AlertDialog(heading=_("Удалить {0}?").format(self.account_label(n)),
                                    body=_("Приложение забудет вход в этот аккаунт. "
                                         "Файлы на Диске и на компьютере не тронутся."))
                d.add_response("cancel", _("Отмена"))
                d.add_response("rm", _("Удалить"))
                d.set_response_appearance("rm", Adw.ResponseAppearance.DESTRUCTIVE)
                d.connect("response", lambda _d, r: r == "rm" and self.remove_account(n))
                d.present(dlg)

            adv = Adw.ExpanderRow(title=_("Параметры входа"), subtitle=_("Только чтение"))
            adv.add_row(ro)
            s = load_json(SETTINGS_FILE, {})
            key_row = Adw.ActionRow(title=_("Свой ключ Google"),
                                    subtitle=(_("настроен — без лимитов общего ключа") if s.get("client_id")
                                              else _("убирает тормоза общего ключа rclone")),
                                    activatable=True)
            key_row.add_prefix(Gtk.Image(icon_name="dialog-password-symbolic"))
            key_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            key_row.connect("activated", lambda *_a: KeyDialog(self).present(dlg))
            add_btn.connect("clicked", lambda *_a: self.login(None, readonly=ro.get_active()))
            rebuild()
            self.settings_hooks.append(rebuild)
            dlg.connect("closed", lambda *_a: rebuild in self.settings_hooks and self.settings_hooks.remove(rebuild))
            page.add(acc)
            g_adv = Adw.PreferencesGroup()
            g_adv.add(key_row)
            g_adv.add(adv)
            page.add(g_adv)

            tray = Adw.PreferencesGroup(title=_("Трей"))
            sw_close = Adw.SwitchRow(title=_("Сворачивать в трей при закрытии окна"),
                                     subtitle=_("Значок в трее показывает скорость и управляет передачами"),
                                     active=setting("close_to_tray", True))
            sw_close.connect("notify::active", lambda r, _p: set_setting("close_to_tray", r.get_active()))
            sw_auto = Adw.SwitchRow(title=_("Запускать в трее при входе в систему"),
                                    active=tray_autostart_enabled())
            sw_auto.connect("notify::active", lambda r, _p: set_tray_autostart(r.get_active()))
            tray.add(sw_close)
            tray.add(sw_auto)
            page.add(tray)

            auto = Adw.PreferencesGroup(title=_("Перезагрузка"))
            sw = Adw.SwitchRow(title=_("Продолжать передачи после входа в систему"),
                               subtitle=_("Незавершённые загрузки и выгрузки стартуют сами, даже без окна"),
                               active=autostart_enabled())
            sw.connect("notify::active", lambda r, _p: set_autostart(r.get_active()))
            auto.add(sw)
            page.add(auto)

            lang = Adw.PreferencesGroup(title=_("Язык") + ("" if LANG == "en" else " · Language"))
            langs = available_langs()
            codes = ["system"] + list(langs)
            sys_name = langs.get(system_lang(), langs.get("en", "English"))
            names = [_("Системный ({0})").format(sys_name)] + list(langs.values())
            row = Adw.ComboRow(title=_("Язык интерфейса"), model=Gtk.StringList.new(names),
                               subtitle=_("Меняется после перезапуска приложения; передачи не прерываются"))
            row.set_selected(codes.index(LANG_CHOICE) if LANG_CHOICE in codes else 0)

            def on_lang(r, _p):
                code = codes[r.get_selected()]
                if code == setting("lang", "system"):
                    return
                set_setting("lang", code)
                t = Adw.Toast(title=_("Язык сменится после перезапуска"), button_label=_("Перезапустить"),
                              timeout=0)
                t.connect("button-clicked", lambda *_a: self.restart())
                dlg.add_toast(t)
            row.connect("notify::selected", on_lang)
            lang.add(row)
            page.add(lang)
            dlg.add(page)
            dlg.present(self.win)

        def restart(self):
            """Start a fresh instance once this one has exited (jobs are systemd units)."""
            subprocess.Popen(["sh", "-c", 'sleep 1; exec "$@"', "sh", *SELF_CMD],
                             start_new_session=True)
            self.quit()

    sys.exit(App().run([sys.argv[0]]))


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--prep":
        prep_download(sys.argv[2], sys.argv[3])
        return
    if "--resume" in sys.argv:
        names = resume_unfinished(Store())
        print("resumed:", ", ".join(names) if names else "nothing")
        return
    refresh_autostart()
    run_gui(start_hidden="--hidden" in sys.argv)


if __name__ == "__main__":
    main()

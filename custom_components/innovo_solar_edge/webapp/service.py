#!/usr/bin/env python3
"""Innovo Solar Edge web app: generated looks, live previews and the viewer page.

Runs as its own process, started and watched by the integration (see control.py), so
it can be updated and restarted without restarting Home Assistant. It uses the
integration's drawing code (screens.py, scenes.py, themes.py, fonts) and reads the
live screen data the integration hands over every minute (the "handoff" file).

It owns:
  * generated looks - <publish dir>/<style>--<palette>-<mode>/<page>-<shape>-<res>.jpg|png,
    redrawn every minute (other shapes every 2 minutes);
  * screens.json, index.html and previews/ (style thumbnails) in the publish folder;
  * a small local HTTP service: POST /command (generate / want / remove / clean),
    GET /preview (one screen of a look that hasn't been generated), GET /ping.
The original screens at the top of the publish folder stay with the integration.

Pictures are drawn by a pool of worker processes (several CPU cores at once), from a
priority queue: the picture someone is looking at first ("want"), then a new look's
square JPGs, its other shapes, its PNGs, and last the regular once-a-minute redraws.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import heapq
import io
import ipaddress
import itertools
import json
import multiprocessing
import os
import re
import shutil
import signal
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

import themes  # noqa: E402  (the integration's drawing code, loaded on its own)
from screens import FORMATS, PAGES, VARIANTS, export, is_ready, render  # noqa: E402

VERSION = "1.18.0"            # also PAGE_VERSION in web/index.html
SITE_PAGE, SITE_INFO, PREVIEWS = "index.html", "screens.json", "previews"
MAX_LOOKS = 4
SHAPES_EVERY = 2              # non-square shapes every N minutes
SLOW_PAGES = {"solar": 10, "week": 10}
PREVIEW_EVERY = 6 * 3600      # style thumbnails
LOOK_RE = re.compile(r"^([a-z]+)--([a-z]+)-([a-z]+)$")
CLEANABLE = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tmp", ".html", ".json", ".tgz")
NAMES = PAGES + list(VARIANTS)
# queue order (lower first)
P_WANT, P_SQUARE, P_SHAPES, P_PNG, P_THUMB, P_ROUND = 0, 1, 2, 3, 4, 5
HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Private-Network": "true",
    "Cache-Control": "no-store",
}


def log(msg: str) -> None:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def write_atomic(target: str, data: bytes) -> None:
    tmp = target + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)


def _decode(obj):
    if isinstance(obj, dict) and "__dt__" in obj:
        return datetime.datetime.fromisoformat(obj["__dt__"])
    return obj


def root_names() -> set[str]:
    """Files the integration publishes at the top of the folder (the original screens)."""
    names = set()
    for page in NAMES:
        for kind in ("jpg", "png"):
            names.add(f"{page}.{kind}")
            names.update(f"{page}-{shape}-{res}.{kind}" for shape, sizes in FORMATS.items() for res in sizes)
    return names


def styled(data: dict, look: str) -> dict:
    style, palette, mode = LOOK_RE.match(look).groups()
    return {**data, "_style": style, "_theme": f"{palette}-{mode}"}


# --- run in the worker processes -----------------------------------------------------------
def _worker_init(parent: int) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        os.nice(5)  # Home Assistant first when the box is busy
    except (AttributeError, OSError):
        pass

    def watch_parent():  # the web app is gone (restarted or stopped): stop too
        while True:
            time.sleep(2)
            if not _alive(parent):
                os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()


def _alive(pid: int) -> bool:
    if hasattr(os, "getppid") and os.name != "nt":
        return os.getppid() == pid
    try:  # Windows (tests): no re-parenting, ask the OS
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return True


def draw_picture(page: str, data: dict, shape: str, kind: str) -> dict:
    return export(page, data, [shape], (kind,))


def draw_thumbnail(style: str, data: dict) -> bytes:
    from PIL import Image

    png = render("live", {**data, "_style": style, "_theme": f"{themes.default_palette(style)}-dark"})
    img = Image.open(io.BytesIO(png)).convert("RGB").resize((360, 360), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


class WebApp:
    def __init__(self, handoff: str, state: str, workers: int):
        self.handoff_path, self.state_path = handoff, state
        self.handoff: dict = {}
        self.handoff_mtime = 0.0
        self.looks: list[str] = []
        self.port = 0
        self.workers = workers
        self.pool = self._new_pool()
        self.queue: list = []                  # heap of [priority, seq, key, job]
        self.queued: dict = {}                 # key -> its heap entry (to re-prioritise)
        self.qlock = threading.Condition()
        self.slots = threading.Semaphore(workers)
        self.seq = itertools.count()
        self.round_left = 0                    # regular redraws still queued / drawing
        self.preview_lock = threading.Lock()   # previews: one at a time, drawn right here
        self.site_dirty = threading.Event()
        self.wake = threading.Event()
        self.ticks = 0
        self.previews_at = -1e9
        self.updated = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._load_state()

    def _new_pool(self):
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=self.workers, mp_context=multiprocessing.get_context("spawn"),
            initializer=_worker_init, initargs=(os.getpid(),))

    def stop_workers(self) -> None:
        procs = list((getattr(self.pool, "_processes", None) or {}).values())
        self.pool.shutdown(wait=False, cancel_futures=True)
        for proc in procs:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    # --- state and handoff ---------------------------------------------------------------
    def _load_state(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.looks = [k for k in json.load(f).get("looks", []) if LOOK_RE.match(k)]
            self.seeded = True
        except (OSError, ValueError):
            self.looks, self.seeded = [], False

    def _save_state(self) -> None:
        write_atomic(self.state_path, json.dumps({"looks": self.looks}).encode())

    def read_handoff(self) -> bool:
        try:
            mtime = os.path.getmtime(self.handoff_path)
            if mtime == self.handoff_mtime:
                return False
            with open(self.handoff_path, encoding="utf-8") as f:
                self.handoff = json.load(f, object_hook=_decode)
            self.handoff_mtime = mtime
        except (OSError, ValueError) as err:
            if not self.handoff:
                log(f"waiting for the integration's data ({err})")
            return False
        if not self.seeded:  # first start: take over the looks the integration had
            self.looks = [k for k in self.handoff.get("looks_seed", []) if LOOK_RE.match(k)][:MAX_LOOKS]
            self.seeded = True
            self._save_state()
        return True

    @property
    def folder(self) -> str | None:
        return self.handoff.get("publish_dir")

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self.handoff.get("kinds") or ("jpg",))

    def data(self) -> dict:
        return dict(self.handoff.get("data") or {})

    # --- the drawing queue -------------------------------------------------------------------
    def enqueue(self, priority: int, key: tuple, job: tuple) -> None:
        """key: ("pic", look, page, shape, kind) or ("thumb", style). A picture already
        waiting keeps its place unless this asks for it sooner."""
        with self.qlock:
            old = self.queued.get(key)
            if old is not None:
                if old[0] <= priority:
                    return
                old[3] = None  # superseded
                if old[0] == P_ROUND:
                    self.round_left -= 1
            entry = [priority, next(self.seq), key, job]
            self.queued[key] = entry
            if priority == P_ROUND:
                self.round_left += 1
            heapq.heappush(self.queue, entry)
            self.qlock.notify()

    def drop_look(self, look: str) -> None:
        with self.qlock:
            for key, entry in list(self.queued.items()):
                if key[0] == "pic" and key[1] == look:
                    entry[3] = None
                    del self.queued[key]
                    if entry[0] == P_ROUND:
                        self.round_left -= 1

    def dispatch(self) -> None:
        """Keeps every worker busy with the most urgent picture."""
        while True:
            self.slots.acquire()
            with self.qlock:
                while True:
                    while not self.queue:
                        self.qlock.wait()
                    entry = heapq.heappop(self.queue)
                    if entry[3] is not None:
                        break
                key, job, priority = entry[2], entry[3], entry[0]
                if self.queued.get(key) is entry:
                    del self.queued[key]
            try:
                future = self.pool.submit(*job)
            except concurrent.futures.process.BrokenProcessPool:
                log("drawing workers stopped - starting new ones")
                self.pool = self._new_pool()
                future = self.pool.submit(*job)
            future.add_done_callback(lambda f, k=key, p=priority: self._done(k, p, f))

    def _done(self, key: tuple, priority: int, future) -> None:
        try:
            if priority == P_ROUND:
                with self.qlock:
                    self.round_left -= 1
            try:
                result = future.result()
            except Exception:  # noqa: BLE001
                log(f"drawing {key} failed:\n{traceback.format_exc()}")
                return
            if key[0] == "thumb":
                sub = os.path.join(self.folder, PREVIEWS)
                os.makedirs(sub, exist_ok=True)
                write_atomic(os.path.join(sub, f"{key[1]}.jpg"), result)
                return
            _, look, page, shape, kind = key
            if look not in self.looks:  # removed meanwhile
                return
            sub = os.path.join(self.folder, look)
            os.makedirs(sub, exist_ok=True)
            # hi-res before low-res: the low-res file is what says "this picture is there"
            names = sorted(result, key=lambda n: "-lowres." in n)
            for name in names:
                write_atomic(os.path.join(sub, name), result[name])
            if shape == "1x1" and (square := result.get(f"{page}-1x1-lowres.{kind}")):
                write_atomic(os.path.join(sub, f"{page}.{kind}"), square)
            if priority != P_ROUND:
                self.site_dirty.set()
        except OSError as err:
            if key[0] == "pic" and key[1] in self.looks:
                log(f"can't write {key}: {err}")
        except Exception:  # noqa: BLE001
            log(f"writing {key} failed:\n{traceback.format_exc()}")
        finally:
            self.slots.release()

    def queue_look(self, look: str, data: dict, priority_of, names=NAMES, shapes=None, kinds=None) -> None:
        data = styled(data, look)
        for kind in kinds or self.kinds:
            for shape in shapes or list(FORMATS):
                for page in names:
                    self.enqueue(priority_of(kind, shape), ("pic", look, page, shape, kind),
                                 (draw_picture, page, data, shape, kind))

    # --- requests ---------------------------------------------------------------------------
    @staticmethod
    def look_key(style: str, palette: str, mode: str) -> str:
        if style not in themes.STYLES or palette not in themes.THEMES or mode not in themes.MODES:
            raise ValueError(f"Unknown look: style {style!r}, colours {palette!r}, mode {mode!r}")
        return f"{style}--{palette}-{mode}"

    def generate(self, style: str, palette: str, mode: str, want: dict | None = None) -> str:
        if not self.folder:
            raise ValueError("Set a publish folder in the integration settings first")
        look = self.look_key(style, palette, mode)
        if look not in self.looks:
            if len(self.looks) >= MAX_LOOKS:
                raise ValueError(f"Up to {MAX_LOOKS} looks; remove one first")
            self.looks.append(look)
            self._save_state()
        data = self.data()
        if want:
            self.want(look, want.get("pages") or [], want.get("shape", "1x1"), want.get("kind", "jpg"))
        self.queue_look(look, data, lambda kind, shape: P_PNG if kind == "png" else P_SQUARE if shape == "1x1" else P_SHAPES)
        self.write_site()
        return look

    def want(self, look: str, pages, shape: str, kind: str) -> None:
        """Someone is looking at these pictures: draw them next."""
        if look not in self.looks or shape not in FORMATS or kind not in self.kinds:
            return
        pages = [p for p in pages if p in NAMES]
        sub = os.path.join(self.folder, look)
        missing = [p for p in pages if not os.path.exists(os.path.join(sub, f"{p}-{shape}-lowres.{kind}"))]
        if missing:
            self.queue_look(look, self.data(), lambda *_: P_WANT, names=missing, shapes=[shape], kinds=[kind])

    def remove(self, look: str) -> None:
        if look in self.looks:
            self.looks.remove(look)
            self._save_state()
        self.drop_look(look)
        self.write_site()
        if self.folder and LOOK_RE.match(look):
            shutil.rmtree(os.path.join(self.folder, look), ignore_errors=True)
            self.write_site()

    def preview(self, style: str, palette: str, mode: str, page: str, shape: str) -> bytes:
        self.look_key(style, palette, mode)
        if page not in NAMES or shape not in FORMATS:
            raise ValueError(f"Unknown page {page!r} or shape {shape!r}")
        data = {**self.data(), "_style": style, "_theme": f"{palette}-{mode}"}
        if not data.get("now"):
            raise ValueError("No data from the integration yet")
        with self.preview_lock:  # drawn here, on a core the workers leave free
            return export(page, data, [shape], ("jpg",))[f"{page}-{shape}-lowres.jpg"]

    # --- clean-up ----------------------------------------------------------------------------
    def cleanable(self) -> list[dict]:
        if not self.folder or not os.path.isdir(self.folder):
            return []
        keep = {SITE_PAGE, SITE_INFO, PREVIEWS, *self.looks, *root_names()}
        out = []
        for entry in sorted(os.listdir(self.folder)):
            if entry in keep:
                continue
            path = os.path.join(self.folder, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                files = [os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs]
                if all(f.lower().endswith(CLEANABLE) for f in files):
                    out.append({"name": entry + "/", "files": len(files), "bytes": sum(os.path.getsize(f) for f in files)})
            elif entry.lower().endswith(CLEANABLE):
                out.append({"name": entry, "files": 1, "bytes": os.path.getsize(path)})
        return out

    def clean(self) -> int:
        removed = 0
        for item in self.cleanable():
            path = os.path.join(self.folder, item["name"].rstrip("/"))
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
        self.write_site()
        log(f"cleaned {removed} unused files/folders")
        return removed

    # --- screens.json and the page ---------------------------------------------------------
    def look_info(self, look: str) -> dict:
        style, palette, mode = LOOK_RE.match(look).groups()
        sub = os.path.join(self.folder, look)
        try:
            entries = os.listdir(sub)
        except OSError:
            entries = []
        present = set(entries)
        # what is there: {kind: {shape: [pages]}} (a low-res file means its hi-res is there too)
        have = {kind: {shape: [p for p in NAMES if f"{p}-{shape}-lowres.{kind}" in present] for shape in FORMATS}
                for kind in self.kinds}
        complete = all(len(pages) == len(NAMES) for shapes in have.values() for pages in shapes.values())
        shapes = [s for s, pages in have.get("jpg", {}).items() if len(pages) == len(NAMES)]
        size = 0
        for name in entries:
            try:
                size += os.path.getsize(os.path.join(sub, name))
            except OSError:
                pass
        return {"key": look, "style": style, "palette": palette, "mode": mode, "have": have,
                "complete": complete, "shapes": shapes, "ready": "1x1" in shapes,
                "files": len(entries), "bytes": size}

    def write_site(self) -> None:
        if not self.folder:
            return
        os.makedirs(self.folder, exist_ok=True)
        h = self.handoff

        def hexes(values):
            return {k.lower(): "#%02x%02x%02x" % values[k] for k in ("BG_TOP", "TEXT", "CYAN", "SOLAR", "HOME", "GRID", "BATT")}

        has_battery = (h.get("data") or {}).get("has_battery")
        info = {
            "updated": max(self.updated, h.get("root_updated") or ""),
            "pages": [p for p in NAMES if not (p == "battery" and has_battery is False)],
            "shapes": {shape: {res: f"{w}x{h_}" for res, (w, h_) in sizes.items()} for shape, sizes in FORMATS.items()},
            "solar_only": (h.get("data") or {}).get("has_grid") is False,
            "formats": list(self.kinds),
            "looks": [self.look_info(k) for k in list(self.looks) if LOOK_RE.match(k)],
            "cleanable": self.cleanable(),
            "page_version": VERSION,
            "max_looks": MAX_LOOKS,
            "styles": [{"key": k, "label": v["label"], "blurb": themes.BLURBS.get(k, ""), "preview": f"{PREVIEWS}/{k}.jpg"}
                       for k, v in themes.STYLES.items()],
            "palettes": [{"key": k, "label": v["label"], "dark": hexes(v["dark"]), "light": hexes(v["light"])}
                         for k, v in themes.THEMES.items()],
            "modes": [[m, themes.MODE_LABELS[m]] for m in themes.MODES],
            "control": {"port": self.port, "preview": True, "want": True},
        }
        write_atomic(os.path.join(self.folder, SITE_INFO), json.dumps(info).encode())

    def write_page(self) -> None:
        if self.folder:
            with open(os.path.join(PKG, "web", SITE_PAGE), "rb") as f:
                write_atomic(os.path.join(self.folder, SITE_PAGE), f.read())

    def site_writer(self) -> None:
        """screens.json at most once a second while new pictures arrive."""
        while True:
            self.site_dirty.wait()
            self.site_dirty.clear()
            try:
                self.write_site()
            except Exception:  # noqa: BLE001
                log(f"writing {SITE_INFO} failed:\n{traceback.format_exc()}")
            time.sleep(1)

    # --- the minute loop -----------------------------------------------------------------------
    def run(self) -> None:
        page_written_for = None
        while True:
            self.wake.wait(timeout=max(1.0, 60 - time.time() % 60))  # each minute
            self.wake.clear()
            self.read_handoff()
            if not self.folder:
                continue
            try:
                if page_written_for != self.folder:
                    self.write_page()
                    self.write_site()
                    page_written_for = self.folder
                data = self.data()
                with self.qlock:
                    busy = self.round_left > 0
                if self.looks and data.get("now") and not busy:  # skipped while the last round still runs
                    self.ticks += 1
                    shapes = list(FORMATS) if self.ticks % SHAPES_EVERY == 1 else ["1x1"]
                    names = [n for n in NAMES if n not in SLOW_PAGES or self.ticks % SLOW_PAGES[n] == 1]
                    for look in list(self.looks):
                        self.queue_look(look, data, lambda *_: P_ROUND, names=names, shapes=shapes)
                    self.updated = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if time.monotonic() - self.previews_at > PREVIEW_EVERY and is_ready("live", data):
                    self.previews_at = time.monotonic()
                    for style in themes.STYLES:
                        self.enqueue(P_THUMB, ("thumb", style), (draw_thumbnail, style, data))
                self.write_site()
            except Exception:  # noqa: BLE001 - keep serving
                log(f"publish round failed:\n{traceback.format_exc()}")


def _local(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "innovo-screens"

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def do_OPTIONS(self):
            self._send(204, b"", "text/plain")

        def do_GET(self):
            if not _local(self.client_address[0]):
                return self._json(403, {"error": "local network only"})
            url = urllib.parse.urlparse(self.path)
            if url.path == "/ping":
                with app.qlock:
                    queued = len(app.queued)
                return self._json(200, {"ok": True, "version": VERSION, "looks": app.looks,
                                        "workers": app.workers, "queued": queued})
            if url.path == "/preview":
                q = dict(urllib.parse.parse_qsl(url.query))
                try:
                    image = app.preview(q.get("style", ""), q.get("palette", ""), q.get("mode", "dark"),
                                        q.get("page", "overview"), q.get("shape", "1x1"))
                except ValueError as err:
                    return self._json(400, {"error": str(err)})
                except Exception as err:  # noqa: BLE001
                    log(f"preview failed:\n{traceback.format_exc()}")
                    return self._json(500, {"error": str(err)})
                return self._send(200, image, "image/jpeg")
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not _local(self.client_address[0]):
                return self._json(403, {"error": "local network only"})
            if urllib.parse.urlparse(self.path).path != "/command":
                return self._json(404, {"error": "not found"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                body = json.loads(self.rfile.read(length) or b"{}")
                action = str(body.get("action", ""))
                want = {"pages": body.get("pages") or [], "shape": str(body.get("shape", "1x1")),
                        "kind": str(body.get("kind", "jpg"))}
                if action == "generate":
                    result = {"look": app.generate(body.get("style", ""), body.get("palette", ""), body.get("mode", "dark"),
                                                   want if want["pages"] else None)}
                elif action == "want":
                    app.want(str(body.get("look", "")), want["pages"], want["shape"], want["kind"])
                    result = {"ok": True}
                elif action == "remove":
                    app.remove(str(body.get("look", "")))
                    result = {"removed": body.get("look")}
                elif action == "clean":
                    result = {"cleaned": app.clean()}
                else:
                    return self._json(400, {"error": f"unknown action {action!r}"})
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            except Exception as err:  # noqa: BLE001
                log(f"command failed:\n{traceback.format_exc()}")
                return self._json(500, {"error": str(err)})
            self._json(200, result)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handoff", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--workers", type=int, default=0, help="drawing processes (default: CPU cores - 2, up to 6)")
    args = ap.parse_args()
    workers = args.workers or max(1, min(6, (os.cpu_count() or 2) - 2))
    app = WebApp(args.handoff, args.state, workers)
    app.port = args.port
    parent = os.getppid()

    def stop(*_):
        app.stop_workers()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    server.daemon_threads = True
    for target in (server.serve_forever, app.dispatch, app.site_writer):
        threading.Thread(target=target, daemon=True).start()

    def watch_parent():  # stop when the integration (Home Assistant) is gone, so a restart finds the port free
        while True:
            time.sleep(5)
            if os.getppid() != parent:
                log("parent process gone - stopping")
                stop()

    threading.Thread(target=watch_parent, daemon=True).start()
    log(f"web app {VERSION} on port {args.port}, {workers} drawing processes")
    app.read_handoff()
    app.wake.set()
    app.run()


if __name__ == "__main__":
    main()

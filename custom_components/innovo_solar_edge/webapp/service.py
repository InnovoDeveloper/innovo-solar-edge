#!/usr/bin/env python3
"""Innovo Solar Edge web app: generated looks, live previews and the viewer page.

Runs as its own process, started and watched by the integration (see control.py), so
it can be updated and restarted without restarting Home Assistant. It uses the
integration's drawing code (screens.py, scenes.py, themes.py, fonts) and reads the
live screen data the integration hands over every minute (the "handoff" file).

It owns:
  * generated looks - <publish dir>/<style>--<palette>-<mode>/<page>-<shape>-<res>.jpg|png,
    redrawn every minute (other shapes every 2 minutes), new ones jump the queue;
  * screens.json, index.html and previews/ (style thumbnails) in the publish folder;
  * a small local HTTP service: POST /command (generate / remove / clean),
    GET /preview (one screen of a look that hasn't been generated), GET /ping.
The original screens at the top of the publish folder stay with the integration.
"""

from __future__ import annotations

import argparse
import datetime
import io
import ipaddress
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)


import themes  # noqa: E402
from screens import FORMATS, PAGES, VARIANTS, export, is_ready, render  # noqa: E402

VERSION = "1.16.1"            # also PAGE_VERSION in web/index.html
SITE_PAGE, SITE_INFO, PREVIEWS = "index.html", "screens.json", "previews"
MAX_LOOKS = 4
SHAPES_EVERY = 2              # non-square shapes every N minutes
SLOW_PAGES = {"solar": 10, "week": 10}
PREVIEW_EVERY = 6 * 3600      # style thumbnails
LOOK_RE = re.compile(r"^([a-z]+)--([a-z]+)-([a-z]+)$")
CLEANABLE = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tmp", ".html", ".json", ".tgz")
NAMES = PAGES + list(VARIANTS)
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


class WebApp:
    def __init__(self, handoff: str, state: str):
        self.handoff_path, self.state_path = handoff, state
        self.handoff: dict = {}
        self.handoff_mtime = 0.0
        self.looks: list[str] = []
        self.urgent: list[str] = []            # new looks: square pictures first
        self.render_lock = threading.Lock()    # one drawing at a time
        self.previews_waiting = 0              # previews go before publishing
        self.pv_cond = threading.Condition()
        self.wake = threading.Event()
        self.ticks = 0
        self.previews_at = -1e9
        self.updated = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._load_state()

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

    # --- drawing ---------------------------------------------------------------------------
    def _yield_to_previews(self) -> None:
        with self.pv_cond:
            while self.previews_waiting:
                self.pv_cond.wait(5)

    def _draw(self, page: str, data: dict, shape: str, kinds) -> dict:
        self._yield_to_previews()
        with self.render_lock:
            return export(page, data, [shape], kinds)

    def publish_look(self, look: str, data: dict, names, shapes, kinds) -> None:
        style, palette, mode = LOOK_RE.match(look).groups()
        styled = {**data, "_style": style, "_theme": f"{palette}-{mode}"}
        sub = os.path.join(self.folder, look)
        os.makedirs(sub, exist_ok=True)
        for shape in shapes:
            for name in names:
                self.serve_urgent(data)
                if look not in self.looks:  # removed meanwhile
                    return
                try:
                    files = self._draw(name, styled, shape, kinds)
                except Exception:  # noqa: BLE001
                    log(f"drawing {name} {shape} in {look} failed:\n{traceback.format_exc()}")
                    continue
                if look not in self.looks:
                    return
                if shape == "1x1":
                    for kind in kinds:
                        if square := files.get(f"{name}-1x1-lowres.{kind}"):
                            files[f"{name}.{kind}"] = square
                try:
                    for file_name, image in files.items():
                        write_atomic(os.path.join(sub, file_name), image)
                except OSError as err:
                    if look in self.looks:  # not just removed meanwhile
                        log(f"can't write look {look}: {err}")
                    return

    def serve_urgent(self, data: dict) -> None:
        """New looks: every view in the square shape first (JPG, a few seconds), then the rest."""
        if getattr(self, "_serving", False) or not self.urgent:
            return
        self._serving = True
        try:
            while self.urgent:
                look = self.urgent[0]
                if look in self.looks:
                    self.publish_look(look, data, NAMES, ["1x1"], ("jpg",))
                self.urgent.pop(0)
                self.write_site()
                if look in self.looks:
                    self.publish_look(look, data, NAMES, [s for s in FORMATS if s != "1x1"], ("jpg",))
                    self.write_site()
                if look in self.looks and "png" in self.kinds:  # then the transparent PNGs, square first
                    self.publish_look(look, data, NAMES, list(FORMATS), ("png",))
                    self.write_site()
        finally:
            self._serving = False

    def write_previews(self, data: dict) -> None:
        from PIL import Image

        sub = os.path.join(self.folder, PREVIEWS)
        os.makedirs(sub, exist_ok=True)
        for style in themes.STYLES:
            self.serve_urgent(data)  # a new look goes first
            self._yield_to_previews()
            try:
                with self.render_lock:
                    png = render("live", {**data, "_style": style, "_theme": f"{themes.default_palette(style)}-dark"})
                img = Image.open(io.BytesIO(png)).convert("RGB").resize((360, 360), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=82)
                write_atomic(os.path.join(sub, f"{style}.jpg"), buf.getvalue())
            except Exception:  # noqa: BLE001
                log(f"style thumbnail {style} failed:\n{traceback.format_exc()}")

    def preview(self, style: str, palette: str, mode: str, page: str, shape: str) -> bytes:
        self.look_key(style, palette, mode)
        if page not in NAMES or shape not in FORMATS:
            raise ValueError(f"Unknown page {page!r} or shape {shape!r}")
        data = {**self.data(), "_style": style, "_theme": f"{palette}-{mode}"}
        if not data.get("now"):
            raise ValueError("No data from the integration yet")
        with self.pv_cond:
            self.previews_waiting += 1
        try:
            with self.render_lock:
                return export(page, data, [shape], ("jpg",))[f"{page}-{shape}-lowres.jpg"]
        finally:
            with self.pv_cond:
                self.previews_waiting -= 1
                self.pv_cond.notify_all()

    # --- looks, clean-up --------------------------------------------------------------------
    @staticmethod
    def look_key(style: str, palette: str, mode: str) -> str:
        if style not in themes.STYLES or palette not in themes.THEMES or mode not in themes.MODES:
            raise ValueError(f"Unknown look: style {style!r}, colours {palette!r}, mode {mode!r}")
        return f"{style}--{palette}-{mode}"

    def generate(self, style: str, palette: str, mode: str) -> str:
        if not self.folder:
            raise ValueError("Set a publish folder in the integration settings first")
        look = self.look_key(style, palette, mode)
        if look not in self.looks:
            if len(self.looks) >= MAX_LOOKS:
                raise ValueError(f"Up to {MAX_LOOKS} looks; remove one first")
            self.looks.append(look)
            self._save_state()
        if look not in self.urgent:
            self.urgent.append(look)
        self.write_site()
        self.wake.set()
        return look

    def remove(self, look: str) -> None:
        if look in self.looks:
            self.looks.remove(look)
            self._save_state()
        if look in self.urgent:
            self.urgent.remove(look)
        self.write_site()
        if self.folder and LOOK_RE.match(look):
            shutil.rmtree(os.path.join(self.folder, look), ignore_errors=True)
            self.write_site()

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
        # a shape is there once the last page of a round (NAMES[-1]) is written for it
        formats = {kind: [s for s in FORMATS if os.path.exists(os.path.join(sub, f"{NAMES[-1]}-{s}-lowres.{kind}"))]
                   for kind in self.kinds}
        shapes = formats.get("jpg", [])
        files = [os.path.join(r, f) for r, _, fs in os.walk(sub) for f in fs]
        return {"key": look, "style": style, "palette": palette, "mode": mode, "shapes": shapes, "formats": formats,
                "ready": "1x1" in shapes and look not in self.urgent, "files": len(files),
                "bytes": sum(os.path.getsize(f) for f in files if os.path.exists(f))}

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
            "looks": [self.look_info(k) for k in self.looks if LOOK_RE.match(k)],
            "cleanable": self.cleanable(),
            "page_version": VERSION,
            "max_looks": MAX_LOOKS,
            "styles": [{"key": k, "label": v["label"], "blurb": themes.BLURBS.get(k, ""), "preview": f"{PREVIEWS}/{k}.jpg"}
                       for k, v in themes.STYLES.items()],
            "palettes": [{"key": k, "label": v["label"], "dark": hexes(v["dark"]), "light": hexes(v["light"])}
                         for k, v in themes.THEMES.items()],
            "modes": [[m, themes.MODE_LABELS[m]] for m in themes.MODES],
            "control": {"port": self.port, "preview": True},
        }
        write_atomic(os.path.join(self.folder, SITE_INFO), json.dumps(info).encode())

    def write_page(self) -> None:
        if self.folder:
            with open(os.path.join(PKG, "web", SITE_PAGE), "rb") as f:
                write_atomic(os.path.join(self.folder, SITE_PAGE), f.read())

    # --- the minute loop -----------------------------------------------------------------------
    def run(self) -> None:
        page_written_for = None
        while True:
            self.wake.wait(timeout=max(1.0, 60 - time.time() % 60))  # each minute, or when woken
            woken = self.wake.is_set()
            self.wake.clear()
            fresh = self.read_handoff()
            if not self.folder:
                continue
            try:
                if page_written_for != self.folder:
                    self.write_page()
                    self.write_site()
                    page_written_for = self.folder
                data = self.data()
                if woken and not fresh:
                    self.serve_urgent(data)
                    continue
                self.ticks += 1
                if self.looks and data.get("now"):
                    shapes = list(FORMATS) if self.ticks % SHAPES_EVERY == 1 else ["1x1"]
                    names = [n for n in NAMES if n not in SLOW_PAGES or self.ticks % SLOW_PAGES[n] == 1]
                    for look in list(self.looks):
                        self.publish_look(look, data, names, shapes, self.kinds)
                        self.write_site()
                    self.updated = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.serve_urgent(data)
                self.write_site()
                if time.monotonic() - self.previews_at > PREVIEW_EVERY and is_ready("live", data):
                    self.previews_at = time.monotonic()
                    self.write_previews(data)
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
                return self._json(200, {"ok": True, "version": VERSION, "looks": app.looks})
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
                if action == "generate":
                    result = {"look": app.generate(body.get("style", ""), body.get("palette", ""), body.get("mode", "dark"))}
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


def _exit_with_parent(parent: int) -> None:
    """Stop when the integration (Home Assistant) is gone, so a restart finds the port free."""
    while True:
        time.sleep(5)
        if os.getppid() != parent:
            log("parent process gone - stopping")
            os._exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handoff", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    app = WebApp(args.handoff, args.state)
    app.port = args.port
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_exit_with_parent, args=(os.getppid(),), daemon=True).start()
    log(f"web app {VERSION} on port {args.port}")
    app.read_handoff()
    app.wake.set()
    app.run()


if __name__ == "__main__":
    main()

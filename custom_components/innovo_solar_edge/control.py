"""A small local HTTP endpoint for the viewer page's Generate / Remove / Clean buttons.

The viewer page is served by another web server on the box (plain HTTP). Home
Assistant itself often runs on HTTPS with a self-signed certificate, which a browser
won't call from that page, so the integration listens on its own plain-HTTP port,
answers with CORS headers the page can read, and only accepts requests from the
local network. The actions are harmless (publish or delete screen images).
"""

from __future__ import annotations

import ipaddress
import json
import logging

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

CONTROL_PORT = 8765
_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Private-Network": "true",
    "Cache-Control": "no-store",
}


def _local(remote: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((remote or "").split("%")[0])
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


class ControlServer:
    def __init__(self, snapshot, port: int = CONTROL_PORT):
        self.snapshot = snapshot
        self.port = port
        self._runner: web.AppRunner | None = None

    async def async_start(self) -> bool:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_route("OPTIONS", "/{tail:.*}", self._options)
        app.router.add_get("/ping", self._ping)
        app.router.add_post("/command", self._command)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, host="0.0.0.0", port=self.port).start()
        except OSError as err:
            await runner.cleanup()
            _LOGGER.warning("Screens control port %s is not available (%s); the viewer page can't generate looks "
                            "- use the generate_screens action instead", self.port, err)
            return False
        self._runner = runner
        return True

    async def async_stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _options(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_HEADERS)

    async def _ping(self, request: web.Request) -> web.Response:
        if not _local(request.remote):
            return web.json_response({"error": "local network only"}, status=403, headers=_HEADERS)
        return web.json_response({"ok": True}, headers=_HEADERS)

    async def _command(self, request: web.Request) -> web.Response:
        if not _local(request.remote):
            return web.json_response({"error": "local network only"}, status=403, headers=_HEADERS)
        try:
            body = json.loads(await request.text() or "{}")
            result = await self.snapshot.async_command(str(body.get("action", "")), body)
            status = 400 if "error" in result else 200
        except Exception as err:  # shown on the page
            result, status = {"error": str(err)}, 400
        return web.json_response(result, status=status, headers=_HEADERS)

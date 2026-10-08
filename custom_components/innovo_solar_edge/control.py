"""Starts and watches the screens web app (webapp/service.py) as its own process.

The web app draws the generated looks, answers the viewer page's Generate / Remove /
Clean buttons and live previews on a plain-HTTP port (Home Assistant itself often runs
on HTTPS with a self-signed certificate, which a browser won't call from that page),
and writes the viewer page and screens.json. Running it outside Home Assistant keeps
its drawing off Home Assistant's threads, and lets it be updated without a Home
Assistant restart: when any of its files change on disk, it is restarted on its own.
It is also restarted if it stops, and stopped with the integration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

CONTROL_PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "webapp", "service.py")
WATCHED = [SERVICE, *(os.path.join(HERE, f) for f in ("screens.py", "scenes.py", "themes.py")),
           os.path.join(HERE, "web", "index.html")]
WATCH_EVERY = 20  # seconds between checks for updated web app files
RESTART_DELAY = 5


def handoff_path(hass: HomeAssistant) -> str:
    """Live screen data the integration hands to the web app every minute."""
    return hass.config.path(".storage", "innovo_solar_edge.screen-data.json")


def _mtimes() -> tuple:
    return tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in WATCHED)


class WebAppSupervisor:
    def __init__(self, hass: HomeAssistant, port: int = CONTROL_PORT):
        self.hass = hass
        self.port = port
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        self._task = self.hass.async_create_background_task(self._run(), "innovo_solar_edge web app")

    async def async_stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def _run(self) -> None:
        while not self._stopping:
            stamp = await self.hass.async_add_executor_job(_mtimes)
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", SERVICE,
                "--handoff", handoff_path(self.hass),
                "--state", self.hass.config.path(".storage", "innovo_solar_edge.webapp.json"),
                "--port", str(self.port),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            _LOGGER.debug("Screens web app started (pid %s)", self._proc.pid)
            reader = asyncio.ensure_future(self._relay(self._proc))
            reason = None
            while reason is None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._proc.wait()), WATCH_EVERY)
                    reason = f"stopped (exit {self._proc.returncode})"
                except asyncio.TimeoutError:
                    if await self.hass.async_add_executor_job(_mtimes) != stamp:
                        reason = "updated"
            await self._kill()
            await reader
            if self._stopping:
                return
            if reason == "updated":
                _LOGGER.info("Screens web app files changed - restarting it")
            else:
                _LOGGER.warning("Screens web app %s - restarting in %s s", reason, RESTART_DELAY)
                await asyncio.sleep(RESTART_DELAY)

    @staticmethod
    async def _relay(proc) -> None:
        """The web app's output goes to the Home Assistant log (problems as warnings)."""
        block: list[str] = []
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line[:4].isdigit() and block:  # a new timestamped message: flush the previous one
                _flush(block)
                block = []
            block.append(line)
        if block:
            _flush(block)


def _flush(block: list[str]) -> None:
    text = "\n".join(block)
    if "Traceback" in text or "failed" in text or "Error" in text:
        _LOGGER.warning("Screens web app: %s", text)
    else:
        _LOGGER.debug("Screens web app: %s", text)


async def async_command(hass: HomeAssistant, payload: dict, port: int = CONTROL_PORT) -> dict:
    """Ask the web app to generate / remove / clean (for the Home Assistant actions)."""
    import aiohttp
    from homeassistant.exceptions import HomeAssistantError

    try:
        async with async_get_clientsession(hass).post(
            f"http://127.0.0.1:{port}/command", json=payload, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            result = await resp.json(content_type=None)
    except Exception as err:  # noqa: BLE001
        raise HomeAssistantError(f"The screens web app isn't answering: {err}") from err
    if "error" in result:
        raise HomeAssistantError(result["error"])
    return result

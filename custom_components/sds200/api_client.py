"""Thin async client for the sds200_bridge add-on's REST/WS API."""

from __future__ import annotations

import aiohttp


class SDS200ApiError(Exception):
    pass


class SDS200Client:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int):
        self.session = session
        self._base = f"http://{host}:{port}"

    @property
    def ws_url(self) -> str:
        return f"{self._base}/ws".replace("http://", "ws://", 1)

    def audio_url(self, scanner_id: str) -> str:
        return f"{self._base}/scanners/{scanner_id}/audio/stream.mp3"

    async def list_scanners(self) -> list[dict]:
        return await self._get("/scanners")

    async def get_status(self, scanner_id: str) -> dict:
        return await self._get(f"/scanners/{scanner_id}/status")

    async def send_key(self, scanner_id: str, code: str, mode: str = "P") -> dict:
        return await self._post(f"/scanners/{scanner_id}/key", {"code": code, "mode": mode})

    async def set_volume(self, scanner_id: str, level: int) -> dict:
        return await self._post(f"/scanners/{scanner_id}/volume", {"level": level})

    async def set_squelch(self, scanner_id: str, level: int) -> dict:
        return await self._post(f"/scanners/{scanner_id}/squelch", {"level": level})

    async def reboot(self, scanner_id: str) -> dict:
        """Power-cycle the scanner via its configured poe_reset_* (add-on option).

        Longer timeout than other POSTs: the add-on's own MikroTik REST call
        allows up to 15s for the router to respond (mikrotik.py), and this
        client-side timeout must comfortably exceed that or every reboot
        request times out here before the add-on's own attempt could ever
        finish either way -- regardless of whether the power-cycle itself
        would have succeeded.
        """
        return await self._post(f"/scanners/{scanner_id}/reboot", {}, timeout=20)

    async def hold(
        self, scanner_id: str, tkw: str = "", xxx1: str = "", xxx2: str = "",
        hold: bool | None = None,
    ) -> dict:
        """Hold on the scanner's current channel, or on a named target.

        With no target the add-on resolves the current channel's list index
        itself -- HLD needs one, and the empty "HLD,,," this used to send is
        rejected outright by the scanner (see docs/protocol-notes.md). With
        no `hold` value the command toggles, which is what the card's button
        wants; True/False set a state instead.
        """
        payload: dict = {"tkw": tkw, "xxx1": xxx1, "xxx2": xxx2}
        if hold is not None:
            payload["hold"] = hold
        return await self._post(f"/scanners/{scanner_id}/hold", payload)

    async def avoid(
        self, scanner_id: str, tkw: str = "", xxx1: str = "", xxx2: str = "", status: int = 1
    ) -> dict:
        return await self._post(
            f"/scanners/{scanner_id}/avoid",
            {"tkw": tkw, "xxx1": xxx1, "xxx2": xxx2, "status": status},
        )

    async def _get(self, path: str):
        try:
            async with self.session.get(
                self._base + path, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise SDS200ApiError(str(exc)) from exc

    async def _post(self, path: str, payload: dict, *, timeout: float = 5):
        try:
            async with self.session.post(
                self._base + path, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status >= 400:
                    # The add-on puts the *useful* part of a failure in the
                    # body (e.g. which MikroTik URL was tried and that
                    # www-ssl is likely disabled). raise_for_status() alone
                    # discards it, leaving the user with a bare
                    # "502, message='Bad Gateway'" that says nothing.
                    raise SDS200ApiError((await resp.text()).strip() or f"HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise SDS200ApiError(str(exc)) from exc

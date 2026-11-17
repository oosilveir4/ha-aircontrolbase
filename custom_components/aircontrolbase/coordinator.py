"""DataUpdateCoordinator do AirControlBase."""

from __future__ import annotations

import logging
import math
from inspect import signature
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AirControlBaseClient, AirControlBaseError, AuthError
from .const import DEFAULT_POLL_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)
_SUPPORTS_CONFIG_ENTRY = (
    "config_entry" in signature(DataUpdateCoordinator.__init__).parameters
)


def _number(value: Any, default: float | None = None) -> float | None:
    """Valores ausentes/inválidos de um aparelho não derrubam a conta inteira."""
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    return result if math.isfinite(result) else default


class AirControlBaseCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Faz polling do getDetails e expõe estrutura parseada."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AirControlBaseClient,
        entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            **({"config_entry": entry} if _SUPPORTS_CONFIG_ENTRY else {}),
            name=DOMAIN,
            update_interval=DEFAULT_POLL_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            raw = await self.client.get_details()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AirControlBaseError as err:
            raise UpdateFailed(f"API: {err}") from err
        return parse_details(raw)


def parse_details(raw: dict[str, Any]) -> dict[str, Any]:
    """Converte o getDetails da nuvem no formato interno (devices/areas/summary)."""
    result = raw.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("areas"), list):
        raise UpdateFailed("getDetails não retornou uma lista de áreas válida")
    areas_raw = result["areas"]
    device_case = result.get("deviceCase") or {}
    if not isinstance(device_case, dict):
        raise UpdateFailed("getDetails retornou um resumo inválido")

    devices: dict[str, dict[str, Any]] = {}
    areas: list[dict[str, Any]] = []

    for area in areas_raw:
        if not isinstance(area, dict) or (
            area.get("data") is not None and not isinstance(area["data"], list)
        ):
            raise UpdateFailed("getDetails retornou uma área inválida")
        area_name = area.get("name") or ""
        area_id = str(area.get("id") or area.get("aid") or "")
        device_ids: list[str] = []
        for d in area.get("data") or []:
            if not isinstance(d, dict):
                raise UpdateFailed("getDetails retornou um aparelho inválido")
            dev_id = str(d.get("id") or "")
            if not dev_id:
                continue
            devices[dev_id] = {
                "id": dev_id,
                "name": d.get("name") or f"Device {dev_id}",
                "area": area_name,
                "area_id": area_id,
                "power": d.get("power") or "n",
                "mode": d.get("mode") or "cool",
                "wind": d.get("wind") or "auto",
                "setTemp": _number(d.get("setTemp")),
                "factTemp": _number(d.get("factTemp")),
                "lock": d.get("lock") or "",
                "unlock": d.get("unlock")
                if d.get("unlock") is not None
                else "mode,cool,heat,wind,remote",
                "swing": d.get("swing") or "n",
                "modeLockValue": d.get("modeLockValue") or "",
                "coolLockValue": d.get("coolLockValue") or "",
                "heatLockValue": d.get("heatLockValue") or "",
                "windLockValue": d.get("windLockValue") or "",
                "raw": d,
            }
            device_ids.append(dev_id)
        areas.append({"id": area_id, "name": area_name, "device_ids": device_ids})

    return {
        "devices": devices,
        "areas": areas,
        "summary": {
            key: int(_number(device_case.get(key), 0))
            for key in ("cool", "heat", "fan", "stop", "lock", "error")
        },
    }

"""Modo local: substitui a nuvem AirControlBase por comunicação direta com o gateway.

- Estado rápido e comandos vêm do `LocalGatewayServer` (TCP, push a cada ~5s).
- Travas e código de erro vêm da leitura HTTP (`LocalGatewayClient`), poll lento.
- O `LocalController` imita `AirControlBaseClient.control_device`, então as
  entidades (climate/switch) não mudam.
- O `LocalCoordinator` produz o MESMO formato de dados do coordinator da nuvem,
  chaveado pelo `device_id` da nuvem, para preservar os `unique_id` das entidades
  (dashboards e automações seguem funcionando).

Escrita de trava não é suportada localmente (o usuário não usa; o encoding foi
capturado mas não implementado — ver protocol/memória).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import protocol as p
from .const import DEFAULT_POLL_INTERVAL, DOMAIN
from .local import LocalGatewayClient, LocalGatewayError, LocalUnitState
from .server import LocalGatewayServer

_LOGGER = logging.getLogger(__name__)

# Tradução entre os nomes locais (protocol) e os campos ACB que as entidades leem.
LOCAL_MODE_TO_ACB = {"cool": "cool", "heat": "heat", "dry": "dry",
                     "fan_only": "fan", "auto": "auto"}
ACB_MODE_TO_LOCAL = {"cool": "cool", "heat": "heat", "dry": "dry",
                     "fan": "fan_only", "auto": "auto"}
LOCAL_FAN_TO_ACB = {"auto": "auto", "low": "low", "medium": "mid", "high": "high"}
ACB_FAN_TO_LOCAL = {"auto": "auto", "low": "low", "mid": "medium", "high": "high",
                    "off": "auto"}

LOCK_TOKENS = ("mode", "cool", "heat", "wind", "remote")
ALL_UNLOCKED = ",".join(LOCK_TOKENS)

# Intervalo mínimo entre comandos (uma rajada quebra a CCM21 — ver v0.4.2).
COMMAND_INTERVAL = 0.4


@dataclass(frozen=True)
class DeviceEntry:
    """Metadados de um aparelho (fixos; estado vem do gateway)."""

    addr: int
    device_id: str
    name: str
    area: str


# Mapa do parque (addr, device_id, nome, área) — confirmado por probe 2026-11-17.
SITE_DEVICES: tuple[DeviceEntry, ...] = (
    DeviceEntry(0, "40783270", "Teto Porta", "Ar Teto"),
    DeviceEntry(1, "40783271", "Teto Catracas", "Ar Teto"),
    DeviceEntry(2, "40783272", "Teto meio", "Ar Teto"),
    DeviceEntry(10, "40783273", "Ent.Ponte", "Ar Parede"),
    DeviceEntry(11, "40783274", "Ent.Ponte2", "Ar Parede"),
    DeviceEntry(12, "40783275", "Parede Caixas", "Ar Parede"),
    DeviceEntry(13, "40783276", "Parede meio2", "Ar Parede"),
    DeviceEntry(14, "40783277", "parede catraca", "Ar Parede"),
    DeviceEntry(20, "53033730", "Sala Suporte", "Ar Interno"),
    DeviceEntry(21, "53033731", "Corredor Suporte", "Ar Interno"),
    DeviceEntry(22, "53033732", "Loja Miudinhos", "Ar Interno"),
    DeviceEntry(23, "53033735", "Ar meio Loja miudinhos", "Ar Interno"),
    DeviceEntry(24, "53033736", "Ar esquerda loja miudinhos", "Ar Interno"),
    DeviceEntry(30, "53033696", "Ar direita loja miudinhos", "Ar Interno"),
    DeviceEntry(31, "53033697", "Ar entrada loja cascata", "Ar Interno"),
    DeviceEntry(32, "53033698", "Ar caixa loja cascata", "Ar Interno"),
    DeviceEntry(40, "104686792", "Ar Caixa Fotos", "Ar Fotos"),
    DeviceEntry(41, "104686793", "Ar Meio salão", "Ar Fotos"),
    DeviceEntry(42, "104686794", "Ar Meio salão 2", "Ar Fotos"),
    DeviceEntry(43, "104686795", "Ar descida corredor", "Ar Fotos"),
)


def _unlock_csv(lock: LocalUnitState | None) -> str:
    if lock is None:
        return ALL_UNLOCKED
    locked = set()
    if lock.lock_mode:
        locked.add("mode")
    if lock.lock_cool_temp:
        locked.add("cool")
    if lock.lock_heat_temp:
        locked.add("heat")
    if lock.lock_wind:
        locked.add("wind")
    if lock.lock_remote:
        locked.add("remote")
    return ",".join(t for t in LOCK_TOKENS if t not in locked)


def build_data(
    devices_meta: tuple[DeviceEntry, ...],
    units: dict[int, p.UnitStatus],
    locks: dict[int, LocalUnitState],
) -> dict[str, Any]:
    """Monta o dict no formato do coordinator da nuvem."""
    devices: dict[str, dict[str, Any]] = {}
    areas_map: dict[str, list[str]] = {}
    summary = {k: 0 for k in ("cool", "heat", "fan", "stop", "lock", "error")}

    for meta in devices_meta:
        areas_map.setdefault(meta.area, [])
        unit = units.get(meta.addr)
        if unit is None:
            continue  # ainda não reportado pelo gateway
        lock = locks.get(meta.addr)
        power = "n" if unit.mode == "off" else "y"
        mode_acb = LOCAL_MODE_TO_ACB.get(
            unit.mode if unit.mode != "off" else "cool", "cool"
        )
        wind_acb = LOCAL_FAN_TO_ACB.get(unit.fan or "auto", "auto")
        err = lock.error if lock else None
        devices[meta.device_id] = {
            "id": meta.device_id,
            "name": meta.name,
            "area": meta.area,
            "area_id": meta.area,
            "power": power,
            "mode": mode_acb,
            "wind": wind_acb,
            "setTemp": float(unit.set_temp),
            "factTemp": float(unit.room_temp),
            "unlock": _unlock_csv(lock),
            "swing": "y" if unit.raw[4] & 0x02 else "n",
            "lock": "",
            "modeLockValue": "",
            "coolLockValue": str(lock.lock_cool_temp) if lock and lock.lock_cool_temp else "",
            "heatLockValue": str(lock.lock_heat_temp) if lock and lock.lock_heat_temp else "",
            "windLockValue": "",
            "raw": {"error": err if err is not None else "n", "addr": meta.addr},
        }
        areas_map[meta.area].append(meta.device_id)
        # resumo
        if power == "n":
            summary["stop"] += 1
        elif unit.mode == "cool":
            summary["cool"] += 1
        elif unit.mode == "heat":
            summary["heat"] += 1
        elif unit.mode == "fan_only":
            summary["fan"] += 1
        if lock and lock.locked:
            summary["lock"] += 1
        if err is not None:
            summary["error"] += 1

    areas = [{"id": name, "name": name, "device_ids": ids}
             for name, ids in areas_map.items()]
    return {"devices": devices, "areas": areas, "summary": summary}


class LocalController:
    """Imita AirControlBaseClient.control_device, mandando comando local."""

    def __init__(self, server: LocalGatewayServer,
                 devices_meta: tuple[DeviceEntry, ...]) -> None:
        self._server = server
        self._addr = {m.device_id: m.addr for m in devices_meta}
        self._last = 0.0

    async def control_device(
        self,
        *,
        device_id: int | str,
        current: dict[str, Any],
        changes: dict[str, Any],
        lock_token: str | None = None,
        locked: bool = False,
    ) -> dict[str, Any]:
        if lock_token is not None:
            raise HomeAssistantError(
                "Trava não é suportada no modo local do AirControlBase"
            )
        addr = self._addr.get(str(device_id))
        if addr is None:
            raise HomeAssistantError(f"device_id desconhecido: {device_id}")
        state = {**current, **changes}
        power = state.get("power", "y")
        if power == "n":
            mode = "off"
        else:
            mode = ACB_MODE_TO_LOCAL.get(state.get("mode", "cool"), "cool")
        fan = ACB_FAN_TO_LOCAL.get(state.get("wind", "auto"), "auto")
        set_temp = int(state.get("setTemp") if state.get("setTemp") is not None else 24)

        wait = COMMAND_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        await self._server.send_command(
            [addr], mode=mode, fan=fan, set_temp=set_temp
        )
        self._last = time.monotonic()
        return {"status": "ok"}

    async def close(self) -> None:  # paridade com o client da nuvem
        return None


class LocalCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator do modo local: servidor TCP + poll HTTP para travas/erros."""

    def __init__(
        self,
        hass: HomeAssistant,
        gateway_host: str,
        server_port: int = 21880,
        devices_meta: tuple[DeviceEntry, ...] = SITE_DEVICES,
        entry: Any = None,
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_local",
            update_interval=DEFAULT_POLL_INTERVAL,  # poll HTTP (travas/erros)
            **({"config_entry": entry} if entry is not None else {}),
        )
        self._meta = devices_meta
        self._server = LocalGatewayServer(
            port=server_port, on_update=self._on_status
        )
        self._http = LocalGatewayClient(gateway_host)
        self._locks: dict[int, LocalUnitState] = {}
        self.controller = LocalController(self._server, devices_meta)

    @property
    def server(self) -> LocalGatewayServer:
        return self._server

    async def async_start(self) -> None:
        await self._server.start()

    async def async_stop(self) -> None:
        await self._server.stop()
        await self._http.close()

    def _on_status(self, units: dict[int, p.UnitStatus]) -> None:
        # push: estado rápido do gateway
        self.async_set_updated_data(build_data(self._meta, units, self._locks))

    async def _async_update_data(self) -> dict[str, Any]:
        # poll lento: atualiza travas/erros via HTTP (não derruba se falhar)
        try:
            self._locks = await self._http.get_units()
        except LocalGatewayError as err:
            _LOGGER.debug("Leitura HTTP de travas falhou: %s", err)
        return build_data(self._meta, self._server.units, self._locks)

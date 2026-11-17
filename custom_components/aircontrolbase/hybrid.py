"""Modo híbrido: estado local rápido + comando/metadados pela nuvem.

O HA atua como proxy transparente entre o gateway e a nuvem AirControlBase:
    gateway -> HA:21880 -> nuvem 52.57.81.51:21880
Encaminha tudo nos dois sentidos (a nuvem segue controlando e provendo travas,
grupos e nomes) e, de passagem, decodifica o push de status do gateway (0x9012),
expondo o estado ao vivo (~5-10s) sem esperar o poll lento da nuvem.

- Estado rápido (power/mode/wind/setTemp/factTemp): do tap local.
- Metadados (nomes, áreas, travas, erro): do getDetails da nuvem (poll lento).
- Comandos: pelo AirControlBaseClient da nuvem (inalterado).

As entidades não mudam: o coordinator entrega o mesmo formato de dados.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import protocol as p
from .api import AirControlBaseClient, AirControlBaseError, AuthError
from .const import DOMAIN
from .coordinator import parse_details
from .localmode import LOCAL_FAN_TO_ACB, LOCAL_MODE_TO_ACB, SITE_DEVICES

_LOGGER = logging.getLogger(__name__)

CLOUD_HOST = "52.57.81.51"
CLOUD_PORT = 21880
METADATA_INTERVAL = 60.0  # s entre refreshes de metadados da nuvem
FAST_INTERVAL = 5.0       # s do poll de estado local


class HybridProxy:
    """Proxy transparente gateway<->nuvem que decodifica o status de passagem."""

    def __init__(self, listen_port: int = 21880,
                 upstream: tuple[str, int] = (CLOUD_HOST, CLOUD_PORT)) -> None:
        self._port = listen_port
        self._upstream = upstream
        self._server: asyncio.AbstractServer | None = None
        self.units: dict[int, p.UnitStatus] = {}
        self.token: bytes | None = None
        self.last_status_ts: float = 0.0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_gateway, "0.0.0.0", self._port
        )
        _LOGGER.info("Proxy híbrido ouvindo em 0.0.0.0:%s -> %s:%s",
                     self._port, *self._upstream)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_gateway(self, gw_reader: asyncio.StreamReader,
                              gw_writer: asyncio.StreamWriter) -> None:
        peer = gw_writer.get_extra_info("peername")
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(*self._upstream), timeout=10
            )
        except (TimeoutError, OSError) as err:
            _LOGGER.warning("Híbrido: falha ao conectar na nuvem: %r", err)
            gw_writer.close()
            return
        _LOGGER.info("Híbrido: gateway %s conectado, repassando para a nuvem", peer)
        await asyncio.gather(
            self._pump(gw_reader, up_writer, tap=True),
            self._pump(up_reader, gw_writer, tap=False),
        )

    async def _pump(self, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter, *, tap: bool) -> None:
        """Encaminha bytes crus; se tap, decodifica status de passagem."""
        buf = bytearray()
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
                if tap:
                    buf += data
                    self._tap(buf)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _tap(self, buf: bytearray) -> None:
        try:
            frames = p.split_frames(buf)
        except p.ProtocolError:
            buf.clear()
            return
        for fr in frames:
            if fr.cmd == p.CMD_REPORT and len(fr.data) >= 32:
                self.token = fr.data[28:32]
            if fr.cmd == p.CMD_REPORT and fr.subtype == p.SUB_STATUS:
                for u in p.parse_status(fr):
                    self.units[u.addr] = u
                self.last_status_ts = time.time()


class HybridCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Mescla metadados da nuvem (lento) com estado local do proxy (rápido)."""

    def __init__(self, hass: HomeAssistant, client: AirControlBaseClient,
                 proxy: HybridProxy, entry: ConfigEntry | None = None) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_hybrid",
            update_interval=timedelta(seconds=FAST_INTERVAL),
            **({"config_entry": entry} if entry is not None else {}),
        )
        self.client = client
        self._proxy = proxy
        self._addr_of = {m.device_id: m.addr for m in SITE_DEVICES}
        self._meta: dict[str, Any] | None = None
        self._meta_ts = 0.0

    async def _refresh_metadata(self) -> None:
        try:
            raw = await self.client.get_details()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AirControlBaseError as err:
            if self._meta is None:
                raise UpdateFailed(f"API: {err}") from err
            _LOGGER.debug("Híbrido: getDetails falhou, mantendo metadados: %s", err)
            return
        meta = parse_details(raw)
        # Chave de grupo estável pelo NOME da área: o getDetails da nuvem passou a
        # devolver id numérico, o que criaria entidades de grupo novas e deixaria as
        # originais (chaveadas por nome) indisponíveis. Ancorar no nome mantém
        # compatibilidade com os grupos já existentes.
        for area in meta["areas"]:
            area["id"] = area.get("name") or area.get("id") or ""
        for dev in meta["devices"].values():
            dev["area_id"] = dev.get("area", "")
        self._meta = meta
        self._meta_ts = time.time()

    async def _async_update_data(self) -> dict[str, Any]:
        if self._meta is None or (time.time() - self._meta_ts) >= METADATA_INTERVAL:
            await self._refresh_metadata()
        assert self._meta is not None
        units = self._proxy.units
        # overlay do estado local sobre os metadados da nuvem
        devices: dict[str, dict[str, Any]] = {}
        for dev_id, dev in self._meta["devices"].items():
            merged = dict(dev)
            addr = self._addr_of.get(dev_id)
            unit = units.get(addr) if addr is not None else None
            if unit is not None:
                merged["power"] = "n" if unit.mode == "off" else "y"
                merged["mode"] = LOCAL_MODE_TO_ACB.get(
                    unit.mode if unit.mode != "off" else dev.get("mode", "cool"),
                    dev.get("mode", "cool"),
                )
                merged["wind"] = LOCAL_FAN_TO_ACB.get(unit.fan or "auto", "auto")
                merged["setTemp"] = float(unit.set_temp)
                merged["factTemp"] = float(unit.room_temp)
            devices[dev_id] = merged
        return {
            "devices": devices,
            "areas": self._meta["areas"],
            "summary": self._meta["summary"],
        }

"""Leitura local do gateway CCM21 ("Cloud Gateway" CARRIER_DZ).

A interface web do gateway expõe, sem autenticação:
    POST http://<gw>/get_mbdata_all.jsn   _web_cmd=get_mbdata_all&_ajax=1
que devolve uma lista [{"addr": n, "Data": "<14 hex>" | "-"}] com 7 bytes de
estado por endereço (0..63). A decodificação abaixo replica get_status_from()
do JS da própria página (aba DataView).

Só leitura: a interface web não tem endpoint de comando.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

MODES = {0: "cool", 1: "heat", 2: "dry", 3: "fan", 4: "off", 5: "auto"}


class LocalGatewayError(Exception):
    """Falha ao ler o gateway local."""


@dataclass(frozen=True)
class LocalUnitState:
    addr: int
    raw: str
    power: bool
    mode: str  # cool/heat/dry/fan/auto (modo configurado mesmo desligado: ver mode_raw)
    mode_raw: int  # bits 2-4 do byte 3, antes de forçar "off"
    fan_raw: int  # bits 5-7 do byte 3 (mapeamento p/ nome ainda a confirmar)
    set_temp: int
    room_temp: int
    error_code: int  # 0 = ok; 1..16 = E0..EF, >16 = P0..
    fahrenheit: bool
    swing: bool
    lock_remote: bool
    lock_mode: bool
    lock_wind: bool
    lock_cool_temp: int  # 0 = sem trava
    lock_heat_temp: int  # 0 = sem trava

    @property
    def error(self) -> str | None:
        if self.error_code == 0:
            return None
        if self.error_code <= 16:
            return f"E{self.error_code - 1:X}"
        return f"P{self.error_code - 17:X}"

    @property
    def locked(self) -> bool:
        return (
            self.lock_remote
            or self.lock_mode
            or self.lock_wind
            or self.lock_cool_temp > 0
            or self.lock_heat_temp > 0
        )


def decode_unit(addr: int, data: str) -> LocalUnitState | None:
    """Decodifica os 7 bytes de um endereço. "-" = endereço sem aparelho."""
    if not data or data == "-":
        return None
    try:
        b = bytes.fromhex(data)
    except ValueError as err:
        raise LocalGatewayError(f"addr {addr}: hex inválido {data!r}") from err
    if len(b) < 7:
        raise LocalGatewayError(f"addr {addr}: esperado 7 bytes, veio {len(b)}")

    lock_cool = (b[0] >> 3) & 31 if (b[5] >> 3) & 1 else 0
    lock_heat = b[1] & 31 if (b[5] >> 4) & 1 else 0
    mode_raw = (b[3] >> 2) & 7
    power = bool(b[3] & 1)
    return LocalUnitState(
        addr=addr,
        raw=data.lower(),
        power=power,
        mode=MODES.get(mode_raw, f"mode{mode_raw}") if power else "off",
        mode_raw=mode_raw,
        fan_raw=(b[3] >> 5) & 7,
        set_temp=(b[4] >> 3) & 31,
        room_temp=b[6] if b[6] < 128 else b[6] - 256,
        error_code=b[2],
        fahrenheit=bool(b[0] & 1),
        swing=bool((b[4] >> 1) & 1),
        lock_remote=bool((b[5] >> 6) & 1),
        lock_mode=bool((b[3] >> 1) & 1),
        lock_wind=bool((b[5] >> 5) & 1),
        lock_cool_temp=lock_cool,
        lock_heat_temp=lock_heat,
    )


def decode_all(payload: Any) -> dict[int, LocalUnitState]:
    if not isinstance(payload, list):
        raise LocalGatewayError("get_mbdata_all não retornou uma lista")
    units: dict[int, LocalUnitState] = {}
    for item in payload:
        if not isinstance(item, dict) or "addr" not in item:
            raise LocalGatewayError(f"item inválido: {item!r}")
        unit = decode_unit(int(item["addr"]), str(item.get("Data", "-")))
        if unit is not None:
            units[unit.addr] = unit
    return units


class LocalGatewayClient:
    """Cliente HTTP do gateway. O servidor web dele é lento: timeout folgado."""

    def __init__(self, host: str, session: aiohttp.ClientSession | None = None,
                 timeout: float = 20) -> None:
        self.host = host
        self._session = session
        self._own_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _post(self, cmd: str) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = f"http://{self.host}/{cmd}.jsn"
        try:
            async with self._session.post(
                url, data={"_web_cmd": cmd, "_ajax": "1"}, timeout=self._timeout
            ) as resp:
                resp.raise_for_status()
                # o gateway responde JSON com content-type text/html
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise LocalGatewayError(f"{cmd}: {err!r}") from err

    async def get_units(self) -> dict[int, LocalUnitState]:
        return decode_all(await self._post("get_mbdata_all"))

    async def get_settings(self) -> dict[str, Any]:
        return await self._post("get_settings")

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

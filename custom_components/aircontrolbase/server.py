"""Servidor local que substitui a nuvem AirControlBase.

O gateway CCM21 é cliente TCP: conecta no "Server IP/Port" configurado nele.
Apontando-o para o HA, esta classe:
  - faz o handshake (envia desafio, confere a resposta base64 do gateway);
  - responde os keepalives com o horário atual, mantendo o relógio do gateway
    em dia (o gateway rejeita comandos com timestamp "velho");
  - guarda o último status de cada aparelho;
  - envia comandos (modo, ventilação, setpoint) para um ou vários endereços.

Sem nuvem no meio: nada é repassado para fora.

Projetado para rodar dentro do Home Assistant (asyncio), mas a lógica de
protocolo fica em `_Connection`, testável sem socket real.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

# O gateway manda keepalive a cada ~1 min; se ficar muito sem falar, derrubamos.
IDLE_TIMEOUT = 180.0
# Bytes [20:28] do quadro (constantes observadas); aprendemos do gateway se vier outro.
DEFAULT_ROUTE = bytes.fromhex("7f0000010100007f")

UpdateCallback = Callable[[dict[int, p.UnitStatus]], None]


class _Connection:
    """Estado de uma conexão do gateway. Transporte injetado (send)."""

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        on_update: UpdateCallback | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._send = send
        self._on_update = on_update
        self._now = now
        self.serial: bytes | None = None
        self.route: bytes = DEFAULT_ROUTE
        self.token: bytes | None = None  # [28:32] do status; eco obrigatório no comando
        self.units: dict[int, p.UnitStatus] = {}
        self._seq = 0x4000
        self._authed = False
        self._challenge = p.make_challenge()

    async def start(self) -> None:
        """Primeiro passo: o servidor manda o desafio."""
        await self._send(self._challenge)

    async def feed(self, buffer: bytearray) -> None:
        """Processa tudo que houver de completo no buffer."""
        if not self._authed:
            if len(buffer) < 16:
                return
            reply = bytes(buffer[:16])
            del buffer[:16]
            if not p.check_challenge_reply(self._challenge, reply):
                raise p.ProtocolError("resposta de handshake inválida")
            self._authed = True
        for frame in p.split_frames(buffer):
            await self._handle(frame)

    async def _handle(self, frame: p.Frame) -> None:
        if frame.serial:
            self.serial = frame.serial
        if len(frame.data) >= 28:
            route = frame.data[20:28]
            if route != bytes(8):
                self.route = route
        if frame.cmd == p.CMD_REPORT and len(frame.data) >= 32:
            # [28:32] é o token da sessão; a nuvem ecoa isso no comando.
            self.token = frame.data[28:32]
        if frame.cmd == p.CMD_KEEPALIVE:
            await self._send(p.keepalive_reply(self._now()))
        elif frame.cmd == p.CMD_REPORT and frame.subtype == p.SUB_STATUS:
            for u in p.parse_status(frame):
                self.units[u.addr] = u
            if self._on_update:
                self._on_update(dict(self.units))
        elif frame.cmd == p.CMD_REPORT and frame.subtype == p.SUB_ACK:
            _LOGGER.info("Ack do gateway: %s", frame.data.hex())
        # 0x9004 (evento) não exige resposta.

    async def send_command(
        self, addrs: list[int], *, mode: str, fan: str, set_temp: int
    ) -> None:
        if not self._authed or self.serial is None:
            raise p.ProtocolError("gateway ainda não autenticado")
        self._seq = (self._seq + 1) & 0xFFFF
        now = self._now()
        # O gateway rejeita comandos cujo timestamp está à frente do seu relógio.
        # Um keepalive-reply com o horário do comando sincroniza o relógio antes.
        await self._send(p.keepalive_reply(now))
        pkt = p.build_control(
            serial=self.serial,
            route=self.route,
            seq=self._seq,
            token=self.token,
            addrs=addrs,
            mode=mode,
            fan=fan,
            set_temp=set_temp,
            now=now,
        )
        _LOGGER.info(
            "Comando -> addrs=%s mode=%s fan=%s set=%d token=%s (%s)",
            addrs, mode, fan, set_temp,
            self.token.hex() if self.token else None, pkt.hex(),
        )
        await self._send(pkt)


class LocalGatewayServer:
    """Escuta a conexão do gateway e expõe estado + comando para a integração."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 21880,
        on_update: UpdateCallback | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_update = on_update
        self._server: asyncio.AbstractServer | None = None
        self._conn: _Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def units(self) -> dict[int, p.UnitStatus]:
        return dict(self._conn.units) if self._conn else {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port
        )
        _LOGGER.info("Servidor local AirControlBase ouvindo em %s:%s",
                     self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._conn = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.info("Gateway conectou de %s", peer)

        async def send(data: bytes) -> None:
            writer.write(data)
            await writer.drain()

        conn = _Connection(send, self._on_update)
        self._conn = conn
        buffer = bytearray()
        try:
            await conn.start()
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), IDLE_TIMEOUT)
                if not chunk:
                    break
                buffer += chunk
                await conn.feed(buffer)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError,
                p.ProtocolError, OSError) as err:
            _LOGGER.warning("Conexão do gateway encerrada: %r", err)
        finally:
            if self._conn is conn:
                self._conn = None
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def send_command(
        self, addrs: list[int], *, mode: str, fan: str, set_temp: int
    ) -> None:
        async with self._lock:
            if self._conn is None:
                raise p.ProtocolError("gateway não está conectado")
            await self._conn.send_command(
                addrs, mode=mode, fan=fan, set_temp=set_temp
            )

"""Protocolo TCP entre o gateway CCM21 ("Cloud Gateway" CARRIER_DZ) e o servidor.

Decodificado por captura (proxy) do tráfego gateway <-> nuvem AirControlBase.
O gateway é o cliente: conecta no "Server IP/Port" configurado na web dele.

Handshake:
    servidor -> gw : 0a 00 ff ff + 8 chars ASCII (desafio)
    gw -> servidor : 10 00 ff ff + base64(desafio)            (16 bytes)
Depois, todos os quadros: u16 LE tamanho total + u16 LE comando + corpo.

    0x9000 gw->srv  36B  keepalive               -> responder 0x9001 (20B)
    0x9012 gw->srv  760B status (subtipo 2)      20 registros de 36B a partir do offset 40
    0x9012 gw->srv  40B  ack de comando (subtipo 4)
    0x9004 gw->srv  28B  evento (contador em [4]); a nuvem não responde
    0x9010 srv->gw  72B  comando (subtipo 3)

Comando (72B):
    [0:2] tamanho  [2:4] 0x9010  [4:8] 0  [8:12] unix time  [12:20] SN do gateway
    [20:28] ecoado do gateway (7f000001 0100007f)  [28:32] 00 9e + seq u16
    [32:36] 00 03 00 00  [36:40] checksum? (o gateway não valida)
    [40:56] bitmap de endereços (addr n = byte n//8, bit n%8)
    [56] 0xc3  [57] modo  [58] ventilação  [59] setpoint °C  [60:72] zeros
"""

from __future__ import annotations

import base64
import os
import struct
import time
from dataclasses import dataclass

CMD_KEEPALIVE = 0x9000
CMD_KEEPALIVE_REPLY = 0x9001
CMD_EVENT = 0x9004
CMD_CONTROL = 0x9010
CMD_REPORT = 0x9012

SUB_STATUS = 2
SUB_CONTROL = 3
SUB_ACK = 4

HEADER_LEN = 40
RECORD_LEN = 36
MAX_FRAME = 4096

# Checksum não-zero (capturado de um comando real); o gateway não valida conteúdo.
CONTROL_CHECKSUM_PLACEHOLDER = bytes.fromhex("3858d942")

# byte de modo: bit 7 = ligado, bits baixos = modo
MODE_OFF = 0x00
MODE_BITS = {"fan_only": 0x01, "dry": 0x02, "heat": 0x04, "cool": 0x08, "auto": 0x10}
MODE_ON = 0x80

FAN_BITS = {"auto": 0x80, "low": 0x04, "medium": 0x02, "high": 0x01}

# Gateway reporta 0x00 de ventilação em alguns modos (ex.: aquecimento pré-aquecendo).
FAN_FROM_BYTE = {v: k for k, v in FAN_BITS.items()}


class ProtocolError(Exception):
    """Quadro malformado."""


def make_challenge() -> bytes:
    """Desafio do handshake: 8 chars hex ASCII (igual à nuvem)."""
    return b"\x0a\x00\xff\xff" + os.urandom(4).hex().encode()


def check_challenge_reply(challenge: bytes, reply: bytes) -> bool:
    return reply[:4] == b"\x10\x00\xff\xff" and reply[4:16] == base64.b64encode(
        challenge[4:12]
    )


def decode_mode(byte: int) -> str:
    if not byte & MODE_ON:
        return "off"
    for name, bit in MODE_BITS.items():
        if byte & bit:
            return name
    return "off"


def decode_fan(byte: int) -> str | None:
    return FAN_FROM_BYTE.get(byte)


def decode_temp(raw: int) -> float:
    """Temperatura ambiente: (raw - 40) / 2."""
    return (raw - 40) / 2


@dataclass(frozen=True)
class UnitStatus:
    addr: int
    mode: str  # off/cool/heat/dry/fan_only/auto (auto vem resolvido pelo gateway)
    mode_byte: int
    fan: str | None
    fan_byte: int
    set_temp: int
    room_temp: float
    raw: bytes

    @property
    def power(self) -> bool:
        return self.mode != "off"


@dataclass(frozen=True)
class Frame:
    cmd: int
    data: bytes

    @property
    def subtype(self) -> int | None:
        return self.data[33] if len(self.data) >= 36 else None

    @property
    def serial(self) -> bytes | None:
        return self.data[12:20] if len(self.data) >= 20 else None


def split_frames(buffer: bytearray) -> list[Frame]:
    """Consome quadros completos do buffer (in-place) e devolve-os."""
    frames: list[Frame] = []
    while len(buffer) >= 4:
        size, cmd = struct.unpack_from("<HH", buffer)
        if size < 4 or size > MAX_FRAME:
            raise ProtocolError(f"tamanho de quadro inválido: {size}")
        if len(buffer) < size:
            break
        frames.append(Frame(cmd, bytes(buffer[:size])))
        del buffer[:size]
    return frames


def parse_status(frame: Frame) -> list[UnitStatus]:
    data = frame.data
    if frame.cmd != CMD_REPORT or frame.subtype != SUB_STATUS:
        raise ProtocolError("não é um quadro de status")
    units = []
    for off in range(HEADER_LEN, len(data) - RECORD_LEN + 1, RECORD_LEN):
        rec = data[off : off + RECORD_LEN]
        units.append(
            UnitStatus(
                addr=rec[0],
                mode=decode_mode(rec[4]),
                mode_byte=rec[4],
                fan=decode_fan(rec[5]),
                fan_byte=rec[5],
                set_temp=rec[6],
                room_temp=decode_temp(rec[7]),
                raw=rec,
            )
        )
    return units


def keepalive_reply(now: float | None = None) -> bytes:
    ts = int(now if now is not None else time.time())
    return struct.pack("<HHII8s", 20, CMD_KEEPALIVE_REPLY, 0, ts, bytes(8))


def address_bitmap(addrs: list[int] | set[int]) -> bytes:
    bm = bytearray(16)
    for a in addrs:
        if not 0 <= a < 128:
            raise ValueError(f"endereço fora da faixa: {a}")
        bm[a // 8] |= 1 << (a % 8)
    return bytes(bm)


def build_control(
    *,
    serial: bytes,
    route: bytes,
    seq: int,
    addrs: list[int] | set[int],
    mode: str,
    fan: str,
    set_temp: int,
    token: bytes | None = None,
    now: float | None = None,
) -> bytes:
    """Monta o comando 0x9010. `mode` = off/cool/heat/dry/fan_only/auto.

    `token` são os 4 bytes [28:32] que o gateway envia no status e espera de volta
    no comando (a nuvem ecoa esse valor). Sem ele, cai no `00 9e` + seq (só testes).
    """
    if len(serial) != 8 or len(route) != 8:
        raise ValueError("serial/route devem ter 8 bytes")
    if token is not None and len(token) != 4:
        raise ValueError("token deve ter 4 bytes")
    if mode == "off":
        mode_byte = MODE_OFF
    else:
        mode_byte = MODE_ON | MODE_BITS[mode]
    if not 0 <= set_temp <= 0x3F:
        raise ValueError(f"setpoint inválido: {set_temp}")
    ts = int(now if now is not None else time.time())
    out = struct.pack("<HHII", 72, CMD_CONTROL, 0, ts)
    out += serial + route
    out += token if token is not None else b"\x00\x9e" + struct.pack("<H", seq & 0xFFFF)
    out += bytes((0, SUB_CONTROL, 0, 0))
    # Campo [36:40]: o gateway aceita um checksum que não bate com o conteúdo
    # (testado), mas pode rejeitar tudo-zero. Usamos um valor não-zero fixo
    # capturado de um comando real da nuvem.
    out += CONTROL_CHECKSUM_PLACEHOLDER
    out += address_bitmap(addrs)
    out += bytes((0xC3, mode_byte, FAN_BITS[fan], set_temp))
    out += bytes(12)
    assert len(out) == 72
    return out

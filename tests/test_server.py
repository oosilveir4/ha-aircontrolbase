"""Servidor local: handshake, keepalive, status e comando, com um gateway falso."""

import asyncio
import base64
import struct

import pytest

from custom_components.aircontrolbase import protocol as p
from custom_components.aircontrolbase.server import LocalGatewayServer, _Connection

SERIAL = bytes.fromhex("11a1182f01000110")
ROUTE = bytes.fromhex("7f0000010100007f")
TOKEN = bytes.fromhex("009e194b")


def _keepalive() -> bytes:
    body = bytearray(36)
    body[0:2] = (36).to_bytes(2, "little")
    body[2:4] = p.CMD_KEEPALIVE.to_bytes(2, "little")
    return bytes(body)


def _status(addr: int, mode_byte: int, fan_byte: int, set_t: int, room_raw: int) -> bytes:
    body = bytearray(40)
    body[2:4] = p.CMD_REPORT.to_bytes(2, "little")
    body[12:20] = SERIAL
    body[20:28] = ROUTE
    body[28:32] = TOKEN
    body[33] = p.SUB_STATUS
    rec = bytearray(36)
    rec[0], rec[4], rec[5], rec[6], rec[7] = addr, mode_byte, fan_byte, set_t, room_raw
    body += rec
    body[0:2] = len(body).to_bytes(2, "little")
    return bytes(body)


class FakeTransport:
    """Captura o que o servidor enviaria."""

    def __init__(self):
        self.sent = bytearray()

    async def send(self, data: bytes):
        self.sent += data


async def test_connection_handshake_e_fluxo():
    t = FakeTransport()
    updates = []
    conn = _Connection(t.send, on_update=updates.append, now=lambda: 1000.0)
    await conn.start()
    challenge = bytes(t.sent)
    assert challenge[:4] == b"\x0a\x00\xff\xff"

    # gateway responde o handshake e manda keepalive + status num só chunk
    buf = bytearray(
        b"\x10\x00\xff\xff" + base64.b64encode(challenge[4:12])
        + _keepalive()
        + _status(21, 0x88, 0x04, 20, 0x50)
    )
    t.sent.clear()
    await conn.feed(buf)

    # respondeu o keepalive com o horário injetado
    assert t.sent == p.keepalive_reply(1000.0)
    assert conn.serial == SERIAL
    assert conn.units[21].mode == "cool"
    assert conn.units[21].room_temp == 20.0
    assert updates and updates[-1][21].set_temp == 20


async def test_connection_recusa_handshake_errado():
    conn = _Connection(FakeTransport().send)
    await conn.start()
    with pytest.raises(p.ProtocolError):
        await conn.feed(bytearray(b"\x10\x00\xff\xffWRONGVALUE__"))


async def test_connection_comando_usa_horario_e_seq():
    t = FakeTransport()
    conn = _Connection(t.send, now=lambda: 0x6AFC7958)
    await conn.start()
    await conn.feed(bytearray(
        b"\x10\x00\xff\xff" + base64.b64encode(bytes(t.sent)[4:12]) + _status(21, 0x88, 0x04, 20, 0x50)
    ))
    t.sent.clear()
    await conn.send_command([21], mode="cool", fan="low", set_temp=20)
    # keepalive-reply (sincroniza relógio) seguido do comando
    frames = p.split_frames(bytearray(t.sent))
    assert frames[0].cmd == p.CMD_KEEPALIVE_REPLY
    assert struct.unpack_from("<I", frames[0].data, 8)[0] == 0x6AFC7958
    cmd = frames[1].data
    assert len(cmd) == 72
    assert struct.unpack_from("<I", cmd, 8)[0] == 0x6AFC7958  # timestamp fresco
    assert cmd[28:32] == TOKEN  # ecoa o token do status
    bm = int.from_bytes(cmd[40:56], "little")
    assert [i for i in range(128) if bm >> i & 1] == [21]
    assert cmd[56:60] == bytes((0xC3, 0x88, 0x04, 20))


async def test_connection_comando_sem_auth_falha():
    conn = _Connection(FakeTransport().send)
    with pytest.raises(p.ProtocolError):
        await conn.send_command([1], mode="cool", fan="auto", set_temp=24)


async def _fake_gateway(host, port, *, reply=True):
    """Conecta como o gateway: faz handshake e devolve leitor/escritor."""
    reader, writer = await asyncio.open_connection(host, port)
    challenge = await reader.readexactly(12)
    if reply:
        writer.write(b"\x10\x00\xff\xff" + base64.b64encode(challenge[4:12]))
        await writer.drain()
    return reader, writer


async def test_servidor_integra_status_e_comando():
    updates = []
    srv = LocalGatewayServer(host="127.0.0.1", port=0, on_update=updates.append)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    reader, writer = await _fake_gateway("127.0.0.1", port)
    try:
        writer.write(_keepalive() + _status(10, 0x84, 0x00, 24, 0x52))
        await writer.drain()
        assert p.split_frames(bytearray(await reader.readexactly(20)))[0].cmd == p.CMD_KEEPALIVE_REPLY

        await asyncio.sleep(0.05)
        assert srv.connected
        assert srv.units[10].mode == "heat"
        assert updates and updates[-1][10].room_temp == 21.0

        await srv.send_command([10], mode="cool", fan="high", set_temp=22)
        # keepalive-reply (20B) + comando (72B)
        ka = bytes(await reader.readexactly(20))
        assert p.split_frames(bytearray(ka))[0].cmd == p.CMD_KEEPALIVE_REPLY
        cmd = bytes(await reader.readexactly(72))
        assert cmd[2:4] == p.CMD_CONTROL.to_bytes(2, "little")
        assert cmd[56:60] == bytes((0xC3, 0x88, 0x01, 22))
    finally:
        writer.close()
        await srv.stop()


async def test_servidor_sem_conexao_recusa_comando():
    srv = LocalGatewayServer(host="127.0.0.1", port=0)
    await srv.start()
    try:
        assert not srv.connected
        with pytest.raises(p.ProtocolError):
            await srv.send_command([1], mode="cool", fan="auto", set_temp=24)
    finally:
        await srv.stop()

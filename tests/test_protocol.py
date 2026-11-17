"""Protocolo TCP do gateway, validado contra pacotes reais capturados em 2026-11-17."""

import base64

import pytest

from custom_components.aircontrolbase import protocol as p

SERIAL = bytes.fromhex("11a1182f01000110")
ROUTE = bytes.fromhex("7f0000010100007f")

# comando real da nuvem: endereço 21, frio, ventilação alta... (12:57:12 = fan 0x04 baixa)
REAL_CMD = bytes.fromhex(
    "48001090000000005879fc6a11a1182f010001107f0000010100007f009eea8200030000"
    "3858d94200002000000000000000000000000000c3880414000000000000000000000000"
)
REAL_ACK = bytes.fromhex(
    "28001290000000005879fc6a11a1182f010001107f0000010100007f009eea820004000021"
    "54f671"
)


def _status_frame(records: list[bytes]) -> bytes:
    body = bytearray(40)
    body[2:4] = (p.CMD_REPORT).to_bytes(2, "little")
    body[12:20] = SERIAL
    body[33] = p.SUB_STATUS
    for r in records:
        body += r.ljust(36, b"\0")
    body[0:2] = len(body).to_bytes(2, "little")
    return bytes(body)


def test_handshake():
    ch = p.make_challenge()
    assert ch[:4] == b"\x0a\x00\xff\xff" and len(ch) == 12
    assert p.check_challenge_reply(ch, b"\x10\x00\xff\xff" + base64.b64encode(ch[4:]))
    # resposta real capturada para o desafio "0bd3b8b0"
    real_ch = bytes.fromhex("0a00ffff3062643362386230")
    real_reply = bytes.fromhex("1000ffff4d474a6b4d324934596a413d")
    assert p.check_challenge_reply(real_ch, real_reply)
    assert not p.check_challenge_reply(ch, real_reply)


def test_build_control_igual_ao_real_menos_ts_seq_checksum():
    built = p.build_control(
        serial=SERIAL, route=ROUTE, seq=0x82EA, addrs=[21],
        mode="cool", fan="low", set_temp=20, now=0x6AFC7958,
    )
    assert len(built) == 72
    # tudo igual, exceto o checksum [36:40] (não validado pelo gateway)
    assert built[:36] == REAL_CMD[:36]
    assert built[40:] == REAL_CMD[40:]


@pytest.mark.parametrize(
    ("mode", "byte"),
    [("off", 0x00), ("cool", 0x88), ("heat", 0x84), ("dry", 0x82),
     ("fan_only", 0x81), ("auto", 0x90)],
)
def test_modos(mode, byte):
    cmd = p.build_control(serial=SERIAL, route=ROUTE, seq=1, addrs=[10],
                          mode=mode, fan="auto", set_temp=24)
    assert cmd[57] == byte
    assert p.decode_mode(byte) == mode


@pytest.mark.parametrize(("fan", "byte"),
                         [("auto", 0x80), ("low", 0x04), ("medium", 0x02), ("high", 0x01)])
def test_ventilacao(fan, byte):
    cmd = p.build_control(serial=SERIAL, route=ROUTE, seq=1, addrs=[10],
                          mode="cool", fan=fan, set_temp=24)
    assert cmd[58] == byte
    assert p.decode_fan(byte) == fan


def test_bitmap_grupo():
    assert p.address_bitmap([21]).hex() == "00002000000000000000000000000000"
    assert p.address_bitmap([20]).hex() == "00001000000000000000000000000000"
    assert p.address_bitmap([0, 10, 43]).hex() == "01040000000800000000000000000000"
    with pytest.raises(ValueError):
        p.address_bitmap([128])


def test_split_frames_parcial_e_multiplo():
    buf = bytearray(REAL_ACK + p.keepalive_reply(0) + REAL_CMD[:10])
    frames = p.split_frames(buf)
    assert [f.cmd for f in frames] == [p.CMD_REPORT, p.CMD_KEEPALIVE_REPLY]
    assert frames[0].subtype == p.SUB_ACK
    assert frames[0].serial == SERIAL
    assert len(buf) == 10  # quadro incompleto fica no buffer
    buf += REAL_CMD[10:]
    assert [f.data for f in p.split_frames(buf)] == [REAL_CMD]
    assert buf == bytearray()


def test_split_frames_invalido():
    with pytest.raises(p.ProtocolError):
        p.split_frames(bytearray(b"\x02\x00\x00\x90"))


def test_keepalive_reply_real():
    # resposta real da nuvem: 1400019000000000aa77fc6a0000000000000000
    assert p.keepalive_reply(0x6AFC77AA).hex() == "1400019000000000aa77fc6a0000000000000000"


def test_parse_status_registros_reais():
    frame = p.split_frames(bytearray(_status_frame([
        bytes.fromhex("150000008804145000000000"),  # addr 21 frio baixa 20 °C, amb 20
        bytes.fromhex("0a0000008400185200000000"),  # addr 10 aquecimento, fan 00
        bytes.fromhex("280000000000134400000000"),  # addr 40 desligado, amb 14
    ])))[0]
    u21, u10, u40 = p.parse_status(frame)
    assert (u21.addr, u21.mode, u21.fan, u21.set_temp, u21.room_temp) == (21, "cool", "low", 20, 20.0)
    assert (u10.mode, u10.fan, u10.set_temp, u10.room_temp) == ("heat", None, 24, 21.0)
    assert (u40.power, u40.room_temp) == (False, 14.0)


def test_parse_status_rejeita_ack():
    frame = p.split_frames(bytearray(REAL_ACK))[0]
    with pytest.raises(p.ProtocolError):
        p.parse_status(frame)


def test_build_control_ecoa_token():
    token = bytes.fromhex("009e194b")
    cmd = p.build_control(serial=SERIAL, route=ROUTE, seq=0x4003, token=token,
                          addrs=[0], mode="cool", fan="high", set_temp=23)
    assert cmd[28:32] == token  # token tem prioridade sobre o seq
    with pytest.raises(ValueError):
        p.build_control(serial=SERIAL, route=ROUTE, seq=1, token=b"\x00",
                        addrs=[0], mode="cool", fan="high", set_temp=23)

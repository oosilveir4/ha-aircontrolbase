"""Modo híbrido: tap de status do proxy e merge de estado local sobre metadados."""

import time

from custom_components.aircontrolbase import protocol as p
from custom_components.aircontrolbase.hybrid import HybridCoordinator, HybridProxy

SERIAL = bytes.fromhex("11a1182f01000110")
TOKEN = bytes.fromhex("009e194b")


def _status(addr, mode_byte, fan_byte, set_t, room_raw):
    body = bytearray(40)
    body[2:4] = p.CMD_REPORT.to_bytes(2, "little")
    body[12:20] = SERIAL
    body[28:32] = TOKEN
    body[33] = p.SUB_STATUS
    rec = bytearray(36)
    rec[0], rec[4], rec[5], rec[6], rec[7] = addr, mode_byte, fan_byte, set_t, room_raw
    body += rec
    body[0:2] = len(body).to_bytes(2, "little")
    return bytes(body)


def test_proxy_tap_decodifica_status_e_token():
    proxy = HybridProxy()
    buf = bytearray(
        _status(10, 0x88, 0x04, 20, 0x50) + _status(21, 0x00, 0x00, 25, 0x52)
    )
    proxy._tap(buf)
    assert proxy.token == TOKEN
    assert proxy.units[10].mode == "cool"
    assert proxy.units[10].set_temp == 20
    assert proxy.units[21].mode == "off"
    assert buf == bytearray()  # consumiu os quadros completos


def test_proxy_tap_guarda_parcial():
    proxy = HybridProxy()
    frame = _status(10, 0x88, 0x04, 20, 0x50)
    buf = bytearray(frame[:-5])  # quadro incompleto
    proxy._tap(buf)
    assert not proxy.units
    buf += frame[-5:]
    proxy._tap(buf)
    assert proxy.units[10].set_temp == 20


class _FakeProxy:
    def __init__(self, units):
        self.units = units


def _coord_with(meta, units):
    c = object.__new__(HybridCoordinator)
    c._meta = meta
    c._meta_ts = time.time()
    c._proxy = _FakeProxy(units)
    c._addr_of = {"40783273": 10, "53033731": 21}
    return c


async def test_merge_overlay_estado_local():
    meta = {
        "devices": {
            "40783273": {"id": "40783273", "name": "Ent.Ponte", "area": "Ar Parede",
                         "power": "n", "mode": "cool", "wind": "auto",
                         "setTemp": 24.0, "factTemp": 0.0, "unlock": "mode"},
            "53033731": {"id": "53033731", "name": "Corredor", "area": "Ar Interno",
                         "power": "n", "mode": "heat", "wind": "low",
                         "setTemp": 25.0, "factTemp": 0.0, "unlock": "mode"},
        },
        "areas": [{"id": "Ar Parede", "name": "Ar Parede", "device_ids": ["40783273"]}],
        "summary": {"cool": 0, "heat": 0, "fan": 0, "stop": 2, "lock": 0, "error": 0},
    }
    # estado local fresco: addr 10 ligado frio 20; addr 21 sem reporte
    units = {
        10: p.UnitStatus(10, "cool", 0, "low", 0, 20, 21.0, b"\0\0\0\0\0\0\0"),
    }
    coord = _coord_with(meta, units)
    data = await coord._async_update_data()
    d10 = data["devices"]["40783273"]
    assert d10["power"] == "y" and d10["setTemp"] == 20.0 and d10["factTemp"] == 21.0
    assert d10["wind"] == "low"
    assert d10["unlock"] == "mode"  # metadado da nuvem preservado
    # addr 21 sem reporte local: mantém o estado da nuvem
    d21 = data["devices"]["53033731"]
    assert d21["power"] == "n" and d21["setTemp"] == 25.0
    assert data["areas"] == meta["areas"]


async def test_merge_mode_off_usa_modo_da_nuvem():
    meta = {
        "devices": {"40783273": {"id": "40783273", "name": "x", "area": "a",
                                 "power": "y", "mode": "heat", "wind": "auto",
                                 "setTemp": 24.0, "factTemp": 0.0}},
        "areas": [], "summary": {},
    }
    units = {10: p.UnitStatus(10, "off", 0, None, 0, 24, 22.0, b"\0\0\0\0\0\0\0")}
    coord = _coord_with(meta, units)
    data = await coord._async_update_data()
    d = data["devices"]["40783273"]
    assert d["power"] == "n"
    # desligado: preserva o modo configurado da nuvem (heat)
    assert d["mode"] == "heat"

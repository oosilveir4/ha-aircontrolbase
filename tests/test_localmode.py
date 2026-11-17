"""Modo local: tradução de dados e comando, sem HA/socket."""

import pytest

from custom_components.aircontrolbase import protocol as p
from custom_components.aircontrolbase.local import decode_unit
from custom_components.aircontrolbase.localmode import (
    SITE_DEVICES,
    DeviceEntry,
    LocalController,
    build_data,
)

META = (
    DeviceEntry(10, "40783273", "Ent.Ponte", "Ar Parede"),
    DeviceEntry(21, "53033731", "Corredor Suporte", "Ar Interno"),
    DeviceEntry(40, "104686792", "Ar Caixa Fotos", "Ar Fotos"),
)


def _unit(addr, mode, fan, set_t, room, raw4=0x00) -> p.UnitStatus:
    return p.UnitStatus(addr=addr, mode=mode, mode_byte=0, fan=fan, fan_byte=0,
                        set_temp=set_t, room_temp=room, raw=bytes([0, 0, 0, 0, raw4, 0, 0]))


def test_site_map_consistente():
    assert len(SITE_DEVICES) == 20
    addrs = [m.addr for m in SITE_DEVICES]
    ids = [m.device_id for m in SITE_DEVICES]
    assert len(set(addrs)) == 20 and len(set(ids)) == 20


def test_build_data_traduz_estado():
    units = {
        10: _unit(10, "cool", "low", 20, 21.0),
        21: _unit(21, "off", None, 25, 18.0),
        40: _unit(40, "heat", "auto", 24, 14.0),
    }
    data = build_data(META, units, {})
    d10 = data["devices"]["40783273"]
    assert (d10["power"], d10["mode"], d10["wind"], d10["setTemp"], d10["factTemp"]) == (
        "y", "cool", "low", 20.0, 21.0)
    d21 = data["devices"]["53033731"]
    assert d21["power"] == "n" and d21["mode"] == "cool"  # mode preservado p/ religar
    assert data["devices"]["104686792"]["mode"] == "heat"


def test_build_data_resumo_e_areas():
    units = {
        10: _unit(10, "cool", "low", 20, 21.0),
        21: _unit(21, "off", None, 25, 18.0),
        40: _unit(40, "heat", "auto", 24, 14.0),
    }
    data = build_data(META, units, {})
    assert data["summary"]["cool"] == 1
    assert data["summary"]["heat"] == 1
    assert data["summary"]["stop"] == 1
    areas = {a["name"]: a["device_ids"] for a in data["areas"]}
    assert areas["Ar Parede"] == ["40783273"]
    assert areas["Ar Fotos"] == ["104686792"]


def test_build_data_unidade_ausente_fica_fora():
    data = build_data(META, {10: _unit(10, "cool", "low", 20, 21.0)}, {})
    assert set(data["devices"]) == {"40783273"}
    # área existe mas sem device
    areas = {a["name"]: a["device_ids"] for a in data["areas"]}
    assert areas["Ar Interno"] == []


def test_build_data_travas_e_erro_do_http():
    # addr 22: trava de aquecimento 22°C, byte2=0 (sem erro)
    lock = decode_unit(10, "00160010a01014")
    units = {10: _unit(10, "cool", "low", 20, 21.0)}
    data = build_data(META, units, {10: lock})
    dev = data["devices"]["40783273"]
    assert "heat" not in dev["unlock"].split(",")  # heat travado
    assert dev["heatLockValue"] == "22"
    assert data["summary"]["lock"] == 1


def test_build_data_erro_conta_no_resumo():
    lock = decode_unit(10, "000003000000fb")  # E2
    units = {10: _unit(10, "cool", "low", 20, 21.0)}
    data = build_data(META, units, {10: lock})
    assert data["devices"]["40783273"]["raw"]["error"] == "E2"
    assert data["summary"]["error"] == 1


class _FakeServer:
    def __init__(self):
        self.sent = []

    async def send_command(self, addrs, *, mode, fan, set_temp):
        self.sent.append((addrs, mode, fan, set_temp))


async def test_controller_liga_no_frio():
    srv = _FakeServer()
    ctl = LocalController(srv, META)
    await ctl.control_device(
        device_id="53033731",
        current={"power": "n", "mode": "cool", "wind": "auto", "setTemp": 24},
        changes={"power": "y", "mode": "cool", "wind": "auto"},
    )
    assert srv.sent == [([21], "cool", "auto", 24)]


async def test_controller_setpoint_preserva_modo():
    srv = _FakeServer()
    ctl = LocalController(srv, META)
    await ctl.control_device(
        device_id="40783273",
        current={"power": "y", "mode": "heat", "wind": "high", "setTemp": 24},
        changes={"setTemp": 20},
    )
    assert srv.sent == [([10], "heat", "high", 20)]


async def test_controller_fan_mid_vira_medium():
    srv = _FakeServer()
    ctl = LocalController(srv, META)
    await ctl.control_device(
        device_id="40783273",
        current={"power": "y", "mode": "cool", "wind": "auto", "setTemp": 22},
        changes={"wind": "mid"},
    )
    assert srv.sent[0][2] == "medium"


async def test_controller_desliga():
    srv = _FakeServer()
    ctl = LocalController(srv, META)
    await ctl.control_device(
        device_id="40783273",
        current={"power": "y", "mode": "cool", "wind": "low", "setTemp": 22},
        changes={"power": "n"},
    )
    assert srv.sent == [([10], "off", "low", 22)]


async def test_controller_rejeita_trava():
    from homeassistant.exceptions import HomeAssistantError
    ctl = LocalController(_FakeServer(), META)
    with pytest.raises(HomeAssistantError):
        await ctl.control_device(device_id="40783273", current={}, changes={},
                                 lock_token="mode", locked=True)


async def test_controller_device_desconhecido():
    from homeassistant.exceptions import HomeAssistantError
    ctl = LocalController(_FakeServer(), META)
    with pytest.raises(HomeAssistantError):
        await ctl.control_device(device_id="999", current={}, changes={"setTemp": 20})


async def test_setup_entry_roteia_para_local(monkeypatch):
    """options[local]=True deve iniciar o coordinator local e parar no unload."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    import custom_components.aircontrolbase as integ

    coord = MagicMock(
        async_start=AsyncMock(),
        async_config_entry_first_refresh=AsyncMock(),
        async_stop=AsyncMock(),
        controller=MagicMock(),
    )
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.services.has_service = MagicMock(return_value=True)
    entry = SimpleNamespace(
        entry_id="e1", data={}, options={"local": True}
    )
    with patch(
        "custom_components.aircontrolbase.localmode.LocalCoordinator",
        return_value=coord,
    ):
        assert await integ.async_setup_entry(hass, entry) is True
    coord.async_start.assert_awaited_once()
    bucket = hass.data["aircontrolbase"]["e1"]
    assert bucket["local"] is True and bucket["client"] is coord.controller

    assert await integ.async_unload_entry(hass, entry) is True
    coord.async_stop.assert_awaited_once()

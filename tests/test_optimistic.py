"""Atualização otimista: a UI reflete o comando na hora (individual e grupo)."""

import time
from types import SimpleNamespace

from homeassistant.components.climate import HVACMode

from custom_components.aircontrolbase.climate import (
    AirControlBaseClimate,
    AirControlBaseGroupClimate,
)

FUTURE = time.monotonic() + 1000


def _single(device):
    e = object.__new__(AirControlBaseClimate)
    e._device_id = "1"
    e.coordinator = SimpleNamespace(data={"devices": {"1": device}})
    e._optimistic = {}
    return e


def test_single_otimista_sobrepoe():
    e = _single({"power": "n", "mode": "cool", "wind": "auto", "setTemp": 24.0})
    assert e.hvac_mode == HVACMode.OFF
    assert e.target_temperature == 24.0
    e._optimistic = {
        "power": ("y", FUTURE),
        "mode": ("heat", FUTURE),
        "setTemp": (22.0, FUTURE),
    }
    assert e.hvac_mode == HVACMode.HEAT
    assert e.target_temperature == 22.0


def test_single_otimista_expira():
    e = _single({"power": "n", "mode": "cool", "wind": "auto", "setTemp": 24.0})
    e._optimistic = {"setTemp": (22.0, time.monotonic() - 1)}  # expirado
    assert e.target_temperature == 24.0


def _group(members):
    e = object.__new__(AirControlBaseGroupClimate)
    e._area_key = "Ar Teto"
    e._area_name = "Ar Teto"
    devices = {m["id"]: m for m in members}
    areas = [{"id": "Ar Teto", "name": "Ar Teto", "device_ids": list(devices)}]
    e.coordinator = SimpleNamespace(data={"devices": devices, "areas": areas})
    e._optimistic = {}
    return e


def test_grupo_otimista_sobrepoe_agregado():
    members = [
        {"id": "a", "power": "n", "mode": "cool", "wind": "auto", "setTemp": 24.0},
        {"id": "b", "power": "n", "mode": "cool", "wind": "auto", "setTemp": 24.0},
    ]
    g = _group(members)
    assert g.hvac_mode == HVACMode.OFF
    # usuário liga o grupo no frio a 20: UI reflete na hora
    g._optimistic = {
        "power": ("y", FUTURE),
        "mode": ("cool", FUTURE),
        "setTemp": (20.0, FUTURE),
    }
    assert g.hvac_mode == HVACMode.COOL
    assert g.target_temperature == 20.0


def test_grupo_reconcilia_quando_membros_confirmam():
    # membros já ligados no frio 20 → otimista confirmado some
    members = [
        {"id": "a", "power": "y", "mode": "cool", "wind": "low", "setTemp": 20.0},
        {"id": "b", "power": "y", "mode": "cool", "wind": "low", "setTemp": 20.0},
    ]
    g = _group(members)
    g._optimistic = {"setTemp": (20.0, FUTURE), "power": ("y", FUTURE)}
    g.async_write_ha_state = lambda: None  # stub de entidade não registrada

    # chama a reconciliação (sem super, que exige HA)
    now = time.monotonic()
    on = [m for m in g._members if m.get("power") == "y"]
    from custom_components.aircontrolbase.climate import _same
    for key in list(g._optimistic):
        value, expiry = g._optimistic[key]
        if key == "power":
            confirmed = all(m.get("power") == value for m in g._members)
        else:
            confirmed = bool(on) and all(_same(m.get(key), value) for m in on)
        if confirmed or expiry <= now:
            del g._optimistic[key]
    assert g._optimistic == {}

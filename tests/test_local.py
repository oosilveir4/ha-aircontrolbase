"""Decodificação local do get_mbdata_all.jsn (amostras reais do gateway 192.168.10.234)."""

import pytest

from custom_components.aircontrolbase.local import (
    LocalGatewayError,
    decode_all,
    decode_unit,
)


def test_ligado_frio():
    u = decode_unit(0, "000000819a0015")
    assert u.power is True
    assert u.mode == "cool"
    assert u.fan_raw == 4
    assert u.set_temp == 19
    assert u.room_temp == 21
    assert u.error is None
    assert u.locked is False


def test_desligado_mantem_modo_raw():
    u = decode_unit(10, "000000b0980015")
    assert u.power is False
    assert u.mode == "off"
    assert u.mode_raw == 4
    assert u.set_temp == 19


def test_travas():
    # byte5=0x10 -> trava de temp. de aquecimento; byte1=0x16 -> 22
    u = decode_unit(22, "00160010a01014")
    assert u.lock_heat_temp == 22
    assert u.lock_cool_temp == 0
    assert u.locked is True


def test_erro_e_temperatura_negativa():
    u = decode_unit(5, "000003000000fb")
    assert u.error == "E2"
    assert u.room_temp == -5
    assert decode_unit(5, "000012000000ff").error == "P1"


def test_endereco_vazio_e_lista():
    assert decode_unit(3, "-") is None
    units = decode_all([
        {"addr": 0, "Data": "000000819a0015"},
        {"addr": 1, "Data": "-"},
        {"addr": 40, "Data": "000000b0980015"},
    ])
    assert sorted(units) == [0, 40]


@pytest.mark.parametrize("payload", [{}, [{"Data": "00"}], [{"addr": 0, "Data": "zz"}],
                                     [{"addr": 0, "Data": "0000"}]])
def test_payload_invalido(payload):
    with pytest.raises(LocalGatewayError):
        decode_all(payload)

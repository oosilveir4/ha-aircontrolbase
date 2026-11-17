"""Regressões com classes reais do HA e transporte HTTP simulado."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.aircontrolbase import async_setup_entry
from custom_components.aircontrolbase.api import (
    AirControlBaseClient,
    AirControlBaseError,
    AuthError,
    SessionExpired,
)
from custom_components.aircontrolbase.climate import AirControlBaseGroupClimate
from custom_components.aircontrolbase.climate import async_setup_entry as setup_climate
from custom_components.aircontrolbase.config_flow import (
    AirControlBaseConfigFlow,
    AirControlBaseOptionsFlow,
)
from custom_components.aircontrolbase.coordinator import AirControlBaseCoordinator
from custom_components.aircontrolbase.sensor import AirControlBaseErrorSensor
from custom_components.aircontrolbase.switch import SWITCH_DESCRIPTIONS, ACBLockSwitch


def details(**device):
    return {
        "code": 200,
        "result": {
            "areas": [{"id": "area-1", "name": "Sala", "data": [{"id": 1, **device}]}],
            "deviceCase": {},
        },
    }


def client():
    result = AirControlBaseClient("account", "password", "user")
    result._logged_in = True
    return result


@pytest.mark.parametrize("code", [500, "500", "invalid"])
async def test_api_rejects_business_errors(code):
    api = client()
    api._request = AsyncMock(return_value={"code": code})
    with pytest.raises(AirControlBaseError):
        await api.get_details()


@pytest.mark.parametrize("code", [40018, "40018"])
async def test_expired_session_retries_once(code):
    api = client()
    api._request = AsyncMock(
        side_effect=[{"code": code}, {"code": 200, "userId": "user"}, details()]
    )
    assert await api.get_details() == details()
    assert api._request.await_count == 3


async def test_session_still_expired_is_auth_failure():
    api = client()
    api._request = AsyncMock(
        side_effect=[{"code": 40018}, {"code": 200}, {"code": 40018}]
    )
    with pytest.raises(SessionExpired):
        await api.get_details()
    assert not api._logged_in


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError(), TimeoutError()])
async def test_network_errors_are_wrapped(error):
    api = client()
    api._session = MagicMock(closed=False)
    api._session.post.side_effect = error
    with pytest.raises(AirControlBaseError):
        await api.get_details()


@pytest.mark.parametrize("payload", [[], None, "bad"])
async def test_invalid_json_shape_is_rejected(payload):
    api = client()
    response = MagicMock(status=200)
    response.text = AsyncMock(return_value=json.dumps(payload))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    api._session = MagicMock(closed=False)
    api._session.post.return_value = context
    with pytest.raises(AirControlBaseError):
        await api.get_details()


async def test_concurrent_requests_share_one_login():
    api = client()
    api._logged_in = False
    api._request = AsyncMock(side_effect=[{"code": 200}, details(), details()])
    await asyncio.gather(api.get_details(), api.get_details())
    assert api._request.await_count == 3


async def test_concurrent_commands_preserve_recent_changes_and_locks():
    api = client()
    api._post = AsyncMock(return_value={"code": 200})
    state = {"setTemp": 24, "unlock": ""}
    with patch(
        "custom_components.aircontrolbase.api.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        await asyncio.gather(
            api.control_device(device_id=1, current=state, changes={"setTemp": 22}),
            api.control_device(device_id=1, current=state, changes={"wind": "high"}),
            api.control_device(
                device_id=1,
                current=state,
                changes={},
                lock_token="remote",
                locked=False,
            ),
            api.control_device(
                device_id=1, current=state, changes={}, lock_token="mode", locked=False
            ),
        )
    commands = [
        json.loads(call.args[1]["control"]) for call in api._post.await_args_list
    ]
    assert commands[1]["setTemp"] == 22
    assert commands[1]["unlock"] == ""
    assert set(commands[3]["unlock"].split(",")) == {"mode", "remote"}
    assert sleep.await_count == 3
    assert all(call.args[0] > 0.7 for call in sleep.await_args_list)


async def test_failed_command_does_not_become_pending():
    api = client()
    api._post = AsyncMock(side_effect=AirControlBaseError())
    with pytest.raises(AirControlBaseError):
        await api.control_device(device_id=1, current={}, changes={"setTemp": 22})
    assert not api._pending["1"]


async def test_coordinator_preserves_empty_unlock_and_bad_temperature(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    api = client()
    api.get_details = AsyncMock(
        return_value=details(unlock="", setTemp="24.5", factTemp="--", error="E0")
    )
    coord = AirControlBaseCoordinator(hass, api)
    coord.data = await coord._async_update_data()
    assert coord.data["devices"]["1"]["unlock"] == ""
    assert coord.data["devices"]["1"]["setTemp"] == 24.5
    assert coord.data["devices"]["1"]["factTemp"] is None
    for description in SWITCH_DESCRIPTIONS:
        assert ACBLockSwitch(coord, api, "1", description).is_on
    sensor = AirControlBaseErrorSensor(coord, "1")
    assert sensor.native_value == "E0"
    coord.data["devices"]["1"]["raw"]["error"] = "n"
    assert sensor.native_value == "OK"
    del coord.data["devices"]["1"]["raw"]["error"]
    assert sensor.native_value is None


@pytest.mark.parametrize(
    "payload", [{}, {"result": []}, {"result": {"areas": [{"data": "bad"}]}}]
)
async def test_coordinator_rejects_incomplete_responses(tmp_path, payload):
    api = client()
    api.get_details = AsyncMock(return_value=payload)
    coord = AirControlBaseCoordinator(HomeAssistant(str(tmp_path)), api)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_coordinator_auth_failure_starts_reauth(tmp_path):
    api = client()
    api.get_details = AsyncMock(side_effect=AuthError())
    coord = AirControlBaseCoordinator(HomeAssistant(str(tmp_path)), api)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


@pytest.mark.parametrize("step", ["user", "userid"])
async def test_config_flow_closes_client_on_failure(step):
    flow = AirControlBaseConfigFlow()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_show_form = MagicMock(side_effect=lambda **kwargs: kwargs)
    api = MagicMock(
        login=AsyncMock(side_effect=AuthError("invalid")), close=AsyncMock()
    )
    with patch(
        "custom_components.aircontrolbase.config_flow.AirControlBaseClient",
        return_value=api,
    ):
        result = await getattr(flow, f"async_step_{step}")(
            {"account": "test", "password": "test", "user_id": "test"}
        )
    assert result["errors"]
    api.close.assert_awaited_once()


@pytest.mark.parametrize(
    "failure,expected",
    [
        (AirControlBaseError(), ConfigEntryNotReady),
        (AuthError(), ConfigEntryAuthFailed),
    ],
)
async def test_setup_closes_client_and_classifies_login_failure(failure, expected):
    api = MagicMock(login=AsyncMock(side_effect=failure), close=AsyncMock())
    entry = SimpleNamespace(data={"account": "test", "password": "test"}, options={})
    with (
        patch(
            "custom_components.aircontrolbase.AirControlBaseClient", return_value=api
        ),
        pytest.raises(expected),
    ):
        await async_setup_entry(MagicMock(), entry)
    api.close.assert_awaited_once()


async def test_setup_closes_client_when_first_refresh_fails():
    api = MagicMock(login=AsyncMock(), close=AsyncMock())
    coord = MagicMock(
        async_config_entry_first_refresh=AsyncMock(side_effect=ConfigEntryNotReady())
    )
    entry = SimpleNamespace(data={"account": "test", "password": "test"}, options={})
    with (
        patch(
            "custom_components.aircontrolbase.AirControlBaseClient", return_value=api
        ),
        patch(
            "custom_components.aircontrolbase.AirControlBaseCoordinator",
            return_value=coord,
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(MagicMock(), entry)
    api.close.assert_awaited_once()


async def test_group_continues_after_failure_and_reports_it():
    coord = SimpleNamespace(
        data={
            "areas": [{"id": "a", "name": "Sala", "device_ids": ["1", "2"]}],
            "devices": {"1": {"id": "1"}, "2": {"id": "2"}},
        },
        async_request_refresh=AsyncMock(),
    )
    api = MagicMock(
        control_device=AsyncMock(side_effect=[AirControlBaseError(), {"code": 200}])
    )
    group = AirControlBaseGroupClimate(coord, api, "entry", "Sala", "a")
    with (
        patch(
            "custom_components.aircontrolbase.climate.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        pytest.raises(HomeAssistantError, match="1/2"),
    ):
        await group._apply_to_all({"power": "n"})
    assert api.control_device.await_count == 2
    coord.async_request_refresh.assert_awaited_once()


def test_group_identity_uses_entry_and_area_id():
    coord = SimpleNamespace()
    a = AirControlBaseGroupClimate(coord, None, "entry1", "Ar Teto", "a")
    b = AirControlBaseGroupClimate(coord, None, "entry1", "Ar-Teto", "b")
    c = AirControlBaseGroupClimate(coord, None, "entry2", "Ar Teto", "a")
    assert len({a.unique_id, b.unique_id, c.unique_id}) == 3
    assert a.device_info["identifiers"] != c.device_info["identifiers"]


def test_options_flow_does_not_assign_readonly_config_entry():
    assert isinstance(AirControlBaseOptionsFlow(MagicMock()), AirControlBaseOptionsFlow)


async def test_closed_client_cannot_reopen_session_or_send_queued_commands():
    api = client()
    api._session = MagicMock(closed=False, close=AsyncMock())
    session = api._session
    await api.close()
    session.close.assert_awaited_once()
    with pytest.raises(AirControlBaseError, match="encerrado"):
        await api._ensure_session()
    with pytest.raises(AirControlBaseError, match="encerrado"):
        await api.control_device(device_id=1, current={}, changes={"power": "n"})
    with pytest.raises(AirControlBaseError, match="encerrado"):
        await api.get_details()
    assert api._session is None


async def test_null_device_list_is_empty_area(tmp_path):
    api = client()
    payload = details()
    payload["result"]["areas"][0]["data"] = None
    api.get_details = AsyncMock(return_value=payload)
    coord = AirControlBaseCoordinator(HomeAssistant(str(tmp_path)), api)
    result = await coord._async_update_data()
    assert result["devices"] == {}
    assert result["areas"][0]["device_ids"] == []


def test_numeric_error_code_is_preserved():
    coord = SimpleNamespace(data={"devices": {"1": {"raw": {"error": 12}}}})
    assert AirControlBaseErrorSensor(coord, "1").native_value == "12"


async def test_reauth_updates_existing_entry_and_closes_client():
    entry = SimpleNamespace(
        data={"account": "test", "password": "old", "user_id": "user"}
    )
    flow = AirControlBaseConfigFlow()
    flow.context = {"entry_id": "entry"}
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = entry
    flow.async_update_reload_and_abort = MagicMock(
        return_value={"type": "abort", "reason": "reauth_successful"}
    )
    api = MagicMock(
        login=AsyncMock(), get_details=AsyncMock(), close=AsyncMock(), user_id="user"
    )
    with patch(
        "custom_components.aircontrolbase.config_flow.AirControlBaseClient",
        return_value=api,
    ):
        result = await flow.async_step_reauth_confirm({"password": "new"})
    assert result["reason"] == "reauth_successful"
    assert flow.async_update_reload_and_abort.call_args.kwargs["data"] == {
        "account": "test",
        "password": "new",
        "user_id": "user",
    }
    api.close.assert_awaited_once()


async def test_group_migration_preserves_entity_id_and_discovers_equal_names():
    coord = SimpleNamespace(
        data={
            "devices": {},
            "areas": [
                {"id": "a", "name": "Sala", "device_ids": ["1", "2"]},
                {"id": "b", "name": "Sala", "device_ids": ["3", "4"]},
            ],
        },
        async_add_listener=MagicMock(),
    )
    entry = SimpleNamespace(entry_id="entry", options={}, async_on_unload=MagicMock())
    hass = SimpleNamespace(
        data={"aircontrolbase": {"entry": {"coordinator": coord, "client": client()}}}
    )
    registry = MagicMock()
    registered = {"aircontrolbase_group_entry_sala": "climate.sala"}
    registry.async_get_entity_id.side_effect = lambda domain, platform, unique_id: (
        registered.get(unique_id)
    )

    def migrate(entity_id, *, new_unique_id):
        registered.pop("aircontrolbase_group_entry_sala")
        registered[new_unique_id] = entity_id

    registry.async_update_entity.side_effect = migrate
    device_registry = MagicMock()
    old_device = SimpleNamespace(id="old-device", config_entries={"entry"})
    device_registry.async_get_device.side_effect = [old_device, None]
    add = MagicMock()
    with (
        patch(
            "custom_components.aircontrolbase.climate.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.aircontrolbase.climate.dr.async_get",
            return_value=device_registry,
        ),
    ):
        await setup_climate(hass, entry, add)
    registry.async_update_entity.assert_called_once_with(
        "climate.sala", new_unique_id="aircontrolbase_group_entry_a"
    )
    device_registry.async_update_device.assert_called_once_with(
        "old-device", new_identifiers={("aircontrolbase", "group_entry_a")}
    )
    assert len(add.call_args.args[0]) == 2
    coord.data["areas"][0]["name"] = "Sala renomeada"
    coord.async_add_listener.call_args.args[0]()
    assert add.call_count == 1

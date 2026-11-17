"""Config flow do AirControlBase."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import AirControlBaseClient, AirControlBaseError, AuthError
from .const import (
    CONF_ACCOUNT,
    CONF_GATEWAY_HOST,
    CONF_HYBRID,
    CONF_LOCAL,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEFAULT_GATEWAY_HOST,
    DOMAIN,
    OPT_GROUP_AREAS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_USER_ID_SCHEMA = vol.Schema({vol.Required(CONF_USER_ID): str})


class AirControlBaseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Fluxo de configuração via UI."""

    VERSION = 1

    def __init__(self) -> None:
        self._account: str | None = None
        self._password: str | None = None

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Atualiza a senha da conta existente sem criar entidades duplicadas."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_entry_missing")
        errors: dict[str, str] = {}
        if user_input is not None:
            client = AirControlBaseClient(
                entry.data[CONF_ACCOUNT],
                user_input[CONF_PASSWORD],
                entry.data.get(CONF_USER_ID),
            )
            try:
                await client.login()
                await client.get_details()
            except AuthError:
                errors["base"] = "invalid_auth"
            except AirControlBaseError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_USER_ID: client.user_id,
                    },
                )
            finally:
                await client.close()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AirControlBaseOptionsFlow:
        return AirControlBaseOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT]
            self._password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(self._account.lower())
            self._abort_if_unique_id_configured()

            client = AirControlBaseClient(self._account, self._password)
            try:
                await client.login()
            except AuthError as err:
                msg = str(err).lower()
                _LOGGER.warning("AirControlBase login falhou: %s", err)
                if "userid não encontrado" in msg or "userid nao encontrado" in msg:
                    return await self.async_step_userid()
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Erro inesperado conectando ao AirControlBase")
                errors["base"] = "cannot_connect"
            else:
                user_id = client.user_id
                return self.async_create_entry(
                    title=self._account,
                    data={
                        CONF_ACCOUNT: self._account,
                        CONF_PASSWORD: self._password,
                        CONF_USER_ID: user_id,
                    },
                )
            finally:
                await client.close()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_userid(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Fallback: pedir userId manualmente se não vier no login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_id = user_input[CONF_USER_ID].strip()
            client = AirControlBaseClient(
                self._account or "", self._password or "", user_id
            )
            try:
                await client.login()
                await client.get_details()
            except AuthError:
                errors["base"] = "invalid_user_id"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=self._account or user_id,
                    data={
                        CONF_ACCOUNT: self._account,
                        CONF_PASSWORD: self._password,
                        CONF_USER_ID: user_id,
                    },
                )
            finally:
                await client.close()

        return self.async_show_form(
            step_id="userid",
            data_schema=STEP_USER_ID_SCHEMA,
            errors=errors,
            description_placeholders={"account": self._account or ""},
        )


class AirControlBaseOptionsFlow(
    getattr(config_entries, "OptionsFlowWithReload", config_entries.OptionsFlow)
):
    """Opções: escolher quais áreas viram climate de grupo."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # áreas disponíveis vêm do coordinator
        bucket = (self.hass.data.get(DOMAIN) or {}).get(self._entry.entry_id) or {}
        coordinator = bucket.get("coordinator")
        if coordinator and coordinator.data:
            area_names = [
                a["name"] for a in coordinator.data.get("areas", []) if a.get("name")
            ]
        else:
            area_names = []

        # default: áreas com mais de 1 device
        if coordinator and coordinator.data:
            default = [
                a["name"]
                for a in coordinator.data.get("areas", [])
                if len(a.get("device_ids", [])) > 1
            ]
        else:
            default = []

        current = self._entry.options.get(OPT_GROUP_AREAS, default)
        area_names = list(dict.fromkeys([*area_names, *current]))

        schema = vol.Schema(
            {
                vol.Optional(OPT_GROUP_AREAS, default=current): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=area_names or [],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                        custom_value=False,
                    )
                ),
                vol.Optional(
                    CONF_HYBRID,
                    default=self._entry.options.get(
                        CONF_HYBRID, self._entry.data.get(CONF_HYBRID, False)
                    ),
                ): bool,
                vol.Optional(
                    CONF_LOCAL,
                    default=self._entry.options.get(
                        CONF_LOCAL, self._entry.data.get(CONF_LOCAL, False)
                    ),
                ): bool,
                vol.Optional(
                    CONF_GATEWAY_HOST,
                    default=self._entry.options.get(
                        CONF_GATEWAY_HOST,
                        self._entry.data.get(CONF_GATEWAY_HOST, DEFAULT_GATEWAY_HOST),
                    ),
                ): str,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

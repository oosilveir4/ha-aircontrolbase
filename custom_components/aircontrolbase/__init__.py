"""Integração AirControlBase (CCM21) para Home Assistant."""

from __future__ import annotations

import logging

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import AirControlBaseClient, AirControlBaseError, AuthError
from .const import (
    CONF_ACCOUNT,
    CONF_GATEWAY_HOST,
    CONF_HYBRID,
    CONF_LOCAL,
    CONF_PASSWORD,
    CONF_SERVER_PORT,
    CONF_USER_ID,
    DEFAULT_GATEWAY_HOST,
    DEFAULT_SERVER_PORT,
    DOMAIN,
)
from .coordinator import AirControlBaseCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.options.get(CONF_HYBRID, entry.data.get(CONF_HYBRID)):
        return await _async_setup_hybrid(hass, entry)
    if entry.options.get(CONF_LOCAL, entry.data.get(CONF_LOCAL)):
        return await _async_setup_local(hass, entry)
    account = entry.data[CONF_ACCOUNT]
    password = entry.data[CONF_PASSWORD]
    user_id = entry.data.get(CONF_USER_ID)

    client = AirControlBaseClient(account, password, user_id)
    try:
        await client.login()
        coordinator = AirControlBaseCoordinator(hass, client, entry)
        await coordinator.async_config_entry_first_refresh()
    except AuthError as err:
        await client.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except AirControlBaseError as err:
        await client.close()
        raise ConfigEntryNotReady(str(err)) from err
    except BaseException:
        await client.close()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await client.close()
        raise
    async_register_services(hass)

    # OptionsFlowWithReload foi adicionado após a versão mínima suportada.
    if not hasattr(config_entries, "OptionsFlowWithReload"):
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_setup_hybrid(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Híbrido: proxy MITM (estado local rápido) + comando/metadados pela nuvem."""
    from .hybrid import HybridCoordinator, HybridProxy

    account = entry.data[CONF_ACCOUNT]
    password = entry.data[CONF_PASSWORD]
    user_id = entry.data.get(CONF_USER_ID)
    port = entry.options.get(
        CONF_SERVER_PORT, entry.data.get(CONF_SERVER_PORT, DEFAULT_SERVER_PORT)
    )

    client = AirControlBaseClient(account, password, user_id)
    proxy = HybridProxy(listen_port=port)
    try:
        await client.login()
        await proxy.start()
        coordinator = HybridCoordinator(hass, client, proxy, entry)
        await coordinator.async_config_entry_first_refresh()
    except AuthError as err:
        await proxy.stop()
        await client.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except OSError as err:
        await proxy.stop()
        await client.close()
        raise ConfigEntryNotReady(f"Não consegui abrir a porta {port}: {err}") from err
    except AirControlBaseError as err:
        await proxy.stop()
        await client.close()
        raise ConfigEntryNotReady(str(err)) from err
    except BaseException:
        await proxy.stop()
        await client.close()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "hybrid_proxy": proxy,
    }
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await proxy.stop()
        await client.close()
        raise
    async_register_services(hass)
    return True


async def _async_setup_local(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Modo local: servidor TCP + leitura HTTP, sem nuvem."""
    # Import adiado para não carregar o servidor no caminho da nuvem.
    from .localmode import LocalCoordinator

    host = entry.options.get(
        CONF_GATEWAY_HOST, entry.data.get(CONF_GATEWAY_HOST, DEFAULT_GATEWAY_HOST)
    )
    port = entry.options.get(
        CONF_SERVER_PORT, entry.data.get(CONF_SERVER_PORT, DEFAULT_SERVER_PORT)
    )
    coordinator = LocalCoordinator(hass, host, port, entry=entry)
    try:
        await coordinator.async_start()
        await coordinator.async_config_entry_first_refresh()
    except OSError as err:
        await coordinator.async_stop()
        raise ConfigEntryNotReady(f"Não consegui abrir a porta {port}: {err}") from err
    except BaseException:
        await coordinator.async_stop()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": coordinator.controller,
        "coordinator": coordinator,
        "local": True,
    }
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await coordinator.async_stop()
        raise
    async_register_services(hass)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data:
            if data.get("local"):
                await data["coordinator"].async_stop()
            else:
                if data.get("hybrid_proxy"):
                    await data["hybrid_proxy"].stop()
                client: AirControlBaseClient = data["client"]
                await client.close()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
            async_unregister_services(hass)
    return unloaded

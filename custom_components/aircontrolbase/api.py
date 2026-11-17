"""Cliente HTTP async para AirControlBase (CCM21)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError

from .const import (
    BASE_URL,
    CODE_SESSION_EXPIRED,
    DEFAULT_TIMEOUT,
    EP_CONTROL,
    EP_GET_DETAILS,
    EP_LOGIN,
)

_LOGGER = logging.getLogger(__name__)
_COMMAND_INTERVAL = 0.8
_PENDING_COMMAND_TTL = 40


class AirControlBaseError(HomeAssistantError):
    """Erro genérico do AirControlBase."""


class AuthError(AirControlBaseError):
    """Falha de autenticação (credenciais inválidas ou usuário sem userId)."""


class SessionExpired(AirControlBaseError):
    """Sessão expirou (code 40018) — interno, usado pra trigger re-login."""


class AirControlBaseClient:
    """Cliente da web API do AirControlBase.

    Mantém um ClientSession próprio (com cookie jar) para guardar o JSESSIONID.
    Em qualquer 40018 faz re-login automático e retenta UMA vez.
    """

    def __init__(self, account: str, password: str, user_id: str | None = None) -> None:
        self._account = account
        self._password = password
        self._user_id = user_id
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._last_command: float | None = None
        self._pending: dict[str, dict[str, tuple[Any, float]]] = {}
        self._logged_in = False
        self._closed = False

    # ── ciclo de vida ─────────────────────────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise AirControlBaseError("Cliente AirControlBase já foi encerrado")
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
            self._session = aiohttp.ClientSession(cookie_jar=jar, timeout=timeout)
        return self._session

    async def close(self) -> None:
        self._closed = True
        async with self._command_lock, self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            self._logged_in = False
            self._pending.clear()

    @property
    def user_id(self) -> str | None:
        return self._user_id

    # ── auth ──────────────────────────────────────────────────────────────────

    async def login(self) -> dict[str, Any]:
        """Faz login. Salva cookies no jar. Tenta extrair userId da resposta."""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/login.html",
        }
        data = {
            "account": self._account,
            "password": self._password,
            "from": "web",
        }
        payload = await self._request(EP_LOGIN, headers, data)

        code = payload.get("code")
        if code is not None and str(code) not in ("0", "200"):
            raise AuthError(
                f"Login falhou: code={code} msg={payload.get('msg') or payload.get('message')}"
            )

        # tenta achar o userId na resposta — caminhos comuns
        result = (
            payload.get("result") if isinstance(payload.get("result"), dict) else {}
        )
        data_field = (
            payload.get("data") if isinstance(payload.get("data"), dict) else {}
        )
        extracted = (
            payload.get("userId")
            or payload.get("uid")
            or payload.get("user_id")
            or result.get("userId")
            or result.get("uid")
            or result.get("id")
            or data_field.get("userId")
            or data_field.get("uid")
            or data_field.get("id")
        )
        if extracted:
            self._user_id = str(extracted)

        if not self._user_id:
            raise AuthError(
                "Login OK mas userId não encontrado na resposta — informe manualmente."
            )

        self._logged_in = True
        return payload

    # ── helper de POST com retry em sessão expirada ───────────────────────────

    async def _post(
        self, endpoint: str, data: dict[str, str], referer: str = ""
    ) -> dict[str, Any]:
        """POST form-urlencoded. Em code=40018 faz re-login + retry 1x."""
        async with self._lock:
            if self._closed:
                raise AirControlBaseError("Cliente AirControlBase já foi encerrado")
            return await self._post_locked(endpoint, data, referer)

    async def _request(
        self, endpoint: str, headers: dict[str, str], data: dict[str, str]
    ) -> dict[str, Any]:
        session = await self._ensure_session()
        try:
            async with session.post(
                BASE_URL + endpoint, headers=headers, data=data
            ) as resp:
                if resp.status in (401, 403):
                    raise AuthError(f"POST {endpoint} HTTP {resp.status}")
                if resp.status != 200:
                    raise AirControlBaseError(f"POST {endpoint} HTTP {resp.status}")
                payload = json.loads(await resp.text())
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AirControlBaseError(f"Falha de conexão em {endpoint}") from err
        except json.JSONDecodeError as err:
            raise AirControlBaseError(f"Resposta não-JSON em {endpoint}") from err
        if not isinstance(payload, dict):
            raise AirControlBaseError(f"Resposta inesperada em {endpoint}")
        return payload

    async def _post_locked(
        self, endpoint: str, data: dict[str, str], referer: str
    ) -> dict[str, Any]:
        if not self._logged_in:
            await self.login()
        data = {**data, "userId": self._user_id or ""}

        for attempt in (1, 2):
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE_URL,
                "Referer": referer or f"{BASE_URL}/devicecontrol.html",
            }
            data["userId"] = self._user_id or ""
            payload = await self._request(endpoint, headers, data)
            code = payload.get("code")
            if str(code) == str(CODE_SESSION_EXPIRED):
                self._logged_in = False
                if attempt == 2:
                    raise SessionExpired("Sessão expirada após re-login")
                _LOGGER.debug("Sessão expirada em %s — refazendo login", endpoint)
                await self.login()
                continue
            if code is not None and str(code) not in ("0", "200"):
                raise AirControlBaseError(f"POST {endpoint} falhou: code={code}")
            return payload

        raise AirControlBaseError(f"POST {endpoint}: falha após retry")

    # ── endpoints públicos ────────────────────────────────────────────────────

    async def get_details(self) -> dict[str, Any]:
        """Estado de todos os ACs agrupados por área + deviceCase (resumo)."""
        payload = await self._post(EP_GET_DETAILS, {"userId": self._user_id or ""})
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("areas"), list):
            raise AirControlBaseError(
                "getDetails não retornou uma lista de áreas válida"
            )
        return payload

    async def control_device(
        self,
        *,
        device_id: int | str,
        current: dict[str, Any],
        changes: dict[str, Any],
        lock_token: str | None = None,
        locked: bool = False,
    ) -> dict[str, Any]:
        """Serializa comandos de todas as entidades e preserva alterações recentes."""
        async with self._command_lock:
            if self._closed:
                raise AirControlBaseError("Cliente AirControlBase já foi encerrado")
            loop = asyncio.get_running_loop()
            if self._last_command is not None:
                await asyncio.sleep(
                    max(0, _COMMAND_INTERVAL - (loop.time() - self._last_command))
                )
            now = loop.time()
            self._pending = {
                dev: active
                for dev, fields in self._pending.items()
                if (active := {k: item for k, item in fields.items() if item[1] > now})
            }
            pending = self._pending.setdefault(str(device_id), {})
            state = {
                **current,
                **{k: v for k, (v, until) in pending.items() if until > now},
            }
            changes = dict(changes)
            if lock_token is not None:
                value = state.get("unlock")
                tokens = set(
                    (
                        value if value is not None else "mode,cool,heat,wind,remote"
                    ).split(",")
                )
                tokens.discard("")
                if locked:
                    tokens.discard(lock_token)
                else:
                    tokens.add(lock_token)
                order = ("mode", "cool", "heat", "wind", "remote")
                changes["unlock"] = ",".join(
                    [t for t in order if t in tokens] + sorted(tokens.difference(order))
                )
            if changes.get("power") == "y" and state.get("wind") in (None, "", "off"):
                changes.setdefault("wind", "auto")
            try:
                response = await self._control_device(
                    device_id=device_id, current=state, changes=changes
                )
            finally:
                self._last_command = loop.time()
            for key, value in changes.items():
                pending[key] = (value, loop.time() + _PENDING_COMMAND_TTL)
            return response

    async def _control_device(
        self, *, device_id: int | str, current: dict[str, Any], changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Envia comando ao device. `current` é o estado espelhado completo,
        `changes` é só o que mudou (operation)."""
        control = {
            "id": int(device_id),
            "power": current.get("power", "n"),
            "mode": current.get("mode", "cool"),
            "setTemp": current.get("setTemp")
            if current.get("setTemp") is not None
            else 24,
            "wind": current.get("wind", "auto"),
            "swing": current.get("swing", "n"),
            "lock": current.get("lock", ""),
            "factTemp": current.get("factTemp")
            if current.get("factTemp") is not None
            else 0,
            "modeLockValue": current.get("modeLockValue", ""),
            "coolLockValue": current.get("coolLockValue", ""),
            "heatLockValue": current.get("heatLockValue", ""),
            "windLockValue": current.get("windLockValue", ""),
            "unlock": current.get("unlock", "mode,cool,heat,wind,remote"),
        }
        control.update(changes)
        data = {
            "userId": self._user_id or "",
            "control": json.dumps(control, separators=(",", ":")),
            "operation": json.dumps(changes, separators=(",", ":")),
            "type": "control",
        }
        return await self._post(EP_CONTROL, data)

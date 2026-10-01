"""Config flow for Codex Usage."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    CodexAuthError,
    CodexUsageError,
    OAuthTokens,
    build_authorize_url,
    exchange_code,
    fetch_plan_usage,
    generate_pkce,
    parse_redirect,
    tokens_from_auth_json,
    validate_admin_key,
)
from .const import (
    AUTH_ADMIN,
    AUTH_IMPORT,
    AUTH_SUBSCRIPTION,
    CONF_ADMIN_KEY,
    CONF_AUTH_TYPE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_ADMIN,
    DEFAULT_SCAN_SUBSCRIPTION,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    OAUTH_REDIRECT_URI,
)
from .coordinator import tokens_to_data

_LOGGER = logging.getLogger(__name__)

CONF_REDIRECT_URL = "redirect_url"
CONF_AUTH_JSON = "auth_json"


class CodexUsageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._verifier: str | None = None
        self._state: str | None = None
        self._auth_url: str | None = None

    def _new_pkce(self) -> None:
        self._verifier, challenge = generate_pkce()
        self._state = secrets.token_urlsafe(24)
        self._auth_url = build_authorize_url(challenge, self._state)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a mode."""
        return self.async_show_menu(
            step_id="user", menu_options=[AUTH_SUBSCRIPTION, AUTH_IMPORT, AUTH_ADMIN]
        )

    async def _create_from_tokens(self, tokens: OAuthTokens, auth_type: str) -> ConfigFlowResult:
        data = {CONF_AUTH_TYPE: auth_type, **tokens_to_data(tokens)}
        unique = f"plan_{tokens.account_id or tokens.email or tokens.refresh_token[-12:]}"
        title = f"Codex {tokens.email or tokens.account_id or ''}".strip()
        return await self._finish(unique, title, data)

    async def _verify(self, tokens: OAuthTokens) -> str | None:
        """Return an error key, or None if the usage endpoint answers."""
        try:
            await fetch_plan_usage(
                async_get_clientsession(self.hass), tokens.access_token, tokens.account_id
            )
        except CodexAuthError as err:
            _LOGGER.warning("Codex usage check failed: %s", err)
            return "invalid_auth"
        except CodexUsageError as err:
            _LOGGER.warning("Codex usage check failed: %s", err)
            return "cannot_connect"
        return None

    # ---------------------------------------------------------- subscription
    async def async_step_subscription(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """ChatGPT sign-in; user pastes the localhost redirect URL."""
        errors: dict[str, str] = {}
        if self._verifier is None:
            self._new_pkce()
        if user_input is not None:
            code, state = parse_redirect(user_input[CONF_REDIRECT_URL])
            if state and state != self._state:
                errors["base"] = "state_mismatch"
            elif not code:
                errors["base"] = "invalid_code"
            else:
                try:
                    tokens = await exchange_code(
                        async_get_clientsession(self.hass), code, self._verifier or ""
                    )
                except CodexAuthError as err:
                    _LOGGER.warning("Codex login failed: %s", err)
                    errors["base"] = "invalid_auth"
                except CodexUsageError:
                    errors["base"] = "cannot_connect"
                else:
                    if not (error := await self._verify(tokens)):
                        return await self._create_from_tokens(tokens, AUTH_SUBSCRIPTION)
                    errors["base"] = error
            self._new_pkce()
        return self.async_show_form(
            step_id="subscription",
            data_schema=vol.Schema({vol.Required(CONF_REDIRECT_URL): str}),
            description_placeholders={
                "auth_url": self._auth_url or "",
                "redirect_uri": OAUTH_REDIRECT_URI,
            },
            errors=errors,
        )

    # ---------------------------------------------------------------- import
    async def async_step_import_auth_json(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste the contents of ~/.codex/auth.json."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens = tokens_from_auth_json(user_input[CONF_AUTH_JSON])
            except CodexAuthError:
                errors["base"] = "invalid_auth_json"
            else:
                if not (error := await self._verify(tokens)):
                    return await self._create_from_tokens(tokens, AUTH_IMPORT)
                errors["base"] = error
        return self.async_show_form(
            step_id="import_auth_json",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_JSON): TextSelector(
                        TextSelectorConfig(multiline=True)
                    )
                }
            ),
            errors=errors,
        )

    # ----------------------------------------------------------------- admin
    async def async_step_admin_api(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """OpenAI Admin API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_ADMIN_KEY].strip()
            try:
                await validate_admin_key(async_get_clientsession(self.hass), key)
            except CodexAuthError:
                errors["base"] = "invalid_auth"
            except CodexUsageError as err:
                _LOGGER.warning("OpenAI Admin API validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                unique = f"admin_{hashlib.sha256(key.encode()).hexdigest()[:16]}"
                return await self._finish(
                    unique, "OpenAI API", {CONF_AUTH_TYPE: AUTH_ADMIN, CONF_ADMIN_KEY: key}
                )
        return self.async_show_form(
            step_id="admin_api",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADMIN_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    # ---------------------------------------------------------------- shared
    async def _finish(self, unique_id: str, title: str, data: dict[str, Any]) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=title, data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials expired or were revoked."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Route to the right login step."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        auth_type = self._get_reauth_entry().data.get(CONF_AUTH_TYPE)
        if auth_type == AUTH_ADMIN:
            return await self.async_step_admin_api()
        if auth_type == AUTH_IMPORT:
            return await self.async_step_import_auth_json()
        return await self.async_step_subscription()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return CodexUsageOptionsFlow()


class CodexUsageOptionsFlow(OptionsFlow):
    """Polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        default = (
            DEFAULT_SCAN_ADMIN
            if self.config_entry.data.get(CONF_AUTH_TYPE) == AUTH_ADMIN
            else DEFAULT_SCAN_SUBSCRIPTION
        )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, int(default.total_seconds()))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=86400,
                            step=30,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )

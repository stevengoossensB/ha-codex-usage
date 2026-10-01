"""Data update coordinator for Codex Usage."""

from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AdminUsage,
    CodexAuthError,
    CodexRateLimitError,
    CodexUsageError,
    OAuthTokens,
    PlanUsage,
    fetch_admin_usage,
    fetch_plan_usage,
    refresh_tokens,
)
from .const import (
    AUTH_ADMIN,
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_ADMIN_KEY,
    CONF_AUTH_TYPE,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_ADMIN,
    DEFAULT_SCAN_SUBSCRIPTION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

type CodexUsageConfigEntry = ConfigEntry[CodexUsageCoordinator]


def tokens_to_data(tokens: OAuthTokens) -> dict[str, Any]:
    """Serialize tokens into config entry data."""
    return {
        CONF_ACCESS_TOKEN: tokens.access_token,
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_ID_TOKEN: tokens.id_token,
        CONF_ACCOUNT_ID: tokens.account_id,
        CONF_EXPIRES_AT: tokens.expires_at,
    }


class CodexUsageCoordinator(DataUpdateCoordinator[PlanUsage | AdminUsage]):
    """Polls wham/usage or the OpenAI Admin API."""

    config_entry: CodexUsageConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CodexUsageConfigEntry) -> None:
        self.auth_type: str = entry.data[CONF_AUTH_TYPE]
        default = DEFAULT_SCAN_ADMIN if self.auth_type == AUTH_ADMIN else DEFAULT_SCAN_SUBSCRIPTION
        seconds = entry.options.get(CONF_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=seconds) if seconds else default,
        )
        self._session = async_get_clientsession(hass)
        self.options_snapshot = dict(entry.options)

    async def _async_update_data(self) -> PlanUsage | AdminUsage:
        try:
            if self.auth_type == AUTH_ADMIN:
                return await fetch_admin_usage(self._session, self.config_entry.data[CONF_ADMIN_KEY])
            return await self._update_plan()
        except CodexAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CodexRateLimitError as err:
            raise UpdateFailed(f"Rate limited, will retry: {err}") from err
        except CodexUsageError as err:
            raise UpdateFailed(str(err)) from err

    def _tokens(self) -> OAuthTokens:
        d = self.config_entry.data
        return OAuthTokens(
            access_token=d[CONF_ACCESS_TOKEN],
            refresh_token=d.get(CONF_REFRESH_TOKEN, ""),
            id_token=d.get(CONF_ID_TOKEN),
            expires_at=float(d.get(CONF_EXPIRES_AT, 0)),
            account_id=d.get(CONF_ACCOUNT_ID),
            email=None,
            plan=None,
        )

    async def _update_plan(self) -> PlanUsage:
        if self._tokens().expires_at - 300 < time.time():
            await self._refresh()
        tokens = self._tokens()
        try:
            return await fetch_plan_usage(self._session, tokens.access_token, tokens.account_id)
        except CodexAuthError:
            await self._refresh()
            tokens = self._tokens()
            return await fetch_plan_usage(self._session, tokens.access_token, tokens.account_id)

    async def _refresh(self) -> None:
        _LOGGER.debug("Refreshing Codex OAuth token")
        new = await refresh_tokens(self._session, self._tokens())
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, **tokens_to_data(new)}
        )

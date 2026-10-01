"""Tests for the Codex Usage integration."""

from __future__ import annotations

import base64
import json
import time

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.codex_usage.api import parse_plan_usage, parse_redirect
from custom_components.codex_usage.const import ADMIN_BASE_URL, DOMAIN, OAUTH_TOKEN_URL, USAGE_URL


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{body}.sig"


ID_TOKEN = _jwt(
    {
        "email": "me@example.com",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "plus"},
    }
)

USAGE = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 74, "limit_window_seconds": 18000, "reset_after_seconds": 6198, "reset_at": 1789801452},
        "secondary_window": {"used_percent": 27, "limit_window_seconds": 604800, "reset_after_seconds": 11991, "reset_at": 1789807245},
    },
    "code_review_rate_limit": {
        "primary_window": {"used_percent": 5, "limit_window_seconds": 604800, "reset_at": 1789807245}
    },
    "additional_rate_limits": [
        {
            "limit_name": "GPT-5.3-Codex-Spark",
            "rate_limit": {"primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_at": 1789801452}},
        }
    ],
    "credits": {"has_credits": True, "unlimited": False, "balance": "42.5"},
}


def test_parse() -> None:
    p = parse_plan_usage(USAGE)
    assert p.windows["primary"].name == "5h"
    assert p.windows["secondary"].name == "Weekly"
    assert p.windows["code_review_primary"].name == "Code review weekly"
    assert p.windows["gpt_5_3_codex_spark_primary"].used_percent == 12
    # Flat (no rate_limit wrapper) and resets_at variants
    flat = parse_plan_usage({"primary_window": {"used_percent": 1, "limit_window_seconds": 18000, "resets_at": "2026-01-01T00:00:00Z"}})
    assert flat.windows["primary"].resets_at.year == 2026


def test_parse_redirect() -> None:
    assert parse_redirect("http://localhost:1455/auth/callback?code=abc&scope=x&state=st") == ("abc", "st")


async def test_login_flow_and_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    access = _jwt({"exp": time.time() + 86400})
    aioclient_mock.post(OAUTH_TOKEN_URL, json={"access_token": access, "refresh_token": "rt", "id_token": ID_TOKEN})
    aioclient_mock.get(USAGE_URL, json=USAGE)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "subscription"})
    assert "auth.openai.com/oauth/authorize" in result["description_placeholders"]["auth_url"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect_url": "http://localhost:1455/auth/callback?code=abc"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"]["account_id"] == "acct-1"
    await hass.async_block_till_done()
    # account header was sent
    assert aioclient_mock.mock_calls[-1][3]["ChatGPT-Account-Id"] == "acct-1"
    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    assert states["sensor.codex_me_example_com_5h_usage"] == "74.0"
    assert states["sensor.codex_me_example_com_weekly_usage"] == "27.0"
    assert states["sensor.codex_me_example_com_code_review_weekly_usage"] == "5.0"
    assert states["sensor.codex_me_example_com_gpt_5_3_codex_spark_5h_usage"] == "12.0"
    assert states["sensor.codex_me_example_com_plan"] == "plus"
    assert states["sensor.codex_me_example_com_credits_balance"] == "42.5"


async def test_import_and_refresh(hass: HomeAssistant, aioclient_mock) -> None:
    expired = _jwt({"exp": time.time() - 10})
    fresh = _jwt({"exp": time.time() + 86400})
    aioclient_mock.post(OAUTH_TOKEN_URL, json={"access_token": fresh, "refresh_token": "rt2"})
    aioclient_mock.get(USAGE_URL, json=USAGE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="plan_acct-1",
        data={"auth_type": "import_auth_json", "access_token": expired, "refresh_token": "rt1",
              "id_token": ID_TOKEN, "account_id": "acct-1", "expires_at": time.time() - 10},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data["refresh_token"] == "rt2"
    assert entry.data["access_token"] == fresh
    body = aioclient_mock.mock_calls[0][2]
    assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "rt1"


async def test_import_auth_json_step(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(USAGE_URL, json=USAGE)
    auth_json = json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": _jwt({"exp": time.time() + 999}),
                            "refresh_token": "r", "id_token": ID_TOKEN, "account_id": "acct-1"}})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "import_auth_json"})
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"auth_json": "{}"})
    assert bad["errors"] == {"base": "invalid_auth_json"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"auth_json": auth_json})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Codex me@example.com"


async def test_admin(hass: HomeAssistant, aioclient_mock) -> None:
    now = int(time.time())
    aioclient_mock.get(
        f"{ADMIN_BASE_URL}/costs",
        json={"data": [{"start_time": now - 60, "results": [{"amount": {"value": 1.25, "currency": "usd"}}]}], "has_more": False},
    )
    aioclient_mock.get(
        f"{ADMIN_BASE_URL}/usage/completions",
        json={"data": [{"start_time": now - 60, "results": [{"input_tokens": 1000, "input_cached_tokens": 200,
              "output_tokens": 300, "num_model_requests": 7}]}], "has_more": False},
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "admin_api"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"admin_key": "sk-admin-x"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    assert states["sensor.openai_api_total_tokens_today"] == "1300"
    assert states["sensor.openai_api_input_tokens_this_month"] == "800"
    assert states["sensor.openai_api_requests_this_month"] == "7"
    assert states["sensor.openai_api_cost_today"] == "1.25"

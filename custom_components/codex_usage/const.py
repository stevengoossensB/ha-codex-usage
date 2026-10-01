"""Constants for the Codex Usage integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "codex_usage"

CONF_AUTH_TYPE: Final = "auth_type"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_ID_TOKEN: Final = "id_token"
CONF_ACCOUNT_ID: Final = "account_id"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_ADMIN_KEY: Final = "admin_key"
CONF_SCAN_INTERVAL: Final = "scan_interval"

AUTH_SUBSCRIPTION: Final = "subscription"
AUTH_IMPORT: Final = "import_auth_json"
AUTH_ADMIN: Final = "admin_api"

DEFAULT_SCAN_SUBSCRIPTION: Final = timedelta(minutes=5)
DEFAULT_SCAN_ADMIN: Final = timedelta(minutes=30)
MIN_SCAN_INTERVAL: Final = 60

# OAuth (same public client the Codex CLI uses).
OAUTH_CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_AUTHORIZE_URL: Final = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL: Final = "https://auth.openai.com/oauth/token"
OAUTH_REDIRECT_URI: Final = "http://localhost:1455/auth/callback"
OAUTH_SCOPES: Final = "openid profile email offline_access"

USAGE_URL: Final = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT: Final = "codex_cli_rs/0.50.0 (ha-codex-usage)"
ORIGINATOR: Final = "codex_cli_rs"

# OpenAI Admin API
ADMIN_BASE_URL: Final = "https://api.openai.com/v1/organization"

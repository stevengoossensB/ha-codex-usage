"""Small async client for Codex (ChatGPT plan) limits and the OpenAI Admin API."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    ADMIN_BASE_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
    ORIGINATOR,
    USAGE_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=60)


class CodexUsageError(Exception):
    """Generic error."""


class CodexAuthError(CodexUsageError):
    """Credentials invalid or expired and cannot be refreshed."""


class CodexRateLimitError(CodexUsageError):
    """Asked to slow down."""


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge)."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorize_url(challenge: str, state: str) -> str:
    """Authorize URL identical to the one `codex login` opens."""
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": ORIGINATOR,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def parse_redirect(pasted: str) -> tuple[str, str | None]:
    """Accept the full localhost redirect URL, or just the code."""
    pasted = pasted.strip()
    if "code=" in pasted:
        query = parse_qs(urlparse(pasted).query)
        return query.get("code", [""])[0], query.get("state", [None])[0]
    return pasted, None


def decode_jwt(token: str | None) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (only used for metadata)."""
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


@dataclass
class OAuthTokens:
    """Token set."""

    access_token: str
    refresh_token: str
    id_token: str | None
    expires_at: float
    account_id: str | None
    email: str | None
    plan: str | None


def tokens_from_dict(data: dict[str, Any], old: OAuthTokens | None = None) -> OAuthTokens:
    """Build tokens from an OAuth response or the `tokens` block of auth.json."""
    access = data.get("access_token")
    if not access:
        raise CodexAuthError("No access_token in response")
    id_token = data.get("id_token") or (old.id_token if old else None)
    id_claims = decode_jwt(id_token)
    at_claims = decode_jwt(access)
    auth_claims = id_claims.get("https://api.openai.com/auth") or at_claims.get(
        "https://api.openai.com/auth", {}
    )
    profile = at_claims.get("https://api.openai.com/profile") or {}
    exp = at_claims.get("exp")
    if exp is None and data.get("expires_in"):
        exp = time.time() + float(data["expires_in"])
    return OAuthTokens(
        access_token=access,
        refresh_token=data.get("refresh_token") or (old.refresh_token if old else ""),
        id_token=id_token,
        expires_at=float(exp or time.time() + 3600),
        account_id=data.get("account_id")
        or auth_claims.get("chatgpt_account_id")
        or (old.account_id if old else None),
        email=id_claims.get("email") or profile.get("email") or (old.email if old else None),
        plan=auth_claims.get("chatgpt_plan_type"),
    )


async def _token_request(
    session: aiohttp.ClientSession, *, json_body: dict | None = None, form: dict | None = None
) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "originator": ORIGINATOR}
    try:
        async with session.post(
            OAUTH_TOKEN_URL, headers=headers, json=json_body, data=form, timeout=TIMEOUT
        ) as resp:
            text = await resp.text()
            if resp.status == 200:
                return json.loads(text)
            if resp.status == 429:
                raise CodexRateLimitError("Token endpoint rate limited")
            if 400 <= resp.status < 500:
                raise CodexAuthError(f"HTTP {resp.status}: {text[:300]}")
            raise CodexUsageError(f"HTTP {resp.status}: {text[:300]}")
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as err:
        raise CodexUsageError(f"Token request failed: {err}") from err


async def exchange_code(session: aiohttp.ClientSession, code: str, verifier: str) -> OAuthTokens:
    """Exchange the authorization code."""
    data = await _token_request(
        session,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
            "code_verifier": verifier,
        },
    )
    return tokens_from_dict(data)


async def refresh_tokens(session: aiohttp.ClientSession, old: OAuthTokens) -> OAuthTokens:
    """Refresh (refresh tokens are single-use and rotate)."""
    data = await _token_request(
        session,
        json_body={
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": old.refresh_token,
            "scope": "openid profile email",
        },
    )
    return tokens_from_dict(data, old)


def tokens_from_auth_json(text: str) -> OAuthTokens:
    """Parse a pasted ~/.codex/auth.json."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise CodexAuthError("Not valid JSON") from err
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        raise CodexAuthError("No `tokens` block (is this an API-key login?)")
    return tokens_from_dict(tokens)


# --------------------------------------------------------------------------- #
# Plan limits (wham/usage)
# --------------------------------------------------------------------------- #


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")


def window_label(seconds: int | None) -> str:
    """Human label for a window length."""
    if not seconds:
        return "Limit"
    if seconds == 604800:
        return "Weekly"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{round(seconds / 60)}min"


@dataclass
class UsageWindow:
    """A single rate-limit window."""

    key: str
    name: str
    used_percent: float | None
    resets_at: datetime | None
    window_seconds: int | None


@dataclass
class PlanUsage:
    """Parsed wham/usage response."""

    plan_type: str | None
    windows: dict[str, UsageWindow]
    credits: dict[str, Any] | None
    status: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)


def _parse_window(prefix: str, label_prefix: str, slot: str, win: Any) -> UsageWindow | None:
    if not isinstance(win, dict):
        return None
    seconds = win.get("limit_window_seconds")
    reset = win.get("reset_at", win.get("resets_at"))
    reset_dt: datetime | None = None
    if isinstance(reset, (int, float)):
        reset_dt = datetime.fromtimestamp(reset, tz=UTC)
    elif isinstance(reset, str):
        try:
            reset_dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        except ValueError:
            reset_dt = None
    if reset_dt is None and win.get("reset_after_seconds") is not None:
        reset_dt = datetime.fromtimestamp(time.time() + float(win["reset_after_seconds"]), tz=UTC)
    used = win.get("used_percent")
    label = window_label(int(seconds) if seconds else None)
    name = f"{label_prefix} {label.lower()}" if label_prefix else label
    return UsageWindow(
        key=f"{prefix}{slot}",
        name=name,
        used_percent=float(used) if used is not None else None,
        resets_at=reset_dt,
        window_seconds=int(seconds) if seconds else None,
    )


def _collect(windows: dict[str, UsageWindow], block: Any, prefix: str, label_prefix: str) -> None:
    if not isinstance(block, dict):
        return
    for slot in ("primary_window", "secondary_window"):
        win = _parse_window(prefix, label_prefix, slot.split("_")[0], block.get(slot))
        if win:
            windows[win.key] = win


def parse_plan_usage(data: dict[str, Any]) -> PlanUsage:
    """Parse the payload; tolerant to fields moving around."""
    windows: dict[str, UsageWindow] = {}
    main = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else data
    _collect(windows, main, "", "")
    _collect(windows, data.get("code_review_rate_limit"), "code_review_", "Code review")
    for extra in data.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        name = extra.get("limit_name") or extra.get("metered_feature") or "Extra"
        _collect(windows, extra.get("rate_limit") or extra, f"{_slug(name)}_", name)

    status = {
        "allowed": main.get("allowed"),
        "limit_reached": main.get("limit_reached"),
        "rate_limit_reached_type": data.get("rate_limit_reached_type"),
        "email": data.get("email"),
    }
    credits = data.get("credits")
    return PlanUsage(
        plan_type=data.get("plan_type"),
        windows=windows,
        credits=credits if isinstance(credits, dict) else None,
        status=status,
        raw=data,
    )


async def fetch_plan_usage(
    session: aiohttp.ClientSession, access_token: str, account_id: str | None
) -> PlanUsage:
    """GET wham/usage. The account header matters for workspace-scoped numbers."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": USER_AGENT,
        "originator": ORIGINATOR,
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        async with session.get(USAGE_URL, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status in (401, 403):
                raise CodexAuthError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            if resp.status == 429:
                raise CodexRateLimitError("Usage endpoint rate limited")
            if resp.status != 200:
                raise CodexUsageError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CodexUsageError(f"Usage request failed: {err}") from err
    return parse_plan_usage(data)


# --------------------------------------------------------------------------- #
# OpenAI Admin API
# --------------------------------------------------------------------------- #

TOKEN_FIELDS = ("input", "output", "cached_input")


@dataclass
class AdminUsage:
    """Month-to-date and today's API usage."""

    month_start: datetime
    day_start: datetime
    tokens_month: dict[str, int]
    tokens_today: dict[str, int]
    requests_month: int
    requests_today: int
    cost_month: float | None
    cost_today: float | None


async def _admin_get_all(
    session: aiohttp.ClientSession, key: str, path: str, params: dict[str, str]
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    buckets: list[dict[str, Any]] = []
    page: str | None = None
    for _ in range(20):
        query = dict(params)
        if page:
            query["page"] = page
        try:
            async with session.get(
                f"{ADMIN_BASE_URL}/{path}", headers=headers, params=query, timeout=TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise CodexAuthError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
                if resp.status == 429:
                    raise CodexRateLimitError("Admin API rate limited")
                if resp.status != 200:
                    raise CodexUsageError(f"{path} HTTP {resp.status}: {(await resp.text())[:200]}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CodexUsageError(f"Admin API request failed: {err}") from err
        buckets.extend(data.get("data") or [])
        if not data.get("has_more") or not data.get("next_page"):
            break
        page = data["next_page"]
    return buckets


async def validate_admin_key(session: aiohttp.ClientSession, key: str) -> None:
    """Raise if the key cannot read organisation costs."""
    await _admin_get_all(
        session, key, "costs", {"start_time": str(int(time.time()) - 86400), "limit": "1"}
    )


async def fetch_admin_usage(session: aiohttp.ClientSession, key: str) -> AdminUsage:
    """Completions usage + costs for the current UTC month."""
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = str(int(month_start.timestamp()))
    day_ts = day_start.timestamp()

    usage = await _admin_get_all(
        session,
        key,
        "usage/completions",
        {"start_time": start, "bucket_width": "1d", "limit": "31"},
    )
    tokens_month = dict.fromkeys(TOKEN_FIELDS, 0)
    tokens_today = dict.fromkeys(TOKEN_FIELDS, 0)
    req_month = req_today = 0
    for bucket in usage:
        is_today = float(bucket.get("start_time") or 0) >= day_ts
        for res in bucket.get("results") or []:
            vals = {
                "input": int(res.get("input_tokens") or 0)
                - int(res.get("input_cached_tokens") or 0),
                "output": int(res.get("output_tokens") or 0),
                "cached_input": int(res.get("input_cached_tokens") or 0),
            }
            reqs = int(res.get("num_model_requests") or 0)
            for k, v in vals.items():
                tokens_month[k] += v
                if is_today:
                    tokens_today[k] += v
            req_month += reqs
            if is_today:
                req_today += reqs

    cost_month: float | None = None
    cost_today: float | None = None
    try:
        costs = await _admin_get_all(
            session, key, "costs", {"start_time": start, "bucket_width": "1d", "limit": "31"}
        )
    except CodexAuthError:
        raise
    except CodexUsageError as err:
        _LOGGER.debug("Costs unavailable: %s", err)
    else:
        cost_month = cost_today = 0.0
        for bucket in costs:
            is_today = float(bucket.get("start_time") or 0) >= day_ts
            for res in bucket.get("results") or []:
                value = float(((res.get("amount") or {}).get("value")) or 0)
                cost_month += value
                if is_today:
                    cost_today += value

    return AdminUsage(
        month_start=month_start,
        day_start=day_start,
        tokens_month=tokens_month,
        tokens_today=tokens_today,
        requests_month=req_month,
        requests_today=req_today,
        cost_month=round(cost_month, 4) if cost_month is not None else None,
        cost_today=round(cost_today, 4) if cost_today is not None else None,
    )

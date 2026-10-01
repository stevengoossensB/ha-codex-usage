# Codex Usage for Home Assistant

[![Maintainer](https://img.shields.io/badge/maintainer-stevengoossensB-green?style=for-the-badge&logo=github)](https://github.com/stevengoossensB)

[![MIT License](https://img.shields.io/github/license/stevengoossensB/ha-codex-usage?style=flat-square)](https://github.com/stevengoossensB/ha-codex-usage/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square)](https://github.com/hacs/integration)

[![GitHub Issues](https://img.shields.io/github/issues/stevengoossensB/ha-codex-usage)](https://github.com/stevengoossensB/ha-codex-usage/issues)
[![PRs Welcome](https://img.shields.io/badge/PRs-Welcome-brightgreen.svg)](https://github.com/stevengoossensB/ha-codex-usage/pulls)


[![Validation Status](https://github.com/stevengoossensB/ha-codex-usage/actions/workflows/validate.yml/badge.svg)](https://github.com/stevengoossensB/ha-codex-usage/actions/workflows/validate.yml)
[![Python](https://img.shields.io/badge/Python-FFD43B?logo=python)](https://github.com/stevengoossensB/ha-codex-usage/search?l=python)
[![Latest Release](https://img.shields.io/github/v/release/stevengoossensB/ha-codex-usage?logo=github)](https://github.com/stevengoossensB/ha-codex-usage/releases)
[![Last Commit](https://img.shields.io/github/last-commit/stevengoossensB/ha-codex-usage)](https://github.com/stevengoossensB/ha-codex-usage/commits)

[![Buy Me a Coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&slug=stevengoossens&button_colour=FFDD00&font_colour=000000&font_family=Arial&outline_colour=000000&coffee_colour=ffffff)](https://coff.ee/stevengoossens)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg?style=flat-square)](https://my.home-assistant.io/redirect/hacs_repository/?owner=stevengoossensB&repository=ha-codex-usage&category=Integration)

Track your **OpenAI Codex** usage in Home Assistant:

- **ChatGPT plan limits** (Plus / Pro / Business…): Codex 5-hour and weekly usage %, code-review limit, model-specific limits (e.g. Codex-Spark), reset times, plan and credit balance. The same numbers as `/status` in the Codex CLI.
- **API usage** (pay-as-you-go, via an OpenAI Admin key): input / output / cached tokens, requests and cost (USD), today and month to date.

Companion integrations: [ha-claude-usage](https://github.com/stevengoossensB/ha-claude-usage) · [ha-copilot-usage](https://github.com/stevengoossensB/ha-copilot-usage)

## Installation

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/stevengoossensB/ha-codex-usage` as type **Integration**.
2. Install **Codex Usage** and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Codex Usage*.


## Setup

### Plan limits – sign in (recommended)
1. Choose *Codex limits of my ChatGPT plan (sign in)*.
2. Open the sign-in link and log in with your ChatGPT account.
3. Your browser is redirected to `http://localhost:1455/auth/callback?code=…` and shows an error page. That's expected: copy the full URL from the address bar and paste it into Home Assistant.

Home Assistant gets its **own** session and refreshes it automatically, so your Codex CLI login is untouched.

### Plan limits – import auth.json
Paste the contents of `~/.codex/auth.json`. OpenAI refresh tokens are **single use**: after Home Assistant refreshes, the CLI that produced that file must log in again. To avoid that, create a separate login: `CODEX_HOME=/tmp/ha-codex codex login` and import `/tmp/ha-codex/auth.json`.

### API usage
Create an Admin key (`sk-admin-…`) at platform.openai.com → Settings → Organization → Admin keys, choose *OpenAI API token usage and cost* and paste it.

## Entities

| Mode | Entity | Notes |
|---|---|---|
| Plan | `5h usage` / `5h reset` | primary window (% used, timestamp) |
| Plan | `Weekly usage` / `Weekly reset` | secondary window |
| Plan | `Code review weekly usage`, `<model> 5h usage`, … | created automatically when reported |
| Plan | `Plan` | plan type; attributes `allowed`, `limit_reached`, `rate_limit_reached_type` |
| Plan | `Credits balance` | Codex credits, if your plan has them |
| API | `Total / Input / Output / Cached input tokens this month` and `… today` | `state_class: total` |
| API | `Requests this month/today`, `Cost this month/today` | cost in USD |

Window names come from their length as reported by OpenAI, so if OpenAI changes a window the sensor name follows. Default polling: 5 min (plan), 30 min (API).

## Caveats

- `chatgpt.com/backend-api/wham/usage` is the endpoint the Codex CLI and app use; it is not a documented public API and may change.
- Admin API data lags a few minutes. Days and months are in UTC.

## License

MIT

"""Sensors for Codex Usage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AdminUsage, PlanUsage, UsageWindow
from .const import AUTH_ADMIN, DOMAIN
from .coordinator import CodexUsageConfigEntry, CodexUsageCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodexUsageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors."""
    coordinator = entry.runtime_data
    if coordinator.auth_type == AUTH_ADMIN:
        async_add_entities(AdminSensor(coordinator, d) for d in ADMIN_SENSORS)
        return

    async_add_entities([PlanSensor(coordinator), CreditsSensor(coordinator)])
    known: set[str] = set()

    @callback
    def _add_new_windows() -> None:
        data = coordinator.data
        if not isinstance(data, PlanUsage):
            return
        new: list[SensorEntity] = []
        for key, window in data.windows.items():
            if key in known:
                continue
            known.add(key)
            new += [
                WindowUsageSensor(coordinator, key, window.name),
                WindowResetSensor(coordinator, key, window.name),
            ]
        if new:
            async_add_entities(new)

    _add_new_windows()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_windows))


class CodexEntity(CoordinatorEntity[CodexUsageCoordinator]):
    """Base entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CodexUsageCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        admin = coordinator.auth_type == AUTH_ADMIN
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="OpenAI",
            model="Admin API" if admin else "Codex (ChatGPT plan)",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://platform.openai.com/usage"
            if admin
            else "https://chatgpt.com/codex/settings/usage",
        )

    @property
    def _plan(self) -> PlanUsage | None:
        data = self.coordinator.data
        return data if isinstance(data, PlanUsage) else None


class WindowUsageSensor(CodexEntity, SensorEntity):
    """Percent of a window used."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:gauge"

    def __init__(self, coordinator: CodexUsageCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"{key}_used")
        self._key = key
        self._attr_name = f"{name} usage"

    @property
    def _window(self) -> UsageWindow | None:
        plan = self._plan
        return plan.windows.get(self._key) if plan else None

    @property
    def available(self) -> bool:
        return super().available and self._window is not None

    @property
    def native_value(self) -> float | None:
        return self._window.used_percent if self._window else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        w = self._window
        if not w:
            return None
        return {
            "resets_at": w.resets_at.isoformat() if w.resets_at else None,
            "window_seconds": w.window_seconds,
        }


class WindowResetSensor(CodexEntity, SensorEntity):
    """Reset time of a window."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-refresh-outline"

    def __init__(self, coordinator: CodexUsageCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"{key}_resets_at")
        self._key = key
        self._attr_name = f"{name} reset"

    @property
    def native_value(self) -> datetime | None:
        plan = self._plan
        w = plan.windows.get(self._key) if plan else None
        return w.resets_at if w else None


class PlanSensor(CodexEntity, SensorEntity):
    """Plan type plus rate-limit status attributes."""

    _attr_icon = "mdi:card-account-details-outline"
    _attr_name = "Plan"

    def __init__(self, coordinator: CodexUsageCoordinator) -> None:
        super().__init__(coordinator, "plan")

    @property
    def native_value(self) -> str | None:
        return self._plan.plan_type if self._plan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return dict(self._plan.status) if self._plan else None


class CreditsSensor(CodexEntity, SensorEntity):
    """Codex credits balance (when the plan has credits)."""

    _attr_icon = "mdi:wallet-outline"
    _attr_name = "Credits balance"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "credits"

    def __init__(self, coordinator: CodexUsageCoordinator) -> None:
        super().__init__(coordinator, "credits")

    @property
    def native_value(self) -> float | None:
        credits = self._plan.credits if self._plan else None
        if not credits or credits.get("balance") is None:
            return None
        try:
            return float(credits["balance"])
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return dict(self._plan.credits) if self._plan and self._plan.credits else None


# ------------------------------------------------------------------- admin API


@dataclass(frozen=True, kw_only=True)
class AdminSensorDescription(SensorEntityDescription):
    """Admin API sensor description."""

    value_fn: Callable[[AdminUsage], float | int | None]
    period: str


def _admin_descriptions() -> tuple[AdminSensorDescription, ...]:
    out: list[AdminSensorDescription] = []
    labels = {"input": "Input", "output": "Output", "cached_input": "Cached input"}
    for period, attr, suffix in (
        ("month", "tokens_month", "this month"),
        ("today", "tokens_today", "today"),
    ):
        out.append(
            AdminSensorDescription(
                key=f"tokens_total_{period}",
                name=f"Total tokens {suffix}",
                icon="mdi:counter",
                native_unit_of_measurement="tokens",
                state_class=SensorStateClass.TOTAL,
                value_fn=lambda d, a=attr: sum(getattr(d, a).values()),
                period=period,
            )
        )
        for field, label in labels.items():
            out.append(
                AdminSensorDescription(
                    key=f"tokens_{field}_{period}",
                    name=f"{label} tokens {suffix}",
                    icon="mdi:counter",
                    native_unit_of_measurement="tokens",
                    state_class=SensorStateClass.TOTAL,
                    value_fn=lambda d, a=attr, f=field: getattr(d, a)[f],
                    period=period,
                    entity_registry_enabled_default=field != "cached_input",
                )
            )
        out.append(
            AdminSensorDescription(
                key=f"requests_{period}",
                name=f"Requests {suffix}",
                icon="mdi:swap-horizontal",
                native_unit_of_measurement="requests",
                state_class=SensorStateClass.TOTAL,
                value_fn=lambda d, p=period: d.requests_month if p == "month" else d.requests_today,
                period=period,
            )
        )
        out.append(
            AdminSensorDescription(
                key=f"cost_{period}",
                name=f"Cost {suffix}",
                device_class=SensorDeviceClass.MONETARY,
                native_unit_of_measurement="USD",
                state_class=SensorStateClass.TOTAL,
                suggested_display_precision=2,
                value_fn=lambda d, p=period: d.cost_month if p == "month" else d.cost_today,
                period=period,
            )
        )
    return tuple(out)


ADMIN_SENSORS = _admin_descriptions()


class AdminSensor(CodexEntity, SensorEntity):
    """OpenAI Admin API usage sensor."""

    entity_description: AdminSensorDescription

    def __init__(self, coordinator: CodexUsageCoordinator, description: AdminSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def _data(self) -> AdminUsage | None:
        data = self.coordinator.data
        return data if isinstance(data, AdminUsage) else None

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self._data) if self._data else None

    @property
    def last_reset(self) -> datetime | None:
        if not self._data:
            return None
        return self._data.month_start if self.entity_description.period == "month" else self._data.day_start

"""Per-client settings of the appointments module (tenant_modules.config)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from api.modules.base import ModuleConfig


class AppointmentsConfig(ModuleConfig):
    slot_minutes: Literal[15, 30, 60] = 30  # the grid slots start on
    min_notice_hours: int = Field(default=2, ge=0, le=168)  # nothing offered sooner than this
    horizon_days: int = Field(default=30, ge=1, le=180)  # nor further ahead than this
    # inside this many hours of the start, the agent hands a move or cancel to the team
    cancel_cutoff_hours: int = Field(default=24, ge=0, le=720)
    # on: the agent's bookings land as `requested` and a person confirms them
    require_team_confirmation: bool = False
    buffer_minutes: int = Field(default=0, ge=0, le=120)  # kept free around each booking


def config_of(raw: object) -> AppointmentsConfig:
    return raw if isinstance(raw, AppointmentsConfig) else AppointmentsConfig()

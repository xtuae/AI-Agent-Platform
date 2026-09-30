"""Per-client settings of the campaigns module (tenant_modules.config)."""

from __future__ import annotations

from pydantic import Field

from api.modules.base import ModuleConfig


class CampaignsConfig(ModuleConfig):
    # at most one marketing message per customer per this many days, across all campaigns (02 §4.4)
    frequency_days: int = Field(default=7, ge=1, le=60)
    # a customer message within this many hours of a campaign message counts as a reply to it
    reply_window_hours: int = Field(default=72, ge=1, le=336)
    # an order within this many days of a campaign message is attributed to it
    attribution_days: int = Field(default=7, ge=1, le=60)
    # a new campaign's send rate (per minute); never above the WABA's messaging tier either way
    throttle_per_minute: int = Field(default=60, ge=1, le=600)
    # links in templates only once the business's domain is verified with Meta
    links_allowed: bool = False


def config_of(raw: object) -> CampaignsConfig:
    return raw if isinstance(raw, CampaignsConfig) else CampaignsConfig()

"""Campaigns (Phase 4, 02 §4): WhatsApp templates, declarative segments, and a campaign engine
with a human approval gate. Not a chatbot that decides to message people.

It adds nothing to the support prompt except, when a customer replies to a campaign, the spec's
reply context (§4.2). Other modules contribute what segments can filter on (`segment_fields`) and
what a campaign led to (`campaign_attribution`).
"""

from __future__ import annotations

from api.modules.base import Module
from api.modules.campaigns import hooks, replies, routes
from api.modules.campaigns.config import CampaignsConfig

MODULE = Module(
    key="campaigns",
    name="Campaigns",
    description="Approved WhatsApp templates sent to opted-in segments, with every Meta guard.",
    config_model=CampaignsConfig,
    reply_context=replies.reply_context,
    router=routes.router,
    today=hooks.today,
    contact_panel=hooks.contact_panel,
)

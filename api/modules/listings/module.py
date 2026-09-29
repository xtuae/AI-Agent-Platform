"""Listings: properties for sale or rent that the agent answers questions about, and (with the
appointments module) books viewings of — through appointments' `appointment_subject` extension
point, which this module implements (book_appointment gains `listing_ref`)."""

from __future__ import annotations

from api.modules.base import Intent, Line, Module
from api.modules.listings import hooks, routes, tools

MODULE = Module(
    key="listings",
    name="Property listings",
    description="Properties for sale or rent. The agent quotes only these, and books viewings.",
    tools=(tools.SEARCH_TOOL, tools.GET_TOOL),
    capabilities=(Line(55, "- Answer questions about available properties"),),
    facts=(Line(15, "a property detail"), Line(16, "a property price")),
    sections=(
        Line(
            15,
            "## PROPERTY QUESTIONS\n"
            "#. Find properties with search_listings: purpose (buy or rent) first, then area, "
            "bedrooms and budget.\n"
            "#. Share at most three at a time — title, area, bedrooms and price, straight from the "
            "tool.\n"
            "#. For more about one, call get_listing. Say only what it returns; if it does not "
            "say, offer to ask the team.",
        ),
    ),
    steps={
        "booking": (
            Line(
                15,
                "#. For a viewing, pass the property's ref as listing_ref, and the place is that "
                "property.",
            ),
        )
    },
    intents=(
        Intent(
            45,
            "property",
            "Asking about a property: price, size, location, availability, or what is on offer.",
        ),
    ),
    tool_params={
        "book_appointment": {
            hooks.PARAM: {
                "type": "string",
                "description": "For a viewing: the ref of the property, from search_listings",
            }
        }
    },
    appointment_subject=hooks.appointment_subject,
    router=routes.router,
    today=hooks.today,
)

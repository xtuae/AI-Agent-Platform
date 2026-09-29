"""Catalog: the products a business sells, with prices. Prompt text is 02_agent_prompts.md §2."""

from __future__ import annotations

from api.modules.base import Intent, Line, Module
from api.modules.catalog import routes, tools

MODULE = Module(
    key="catalog",
    name="Products and prices",
    description="The products the business sells. The agent quotes only these prices.",
    tools=(tools.TOOL,),
    capabilities=(
        Line(40, "- Answer prices and explain current offers"),
        Line(50, "- Answer questions about the products"),
    ),
    facts=(Line(10, "a price"), Line(30, "a stock level")),
    steps={
        "ordering": (
            Line(
                30,
                "#. Once per order conversation, and only when it fits naturally, mention ONE "
                "relevant\n   {{cross_sell_category}} item from the tool results. If they decline, "
                "never raise it again.",
            ),
        )
    },
    intents=(Intent(40, "price", "Asking prices, offers, coupon packages, or what is available."),),
    router=routes.router,
)

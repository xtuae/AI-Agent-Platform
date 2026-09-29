"""Coupons: prepaid bottle books (packages for 30 / 60 / 120 days etc.). Prompt text is
02_agent_prompts.md §2 and §1."""

from __future__ import annotations

from api.modules.base import Intent, Line, Module
from api.modules.coupons import hooks, tools

MODULE = Module(
    key="coupons",
    name="Coupon books",
    description="Prepaid bottle books: balances, packages on sale, paying for orders with coupons.",
    requires=("orders",),
    tools=(tools.TOOL,),
    capabilities=(Line(20, "- Tell a customer their coupon balance"),),
    facts=(Line(20, "a coupon balance"),),
    steps={
        "ordering": (
            Line(
                20,
                "#. If they are buying water and have no live coupon book, mention the coupon "
                "packages once —\n   the savings are real and customers like them. Do not push "
                "twice.",
            ),
        )
    },
    context_fetches=(Line(20, "live coupon balance"),),
    context_topics=(Line(10, "their balance"),),
    intents=(
        Intent(
            20, "balance", "Asking how many bottles/coupons remain, or about their coupon book."
        ),
    ),
    tool_params={
        "create_order": {
            "use_coupon_book": {
                "type": "boolean",
                "description": (
                    "Draw bottles from the customer's existing coupon book rather than charging"
                ),
            }
        }
    },
    customer_block=hooks.customer_block,
    customer_context=hooks.customer_context,
    order_redeem=hooks.redeem,
    order_cancelled=hooks.restore,
    contact_panel=hooks.contact_panel,
)

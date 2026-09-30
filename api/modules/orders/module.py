"""Orders: taking, scheduling and delivering orders. Prompt text is 02_agent_prompts.md §2 and §1.

Extension points it offers other modules (see service.py): `order_redeem` (pay for lines from a
prepaid balance) and `order_cancelled` (give it back).
"""

from __future__ import annotations

from pydantic import Field

from api.modules.base import Intent, Line, Module, ModuleConfig
from api.modules.orders import hooks, routes, segments
from api.modules.orders.tools import create_order, get_order_status, reschedule_delivery


class OrdersConfig(ModuleConfig):
    # Days from order to delivery when the customer does not ask for a date. None → the agent
    # says the team will confirm the date.
    lead_days: int | None = Field(default=None, ge=0, le=30)


MODULE = Module(
    key="orders",
    name="Orders and delivery",
    description="Orders from the agent or the dashboard, delivery dates and the van list.",
    requires=("catalog",),
    config_model=OrdersConfig,
    tools=(create_order.TOOL, get_order_status.TOOL, reschedule_delivery.TOOL),
    capabilities=(
        Line(10, "- Take orders and repeat orders"),
        Line(30, "- Answer delivery questions and reschedule deliveries"),
    ),
    facts=(Line(40, "a delivery time"),),
    rules=(
        Line(
            10,
            "#. NEVER confirm an order until you have said back to the customer what they are "
            "buying, the total,\n   and the delivery area, and they have agreed.",
        ),
    ),
    sections=(Line(10, "## ORDERING FLOW\n<<steps:ordering>>"),),
    steps={
        "ordering": (
            Line(
                10,
                '#. Work out what they want. "Same as last time" → call get_customer_context '
                "and confirm it back.",
            ),
            Line(40, "#. Confirm: items, quantity, total, area. Then call create_order."),
            Line(
                50,
                "#. Give them the order reference and the delivery window from the tool result.",
            ),
        )
    },
    business_facts=(Line(10, "Delivery areas: {{service_areas}}"),),
    new_contact=Line(
        10,
        "This is a new contact. You do not know their name, area or history yet.\n"
        "Ask their area before quoting delivery, and their name once, politely.",
    ),
    context_fetches=(Line(30, "recent orders"),),
    context_topics=(Line(20, "their history"), Line(30, "a repeat order")),
    intents=(
        Intent(10, "order", 'Wants to buy, reorder, or add items. Includes "same as last time".'),
        Intent(
            30,
            "delivery",
            "Where is my order, when will it come, change address, change time, reschedule.",
        ),
    ),
    intent_rules=(
        Line(
            10,
            "- complaint wins over order. An angry customer who also wants to reorder is a complaint.",
        ),
    ),
    customer_block=hooks.customer_block,
    customer_context=hooks.customer_context,
    segment_fields=segments.FIELDS,
    campaign_attribution=segments.attribution,
    router=routes.router,
    today=hooks.today,
    contact_panel=hooks.contact_panel,
)

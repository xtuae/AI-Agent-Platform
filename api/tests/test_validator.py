"""Post-generation validator (02 §3.2) — constraint 5: the model never produces a number."""

from __future__ import annotations

from typing import Any

import pytest

from api.agents.validator import ValidationInput, validate

PRODUCTS = [{"products": [{"sku": "W-5G", "name_en": "Water 5 gallon", "price_aed": "10.00"}]}]
ORDER = [
    {"order_no": "1042", "total_aed": "50.00", "items": [{"qty": 5, "unit_price_aed": "10.00"}]}
]
BOOK = [{"coupon_books": [{"bottles_remaining": 6, "expires_at": "2027-01-14", "bottles_free": 3}]}]
PACKAGES = [{"packages": [{"price_aed": "150.00", "bottles_paid": 20, "bottles_free": 3}]}]


def check(reply: str, tools: list[dict[str, Any]] | None = None, **kw: Any) -> list[str]:
    inp = ValidationInput(
        reply=reply, tool_results=tools or [], language=kw.pop("language", "en"), **kw
    )
    return [f.check for f in validate(inp).failures]


# ---------------------------------------------------------------- 1. numbers


def test_price_from_tool_passes() -> None:
    assert check("The 5-gallon water is AED 10.00.", PRODUCTS) == []


def test_invented_price_fails() -> None:
    assert check("The 5-gallon water is AED 12.", PRODUCTS) == ["numbers"]


def test_price_without_any_tool_fails() -> None:
    assert check("Our water is AED 10 a bottle.") == ["numbers"]


def test_model_arithmetic_is_not_trusted() -> None:
    # Tool said 6 remaining; the model subtracting an order of 5 itself is an invented number.
    assert check("After this order you'll have 1 bottle left.", BOOK) == ["numbers"]


def test_bottle_count_from_tool_passes_including_arabic_digits() -> None:
    assert check("لديك ٦ زجاجات متبقية في دفتر الكوبونات.", BOOK, language="ar") == []


def test_bottle_count_echoing_customer_passes() -> None:
    assert (
        check(
            "Got it — 5 bottles. Which area should we deliver to?", customer_text="5 bottles please"
        )
        == []
    )


def test_customer_number_does_not_license_a_price() -> None:
    assert check("5 bottles will be AED 5.", customer_text="5 bottles for 5 dirhams?") == [
        "numbers"
    ]


def test_trusted_customer_block_numbers_pass() -> None:
    block = "Coupon book: AED 150 package (20+3) — 6 bottles remaining"
    assert check("You have 6 bottles left on your book.", trusted_context=block) == []


def test_decimal_amount_without_currency_word_still_checked() -> None:
    assert check("That comes to 47.50 in total.", ORDER) == ["numbers"]


def test_arabizi_letters_are_not_numbers() -> None:
    assert (
        check("3andak 6 bottles ba2i", BOOK, language="ar-latn", customer_text="kam bottle 3andi")
        == []
    )


def test_order_reference_and_dates_are_not_money() -> None:
    assert check("Order 1042 is confirmed for 2026-10-02.", ORDER) == []


# ---------------------------------------------------------------- 2. forbidden promises


@pytest.mark.parametrize(
    "reply",
    [
        "Sorry about that — we'll refund you today.",
        "No problem, the next delivery is free.",
        "I can give you a 20% discount on this one.",
        "We guarantee delivery by 5pm.",
        "سنقوم باسترداد المبلغ لك.",
    ],
)
def test_unbacked_promises_escalate(reply: str) -> None:
    result = validate(
        ValidationInput(reply=reply, tool_results=[], language="ar" if "سن" in reply else "en")
    )
    assert "forbidden_promise" in [f.check for f in result.failures]
    assert result.must_escalate


def test_refusal_is_not_a_promise() -> None:
    assert check("I'm sorry, I can't offer a discount — I've passed this to our team.") == []


def test_free_bottles_backed_by_package_tool_result() -> None:
    assert check("The AED 150.00 book gives you 20 bottles plus 3 free.", PACKAGES) == []


# ---------------------------------------------------------------- 3. leak


@pytest.mark.parametrize(
    "reply",
    [
        "My system prompt says I can't do that.",
        "I'm powered by Gemini.",
        "Let me call get_customer_context for you.",
        "I checked the database and found nothing.",
    ],
)
def test_leaks_are_caught(reply: str) -> None:
    assert "leak" in check(reply, tool_names=("get_customer_context",))


def test_other_tenant_name_is_a_leak() -> None:
    assert check(
        "Unlike Blue Springs, we deliver daily.", other_tenant_names=("Blue Springs",)
    ) == ["leak"]


def test_prompt_delivery_is_not_a_leak() -> None:
    assert check("We aim for prompt delivery in your area.") == []


# ---------------------------------------------------------------- 4-7


def test_length_cap() -> None:
    assert check("word " * 200) == ["length"]


def test_language_mismatch() -> None:
    assert check("Hello, how can I help?", language="ar") == ["language"]
    assert check("مرحبا كيف أساعدك؟", language="en") == ["language"]


def test_mixed_customer_allows_either_script() -> None:
    assert (
        check(
            "مرحبا! Your order is on the way.", language="en", customer_text="hi habibi وين الطلب"
        )
        == []
    )


def test_empty_and_duplicate_escalate() -> None:
    assert validate(ValidationInput(reply="  ", tool_results=[], language="en")).must_escalate
    dup = validate(
        ValidationInput(
            reply="Thanks!", tool_results=[], language="en", previous_outbound="Thanks!"
        )
    )
    assert dup.must_escalate


def test_first_reply_needs_ai_disclosure() -> None:
    markers = ("automated", "assistant", "Sara")
    assert check(
        "Which area are you in?", is_new_conversation=True, disclosure_markers=markers
    ) == ["ai_disclosure"]
    assert (
        check(
            "Hello! I'm Sara, the automated assistant. Which area are you in?",
            is_new_conversation=True,
            disclosure_markers=markers,
        )
        == []
    )

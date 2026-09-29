"""Appointments: book, move, cancel and check appointments (04_modules_appointments_listings.md).

Extension point it offers other modules: `appointment_subject` — an appointment can be ABOUT one of
their records (listings: a viewing of a property). Such a module adds its parameter to
book_appointment through `tool_params` and resolves it in its hook.
"""

from __future__ import annotations

from api.modules.appointments import hooks, routes
from api.modules.appointments.config import AppointmentsConfig
from api.modules.appointments.tools import (
    book_appointment,
    cancel_appointment,
    get_availability,
    get_my_appointments,
    reschedule_appointment,
)
from api.modules.base import Intent, Line, Module

MODULE = Module(
    key="appointments",
    name="Appointments",
    description="Bookable time: viewings, consultations, visits. No double bookings.",
    config_model=AppointmentsConfig,
    tools=(
        get_availability.TOOL,
        book_appointment.TOOL,
        get_my_appointments.TOOL,
        reschedule_appointment.TOOL,
        cancel_appointment.TOOL,
    ),
    capabilities=(Line(60, "- Book, move or cancel appointments"),),
    facts=(Line(50, "an appointment time"),),
    rules=(
        Line(
            20,
            "#. NEVER confirm a booking until you have said back the type, date, time and place, "
            "and they have agreed.",
        ),
    ),
    sections=(Line(20, "## BOOKING FLOW\n<<steps:booking>>"),),
    steps={
        "booking": (
            Line(
                10,
                "#. Work out which kind of appointment they need. If you do not know the kinds, "
                "call get_availability\n   without a type.",
            ),
            Line(
                20,
                "#. Call get_availability and offer two or three of the times it returns. Never "
                "offer a time it did not return.",
            ),
            Line(
                30,
                "#. Say back the type, date, time and place. When they agree, call book_appointment.",
            ),
            Line(
                40,
                "#. Give them the reference from the tool result. If the status is requested, tell "
                "them the team will\n   confirm it.",
            ),
            Line(
                50,
                "#. To move or cancel, call get_my_appointments first. If the tool says it is too "
                "close to the time,\n   tell them a person from the team will be in touch.",
            ),
        )
    },
    new_contact=Line(
        5,
        "This is a new contact. You do not know their name or history yet.\n"
        "Ask their name once, politely, before you book anything.",
    ),
    context_fetches=(Line(40, "upcoming appointments"),),
    context_topics=(Line(40, "their appointments"),),
    intents=(Intent(50, "booking", "Wants to book, move, cancel or check an appointment."),),
    customer_block=hooks.customer_block,
    customer_context=hooks.customer_context,
    router=routes.router,
    today=hooks.today,
    contact_panel=hooks.contact_panel,
)

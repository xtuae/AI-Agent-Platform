"""Load test: N concurrent WhatsApp conversations against the real webhook → worker → reply path,
with the LLM and Meta stubbed at realistic latency (loadtest/stub_upstreams.py).

    locust -f loadtest/locustfile.py --headless -u 200 -r 20 -t 5m --host http://127.0.0.1:8000

Each simulated customer is one conversation: it sends a signed webhook, waits until its reply
reaches the (stub) Meta API, then pauses like a person typing (8-20 s) and writes again. The
measured time — "turn" — is webhook POST to reply sent: what the customer experiences, minus
WhatsApp's own delivery. It includes the deliberate TURN_DEBOUNCE_S pause (default 2 s), which
waits for a second message typed right after the first.

Env: META_APP_SECRET (to sign), LOAD_PNID / LOAD_WABA (the seeded channel), STUB_URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
import uuid

import httpx
from locust import HttpUser, between, events, task

SECRET = os.environ["META_APP_SECRET"].encode()
PNID = os.environ.get("LOAD_PNID", "900100200300")
WABA = os.environ.get("LOAD_WABA", "900100200301")
STUB = os.environ.get("STUB_URL", "http://127.0.0.1:9100")
REPLY_TIMEOUT_S = 30.0
QUESTIONS = [
    "Do you deliver to Al Nahda?",
    "What time do you open tomorrow?",
    "Can I change my delivery address?",
    "Is the water bottle returnable?",
]


class Customer(HttpUser):
    wait_time = between(8, 20)

    def on_start(self) -> None:
        self.wa_id = "9715" + str(random.randrange(10**8, 10**9))  # noqa: S311
        self.stub = httpx.Client(base_url=STUB, timeout=5)

    def _payload(self, text: str) -> bytes:
        msg = {
            "from": self.wa_id,
            "id": f"wamid.LT{uuid.uuid4().hex}",
            "timestamp": str(int(time.time())),
            "type": "text",
            "text": {"body": text},
        }
        value = {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "971600000000", "phone_number_id": PNID},
            "contacts": [{"wa_id": self.wa_id, "profile": {"name": "Load"}}],
            "messages": [msg],
        }
        body = {
            "object": "whatsapp_business_account",
            "entry": [{"id": WABA, "changes": [{"field": "messages", "value": value}]}],
        }
        return json.dumps(body).encode()

    @task
    def conversation_turn(self) -> None:
        before = len(self.stub.get(f"/sent/{self.wa_id}").json()["at"])
        body = self._payload(random.choice(QUESTIONS))  # noqa: S311
        sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        started = time.time()
        r = self.client.post(
            "/webhook/meta",
            data=body,
            headers={"content-type": "application/json", "x-hub-signature-256": sig},
            name="webhook POST",
        )
        if r.status_code != 200:
            return
        while time.time() - started < REPLY_TIMEOUT_S:
            at = self.stub.get(f"/sent/{self.wa_id}").json()["at"]
            if len(at) > before:
                events.request.fire(
                    request_type="TURN",
                    name="webhook → reply sent",
                    response_time=(at[before] - started) * 1000,
                    response_length=0,
                    exception=None,
                    context={},
                )
                return
            time.sleep(0.5)  # the reply time comes from the stub's clock, not this loop
        events.request.fire(
            request_type="TURN",
            name="webhook → reply sent",
            response_time=REPLY_TIMEOUT_S * 1000,
            response_length=0,
            exception=TimeoutError("no reply"),
            context={},
        )

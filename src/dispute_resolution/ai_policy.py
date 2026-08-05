"""AI-backed policy agent using Groq's OpenAI-compatible JSON API."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODEL_PROVIDER = "groq"
MODEL_NAME = "llama-3.1-8b-instant"
MODEL_PARAMETER_SIZE = 8_000_000_000
MODEL_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_TEMPERATURE = 0.0
MODEL_SEED = 2026
MODEL_MAX_TOKENS = 80
# The free tier is currently limited by 6K tokens/minute for this model. A
# conservative interval keeps a 50-case run below that budget without billing.
MODEL_MIN_INTERVAL_SECONDS = 7.0

POLICY_SYSTEM_PROMPT = """You are the EC_POLICY_V2 classification agent.
The user supplies six verified boolean primary_conditions. Select the FIRST true key in
this exact precedence: canceled_order_paid, unavailable_order_paid, late_delivery_seller,
late_delivery_logistics, valid_split_payment, unsupported_late_claim.
Return JSON only as {"primary":"the_selected_key"}. Never create another label."""

POLICY_CATALOG: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "cause": "ORDER_CANCELED_AFTER_PAYMENT",
        "party_type": "platform",
        "party_ids": ["OLIST_PLATFORM"],
        "refund_fact": "payment_total_brl",
        "actions": ["issue_full_refund", "verify_refund_completion"],
    },
    "unavailable_order_paid": {
        "cause": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
        "party_type": "platform",
        "party_ids": ["OLIST_PLATFORM"],
        "refund_fact": "payment_total_brl",
        "actions": ["issue_full_refund", "verify_refund_completion"],
    },
    "late_delivery_seller": {
        "cause": "SELLER_HANDOFF_AFTER_LIMIT",
        "party_type": "seller",
        "party_ids_fact": "late_handoff_seller_ids",
        "refund_fact": "freight_total_brl",
        "actions": ["refund_freight", "review_seller_handoff"],
    },
    "late_delivery_logistics": {
        "cause": "CARRIER_DELIVERED_AFTER_ESTIMATE",
        "party_type": "logistics_provider",
        "party_ids": ["LOGISTICS_PROVIDER"],
        "refund_fact": "freight_total_brl",
        "actions": ["refund_freight", "review_carrier_delay"],
    },
    "valid_split_payment": {
        "cause": "MULTIPLE_PAYMENTS_RECONCILED",
        "party_type": None,
        "party_ids": [],
        "refund_value": 0.0,
        "actions": ["explain_valid_split_payment"],
    },
    "unsupported_late_claim": {
        "cause": "DELIVERY_WITHIN_ESTIMATE",
        "party_type": None,
        "party_ids": [],
        "refund_value": 0.0,
        "actions": ["reject_late_refund"],
    },
}
SECONDARY_SEQUENCE = (
    "multi_item_order",
    "multi_seller_order",
    "split_payment",
    "repeat_customer",
    "multiple_categories",
)
SUPPLEMENTAL_ACTIONS = (
    ("multi_seller_order", None, "coordinate_multi_seller_case"),
    ("split_payment", "valid_split_payment", "verify_payment_allocation"),
)


class AIConfigurationError(RuntimeError):
    """Raised when the provider credentials are missing."""


class AIResponseError(RuntimeError):
    """Raised when the provider cannot return a usable policy decision."""


def load_dotenv(path: Path) -> None:
    """Load a minimal KEY=VALUE .env file without overriding existing environment values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


class GroqJSONClient:
    """Small dependency-free client for Groq JSON Object Mode."""

    def __init__(self, api_key: str, timeout_seconds: int = 60) -> None:
        if not api_key:
            raise AIConfigurationError(
                "Missing GROQ_API_KEY. Copy .env.example to .env and add your free Groq API key."
            )
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.last_metadata: dict[str, Any] = {}
        self.last_request_at = 0.0

    def generate(self, facts: dict[str, Any]) -> dict[str, Any]:
        # Preserve policy precedence in primary_conditions for the small model.
        serialized_facts = json.dumps(facts, ensure_ascii=False)
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": POLICY_SYSTEM_PROMPT},
                {"role": "user", "content": serialized_facts},
            ],
            "temperature": MODEL_TEMPERATURE,
            "seed": MODEL_SEED,
            "max_tokens": MODEL_MAX_TOKENS,
            "response_format": {"type": "json_object"},
        }
        request = Request(
            MODEL_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "olist-dispute-resolution/0.1",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                elapsed = time.monotonic() - self.last_request_at
                if elapsed < MODEL_MIN_INTERVAL_SECONDS:
                    time.sleep(MODEL_MIN_INTERVAL_SECONDS - elapsed)
                self.last_request_at = time.monotonic()
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                decision = json.loads(content)
                self.last_metadata = {
                    "provider": MODEL_PROVIDER,
                    "model": MODEL_NAME,
                    "response_id": body.get("id"),
                    "system_fingerprint": body.get("system_fingerprint"),
                    "usage": body.get("usage", {}),
                    "facts_sha256": hashlib.sha256(serialized_facts.encode()).hexdigest(),
                    "model_decision": decision,
                }
                if not isinstance(decision, dict):
                    raise AIResponseError("model response is not a JSON object")
                return decision
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                last_error = AIResponseError(f"Groq HTTP {error.code}: {detail[:300]}")
                if error.code == 429:
                    retry_after = error.headers.get("Retry-After", "5")
                    try:
                        time.sleep(min(float(retry_after), 30.0))
                    except ValueError:
                        time.sleep(5)
            except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
                last_error = AIResponseError(f"invalid Groq response: {error}")
            if attempt < 2:
                time.sleep(2**attempt)
        raise AIResponseError(f"policy model failed after 3 attempts: {last_error}")


class AIPolicyAgent:
    """Build verified facts and delegate the EC_POLICY_V2 decision to the 8B model."""

    REQUIRED_FIELDS = {"primary"}
    PRIMARY_ISSUES = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "unsupported_late_claim",
    }
    CAUSES = {
        "ORDER_CANCELED_AFTER_PAYMENT",
        "ORDER_UNAVAILABLE_AFTER_PAYMENT",
        "SELLER_HANDOFF_AFTER_LIMIT",
        "CARRIER_DELIVERED_AFTER_ESTIMATE",
        "MULTIPLE_PAYMENTS_RECONCILED",
        "DELIVERY_WITHIN_ESTIMATE",
    }

    def __init__(self, client: GroqJSONClient) -> None:
        self.client = client

    def decide(
        self,
        order: dict[str, str],
        item_facts: dict[str, Any],
        payment: dict[str, Any],
        delivery: dict[str, Any],
        customer: dict[str, Any],
    ) -> dict[str, Any]:
        facts = {
            "policy_version": "EC_POLICY_V2",
            "order_id": order["order_id"],
            "order_status": order["order_status"],
            "payment_total_brl": payment["payment_total_brl"],
            "freight_total_brl": payment["freight_total_brl"],
            "reconciled": payment["reconciled"],
            "delivery_variance_hours": delivery["delivery_variance_hours"],
            "late_delivery": delivery["late_delivery"],
            "late_handoff_seller_ids": delivery["late_handoff_seller_ids"][:3],
            "primary_conditions": {
                "canceled_order_paid": (
                    order["order_status"] == "canceled" and payment["payment_total_brl"] > 0
                ),
                "unavailable_order_paid": (
                    order["order_status"] == "unavailable" and payment["payment_total_brl"] > 0
                ),
                "late_delivery_seller": (
                    delivery["late_delivery"] and bool(delivery["late_handoff_seller_ids"])
                ),
                "late_delivery_logistics": (
                    delivery["late_delivery"] and not delivery["late_handoff_seller_ids"]
                ),
                "valid_split_payment": (
                    payment["split_payment"] and payment["reconciled"] is True
                ),
                "unsupported_late_claim": (
                    delivery["delivery_variance_hours"] is not None
                    and delivery["delivery_variance_hours"] <= 0
                    and payment["reconciled"] is True
                ),
            },
            "secondary_conditions": {
                "multi_item_order": item_facts["multi_item"],
                "multi_seller_order": item_facts["multi_seller"],
                "split_payment": payment["split_payment"],
                "repeat_customer": bool(customer["related_order_ids"]),
                "multiple_categories": item_facts["multiple_categories"],
            },
        }
        model_decision = self.client.generate(
            {
                "policy_version": facts["policy_version"],
                "primary_conditions": facts["primary_conditions"],
            }
        )
        if set(model_decision) != self.REQUIRED_FIELDS:
            raise AIResponseError(
                "model classification must contain exactly the primary field"
            )
        primary = model_decision["primary"]
        if primary not in self.PRIMARY_ISSUES:
            raise AIResponseError(
                f"model returned an unknown primary issue: {primary!r}"
            )
        if facts["primary_conditions"].get(primary) is not True:
            raise AIResponseError(f"model selected an unsatisfied primary issue: {primary}")

        spec = POLICY_CATALOG[primary]
        party_ids = (
            facts[spec["party_ids_fact"]]
            if "party_ids_fact" in spec
            else spec["party_ids"]
        )
        parties = [
            {"party_type": spec["party_type"], "party_id": party_id}
            for party_id in party_ids
        ]
        refund = (
            facts[spec["refund_fact"]]
            if "refund_fact" in spec
            else spec["refund_value"]
        )
        secondary = [
            issue
            for issue in SECONDARY_SEQUENCE
            if facts["secondary_conditions"][issue]
        ]
        actions = list(spec["actions"])
        actions.extend(
            action
            for condition, excluded_primary, action in SUPPLEMENTAL_ACTIONS
            if facts["secondary_conditions"][condition] and primary != excluded_primary
        )
        return {
            "primary": primary,
            "cause": spec["cause"],
            "parties": parties,
            "refund": refund,
            "secondary": secondary,
            "actions": actions,
            "case_status": "action_required" if refund > 0 else "no_action",
            "confidence": 1.0,
        }

    def trace_metadata(self) -> dict[str, Any]:
        return self.client.last_metadata.copy()


def build_policy_agent(root: Path) -> AIPolicyAgent:
    load_dotenv(root / ".env")
    return AIPolicyAgent(GroqJSONClient(os.environ.get("GROQ_API_KEY", "")))

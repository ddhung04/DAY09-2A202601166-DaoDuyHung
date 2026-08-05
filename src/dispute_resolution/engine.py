"""Deterministic multi-agent pipeline for the EC_POLICY_V2 assignment."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


MODEL_NAME = "deterministic_ec_policy_v2"
MODEL_PARAMETER_SIZE = "N/A (rule engine, no language model)"
POLICY_VERSION = "EC_POLICY_V2"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
ZERO = Decimal("0.00")
TWO_DECIMALS = Decimal("0.01")
RECONCILIATION_TOLERANCE = Decimal("0.10")

PRIMARY_CAUSES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_AFTER_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERED_AFTER_ESTIMATE",
    "valid_split_payment": "MULTIPLE_PAYMENTS_RECONCILED",
    "unsupported_late_claim": "DELIVERY_WITHIN_ESTIMATE",
}
SECONDARY_ORDER = (
    "multi_item_order",
    "multi_seller_order",
    "split_payment",
    "repeat_customer",
    "multiple_categories",
)
PRIMARY_ACTIONS = {
    "canceled_order_paid": "issue_full_refund",
    "unavailable_order_paid": "issue_full_refund",
    "late_delivery_seller": "refund_freight",
    "late_delivery_logistics": "refund_freight",
    "valid_split_payment": "explain_valid_split_payment",
    "unsupported_late_claim": "reject_late_refund",
}
TOP_LEVEL_FIELDS = {
    "case_id",
    "case_assessment",
    "affected_entities",
    "customer_context",
    "product_context",
    "delivery_analysis",
    "payment_reconciliation",
    "root_cause_analysis",
    "evidence_ids",
    "financial_resolution",
    "resolution_actions",
}
EVIDENCE_PATTERN = re.compile(
    r"^(?:order:[0-9a-f]{32}|item:[0-9a-f]{32}:\d+|payment:[0-9a-f]{32}:\d+|"
    r"seller:[0-9a-f]{32}|policy:[A-Z_]+)$"
)


class PolicyError(ValueError):
    """Raised when a case does not satisfy any explicit EC_POLICY_V2 branch."""


def parse_date(value: str | None) -> datetime | None:
    return datetime.strptime(value, DATE_FORMAT) if value else None


def hours_between(later: str | None, earlier: str | None) -> float | None:
    if not later or not earlier:
        return None
    seconds = Decimal(str((parse_date(later) - parse_date(earlier)).total_seconds()))
    return float((seconds / Decimal("3600")).quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP))


def rounded_money(value: Decimal) -> float:
    return float(value.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP))


def decimal_sum(rows: Iterable[dict[str, str]], field: str) -> Decimal:
    return sum((Decimal(row[field]) for row in rows), start=ZERO)


def stable_unique(values: Iterable[str | None], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
            if limit is not None and len(result) == limit:
                break
    return result


def iter_csv(data_dir: Path, name: str) -> Iterable[dict[str, str]]:
    """Stream rows and normalize an optional UTF-8 BOM in the supplied files."""
    with (data_dir / name).open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


@dataclass(frozen=True)
class OlistData:
    orders: dict[str, dict[str, str]]
    customers: dict[str, dict[str, str]]
    items_by_order: dict[str, list[dict[str, str]]]
    payments_by_order: dict[str, list[dict[str, str]]]
    products: dict[str, dict[str, str]]
    category_translations: dict[str, str]
    orders_by_customer: dict[str, list[str]]

    @classmethod
    def from_directory(cls, data_dir: Path) -> "OlistData":
        customers = {
            row["customer_id"]: row
            for row in iter_csv(data_dir, "olist_customers_dataset.csv")
        }
        orders = {
            row["order_id"]: row
            for row in iter_csv(data_dir, "olist_orders_dataset.csv")
        }
        products = {
            row["product_id"]: row
            for row in iter_csv(data_dir, "olist_products_dataset.csv")
        }
        translations = {
            row["product_category_name"]: row["product_category_name_english"]
            for row in iter_csv(data_dir, "product_category_name_translation.csv")
        }
        items_by_order: dict[str, list[dict[str, str]]] = defaultdict(list)
        payments_by_order: dict[str, list[dict[str, str]]] = defaultdict(list)
        orders_by_customer: dict[str, list[str]] = defaultdict(list)
        for row in iter_csv(data_dir, "olist_order_items_dataset.csv"):
            items_by_order[row["order_id"]].append(row)
        for row in iter_csv(data_dir, "olist_order_payments_dataset.csv"):
            payments_by_order[row["order_id"]].append(row)
        for order in orders.values():
            customer = customers.get(order["customer_id"])
            if customer is not None:
                orders_by_customer[customer["customer_unique_id"]].append(order["order_id"])
        return cls(
            orders=orders,
            customers=customers,
            items_by_order=dict(items_by_order),
            payments_by_order=dict(payments_by_order),
            products=products,
            category_translations=translations,
            orders_by_customer=dict(orders_by_customer),
        )


class CustomerAgent:
    """Read-only customer identity and order-history specialist."""

    def __init__(self, data: OlistData) -> None:
        self.data = data

    def analyze(
        self, customer: dict[str, str], order_id: str, include_history: bool
    ) -> dict[str, Any]:
        unique_id = customer["customer_unique_id"]
        related = []
        if include_history:
            related = [
                value
                for value in self.data.orders_by_customer.get(unique_id, [])
                if value != order_id
            ][:5]
        return {"customer_unique_id": unique_id, "related_order_ids": related}


class OrderProductAgent:
    """Read-only item, seller, product and category specialist."""

    def __init__(self, data: OlistData) -> None:
        self.data = data

    def analyze(
        self, items: list[dict[str, str]], include_product_context: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        all_product_ids = stable_unique(row["product_id"] for row in items)
        all_seller_ids = stable_unique(row["seller_id"] for row in items)
        categories: list[str | None] = []
        for product_id in all_product_ids:
            raw_category = self.data.products.get(product_id, {}).get("product_category_name")
            categories.append(self.data.category_translations.get(raw_category, raw_category))
        all_categories = stable_unique(categories)
        context = {
            "product_ids": all_product_ids[:5] if include_product_context else [],
            "category_names": all_categories[:5] if include_product_context else [],
        }
        facts = {
            "item_ids": [f"{row['order_id']}:{row['order_item_id']}" for row in items[:5]],
            "seller_ids": all_seller_ids[:3],
            "multi_item": len(items) >= 2,
            "multi_seller": len(all_seller_ids) >= 2,
            "multiple_categories": len(all_categories) >= 2,
        }
        return context, facts


class PaymentAgent:
    """Exact Decimal-based payment reconciliation specialist."""

    @staticmethod
    def analyze(items: list[dict[str, str]], payments: list[dict[str, str]]) -> dict[str, Any]:
        payment_total = decimal_sum(payments, "payment_value")
        common = {
            "currency": "BRL",
            "payment_total_brl": rounded_money(payment_total),
            "payment_types": stable_unique(row["payment_type"] for row in payments),
            "split_payment": len(payments) >= 2,
        }
        if not items:
            return {
                "currency": "BRL",
                "item_total_brl": 0.0,
                "freight_total_brl": 0.0,
                "expected_total_brl": None,
                "payment_total_brl": common["payment_total_brl"],
                "difference_brl": None,
                "reconciled": None,
                "payment_types": common["payment_types"],
                "split_payment": common["split_payment"],
            }
        item_total = decimal_sum(items, "price")
        freight_total = decimal_sum(items, "freight_value")
        expected = item_total + freight_total
        difference = payment_total - expected
        return {
            "currency": "BRL",
            "item_total_brl": rounded_money(item_total),
            "freight_total_brl": rounded_money(freight_total),
            "expected_total_brl": rounded_money(expected),
            "payment_total_brl": common["payment_total_brl"],
            "difference_brl": rounded_money(difference),
            "reconciled": abs(difference) <= RECONCILIATION_TOLERANCE,
            "payment_types": common["payment_types"],
            "split_payment": common["split_payment"],
        }


class DeliveryAgent:
    """Delivery deadline and seller handoff specialist."""

    @staticmethod
    def analyze(order: dict[str, str], items: list[dict[str, str]]) -> dict[str, Any]:
        seller_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in items:
            seller_rows[item["seller_id"]].append(item)
        handoffs = []
        for seller_id, seller_items in seller_rows.items():
            limits = [
                row["shipping_limit_date"]
                for row in seller_items
                if row["shipping_limit_date"]
            ]
            earliest_limit = min(limits) if limits else None
            variance = hours_between(order["order_delivered_carrier_date"], earliest_limit)
            handoffs.append(
                {
                    "seller_id": seller_id,
                    "shipping_limit_at": earliest_limit,
                    "handoff_variance_hours": variance,
                    "late_handoff": variance is not None and variance > 0,
                }
            )
        late_sellers = [entry["seller_id"] for entry in handoffs if entry["late_handoff"]]
        delivery_variance = hours_between(
            order["order_delivered_customer_date"], order["order_estimated_delivery_date"]
        )
        return {
            "delivered_at": order["order_delivered_customer_date"] or None,
            "estimated_delivery_at": order["order_estimated_delivery_date"] or None,
            "carrier_handoff_at": order["order_delivered_carrier_date"] or None,
            "delivery_variance_hours": delivery_variance,
            "seller_handoff_analysis": handoffs,
            "late_handoff_seller_ids": late_sellers,
            "late_delivery": delivery_variance is not None and delivery_variance > 0,
        }


class PolicyAgent:
    """Strict EC_POLICY_V2 decision specialist; unmatched cases are rejected."""

    @staticmethod
    def decide(
        order: dict[str, str],
        item_facts: dict[str, Any],
        payment: dict[str, Any],
        delivery: dict[str, Any],
        customer: dict[str, Any],
    ) -> dict[str, Any]:
        payment_total = payment["payment_total_brl"]
        if order["order_status"] == "canceled" and payment_total > 0:
            primary = "canceled_order_paid"
            parties = [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
            refund = payment_total
        elif order["order_status"] == "unavailable" and payment_total > 0:
            primary = "unavailable_order_paid"
            parties = [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
            refund = payment_total
        elif delivery["late_delivery"] and delivery["late_handoff_seller_ids"]:
            primary = "late_delivery_seller"
            parties = [
                {"party_type": "seller", "party_id": seller_id}
                for seller_id in delivery["late_handoff_seller_ids"][:3]
            ]
            refund = payment["freight_total_brl"]
        elif delivery["late_delivery"]:
            primary = "late_delivery_logistics"
            parties = [
                {
                    "party_type": "logistics_provider",
                    "party_id": "LOGISTICS_PROVIDER",
                }
            ]
            refund = payment["freight_total_brl"]
        elif payment["split_payment"] and payment["reconciled"] is True:
            primary = "valid_split_payment"
            parties = []
            refund = 0.0
        elif (
            delivery["delivery_variance_hours"] is not None
            and delivery["delivery_variance_hours"] <= 0
            and payment["reconciled"] is True
        ):
            primary = "unsupported_late_claim"
            parties = []
            refund = 0.0
        else:
            raise PolicyError(
                f"order {order['order_id']} does not match an EC_POLICY_V2 primary issue"
            )

        secondary = []
        conditions = {
            "multi_item_order": item_facts["multi_item"],
            "multi_seller_order": item_facts["multi_seller"],
            "split_payment": payment["split_payment"],
            "repeat_customer": bool(customer["related_order_ids"]),
            "multiple_categories": item_facts["multiple_categories"],
        }
        for issue in SECONDARY_ORDER:
            if conditions[issue]:
                secondary.append(issue)

        actions = [PRIMARY_ACTIONS[primary]]
        if primary == "late_delivery_seller":
            actions.append("review_seller_handoff")
        elif primary == "late_delivery_logistics":
            actions.append("review_carrier_delay")
        if primary in {"canceled_order_paid", "unavailable_order_paid"}:
            actions.append("verify_refund_completion")
        if item_facts["multi_seller"]:
            actions.append("coordinate_multi_seller_case")
        if payment["split_payment"] and primary != "valid_split_payment":
            actions.append("verify_payment_allocation")
        return {
            "primary": primary,
            "cause": PRIMARY_CAUSES[primary],
            "parties": parties,
            "refund": refund,
            "secondary": secondary,
            "actions": actions[:5],
        }


class VerifierAgent:
    """Hard schema and cross-field gate executed before any output is written."""

    @staticmethod
    def validate(result: dict[str, Any]) -> None:
        validate_output(result)


class CaseResolver:
    """Coordinator that calls specialist agents and aggregates their handoffs."""

    def __init__(self, data: OlistData) -> None:
        self.data = data
        self.customer_agent = CustomerAgent(data)
        self.order_product_agent = OrderProductAgent(data)
        self.payment_agent = PaymentAgent()
        self.delivery_agent = DeliveryAgent()
        self.policy_agent = PolicyAgent()
        self.verifier_agent = VerifierAgent()

    def resolve(self, case: dict[str, Any]) -> dict[str, Any]:
        if case.get("policy_version") != POLICY_VERSION:
            raise ValueError(f"unsupported policy version: {case.get('policy_version')}")
        order_id = case["customer_request"]["claimed_order_id"]
        order = self.data.orders.get(order_id)
        if order is None:
            raise ValueError(f"claimed order does not exist: {order_id}")
        customer = self.data.customers.get(order["customer_id"])
        if customer is None:
            raise ValueError(f"customer does not exist: {order['customer_id']}")
        items = self.data.items_by_order.get(order_id, [])
        payments = self.data.payments_by_order.get(order_id, [])
        scope = case.get("investigation_scope", {})

        customer_context = self.customer_agent.analyze(
            customer, order_id, scope.get("include_customer_history", False)
        )
        product_context, item_facts = self.order_product_agent.analyze(
            items, scope.get("include_product_context", False)
        )
        payment = self.payment_agent.analyze(items, payments)
        delivery = self.delivery_agent.analyze(order, items)
        policy = self.policy_agent.decide(order, item_facts, payment, delivery, customer_context)
        result = self._compose(
            case,
            order,
            customer_context,
            product_context,
            item_facts,
            payments,
            payment,
            delivery,
            policy,
        )
        self.verifier_agent.validate(result)
        return result

    @staticmethod
    def _compose(
        case: dict[str, Any],
        order: dict[str, str],
        customer: dict[str, Any],
        products: dict[str, Any],
        items: dict[str, Any],
        payments: list[dict[str, str]],
        payment: dict[str, Any],
        delivery: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        order_id = order["order_id"]
        payment_ids = [f"{order_id}:{row['payment_sequential']}" for row in payments[:5]]
        evidence = [f"order:{order_id}"]
        evidence.extend(f"item:{item_id}" for item_id in items["item_ids"])
        evidence.extend(f"payment:{payment_id}" for payment_id in payment_ids)
        evidence.extend(
            f"seller:{party['party_id']}"
            for party in policy["parties"]
            if party["party_type"] == "seller"
        )
        evidence.append(f"policy:{policy['cause']}")
        public_delivery = {
            key: value
            for key, value in delivery.items()
            if key != "late_delivery"
        }
        public_delivery["seller_handoff_analysis"] = public_delivery["seller_handoff_analysis"][:3]
        public_delivery["late_handoff_seller_ids"] = public_delivery["late_handoff_seller_ids"][:3]
        return {
            "case_id": case["case_id"],
            "case_assessment": {
                "primary_issue": policy["primary"],
                "secondary_issues": policy["secondary"],
                "case_status": "action_required" if policy["refund"] > 0 else "no_action",
                "confidence": 1.0,
            },
            "affected_entities": {
                "order_ids": [order_id],
                "item_ids": items["item_ids"],
                "seller_ids": items["seller_ids"],
                "payment_ids": payment_ids,
            },
            "customer_context": customer,
            "product_context": products,
            "delivery_analysis": public_delivery,
            "payment_reconciliation": {
                key: value for key, value in payment.items() if key != "split_payment"
            },
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": policy["cause"], "rank": 1}],
                "responsible_parties": policy["parties"],
            },
            "evidence_ids": evidence[:20],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": policy["refund"],
            },
            "resolution_actions": policy["actions"],
        }


def _require_exact_fields(value: Any, fields: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    if set(value) != fields:
        raise ValueError(f"{location} fields do not match schema")
    return value


def _require_list(value: Any, location: str, limit: int, unique: bool = True) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{location} must be a list with at most {limit} values")
    if unique and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
        raise ValueError(f"{location} contains duplicate values")
    return value


def _require_number(value: Any, location: str, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{location} must be numeric")
    if round(value, 2) != value:
        raise ValueError(f"{location} must be rounded to two decimal places")


def validate_output(result: dict[str, Any]) -> None:
    """Validate the complete submission schema and important cross-field invariants."""
    _require_exact_fields(result, TOP_LEVEL_FIELDS, "output")
    if not isinstance(result["case_id"], str) or not re.fullmatch(r"EC_\d{3}", result["case_id"]):
        raise ValueError("case_id must use EC_NNN format")

    assessment = _require_exact_fields(
        result["case_assessment"],
        {"primary_issue", "secondary_issues", "case_status", "confidence"},
        "case_assessment",
    )
    primary = assessment["primary_issue"]
    if primary not in PRIMARY_CAUSES:
        raise ValueError("unknown primary_issue")
    secondary = _require_list(assessment["secondary_issues"], "secondary_issues", 5)
    if any(issue not in SECONDARY_ORDER for issue in secondary):
        raise ValueError("unknown secondary issue")
    if secondary != [issue for issue in SECONDARY_ORDER if issue in secondary]:
        raise ValueError("secondary issues are not in policy order")
    if assessment["case_status"] not in {"action_required", "no_action"}:
        raise ValueError("invalid case_status")
    _require_number(assessment["confidence"], "confidence")
    if not 0 <= assessment["confidence"] <= 1:
        raise ValueError("confidence must be in [0, 1]")

    entities = _require_exact_fields(
        result["affected_entities"],
        {"order_ids", "item_ids", "seller_ids", "payment_ids"},
        "affected_entities",
    )
    for key, limit in (("order_ids", 5), ("item_ids", 5), ("seller_ids", 3), ("payment_ids", 5)):
        values = _require_list(entities[key], f"affected_entities.{key}", limit)
        if any(not isinstance(value, str) for value in values):
            raise ValueError(f"affected_entities.{key} must contain strings")
    if len(entities["order_ids"]) != 1:
        raise ValueError("each case must affect exactly its claimed order")

    customer = _require_exact_fields(
        result["customer_context"], {"customer_unique_id", "related_order_ids"}, "customer_context"
    )
    if not isinstance(customer["customer_unique_id"], str):
        raise ValueError("customer_unique_id must be a string")
    _require_list(customer["related_order_ids"], "related_order_ids", 5)

    products = _require_exact_fields(
        result["product_context"], {"product_ids", "category_names"}, "product_context"
    )
    _require_list(products["product_ids"], "product_ids", 5)
    _require_list(products["category_names"], "category_names", 5)

    delivery = _require_exact_fields(
        result["delivery_analysis"],
        {
            "delivered_at", "estimated_delivery_at", "carrier_handoff_at",
            "delivery_variance_hours", "seller_handoff_analysis", "late_handoff_seller_ids",
        },
        "delivery_analysis",
    )
    for key in ("delivered_at", "estimated_delivery_at", "carrier_handoff_at"):
        if delivery[key] is not None:
            parse_date(delivery[key])
    _require_number(delivery["delivery_variance_hours"], "delivery_variance_hours", nullable=True)
    handoffs = _require_list(delivery["seller_handoff_analysis"], "seller_handoff_analysis", 3)
    for entry in handoffs:
        handoff = _require_exact_fields(
            entry,
            {"seller_id", "shipping_limit_at", "handoff_variance_hours", "late_handoff"},
            "seller_handoff_analysis[]",
        )
        if handoff["shipping_limit_at"] is not None:
            parse_date(handoff["shipping_limit_at"])
        _require_number(handoff["handoff_variance_hours"], "handoff_variance_hours", nullable=True)
        if not isinstance(handoff["late_handoff"], bool):
            raise ValueError("late_handoff must be boolean")
    late_sellers = _require_list(delivery["late_handoff_seller_ids"], "late_handoff_seller_ids", 3)
    if any(seller not in [entry["seller_id"] for entry in handoffs] for seller in late_sellers):
        raise ValueError("late_handoff_seller_ids must exist in seller_handoff_analysis")

    payment = _require_exact_fields(
        result["payment_reconciliation"],
        {
            "currency", "item_total_brl", "freight_total_brl", "expected_total_brl",
            "payment_total_brl", "difference_brl", "reconciled", "payment_types",
        },
        "payment_reconciliation",
    )
    if payment["currency"] != "BRL":
        raise ValueError("payment currency must be BRL")
    for key in ("item_total_brl", "freight_total_brl", "payment_total_brl"):
        _require_number(payment[key], key)
    for key in ("expected_total_brl", "difference_brl"):
        _require_number(payment[key], key, nullable=True)
    if payment["reconciled"] is not None and not isinstance(payment["reconciled"], bool):
        raise ValueError("reconciled must be boolean or null")
    _require_list(payment["payment_types"], "payment_types", 5)
    null_triplet = (
        payment["expected_total_brl"],
        payment["difference_brl"],
        payment["reconciled"],
    )
    if any(value is None for value in null_triplet) and not all(
        value is None for value in null_triplet
    ):
        raise ValueError("expected_total, difference and reconciled must become null together")

    root_cause = _require_exact_fields(
        result["root_cause_analysis"],
        {"ranked_causes", "responsible_parties"},
        "root_cause_analysis",
    )
    causes = _require_list(root_cause["ranked_causes"], "ranked_causes", 3)
    if causes != [{"cause_code": PRIMARY_CAUSES[primary], "rank": 1}]:
        raise ValueError("root cause must match the primary issue")
    parties = _require_list(root_cause["responsible_parties"], "responsible_parties", 3)
    for party in parties:
        _require_exact_fields(party, {"party_type", "party_id"}, "responsible_parties[]")

    evidence = _require_list(result["evidence_ids"], "evidence_ids", 20)
    if any(
        not isinstance(value, str) or not EVIDENCE_PATTERN.fullmatch(value)
        for value in evidence
    ):
        raise ValueError("invalid evidence ID")
    if f"policy:{PRIMARY_CAUSES[primary]}" not in evidence:
        raise ValueError("primary policy evidence is missing")

    financial = _require_exact_fields(
        result["financial_resolution"],
        {"currency", "recommended_refund_brl"},
        "financial_resolution",
    )
    if financial["currency"] != "BRL":
        raise ValueError("refund currency must be BRL")
    _require_number(financial["recommended_refund_brl"], "recommended_refund_brl")
    refund = financial["recommended_refund_brl"]
    if (refund > 0) != (assessment["case_status"] == "action_required"):
        raise ValueError("case_status and refund amount disagree")

    actions = _require_list(result["resolution_actions"], "resolution_actions", 5)
    if not actions or actions[0] != PRIMARY_ACTIONS[primary]:
        raise ValueError("primary resolution action is missing or out of order")


def load_case(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)

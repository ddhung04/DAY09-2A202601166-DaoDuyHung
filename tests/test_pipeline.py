from __future__ import annotations

import json
import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path

from dispute_resolution.cli import project_root, run_preflight, source_consistency_errors
from dispute_resolution.ai_policy import AIPolicyAgent
from dispute_resolution.engine import (
    CaseResolver,
    OlistData,
    hours_between,
    load_case,
    validate_output,
)


class RecordedPolicyAgent:
    """Offline test double populated from the checked output fixtures."""

    def __init__(self, root: Path) -> None:
        self.decisions = {}
        for output_path in (root / "output").glob("EC_*.json"):
            result = json.loads(output_path.read_text(encoding="utf-8"))
            order_id = result["affected_entities"]["order_ids"][0]
            self.decisions[order_id] = {
                "primary": result["case_assessment"]["primary_issue"],
                "cause": result["root_cause_analysis"]["ranked_causes"][0]["cause_code"],
                "parties": result["root_cause_analysis"]["responsible_parties"],
                "refund": result["financial_resolution"]["recommended_refund_brl"],
                "secondary": result["case_assessment"]["secondary_issues"],
                "actions": result["resolution_actions"],
                "case_status": result["case_assessment"]["case_status"],
                "confidence": result["case_assessment"]["confidence"],
            }

    def decide(self, order, item_facts, payment, delivery, customer):
        return deepcopy(self.decisions[order["order_id"]])


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = project_root()
        cls.data = OlistData.from_directory(cls.root / "data")
        cls.resolver = CaseResolver(cls.data, RecordedPolicyAgent(cls.root))

    def test_repository_preflight(self) -> None:
        self.assertEqual(run_preflight(self.root), 0)

    def test_half_up_hour_rounding(self) -> None:
        self.assertEqual(
            hours_between("2018-01-01 01:00:18", "2018-01-01 00:00:00"),
            1.01,
        )

    def test_all_cases_resolve_and_match_source(self) -> None:
        counts: Counter[str] = Counter()
        for number in range(1, 51):
            name = f"EC_{number:03d}.json"
            case = load_case(self.root / "input" / name)
            result = self.resolver.resolve(case)
            validate_output(result)
            self.assertEqual(source_consistency_errors(result, case, self.data), [])
            counts[result["case_assessment"]["primary_issue"]] += 1
        self.assertEqual(
            counts,
            Counter(
                {
                    "canceled_order_paid": 8,
                    "unavailable_order_paid": 6,
                    "late_delivery_seller": 10,
                    "late_delivery_logistics": 10,
                    "valid_split_payment": 8,
                    "unsupported_late_claim": 8,
                }
            ),
        )

    def test_no_item_cases_use_required_null_triplet(self) -> None:
        checked = 0
        for number in range(1, 51):
            case = load_case(self.root / "input" / f"EC_{number:03d}.json")
            order_id = case["customer_request"]["claimed_order_id"]
            if self.data.items_by_order.get(order_id):
                continue
            payment = self.resolver.resolve(case)["payment_reconciliation"]
            self.assertEqual(payment["item_total_brl"], 0.0)
            self.assertEqual(payment["freight_total_brl"], 0.0)
            self.assertIsNone(payment["expected_total_brl"])
            self.assertIsNone(payment["difference_brl"])
            self.assertIsNone(payment["reconciled"])
            checked += 1
        self.assertGreater(checked, 0)

    def test_product_categories_keep_source_values(self) -> None:
        case = load_case(self.root / "input" / "EC_002.json")
        result = self.resolver.resolve(case)
        self.assertEqual(result["product_context"]["category_names"], ["esporte_lazer"])

    def test_stored_outputs_are_valid_json(self) -> None:
        names = sorted(path.name for path in (self.root / "output").glob("*.json"))
        self.assertEqual(names, [f"EC_{number:03d}.json" for number in range(1, 51)])
        for name in names:
            with (self.root / "output" / name).open(encoding="utf-8") as handle:
                validate_output(json.load(handle))

    def test_ai_policy_agent_uses_model_json(self) -> None:
        class FakeClient:
            last_metadata = {"provider": "fake", "model": "fake-8b"}

            def generate(self, facts):
                self.facts = facts
                return {"primary": "late_delivery_seller"}

        client = FakeClient()
        agent = AIPolicyAgent(client)
        decision = agent.decide(
            {"order_id": "0" * 32, "order_status": "delivered"},
            {"multi_item": False, "multi_seller": False, "multiple_categories": False},
            {
                "payment_total_brl": 212.27,
                "freight_total_brl": 18.27,
                "split_payment": True,
                "reconciled": True,
            },
            {
                "late_delivery": True,
                "late_handoff_seller_ids": ["a" * 32],
                "delivery_variance_hours": 87.39,
            },
            {"related_order_ids": []},
        )
        self.assertEqual(decision["primary"], "late_delivery_seller")
        self.assertTrue(client.facts["primary_conditions"]["late_delivery_seller"])
        self.assertEqual(decision["refund"], 18.27)
        self.assertEqual(decision["secondary"], ["split_payment"])


if __name__ == "__main__":
    unittest.main()

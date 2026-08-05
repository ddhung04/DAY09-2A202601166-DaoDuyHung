from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path

from dispute_resolution.cli import project_root, run_preflight, source_consistency_errors
from dispute_resolution.engine import (
    CaseResolver,
    OlistData,
    PolicyAgent,
    PolicyError,
    hours_between,
    load_case,
    validate_output,
)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = project_root()
        cls.data = OlistData.from_directory(cls.root / "data")
        cls.resolver = CaseResolver(cls.data)

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

    def test_policy_rejects_an_unmatched_case(self) -> None:
        with self.assertRaises(PolicyError):
            PolicyAgent.decide(
                {"order_id": "0" * 32, "order_status": "delivered"},
                {"multi_item": False, "multi_seller": False, "multiple_categories": False},
                {
                    "payment_total_brl": 10.0,
                    "freight_total_brl": 1.0,
                    "split_payment": False,
                    "reconciled": False,
                },
                {
                    "late_delivery": False,
                    "late_handoff_seller_ids": [],
                    "delivery_variance_hours": -1.0,
                },
                {"related_order_ids": []},
            )


if __name__ == "__main__":
    unittest.main()

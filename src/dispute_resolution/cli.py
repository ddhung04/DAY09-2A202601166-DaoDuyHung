"""Command-line entry point for the AI-backed dispute-resolution pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from dispute_resolution.ai_policy import (
    AIConfigurationError,
    AIResponseError,
    MODEL_PARAMETER_SIZE,
    MODEL_PROVIDER,
    build_policy_agent,
)
from dispute_resolution.engine import (
    MODEL_NAME,
    POLICY_VERSION,
    PRIMARY_CAUSES,
    CaseResolver,
    OlistData,
    load_case,
    stable_unique,
    validate_output,
)


REQUIRED_DATASETS = (
    "olist_customers_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_order_reviews_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
    "olist_geolocation_dataset.csv",
    "product_category_name_translation.csv",
)


def project_root() -> Path:
    """Return the repository root when installed in editable mode or run from source."""
    return Path(__file__).resolve().parents[2]


def validate_case(path: Path) -> list[str]:
    """Return schema-level errors for one case input without processing customer data."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{path.name}: cannot read JSON ({error})"]

    errors: list[str] = []
    if payload.get("case_id") != path.stem:
        errors.append(f"{path.name}: case_id must equal {path.stem}")
    request = payload.get("customer_request")
    if not isinstance(request, dict) or not isinstance(request.get("claimed_order_id"), str):
        errors.append(f"{path.name}: missing customer_request.claimed_order_id")
    scope = payload.get("investigation_scope")
    if not isinstance(scope, dict) or not all(
        isinstance(scope.get(key), bool)
        for key in ("include_customer_history", "include_product_context")
    ):
        errors.append(f"{path.name}: investigation_scope flags must be boolean")
    if payload.get("policy_version") != POLICY_VERSION:
        errors.append(f"{path.name}: policy_version must be {POLICY_VERSION}")
    return errors


def run_preflight(root: Path) -> int:
    """Verify required source data and all 50 expected case files."""
    errors: list[str] = []
    data_dir = root / "data"
    input_dir = root / "input"
    for dataset in REQUIRED_DATASETS:
        if not (data_dir / dataset).is_file():
            errors.append(f"missing dataset: data/{dataset}")

    expected_cases = [input_dir / f"EC_{number:03d}.json" for number in range(1, 51)]
    for case_path in expected_cases:
        if not case_path.is_file():
            errors.append(f"missing case: input/{case_path.name}")
        else:
            errors.extend(validate_case(case_path))

    if errors:
        print("Preflight failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Preflight passed: 9 datasets and 50 EC_POLICY_V2 cases are ready.")
    return 0


def run_batch(root: Path) -> int:
    """Resolve all cases with AI, then atomically replace outputs and audit files."""
    if run_preflight(root) != 0:
        return 1
    data = OlistData.from_directory(root / "data")
    resolver = CaseResolver(data, build_policy_agent(root))
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    trace_events: list[dict[str, Any]] = []
    pending_outputs: list[tuple[Path, str]] = []
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for number in range(1, 51):
        case_path = root / "input" / f"EC_{number:03d}.json"
        case = load_case(case_path)
        result = resolver.resolve(case)
        semantic_errors = source_consistency_errors(result, case, data)
        if semantic_errors:
            raise RuntimeError(
                f"AI policy decision failed verification for {case['case_id']}: {semantic_errors}"
            )
        target = output_dir / case_path.name
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        pending_outputs.append((target, serialized))
        model_call = resolver.policy_agent.trace_metadata()
        usage = model_call.get("usage", {})
        for key in token_usage:
            value = usage.get(key, 0)
            if isinstance(value, int):
                token_usage[key] += value
        common = {"case_id": case["case_id"], "model": MODEL_NAME}
        trace_events.extend(
            [
                {
                    **common,
                    "sequence": 1,
                    "agent": "coordinator",
                    "event": "case_received",
                    "order_id": result["affected_entities"]["order_ids"][0],
                },
                {
                    **common,
                    "sequence": 2,
                    "agent": "customer",
                    "event": "handoff",
                    "related_order_count": len(
                        result["customer_context"]["related_order_ids"]
                    ),
                },
                {
                    **common,
                    "sequence": 3,
                    "agent": "order_product",
                    "event": "handoff",
                    "item_count": len(result["affected_entities"]["item_ids"]),
                    "seller_count": len(result["affected_entities"]["seller_ids"]),
                },
                {
                    **common,
                    "sequence": 4,
                    "agent": "payment",
                    "event": "handoff",
                    "reconciled": result["payment_reconciliation"]["reconciled"],
                },
                {
                    **common,
                    "sequence": 5,
                    "agent": "delivery",
                    "event": "handoff",
                    "delivery_variance_hours": result["delivery_analysis"][
                        "delivery_variance_hours"
                    ],
                    "late_seller_count": len(
                        result["delivery_analysis"]["late_handoff_seller_ids"]
                    ),
                },
                {
                    **common,
                    "sequence": 6,
                    "agent": "policy",
                    "event": "decision",
                    "provider": MODEL_PROVIDER,
                    "primary_issue": result["case_assessment"]["primary_issue"],
                    "refund_brl": result["financial_resolution"][
                        "recommended_refund_brl"
                    ],
                    "model_call": model_call,
                },
                {
                    **common,
                    "sequence": 7,
                    "agent": "verifier",
                    "event": "schema_validated",
                    "output": str(target.relative_to(root)),
                },
            ]
        )
    # Do not damage a previously valid submission if an API call or verification
    # fails midway: persistent files are only replaced after all 50 cases pass.
    for target, serialized in pending_outputs:
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(target)

    trace_content = "".join(json.dumps(event) + "\n" for event in trace_events)
    for trace_path in (root / "trace.jsonl", root / "logging" / "trace.jsonl"):
        trace_path.parent.mkdir(exist_ok=True)
        temporary = trace_path.with_suffix(".jsonl.tmp")
        temporary.write_text(trace_content, encoding="utf-8")
        temporary.replace(trace_path)

    metadata = {
        "model": MODEL_NAME,
        "provider": MODEL_PROVIDER,
        "parameter_size": MODEL_PARAMETER_SIZE,
        "parameter_size_unit": "parameters",
        "uses_language_model": True,
        "framework": "custom Python hybrid multi-agent pipeline",
        "runtime": f"Python {platform.python_version()} on {platform.system()}",
        "policy_version": POLICY_VERSION,
        "api_key_env": "GROQ_API_KEY",
        "model_name_location": "src/dispute_resolution/ai_policy.py",
        "agents": [
            "coordinator",
            "customer",
            "order_product",
            "payment",
            "delivery",
            "policy_ai",
            "verifier",
        ],
        "case_count": 50,
        "model_call_count": 50,
        "token_usage": token_usage,
        "trace_event_count": len(trace_events),
        "trace_path": "trace.jsonl",
        "output_archive": "output.zip",
        "run_status": "completed",
        "secrets_in_metadata": False,
    }
    metadata_content = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    for metadata_path in (root / "metadata.json", root / "logging" / "metadata.json"):
        metadata_path.parent.mkdir(exist_ok=True)
        temporary = metadata_path.with_suffix(".json.tmp")
        temporary.write_text(metadata_content, encoding="utf-8")
        temporary.replace(metadata_path)
    print(f"Resolved and validated {len(trace_events) // 7} cases.")
    return 0


def source_consistency_errors(
    result: dict[str, Any], case: dict[str, Any], data: OlistData
) -> list[str]:
    """Independently audit high-value output fields against source rows and policy predicates."""
    errors: list[str] = []
    order_id = case["customer_request"]["claimed_order_id"]
    order = data.orders[order_id]
    items = data.items_by_order.get(order_id, [])
    payments = data.payments_by_order.get(order_id, [])
    customer = data.customers[order["customer_id"]]
    entities = result["affected_entities"]
    expected_items = [f"{order_id}:{row['order_item_id']}" for row in items[:5]]
    expected_sellers = stable_unique((row["seller_id"] for row in items), limit=3)
    expected_payments = [f"{order_id}:{row['payment_sequential']}" for row in payments[:5]]
    if entities["order_ids"] != [order_id]:
        errors.append("affected order does not match claimed_order_id")
    if entities["item_ids"] != expected_items:
        errors.append("affected item IDs do not match source order")
    if entities["seller_ids"] != expected_sellers:
        errors.append("affected seller IDs do not match source order")
    if entities["payment_ids"] != expected_payments:
        errors.append("affected payment IDs do not match source order")

    customer_context = result["customer_context"]
    unique_id = customer["customer_unique_id"]
    expected_related = [
        value for value in data.orders_by_customer.get(unique_id, []) if value != order_id
    ][:5]
    if customer_context != {
        "customer_unique_id": unique_id,
        "related_order_ids": expected_related,
    }:
        errors.append("customer context does not match source history")

    all_products = stable_unique(row["product_id"] for row in items)
    categories = []
    for product_id in all_products:
        raw = data.products.get(product_id, {}).get("product_category_name")
        categories.append(raw)
    expected_product_context = {
        "product_ids": all_products[:5],
        "category_names": stable_unique(categories, limit=5),
    }
    if result["product_context"] != expected_product_context:
        errors.append("product context does not match source products")

    assessment = result["case_assessment"]
    primary = assessment["primary_issue"]
    payment = result["payment_reconciliation"]
    delivery = result["delivery_analysis"]
    variance = delivery["delivery_variance_hours"]
    late = variance is not None and variance > 0
    split = len(payments) >= 2
    primary_conditions = {
        "canceled_order_paid": (
            order["order_status"] == "canceled" and payment["payment_total_brl"] > 0
        ),
        "unavailable_order_paid": (
            order["order_status"] == "unavailable" and payment["payment_total_brl"] > 0
        ),
        "late_delivery_seller": late and bool(delivery["late_handoff_seller_ids"]),
        "late_delivery_logistics": late and not delivery["late_handoff_seller_ids"],
        "valid_split_payment": split and payment["reconciled"] is True,
        "unsupported_late_claim": (
            variance is not None and variance <= 0 and payment["reconciled"] is True
        ),
    }
    expected_primary = next(
        (issue for issue, condition in primary_conditions.items() if condition),
        None,
    )
    if primary != expected_primary:
        errors.append(
            f"primary issue violates policy precedence; expected {expected_primary}"
        )

    expected_secondary = []
    if len(items) >= 2:
        expected_secondary.append("multi_item_order")
    if len(stable_unique(row["seller_id"] for row in items)) >= 2:
        expected_secondary.append("multi_seller_order")
    if split:
        expected_secondary.append("split_payment")
    if expected_related:
        expected_secondary.append("repeat_customer")
    if len(stable_unique(categories)) >= 2:
        expected_secondary.append("multiple_categories")
    if assessment["secondary_issues"] != expected_secondary:
        errors.append("secondary issues do not match source predicates")

    refund = result["financial_resolution"]["recommended_refund_brl"]
    if primary in {"canceled_order_paid", "unavailable_order_paid"}:
        expected_refund = payment["payment_total_brl"]
    elif primary in {"late_delivery_seller", "late_delivery_logistics"}:
        expected_refund = payment["freight_total_brl"]
    else:
        expected_refund = 0.0
    if refund != expected_refund:
        errors.append("recommended refund does not match policy")

    parties = result["root_cause_analysis"]["responsible_parties"]
    expected_evidence = [f"order:{order_id}"]
    expected_evidence.extend(f"item:{value}" for value in expected_items)
    expected_evidence.extend(f"payment:{value}" for value in expected_payments)
    expected_evidence.extend(
        f"seller:{party['party_id']}" for party in parties if party["party_type"] == "seller"
    )
    expected_evidence.append(f"policy:{PRIMARY_CAUSES[primary]}")
    if result["evidence_ids"] != expected_evidence:
        errors.append("evidence IDs do not match source entities and policy")
    return errors


def verify_batch(root: Path) -> int:
    """Validate stored AI outputs against source data without making new model calls."""
    data = OlistData.from_directory(root / "data")
    expected_names = {f"EC_{number:03d}.json" for number in range(1, 51)}
    output_dir = root / "output"
    actual_names = {path.name for path in output_dir.iterdir() if path.is_file()}
    if actual_names != expected_names:
        extras = sorted(actual_names - expected_names)
        missing = sorted(expected_names - actual_names)
        print(f"Verification failed: output directory mismatch; missing={missing}, extra={extras}")
        return 1
    for name in sorted(expected_names):
        case = load_case(root / "input" / name)
        with (root / "output" / name).open(encoding="utf-8") as handle:
            actual = json.load(handle)
        try:
            validate_output(actual)
        except ValueError as error:
            print(f"Verification failed: output/{name}: {error}")
            return 1
        semantic_errors = source_consistency_errors(actual, case, data)
        if semantic_errors:
            print(f"Verification failed: output/{name}: {semantic_errors}")
            return 1
    trace_path = root / "trace.jsonl"
    if not trace_path.is_file():
        trace_path = root / "logging" / "trace.jsonl"
    try:
        trace_lines = trace_path.read_text(encoding="utf-8").splitlines()
        trace_events = [json.loads(line) for line in trace_lines]
    except (OSError, json.JSONDecodeError) as error:
        print(f"Verification failed: invalid trace ({error}).")
        return 1
    if len(trace_lines) != 350:
        print("Verification failed: trace must contain seven events for each of 50 cases.")
        return 1
    agent_order = [
        "coordinator",
        "customer",
        "order_product",
        "payment",
        "delivery",
        "policy",
        "verifier",
    ]
    for number in range(1, 51):
        case_id = f"EC_{number:03d}"
        case_events = trace_events[(number - 1) * 7 : number * 7]
        if [event.get("case_id") for event in case_events] != [case_id] * 7:
            print(f"Verification failed: trace case sequence is invalid for {case_id}.")
            return 1
        if [event.get("agent") for event in case_events] != agent_order:
            print(f"Verification failed: agent handoff order is invalid for {case_id}.")
            return 1
        if [event.get("sequence") for event in case_events] != list(range(1, 8)):
            print(f"Verification failed: trace event sequence is invalid for {case_id}.")
            return 1
        if any(event.get("model") != MODEL_NAME for event in case_events):
            print(f"Verification failed: trace model is invalid for {case_id}.")
            return 1
    print("Verification passed: 50 outputs match the policy pipeline and trace has 350 events.")
    return 0


def package_submission(root: Path) -> int:
    """Create a reproducible root-level output.zip containing only the 50 JSON files."""
    if verify_batch(root) != 0:
        return 1
    target = root / "output.zip"
    temporary = root / "output.zip.tmp"
    expected_files = [f"EC_{number:03d}.json" for number in range(1, 51)]
    expected_names = [f"output/{name}" for name in expected_files]
    with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for file_name, archive_name in zip(expected_files, expected_names, strict=True):
            info = ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info,
                (root / "output" / file_name).read_bytes(),
                compresslevel=9,
            )
    temporary.replace(target)
    with ZipFile(target) as archive:
        if archive.namelist() != expected_names:
            print("Packaging failed: ZIP entry list is invalid.")
            return 1
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"Created output.zip with 50 JSON files; sha256={digest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Olist dispute-resolution tools")
    parser.add_argument(
        "command",
        choices=("preflight", "run", "verify", "package"),
        help="command to run",
    )
    parser.add_argument("--root", type=Path, default=project_root(), help="repository root")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            return run_preflight(args.root)
        if args.command == "run":
            return run_batch(args.root)
        if args.command == "verify":
            return verify_batch(args.root)
        return package_submission(args.root)
    except (AIConfigurationError, AIResponseError, RuntimeError, ValueError) as error:
        print(f"Command failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

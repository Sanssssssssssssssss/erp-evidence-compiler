from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.compiler_runtime.kernel import _facet_policy_refs
from app.compiler_runtime.models import ActionProposal, ProofNode, ProposedAction
from app.compiler_runtime.requirement_pack import (
    RequirementPack,
    registered_requirement_pack,
)
from app.compiler_runtime.runtime import EvidenceCompilerRuntime, prepare_sources

from tests.compiler_child.scenario_2032 import build_source_manifest


def test_requirement_pack_and_odoo_source_are_runtime_inputs() -> None:
    pack = RequirementPack.from_mapping(
        {
            "pack_id": "test.odoo.screening",
            "version": "1",
            "requirement_pack_version": "test-v1",
            "policy_version": "test-policy-v1",
            "policy_basis": {"status": "fixture"},
            "max_count": 25,
            "requirements": {
                "screening_valid": {
                    "kind": "field",
                    "owner": "evidence",
                    "default_required": True,
                    "required_policy_values": ["max_count"],
                }
            },
            "profiles": {"default": [{"id": "screening_valid", "required": True}]},
        }
    )
    content = '{"id":2032,"write_date":"2026-08-28 10:00:00"}'
    source = prepare_sources(
        [
            {
                "source_id": "sale.order:2032",
                "source_content": content,
                "source_fingerprint": hashlib.sha256(content.encode()).hexdigest(),
                "already_persisted": True,
                "evidence_type": "odoo_record",
                "observed_at": "2026-08-28T10:00:01Z",
                "write_date": "2026-08-28 10:00:00",
                "provenance": {
                    "odoo_model": "sale.order",
                    "runtime_admission": "must-not-override-runtime",
                },
            }
        ]
    )[0]

    assert pack.policy_excerpt_for(["screening_valid"])["values"]["max_count"]["value"] == 25
    assert source.source_id == "sale.order:2032"
    assert source.source_kind == "odoo_record"
    assert source.canonical_content == content
    assert source.upstream_revision == "2026-08-28 10:00:00"
    assert source.provenance["odoo_model"] == "sale.order"
    assert source.provenance["runtime_admission"] == "admitted"


def test_2032_fixture_excludes_oracle_and_requires_registered_plan() -> None:
    manifest = build_source_manifest()
    prepared = prepare_sources(manifest["sources"])
    source = prepared[0]
    proposal = ActionProposal(
        proposal_id="manager:test-hypothesis:r1",
        actions=[
            ProposedAction(
                record_ref=source.source_id,
                action="cancel_sales_order",
            )
        ],
        target_record_refs=[source.source_id],
        expected_preconditions={
            source.source_id: {"upstream_revision": source.upstream_revision}
        },
    )

    assert registered_requirement_pack("erp_bench.sales_order_acceptance.v1", "1").raw[
        "requires_action_proposal"
    ]
    assert len(proposal.target_record_refs) == 1
    assert all("optimal_plan" not in item.canonical_content for item in prepared)
    assert _facet_policy_refs(
        ProofNode(
            id="check.orders",
            kind="CHECK",
            statement="Every proposed action matches policy.",
            requirement_refs=["order_acceptance_plan_valid"],
            facet_refs=["order_decision"],
        ),
        "order_decision",
        registered_requirement_pack("erp_bench.sales_order_acceptance.v1", "1"),
    ) == {"minimum_quantity", "maximum_quantity"}

    runtime = EvidenceCompilerRuntime(
        SimpleNamespace(),
        settings=SimpleNamespace(),
        requirement_pack=registered_requirement_pack("erp_bench.sales_order_acceptance.v1", "1"),
    )
    with pytest.raises(ValueError, match="requires a registered ProofPlan"):
        runtime.run(
            active_requirement_ids=["order_acceptance_plan_valid"],
            prepared_sources=prepared[:-1],
            action_proposal=proposal,
        )

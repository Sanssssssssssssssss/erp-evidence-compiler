from __future__ import annotations

from typing import Iterable

from app.compiler_runtime.graph_walk import reachable_ids
from app.compiler_runtime.models import ProofNode, ProofPlan
from app.compiler_runtime.requirement_pack import DEFAULT_REQUIREMENT_PACK, RequirementPack
from app.proof_schema import ProofFacet, ProofPath, ProofSignature, ProofTerm, RootComposition


def proof_signatures(
    pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> tuple[ProofSignature, ...]:
    return pack.proof_signatures


def proof_signature_for(
    requirement_id: str,
    pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> ProofSignature | None:
    return pack.signature_for(requirement_id)


def proof_signature_hash_for(
    requirement_ids: Iterable[str],
    pack: RequirementPack = DEFAULT_REQUIREMENT_PACK,
) -> str:
    """Hash the active minimal type constraints, including the empty set."""

    return pack.signature_hash_for(list(requirement_ids))


class PlanConformanceGate:
    """Type-check a freely generated ProofPlan against minimal proof signatures."""

    def __init__(self, signatures: Iterable[ProofSignature] | None = None) -> None:
        selected = tuple(signatures) if signatures is not None else proof_signatures()
        requirement_ids = [signature.requirement_id for signature in selected]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("PlanConformanceGate requires at most one signature per requirement")
        self._signatures = selected

    def validate(self, plan: ProofPlan) -> None:
        nodes = {node.id: node for node in plan.nodes}
        for signature in self._signatures:
            if signature.requirement_id not in plan.active_requirement_ids:
                continue
            self._validate_signature(plan, nodes, signature)

    def _validate_signature(
        self,
        plan: ProofPlan,
        nodes: dict[str, ProofNode],
        signature: ProofSignature,
    ) -> None:
        root_id = plan.roots.get(signature.requirement_id)
        if not root_id or root_id not in nodes:
            raise ValueError(
                f"ProofPlan has no root for signature requirement {signature.requirement_id!r}"
            )
        reachable = reachable_ids(root_id, lambda node_id: nodes[node_id].depends_on)
        checks = [nodes[node_id] for node_id in reachable if nodes[node_id].kind == "CHECK"]

        allowed_roles_by_check: dict[str, set[str]] = {node.id: set() for node in checks}
        for node in checks:
            for declared_signature in self._signatures:
                if declared_signature.requirement_id not in node.requirement_refs:
                    continue
                for facet in declared_signature.facets:
                    if facet.id in node.facet_refs:
                        allowed_roles_by_check[node.id].update(facet.semantic_roles)
            unexpected = sorted(set(node.semantic_role_refs) - allowed_roles_by_check[node.id])
            if unexpected:
                raise ValueError(
                    f"CHECK {node.id!r} declares semantic roles outside its ProofSignature facets: "
                    f"{unexpected}"
                )

        for facet in signature.facets:
            facet_checks = [node for node in checks if facet.id in node.facet_refs]
            if not facet_checks:
                raise ValueError(
                    f"Required facet {facet.id!r} is not reachable from "
                    f"requirement root {signature.requirement_id!r}"
                )
            invalid_paths = {
                node.id: sorted(set(node.semantic_role_refs).intersection(facet.semantic_roles))
                for node in facet_checks
                if facet.path_for_roles(node.semantic_role_refs) is None
            }
            if invalid_paths:
                details = "; ".join(
                    f"CHECK {check_id!r} has roles {roles}"
                    for check_id, roles in sorted(invalid_paths.items())
                )
                raise ValueError(
                    f"Required facet {facet.id!r} CHECKs must select one complete proof path: "
                    f"{details}"
                )

            for path in facet.proof_paths:
                path_roles = frozenset(path.semantic_roles)
                path_checks = [
                    node
                    for node in facet_checks
                    if facet.path_for_roles(node.semantic_role_refs) == path
                ]
                if not path_checks:
                    raise ValueError(
                        f"Required facet {facet.id!r} is missing proof path roles "
                        f"{sorted(path_roles)}"
                    )
                if not self._can_succeed(
                    root_id,
                    nodes,
                    check_value=lambda node, current=path_roles, selected=facet: (
                        selected.id not in node.facet_refs
                        or (
                            (matched := selected.path_for_roles(node.semantic_role_refs))
                            is not None
                            and frozenset(matched.semantic_roles) == current
                        )
                    ),
                ):
                    raise ValueError(
                        f"Required facet {facet.id!r} proof path roles "
                        f"{sorted(path_roles)} are not independently sufficient"
                    )

                if "WITNESS" in path.minimum_proof_terms:
                    for node in path_checks:
                        missing = sorted(
                            set(
                                signature.required_policy_refs
                                if facet.required_policy_refs is None
                                else facet.required_policy_refs
                            )
                            - set(node.policy_refs)
                        )
                        if missing:
                            raise ValueError(
                                f"WITNESS facet {facet.id!r} CHECK {node.id!r} must declare "
                                f"required policy refs: {missing}"
                            )

        for policy_ref in signature.required_policy_refs:
            if policy_ref not in plan.policy_refs or not any(
                policy_ref in node.policy_refs for node in checks
            ):
                raise ValueError(
                    f"Required policy ref {policy_ref!r} is not covered below "
                    f"requirement root {signature.requirement_id!r}"
                )

        facet_ids = frozenset(facet.id for facet in signature.facets)
        if signature.root_composition == "ALL_REQUIRED":
            for facet_id in facet_ids:
                if self._can_succeed(
                    root_id,
                    nodes,
                    check_value=lambda node, current=facet_id: current not in node.facet_refs,
                ):
                    raise ValueError(
                        f"ALL_REQUIRED root can bypass required facet {facet_id!r}"
                    )
        else:
            if self._can_succeed(
                root_id,
                nodes,
                check_value=lambda node: not facet_ids.intersection(node.facet_refs),
            ):
                raise ValueError("ANY_SUFFICIENT root can bypass every declared facet")
            for facet_id in facet_ids:
                if not self._can_succeed(
                    root_id,
                    nodes,
                    check_value=lambda node, current=facet_id: (
                        current in node.facet_refs
                        or not facet_ids.intersection(node.facet_refs)
                    ),
                ):
                    raise ValueError(
                        f"ANY_SUFFICIENT root does not admit facet {facet_id!r} as a sufficient path"
                    )

        for policy_ref in signature.required_policy_refs:
            if self._can_succeed(
                root_id,
                nodes,
                check_value=lambda node, current=policy_ref: current not in node.policy_refs,
            ):
                raise ValueError(
                    f"Requirement root can bypass required policy ref {policy_ref!r}"
                )

    @classmethod
    def _can_succeed(
        cls,
        node_id: str,
        nodes: dict[str, ProofNode],
        *,
        check_value,
    ) -> bool:
        node = nodes[node_id]
        if node.kind == "CHECK":
            return bool(check_value(node))
        child_values = [
            cls._can_succeed(child_id, nodes, check_value=check_value)
            for child_id in node.depends_on
        ]
        return all(child_values) if node.kind == "ALL" else any(child_values)


def validate_plan_conformance(
    plan: ProofPlan,
    signatures: Iterable[ProofSignature] | None = None,
) -> None:
    PlanConformanceGate(signatures).validate(plan)


__all__ = [
    "PlanConformanceGate",
    "ProofFacet",
    "ProofPath",
    "ProofSignature",
    "ProofTerm",
    "RootComposition",
    "proof_signature_for",
    "proof_signature_hash_for",
    "proof_signatures",
    "validate_plan_conformance",
]

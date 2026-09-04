"""One paid protocol probe for the Fine Verifier's required status field."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src"), str(ROOT)]

from app.compiler_runtime.runtime import EvidenceCompilerRuntime, VerificationBatch
from app.config import get_settings
from app.llm import LlmClient
from app.state.persistence import atomic_write_text
from probe import jsonable, save


def run(output_dir: Path) -> None:
    if output_dir.exists():
        raise ValueError("Use a fresh receipt directory")
    output_dir.mkdir(parents=True)
    payload = {
        "checks": [{
            "id": "probe:required-status",
            "statement": "The required authorization source was not supplied.",
            "submitted_claim_refs": [],
            "candidate_claims": [],
            "submitted_binding_refs": [],
            "candidate_binding_proposals": [],
            "submitted_witness_refs": [],
            "candidate_calculation_witnesses": [],
            "candidate_resolver_witnesses": [],
            "terminal_closures": [],
        }],
        "verification_contracts": [],
        "focus_check_ids": ["probe:required-status"],
        "sources": [],
        "policy": {},
    }
    save(output_dir / "request.json", payload)
    settings = get_settings()
    if settings.llm_base_url.rstrip("/") != "https://api.commandcode.ai/provider/v1":
        raise ValueError("This probe requires CommandCode")
    llm = LlmClient(settings)
    captured = []
    try:
        batch = EvidenceCompilerRuntime(llm, settings=settings)._run_phase(
            name="fine_verifier", prompt_file="evidence_verifier.md",
            payload=payload, output_type=VerificationBatch, max_turns=None,
            max_output_tokens=None,
            result_sink=captured.append,
        )
        save(output_dir / "result.json", batch)
        save(output_dir / "sdk-responses.json", captured[0].raw_responses)
        assert len(batch.assessments) == 1
        assert batch.assessments[0].status in {"SUPPORTED", "CONTRADICTED", "NOT_FOUND"}
    finally:
        with (output_dir / "model-calls.jsonl").open("w", encoding="utf-8") as stream:
            for call in llm.calls:
                stream.write(json.dumps(jsonable(call), ensure_ascii=False) + "\n")
        atomic_write_text(output_dir / "reasoning.txt", "\n\n".join(call.reasoning_full for call in llm.calls))


if __name__ == "__main__":
    run(Path(sys.argv[1]))

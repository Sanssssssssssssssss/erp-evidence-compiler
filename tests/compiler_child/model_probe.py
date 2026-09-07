"""One bounded real-model Tau -> Evidence Compiler child probe, without MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
TAU_SRC = ROOT.parent / "erp-harness-tau" / "src"
if TAU_SRC.is_dir():
    sys.path.insert(0, str(TAU_SRC))

from tau_agent.messages import AssistantMessage, TextContent, ToolCall
from tau_agent.session import JsonlSessionStorage
from tau_ai.env import OpenAICompatibleConfig
from tau_ai.openai_compatible import OpenAICompatibleProvider
from tau_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderModelMetadata,
    ProviderSettings,
)
from tau_coding.session import CodingSession, CodingSessionConfig
from tau_coding.resources import TauResourcePaths

HERE = Path(__file__).resolve().parent
EXTENSION = (
    HERE.parents[1] / "src" / "erp_agent_odoo" / "compiler_child" / "extension.py"
)
CONTEXT_WINDOW = 128_000
MAX_OUTPUT_TOKENS = 8_192


def _load_env(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"Invalid environment line in {path.name}")
        os.environ[key.strip()] = value.strip().strip("\"'")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--resume-from", type=Path, help="Replay a saved child snapshot in an isolated parent session.")
    return parser.parse_args()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


async def run(args: argparse.Namespace) -> Path:
    if args.env_file:
        _load_env(args.env_file)
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("LLM_MODEL", "").strip()
    provider_name = os.getenv("LLM_PROVIDER", "openai-compatible").strip()
    if not api_key or not base_url or not model:
        raise RuntimeError("LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL are required")

    run_id = f"probe_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    run_dir = (args.artifact_root or HERE / "artifacts") / run_id
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    source_manifest = run_dir / "source-manifest.json"
    if "solution" in {part.lower() for part in args.manifest.parts}:
        raise ValueError("Manager manifest must not come from a solution directory")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    proposals = manifest.get("proposals") or []
    sources = manifest.get("sources") or []
    task_objective = str(manifest.get("task_objective") or "").strip()
    if len(proposals) != 1 or not sources or not task_objective:
        raise ValueError("Manifest requires task_objective, sources, and exactly one proposal")
    proposal_id = str(proposals[0].get("proposal_id") or "").strip()
    if not proposal_id:
        raise ValueError("Manager proposal requires proposal_id")
    source_refs = [str(item["source_id"]) for item in sources]
    instruction = (
        "Run exactly one durable evidence review. Start evidence_reviewer with task_objective="
        f"{task_objective!r}, source_refs={source_refs!r}, and proposal_ref={proposal_id!r}. "
        "When the child pauses, inspect that exact compiler_run_id and resume the same run. "
        "Do not re-submit sources, do not use an oracle or hidden answer, and stop after reporting "
        "the completed or irrecoverable child status."
    )
    _write(source_manifest, manifest)
    if resume_from := getattr(args, "resume_from", None):
        checkpoint = json.loads((resume_from / "checkpoint.json").read_text(encoding="utf-8"))
        child_id = checkpoint["compiler_run_id"]
        destination = run_dir / "child-runs" / run_id / child_id
        destination.mkdir(parents=True)
        copied = {}
        for name in ("request.json", "checkpoint.json", "events.jsonl"):
            shutil.copyfile(resume_from / name, destination / name)
            copied[name] = str(resume_from / name)
        _write(run_dir / "recovery-inputs.json", copied)
        instruction = (
            f"Inspect the saved evidence review {child_id!r}. If already completed, report its status. "
            "Otherwise call evidence_reviewer to resume that exact compiler_run_id ONCE, then report "
            "the returned result and stop, including if it fails. Do not start a new review, recheck, "
            "change sources or infer an expected verdict. This is one bounded checkpoint recovery test."
        )
    os.environ.update(
        {
            "LLM_THINKING_TYPE": args.thinking,
            "ERP_COMPILER_SOURCE_MANIFEST": str(source_manifest),
            "ERP_COMPILER_RUN_ROOT": str(run_dir / "child-runs"),
            "INVOICE_AGENT_STORAGE_ROOT": str(run_dir / "compiler-storage"),
            "INVOICE_AGENT_SESSION_DB": str(
                run_dir / "compiler-storage" / "sessions.sqlite"
            ),
        }
    )
    profile = {
        "provider": provider_name,
        "model": model,
        "base_url": base_url,
        "thinking": args.thinking,
        "max_turns": args.max_turns,
        "manifest": args.manifest.name,
        "api_key_present": True,
    }
    _write(run_dir / "profile.json", profile)
    (run_dir / "instruction.txt").write_text(instruction, encoding="utf-8")

    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key=api_key,
            base_url=base_url,
            reasoning_effort=args.thinking,
            thinking_format="openai",
            compat={"supportsReasoningEffort": True},
            provider_name=provider_name,
            timeout_seconds=180,
            max_retries=0,
            max_tokens=MAX_OUTPUT_TOKENS,
            infer_api_from_model=False,
        )
    )
    provider_config = OpenAICompatibleProviderConfig(
        name=provider_name,
        base_url=base_url,
        api_key_env="LLM_API_KEY",
        models=(model,),
        default_model=model,
        context_windows={model: CONTEXT_WINDOW},
        compat={"supportsReasoningEffort": True},
        model_metadata={
            model: ProviderModelMetadata(
                reasoning=True,
                context_window=CONTEXT_WINDOW,
                max_tokens=MAX_OUTPUT_TOKENS,
            )
        },
        timeout_seconds=180,
        max_retries=0,
        thinking_levels=(args.thinking,),
        thinking_models=(model,),
        thinking_default=args.thinking,
        thinking_parameter="reasoning_effort",
        thinking_defaults={model: args.thinking},
    )
    session: CodingSession | None = None
    summary: dict[str, object] = {"status": "error", **profile}
    try:
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=provider,
                model=model,
                storage=JsonlSessionStorage(run_dir / "tau-session.jsonl"),
                cwd=workspace,
                resource_paths=TauResourcePaths(root=run_dir / "tau-home", cwd=workspace, agents_root=run_dir / "agents-home"),
                tools=(),
                max_turns=args.max_turns,
                session_id=run_id,
                provider_name=provider_name,
                provider_settings=ProviderSettings(providers=(provider_config,)),
                runtime_provider_config=provider_config,
                extension_paths=(EXTENSION,),
                extensions_enabled=False,
                skills_enabled=False,
                thinking_level=args.thinking,
            )
        )
        (run_dir / "system-prompt.txt").write_text(
            session.system_prompt, encoding="utf-8"
        )
        with (run_dir / "runtime-events.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        ) as events:
            async for event in session.prompt(instruction):
                events.write(event.model_dump_json(by_alias=True) + "\n")
                events.flush()
        assistants = [
            message
            for message in session.messages
            if isinstance(message, AssistantMessage)
        ]
        tool_calls = [
            block.name
            for message in assistants
            for block in message.content
            if isinstance(block, ToolCall)
        ]
        final_text = "".join(
            block.text
            for message in assistants[-1:]
            for block in message.content
            if isinstance(block, TextContent)
        )
        checkpoints = list((run_dir / "child-runs" / run_id).glob("*/checkpoint.json"))
        checkpoint = (
            json.loads(checkpoints[-1].read_text(encoding="utf-8"))
            if checkpoints
            else {}
        )
        summary = {
            "status": "completed"
            if checkpoint.get("status") == "completed"
            else "incomplete",
            **profile,
            "tool_calls": tool_calls,
            "compiler_run_id": checkpoint.get("compiler_run_id"),
            "compiler_status": checkpoint.get("status"),
            "compiler_revision": checkpoint.get("revision"),
            "completed_check_ids": checkpoint.get("completed_check_ids") or [],
            "decisions": checkpoint.get("proof", {}).get("decisions") or [],
            "diagnostics": checkpoint.get("proof", {}).get("diagnostics") or [],
            "lineage": {
                "requirement_pack_id": checkpoint.get("requirement_pack_id"),
                "requirement_pack_version": checkpoint.get("requirement_pack_version"),
                "requirement_pack_hash": checkpoint.get("requirement_pack_hash"),
                "source_snapshot_hash": checkpoint.get("artifact", {}).get(
                    "source_snapshot_hash"
                ),
                "proposal_hash": checkpoint.get("artifact", {}).get("proposal_hash"),
                "policy_hash": checkpoint.get("artifact", {}).get("policy_hash"),
            },
            "final_text": final_text,
        }
    except Exception as exc:  # noqa: BLE001 - probe must preserve any provider/runtime failure
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)}
        (run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        assistants = [message for message in session.messages if isinstance(message, AssistantMessage)] if session else []
        keys = ("input", "output", "cache_read", "cache_write", "reasoning", "total_tokens")
        summary["parent_model_calls"] = len(assistants)
        summary["parent_known_usage_lower_bound"] = {
            key: sum(getattr(message.usage, key) or 0 for message in assistants) for key in keys
        }
        summary["parent_usage"] = {
            key: summary["parent_known_usage_lower_bound"][key]
            if assistants and not summary.get("error") and all(
                not message.error_message and message.stop_reason not in {"error", "aborted"}
                and getattr(message.usage, key) is not None for message in assistants
            ) else None for key in keys
        }
        _write(run_dir / "summary.json", summary)
        if session is not None:
            await session.aclose()
        await provider.aclose()
    return run_dir


if __name__ == "__main__":
    artifact_dir = asyncio.run(run(arguments()))
    print(artifact_dir)
    print((artifact_dir / "summary.json").read_text(encoding="utf-8"))

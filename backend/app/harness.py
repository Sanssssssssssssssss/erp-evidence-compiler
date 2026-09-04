from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Literal

from app.state.case_store import CaseStore
from app.state.persistence import PERSISTENCE_LOCK, append_text, atomic_write_text
from app.state.schemas import SupervisorDecision


MAX_ACTION_INPUT_CHARS = 4000
MAX_ACTION_PATCH_CHARS = 80000
MAX_FINAL_ANSWER_CHARS = 6000

RunPhase = Literal[
    "session_started",
    "attachment_read",
    "evidence_reviewed",
    "patch_ready",
    "patch_written",
    "report_drafted",
    "report_written",
    "waiting_approval",
    "finalized",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HarnessRunState:
    run_id: str
    case_id: str
    step_count: int = 0
    max_steps: int = 10
    current_goal: str = ""
    phase: RunPhase = "session_started"
    allowed_actions: list[str] = field(
        default_factory=lambda: ["call_role", "call_tool", "write_case_patch", "final_answer"]
    )
    trace_checkpoints: list[dict[str, Any]] = field(default_factory=list)
    phase_history: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    role_calls: list[dict[str, Any]] = field(default_factory=list)
    supervisor_decisions: list[dict[str, Any]] = field(default_factory=list)
    planner_actions: list[dict[str, Any]] = field(default_factory=list)
    plan_progress: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    turn_id: str = ""
    user_message_for_planner: str = ""
    user_message_summary: str = ""
    user_message_artifact_ref: str = ""
    session_compacted: bool = False
    pre_run_context_estimate_chars: int = 0
    pre_run_context_limit_chars: int = 0
    final_answer: str = ""
    debug_event_seq: int = 0
    debug_model_event_count: int = 0
    debug_file_snapshot: dict[str, str] = field(default_factory=dict)
    debug_final_event_recorded: bool = False
    last_debug_event_id: str = ""
    last_model_event_id: str = ""
    last_planner_model_event_id: str = ""
    last_action_event_id: str = ""
    started_at: str = field(default_factory=utc_now)
    completed_at: str = ""
    observability: dict[str, Any] = field(default_factory=dict)

    def compact_trace(self) -> dict[str, Any]:
        feedback = _feedback_observations(self.observations)
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "step_count": self.step_count,
            "max_steps": self.max_steps,
            "phase": self.phase,
            "session_compacted": self.session_compacted,
            "pre_run_context_estimate_chars": self.pre_run_context_estimate_chars,
            "pre_run_context_limit_chars": self.pre_run_context_limit_chars,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "feedback_summary": _feedback_summary(feedback),
            "feedback": feedback,
            "observations": list(self.observations),
            "interrupts": list(self.pending_approvals),
            "supervisor_decisions": list(self.supervisor_decisions),
            "planner_actions": list(self.planner_actions),
            "model_calls": list(self.model_calls),
            "tool_calls": list(self.tool_calls),
            "role_calls": list(self.role_calls),
            "plan_progress": list(self.plan_progress),
            "observability": _public_observability(self.observability),
            "trace_checkpoints": [
                {
                    "checkpoint_id": item.get("checkpoint_id"),
                    "step_count": item.get("step_count"),
                    "supervisor_decision": (item.get("supervisor_decision") or item.get("planner_action") or {}).get("action"),
                }
                for item in self.trace_checkpoints
            ],
            "checkpoints": [
                {
                    "checkpoint_id": item.get("checkpoint_id"),
                    "step_count": item.get("step_count"),
                    "supervisor_decision": (item.get("supervisor_decision") or item.get("planner_action") or {}).get("action"),
                }
                for item in self.trace_checkpoints
            ],
        }


class HarnessRuntime:
    def __init__(
        self,
        store: CaseStore | None = None,
        max_steps: int = 10,
    ) -> None:
        self.store = store or CaseStore()
        self.max_steps = max_steps

    def new_run_id(self) -> str:
        return f"run_{uuid4().hex[:12]}"

    def begin_run(self, case_id: str, current_goal: str, run_id: str | None = None) -> HarnessRunState:
        return HarnessRunState(
            run_id=run_id or self.new_run_id(),
            case_id=self.store.validate_case_id(case_id),
            max_steps=self.max_steps,
            current_goal=current_goal,
        )

    def set_phase(self, state: HarnessRunState, phase: RunPhase, detail: str = "") -> None:
        if state.phase == phase and not detail:
            return
        state.phase = phase
        state.phase_history.append({"phase": phase, "detail": detail, "step_count": state.step_count, "ts": utc_now()})

    def record_observation(self, state: HarnessRunState, observation: dict[str, Any]) -> None:
        state.observations.append(observation)
        self.append_debug_event(
            state,
            kind="observation",
            name=f"{observation.get('kind', '-')}/{observation.get('name', '-')}",
            payload=observation,
            summary=str(observation.get("summary") or observation.get("next_action_hint") or ""),
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )

    def record_guard_error(
        self,
        state: HarnessRunState,
        name: str,
        exc: Exception,
        runtime_feedback: dict[str, Any] | None = None,
    ) -> None:
        observation = {
            "kind": "guard",
            "name": name,
            "summary": f"{name} failed: {type(exc).__name__}: {exc}",
            "key_facts": [],
            "risks": [f"{type(exc).__name__}: {exc}"],
            "missing_items": [],
            "next_action_hint": "final_answer",
            "must_preserve_refs": [],
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        if runtime_feedback:
            observation["runtime_feedback"] = runtime_feedback
        self.record_observation(state, observation)

    def record_model_calls(
        self,
        state: HarnessRunState,
        calls: list[dict[str, Any]],
        debug_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        prefix = state.observability.get("_resumed_model_call_prefix")
        state.model_calls = (
            list(prefix) + list(calls)
            if isinstance(prefix, list)
            else list(calls)
        )
        debug_source = debug_calls or calls
        for call in debug_source[state.debug_model_event_count :]:
            public_call = dict(call)
            public_call.pop("reasoning_excerpt", None)
            public_call.pop("reasoning_full", None)
            event = self.append_debug_event(
                state,
                kind="model_call",
                name=str(call.get("role") or "-"),
                payload=public_call,
                summary=f"{call.get('role', '-')} / {call.get('model', '-')} {call.get('finish_reason', '')}".strip(),
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
            state.last_model_event_id = str(event.get("event_id") or "")
            if call.get("role") == "planner":
                state.last_planner_model_event_id = state.last_model_event_id
            if call.get("schema_validation_error"):
                self.record_observation(state, _schema_retry_observation(call))
        state.debug_model_event_count = len(calls)

    def finalize_run(self, state: HarnessRunState, final_answer: str | None = None) -> None:
        if final_answer is not None:
            state.final_answer = final_answer
        state.completed_at = state.completed_at or utc_now()
        state.pending_approvals = []
        self.set_phase(state, "finalized")
        if not state.debug_final_event_recorded:
            self.append_debug_event(
                state,
                kind="final_answer",
                name="assistant_reply",
                payload={"final_answer": state.final_answer, "completed_at": state.completed_at},
                summary=f"Final answer chars={len(state.final_answer or '')}",
                parent_event_id=state.last_action_event_id,
                caused_by_event_id=state.last_action_event_id,
            )
            state.debug_final_event_recorded = True

    def record_supervisor_decision(self, state: HarnessRunState, decision: SupervisorDecision) -> None:
        state.step_count += 1
        payload = _compact_decision_payload(decision.model_dump(exclude_none=True))
        payload["supervisor_model_call_event_id"] = state.last_planner_model_event_id
        state.supervisor_decisions.append(payload)
        state.planner_actions.append(payload)
        event = self.append_debug_event(
            state,
            kind="supervisor_decision",
            name=decision.action,
            payload=payload,
            summary=f"step={state.step_count}; action={decision.action}; target={decision.target or '-'}",
            parent_event_id=state.last_planner_model_event_id,
            caused_by_event_id=state.last_planner_model_event_id,
        )
        state.last_action_event_id = str(event.get("event_id") or "")
        if decision.short_plan or decision.plan_progress:
            state.plan_progress.append(
                {
                    "step_count": state.step_count,
                    "action": decision.action,
                    "target": decision.target,
                    "short_plan": list(decision.short_plan),
                    "plan_progress": decision.plan_progress,
                    "ts": utc_now(),
                }
            )

    def record_planner_action(self, state: HarnessRunState, decision: SupervisorDecision) -> None:
        self.record_supervisor_decision(state, decision)

    def record_tool_call(
        self,
        state: HarnessRunState,
        tool: str,
        tool_input: dict[str, Any],
        result: Any,
        error: str = "",
        capability: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "tool": tool,
            "input": tool_input,
            "result": result,
            "error": error,
            "capability": capability or {},
            "ts": utc_now(),
        }
        state.tool_calls.append(
            {
                "tool": tool,
                "input": _redact_large(tool_input),
                "result_preview": _preview(result),
                "error": error,
                "capability": capability or {},
                "ts": payload["ts"],
            }
        )
        self.append_debug_event(
            state,
            kind="tool_call",
            name=tool,
            payload=payload,
            summary=f"{tool} {'failed' if error else 'completed'}",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )

    def record_role_call(
        self,
        state: HarnessRunState,
        role: str,
        role_input: dict[str, Any],
        result: Any,
        error: str = "",
        capability: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "role": role,
            "input": role_input,
            "result": result,
            "error": error,
            "capability": capability or {},
            "ts": utc_now(),
        }
        state.role_calls.append(
            {
                "role": role,
                "input": _redact_large(role_input),
                "result_preview": _preview(result),
                "error": error,
                "capability": capability or {},
                "ts": payload["ts"],
            }
        )
        self.append_debug_event(
            state,
            kind="role_call",
            name=role,
            payload=payload,
            summary=f"{role} {'failed' if error else 'completed'}",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )

    def persist_trace_checkpoint(self, state: HarnessRunState, supervisor_decision: SupervisorDecision | None = None) -> None:
        feedback = self._record_step_feedback(state, supervisor_decision)
        checkpoint = {
            "checkpoint_id": f"trace_ckpt_{len(state.trace_checkpoints) + 1:03d}",
            "run_id": state.run_id,
            "case_id": state.case_id,
            "step_count": state.step_count,
            "phase": state.phase,
            "supervisor_decision": supervisor_decision.model_dump(exclude_none=True) if supervisor_decision else None,
            "planner_action": supervisor_decision.model_dump(exclude_none=True) if supervisor_decision else None,
            "observations": state.observations[-6:],
            "feedback": feedback,
            "ts": utc_now(),
        }
        state.trace_checkpoints.append(checkpoint)
        self.append_debug_event(
            state,
            kind="checkpoint",
            name=checkpoint["checkpoint_id"],
            payload=checkpoint,
            summary=f"checkpoint step={state.step_count}; phase={state.phase}",
            parent_event_id=state.last_action_event_id,
            caused_by_event_id=state.last_action_event_id,
        )
        self.write_trace(state)

    def persist_checkpoint(self, state: HarnessRunState, supervisor_decision: SupervisorDecision | None = None) -> None:
        self.persist_trace_checkpoint(state, supervisor_decision)

    def write_trace(self, state: HarnessRunState) -> None:
        root = self.store.ensure_case_dirs(state.case_id)
        feedback = _feedback_observations(state.observations)
        payload = {
            "run_id": state.run_id,
            "case_id": state.case_id,
            "started_at": state.started_at,
            "completed_at": state.completed_at,
            "step_count": state.step_count,
            "current_goal": state.current_goal,
            "phase": state.phase,
            "phase_history": state.phase_history,
            "allowed_actions": state.allowed_actions,
            "trace_checkpoints": state.trace_checkpoints,
            "checkpoints": state.trace_checkpoints,
            "supervisor_decisions": state.supervisor_decisions,
            "planner_actions": state.planner_actions,
            "model_calls": state.model_calls,
            "tool_calls": state.tool_calls,
            "role_calls": state.role_calls,
            "plan_progress": state.plan_progress,
            "observations": state.observations,
            "interrupts": state.pending_approvals,
            "feedback_summary": _feedback_summary(feedback),
            "feedback": feedback,
            "debug_events_ref": f"traces/{state.run_id}/events.jsonl",
            "session_events_ref": "traces/events.jsonl",
            "session_id": state.session_id,
            "turn_id": state.turn_id,
            "user_message_summary": state.user_message_summary,
            "user_message_artifact_ref": state.user_message_artifact_ref,
            "session_compacted": state.session_compacted,
            "pre_run_context_estimate_chars": state.pre_run_context_estimate_chars,
            "pre_run_context_limit_chars": state.pre_run_context_limit_chars,
            "final_answer": state.final_answer,
            "observability": _public_observability(state.observability),
        }
        atomic_write_text(
            root / "traces" / f"{state.run_id}.json",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        )

    def append_debug_event(
        self,
        state: HarnessRunState,
        *,
        kind: str,
        name: str,
        payload: Any,
        summary: str = "",
        parent_event_id: str = "",
        caused_by_event_id: str = "",
    ) -> dict[str, Any]:
        with PERSISTENCE_LOCK:
            state.debug_event_seq += 1
            root = self.store.ensure_case_dirs(state.case_id)
            run_seq = state.debug_event_seq
            case_seq = self.store.next_trace_case_seq(state.case_id)
            event_id = f"{state.run_id}_evt_{run_seq:06d}"
            event = {
                "seq": run_seq,
                "run_seq": run_seq,
                "case_seq": case_seq,
                "event_id": event_id,
                "parent_event_id": parent_event_id,
                "caused_by_event_id": caused_by_event_id,
                "ts": utc_now(),
                "case_id": state.case_id,
                "session_id": state.session_id,
                "turn_id": state.turn_id,
                "run_id": state.run_id,
                "step_count": state.step_count,
                "phase": state.phase,
                "kind": kind,
                "name": name,
                "summary": summary,
                "payload_sha256": _sha256_payload(payload),
                "payload_preview": _preview(payload, max_chars=1600),
                "payload": payload,
            }
            line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
            run_root = root / "traces" / state.run_id
            run_root.mkdir(parents=True, exist_ok=True)
            for path in (run_root / "events.jsonl", root / "traces" / "events.jsonl"):
                append_text(path, line)
        state.last_debug_event_id = event_id
        return event

    def reconcile_debug_sequence(self, state: HarnessRunState) -> None:
        """Continue a resumed run after the largest event already on disk."""

        with PERSISTENCE_LOCK:
            path = self.store.resolve_case_path(
                state.case_id,
                f"traces/{state.run_id}/events.jsonl",
            )
            maximum = 0
            latest_event_id = ""
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        run_seq = int(row.get("run_seq", row.get("seq", 0)) or 0)
                    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                        continue
                    if run_seq >= maximum:
                        maximum = run_seq
                        latest_event_id = str(row.get("event_id") or "")
            state.debug_event_seq = max(state.debug_event_seq, maximum)
            if latest_event_id:
                state.last_debug_event_id = latest_event_id
            state.observability["_resumed_model_call_prefix"] = list(state.model_calls)
            state.debug_model_event_count = 0

    def step_limit_answer(self, state: HarnessRunState) -> str:
        feedback = _latest_runtime_feedback(state.observations)
        case_snapshot = _case_snapshot(self.store.load(state.case_id))
        completed = _completed_work_labels(state)
        latest_observation = _latest_non_feedback_observation(state.observations)
        if latest_observation and latest_observation.get("kind") == "policy":
            latest_observation = None
        lines = [
            "当前运行已到本轮步数上限，系统已保存完整 trace，可继续追问或让我接着处理。",
            f"已完成：{completed or '已记录本轮上下文和检查点'}。",
            (
                "当前案卷状态："
                f"{case_snapshot['status']}；证据 {case_snapshot['evidence_count']} 条；"
                f"满足 {case_snapshot['satisfied_count']} 项，缺失 {case_snapshot['missing_count']} 项，"
                f"偏弱 {case_snapshot['weak_count']} 项，冲突 {case_snapshot['conflict_count']} 项。"
            ),
        ]
        error_type = str(feedback.get("error_type") or "")
        if error_type:
            lines.append(f"本轮最后的阻塞类型：{_feedback_label(error_type)}。我不会把内部修复指令当作用户回复。")
        if latest_observation and latest_observation.get("summary"):
            lines.append(f"最近一步结果：{_sanitize_internal_runtime_text(str(latest_observation.get('summary')))[:220]}")
        lines.append("下一步可以直接让我继续生成报告、追问缺口，或指定某份材料重新审查。")
        return "\n".join(lines)

    def _record_step_feedback(
        self,
        state: HarnessRunState,
        planner_action: SupervisorDecision | None,
    ) -> dict[str, Any]:
        recent_observations = _observations_since_last_feedback(state.observations)
        case_state = self.store.load(state.case_id)
        current_snapshot = _workspace_file_snapshot(self.store.ensure_case_dirs(state.case_id))
        file_changes = _file_changes(state.debug_file_snapshot, current_snapshot)
        feedback = _build_step_feedback(
            state=state,
            planner_action=planner_action,
            recent_observations=recent_observations,
            case_state=case_state,
            file_changes=file_changes,
            workspace_files=_file_rows(current_snapshot),
        )
        state.debug_file_snapshot = current_snapshot
        self.record_observation(state, feedback)
        return feedback


def _preview(value: Any, max_chars: int = 900) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return text[:max_chars]


def _redact_large(value: dict[str, Any], max_chars: int = 700) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str) and len(item) > max_chars:
            result[key] = item[:max_chars] + "..."
        else:
            result[key] = item
    return result


def _sha256_payload(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _observations_since_last_feedback(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    start = 0
    for index in range(len(observations) - 1, -1, -1):
        item = observations[index]
        if isinstance(item, dict) and item.get("kind") == "feedback":
            start = index + 1
            break
    return [item for item in observations[start:] if isinstance(item, dict)]


def _build_step_feedback(
    *,
    state: HarnessRunState,
    planner_action: SupervisorDecision | None,
    recent_observations: list[dict[str, Any]],
    case_state: Any,
    file_changes: dict[str, list[str]],
    workspace_files: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_kind, expected_name = _expected_observation(planner_action)
    observed = _matching_observation(recent_observations, expected_kind, expected_name)
    errors = [item for item in recent_observations if item.get("error")]
    diagnostic_warnings = [item for item in recent_observations if item.get("diagnostic_risk")]
    checks: list[dict[str, Any]] = []
    if expected_kind:
        checks.append(
            {
                "name": "expected_observation",
                "status": "pass" if observed else "fail",
                "expected": f"{expected_kind}/{expected_name}",
                "observed": _observation_label(observed) if observed else "",
            }
        )
    if planner_action and planner_action.action == "final_answer":
        checks.append(
            {
                "name": "final_answer_present",
                "status": "pass" if state.final_answer else "fail",
                "observed": f"{len(state.final_answer)} chars",
            }
        )
    checks.append(
        {
            "name": "error_scan",
            "status": "fail" if errors else "pass",
            "observed": len(errors),
        }
    )
    if errors:
        status = "error"
        score = 0.1
    elif diagnostic_warnings:
        status = "warning"
        score = 0.65
    elif any(item.get("status") == "fail" for item in checks):
        status = "warning"
        score = 0.45
    else:
        status = "ok"
        score = 0.9
    latest = _latest_non_feedback_observation(recent_observations)
    next_hint = str((latest or {}).get("next_action_hint") or "")
    runtime_feedback = _latest_runtime_feedback(recent_observations)
    if status == "error" and runtime_feedback.get("retry_allowed") is False:
        next_hint = str(runtime_feedback.get("recommended_action") or "final_answer")
    elif status == "error":
        next_hint = "retry_or_final_answer"
    case_snapshot = _case_snapshot(case_state)
    action_label = _action_label(planner_action)
    summary = (
        f"Feedback after step {state.step_count}: {status}; "
        f"action={action_label}; "
        f"case={case_snapshot['status']} evidence={case_snapshot['evidence_count']} "
        f"missing={case_snapshot['missing_count']} weak={case_snapshot['weak_count']} "
        f"conflict={case_snapshot['conflict_count']} risks={case_snapshot['risk_count']}; "
        f"next={next_hint or '-'}."
    )
    return {
        "kind": "feedback",
        "name": "step_result",
        "summary": summary,
        "status": status,
        "score": score,
        "step_count": state.step_count,
        "action": action_label,
        "quality_checks": checks,
        "case_snapshot": case_snapshot,
        "file_changes": file_changes,
        "workspace_files": workspace_files,
        "recent_observation_count": len(recent_observations),
        "next_action_hint": next_hint,
        "key_facts": _feedback_key_facts(case_snapshot),
        "risks": [_feedback_risk_text(item) for item in errors + diagnostic_warnings],
        "missing_items": list(getattr(case_state, "missing_materials", []) or [])[:8],
        "must_preserve_refs": _feedback_refs(recent_observations),
        "runtime_feedback": runtime_feedback,
    }


def _feedback_risk_text(observation: dict[str, Any]) -> str:
    summary = str(observation.get("summary") or observation.get("error") or "")
    schema_error = str(observation.get("schema_validation_error") or "")
    if schema_error and schema_error not in summary:
        summary = f"{summary}: {schema_error}"
    return summary[:700]


def _schema_retry_observation(call: dict[str, Any]) -> dict[str, Any]:
    role = str(call.get("role") or "model")
    latency = call.get("latency_ms")
    latency_text = f"; latency_ms={latency}" if latency not in (None, "") else ""
    error = str(call.get("schema_validation_error") or "")
    return {
        "kind": "model",
        "name": "schema_retry",
        "summary": f"{role} output failed schema validation and triggered JSON retry{latency_text}.",
        "key_facts": [
            f"role={role}",
            f"retry_attempted={bool(call.get('schema_retry_attempted'))}",
        ],
        "risks": [error[:700]],
        "missing_items": [],
        "next_action_hint": "",
        "must_preserve_refs": [],
        "diagnostic_risk": True,
        "schema_validation_error": error,
    }


def _expected_observation(planner_action: SupervisorDecision | None) -> tuple[str, str]:
    if planner_action is None:
        return "", ""
    if planner_action.action == "call_tool":
        return "tool", str(planner_action.target or "")
    if planner_action.action == "delegate_agent":
        return "role", str(planner_action.target or "")
    if planner_action.action == "write_case_patch":
        return "tool", "write_case_patch"
    return "", ""


def _matching_observation(observations: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    if not kind or not name:
        return None
    for item in reversed(observations):
        if item.get("kind") == kind and item.get("name") == name:
            return item
    return None


def _latest_non_feedback_observation(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(observations):
        if item.get("kind") != "feedback":
            return item
    return None


def _latest_runtime_feedback(observations: list[dict[str, Any]]) -> dict[str, Any]:
    for observation in reversed(observations):
        if not isinstance(observation, dict):
            continue
        feedback = observation.get("runtime_feedback")
        if isinstance(feedback, dict) and feedback:
            return dict(feedback)
    return {}


def _observation_label(observation: dict[str, Any] | None) -> str:
    if not observation:
        return ""
    return f"{observation.get('kind', '-')}/{observation.get('name', '-')}"


def _action_label(planner_action: SupervisorDecision | None) -> str:
    if planner_action is None:
        return "planner_error"
    target = planner_action.target or ""
    return f"{planner_action.action}:{target}" if target else str(planner_action.action)


def _case_snapshot(case_state: Any) -> dict[str, Any]:
    requirements = [
        {"id": item.id, "status": item.status, "evidence_count": len(item.evidence_ids)}
        for item in getattr(case_state, "requirements", []) or []
    ]
    return {
        "status": getattr(case_state, "status", ""),
        "evidence_count": len(getattr(case_state, "evidence_items", []) or []),
        "missing_count": len(getattr(case_state, "missing_materials", []) or []),
        "weak_count": len(getattr(case_state, "weak_materials", []) or []),
        "conflict_count": len(getattr(case_state, "conflict_materials", []) or []),
        "satisfied_count": len(getattr(case_state, "satisfied_materials", []) or []),
        "missing_materials": list(getattr(case_state, "missing_materials", []) or []),
        "weak_materials": list(getattr(case_state, "weak_materials", []) or []),
        "conflict_materials": list(getattr(case_state, "conflict_materials", []) or []),
        "satisfied_materials": list(getattr(case_state, "satisfied_materials", []) or []),
        "risk_count": len(getattr(case_state, "risk_flags", []) or []),
        "requirements": requirements,
    }


def _feedback_key_facts(case_snapshot: dict[str, Any]) -> list[str]:
    facts = [
        f"case_status={case_snapshot.get('status', '')}",
        f"evidence_count={case_snapshot.get('evidence_count', 0)}",
        f"missing_count={case_snapshot.get('missing_count', 0)}",
        f"weak_count={case_snapshot.get('weak_count', 0)}",
        f"conflict_count={case_snapshot.get('conflict_count', 0)}",
        f"risk_count={case_snapshot.get('risk_count', 0)}",
    ]
    for item in case_snapshot.get("requirements", [])[:8]:
        facts.append(f"{item.get('id')}={item.get('status')}")
    return facts


def _feedback_refs(observations: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for item in observations:
        ref = item.get("artifact_ref")
        if ref:
            refs.append(str(ref))
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            result.append(ref)
    return result[:8]


def _feedback_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in observations if isinstance(item, dict) and item.get("kind") == "feedback"]


def _feedback_summary(feedback: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"ok": 0, "warning": 0, "error": 0}
    for item in feedback:
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1
    worst = "ok"
    if counts["error"]:
        worst = "error"
    elif counts["warning"]:
        worst = "warning"
    return {
        "total": len(feedback),
        "ok": counts["ok"],
        "warning": counts["warning"],
        "error": counts["error"],
        "worst_status": worst if feedback else "",
    }


def _completed_work_labels(state: HarnessRunState) -> str:
    labels: list[str] = []
    if any(item.get("tool") == "read_attachment" and not item.get("error") for item in state.tool_calls):
        labels.append("读取附件")
    if any(item.get("role") == "evidence_reviewer" and not item.get("error") for item in state.role_calls):
        labels.append("证据抽取/审核")
    if any(item.get("role") == "case_patch_writer" and not item.get("error") for item in state.role_calls):
        labels.append("整理案卷更新")
    if any(item.get("tool") == "write_case_patch" and not item.get("error") for item in state.tool_calls):
        labels.append("写入本地 case")
    if any(item.get("role") == "materials_advisor" and not item.get("error") for item in state.role_calls):
        labels.append("材料顾问补料建议")
    if any(item.get("role") == "report_writer" and not item.get("error") for item in state.role_calls):
        labels.append("撰写报告草稿")
    if any(item.get("tool") == "write_case_file" and not item.get("error") for item in state.tool_calls):
        labels.append("写入 Markdown 报告")
    if any(item.get("tool") == "render_pdf" and not item.get("error") for item in state.tool_calls):
        labels.append("渲染 PDF")
    return "、".join(labels)


def _feedback_label(error_type: str) -> str:
    return {
        "final_answer_internal_retry_instruction": "最终回复包含内部修复提示",
        "final_answer_no_execution_wording": "最终回复包含执行性措辞",
        "final_answer_case_state_consistency": "最终回复与 case_state 不一致",
        "final_answer_invoice_only_scope": "单张发票范围越界",
        "final_answer_optional_quality_as_required": "单张发票可选质量项被写成必填项",
        "step_budget_near_limit": "本轮步数即将耗尽",
    }.get(error_type, error_type)


def _sanitize_internal_runtime_text(text: str) -> str:
    value = str(text or "")
    blocked = (
        "The previous final_answer",
        "Rewrite the answer",
        "Rewrite the final_answer",
        "previous final answer",
        "case_state facts only",
    )
    if any(item.lower() in value.lower() for item in blocked):
        return "最终回复被安全校验拦截，已记录为 guard feedback。"
    return value


def _workspace_file_snapshot(root: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for path in root.rglob("*"):
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                stat = path.stat()
                result[relative] = f"{stat.st_size}:{stat.st_mtime_ns}"
    except Exception:
        return {}
    return result


def _file_changes(previous: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    previous_keys = set(previous)
    current_keys = set(current)
    added = sorted(current_keys - previous_keys)
    removed = sorted(previous_keys - current_keys)
    modified = sorted(key for key in previous_keys & current_keys if previous.get(key) != current.get(key))
    return {"added": added, "modified": modified, "removed": removed}


def _file_rows(snapshot: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, signature in sorted(snapshot.items()):
        size, _, mtime_ns = signature.partition(":")
        rows.append({"path": path, "bytes": int(size or 0), "mtime_ns": int(mtime_ns or 0)})
    return rows


def _public_observability(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not str(key).startswith("_")}


def validate_decision_size(decision: SupervisorDecision) -> None:
    input_chars = len(json.dumps(decision.input or {}, ensure_ascii=False, default=str))
    patch_chars = len(json.dumps(decision.case_patch or {}, ensure_ascii=False, default=str))
    final_chars = len(str(decision.final_answer or ""))
    if input_chars > MAX_ACTION_INPUT_CHARS:
        raise ValueError(f"Planner action.input too large: {input_chars} chars")
    if patch_chars > MAX_ACTION_PATCH_CHARS:
        raise ValueError(f"Planner action.case_patch too large: {patch_chars} chars")
    if final_chars > MAX_FINAL_ANSWER_CHARS:
        raise ValueError(f"Planner final_answer too large: {final_chars} chars")


def _compact_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if isinstance(result.get("input"), dict):
        result["input"] = _redact_large(result["input"], max_chars=900)
    if isinstance(result.get("case_patch"), dict):
        patch_text = json.dumps(result["case_patch"], ensure_ascii=False, default=str)
        if len(patch_text) > MAX_ACTION_PATCH_CHARS:
            result["case_patch"] = {"_truncated": True, "chars": len(patch_text), "preview": patch_text[:1600]}
    final_answer = str(result.get("final_answer") or "")
    if len(final_answer) > MAX_FINAL_ANSWER_CHARS:
        result["final_answer"] = final_answer[:MAX_FINAL_ANSWER_CHARS] + "...[truncated]"
    return result

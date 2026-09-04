from __future__ import annotations

import json
from dataclasses import asdict, fields
from typing import Any

from app.harness import HarnessRunState
from app.state.case_store import CaseStore
from app.state.persistence import PERSISTENCE_LOCK, atomic_write_text
from app.state.schemas import AgentTurnRequest


class RuntimeCheckpointStore:
    """Persist resumable SDK and harness state outside the model runtime."""

    def __init__(self, store: CaseStore) -> None:
        self.store = store

    def save(
        self,
        *,
        state: HarnessRunState,
        request: AgentTurnRequest,
        sdk_state: str = "",
        interruptions: list[dict[str, Any]] | None = None,
    ) -> None:
        payload = {
            "run_state": asdict(state),
            "request": request.model_dump(mode="json"),
            "sdk_state": sdk_state,
            "interruptions": interruptions or [],
        }
        path = self.store.resolve_case_path(state.case_id, f"traces/{state.run_id}/runtime_state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def load(self, case_id: str, run_id: str) -> tuple[HarnessRunState, AgentTurnRequest, str, list[dict[str, Any]]]:
        with PERSISTENCE_LOCK:
            case_id = self.store.validate_case_id(case_id)
            path = self.store.resolve_case_path(case_id, f"traces/{run_id}/runtime_state.json")
            if not path.exists():
                raise FileNotFoundError(run_id)
            payload = json.loads(path.read_text(encoding="utf-8"))
        return _checkpoint_from_json(payload, case_id)

    def clear(self, case_id: str, run_id: str) -> None:
        with PERSISTENCE_LOCK:
            path = self.store.resolve_case_path(
                self.store.validate_case_id(case_id),
                f"traces/{run_id}/runtime_state.json",
            )
            if path.exists():
                path.unlink()

    def save_compiler(
        self,
        *,
        case_id: str,
        run_id: str,
        compiler_run_id: str,
        payload: dict[str, Any],
    ) -> None:
        revision = int(payload.get("revision") or 1)
        root = self.store.resolve_case_path(
            self.store.validate_case_id(case_id),
            f"traces/{run_id}/compiler/{compiler_run_id}",
        )
        path = root / f"revision_{revision:04d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def load_compiler(
        self,
        case_id: str,
        run_id: str,
        compiler_run_id: str,
        *,
        revision: int | None = None,
    ) -> dict[str, Any]:
        with PERSISTENCE_LOCK:
            root = self.store.resolve_case_path(
                self.store.validate_case_id(case_id),
                f"traces/{run_id}/compiler/{compiler_run_id}",
            )
            if revision is None:
                candidates = sorted(root.glob("revision_*.json")) if root.exists() else []
                if not candidates:
                    raise FileNotFoundError(compiler_run_id)
                path = candidates[-1]
            else:
                path = root / f"revision_{int(revision):04d}.json"
            if not path.exists():
                raise FileNotFoundError(compiler_run_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def latest_compiler(
        self,
        case_id: str,
        compiler_run_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Return the latest case-local compiler revision and its parent run id."""

        with PERSISTENCE_LOCK:
            traces = self.store.resolve_case_path(
                self.store.validate_case_id(case_id),
                "traces",
            )
            candidates = list(traces.glob("*/compiler/*/revision_*.json")) if traces.exists() else []
            if compiler_run_id:
                candidates = [path for path in candidates if path.parent.name == compiler_run_id]
            if not candidates:
                raise FileNotFoundError(compiler_run_id or "latest compiler run")
            path = max(candidates, key=lambda item: (item.stat().st_mtime_ns, str(item)))
            return path.parents[2].name, json.loads(path.read_text(encoding="utf-8"))


def _checkpoint_from_json(
    payload: dict[str, Any],
    case_id: str,
) -> tuple[HarnessRunState, AgentTurnRequest, str, list[dict[str, Any]]]:
    state = _run_state_from_json(payload.get("run_state") or {})
    request = AgentTurnRequest.model_validate(payload.get("request") or {"case_id": case_id, "message": ""})
    sdk_state = str(payload.get("sdk_state") or "")
    interruptions = payload.get("interruptions") if isinstance(payload.get("interruptions"), list) else []
    return state, request, sdk_state, [item for item in interruptions if isinstance(item, dict)]


def _run_state_from_json(data: dict[str, Any]) -> HarnessRunState:
    names = {field.name for field in fields(HarnessRunState)}
    kwargs = {key: value for key, value in dict(data or {}).items() if key in names}
    return HarnessRunState(**kwargs)

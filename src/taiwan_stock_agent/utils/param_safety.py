"""Safe parameter change validation and application for optimize_agent."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_DEFAULT_PARAMS = _CONFIG_DIR / "engine_params.json"
_DEFAULT_HISTORY = _CONFIG_DIR / "param_history.json"
_HISTORY_LIMIT = 100


def validate_changes(
    changes: list[dict],
    current_params: dict,
) -> tuple[bool, list[str]]:
    """Validate LLM-proposed changes against whitelist and ±20% cap.

    Returns (ok, error_messages).
    """
    whitelist = set(current_params.get("tunable_whitelist", []))
    errors: list[str] = []

    for c in changes:
        param = c.get("param", "")
        to_val = c.get("to")
        from_val = current_params.get(param)

        if param not in whitelist:
            errors.append(f"'{param}' not in tunable whitelist")
            continue
        if from_val is None or to_val is None:
            errors.append(f"'{param}' missing from/to values")
            continue
        if from_val == 0:
            continue
        delta = abs(to_val - from_val) / abs(from_val)
        if delta > 0.20:
            errors.append(
                f"'{param}' change {from_val}→{to_val} exceeds ±20% cap ({delta:.0%})"
            )

    return len(errors) == 0, errors


def apply_changes(
    changes: list[dict],
    params_path: Path = _DEFAULT_PARAMS,
    history_path: Path = _DEFAULT_HISTORY,
) -> None:
    """Apply validated changes to engine_params.json and record in history."""
    params = json.loads(params_path.read_text())
    for c in changes:
        params[c["param"]] = c["to"]
    params_path.write_text(json.dumps(params, indent=2, ensure_ascii=False))

    history = json.loads(history_path.read_text()) if history_path.exists() else []
    history.append({"timestamp": datetime.now().isoformat(), "changes": changes})
    if len(history) > _HISTORY_LIMIT:
        history = history[-_HISTORY_LIMIT:]
    history_path.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def rollback_params(
    params_path: Path = _DEFAULT_PARAMS,
    history_path: Path = _DEFAULT_HISTORY,
) -> list[dict] | None:
    """Revert to previous params. Returns the reverted changes or None if no history."""
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    if not history:
        return None
    last = history.pop()
    params = json.loads(params_path.read_text())
    for c in last["changes"]:
        params[c["param"]] = c["from"]
    params_path.write_text(json.dumps(params, indent=2, ensure_ascii=False))
    history_path.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    return last["changes"]

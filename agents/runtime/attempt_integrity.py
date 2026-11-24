"""Validate saved attempts without starting a VM or calling a model."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def json_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def inspect_attempt(folder: Path, result: dict, task_source: Path) -> dict:
    errors = []
    warnings = []
    provenance = "legacy_task_record"
    try:
        if result.get("task_source_sha256") != hashlib.sha256(task_source.read_bytes()).hexdigest():
            errors.append("task_source_mismatch")
        score = result.get("score")
        if type(score) not in (int, float) or not math.isfinite(score):
            errors.append("invalid_score")
        trajectory = json_lines(folder / "traj.jsonl")
        if [row.get("step") for row in trajectory] != list(range(1, result.get("steps", 0) + 1)):
            errors.append("trajectory_steps_mismatch")
        if not trajectory:
            errors.append("missing_trajectory")
        if not (folder / "step_0.png").is_file():
            errors.append("missing_initial_frame")
        if any(not (folder / row["screenshot_file"]).is_file() for row in trajectory):
            errors.append("missing_step_frame")
        actions = json_lines(folder / "action_events.jsonl")
        submitted = [row for row in actions if row.get("phase") == "submitted"]
        returned = [row for row in actions if row.get("phase") == "returned"]
        if len(submitted) != result.get("actions_sent") or len(returned) != len(submitted):
            errors.append("action_receipt_count_mismatch")
        if [(r.get("step"), r.get("action")) for r in submitted] != [
                (r.get("step"), r.get("action")) for r in returned]:
            errors.append("action_receipt_content_mismatch")
        score_file = folder / "result.txt"
        if score_file.is_file():
            provenance = "score_file"
            if float(score_file.read_text(encoding="utf-8")) != score:
                errors.append("score_file_mismatch")
        else:
            warnings.append("legacy_score_has_no_separate_result_file")
        evaluation_file = folder / "evaluation.json"
        if evaluation_file.is_file():
            raw = json.loads(evaluation_file.read_bytes())
            evaluated = raw.get("score") if isinstance(raw, dict) else raw
            if type(evaluated) is bool or float(evaluated) != score:
                errors.append("raw_evaluation_mismatch")
            provenance = "raw_evaluation"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(type(exc).__name__)
    return {"usable": not errors, "errors": errors, "warnings": warnings,
            "score_provenance": provenance}


def supplementary_regrades(root: Path, framework: str, task: str, source: Path) -> list[tuple[Path, dict]]:
    """Read the separately recorded Task046 regrade, retaining its original attempt."""
    if framework != "agent_s2" or task != "046":
        return []
    results = []
    for path in (root / "targeted_recovery_20261116").glob("046_original*/reevaluation.json"):
        try:
            receipt = json.loads(path.read_bytes())
            if (receipt.get("task"), receipt.get("status"), receipt.get("started_model")) != (task, "regraded", False):
                continue
            if receipt.get("evaluator_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
                continue
            folder = Path(receipt["source_attempt"])
            if not folder.resolve().is_relative_to(root.resolve()):
                continue
            original = json.loads((folder / "task_result.json").read_bytes())
            if original.get("task") != task or original.get("stage") != "evaluation":
                continue
            trajectory = json_lines(folder / "traj.jsonl")
            submitted = [a for a in json_lines(folder / "action_events.jsonl") if a.get("phase") == "submitted"]
            result = original | {"status": "completed", "score": receipt["result"]["score"],
                                 "steps": len(trajectory), "actions_sent": len(submitted),
                                 "_supplementary_evaluation": str(path)}
            results.append((folder, result))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return results

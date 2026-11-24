"""Run one Windows shard of the pinned OSWorld 2.1 UiPath campaign."""
import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from run_reliability import (BATCH_START_COMMIT_MIB, PlannerJsonDecodeError,
                             RuntimeGuard, append_event, memory_snapshot)


HERE = Path(__file__).resolve().parent
CODE = HERE / "OSWorld-v2.1"
WORKER = str(HERE / "desktop_worker.py")
MODEL = "gpt-6.1-sol"
EFFORT = "medium"
ROLES = {"main": 27, "second": 27, "teammate": 54}
RESOURCE_WAIT_SECONDS = 6 * 3600


def sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def assigned(shards: dict, universe: list[str], role: str) -> list[str]:
    flat = [task for ids in shards.values() for task in ids]
    if ({key: len(value) for key, value in shards.items()} != ROLES or len(universe) != 108
            or len(flat) != len(set(flat)) or set(flat) != set(universe)):
        raise ValueError("Malformed campaign split: use the shared campaign.json")
    return shards[role]


def has_evidence(path: Path, task: str, fingerprint: str, smoke: bool) -> bool:
    try:
        identity = read(path / "run_identity.json")
        result = read(path / "task_result.json")
        expected = dict(task=task, architecture="ui_path_screen_agent", benchmark_release="osworld-v2.1",
                        model=MODEL, reasoning_effort=EFFORT, max_steps=150, guest_memory_mb=4096,
                        status="completed")
        score = result["score"]
        if (identity.get("campaign_sha256") != fingerprint or identity.get("task") != task
                or any(result.get(k) != v for k, v in expected.items())
                or type(score) not in (int, float) or not math.isfinite(score)
                or float((path / "result.txt").read_text()) != score
                or not (path / "step_0.png").is_file() or not (path / "traj.jsonl").stat().st_size):
            return False
        if not smoke:
            return True
        def rows(name: str):
            with (path / name).open(encoding="utf-8") as log:
                yield from (json.loads(line) for line in log if line.strip())
        return (any(r.get("phase") == "request_completed" and r.get("model") == MODEL and r.get("image_count", 0) > 0
                    for r in rows("api_events.jsonl"))
                and any(r.get("phase") == "returned" and isinstance(r.get("action"), dict)
                        and str(r["action"].get("action_type", "")).upper() not in {"DONE", "WAIT", "FAIL", ""}
                        for r in rows("action_events.jsonl")))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def wait_for_resources(out: Path, data: Path, image: Path, task: str,
                       minimum_commit: int = BATCH_START_COMMIT_MIB) -> bool:
    deadline = time.monotonic() + RESOURCE_WAIT_SECONDS
    last = ""
    while not (out / "STOP").exists():
        mem = memory_snapshot()
        if min(shutil.disk_usage(p).free for p in (out, data, image.parent)) < 8 * 1024**3:
            reason = "disk"
        elif mem["free_physical_mb"] < 1200 or mem["free_commit_mb"] < minimum_commit:
            reason = "memory"
        else:
            return True
        if last != reason:
            print(f"Waiting for {reason} before #{task}; available memory={mem}, commit_min={minimum_commit}MiB", flush=True)
            last = reason
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Resource wait expired before #{task}; no score recorded")
        time.sleep(15)
    print(f"Paused before #{task}", flush=True)
    return False


def qualifies_for_defer(result: dict, task: str) -> bool:
    return (result.get("task") == task and result.get("status") == "incomplete"
            and result.get("stage") == "prediction"
            and result.get("failure_category") in {"api_timeout", PlannerJsonDecodeError.category}
            and result.get("score") is None and not result.get("cleanup_error"))


def previous_shard(root: Path, current: Path, role: str, ids: list[str], fingerprint: str) -> Path:
    prior = root.resolve() / role
    expected = {"campaign_sha256": fingerprint, "role": role, "ids": ids}
    if prior == current:
        raise ValueError("R3.1 and previous R3 results must use distinct directories")
    if read(prior / "shard_identity.json") != expected:
        raise ValueError("Previous results do not match the pinned R3 campaign and selected shard")
    return prior


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "prepare", "preflight", "smoke", "run"))
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--previous-results", type=Path,
                        help="Read-only root of the original R3 results, containing the role directory")
    parser.add_argument("--gateway-config", type=Path)
    parser.add_argument("--only", choices=("tasks", "assets", "image", "all"), default="all")
    args = parser.parse_args()
    data = args.data.resolve()
    out = (args.out or data / "uipath_campaign_results_r31_json_retry").resolve() / args.role
    spec_file = HERE / "campaign.json"
    spec = read(spec_file)
    fingerprint = sha(spec_file)
    for name, digest in spec["code_sha256"].items():
        src = HERE / name
        # prepare tasks is the one mode allowed to restore missing task files.
        if args.mode == "prepare" and name.startswith("OSWorld-v2.1/evaluation_examples/task_class/task_"):
            continue
        if not src.is_file() or sha(src) != digest:
            raise ValueError("Code mismatch: " + name)
    ids = assigned(spec["shards"], read(CODE / "evaluation_examples/test_v2.json")["tasks"], args.role)
    prior = (previous_shard(args.previous_results, out, args.role, ids,
                            spec["previous_campaign_sha256"]) if args.previous_results else None)
    print(f"{args.role}: {len(ids)} tasks; smoke #{ids[0]}; campaign SHA256={fingerprint}", flush=True)
    env = dict(os.environ, OSWORLD21_DATA=str(data), OSWORLD21_CODE=str(CODE), OSWORLD21_RESULTS=str(out),
               OSWORLD21_SOL_MODEL=MODEL, OSWORLD21_GATEWAY_PROFILE="openlux_existing",
               WEBSITE_HOST_SUFFIX="site.hku.icu")
    def run(args: list[str], **kwargs):
        return subprocess.run([sys.executable, "-u", "-B", "-X", "utf8", *args], env=env, cwd=HERE, **kwargs)
    if args.mode == "prepare":
        run([str(HERE / "prepare_data.py"), "--only", args.only], check=True)
        return
    run([WORKER, "--verify"], check=True)
    if args.mode == "check":
        return
    if not args.gateway_config or not args.gateway_config.is_file():
        parser.error("Point --gateway-config to your local OpenLux configuration")
    env["OSWORLD21_ENV"] = str(args.gateway_config.resolve())
    run(["-c", "from gateway_settings import gateway_settings; from native_qemu_v2 import preflight_paths; "
               "import os; from pathlib import Path; "
               "gateway_settings(Path(os.environ['OSWORLD21_ENV']), 'openlux_existing'); preflight_paths()"], check=True)
    image = Path(env.get("OSWORLD21_QCOW2", str(data / "v2_official_image/osworld-v2-ubuntu-x86.qcow2"))).resolve()
    if sha(image) != spec["qcow2_sha256"]:
        raise ValueError("Base QCOW2 SHA256 mismatch")
    if not any(p.is_file() for p in (data / "v2_assets").rglob("*")):
        raise ValueError("Task assets missing; use prepare --only assets")
    def probe(extra: list[str], *, check: bool):
        return run([str(HERE / "model_probe.py"), "--env-file", env["OSWORLD21_ENV"], "--profile",
                    "openlux_existing", "--model", MODEL, *extra], check=check)
    if args.mode == "preflight":
        for extra in ([], ["--vision"]):
            probe(extra, check=True)
        return
    out.mkdir(parents=True, exist_ok=True)
    guard = RuntimeGuard(data / "uipath_campaign_host.lock")
    guard.acquire()
    try:
        identity = {"campaign_sha256": fingerprint, "role": args.role, "ids": ids}
        identity_path = out / "shard_identity.json"
        if identity_path.exists() and read(identity_path) != identity:
            raise ValueError("Output folder belongs to another campaign; set --out to a new folder")
        write(identity_path, identity)
        def already_valid(task: str, smoke: bool = False) -> bool:
            if any(has_evidence(p, task, fingerprint, smoke) for p in (out / task).glob("attempt_*") if p.is_dir()):
                return True
            if prior is not None and any(
                    has_evidence(p, task, spec["previous_campaign_sha256"], smoke)
                    for p in (prior / task).glob("attempt_*") if p.is_dir()):
                print(f"#{task} has valid R3 evidence; keeping the existing result", flush=True)
                return True
            return False
        if args.mode == "run" and not already_valid(ids[0], smoke=True):
            raise ValueError("Run this role's smoke in this output folder before batch mode")
        deferred = []
        for task in (ids[:1] if args.mode == "smoke" else ids):
            if already_valid(task, smoke=args.mode == "smoke"):
                continue
            if (out / "STOP").exists():
                print("Paused before #" + task)
                return
            home = out / task
            home.mkdir(parents=True, exist_ok=True)
            for minimum in (BATCH_START_COMMIT_MIB, 6656):
                if not wait_for_resources(out, data, image, task, minimum):
                    return
                attempt = home / ("attempt_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
                print(f"Starting #{task}: {attempt}", flush=True)
                with attempt.with_suffix(".log").open("wb") as log:
                    run([WORKER, "--framework", "ui_path_screen_agent", "--task", task, "--out", str(attempt)],
                        stdout=log, stderr=subprocess.STDOUT, check=False)
                if attempt.is_dir():
                    write(attempt / "run_identity.json", dict(identity, task=task, qcow2_sha256=spec["qcow2_sha256"],
                          gateway="openlux_existing", python=sys.version.split()[0]))
                result_file = attempt / "task_result.json"
                result = read(result_file) if result_file.is_file() else {}
                if (minimum == BATCH_START_COMMIT_MIB and result.get("status") == "blocked_resources"
                        and result.get("reason") == "Insufficient physical/commit headroom for guest and Python runtime"):
                    print("Loaded worker did not meet the unchanged VM memory gate; waiting for more host headroom.", flush=True)
                    continue  # No VM, no request, no GUI action at this point.
                break
            if not has_evidence(attempt, task, fingerprint, args.mode == "smoke"):
                if args.mode == "run" and qualifies_for_defer(result, task):
                    append_event(out / "deferred_events.jsonl",
                                 {"task": task, "attempt": attempt.name, "reason": result["failure_category"],
                                  "score": None, "time": time.time()})
                    deferred.append(task)
                    if probe([], check=False).returncode == 0:
                        print(f"#{task} deferred without a score; gateway responding, continuing", flush=True)
                        continue
                    raise RuntimeError(f"#{task} deferred without a score; gateway probe failed, queue paused")
                raise RuntimeError(f"#{task} lacks valid evidence; inspect the log before resuming. Not counted as 0.")
        print(f"{args.mode} loop finished for {args.role}; deferred this pass: {', '.join(deferred) or 'none'}", flush=True)
    finally:
        guard.close()


if __name__ == "__main__":
    main()

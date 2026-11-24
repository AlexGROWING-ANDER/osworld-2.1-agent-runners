"""Run a separate OSWorld 2.1 Agent-S2 #001–#054 campaign in the existing Windows harness."""
import argparse
from datetime import datetime
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
BENCH = Path("H:/GUI_AGENT_OSWorld")
SOURCE = Path("H:/GUI_AGENT_runs/OSWORLD_HOST_SWAP_20261115")
UIPATH = Path("H:/GUI_AGENT_runs/OSWORLD21_UNIPATH_SOL_SMOKE_20261119/campaign_r31_json_retry/UiPath_OSWorld21_GPT61Sol")
OUT = BENCH / "agent_s2_gpt61sol_medium_150_first54"
MODEL = "gpt-6.1-sol"
EFFORT = "medium"
STEP_LIMIT = 150
TASKS = ("004",) + tuple(f"{n:03d}" for n in range(1, 55) if n != 4)


def save(path, data):
    fresh = path.with_suffix(".pending")
    fresh.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    fresh.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_gateway(config):
    sys.path.insert(0, str(UIPATH))
    from gateway_settings import gateway_settings
    return gateway_settings(config.resolve(strict=True), "openlux_existing")


def configure_worker(config, task):
    # Keep native math libraries from allocating their default thread pools before the first Agent-S2 step.
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    from dotenv import dotenv_values
    from uipath_gitlab import GUEST_STATUS, SETTINGS, install_guest_ca

    url, key = verified_gateway(config)
    gitlab_ip, pages_ip = (HERE / "host-only-ip.txt").read_text().split()
    gitlab_url = f"https://{gitlab_ip}:18182"
    local_gitlab = dotenv_values(SETTINGS, encoding="utf-8", interpolate=False) if SETTINGS.is_file() else {}
    host_verified = GUEST_STATUS.is_file() and json.loads(GUEST_STATUS.read_bytes()).get("gitlab") == gitlab_url
    if not host_verified or local_gitlab.get("GITLAB_URL") != gitlab_url:
        local_gitlab = {}
    os.environ["OSWORLD_DESKTOP_GUEST_MB"] = "4096"
    sys.path.insert(0, str(SOURCE))
    import desktop_worker as worker
    worker.MODEL = MODEL
    worker.STEPS = STEP_LIMIT
    worker.environment()

    from bounded_api_transport import BoundedApiTransport
    worker.load_evaluators_on_demand()
    from desktop_env.desktop_env import DesktopEnv
    worker.api_client = lambda: SolMediumTransport(BoundedApiTransport(url, key, 480, trust_env=False))
    os.environ.update(
        REQUESTS_CA_BUNDLE=str(HERE / "requests-ca-bundle.pem"),
        NO_PROXY=f"localhost,127.0.0.1,{gitlab_ip},{pages_ip}",
        no_proxy=f"localhost,127.0.0.1,{gitlab_ip},{pages_ip}",
        OSWORLD_STRICT_SETUP_TRANSPORT="1",
    )

    if local_gitlab:
        os.environ.update(GITLAB_URL=local_gitlab["GITLAB_URL"],
                          GITLAB_PRIVATE_TOKEN=local_gitlab["GITLAB_PRIVATE_TOKEN"])
    if task == "035":
        os.environ.update(OSWORLD_EVAL_MODEL_PROVIDER="openai",
                          OSWORLD_EVAL_MODEL_NAME="gpt-5.2",
                          OSWORLD_EVAL_MODEL_BASE_URL=url,
                          OSWORLD_EVAL_MODEL_API_KEY=key)
    original_setup = DesktopEnv._call_task_setup

    def task_setup(env, task, use_proxy):
        if task.id in ("026", "041"):
            install_guest_ca(env.setup_controller)
        return original_setup(env, task, use_proxy)

    DesktopEnv._call_task_setup = task_setup
    write = worker.write

    def write_result(path, value):
        if path.name == "task_result.json":
            value["reasoning_effort"] = EFFORT
            if task == "035":
                value["evaluation_model"] = "gpt-5.2"
            value["adaptation"] = "GPT-6.1 Sol language and grounding; existing literal ACI dispatch"
        write(path, value)

    worker.write = write_result
    return worker


class SolMediumTransport:
    def __init__(self, transport):
        self.transport = transport

    def create(self, **kwargs):
        response = self.transport.create(**dict(kwargs, reasoning_effort=EFFORT))
        if str(response.model).casefold() != MODEL.casefold():
            raise ValueError("Unexpected gateway response model")
        return response

    def close(self):
        self.transport.close()


def complete(folder, task):
    try:
        row = json.loads((folder / "task_result.json").read_bytes())
        desired = (task, "agent_s2", MODEL, EFFORT, STEP_LIMIT, 4096, "completed")
        fields = ("task", "architecture", "model", "reasoning_effort", "max_steps", "guest_memory_mb", "status")
        if tuple(row.get(field) for field in fields) != desired:
            return False
        from attempt_integrity import inspect_attempt
        official = BENCH / "OSWorld-v2.1/evaluation_examples/task_class" / f"task_{task}.py"
        return inspect_attempt(folder, row, official)["usable"]
    except (OSError, ValueError):
        return False


def wait_for_ui_path():
    import psutil
    from uipath_gitlab import GUEST_STATUS, RESULTS

    workflow = json.loads((HERE / "current_workflow.json").read_text(encoding="utf-8-sig"))
    log = Path(workflow["log"])
    print("Agent-S2 queue is waiting for both UiPath cases", flush=True)
    while True:
        if (OUT / "STOP").exists():
            return False
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        if GUEST_STATUS.is_file() and all(f"UiPath #{task}:" in text for task in ("026", "041")):
            return all(any((p / "api_events.jsonl").is_file() for p in (RESULTS / task).glob("attempt_*"))
                       for task in ("026", "041"))
        try:
            running = psutil.Process(workflow["pid"]).is_running()
        except psutil.NoSuchProcess:
            running = False
        if not running:
            return False
        time.sleep(20)


def wait_for_resources(task):
    from run_reliability import ResourceBlocked, check_memory, memory_snapshot

    waiting = None
    while not (OUT / "STOP").exists():
        phase = None
        try:
            check_memory(memory_snapshot(), 4096, 1200)
        except ResourceBlocked:
            phase = "waiting_memory"
        if min(shutil.disk_usage(path).free for path in (BENCH, OUT)) < 8 * 1024**3:
            phase = "waiting_disk"
        if phase is None:
            save(OUT / "runtime.json", {"task": task, "phase": "ready"})
            return True
        if waiting != phase:
            waiting = phase
            save(OUT / "runtime.json", {"task": task, "phase": phase})
            print(f"Agent-S2 #{task}: {phase}", flush=True)
        time.sleep(25)
    return False


def has_model_response(attempt):
    api_log = attempt / "api_events.jsonl"
    if not api_log.is_file():
        return False
    for line in api_log.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("phase") == "request_completed" and event.get("model") == MODEL:
            return True
    return False


def run_attempt(config, task, out):
    import psutil

    log_path = out.with_suffix(".log")
    watched = [log_path] + [out / f for f in (
        "traj.jsonl", "runtime_events.jsonl", "api_events.jsonl", "action_events.jsonl"
    )]
    with log_path.open("wb") as log:
        worker = subprocess.Popen(
            [sys.executable, "-B", "-u", "-X", "utf8", __file__, "worker", "--gateway-config",
             str(config), "--task", task, "--out", str(out)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        activity = {}
        last_change = time.monotonic()
        last_step = last_change
        trajectory_bytes = 0
        while worker.poll() is None:
            sizes = {p: p.stat().st_size for p in watched if p.is_file()}
            if sizes != activity:
                activity = sizes
                last_change = time.monotonic()
            current_steps = sizes.get(out / "traj.jsonl", 0)
            if current_steps != trajectory_bytes:
                trajectory_bytes = current_steps
                last_step = time.monotonic()
            stalled = time.monotonic() - last_change > 20 * 60
            no_step = time.monotonic() - last_step > 12 * 60 and has_model_response(out)
            if stalled or no_step:
                parent = psutil.Process(worker.pid)
                owned = parent.children(recursive=True) + [parent]
                for proc in owned:
                    if proc.is_running():
                        try:
                            proc.terminate()
                        except psutil.NoSuchProcess:
                            pass
                _, live = psutil.wait_procs(owned, timeout=10)
                for proc in live:
                    try:
                        proc.kill()
                    except psutil.NoSuchProcess:
                        pass
                reason = "stalled_without_step" if no_step else "stalled"
                print(f"Agent-S2 #{task} {reason}. Keeping the partial attempt.", flush=True)
                return reason
            time.sleep(15)
    return "worker_exited"


def queued(config, source_identity):
    sys.path.insert(0, str(SOURCE))
    OUT.mkdir(parents=True, exist_ok=True)
    previous = OUT / "batch_identity.json"
    if previous.exists() and json.loads(previous.read_bytes()) != source_identity:
        raise RuntimeError("Existing output directory belongs to another configuration")
    save(previous, source_identity)
    if not wait_for_ui_path():
        if (OUT / "STOP").exists():
            return 0
        print("UiPath or GitLab is blocked. Starting independent Agent-S2 cases.", flush=True)

    os.environ["OSWORLD_COMMIT_HEADROOM_MB"] = "512"
    sys.path.insert(0, str(UIPATH))
    from run_reliability import RuntimeGuard
    from uipath_gitlab import GUEST_STATUS

    guard = RuntimeGuard(OUT / "queue.lock")
    guard.acquire()
    try:
        status_path = OUT / "queue_status.json"
        state = json.loads(status_path.read_bytes()) if status_path.is_file() else {}
        for task in TASKS:
            if task in state:
                continue
            prior = next((p for p in sorted((OUT / task).glob("attempt_*"), reverse=True) if complete(p, task)), None)
            if prior:
                state[task] = {"status": "completed", "attempt": str(prior)}
                save(OUT / "queue_status.json", state)
                continue
            if (OUT / "STOP").exists():
                break
            if task == "004":
                deferred = OUT / "004" / "deferred_initial_attempt.json"
                if deferred.is_file():
                    info = json.loads(deferred.read_bytes())
                    attempt = Path(info["attempt"])
                    if attempt.parent == OUT / "004" and has_model_response(attempt):
                        state[task] = info
                        save(OUT / "queue_status.json", state)
                        print("Agent-S2 #004 deferred after repeated no-action replanning; continuing", flush=True)
                        continue
            if task in ("026", "041") and not GUEST_STATUS.is_file():
                state[task] = {"status": "deferred", "stage": "gitlab_preflight"}
                save(OUT / "queue_status.json", state)
                print(f"Agent-S2 #{task} deferred for GitLab; continuing", flush=True)
                continue
            if not wait_for_resources(task):
                break
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            out = OUT / task / ("attempt_" + stamp)
            out.parent.mkdir(parents=True, exist_ok=True)
            print(f"Agent-S2 #{task} starting, {out}", flush=True)
            process_status = run_attempt(config, task, out)
            result_file = out / "task_result.json"
            row = json.loads(result_file.read_bytes()) if result_file.is_file() else {"status": process_status}
            good = complete(out, task)
            state[task] = {"status": "completed" if good else row.get("status", "incomplete"),
                           "score": row.get("score") if good else None,
                           "stage": row.get("stage"), "attempt": str(out)}
            save(OUT / "queue_status.json", state)
            print(f"Agent-S2 #{task} {state[task]['status']}", flush=True)
            if row.get("failure_category") in {"authentication_or_permission", "billing_or_payment"}:
                stop = OUT / "STOP"
                if not stop.exists():
                    stop.write_text("Paused at a case boundary after a gateway access or billing rejection.\n", encoding="utf-8")
                index = TASKS.index(task) + 1
                save(OUT / "runtime.json", {
                    "task": TASKS[index] if index < len(TASKS) else None,
                    "phase": "waiting_gateway",
                    "previous_task": task,
                })
                print("Gateway rejected the model request; holding the remaining queue.", flush=True)
                return 0
            if task == "004" and not good and not has_model_response(out):
                print("Agent-S2 #004 did not produce a successful model request. Batch held for infrastructure repair.", flush=True)
                return 1
    finally:
        guard.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "run", "worker"))
    parser.add_argument("--gateway-config", type=Path, required=True)
    parser.add_argument("--task", choices=TASKS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    identity = {"release": "osworld-v2.1", "framework": "agent_s2",
                "model": MODEL, "reasoning_effort": EFFORT, "max_steps": STEP_LIMIT,
                "guest_mib": 4096, "ids": list(TASKS),
                "original_worker_sha256": sha(SOURCE / "desktop_worker.py"),
                "adapter_sha256": sha(Path(__file__))}
    if args.mode == "check":
        verified_gateway(args.gateway_config)
        official = json.loads((BENCH / "OSWorld-v2.1/evaluation_examples/test_v2.json").read_bytes())["tasks"]
        assert len(set(TASKS)) == 54 and set(TASKS) <= set(official)
        print("Agent-S2 54-task shard, existing OpenLux config, GPT-6.1 Sol medium, 150 steps verified")
        return 0
    if args.mode == "run":
        return queued(args.gateway_config, identity)
    if not args.task or not args.out:
        parser.error("worker requires one shard task and an output path")
    worker = configure_worker(args.gateway_config, args.task)
    result = worker.run_one("agent_s2", args.task, args.out)
    if args.out.is_dir():
        save(args.out / "run_identity.json", identity | {"task": args.task})
    return 0 if result == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

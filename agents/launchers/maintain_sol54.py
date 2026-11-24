"""Keep the authorized Agent-S2 queue making progress while the client is idle."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil

from agent_s2_sol54 import EFFORT, HERE, MODEL, OUT, STEP_LIMIT, TASKS, has_model_response, save, sha


CURRENT = HERE / "agent_s2_current.json"
ENTRY = HERE / "agent_s2_sol54.py"
POLL_SECONDS = 25
STEP_STALL_SECONDS = 12 * 60
FIRST_STEP_TIMEOUT_SECONDS = 30 * 60


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def sync_retry_queue(rows):
    path = OUT / "retry_queue.json"
    previous = read_json(path)
    pending = previous.copy()
    for task in TASKS:
        row = rows.get(task)
        if row is None:
            continue
        status = row.get("status", "incomplete")
        if status == "completed" and ("score" not in row or row["score"] is not None):
            pending.pop(task, None)
            continue
        attempt = Path(row["attempt"]) if row.get("attempt") else None
        marker = read_json(attempt / "scheduler_stall.json") if attempt else {}
        result = read_json(attempt / "task_result.json") if attempt else {}
        reason = (
            marker.get("reason")
            or result.get("failure_category")
            or row.get("reason")
            or ("invalid_or_missing_score" if status == "completed" else status)
        )
        pending[task] = {
            "status": "pending",
            "original_status": status,
            "stage": marker.get("stage") or row.get("stage"),
            "reason": reason,
            "attempt": row.get("attempt"),
        }
    if pending != previous or not path.exists():
        save(path, pending)
        print(f"Agent-S2 retry queue synced: {len(pending)} pending.", flush=True)


def watch_progress(worker, task, attempt):
    trajectory = attempt / "traj.jsonl"
    info = trajectory.stat() if trajectory.is_file() else None
    last_step = info.st_mtime if info and info.st_size else worker.create_time()
    elapsed = time.time() - last_step
    if elapsed < STEP_STALL_SECONDS:
        return
    if has_model_response(attempt):
        stop_stalled(worker, task, attempt, "prediction", "no_completed_step_for_12_minutes")
    elif elapsed >= FIRST_STEP_TIMEOUT_SECONDS:
        attempt.mkdir(parents=True, exist_ok=True)
        stop_stalled(worker, task, attempt, "setup_or_initial_prediction", "no_step_or_model_response_for_30_minutes")


def queue(state):
    try:
        proc = psutil.Process(state["pid"])
        args = proc.cmdline()
        if any(
            arg.lower() == str(ENTRY).lower()
            or (arg.lower() == ENTRY.name.lower() and Path(proc.cwd()).resolve() == HERE.resolve())
            for arg, mode in zip(args, args[1:])
            if mode == "run"
        ):
            return proc
    except psutil.NoSuchProcess:
        pass
    return None


def workers(proc):
    for child in proc.children(recursive=True):
        try:
            args = child.cmdline()
            if "worker" in args and "--task" in args and "--out" in args:
                task = args[args.index("--task") + 1]
                out = Path(args[args.index("--out") + 1])
                if task in TASKS and out.resolve().is_relative_to(OUT / task):
                    yield child, task, out
        except (psutil.NoSuchProcess, IndexError):
            continue


def defer(task, attempt, reason, stage="prediction"):
    data = {"status": "deferred", "stage": stage, "score": None,
            "attempt": str(attempt), "reason": reason}
    save(attempt / "scheduler_stall.json", data)
    if task == "004":
        save(OUT / task / "deferred_initial_attempt.json", data)


def stop_stalled(proc, task, attempt, stage, reason):
    try:
        processes = proc.children(recursive=True) + [proc]
    except psutil.NoSuchProcess:
        return
    defer(task, attempt, reason, stage)
    print(f"Agent-S2 #{task} deferred: {reason}; keeping original files.", flush=True)
    for child in processes:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, still_alive = psutil.wait_procs(processes, timeout=8)
    for child in still_alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass


def restart(config):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    identity = OUT / "batch_identity.json"
    if identity.is_file():
        old = json.loads(identity.read_bytes())
        if (old["model"], old["reasoning_effort"], old["max_steps"]) != (MODEL, EFFORT, STEP_LIMIT):
            raise RuntimeError("Cannot restart a different campaign")
        if old.get("adapter_sha256") != sha(ENTRY):
            identity.replace(OUT / f"batch_identity_before_scheduler_{stamp}.json")

    stdout = HERE / f"agent_s2_sol54_{stamp}_resume.log"
    stderr = HERE / f"agent_s2_sol54_{stamp}_resume.err.log"
    with stdout.open("ab") as handle, stderr.open("ab") as errors:
        proc = subprocess.Popen([sys.executable, "-B", "-u", "-X", "utf8", str(ENTRY),
                                 "run", "--gateway-config", str(config)],
                                cwd=HERE, stdin=subprocess.DEVNULL, stdout=handle, stderr=errors,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    save(CURRENT, {"pid": proc.pid, "log": str(stdout), "errors": str(stderr)})
    print(f"Resumed the Agent-S2 scheduler, PID {proc.pid}", flush=True)


def supervise(config):
    print("Watching the existing Agent-S2 queue and its official step receipts", flush=True)
    restarts = 0
    seen = set()
    watched_pid = None
    while True:
        rows = read_json(OUT / "queue_status.json")
        sync_retry_queue(rows)
        if rows.keys() - seen:
            restarts = 0
        seen = set(rows)

        state = read_json(CURRENT)
        try:
            proc = queue(state) if state else None
        except psutil.AccessDenied:
            print("Cannot inspect the current scheduler yet; will recheck.", flush=True)
            time.sleep(POLL_SECONDS)
            continue
        if proc is not None:
            if watched_pid != proc.pid:
                print(f"Adopted Agent-S2 scheduler, PID {proc.pid}.", flush=True)
                watched_pid = proc.pid
            try:
                active = next(workers(proc), None)
                if active:
                    watch_progress(*active)
            except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
                pass
            time.sleep(POLL_SECONDS)
            continue

        if (OUT / "STOP").exists():
            print("The scheduled pass is paused. Retry records are up to date.", flush=True)
            return
        if set(TASKS) <= rows.keys():
            print("The scheduled pass finished. Unscored cases are saved in retry_queue.json.", flush=True)
            return
        log = Path(state["log"]) if state.get("log") else None
        trace = log.read_text(encoding="utf-8", errors="replace") if log and log.is_file() else ""
        if "Batch held for infrastructure repair" in trace:
            print("Batch held because no successful model request was observed.", flush=True)
            return
        smoke = rows.get("004", {})
        attempt = Path(smoke["attempt"]) if smoke.get("attempt") else None
        if attempt and smoke.get("status") != "completed" and has_model_response(attempt):
            defer("004", attempt, "First attempt has a successful model request and no valid final score")
        if restarts >= 2:
            print("The scheduler failed two recovery attempts. Logs were preserved.", flush=True)
            return
        restart(config)
        restarts += 1
        time.sleep(15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-config", type=Path, required=True)
    args = parser.parse_args()

    from agent_s2_sol54 import UIPATH

    sys.path.insert(0, str(UIPATH))
    from run_reliability import ResourceBlocked, RuntimeGuard

    guard = RuntimeGuard(OUT / "maintenance.lock")
    try:
        guard.acquire()
    except ResourceBlocked:
        print("Agent-S2 already has a queue supervisor.", flush=True)
        return
    try:
        supervise(args.gateway_config)
    finally:
        guard.close()


if __name__ == "__main__":
    main()

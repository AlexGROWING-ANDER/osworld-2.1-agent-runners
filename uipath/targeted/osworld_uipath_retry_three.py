"""Preflight and retry the three interrupted cases in separate workers."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-config", type=Path, required=True)
    parser.add_argument("--task", choices=("021", "026", "030", "041", "048"), action="append")
    args = parser.parse_args()
    local = Path("H:/GUI_AGENT_OSWorld/gitlab_deploy/local_vm")
    results = local.parents[1] / "uipath_gitlab_r31_targeted"
    config = str(args.gateway_config.resolve(strict=True))
    preflight = Path(__file__).with_name("osworld_gitlab_resume_eval.py")
    cases = args.task or ("021", "026", "041")
    if any(case in ("026", "041") for case in cases):
        subprocess.run([sys.executable, "-B", "-u", str(preflight), "--start-if-needed",
                        "--preflight-only", "--gateway-config", config], check=True)
    for case in cases:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = local / f"retry_{case}_{stamp}.log"
        print(f"Starting #{case}. {log_path}", flush=True)
        with log_path.open("wb") as log:
            process = subprocess.Popen([sys.executable, "-B", "-u", str(local / "uipath_gitlab.py"),
                                        "--gateway-config", config, "--task", case, "run"],
                                       cwd=local, stdout=log, stderr=subprocess.STDOUT)
            last_activity = time.monotonic()
            previous = None
            while process.poll() is None:
                attempts = sorted((results / case).glob("attempt_*"), reverse=True)
                attempt = next((path for path in attempts if path.name[8:] >= stamp), None)
                if attempt is not None:
                    watched = [attempt / name for name in ("traj.jsonl", "action_events.jsonl")]
                    signature = tuple(path.stat().st_size if path.is_file() else 0 for path in watched)
                    if signature != previous:
                        previous = signature
                        last_activity = time.monotonic()
                    if time.monotonic() - last_activity > 20 * 60:
                        children = psutil.Process(process.pid).children(recursive=True)
                        process.terminate()
                        for child in children:
                            try:
                                child.terminate()
                            except psutil.NoSuchProcess:
                                pass
                        _, alive = psutil.wait_procs(children, timeout=10)
                        for child in alive:
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                        process.wait(timeout=10)
                        print(f"#{case} stalled. Kept {attempt} for retry.", flush=True)
                        break
                time.sleep(10)
        print(f"#{case} worker exited with {process.returncode}", flush=True)
        attempts = sorted((results / case).glob("attempt_*"), reverse=True)
        attempt = next((path for path in attempts if path.name[8:] >= stamp), None)
        if attempt is not None:
            result_path = attempt / "task_result.json"
            result = json.loads(result_path.read_bytes()) if result_path.is_file() else {}
        else:
            result = {}
        if result.get("score") is None:
            results.mkdir(parents=True, exist_ok=True)
            with (results / "deferred_events.jsonl").open("a", encoding="utf-8") as deferred:
                deferred.write(json.dumps({"case": case, "attempt": str(attempt) if attempt else None,
                                           "reason": result.get("failure_category") or result.get("error_type")
                                           or "worker_exited_without_score"}) + "\n")


if __name__ == "__main__":
    main()

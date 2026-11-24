"""Select the second computer's Agent-S2 queue using the shared runner."""
import argparse
from datetime import datetime
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "run"))
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--gateway-config", type=Path, required=True)
    parser.add_argument("--local", action="store_true", help="Resume the original #001–#027 half on the primary computer")
    args = parser.parse_args()
    runner_path = args.runner.resolve(strict=True)
    sys.path.insert(0, str(runner_path.parent))
    import agent_s2_sol54 as runner

    if Path(runner.__file__).resolve() != runner_path:
        raise RuntimeError("Unexpected shared runner path")
    if (runner.MODEL, runner.EFFORT, runner.STEP_LIMIT) != ("gpt-6.1-sol", "medium", 150):
        raise RuntimeError("Unexpected shared runner settings")
    runner.TASKS = (tuple(task for task in runner.TASKS if int(task) <= 27) if args.local
                    else tuple(f"{number:03d}" for number in range(28, 55)))
    if not args.local:
        runner.OUT = runner.BENCH / "agent_s2_gpt61sol_medium_150_028_054"
    runner.wait_for_ui_path = lambda: True
    official = runner.BENCH / "OSWorld-v2.1/evaluation_examples/test_v2.json"
    if not set(runner.TASKS) <= set(json.loads(official.read_bytes())["tasks"]):
        raise RuntimeError("Official task shard is incomplete")
    runner.verified_gateway(args.gateway_config)
    identity = {
        "release": "osworld-v2.1", "framework": "agent_s2", "model": runner.MODEL,
        "reasoning_effort": runner.EFFORT, "max_steps": runner.STEP_LIMIT, "guest_mib": 4096,
        "ids": list(runner.TASKS), "original_worker_sha256": runner.sha(runner.SOURCE / "desktop_worker.py"),
        "adapter_sha256": runner.sha(runner_path), "shard_launcher_sha256": runner.sha(Path(__file__)),
    }
    shard = "#001–#027" if args.local else "#028–#054"
    print(f"Verified {len(runner.TASKS)} tasks, {shard}, {runner.MODEL} {runner.EFFORT}, {runner.STEP_LIMIT} steps")
    if args.local and args.mode == "run":
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        saved = json.loads((runner.OUT / "batch_identity.json").read_bytes())
        if any(saved.get(key) != identity[key] for key in (
            "release", "framework", "model", "reasoning_effort", "max_steps", "guest_mib",
            "original_worker_sha256", "adapter_sha256",
        )) or not set(runner.TASKS) <= set(saved["ids"]):
            raise RuntimeError("Existing Agent-S2 campaign identity does not match")
        state_file = runner.OUT / "queue_status.json"
        if state_file.exists():
            current = json.loads(state_file.read_bytes())
            runner.save(runner.OUT / f"queue_status.before_local_resume_{stamp}.json", current)
            runner.save(state_file, {task: row for task, row in current.items()
                                     if task not in runner.TASKS or row.get("status") == "completed"})
        stop = runner.OUT / "STOP"
        if stop.exists():
            stop.rename(runner.OUT / f"STOP.before_local_resume_{stamp}")
        identity = saved
    return runner.queued(args.gateway_config, identity) if args.mode == "run" else 0


if __name__ == "__main__":
    raise SystemExit(main())

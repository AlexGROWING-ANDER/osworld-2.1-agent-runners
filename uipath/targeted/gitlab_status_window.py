"""Show the local GitLab UiPath run and host memory in a floating window."""

from pathlib import Path
import sys
import tkinter as tk

import psutil
from agent_s2_status_window import latest_event, read_object


ROOT = Path(r"H:\GUI_AGENT_OSWorld")
RESULTS = ROOT / "uipath_gitlab_r31_targeted"
AGENTS = ROOT / "agent_s2_gpt61sol_medium_150_first54"
RUNTIME = Path(r"H:\GUI_AGENT_runs\OSWORLD21_UNIPATH_SOL_SMOKE_20261119\campaign_r31_json_retry\UiPath_OSWorld21_GPT61Sol")
TITLE = "GitLab · UiPath 评测监督器"
sys.path.insert(0, str(RUNTIME))
from run_reliability import memory_snapshot


def progress() -> str:
    processes = list(psutil.process_iter(["name", "cmdline", "create_time"], ad_value=None))
    python = [p.info for p in processes if (p.info["name"] or "").lower().startswith("python")]
    started = max((p["create_time"] or 0 for p in python if any(
        Path(arg).name == "osworld_uipath_retry_three.py" for arg in p["cmdline"] or [])), default=0)
    live = bool(started)

    rows = []
    for task in ("021", "026", "030", "041", "048"):
        attempt = max((RESULTS / task).glob("attempt_*/"), default=None)
        if not attempt:
            rows.append(f"#{task}  待启动")
            continue
        result = read_object(attempt / "task_result.json")
        api = latest_event(attempt / "api_events.jsonl")
        action = latest_event(attempt / "action_events.jsonl")
        if live and attempt.stat().st_ctime >= started and not result:
            step = max(api.get("step") or 0, action.get("step") or 0)
            state = f"第{step}步 · 正在运行" if step else "正在初始化"
        elif result.get("score") is not None:
            state = f"已出分 {result['score']}"
        elif result:
            state = "上次未出分"
        else:
            state = "运行已中断"
        rows.append(f"#{task}  {state}")
    agent = next((p for p in python if any(Path(arg).name == "agent_s2_028_054.py"
                 for arg in p["cmdline"] or [])), None)
    runtime = read_object(AGENTS / "runtime.json")
    detail = f"Agent-S2 · #{runtime.get('task', '—')} · {runtime.get('phase', '启动中')}" if agent else "Agent-S2 · 未运行"
    if live:
        active = next((row for row in rows if "正在运行" in row or "正在初始化" in row), None)
        return "\n".join(["UiPath · 后台运行中", active or "等待初始化或分配案例"])
    if agent:
        task = runtime.get("task")
        attempt = max((AGENTS / task).glob("attempt_*/"), default=None) if task else None
        event = latest_event(attempt / "action_events.jsonl") if attempt else {}
        step = event.get("step")
        if runtime.get("phase") == "ready":
            return "\n".join(["Agent-S2 · 后台运行中", f"#{task} · 第{step}步" if step else f"#{task} · 正在初始化"])
        return detail
    return "当前没有评测在运行"


def main() -> None:
    window = tk.Tk()
    window.title("OSWorld 评测监督器")
    window.geometry(f"380x112+{max(0, window.winfo_screenwidth() - 410)}+80")
    window.attributes("-topmost", True)
    statuses = []
    for index in range(2):
        label = tk.Label(window, anchor="w", font=("Microsoft YaHei UI", 11))
        label.pack(fill="x", padx=14, pady=(9 if index == 0 else 0, 0))
        statuses.append(label)
    memory = tk.Label(window, justify="left", anchor="w", font=("Microsoft YaHei UI", 10, "bold"))
    memory.pack(fill="x", padx=14, pady=(2, 8))

    def refresh() -> None:
        lines = progress().splitlines()
        for index, label in enumerate(statuses):
            line = lines[index] if index < len(lines) else ""
            running = "正在运行" in line or "后台运行中" in line
            label.configure(
                text=line,
                fg="#126435" if running else "#253950",
                bg="#d7f5df" if running else window.cget("bg"),
                font=("Microsoft YaHei UI", 11, "bold" if running else "normal"),
            )
        snapshot = memory_snapshot()
        physical = snapshot["free_physical_mb"]
        commit = snapshot["free_commit_mb"]
        low = physical < 1200 or commit < 4608
        memory.configure(
            text=f"{'内存余量低' if low else '内存余量充足'}\n可用物理 {physical:,} MiB · 提交余量 {commit:,} MiB",
            fg="#a52119" if low else "#186849",
        )
        window.after(3000, refresh)

    refresh()
    window.mainloop()


if __name__ == "__main__":
    main()

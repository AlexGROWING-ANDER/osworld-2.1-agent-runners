"""Display the existing Agent-S2 queue without controlling it."""

import json
import tkinter as tk
from datetime import datetime
from pathlib import Path

import psutil


RUN = Path(r"H:\GUI_AGENT_OSWorld\agent_s2_gpt61sol_medium_150_first54")
LAUNCH = RUN.parent / "gitlab_deploy" / "local_vm" / "agent_s2_current.json"
TITLE = "OSWorld 2.1 · Agent-S2 监督器"


def read_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_event(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 16384))
            lines = stream.read().splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            return event
    return {}


def queue_alive() -> bool:
    pid = read_object(LAUNCH).get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        args = psutil.Process(pid).cmdline()
    except psutil.Error:
        return False
    return any(Path(arg).name.lower() == "agent_s2_sol54.py" and mode == "run"
               for arg, mode in zip(args, args[1:]))


def describe(runtime: dict, rows: dict, alive: bool, retry_handoff: bool = False) -> tuple[str, str]:
    task = str(runtime.get("task") or "")
    phase = runtime.get("phase")
    if len(rows) >= 54:
        return "主队列已跑完", "查看下方的出分和补跑数量"
    if (RUN / "STOP").exists():
        if phase in {"waiting_gateway", "waiting_openlux_account_quota"}:
            return "暂停 · 等待模型网关", f"下一个案例 #{task}" if task else ""
        if not retry_handoff:
            return "主队列已暂停", f"当前案例 #{task}" if task else ""
        if not alive:
            return "正在切换到补跑", "等待后台监督器重启队列"
    if not alive:
        return "主队列进程未运行", f"最后记录案例 #{task}" if task else ""
    if not (task.isascii() and task.isdigit() and len(task) == 3):
        return "主队列已启动", "等待任务分配"
    if phase == "waiting_memory":
        return f"等待内存 · #{task}", "内存达到启动条件后自动继续"
    if phase == "waiting_disk":
        return f"等待磁盘空间 · #{task}", "空间达到启动条件后自动继续"
    if task in rows:
        return f"#{task} 已收尾", "正在切换案例"

    attempt = max((RUN / task).glob("attempt_*/"), default=None)
    if attempt is None or (attempt / "task_result.json").exists():
        return f"初始化中 · #{task}", "等待虚拟机和任务环境"
    action = latest_event(attempt / "action_events.jsonl")
    api = latest_event(attempt / "api_events.jsonl")
    step = action.get("step")
    if action.get("phase") == "submitted" and isinstance(step, int):
        return f"运行中 · #{task}", f"正在执行第{step}步动作"
    if api.get("phase") == "request_started":
        return f"运行中 · #{task}", f"等待第{api.get('step', '?')}步模型回包"
    if step is not None:
        return f"运行中 · #{task}", f"第{step}步动作已返回，步数上限150"
    if api:
        return f"运行中 · #{task}", "已进入模型调用"
    return f"初始化中 · #{task}", "等待虚拟机和任务环境"


def main() -> None:
    window = tk.Tk()
    window.title(TITLE)
    window.geometry(f"345x143+{max(0, window.winfo_screenwidth() - 375)}+78")
    window.resizable(False, False)
    window.attributes("-topmost", True)
    window.configure(bg="#f6f8fa")

    status = tk.Label(window, anchor="w", font=("Microsoft YaHei UI", 13, "bold"), bg="#f6f8fa")
    status.pack(fill="x", padx=14, pady=(11, 2))
    detail = tk.Label(window, anchor="w", font=("Microsoft YaHei UI", 10), bg="#f6f8fa", fg="#202b3a")
    detail.pack(fill="x", padx=14)
    totals = tk.Label(window, anchor="w", font=("Microsoft YaHei UI", 10), bg="#f6f8fa", fg="#202b3a")
    totals.pack(fill="x", padx=14, pady=(6, 1))
    updated = tk.Label(window, anchor="w", font=("Microsoft YaHei UI", 9), bg="#f6f8fa", fg="#536174")
    updated.pack(fill="x", padx=14)

    def refresh() -> None:
        rows = read_object(RUN / "queue_status.json")
        pending = read_object(RUN / "retry_queue.json")
        runtime = read_object(RUN / "runtime.json")
        try:
            pause = (RUN / "STOP").read_text(encoding="utf-8")
        except OSError:
            pause = ""
        retry_handoff = pause.startswith("Finish #017, then prioritize")
        caption, info = describe(runtime, rows, queue_alive(), retry_handoff)
        scored = sum(row.get("status") == "completed" and row.get("score") is not None
                     for row in rows.values())
        status.configure(text=caption, fg="#186849" if caption.startswith("运行中") else "#253950")
        detail.configure(text=info)
        totals.configure(text=f"已出分 {scored}/54   ·   待补跑 {len(pending)}")
        footer = "#017结束后优先补跑" if retry_handoff else "GPT-6.1 Sol medium"
        updated.configure(text=f"{footer}   ·   {datetime.now():%H:%M:%S}")
        window.after(4000, refresh)

    refresh()
    window.mainloop()


if __name__ == "__main__":
    main()

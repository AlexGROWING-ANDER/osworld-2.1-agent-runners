"""OSWorld 2.1 desktop UniPath worker for Windows QEMU."""
import argparse
import base64
import hashlib
import importlib
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from planner_response_schema import RESPONSE_FORMAT, VALIDATOR
from run_reliability import PlannerJsonDecodeError

HERE = Path(__file__).resolve().parent
BENCH = Path(os.environ.get("OSWORLD21_DATA", HERE)).expanduser().resolve()
REPO = Path(os.environ.get("OSWORLD21_CODE", HERE / "OSWorld-v2.1")).expanduser().resolve()
SHARED = HERE
ROOT = Path(os.environ.get("OSWORLD21_RESULTS", HERE / "results")).expanduser().resolve()
MODEL = os.environ.get("OSWORLD21_SOL_MODEL", "")
REASONING_EFFORT = "medium"
RELEASE = "osworld-v2.1"
FRAMEWORKS = ("ui_path_screen_agent",)
STEPS = 150
GUEST_MB = 4096
HOST_COMMIT_RESERVE_MB = 512


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)


def tasks():
    ids = read(REPO / "evaluation_examples/test_v2.json")["tasks"]
    hashes = read(REPO / "benchmark_releases/osworld-v2.1.task_hashes.json")["files"]
    names = {f"task_{task}.py" for task in ids if re.fullmatch(r"[0-9]{3}", task)}
    if len(ids) != 108 or len(names) != 108 or names != set(hashes):
        raise RuntimeError("OSWorld 2.1 task list mismatch")
    for name, row in hashes.items():
        if hashlib.sha256((REPO / "evaluation_examples/task_class" / name).read_bytes()).hexdigest() != row["sha256"]:
            raise RuntimeError("Task source hash mismatch: " + name)
    return ["004"] + [task for task in ids if task != "004"]


def environment():
    for path in (SHARED, REPO):
        sys.path.insert(0, str(path))
    os.chdir(REPO)
    os.environ.update(OSWORLD_BENCHMARK_RELEASE=RELEASE,
                      OSWORLD_FILE_BASE_URL=str(BENCH / "v2_assets"),
                      OSWORLD_QEMU_MEMORY_MB=str(GUEST_MB),
                      OSWORLD_COMMIT_HEADROOM_MB=str(HOST_COMMIT_RESERVE_MB),
                      WEBSITE_HOST_SUFFIX=os.environ.get("WEBSITE_HOST_SUFFIX", "site.hku.icu"),
                      NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1")
    tess = Path(os.environ.get("OSWORLD21_TESSERACT_DIR", "C:/Program Files/Tesseract-OCR"))
    if tess.is_dir():
        os.environ["PATH"] = str(tess) + os.pathsep + os.environ["PATH"]


def api_client():
    from bounded_api_transport import BoundedApiTransport
    from gateway_settings import gateway_settings
    from run_reliability import API_TIMEOUT_SECONDS
    path = Path(os.environ.get("OSWORLD21_ENV", HERE / ".env")).expanduser()
    profile = os.environ.get("OSWORLD21_GATEWAY_PROFILE", "ground")
    url, key = gateway_settings(path, profile)
    return BoundedApiTransport(url, key, API_TIMEOUT_SECONDS, trust_env=False)


def connect_api(transport, audit):
    def invoke(module, messages, **params):
        audit.module = module
        def request():
            response = transport.create(model=MODEL, messages=messages,
                                        reasoning_effort=REASONING_EFFORT, **params)
            if response.model.casefold() != MODEL.casefold():
                raise ValueError("Gateway returned a different model")
            if module == "unipath_planner":
                choice = response.choices[0]
                if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None):
                    raise ValueError("Planner did not return a complete structured response")
                try:
                    parsed = json.loads(choice.message.content)
                except json.JSONDecodeError as exc:
                    raise PlannerJsonDecodeError(exc) from exc
                except TypeError:
                    raise ValueError("Planner did not return structured JSON") from None
                if not VALIDATOR.is_valid(parsed):
                    raise ValueError("Planner response violates strict schema")
            return response

        response = audit.call(
            request,
            {"model": MODEL, "reasoning_effort": REASONING_EFFORT,
             "response_format": "json_schema_strict" if module == "unipath_planner" else "unconstrained", "image_count": sum(
                c.get("type") == "image_url" for m in messages
                for c in (m["content"] if isinstance(m.get("content"), list) else [])
                if isinstance(c, dict))})
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Empty model content")
        return content
    return invoke


def grounded_point(invoke, data_url, description):
    response = invoke("grounding", [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "text", "text": f"Locate this element: {description}\nReturn only (x, y), normalized to 0–1000 over the whole screenshot."},
    ]}], temperature=0, max_tokens=128)
    match = re.fullmatch(r"\s*\(?\s*(\d{1,4})\s*,\s*(\d{1,4})\s*\)?\s*", response)
    if not match or any(int(v) > 1000 for v in match.groups()):
        raise ValueError("Invalid grounding coordinate")
    return [int(v) for v in match.groups()]


def make_uni(invoke):
    import mm_agents.uipath.action_planner as planner
    from mm_agents.uipath import utils
    from mm_agents.uipath_agent import UipathBaseAgent
    from PIL import Image

    def send(payload):
        return invoke("unipath_planner", payload["messages"],
                      max_tokens=payload["max_completion_tokens"], response_format=RESPONSE_FORMAT)
    planner.llm_client.send_messages = send

    class Grounder:
        async def predict(self, image_base64, action_description, action=None):
            image = base64.b64decode(image_base64, validate=True)
            with Image.open(io.BytesIO(image)) as frame:
                width, height = frame.size
            x, y = grounded_point(invoke, "data:image/png;base64," + image_base64,
                                  action_description)
            return utils.GroundingOutput(description=action_description,
                                         position=(round(x*(width-1)/1000), round(y*(height-1)/1000)))
    agent = UipathBaseAgent(model=MODEL, client_password="osworld-public-evaluation")
    agent.uipath_computer_use_model.executor = Grounder()
    predict = agent.uipath_computer_use_model.predict_request
    async def predict_with_guest_password(request, model):
        request = dict(request, userTask=request["userTask"].replace(
            "The sudo password is password, if needed.",
            "The sudo password is osworld-public-evaluation, if needed."))
        return await predict(request, model)
    agent.uipath_computer_use_model.predict_request = predict_with_guest_password
    return agent


def validate_adapters(check_data=True):
    environment()
    import desktop_env.desktop_env
    make_uni(None)
    print("UniPath and DesktopEnv imports and constructor passed; no model calls")
    if check_data:
        print("Official task hashes verified:", len(tasks()))


def persist_evaluation(out, evaluation):
    # OSWorld 2.1 custom evaluators may return the canonical score plus
    # structured details. Keep the full official payload for later review.
    if isinstance(evaluation, dict):
        write(out / "evaluation.json", evaluation)
        evaluation = evaluation.get("score")
    if type(evaluation) not in (int, float, str):
        raise TypeError("Evaluator did not return a numeric score")
    score = float(evaluation)
    if not math.isfinite(score):
        raise ValueError("Evaluator returned nonfinite score")
    (out / "result.txt").write_text(f"{score}\n", encoding="utf-8")
    return score


class GuestScreenshotUnavailable(RuntimeError):
    """The current frame is missing after recovery reads."""


def require_screenshot(env, observation, out, step):
    from run_reliability import append_event

    if isinstance(observation.get("screenshot"), bytes) and observation["screenshot"]:
        return observation
    # Recover the observation only. The guest may already have applied the action.
    for attempt in range(1, 3):
        append_event(out / "runtime_events.jsonl",
                     {"phase": "screenshot_retry", "step": step, "attempt": attempt})
        shot = env.controller.get_screenshot()
        if isinstance(shot, bytes) and shot:
            observation["screenshot"] = shot
            return observation
    raise GuestScreenshotUnavailable(f"No guest screenshot at step {step}")


def run_one(framework, task_id, out):
    from run_reliability import InfrastructureFailure, RuntimeGuard, ResourceBlocked, StepTimeoutAudit, append_event, check_memory, memory_snapshot
    from native_qemu_v2 import NativeQemuProvider
    from direct_cdp import install as install_direct_cdp
    import desktop_env.desktop_env as env_module

    out.mkdir(parents=True, exist_ok=False)
    task_source = REPO / "evaluation_examples/task_class" / f"task_{task_id}.py"
    result = dict(task=task_id, architecture=framework, model=MODEL, reasoning_effort=REASONING_EFFORT,
                  benchmark_release=RELEASE, max_steps=STEPS, guest_memory_mb=GUEST_MB,
                  formal_benchmark=False, provider="native Windows QEMU, official V2 image",
                  task_source_sha256=hashlib.sha256(task_source.read_bytes()).hexdigest(),
                  status="incomplete", score=None)
    env = transport = provider = None
    guard = RuntimeGuard(BENCH / "experiment_runtime.lock")
    recording = False
    try:
        stage = "resources"
        guard.acquire()
        try:
            check_memory(memory_snapshot(), GUEST_MB, 1200)
        except ResourceBlocked:
            append_event(out / "runtime_events.jsonl", {"phase": "waiting_vm_memory", **memory_snapshot()})
            memory_deadline = time.monotonic() + 30
            while True:
                time.sleep(5)
                try:
                    check_memory(memory_snapshot(), GUEST_MB, 1200)
                except ResourceBlocked:
                    if time.monotonic() >= memory_deadline:
                        raise
                else:
                    break
        if min(shutil.disk_usage(p).free for p in (BENCH, out)) < 8 * 1024**3:
            raise ResourceBlocked("Disk reserve")
        stage = "task_import"
        task_module = importlib.import_module(f"evaluation_examples.task_class.task_{task_id}")
        task = getattr(task_module, "Task" + task_id)()
        if task.id != task_id:
            raise RuntimeError("Task ID mismatch")
        stage = "setup"
        install_direct_cdp()
        provider = NativeQemuProvider(out / "guest")
        env_module.create_vm_manager_and_provider = lambda *a, **kw: (None, provider)
        env = env_module.DesktopEnv(provider_name="docker", path_to_vm=str(provider.base),
                                   action_space="computer_13",
                                   headless=True, screen_size=(1920, 1080),
                                   client_password="osworld-public-evaluation",
                                   require_a11y_tree=False, cache_dir=str(out / "cache"))
        obs = require_screenshot(env, env.reset(task), out, 0)
        (out / "step_0.png").write_bytes(obs["screenshot"])
        append_event(out / "runtime_events.jsonl", {"phase": "environment_ready"})
        stage = "agent_init"
        transport = api_client()
        audit = StepTimeoutAudit(out / "api_events.jsonl")
        invoke = connect_api(transport, audit)
        result.update(adaptation="GPT 6.1 Sol planner and grounding; local model transport")
        agent = make_uni(invoke)
        env.controller.start_recording()
        recording = True
        actions_sent = 0
        termination_reason = "step_limit"
        for step in range(1, STEPS + 1):
            audit.begin_step(step)
            stage = "prediction"
            response, actions = agent.predict(task.instruction, obs,
                SimpleNamespace(max_steps=STEPS, uipath_model_name=MODEL), step - 1)
            if audit.last_failure is not None:
                raise audit.last_failure
            if not isinstance(actions, list):
                raise ValueError("Framework returned invalid actions")
            stage = "execution"
            done = False
            for action in actions:
                if not (isinstance(action, dict) and "action_type" in action
                                   or isinstance(action, str) and action in {"DONE", "FAIL", "WAIT"}):
                    raise ValueError("Invalid UniPath action")
                audit.action_submitted = True
                append_event(out / "action_events.jsonl", {"step": step, "phase": "submitted", "action": action})
                obs, _, done, info = env.step(action, 2)
                actions_sent += 1
                append_event(out / "action_events.jsonl", {"step": step, "phase": "returned", "action": action, "info": info})
                if done:
                    termination_reason = ("agent_fail" if info.get("fail")
                                          else "agent_done" if info.get("done")
                                          else "environment_done")
                    break
            shot = require_screenshot(env, obs, out, step)["screenshot"]
            (out / f"step_{step}.png").write_bytes(shot)
            append_event(out / "traj.jsonl", {"step": step, "response": response, "actions": actions,
                         "done": done, "screenshot_file": f"step_{step}.png"})
            if done:
                break
        stage = "evaluation"
        score = persist_evaluation(out, env.evaluate())
        result.update(status="completed", score=score, steps=step,
                      actions_sent=actions_sent, termination_reason=termination_reason)
    except ResourceBlocked as exc:
        result.update(status="blocked_resources", stage="resources", reason=str(exc),
                      resources=memory_snapshot())
    except Exception as exc:
        msg = str(exc)
        status = "incomplete"
        if stage == "task_import" and "GITLAB_URL and GITLAB_PRIVATE_TOKEN" in msg:
            status = "blocked_configuration"
        result.update(status=status, stage=stage, error_type=type(exc).__name__)
        if isinstance(exc, InfrastructureFailure):
            result.update(failure_category=exc.category, failure_step=audit.step)
        elif isinstance(exc, GuestScreenshotUnavailable):
            result["failure_category"] = "guest_screenshot_unavailable"
        if stage != "prediction":
            logging.exception("Attempt failed at %s", stage)
        else:
            logging.error("Prediction failed: %s", type(exc).__name__)
    finally:
        if recording:
            try:
                env.controller.end_recording(str(out / "recording.mp4"))
            except Exception as exc:
                result["recording_error"] = type(exc).__name__
        if env is not None:
            try:
                env.close()
            except Exception as exc:
                result["cleanup_error"] = type(exc).__name__
        elif provider is not None:
            provider.stop_emulator(provider.base)
        if transport is not None:
            transport.close()
        guard.close()
        write(out / "task_result.json", result)
        # The benchmark uses disposable overlays. Keep interrupted overlays for
        # diagnosis, but release successful runs after durable results are saved.
        if provider is not None and result["status"] == "completed" and not result.get("cleanup_error"):
            overlay = provider.overlay.resolve()
            disk_root = (BENCH / "v2_work_disks").resolve()
            if (overlay.parent.parent == disk_root and overlay.suffix == ".qcow2"
                    and overlay.name.startswith("working")
                    and provider.process is not None and provider.process.poll() is not None):
                try:
                    overlay.unlink(missing_ok=True)
                    overlay.parent.rmdir()
                except OSError:
                    logging.warning("Could not release completed guest overlay")
    return result["status"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--framework", choices=FRAMEWORKS)
    parser.add_argument("--task")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if not re.fullmatch(r"[a-zA-Z0-9._:/-]*gpt[._-]?6[._-]?1[._-]?sol[a-zA-Z0-9._:/-]*", MODEL, re.I):
        parser.error("OSWORLD21_SOL_MODEL must be the verified GPT 6.1 Sol gateway SKU")
    if args.verify:
        validate_adapters()
        return
    if not args.framework or not re.fullmatch(r"[0-9]{3}", args.task or "") or not args.out:
        parser.error("framework, three-digit task ID and out are required")
    environment()
    if args.task not in tasks():
        parser.error("task is not in the official manifest")
    run_one(args.framework, args.task, args.out.resolve())


if __name__ == "__main__":
    main()

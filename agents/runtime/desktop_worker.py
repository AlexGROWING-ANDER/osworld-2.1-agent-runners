"""Isolated OSWorld 2.1 runner for desktop Agent-S2 and UniPath."""
import argparse
import ast
import base64
import hashlib
import importlib
import importlib.util
import io
import json
import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
BENCH = Path("H:/GUI_AGENT_OSWorld")
PROJECT = Path("C:/Users/ASUS/Documents/Codex/2026-09-19/new-chat")
REPO = BENCH / "OSWorld-v2.1"
S2 = PROJECT / "agent_s"
SHARED = PROJECT / "osworld_uipath"
DEFAULT_ROOT = HERE / "results"
MODEL = "qwen3.7-plus"
RELEASE = "osworld-v2.1"
FRAMEWORKS = ("agent_s2", "ui_path_screen_agent")
STEPS = 30
if os.environ.get("OSWORLD_DESKTOP_GUEST_MB"):
    GUEST_MB = int(os.environ["OSWORLD_DESKTOP_GUEST_MB"])
    if GUEST_MB not in (2048, 3072, 4096):
        raise ValueError("Unsupported desktop guest memory")
elif (HERE / "CONTINUE_033_2GIB").is_file():
    GUEST_MB = 2048
elif (HERE / "CONTINUE_033_3GIB").is_file():
    GUEST_MB = 3072
else:
    GUEST_MB = 4096
ROOT = DEFAULT_ROOT if GUEST_MB == 4096 else HERE / f"results_guest_{GUEST_MB}"
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
    for path in (SHARED, S2, REPO):
        sys.path.insert(0, str(path))
    os.chdir(REPO)
    os.environ.update(OSWORLD_BENCHMARK_RELEASE=RELEASE,
                      OSWORLD_FILE_BASE_URL=str(BENCH / "v2_assets"),
                      OSWORLD_QEMU_MEMORY_MB=str(GUEST_MB),
                      OSWORLD_COMMIT_HEADROOM_MB=str(HOST_COMMIT_RESERVE_MB),
                      WEBSITE_HOST_SUFFIX="site.hku.icu",
                      NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1")
    os.environ["PATH"] = "C:/Program Files/Tesseract-OCR;" + os.environ["PATH"]
    if GUEST_MB == 2048:
        for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ[variable] = "1"


def api_client():
    from bounded_api_transport import BoundedApiTransport
    from dotenv import dotenv_values
    cfg = dotenv_values(S2 / "config/.env", encoding="utf-8-sig")
    if not cfg.get("GROUND_API_KEY") or not cfg.get("GROUND_URL"):
        raise RuntimeError("Missing existing Qwen configuration")
    return BoundedApiTransport(cfg["GROUND_URL"], cfg["GROUND_API_KEY"], 240, trust_env=False)


def connect_api(transport, audit):
    from run_reliability import InfrastructureFailure
    blocked_failure = None

    def request(**params):
        try:
            return transport.create(model=MODEL, **params)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 413:
                exc.category = "api_payload_too_large"
            raise

    def invoke(module, messages, **params):
        nonlocal blocked_failure
        audit.module = module
        # Native parsers can retry an invalid/empty reply many times. A rejected
        # request body or disabled credential cannot recover through that loop.
        if blocked_failure is not None:
            audit.last_failure = blocked_failure
            raise blocked_failure
        try:
            response = audit.call(
                lambda: request(messages=messages, **params),
                {"model": MODEL, "image_count": sum(
                    c.get("type") == "image_url" for m in messages
                    for c in (m["content"] if isinstance(m.get("content"), list) else [])
                    if isinstance(c, dict))})
        except InfrastructureFailure as exc:
            if exc.category in {"api_payload_too_large", "authentication_or_permission", "billing_or_payment"}:
                blocked_failure = exc
            raise
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


# The upstream Worker uses eval on a model string. Resolve only the literal
# arguments of published ACI methods and never execute model-supplied Python.
ACI_METHODS = frozenset("""
    click type open switch_applications save_to_knowledge drag_and_drop
    highlight_text_span set_cell_values scroll hotkey hold_and_press wait done fail
""".split())


def dispatch_aci(source, names):
    try:
        call = ast.parse(source.strip(), mode="eval").body
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            raise ValueError
        if not isinstance(call.func.value, ast.Name) or call.func.value.id != "agent" or call.func.attr not in ACI_METHODS:
            raise ValueError
        if any(kw.arg is None for kw in call.keywords):
            raise ValueError
        positional = [ast.literal_eval(x) for x in call.args]
        named = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
        if call.func.attr == "switch_applications":
            name = positional[0] if positional else named.get("app_code")
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9._ -]{1,120}", name):
                raise ValueError
        if call.func.attr == "set_cell_values":
            for offset, key in ((1, "app_name"), (2, "sheet_name")):
                value = positional[offset] if len(positional) > offset else named.get(key)
                if not isinstance(value, str) or not re.fullmatch(r"[^\\\"\r\n]{1,160}", value):
                    raise ValueError
        if call.func.attr == "wait":
            seconds = positional[0] if positional else named.get("time")
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                raise ValueError
    except (TypeError, SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid literal ACI instruction") from exc
    return getattr(names["agent"], call.func.attr)(*positional, **named)


def resolve_s2_action(plan, agent, obs, parse_code, sanitize, extract):
    _, marker, action_section = plan.rpartition("Grounded Action")
    action_section = action_section.lstrip().lstrip(":)").strip()
    candidate = parse_code(action_section).strip()
    tokens = {"DONE": "agent.done()", "FAIL": "agent.fail()"}
    if candidate in tokens:
        code = tokens[candidate]
    else:
        code = extract(sanitize(candidate))
        if code is None:
            # A single, unfenced call under Grounded Action is also unambiguous.
            inline = action_section
            if not marker or not re.match(r"^agent\.", inline):
                raise ValueError("No valid Grounded Action")
            ast.parse(inline, mode="eval")
            code = inline
    if not re.match(r"^agent\.(done|fail)\s*\(", code):
        agent.assign_coordinates("Grounded Action\n```python\n" + code + "\n```", obs)
    return code, dispatch_aci(code, {"agent": agent})


def make_s2(invoke, out, width, height):
    from gui_agents.s2.agents import worker
    from gui_agents.s2.agents.agent_s import AgentS2
    from gui_agents.s2.agents.grounding import OSWorldACI
    from gui_agents.s2.core.engine import LMMEngineOpenAI
    from gui_agents.s2.utils.common_utils import calculate_tokens
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = "C:/Program Files/Tesseract-OCR/tesseract.exe"
    # Keep native Manager/Worker/Reflector and VM-side ACI implementations.
    import inspect
    code = inspect.getsource(worker.Worker.generate_next_action)
    if code.count("eval(plan_code)") != 2:
        raise RuntimeError("Unexpected upstream S2 Worker source")
    start = code.index("            agent.assign_coordinates(plan, obs)")
    end = code.index("        except Exception as e:", start)
    code = code[:start] + (
        "            plan_code, exec_code = _resolve_s2_action(\n"
        "                plan, agent, obs, parse_single_code_from_string,\n"
        "                sanitize_code, extract_first_agent_function)\n"
    ) + code[end:]
    code = code.replace("eval(plan_code)", "_dispatch_aci(plan_code, locals())")
    from textwrap import dedent
    ns = dict(vars(worker), _dispatch_aci=dispatch_aci,
              _resolve_s2_action=resolve_s2_action, calculate_tokens=calculate_tokens)
    exec(compile(dedent(code), str(Path(worker.__file__)), "exec"), ns)
    worker.Worker.generate_next_action = ns["generate_next_action"]

    class DesktopAgentS2(AgentS2):
        def predict(self, instruction, observation):
            return predict_with_empty_plan(self, instruction, observation)

    # The native empty-queue branch already returns DONE. Its info record
    # must also handle a first plan with no current subtask.
    predictor = dedent(inspect.getsource(AgentS2.predict))
    for field in ("name", "info"):
        original = f"self.current_subtask.{field}"
        new = f"getattr(self.current_subtask, {field!r}, None)"
        if predictor.count(original) != 2:
            raise RuntimeError("Unexpected upstream S2 prediction source")
        key = "subtask" if field == "name" else "subtask_info"
        predictor = predictor.replace(f'"{key}": {original}', f'"{key}": {new}')
    globals_for_predict = dict(AgentS2.predict.__globals__)
    exec(compile(predictor, str(inspect.getsourcefile(AgentS2)), "exec"), globals_for_predict)
    predict_with_empty_plan = globals_for_predict["predict"]

    def generate(self, messages, temperature=0.0, max_new_tokens=None, **kw):
        return invoke("s2_language", messages, temperature=temperature,
                      max_tokens=max_new_tokens or 4096, **kw)
    LMMEngineOpenAI.generate = generate

    class QwenACI(OSWorldACI):
        def generate_coords(self, ref_expr, obs):
            return grounded_point(invoke, "data:image/png;base64," +
                                  base64.b64encode(obs["screenshot"]).decode("ascii"), ref_expr)

        def switch_applications(self, app_code):
            return "import time; " + super().switch_applications(app_code)

    params = {"engine_type": "openai", "model": MODEL}
    ground = QwenACI("linux", params.copy(),
                     dict(params, grounding_width=1000, grounding_height=1000),
                     width=width, height=height)
    return DesktopAgentS2(params.copy(), ground, platform="linux", action_space="pyautogui",
                   observation_type="mixed", search_engine=None, use_default_kb=False,
                   memory_root_path=str(out))


def make_uni(invoke):
    import mm_agents.uipath.action_planner as planner
    from mm_agents.uipath import utils
    from mm_agents.uipath_agent import UipathBaseAgent
    from PIL import Image

    def send(payload):
        return invoke("unipath_planner", payload["messages"],
                      max_tokens=payload["max_completion_tokens"])
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


def validate_adapters():
    environment()
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(dir=HERE) as tmp:
        make_s2(None, Path(tmp), 1920, 1080)
    make_uni(None)
    print("Verified both framework constructors and all", len(tasks()), "task hashes")


def persist_evaluation(out, evaluation):
    # OSWorld 2.1 custom evaluators may return the canonical score plus
    # structured details. Keep the full official payload for later review.
    write(out / "evaluation.json", evaluation)
    if isinstance(evaluation, dict):
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


class GuestExecutionUnavailable(RuntimeError):
    """The controller did not confirm whether the submitted action executed."""


class TaskAssetUnavailable(RuntimeError):
    """A pinned task asset is missing before any VM or model is started."""


def missing_task_assets(task_source):
    missing = []
    for node in ast.walk(ast.parse(task_source.read_text(encoding="utf-8-sig"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "asset" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            relative = node.args[0].value
            base = BENCH / "v2_assets"
            if not (base / relative).exists() and not (base / unquote(relative)).exists():
                missing.append(relative)
    return sorted(set(missing))


def install_execution_receipts(controller, out, audit):
    from run_reliability import append_event

    execute = controller.execute_python_command

    def confirmed_execution(command):
        receipt = execute(command)
        append_event(out / "execution_receipts.jsonl", {
            "step": audit.step, "time": time.time(),
            "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "receipt": receipt,
        })
        if not isinstance(receipt, dict) or "returncode" not in receipt:
            raise GuestExecutionUnavailable("No guest command execution receipt")
        return receipt

    controller.execute_python_command = confirmed_execution


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


def load_evaluators_on_demand():
    # The upstream packages eagerly import metrics for every application.
    # Keep the original callables, but only import modules used by this task.
    parent = importlib.import_module("desktop_env.evaluators")
    for name in ("metrics", "getters"):
        qualified = f"{parent.__name__}.{name}"
        if qualified in sys.modules:
            continue
        spec = importlib.util.find_spec(qualified)
        module = importlib.util.module_from_spec(spec)
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"), filename=spec.origin)
        deferred = {}
        remaining = []
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.level == 1 and statement.module and all(
                    alias.name != "*" for alias in statement.names):
                for alias in statement.names:
                    deferred[alias.asname or alias.name] = (statement.module, alias.name)
            else:
                remaining.append(statement)
        tree.body = remaining
        exec(compile(tree, spec.origin, "exec"), vars(module))

        def resolve(symbol, module=module, deferred=deferred):
            if symbol not in deferred:
                raise AttributeError(f"{module.__name__} has no attribute {symbol}")
            child, original = deferred[symbol]
            value = getattr(importlib.import_module(f".{child}", module.__name__), original)
            setattr(module, symbol, value)
            return value

        module.__getattr__ = resolve
        sys.modules[qualified] = module
        setattr(parent, name, module)


def apply_task_host_compatibility(task_module, task_id):
    if task_id == "021" and os.name == "nt":
        # Preserve the pinned task source and Linux's unpadded day semantics.
        from datetime import datetime

        class PortableDateTime(datetime):
            def strftime(self, format):
                marker = "__OSWORLD_UNPADDED_DAY__"
                return super().strftime(format.replace("%-d", marker)).replace(marker, str(self.day))

        task_module.datetime = PortableDateTime


def run_one(framework, task_id, out):
    from run_reliability import ApiAudit, InfrastructureFailure, RuntimeGuard, ResourceBlocked, append_event, check_memory, memory_snapshot
    from native_qemu_v2 import NativeQemuProvider
    from direct_cdp import install as install_direct_cdp
    # Keep unused application evaluators out of every worker's startup memory,
    # independently of guest size. Their original functions load when needed.
    load_evaluators_on_demand()
    import desktop_env.desktop_env as env_module
    from PIL import Image

    out.mkdir(parents=True, exist_ok=False)
    task_source = REPO / "evaluation_examples/task_class" / f"task_{task_id}.py"
    result = dict(task=task_id, architecture=framework, model=MODEL,
                  benchmark_release=RELEASE, max_steps=STEPS, guest_memory_mb=GUEST_MB,
                  formal_benchmark=False, provider="native Windows QEMU, official V2 image",
                  evaluator_imports="on_demand",
                  task_source_sha256=hashlib.sha256(task_source.read_bytes()).hexdigest(),
                  status="incomplete", score=None)
    env = transport = provider = None
    guard = RuntimeGuard(BENCH / "experiment_runtime.lock")
    recording = False
    try:
        stage = "resources"
        guard.acquire()
        check_memory(memory_snapshot(), GUEST_MB, 1200)
        stage = "task_import"
        task_module = importlib.import_module(f"evaluation_examples.task_class.task_{task_id}")
        task = getattr(task_module, "Task" + task_id)()
        if task.id != task_id:
            raise RuntimeError("Task ID mismatch")
        apply_task_host_compatibility(task_module, task_id)
        stage = "task_assets"
        missing_assets = missing_task_assets(task_source)
        if missing_assets:
            result["missing_assets"] = missing_assets
            raise TaskAssetUnavailable("Required local task assets are missing")
        stage = "setup"
        install_direct_cdp()
        provider = NativeQemuProvider(out / "guest")
        env_module.create_vm_manager_and_provider = lambda *a, **kw: (None, provider)
        env = env_module.DesktopEnv(provider_name="docker", path_to_vm=str(provider.base),
                                   action_space="pyautogui" if framework == "agent_s2" else "computer_13",
                                   headless=True, screen_size=(1920, 1080),
                                   client_password="osworld-public-evaluation",
                                   require_a11y_tree=False, cache_dir=str(out / "cache"))
        obs = require_screenshot(env, env.reset(task), out, 0)
        (out / "step_0.png").write_bytes(obs["screenshot"])
        append_event(out / "runtime_events.jsonl", {"phase": "environment_ready"})
        stage = "agent_init"
        transport = api_client()
        audit = ApiAudit(out / "api_events.jsonl", attempts=1)
        install_execution_receipts(env.controller, out, audit)
        invoke = connect_api(transport, audit)
        with Image.open(io.BytesIO(obs["screenshot"])) as im:
            width, height = im.size
        s2 = framework == "agent_s2"
        if s2:
            result.update(default_kb=False, external_search=False,
                          adaptation="Qwen grounding; literal ACI dispatch")
        else:
            result.update(adaptation="Qwen planner and grounding; local model transport")
        agent = make_s2(invoke, out, width, height) if s2 else make_uni(invoke)
        env.controller.start_recording()
        recording = True
        actions_sent = 0
        termination_reason = "step_limit"
        for step in range(1, STEPS + 1):
            audit.step = step
            stage = "prediction"
            audit.last_failure = None
            if s2:
                agent.grounding_agent.width, agent.grounding_agent.height = width, height
                response, actions = agent.predict(task.instruction, obs)
            else:
                response, actions = agent.predict(task.instruction, obs,
                    SimpleNamespace(max_steps=STEPS, uipath_model_name=MODEL), step - 1)
            if audit.last_failure is not None:
                raise audit.last_failure
            if not isinstance(actions, list):
                raise ValueError("Framework returned invalid actions")
            stage = "execution"
            done = False
            for action in actions:
                if s2 and not isinstance(action, str):
                    raise ValueError("Invalid S2 action")
                if not s2 and not (isinstance(action, dict) and "action_type" in action
                                   or isinstance(action, str) and action in {"DONE", "FAIL", "WAIT"}):
                    raise ValueError("Invalid UniPath action")
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
            with Image.open(io.BytesIO(shot)) as im:
                width, height = im.size
            append_event(out / "traj.jsonl", {"step": step, "response": response, "actions": actions,
                         "done": done, "screenshot_file": f"step_{step}.png"})
            if done:
                break
        stage = "evaluation"
        score = persist_evaluation(out, env.evaluate())
        result.update(status="completed", score=score, steps=step,
                      actions_sent=actions_sent, termination_reason=termination_reason)
    except ResourceBlocked:
        result.update(status="blocked_resources", stage="resources", resources=memory_snapshot())
    except Exception as exc:
        msg = str(exc)
        status = "incomplete"
        if stage == "task_import" and "GITLAB_URL and GITLAB_PRIVATE_TOKEN" in msg:
            status = "blocked_configuration"
        if isinstance(exc, TaskAssetUnavailable):
            status = "blocked_configuration"
            result["failure_category"] = "missing_task_assets"
        result.update(status=status, stage=stage, error_type=type(exc).__name__)
        if isinstance(exc, InfrastructureFailure):
            result["failure_category"] = exc.category
        elif isinstance(exc, GuestScreenshotUnavailable):
            result["failure_category"] = "guest_screenshot_unavailable"
        elif isinstance(exc, GuestExecutionUnavailable):
            result["failure_category"] = "guest_execution_unconfirmed"
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
    if args.verify:
        validate_adapters()
        return
    if not args.framework or not re.fullmatch(r"[0-9]{3}", args.task or "") or not args.out:
        parser.error("framework, three-digit task ID and out are required")
    environment()
    run_one(args.framework, args.task, args.out)


if __name__ == "__main__":
    main()

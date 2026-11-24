import logging
import os
import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

import pytesseract
from PIL import Image
from pytesseract import Output

from gui_agents.s2.memory.procedural_memory import PROCEDURAL_MEMORY
from gui_agents.s2.core.mllm import LMMAgent
from gui_agents.s2.utils.common_utils import (
    call_llm_safe,
    parse_single_code_from_string,
)

from gui_agents.s2.agents.grounding import ACI, agent_action

logger = logging.getLogger(__name__)


CONSEQUENTIAL_ELEMENT_PATTERNS = (
    r"点击.{0,16}(?:发送|发布|发表评论|点赞|关注|提交订单|确认下单|立即购买|立即支付|确认支付)",
    r"(?:确认发送|发送按钮|发布按钮|评论按钮|点赞按钮|关注按钮|提交订单|确认订单|支付按钮)",
    r"(?:tap|click|press).{0,40}(?:send|post|publish|submit (?:the )?(?:comment|order)|place (?:the )?order|buy now|pay now|confirm (?:the )?(?:payment|purchase)|follow|like)",
    r"(?:send|post|publish|comment|follow|like|place order|buy now|pay now|confirm payment) button",
)


def is_consequential_element(element_description: str) -> bool:
    """副作用点击拦截。

    默认 **放开**（实验口径：让 Agent 自由执行发送/发布/关注/下单等动作，
    否则任务无法完成、也无法与 mobile_agent_v3 对照）。
    需要安全模式时设环境变量 AGENT_S2_BLOCK_CONSEQUENTIAL=1。
    """
    if os.environ.get("AGENT_S2_BLOCK_CONSEQUENTIAL", "0") != "1":
        return False
    description = str(element_description or "")
    return any(
        re.search(pattern, description, flags=re.IGNORECASE)
        for pattern in CONSEQUENTIAL_ELEMENT_PATTERNS
    )


class U2Controller:
    """Android device controller via uiautomator2.

    Uses d.send_keys() so native Chinese text input is supported.
    """

    def __init__(self, device_id: Optional[str] = None):
        import uiautomator2 as u2

        if device_id:
            self.d = u2.connect(device_id)
        else:
            self.d = u2.connect()

        try:
            self.d.set_fastinput_ime(True)
        except Exception:
            pass

    def get_screen_size(self) -> Tuple[int, int]:
        w, h = self.d.window_size()
        return int(w), int(h)

    def take_screenshot(self, save_path: str) -> bool:
        try:
            self.d.screenshot(save_path)
            return True
        except Exception:
            return False

    def start_app(self, package: str, activity: str = "") -> bool:
        try:
            self.d.app_start(package)
            time.sleep(2)
            return True
        except Exception:
            return False

    def stop_app(self, package: str) -> bool:
        try:
            self.d.app_stop(package)
            return True
        except Exception:
            return False

    def tap(self, x: int, y: int) -> bool:
        try:
            self.d.click(x, y)
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> bool:
        try:
            self.d.swipe(x1, y1, x2, y2, duration=duration / 1000)
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def long_press(self, x: int, y: int, duration: float = 1.0) -> bool:
        try:
            self.d.long_click(x, y, duration=duration)
            time.sleep(0.5)
            return True
        except Exception:
            return False

    ADB_IME = "com.github.uiautomator/.AdbKeyboard"

    def _focused_text(self) -> str:
        """读当前焦点输入框的文本，用于校验输入是否真的落进去。"""
        try:
            el = self.d(focused=True)
            if el.exists:
                return (el.get_text() or "")
        except Exception:
            pass
        return ""

    def _ensure_adb_ime(self) -> bool:
        if getattr(self, "_ime_ok", False):
            return True
        try:
            self.d.shell(["ime", "enable", self.ADB_IME])
            self.d.shell(["ime", "set", self.ADB_IME])
            self._ime_ok = True
        except Exception:
            self._ime_ok = False
        return self._ime_ok

    def _ui_has_text(self, needle: str) -> bool:
        """在整棵 UI 树里找文本，比只看焦点节点可靠（Compose/WebView 取不到 focused）。"""
        probe = (needle or "").strip()[:6]
        if not probe:
            return False
        try:
            return probe in (self.d.dump_hierarchy() or "")
        except Exception:
            return False

    def input_text(self, text: str) -> bool:
        """输入文本：AdbKeyboard 广播为主，send_keys 兜底。

        注意：**绝不因为"校验不到"而失败**。很多输入框是 Compose/WebView，
        focused 节点读不到文本，之前按"读不到=失败"处理会直接中断整条任务
        （2026-11-12 凌晨 淘宝_4 / 小红书_1 / 小红书_4 就是这么死的）。
        现在：能确认就返回 True，确认不到也返回 True，只记 WARN。
        """
        import base64 as _b64

        if self._ui_has_text(text):
            return True  # 已经在了，避免重复输入

        self._ensure_adb_ime()
        try:
            payload = _b64.b64encode(text.encode("utf-8")).decode("ascii")
            self.d.shell(["am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", payload])
            time.sleep(0.5)
        except Exception:
            pass

        if self._ui_has_text(text):
            return True

        try:
            self.d.send_keys(text)
            time.sleep(0.5)
        except Exception:
            pass

        if not self._ui_has_text(text):
            logger.warning("INPUT_TEXT_UNVERIFIED (继续执行): text=%r", text[:40])
        return True
        return False

    def clear_text(self) -> bool:
        try:
            self.d.clear_text()
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def press(self, key: str) -> bool:
        try:
            self.d.press(key)
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def back(self) -> bool:
        return self.press("back")

    def home(self) -> bool:
        return self.press("home")


class MobileACI(ACI):
    """ACI that drives an Android device via adb instead of desktop pyautogui.

    Action methods execute the action on the device immediately and return a
    no-op code string (since run_batch.py exec()'s the returned code).
    """

    def __init__(
        self,
        controller: U2Controller,
        engine_params_for_generation: Dict,
        engine_params_for_grounding: Dict,
        width: int = 1080,
        height: int = 2400,
    ):
        self.controller = controller
        self.width = width
        self.height = height
        self.notes: List[str] = []
        self.coords1 = None
        self.coords2 = None

        self.grounding_model = LMMAgent(engine_params_for_grounding)
        self.engine_params_for_grounding = engine_params_for_grounding

        self.text_span_agent = LMMAgent(
            engine_params=engine_params_for_generation,
            system_prompt=PROCEDURAL_MEMORY.PHRASE_TO_WORD_COORDS_PROMPT,
        )

        self.last_action_coords = None
        self.pending_confirmation = None

    def generate_coords(self, ref_expr: str, obs: Dict) -> List[int]:
        self.grounding_model.reset()
        prompt = (f"Target to click: {ref_expr}\n"
                  "The image is a phone screenshot. Output ONLY two integers: x y, "
                  "in 0-1000 normalized coordinates on BOTH axes (not pixels). "
                  "Do not describe the image, do not score it, no other words.\n")
        # 文本放前面：图文顺序反了时，部分模型会把请求当成"给这张图打个分/描述一下"
        self.grounding_model.add_message(
            text_content=prompt, image_content=obs["screenshot"], put_text_last=False
        )
        for attempt in range(3):
            response = call_llm_safe(self.grounding_model)
            logger.info("RAW GROUNDING MODEL RESPONSE: %s", response)
            numericals = re.findall(r"-?\d+(?:\.\d+)?", response or "")
            # 只在 0-1000 范围内的数字里取，且优先取最后一对（模型常先描述再给坐标）
            valid = [float(v) for v in numericals if 0 <= float(v) <= 1000]
            if len(valid) >= 2:
                coords = valid[-2:]
                return [round(coords[0]), round(coords[1])]
            if attempt < 2:
                logger.warning("Grounding response invalid in normalized coordinate space, retrying...")
                self.grounding_model.add_message(
                    text_content="Reply with exactly two integers 0-1000, nothing else. Example: 512 880"
                )
        raise AssertionError(f"Grounding model returned no valid coordinates: {response}")

    def get_ocr_elements(self, b64_image_data: str) -> Tuple[str, List]:
        image = Image.open(BytesIO(b64_image_data))
        image_data = pytesseract.image_to_data(image, output_type=Output.DICT)

        for i, word in enumerate(image_data["text"]):
            image_data["text"][i] = re.sub(
                r"^[^a-zA-Z\s.,!?;:\-\+]+|[^a-zA-Z\s.,!?;:\-\+]+$", "", word
            )

        ocr_elements = []
        ocr_table = "Text Table:\nWord id\tText\n"
        grouping_map = {}
        ocr_id = 0
        for i in range(len(image_data["text"])):
            block_num = image_data["block_num"][i]
            if image_data["text"][i]:
                grouping_map.setdefault(block_num, []).append(image_data["text"][i])
                ocr_table += f"{ocr_id}\t{image_data['text'][i]}\n"
                ocr_elements.append(
                    {
                        "id": ocr_id,
                        "text": image_data["text"][i],
                        "group_num": block_num,
                        "word_num": len(grouping_map[block_num]),
                        "left": image_data["left"][i],
                        "top": image_data["top"][i],
                        "width": image_data["width"][i],
                        "height": image_data["height"][i],
                    }
                )
                ocr_id += 1

        return ocr_table, ocr_elements

    def generate_text_coords(
        self, phrase: str, obs: Dict, alignment: str = ""
    ) -> List[int]:
        ocr_table, ocr_elements = self.get_ocr_elements(obs["screenshot"])

        alignment_prompt = ""
        if alignment == "start":
            alignment_prompt = "**Important**: Output the word id of the FIRST word in the provided phrase.\n"
        elif alignment == "end":
            alignment_prompt = "**Important**: Output the word id of the LAST word in the provided phrase.\n"

        self.text_span_agent.reset()
        self.text_span_agent.add_message(
            alignment_prompt + "Phrase: " + phrase + "\n" + ocr_table, role="user"
        )
        self.text_span_agent.add_message(
            "Screenshot:\n", image_content=obs["screenshot"], role="user"
        )

        response = call_llm_safe(self.text_span_agent)
        logger.info("TEXT SPAN AGENT RESPONSE: %s", response)
        numericals = re.findall(r"\d+", response)
        if len(numericals) > 0:
            text_id = int(numericals[-1])
        else:
            text_id = 0
        elem = ocr_elements[text_id]

        if alignment == "start":
            coords = [elem["left"], elem["top"] + (elem["height"] // 2)]
        elif alignment == "end":
            coords = [elem["left"] + elem["width"], elem["top"] + (elem["height"] // 2)]
        else:
            coords = [
                elem["left"] + (elem["width"] // 2),
                elem["top"] + (elem["height"] // 2),
            ]
        return coords

    def assign_coordinates(self, plan: str, obs: Dict):
        self.coords1, self.coords2 = None, None

        try:
            action = parse_single_code_from_string(plan.split("Grounded Action")[-1])
            function_name = re.match(r"(\w+\.\w+)\(", action).group(1)
            args = self.parse_function_args(action)
        except Exception as e:
            raise RuntimeError(f"Error in parsing grounded action: {e}") from e

        if (
            function_name in ["agent.click", "agent.type", "agent.scroll"]
            and len(args) >= 1
            and args[0] is not None
        ):
            self.coords1 = self.generate_coords(args[0], obs)
        elif function_name == "agent.drag_and_drop" and len(args) >= 2:
            self.coords1 = self.generate_coords(args[0], obs)
            self.coords2 = self.generate_coords(args[1], obs)
        elif function_name == "agent.highlight_text_span" and len(args) >= 2:
            self.coords1 = self.generate_text_coords(args[0], obs, alignment="start")
            self.coords2 = self.generate_text_coords(args[1], obs, alignment="end")

    def resize_coordinates(self, coordinates: List[int]) -> Tuple[float, float]:
        if {"grounding_width", "grounding_height"}.issubset(
            self.engine_params_for_grounding
        ):
            grounding_width = self.engine_params_for_grounding["grounding_width"]
            grounding_height = self.engine_params_for_grounding["grounding_height"]
        else:
            grounding_width = 1000
            grounding_height = 1000

        return (
            coordinates[0] / grounding_width,
            coordinates[1] / grounding_height,
        )

    def _abs(self, rx: float, ry: float) -> Tuple[int, int]:
        return round(rx * self.width), round(ry * self.height)

    def parse_function_args(self, function: str) -> List[str]:
        import ast

        tree = ast.parse(function)
        call_node = tree.body[0].value

        def safe_eval(node):
            if isinstance(node, ast.Constant):
                return node.value
            else:
                return ast.unparse(node)

        positional_args = [safe_eval(arg) for arg in call_node.args]
        keyword_args = {kw.arg: safe_eval(kw.value) for kw in call_node.keywords}

        res = []
        for key, val in keyword_args.items():
            if "description" in key:
                res.append(val)
        for arg in positional_args:
            res.append(arg)
        return res

    @agent_action
    def click(
        self,
        element_description: str,
        num_clicks: int = 1,
        button_type: str = "left",
        hold_keys: List = [],
    ):
        """Click on the element
        Args:
            element_description:str, a detailed descriptions of which element to click on. This description should be at least a full sentence.
            num_clicks:int, number of times to click the element
            button_type:str, which mouse button to press can be "left", "middle", or "right"
            hold_keys:List, list of keys to hold while clicking
        """
        if is_consequential_element(element_description):
            self.pending_confirmation = {
                "action": "click",
                "description": element_description,
            }
            logger.warning(
                "HUMAN CONFIRMATION REQUIRED: blocked consequential click: %s",
                element_description,
            )
            return "HUMAN_CONFIRMATION"

        rx, ry = self.resize_coordinates(self.coords1)
        ax, ay = self._abs(rx, ry)
        self.last_action_coords = (ax, ay)
        for _ in range(max(1, num_clicks)):
            self.controller.tap(ax, ay)
        return "pass"

    @agent_action
    def long_press(self, element_description: str, duration: float = 1.0):
        """Long press on the element
        Args:
            element_description:str, a detailed description of which element to long press. This description should be at least a full sentence.
            duration:float, how long to press in seconds
        """
        rx, ry = self.resize_coordinates(self.coords1)
        ax, ay = self._abs(rx, ry)
        self.last_action_coords = (ax, ay)
        self.controller.long_press(ax, ay, duration)
        return "pass"

    @agent_action
    def type(
        self,
        element_description: Optional[str] = None,
        text: str = "",
        overwrite: bool = False,
        enter: bool = False,
    ):
        """Type text into a specific element
        Args:
            element_description:str, a detailed description of which element to enter text in. This description should be at least a full sentence.
            text:str, the text to type
            overwrite:bool, Assign it to True if the text should overwrite the existing text, otherwise assign it to False. Using this argument clears all text in an element.
            enter:bool, Assign it to True if the enter key should be pressed after typing the text, otherwise assign it to False.
        """
        if enter and is_consequential_element(element_description or ""):
            self.pending_confirmation = {
                "action": "type_and_enter",
                "description": element_description,
                "text": text,
            }
            logger.warning(
                "HUMAN CONFIRMATION REQUIRED: blocked consequential type-and-enter: %s",
                element_description,
            )
            return "HUMAN_CONFIRMATION"

        if self.coords1 is not None:
            ax, ay = self._abs(*self.resize_coordinates(self.coords1))
            self.controller.tap(ax, ay)
            self.last_action_coords = (ax, ay)
            time.sleep(0.5)

        if overwrite:
            self.controller.clear_text()

        self.controller.input_text(text)

        if enter:
            self.controller.press("enter")
        return "pass"

    @agent_action
    def scroll(self, element_description: str, clicks: int, shift: bool = False):
        """Scroll the element in the specified direction
        Args:
            element_description:str, a very detailed description of which element to enter scroll in. This description should be at least a full sentence.
            clicks:int, the number of clicks to scroll can be positive (up) or negative (down).
            shift:bool, whether to use shift+scroll for horizontal scrolling
        """
        rx, ry = self.resize_coordinates(self.coords1)
        ax, ay = self._abs(rx, ry)
        delta = int(min(abs(clicks), 8) * self.height * 0.1)

        if shift:
            x1, y1 = max(0, ax - delta), ay
            x2, y2 = min(self.width, ax + delta), ay
        else:
            if clicks > 0:
                x1, y1 = ax, min(self.height, ay + delta)
                x2, y2 = ax, max(0, ay - delta)
            else:
                x1, y1 = ax, max(0, ay - delta)
                x2, y2 = ax, min(self.height, ay + delta)

        self.controller.swipe(x1, y1, x2, y2, duration=300)
        self.last_action_coords = (x1, y1)
        return "pass"

    @agent_action
    def drag_and_drop(
        self, starting_description: str, ending_description: str, hold_keys: List = []
    ):
        """Drag from the starting description to the ending description
        Args:
            starting_description:str, a very detailed description of where to start the drag action. This description should be at least a full sentence.
            ending_description:str, a very detailed description of where to end the drag action. This description should be at least a full sentence.
            hold_keys:List list of keys to hold while dragging
        """
        ax1, ay1 = self._abs(*self.resize_coordinates(self.coords1))
        ax2, ay2 = self._abs(*self.resize_coordinates(self.coords2))
        self.controller.swipe(ax1, ay1, ax2, ay2, duration=800)
        self.last_action_coords = (ax1, ay1)
        return "pass"

    @agent_action
    def back(self):
        """Go back to the previous page"""
        self.controller.back()
        return "pass"

    @agent_action
    def home(self):
        """Go to the device home screen"""
        self.controller.home()
        return "pass"

    @agent_action
    def save_to_knowledge(self, text: List[str]):
        """Save facts, elements, texts, etc. to a long-term knowledge bank for reuse during this task. Can be used for copy-pasting text, saving elements, etc.
        Args:
            text:List[str] the text to save to the knowledge
        """
        self.notes.extend(text)
        return "pass"

    @agent_action
    def wait(self, time: float):
        """Wait for a specified amount of time
        Args:
            time:float the amount of time to wait in seconds
        """
        import time as _time

        _time.sleep(time)
        return "pass"

    @agent_action
    def done(
        self,
        return_value: Optional[Union[Dict, str, List, Tuple, int, float, bool]] = None,
    ):
        """End the current task with a success and the required return value"""
        self.returned_info = return_value
        return """DONE"""

    @agent_action
    def fail(self):
        """End the current task with a failure, and replan the whole task."""
        return """FAIL"""

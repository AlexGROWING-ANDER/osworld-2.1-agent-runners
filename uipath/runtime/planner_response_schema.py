"""R3 wire contract. The upstream UiPath parser still validates the reply."""
from typing import Any

from jsonschema import Draft202012Validator

STRING = {"type": "string"}


def obj(**fields: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "required": list(fields),
            "additionalProperties": False}


def enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def action(kinds: tuple[str, ...], params: dict[str, Any] | None = None) -> dict[str, Any]:
    fields = {"type": enum(*kinds), "description": STRING}
    if params is not None:
        fields["parameters"] = params
    return obj(**fields)


NO_PARAMS = ("click", "right_click", "double_click", "mouse_move", "move_mouse", "wait")
DIR = enum("up", "down", "left", "right")
SCHEMA = obj(
    previous_action_result=STRING,
    thought=STRING,
    action_description=STRING,
    action={"anyOf": [
        action(NO_PARAMS),
        action(NO_PARAMS, obj()),
        action(("type",), obj(text=STRING)),
        action(("key_press",), obj(key=STRING)),
        action(("scroll",), obj(direction=DIR)),
        action(("scroll",), obj(direction=DIR, distance={"type": "integer"})),
        action(("drag",), obj(start_description=STRING, end_description=STRING)),
        # The upstream prompt already permits either text or JSON for extracted data.
        # Use the text form here, including a serialized JSON string when needed.
        action(("extract_data",), obj(description=STRING, data=STRING)),
        action(("finish",), obj(status=enum("success", "failure"))),
    ]},
)
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {
    "name": "uipath_r3_planner", "strict": True, "schema": SCHEMA}}
VALIDATOR = Draft202012Validator(SCHEMA)

"""Minimal gateway checks; no credentials, private data, or raw response bodies are printed."""

import argparse
import base64
import io
import json
import re
from pathlib import Path

import httpx
from openai import APIError, OpenAI

from gateway_settings import gateway_settings


def safe_sku(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", value) else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--model", type=str)
    parser.add_argument("--profile", choices=("ground", "openlux_existing"), default="ground")
    parser.add_argument("--vision", action="store_true", help="One request using an artificial PNG and the worker grounding parameters")
    args = parser.parse_args()
    try:
        url, key = gateway_settings(args.env_file, args.profile)
    except ValueError:
        print('{"error":"missing_gateway_configuration"}')
        return 2
    with OpenAI(base_url=url, api_key=key, max_retries=0, timeout=30,
                http_client=httpx.Client(trust_env=False, timeout=30)) as client:
        try:
            if not args.model:
                skus = [model.id for model in client.models.list()
                        if re.search(r"gpt[._-]?6[._-]?1", model.id, re.I) and safe_sku(model.id)]
                print(json.dumps({"matching_skus": sorted(set(skus))}))
                return 0
            if not safe_sku(args.model):
                parser.error("invalid model SKU")
            if args.vision:
                from PIL import Image, ImageDraw
                from desktop_worker import grounded_point

                frame = Image.new("RGB", (400, 300), "white")
                ImageDraw.Draw(frame).rectangle((150, 100, 250, 200), fill="blue")
                data = io.BytesIO()
                frame.save(data, format="PNG")
                served_sku = None
                def invoke(_module, messages, **params):
                    nonlocal served_sku
                    response = client.chat.completions.create(model=args.model, reasoning_effort="medium",
                                                             messages=messages, **params)
                    served_sku = safe_sku(response.model)
                    content = response.choices[0].message.content if response.choices else None
                    return content if isinstance(content, str) else ""
                try:
                    point = grounded_point(invoke, "data:image/png;base64," + base64.b64encode(data.getvalue()).decode(),
                                           "center of the blue square on the white background")
                except ValueError:
                    print(json.dumps({"served_model": served_sku, "error": "invalid_grounding_output"}))
                    return 3
                matched = bool(served_sku and served_sku.casefold() == args.model.casefold())
                in_square = 375 <= point[0] <= 625 and 333 <= point[1] <= 667
                print(json.dumps({"served_model": served_sku, "request": "synthetic_grounding", "point": point,
                                  "requested_reasoning_effort": "medium", "model_matches": matched, "in_square": in_square}))
                return 0 if matched and in_square else 3
            response = client.chat.completions.create(
                model=args.model, reasoning_effort="medium",
                messages=[{"role": "user", "content": "Reply with only this marker: UNI_SOL_CHECK_OK"}],
                max_completion_tokens=512,
            )
            content = response.choices[0].message.content if response.choices else None
            marker_ok = bool(content and content.strip() == "UNI_SOL_CHECK_OK")
            served_sku = safe_sku(response.model)
            model_matches = bool(served_sku and served_sku.casefold() == args.model.casefold())
            print(json.dumps({"requested_model": args.model, "requested_reasoning_effort": "medium",
                              "served_model": served_sku, "model_matches": model_matches,
                              "marker_ok": marker_ok}))
            return 0 if marker_ok and model_matches else 3
        except APIError as exc:
            print(json.dumps({"error_type": type(exc).__name__, "status": getattr(exc, "status_code", None),
                              "error_code": safe_sku(getattr(exc, "code", None))}))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

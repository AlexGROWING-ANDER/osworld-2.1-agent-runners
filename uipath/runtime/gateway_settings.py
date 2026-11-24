"""Resolve an existing local gateway profile without copying its credentials."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def gateway_settings(path: Path, profile: str):
    cfg = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    if profile == "openlux_existing":
        url = cfg.get("MOBILE_AGENT_BASE_URL") or ""
        key = cfg.get("MOBILE_AGENT_API_KEY")
        endpoint = urlsplit(url)
        if (endpoint.scheme, endpoint.netloc, endpoint.path.rstrip("/"), endpoint.query,
                endpoint.fragment) != ("https", "api.openlux.ai", "/v1", "", ""):
            raise ValueError("OpenLux profile must use its existing HTTPS API endpoint")
    elif profile == "ground":
        url = os.environ.get("GROUND_URL") or cfg.get("GROUND_URL")
        key = os.environ.get("GROUND_API_KEY") or cfg.get("GROUND_API_KEY")
    else:
        raise ValueError("Unknown UniPath gateway profile")
    if not url or not key or key == "CHANGE_ME" or ".example" in url:
        raise ValueError("Missing UniPath gateway configuration")
    return url, key

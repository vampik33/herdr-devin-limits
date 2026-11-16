#!/usr/bin/env python3
"""Print Devin CLI quota usage as one JSON line.

Reads the Devin credentials TOML and posts GetUserStatus to the configured API
server. No account credentials or request data are written to stderr or stdout.

Output, always exactly one line on stdout:
  {"windows":{"day":{"usedPercent":37,"resetsAt":1794902400},
              "wk":{"usedPercent":18,"resetsAt":1795334400}},
   "overageMicros":7083734,"plan":"Pro","fetchedAt":1794200000}
or
  {"error":"not_signed_in"|"no_quota"|"timeout"|"failed","detail":"..."}

Environment:
  DEVIN_CREDENTIALS       credentials TOML path (default ~/.local/share/devin/credentials.toml)
  DEVIN_LIMITS_TIMEOUT    overall request timeout in seconds (default 15)
"""

import json
import os
from pathlib import Path
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.request

DEFAULT_API_SERVER_URL = "https://server.codeium.com"
STATUS_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"
DEFAULT_TIMEOUT = 15.0
QUOTA_BILLING_STRATEGY = "BILLING_STRATEGY_QUOTA"
AUTH_CODES = {"unauthenticated", "permission_denied"}


class Failure(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = str(detail)[:200]


def endpoint_url(credentials):
    base = credentials.get("api_server_url", DEFAULT_API_SERVER_URL)
    if base is None:
        base = DEFAULT_API_SERVER_URL
    if not isinstance(base, str) or not base.strip():
        raise Failure("failed", "invalid api_server_url")
    return base.rstrip("/") + STATUS_PATH


def load_credentials():
    path = Path(os.environ.get("DEVIN_CREDENTIALS") or Path.home() / ".local/share/devin/credentials.toml")
    try:
        with path.open("rb") as f:
            credentials = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        raise Failure("not_signed_in", "credentials file is missing or invalid")
    key = credentials.get("windsurf_api_key")
    if not isinstance(key, str) or not key.strip():
        raise Failure("not_signed_in", "credentials file has no API key")
    credentials["windsurf_api_key"] = key.strip()
    return credentials


def parse_integer(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return int(value.strip(), 10)
    raise ValueError("expected an integer")


def parse_optional_integer(value):
    try:
        parsed = parse_integer(value)
    except (TypeError, ValueError):
        return None
    return parsed


def server_detail(body, fallback, key):
    detail = fallback
    try:
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            detail = data["message"]
    except (TypeError, ValueError):
        pass
    if key:
        detail = detail.replace(key, "[redacted]")
    return detail[:200]


def is_timeout(error):
    reason = getattr(error, "reason", None)
    return isinstance(error, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError))


def fetch():
    try:
        timeout = float(os.environ.get("DEVIN_LIMITS_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    timeout = max(0.001, timeout)
    deadline = time.monotonic() + timeout

    credentials = load_credentials()
    key = credentials["windsurf_api_key"]
    request_body = {
        "metadata": {
            "apiKey": key,
            "ideName": "devin-cli",
            "ideVersion": "0.1.0",
            "extensionName": "herdr-devin-limits",
            "extensionVersion": "0.1.0",
        }
    }
    request = urllib.request.Request(
        endpoint_url(credentials),
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
        method="POST",
    )

    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failure("timeout", "request timed out")
        with urllib.request.urlopen(request, timeout=remaining) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        try:
            body = error.read(8192).decode("utf-8", errors="replace")
        except (socket.timeout, TimeoutError):
            raise Failure("timeout", "request timed out")
        except OSError:
            body = ""
        detail = server_detail(body, f"HTTP {error.code}", key)
        if error.code in (401, 403):
            raise Failure("not_signed_in", detail)
        raise Failure("failed", detail)
    except (socket.timeout, TimeoutError):
        raise Failure("timeout", "request timed out")
    except urllib.error.URLError as error:
        if is_timeout(error):
            raise Failure("timeout", "request timed out")
        raise Failure("failed", "could not connect to API server")
    except OSError:
        raise Failure("failed", "could not connect to API server")

    if time.monotonic() > deadline:
        raise Failure("timeout", "request timed out")
    if status != 200:
        raise Failure("failed", f"HTTP {status}")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise Failure("failed", "API response is not valid JSON")
    return normalize(payload, key)


def normalize(payload, key=""):
    if not isinstance(payload, dict):
        raise Failure("failed", "API response has an invalid shape")
    if payload.get("code") in AUTH_CODES:
        raise Failure("not_signed_in", server_detail(json.dumps(payload), "authentication failed", key))

    user_status = payload.get("userStatus")
    plan_status = user_status.get("planStatus") if isinstance(user_status, dict) else None
    plan_info = plan_status.get("planInfo") if isinstance(plan_status, dict) else None
    if not isinstance(plan_info, dict):
        raise Failure("failed", "API response has no plan information")

    if "billingStrategy" in plan_info and plan_info["billingStrategy"] != QUOTA_BILLING_STRATEGY:
        raise Failure("no_quota", "account does not use quota billing")

    windows = {}
    for slot, remaining_field, reset_field, hidden_field in (
        ("day", "dailyQuotaRemainingPercent", "dailyQuotaResetAtUnix", "hideDailyQuota"),
        ("wk", "weeklyQuotaRemainingPercent", "weeklyQuotaResetAtUnix", "hideWeeklyQuota"),
    ):
        hidden = plan_info.get(hidden_field, False)
        if not isinstance(hidden, bool):
            raise Failure("failed", "API response has an invalid quota visibility flag")
        if hidden:
            continue
        reset = parse_optional_integer(plan_status.get(reset_field))
        if reset is None or reset <= 0:
            continue
        try:
            remaining = parse_integer(plan_status.get(remaining_field, 0))
        except (TypeError, ValueError):
            raise Failure("failed", "API response has an invalid quota percentage")
        windows[slot] = {"usedPercent": max(0, min(100, 100 - remaining)), "resetsAt": reset}

    if not windows:
        raise Failure("no_quota", "no quota windows are available")

    try:
        overage = parse_integer(plan_status.get("overageBalanceMicros", 0))
    except (TypeError, ValueError):
        raise Failure("failed", "API response has an invalid overage balance")
    plan = plan_info.get("planName")
    if plan is not None and not isinstance(plan, str):
        raise Failure("failed", "API response has an invalid plan name")
    return {
        "windows": windows,
        "overageMicros": overage,
        "plan": plan,
        "fetchedAt": int(time.time()),
    }


def main():
    try:
        out = fetch()
    except Failure as failure:
        out = {"error": failure.code, "detail": failure.detail}
    except Exception as error:  # never let a traceback reach stdout/stderr consumers
        out = {"error": "failed", "detail": type(error).__name__}
    sys.stdout.write(json.dumps(out, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

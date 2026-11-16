"""Tests for mods/devin-limits/devin_limits.py against a local fake HTTP server.

Run from the repo root: python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "mods" / "devin-limits" / "devin_limits.py"
TEST_KEY = "test-devin-api-key-12345"
STATUS_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"

spec = importlib.util.spec_from_file_location("devin_limits", HELPER)
devin_limits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devin_limits)


def normal_payload():
    return {
        "userStatus": {
            "planStatus": {
                "planInfo": {
                    "planName": "Pro",
                    "billingStrategy": "BILLING_STRATEGY_QUOTA",
                },
                "dailyQuotaRemainingPercent": 63,
                "weeklyQuotaRemainingPercent": 82,
                "overageBalanceMicros": "7083734",
                "dailyQuotaResetAtUnix": "1794902400",
                "weeklyQuotaResetAtUnix": "1795334400",
            }
        }
    }


class FakeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length)
        self.server.received = {
            "path": self.path,
            "headers": self.headers,
            "body": request_body,
        }
        if self.server.delay:
            time.sleep(self.server.delay)
        response = self.server.response
        if isinstance(response, bytes):
            body = response
        else:
            body = json.dumps(response).encode("utf-8")
        try:
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.server.delay_after_headers:
                time.sleep(self.server.delay_after_headers)
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, _format, *_args):
        pass


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.creds_path = Path(self.tmp.name) / "credentials.toml"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.server.response = normal_payload()
        self.server.status = 200
        self.server.delay = 0
        self.server.delay_after_headers = 0
        self.server.received = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_url = f"http://127.0.0.1:{self.server.server_port}"
        self.write_credentials(api_url=self.api_url)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def write_credentials(self, api_url=None, key=TEST_KEY):
        lines = [f'windsurf_api_key = "{key}"']
        if api_url is not None:
            lines.append(f'api_server_url = "{api_url}"')
        self.creds_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_helper(self, credentials_path=None, timeout=None):
        env = os.environ.copy()
        env.pop("DEVIN_CREDENTIALS", None)
        env.pop("DEVIN_LIMITS_TIMEOUT", None)
        env["DEVIN_CREDENTIALS"] = str(credentials_path or self.creds_path)
        if timeout is not None:
            env["DEVIN_LIMITS_TIMEOUT"] = str(timeout)
        proc = subprocess.run(
            [sys.executable, str(HELPER)],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertTrue(proc.stdout.endswith("\n"), repr(proc.stdout))
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, repr(proc.stdout))
        self.assertNotIn(TEST_KEY, proc.stdout)
        return json.loads(lines[0])

    def test_normal_response_and_request(self):
        out = self.run_helper()
        self.assertEqual(out["windows"], {
            "day": {"usedPercent": 37, "resetsAt": 1794902400},
            "wk": {"usedPercent": 18, "resetsAt": 1795334400},
        })
        self.assertEqual(out["overageMicros"], 7083734)
        self.assertEqual(out["plan"], "Pro")
        self.assertIsInstance(out["fetchedAt"], int)
        self.assertEqual(self.server.received["path"], STATUS_PATH)
        headers = self.server.received["headers"]
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Connect-Protocol-Version"), "1")
        body = json.loads(self.server.received["body"])
        self.assertEqual(body["metadata"], {
            "apiKey": TEST_KEY,
            "ideName": "devin-cli",
            "ideVersion": "0.1.0",
            "extensionName": "herdr-devin-limits",
            "extensionVersion": "0.1.0",
        })

    def test_string_and_integer_numbers(self):
        payload = normal_payload()
        status = payload["userStatus"]["planStatus"]
        status["dailyQuotaRemainingPercent"] = "63"
        status["weeklyQuotaRemainingPercent"] = 82
        status["dailyQuotaResetAtUnix"] = 1794902400
        status["weeklyQuotaResetAtUnix"] = "1795334400"
        status["overageBalanceMicros"] = 7083734
        self.server.response = payload
        out = self.run_helper()
        self.assertEqual(out["windows"]["day"]["usedPercent"], 37)
        self.assertEqual(out["windows"]["wk"]["usedPercent"], 18)
        self.assertEqual(out["overageMicros"], 7083734)

    def test_omitted_remaining_percent_means_fully_used(self):
        payload = normal_payload()
        del payload["userStatus"]["planStatus"]["dailyQuotaRemainingPercent"]
        self.server.response = payload
        out = self.run_helper()
        self.assertEqual(out["windows"]["day"]["usedPercent"], 100)

    def test_hidden_daily_quota_leaves_weekly(self):
        payload = normal_payload()
        payload["userStatus"]["planStatus"]["planInfo"]["hideDailyQuota"] = True
        self.server.response = payload
        self.assertEqual(list(self.run_helper()["windows"]), ["wk"])

    def test_both_hidden_returns_no_quota(self):
        payload = normal_payload()
        plan_info = payload["userStatus"]["planStatus"]["planInfo"]
        plan_info["hideDailyQuota"] = True
        plan_info["hideWeeklyQuota"] = True
        self.server.response = payload
        self.assertEqual(self.run_helper()["error"], "no_quota")

    def test_non_quota_billing_strategy_returns_no_quota(self):
        payload = normal_payload()
        payload["userStatus"]["planStatus"]["planInfo"]["billingStrategy"] = "BILLING_STRATEGY_UNLIMITED"
        self.server.response = payload
        self.assertEqual(self.run_helper()["error"], "no_quota")

    def test_omitted_overage_means_zero(self):
        payload = normal_payload()
        del payload["userStatus"]["planStatus"]["overageBalanceMicros"]
        self.server.response = payload
        self.assertEqual(self.run_helper()["overageMicros"], 0)

    def test_missing_credentials_file(self):
        missing = Path(self.tmp.name) / "missing.toml"
        self.assertEqual(self.run_helper(credentials_path=missing)["error"], "not_signed_in")

    def test_credentials_without_key(self):
        self.creds_path.write_text('api_server_url = "http://127.0.0.1"\n', encoding="utf-8")
        self.assertEqual(self.run_helper()["error"], "not_signed_in")

    def test_broken_credentials_toml(self):
        self.creds_path.write_text("windsurf_api_key = [\n", encoding="utf-8")
        self.assertEqual(self.run_helper()["error"], "not_signed_in")

    def test_default_api_server_url(self):
        self.assertEqual(
            devin_limits.endpoint_url({"windsurf_api_key": TEST_KEY}),
            "https://server.codeium.com" + STATUS_PATH,
        )

    def test_401_unauthenticated_is_not_signed_in(self):
        self.server.status = 401
        self.server.response = {"code": "unauthenticated", "message": "invalid api key"}
        self.assertEqual(self.run_helper()["error"], "not_signed_in")

    def test_permission_denied_response_is_not_signed_in(self):
        self.server.response = {"code": "permission_denied", "message": "access denied"}
        self.assertEqual(self.run_helper()["error"], "not_signed_in")

    def test_500_is_failed(self):
        self.server.status = 500
        self.server.response = {"code": "internal", "message": f"{TEST_KEY} " + "x" * 300}
        out = self.run_helper()
        self.assertEqual(out["error"], "failed")
        self.assertLessEqual(len(out["detail"]), 200)

    def test_non_json_success_is_failed(self):
        self.server.response = b"not JSON"
        self.assertEqual(self.run_helper()["error"], "failed")

    def test_slow_server_times_out(self):
        self.server.delay = 2
        self.assertEqual(self.run_helper(timeout=1)["error"], "timeout")

    def test_slow_error_response_times_out(self):
        self.server.status = 500
        self.server.delay_after_headers = 2
        self.assertEqual(self.run_helper(timeout=1)["error"], "timeout")

    def test_connection_refused_is_failed(self):
        self.write_credentials(api_url="http://127.0.0.1:1")
        self.assertEqual(self.run_helper()["error"], "failed")


if __name__ == "__main__":
    unittest.main()

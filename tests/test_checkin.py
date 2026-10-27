import json
import os
import unittest
from unittest import mock

import checkin


def ok_response(payload):
    response = mock.Mock(status_code=200)
    response.json.return_value = payload
    return response


class FakeClient:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    def checkin(self):
        self.calls += 1
        return next(self.results)


class ResultTests(unittest.TestCase):
    def test_observation_message_is_success(self):
        result = {
            "code": 1,
            "message": "Today's observation logged. Return tomorrow for more points.",
        }
        self.assertTrue(checkin.is_success(result))

    def test_code_zero_is_success(self):
        self.assertTrue(checkin.is_success({"code": 0, "message": "Checkin! Got 15 Points"}))

    def test_unknown_error_is_failure(self):
        self.assertFalse(checkin.is_success({"code": 2, "message": "Cookie expired"}))
        self.assertFalse(checkin.is_success(None))

    def test_permission_denied_is_auth_failure(self):
        self.assertTrue(checkin.is_auth_failure({"code": -2, "message": "没有权限"}))

    def test_device_mismatch_is_auth_failure(self):
        result = {"code": 4, "reason": "device-mismatch", "message": "denied"}
        self.assertTrue(checkin.is_auth_failure(result))

    def test_automated_detection_is_auth_failure(self):
        result = {"code": 4, "message": "Automated check-in detected."}
        self.assertTrue(checkin.is_auth_failure(result))


class RetryTests(unittest.TestCase):
    @mock.patch("checkin.time.sleep")
    def test_retry_stops_after_success(self, sleep):
        client = FakeClient([None, {"code": 0, "message": "Checkin! Got 15 Points"}])

        result, success = checkin.checkin_with_retry(client, attempts=3, delay=1)

        self.assertTrue(success)
        self.assertEqual(client.calls, 2)
        sleep.assert_called_once_with(1)

    @mock.patch("checkin.time.sleep")
    def test_auth_failure_does_not_retry(self, sleep):
        client = FakeClient([{"code": -2, "message": "没有权限"}])

        result, success = checkin.checkin_with_retry(client, attempts=3, delay=1)

        self.assertFalse(success)
        self.assertEqual(result["code"], -2)
        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()

    @mock.patch("checkin.time.sleep")
    def test_transient_failure_retries_until_limit(self, sleep):
        failure = {"code": 5, "message": "server busy"}
        client = FakeClient([failure, failure, failure])

        result, success = checkin.checkin_with_retry(client, attempts=3, delay=1)

        self.assertFalse(success)
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.call_count, 2)


class ClientTests(unittest.TestCase):
    COOKIE = "gld:sess=current; gld:sess.sig=signature"

    @mock.patch("checkin.requests.Session.request")
    def test_sends_cookie_and_edge_headers(self, request_method):
        request_method.return_value = ok_response({"code": 0, "data": {}})

        client = checkin.GLaDOSClient(self.COOKIE)
        result = client.status()

        self.assertEqual(result["code"], 0)
        self.assertEqual(client.http.headers["Cookie"], self.COOKIE)
        self.assertIn("Edg/", client.http.headers["User-Agent"])
        self.assertIn("Microsoft Edge", client.http.headers["Sec-CH-UA"])
        headers = request_method.call_args.kwargs["headers"]
        self.assertEqual(headers["Origin"], "https://glados.cloud")
        self.assertEqual(headers["Referer"], "https://glados.cloud/console/checkin")

    @mock.patch("checkin.requests.Session.request")
    def test_remembers_working_domain(self, request_method):
        request_method.side_effect = [
            mock.Mock(status_code=503),
            ok_response({"code": 0, "data": {}}),
            ok_response({"code": 0, "data": {}}),
        ]

        client = checkin.GLaDOSClient(self.COOKIE)
        client.status()
        client.status()

        first_url = request_method.call_args_list[0].args[1]
        second_url = request_method.call_args_list[1].args[1]
        third_url = request_method.call_args_list[2].args[1]
        self.assertTrue(first_url.startswith("https://glados.cloud"))
        self.assertTrue(second_url.startswith("https://glados.rocks"))
        self.assertTrue(third_url.startswith("https://glados.rocks"))

    @mock.patch("checkin.requests.Session.request")
    def test_exchange_uses_form_encoding(self, request_method):
        request_method.return_value = ok_response({"code": 0})

        client = checkin.GLaDOSClient(self.COOKIE)
        client.exchange("plan500")

        kwargs = request_method.call_args.kwargs
        self.assertEqual(kwargs["data"], {"planType": "plan500"})
        self.assertNotIn("json", kwargs)

    @mock.patch("checkin.requests.Session.request")
    def test_request_error_returns_none_without_leaking_cookie(self, request_method):
        request_method.side_effect = checkin.requests.exceptions.InvalidHeader(self.COOKIE)

        client = checkin.GLaDOSClient(self.COOKIE)

        with mock.patch("checkin.log") as log:
            self.assertIsNone(client.status())
        self.assertNotIn(self.COOKIE, str(log.call_args_list))

    @mock.patch("checkin.requests.Session.request")
    def test_non_json_response_returns_none(self, request_method):
        bad = mock.Mock(status_code=200)
        bad.json.side_effect = ValueError("not json")
        request_method.return_value = bad

        client = checkin.GLaDOSClient(self.COOKIE)

        with mock.patch("checkin.log"):
            self.assertIsNone(client.status())


class CookieTests(unittest.TestCase):
    def test_header_prefix_is_removed(self):
        raw = "Cookie: gld:sess=current; gld:sess.sig=signature"
        self.assertEqual(
            checkin.parse_cookie(raw),
            "gld:sess=current; gld:sess.sig=signature",
        )

    def test_cookie_editor_json_export(self):
        raw = json.dumps(
            [
                {"name": "koa:sess", "value": "old"},
                {"name": "gld:sess", "value": "current"},
                {"name": "gld:sess.sig", "value": "signature"},
            ]
        )
        cookie = checkin.parse_cookie(raw)
        self.assertEqual(checkin.session_kind(cookie), "gld")

    def test_token_json_maps_to_legacy_cookie(self):
        self.assertEqual(checkin.parse_cookie('{"token":"abc"}'), "koa:sess=abc")

    def test_malformed_json_is_rejected(self):
        self.assertIsNone(checkin.parse_cookie("[\n{\n"))

    def test_bare_jwt_maps_to_legacy_cookie(self):
        token = "a" * 30 + "." + "b" * 30 + "." + "c" * 30
        self.assertEqual(checkin.parse_cookie(token), f"koa:sess={token}")

    def test_session_kinds(self):
        self.assertEqual(checkin.session_kind("gld:sess=a; gld:sess.sig=b"), "gld")
        self.assertEqual(checkin.session_kind("koa:sess=a; koa:sess.sig=b"), "koa")
        self.assertIsNone(checkin.session_kind("gld:sess=a"))
        self.assertIsNone(checkin.session_kind(""))

    def test_multi_account_separator(self):
        cookies = [
            "gld:sess=first; gld:sess.sig=first-sig",
            "gld:sess=second; gld:sess.sig=second-sig",
        ]
        for separator in ("\n", "&"):
            with mock.patch.dict(os.environ, {"GLADOS_COOKIE": separator.join(cookies)}):
                self.assertEqual(checkin.load_cookies(), cookies)

    def test_formatted_json_is_one_account(self):
        raw = json.dumps(
            [
                {"name": "gld:sess", "value": "current"},
                {"name": "gld:sess.sig", "value": "signature"},
            ],
            indent=2,
        )
        with mock.patch.dict(os.environ, {"GLADOS_COOKIE": raw}):
            self.assertEqual(
                checkin.load_cookies(),
                ["gld:sess=current; gld:sess.sig=signature"],
            )

    def test_missing_configuration_returns_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(checkin.load_cookies(), [])


class ExchangeTests(unittest.TestCase):
    def test_skips_when_points_below_threshold(self):
        client = mock.Mock()
        state, text = checkin.try_exchange(client, "500", 499)
        self.assertEqual(state, "skip")
        self.assertIn("积分不足", text)
        client.exchange.assert_not_called()

    def test_skips_when_points_unknown(self):
        client = mock.Mock()
        state, _ = checkin.try_exchange(client, "100", None)
        self.assertEqual(state, "skip")
        client.exchange.assert_not_called()

    def test_success_reports_ok(self):
        client = mock.Mock()
        client.exchange.return_value = {"code": 0}
        state, text = checkin.try_exchange(client, "500", 500)
        self.assertEqual(state, "ok")
        self.assertIn("+100天", text)
        client.exchange.assert_called_once_with("plan500")

    def test_failure_reports_reason(self):
        client = mock.Mock()
        client.exchange.return_value = {"code": 1, "message": "denied"}
        state, text = checkin.try_exchange(client, "500", 600)
        self.assertEqual(state, "fail")
        self.assertIn("denied", text)


class BrowserHeadersTests(unittest.TestCase):
    def test_edge_headers_are_frozen(self):
        headers = checkin.BROWSER_HEADERS
        self.assertIn("Edg/120.0.0.0", headers["User-Agent"])
        self.assertEqual(headers["Sec-CH-UA-Mobile"], "?0")
        self.assertEqual(headers["Sec-CH-UA-Platform"], '"Windows"')


class DiagnosticTests(unittest.TestCase):
    @mock.patch("checkin.log")
    @mock.patch("checkin.push_meow")
    @mock.patch("checkin.GLaDOSClient")
    def test_diagnostic_reads_status_only(
        self, client_class, push, log
    ):
        cookie = "gld:sess=private-cookie; gld:sess.sig=private-signature"
        client = client_class.return_value
        client.status.return_value = {
            "code": 0,
            "data": {"email": "private@example.test", "leftDays": "123"},
        }
        with mock.patch.dict(
            os.environ,
            {"GLADOS_COOKIE": cookie, "CHECKIN_DIAGNOSTIC_ONLY": "true"},
        ):
            self.assertEqual(checkin.main(), 0)

        client.status.assert_called_once()
        client.checkin.assert_not_called()
        client.exchange.assert_not_called()
        client.points.assert_not_called()
        push.assert_not_called()
        printed = str(log.call_args_list)
        for private in ("private-cookie", "private-signature", "private@example.test"):
            self.assertNotIn(private, printed)

    @mock.patch("checkin.GLaDOSClient")
    def test_diagnostic_fails_on_auth_or_bad_response(self, client_class):
        client = client_class.return_value
        for response in (
            None,
            {"code": -2, "message": "没有权限"},
            {"code": 0, "data": None},
            {"code": 0, "data": {}},
        ):
            with self.subTest(response=response):
                client.status.return_value = response
                self.assertEqual(checkin.diagnose(["synthetic-cookie"]), 1)


if __name__ == "__main__":
    unittest.main()

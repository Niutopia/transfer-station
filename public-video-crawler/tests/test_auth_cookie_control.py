import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(PROJECT / "scripts"))

import auth_cookie


class AuthCookieControlTests(unittest.TestCase):
    def test_normal_page_may_load_cloudflare_script_without_being_a_challenge(self) -> None:
        body = b"<html><title>91porn</title><script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></html>"
        self.assertFalse(auth_cookie.is_cloudflare_challenge(body))

    def test_real_cloudflare_challenge_is_detected(self) -> None:
        body = b"<html><title>Just a moment...</title><script>window._cf_chl_opt={}</script></html>"
        self.assertTrue(auth_cookie.is_cloudflare_challenge(body))

    def test_cookie_header_is_normalized_without_exposing_values(self) -> None:
        normalized, cookies = auth_cookie.normalize_cookie_header(
            "Cookie: session=private; cf_clearance=opaque; session=ignored"
        )
        self.assertEqual(normalized, "session=private; cf_clearance=opaque")
        self.assertEqual([name for name, _ in cookies], ["session", "cf_clearance"])

    def test_cookie_header_requires_cloudflare_clearance(self) -> None:
        with self.assertRaisesRegex(auth_cookie.AuthCookieError, "cf_clearance"):
            auth_cookie.normalize_cookie_header("session=private")

    def test_cookie_header_rejects_newlines(self) -> None:
        with self.assertRaisesRegex(auth_cookie.AuthCookieError, "换行"):
            auth_cookie.normalize_cookie_header("cf_clearance=opaque\nsecond=value")

    def test_cookie_header_rejects_other_control_characters(self) -> None:
        with self.assertRaisesRegex(auth_cookie.AuthCookieError, "控制字符"):
            auth_cookie.normalize_cookie_header("cf_clearance=opaque\x00value")

    def test_replace_checks_local_format_before_writing_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "auth-cookie.txt"
            user_agent_path = root / "auth-user-agent.txt"
            cookie_path.write_text("cf_clearance=old\n", encoding="utf-8")
            user_agent_path.write_text("Mozilla/5.0 old\n", encoding="utf-8")
            status = auth_cookie.replace_auth_profile(
                cookie_path,
                user_agent_path,
                "cf_clearance=fresh",
                "Mozilla/5.0 Edg/150.0.0.0",
            )
            self.assertEqual(cookie_path.read_text(encoding="utf-8"), "cf_clearance=fresh\n")
            self.assertEqual(user_agent_path.read_text(encoding="utf-8"), "Mozilla/5.0 Edg/150.0.0.0\n")
            self.assertEqual(cookie_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(user_agent_path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(status["valid"])
            self.assertNotIn("fresh", repr(status))

    def test_failed_validation_keeps_existing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "auth-cookie.txt"
            user_agent_path = root / "auth-user-agent.txt"
            cookie_path.write_text("cf_clearance=old\n", encoding="utf-8")
            user_agent_path.write_text("Mozilla/5.0 old\n", encoding="utf-8")
            with self.assertRaises(auth_cookie.AuthCookieError):
                auth_cookie.replace_auth_profile(
                    cookie_path,
                    user_agent_path,
                    "session=missing-clearance",
                    "Mozilla/5.0 Edg/150.0.0.0",
                )
            self.assertEqual(cookie_path.read_text(encoding="utf-8"), "cf_clearance=old\n")
            self.assertEqual(user_agent_path.read_text(encoding="utf-8"), "Mozilla/5.0 old\n")

    def test_no_manual_crawl_result_leaves_status_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "auth-cookie.txt"
            user_agent_path = root / "auth-user-agent.txt"
            cookie_path.write_text("cf_clearance=present\n", encoding="utf-8")
            user_agent_path.write_text("Mozilla/5.0 Edg/150.0.0.0\n", encoding="utf-8")
            status = auth_cookie.validate_stored_profile(cookie_path, user_agent_path)
            self.assertTrue(status["configured"])
            self.assertIsNone(status["valid"])

    def test_failed_manual_crawl_with_auth_failure_marks_cookie_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "auth-cookie.txt"
            user_agent_path = root / "auth-user-agent.txt"
            cookie_path.write_text("cf_clearance=present\n", encoding="utf-8")
            user_agent_path.write_text("Mozilla/5.0 Edg/150.0.0.0\n", encoding="utf-8")
            status = auth_cookie.validate_stored_profile(
                cookie_path,
                user_agent_path,
                latest_manual_crawl={"resultStatus": "failed", "authFailure": True, "crawlExitCode": 1},
            )
            self.assertFalse(status["valid"])
            self.assertIn("上次手动抓取", str(status["error"]))

    def test_successful_manual_crawl_marks_cookie_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "auth-cookie.txt"
            user_agent_path = root / "auth-user-agent.txt"
            cookie_path.write_text("cf_clearance=present\n", encoding="utf-8")
            user_agent_path.write_text("Mozilla/5.0 Edg/150.0.0.0\n", encoding="utf-8")
            status = auth_cookie.validate_stored_profile(
                cookie_path,
                user_agent_path,
                latest_manual_crawl={"resultStatus": "success", "authFailure": False, "crawlExitCode": 0},
            )
            self.assertTrue(status["valid"])

    def test_locally_invalid_stored_profile_still_requires_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "auth-cookie.txt"
            user_agent_path = root / "auth-user-agent.txt"
            cookie_path.write_text("cf_clearance=present\n", encoding="utf-8")
            user_agent_path.write_text("not-a-browser\n", encoding="utf-8")
            status = auth_cookie.validate_stored_profile(cookie_path, user_agent_path)
            self.assertFalse(status["valid"])

    def test_partial_replace_rolls_back_the_browser_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "auth-cookie.txt"
            user_agent_path = root / "auth-user-agent.txt"
            cookie_path.write_text("cf_clearance=old\n", encoding="utf-8")
            user_agent_path.write_text("Mozilla/5.0 old\n", encoding="utf-8")
            real_replace = os.replace
            failed = False

            def fail_cookie_replace(source, destination):
                nonlocal failed
                if Path(destination) == cookie_path and not failed:
                    failed = True
                    raise OSError("simulated replacement failure")
                return real_replace(source, destination)

            with mock.patch.object(auth_cookie.os, "replace", side_effect=fail_cookie_replace):
                with self.assertRaises(OSError):
                    auth_cookie.replace_auth_profile(
                        cookie_path,
                        user_agent_path,
                        "cf_clearance=fresh",
                        "Mozilla/5.0 fresh",
                    )
            self.assertEqual(cookie_path.read_text(encoding="utf-8"), "cf_clearance=old\n")
            self.assertEqual(user_agent_path.read_text(encoding="utf-8"), "Mozilla/5.0 old\n")
            self.assertEqual(list(root.glob(".*.restore.*")), [])


if __name__ == "__main__":
    unittest.main()

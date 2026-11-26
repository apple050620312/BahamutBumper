import os
import tempfile
import unittest
from unittest.mock import patch

from bahamut_bumper.config import AppConfig
from bahamut_bumper.errors import ConfigurationError
from bahamut_bumper.notifications import send_discord_notification


SINGLE_ACCOUNT = {
    "BAHAMUT_ACCOUNT": "bump_test",
    "BAHAMUT_TARGET_URL": "https://forum.gamer.com.tw/C.php?bsn=100&snA=101",
}


class ConfigTests(unittest.TestCase):
    def test_account_and_target_must_be_set_explicitly(self):
        for settings in ({}, {"BAHAMUT_ACCOUNT": "bump_test"}):
            with self.subTest(settings=settings), patch.dict(os.environ, settings, clear=True):
                with self.assertRaisesRegex(ConfigurationError, "BAHAMUT_ACCOUNTS_FILE"):
                    AppConfig.from_env()

    def test_cleanup_uses_current_reply_as_one_exact_match(self):
        settings = {**SINGLE_ACCOUNT, "BAHAMUT_MESSAGE": "更新,辛苦了"}
        with patch.dict(os.environ, settings, clear=True):
            profile = AppConfig.from_env().accounts[0]
        self.assertEqual(profile.deletable_messages, ("更新,辛苦了",))

    def test_cleanup_accepts_an_explicit_list_or_can_be_disabled(self):
        for value, expected in (("推,已更新,推", ("推", "已更新")), ("", ())):
            settings = {**SINGLE_ACCOUNT, "BAHAMUT_DELETE_MESSAGES": value}
            with self.subTest(value=value), patch.dict(os.environ, settings, clear=True):
                self.assertEqual(AppConfig.from_env().accounts[0].deletable_messages, expected)

    def test_discord_mentions_only_a_configured_user(self):
        settings = {
            **SINGLE_ACCOUNT,
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/12345/test-token",
        }
        cases = (
            ({}, "需要處理", {"parse": []}),
            (
                {"DISCORD_ATTENTION_USER_ID": "123456789012345678"},
                "<@123456789012345678> 需要處理",
                {"users": ["123456789012345678"]},
            ),
        )
        for override, content, mentions in cases:
            with (
                self.subTest(override=override),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {**settings, **override, "DATA_DIR": directory}, clear=True),
                patch("requests.post") as post,
            ):
                post.return_value.status_code = 204
                profile = AppConfig.from_env().accounts[0]
                self.assertTrue(send_discord_notification(profile, "需要處理", attention=True))
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["content"], content)
            self.assertEqual(payload["allowed_mentions"], mentions)

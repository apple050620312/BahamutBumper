import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MAIN = Path(__file__).resolve().parents[1] / "main.py"


class CliTests(unittest.TestCase):
    def test_cli_loads_dotenv_and_prefers_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, account in (("account list.json", "from_file"), ("override.json", "from_env")):
                (root / name).write_text(
                    json.dumps([{
                        "account": account,
                        "target_url": "https://forum.gamer.com.tw/C.php?bsn=1&snA=2",
                        "password_file": "missing-password.txt",
                        "auto_mobile_login": True,
                    }]),
                    encoding="utf-8",
                )
            (root / ".env").write_text(
                '# Local settings\nBAHAMUT_ACCOUNTS_FILE="account list.json" # comment\n',
                encoding="utf-8-sig",
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("BAHAMUT_", "DISCORD_"))
            }
            env.update(DATA_DIR="data", TZ="Asia/Taipei", LOG_LEVEL="INFO")
            cases = (
                ({}, "from_file"),
                ({"BAHAMUT_ACCOUNTS_FILE": "override.json"}, "from_env"),
            )
            for override, account in cases:
                with self.subTest(account=account):
                    result = subprocess.run(
                        [sys.executable, "-B", "-X", "utf8", str(MAIN), "--check"],
                        cwd=root,
                        env={**env, **override},
                        capture_output=True,
                        encoding="utf-8",
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(account, result.stdout)
                    payload = json.loads(result.stdout)
                    self.assertEqual([item["account"] for item in payload["results"]], [account])

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "config.json"

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_relative_database_and_environment_token(self):
        self.write({"allowed_users": ["12345", "12345"], "onebot_token_env": "CCF_TEST_TOKEN"})
        with mock.patch.dict(os.environ, {"CCF_TEST_TOKEN": "synthetic-token"}, clear=True):
            config = bootstrap.load_config(self.path)
        self.assertEqual(config["database"], str(self.directory / "state/relay.sqlite3"))
        self.assertEqual(config["onebot_token"], "synthetic-token")
        self.assertNotIn("allowed_users", config)  # Legacy allowlist is ignored.
        self.assertEqual(config["poll_seconds"], 3)

    def test_legacy_allowlist_is_ignored_and_not_required(self):
        for legacy in ([], ["12345"], "everyone"):
            self.write({"allowed_users": legacy})
            self.assertNotIn("allowed_users", bootstrap.load_config(self.path))
        self.write({})
        self.assertNotIn("allowed_users", bootstrap.load_config(self.path))

    def test_reject_unsafe_endpoints_and_intervals(self):
        for value in ("http://localhost", "ws://user:secret@localhost:3001", "ws://localhost:bad", "ws://localhost:0", "ws://localhost:3001/?token=secret", "ws://localhost:3001/#fragment", "ws://localhost:3001/\n"):
            with self.subTest(endpoint=value), self.assertRaises(bootstrap.ConfigError):
                bootstrap.validate_config({"onebot_url": value}, self.directory)
        for value in (0, -1, True, "3", float("inf"), float("nan")):
            with self.subTest(interval=value), self.assertRaises(bootstrap.ConfigError):
                bootstrap.validate_config({"poll_seconds": value}, self.directory)

    def test_secret_not_in_error_or_check_output(self):
        secret = "SYNTHETIC_SECRET_DO_NOT_PRINT"
        self.write({"onebot_url": "ws://name:" + secret + "@localhost:3001"})
        with self.assertRaises(bootstrap.ConfigError) as failure:
            bootstrap.load_config(self.path)
        self.assertNotIn(secret, str(failure.exception))
        self.write({"allowed_users": ["12345"]})
        with mock.patch.dict(os.environ, {"CCF_ONEBOT_TOKEN": secret}), mock.patch.object(sys, "argv", ["bootstrap.py", "check", "--config", str(self.path)]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                bootstrap.main()
        self.assertNotIn(secret, output.getvalue())

    def test_napcat_selects_unique_enabled_forward_server(self):
        napcat = self.directory / "napcat.json"
        napcat.write_text(json.dumps({"network": {"websocketServers": [
            {"enable": False, "port": 3001, "token": "disabled"},
            {"enable": True, "port": 3001, "token": "selected-synthetic"},
            {"enable": True, "port": 3002, "token": "other"}]}}))
        self.write({"napcat_config": "napcat.json", "napcat_ws_port": 3001})
        config = bootstrap.load_config(self.path)
        self.assertEqual(config["onebot_token"], "selected-synthetic")
        self.assertEqual(config["napcat_config"], str(napcat))
        document = json.loads(napcat.read_text())
        document["network"]["websocketServers"].append({"enable": True, "port": 3001})
        napcat.write_text(json.dumps(document))
        with self.assertRaises(bootstrap.ConfigError):
            bootstrap.load_config(self.path)

    def test_duplicate_json_and_symlink_database_rejected(self):
        self.path.write_text('{"allowed_users": [], "allowed_users": ["12345"]}')
        with self.assertRaises(bootstrap.ConfigError):
            bootstrap.load_config(self.path)
        target = self.directory / "target"
        target.write_text("private")
        (self.directory / "database").symlink_to(target)
        self.write({"database": "database"})
        with self.assertRaises(bootstrap.ConfigError):
            bootstrap.load_config(self.path)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "release"
        self.source.mkdir()
        for name in bootstrap.RUNTIME_FILES:
            (self.source / name).write_text("synthetic release\n")
        (self.source / "ccf-qq-relay.service.in").write_text(
            "WorkingDirectory=@WORKING_DIRECTORY@\nExecStart=@PYTHON@ @RELAY@ --config @CONFIG@\nEnvironmentFile=-@ENVIRONMENT_FILE@\n")
        self.prefix = self.directory / "new install"
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.directory / "config-home")}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def args(self):
        return ["--prefix", str(self.prefix), "--dry-run"]

    def test_dry_run_leaves_no_destination_or_service(self):
        with mock.patch.object(subprocess := bootstrap.subprocess, "run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                bootstrap.install(self.args(), self.source)
        run.assert_not_called()
        self.assertFalse(self.prefix.exists())
        self.assertFalse((self.directory / "config-home").exists())

    def test_rejects_unsafe_path_but_accepts_legacy_admin_argument(self):
        for path in ("a%b", "a$b", 'a"b', "a\nb", "a\\b", "trailing ", "a*b", "a?b", "a[b]"):
            with self.subTest(path=path), self.assertRaises(bootstrap.ConfigError):
                bootstrap.install(["--prefix", str(self.directory / path), "--dry-run"], self.source)
        with contextlib.redirect_stdout(io.StringIO()):
            bootstrap.install(self.args() + ["--admin-qq", "12345"], self.source)

    def test_refuses_overwriting_existing_runtime_and_symlink(self):
        self.prefix.mkdir()
        (self.prefix / "relay.py").write_text("local edits")
        with self.assertRaises(bootstrap.ConfigError):
            bootstrap.install(self.args(), self.source)
        (self.prefix / "relay.py").unlink()
        (self.prefix / "config.json").symlink_to(self.source / "config.example.json")
        with self.assertRaises(bootstrap.ConfigError):
            bootstrap.install(self.args(), self.source)

    def test_existing_configuration_is_validated_in_dry_run(self):
        self.prefix.mkdir()
        (self.prefix / "config.json").write_text('{"allowed_users": "everyone"}')
        with contextlib.redirect_stdout(io.StringIO()):
            bootstrap.install(self.args(), self.source)

    def test_systemd_path_directives_are_raw_but_exec_arguments_are_quoted(self):
        unit = bootstrap.render_unit((self.source / "ccf-qq-relay.service.in").read_text(), self.prefix)
        lines = unit.splitlines()
        self.assertIn("WorkingDirectory=" + str(self.prefix), lines)
        self.assertIn("EnvironmentFile=-" + str(self.prefix / "token.env"), lines)
        self.assertIn('ExecStart="' + str(self.prefix / ".venv/bin/python") + '" "'
                      + str(self.prefix / "relay.py") + '" --config "'
                      + str(self.prefix / "config.json") + '"', lines)
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertNotIn('EnvironmentFile=-"', unit)

    def test_actual_install_generates_private_config_and_preserves_it(self):
        args = self.args()[:-1]
        with mock.patch.object(bootstrap.sys, "platform", "linux"), mock.patch.object(bootstrap.shutil, "which", return_value="systemctl"), mock.patch.object(bootstrap, "_run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                bootstrap.install(args, self.source)
        self.assertEqual((self.prefix / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.prefix.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.prefix / "state").stat().st_mode & 0o777, 0o700)
        config = (self.prefix / "config.json").read_bytes()
        self.assertNotIn(b"onebot_token\"", config)
        self.assertNotIn(b"allowed_users", config)
        self.assertNotIn(b"password", config)
        self.assertTrue(any(call.args[0] == ["systemctl", "--user", "daemon-reload"] for call in run.call_args_list))
        self.assertFalse(any("enable" in call.args[0] for call in run.call_args_list))
        with mock.patch.object(bootstrap.sys, "platform", "linux"), mock.patch.object(bootstrap.shutil, "which", return_value="systemctl"), mock.patch.object(bootstrap, "_run"), contextlib.redirect_stdout(io.StringIO()):
            bootstrap.install(args + ["--admin-qq", "67890"], self.source)
        self.assertEqual((self.prefix / "config.json").read_bytes(), config)


if __name__ == "__main__":
    unittest.main()

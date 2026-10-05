"""Wrapper safety tests; these do not replace testing the pinned container."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

import config_maintenance as maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.yaml"
        self.original = {"model": {"default": "keep-this-route"}, "terminal": {"backend": "docker"}}
        self.write(self.original)
        self.calls = []
        self.report = []
        self.native = SimpleNamespace(
            get_config_path=lambda: self.config,
            check_config_version=lambda: (self.read().get("_config_version", 0), 38),
            DEFAULT_CONFIG={"_config_version": 38, "model": {}, "memory": {}},
            REQUIRED_ENV_VARS={},
        )
        for name, value in (("CONFIG", self.config),):
            patcher = patch.object(maintenance, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, value):
        self.config.write_text(yaml.safe_dump(value), encoding="utf-8")

    def read(self):
        return yaml.safe_load(self.config.read_text(encoding="utf-8"))

    def cli(self, *args):
        self.calls.append(args)
        if args == ("path",):
            return str(self.config)
        if args == ("migrate",):
            # Simulated native migration only; wrapper never stamps the version.
            self.write(dict(self.read(), _config_version=38))
        if args[0] == "set":
            data = self.read()
            data["platform_toolsets"] = {"telegram": json.loads(args[2])}
            self.write(data)
        if args[0] == "get":
            return json.dumps(self.read()["platform_toolsets"]["telegram"])
        return ""

    def run_maintenance(self, action="migrate", tools=None, resolver=None):
        with patch.object(maintenance, "cli", side_effect=self.cli):
            maintenance.maintain(action, tools or maintenance.TOOLSETS, self.native,
                                 resolver or (lambda config: maintenance.TOOLSETS), self.report)

    def test_none_and_check_never_write(self):
        before = self.config.read_bytes()
        self.run_maintenance("none")
        self.assertEqual(self.calls, [])
        self.run_maintenance("check")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.config.parent.glob("*.bak")), [])

    def test_backup_precedes_writes_and_restart_is_idempotent(self):
        original_bytes = self.config.read_bytes()
        self.run_maintenance()
        backups = list(self.config.parent.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_bytes)
        self.assertEqual(self.read()["model"], self.original["model"])
        self.assertEqual(self.read()["terminal"], self.original["terminal"])
        self.assertEqual(self.read()["platform_toolsets"]["telegram"], maintenance.TOOLSETS)
        before = self.config.stat().st_mtime_ns
        self.calls.clear()
        self.run_maintenance()
        self.assertEqual(self.config.stat().st_mtime_ns, before)
        self.assertFalse(any(call[0] in ("migrate", "set") for call in self.calls))
        self.assertEqual(len(list(self.config.parent.glob("*.bak"))), 1)

    def test_missing_backup_stops_before_migration(self):
        with patch.object(maintenance, "backup_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_maintenance()
        self.assertNotIn(("migrate",), self.calls)
        self.assertEqual(self.read(), self.original)

    def test_failed_native_migration_retains_exact_backup(self):
        before = self.config.read_bytes()
        ordinary_cli = self.cli
        def failing_cli(*args):
            if args == ("migrate",):
                raise maintenance.MaintenanceError("native CLI failed")
            return ordinary_cli(*args)
        self.cli = failing_cli
        with self.assertRaises(maintenance.MaintenanceError):
            self.run_maintenance()
        self.assertEqual(next(self.config.parent.glob("*.bak")).read_bytes(), before)

    def test_refuses_floor_credentials_extra_tools_and_injection(self):
        cases = [
            ({"_config_version": 11}, {}, None, maintenance.TOOLSETS),
            ({}, {"NEW_SECRET": {}}, None, maintenance.TOOLSETS),
            ({"mcp_servers": {"custom": {}}}, {}, None, maintenance.TOOLSETS),
            ({}, {}, lambda config: ["memory", "session_search", "todo", "terminal"], maintenance.TOOLSETS),
            ({}, {}, None, ["memory", "session_search", "todo; touch /tmp/pwn"]),
        ]
        for raw, required, resolver, tools in cases:
            with self.subTest(raw=raw, required=bool(required), tools=tools):
                self.write(raw)
                self.native.REQUIRED_ENV_VARS = required
                with self.assertRaises(maintenance.MaintenanceError):
                    self.run_maintenance(tools=tools, resolver=resolver)
                self.assertEqual(list(self.config.parent.glob("*.bak")), [])

    def test_native_cli_closes_stdin_and_does_not_relay_secrets(self):
        result = SimpleNamespace(returncode=1, stdout=b"SECRET_SENTINEL", stderr=b"SECRET_SENTINEL")
        with patch.object(maintenance.subprocess, "run", return_value=result) as run:
            with self.assertRaises(maintenance.MaintenanceError) as error:
                maintenance.cli("migrate")
        self.assertNotIn("SECRET_SENTINEL", str(error.exception))
        self.assertEqual(run.call_args.kwargs["stdin"], maintenance.subprocess.DEVNULL)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_main_suppresses_raw_exception_values(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(maintenance.main("SECRET_SENTINEL unsupported action"), 1)
        self.assertNotIn("SECRET_SENTINEL", output.getvalue())

    def test_none_does_not_require_supervisor_options_access(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("options are root-only")):
            self.assertEqual(maintenance.main("none"), 0)

    def test_malformed_config_rejected_before_native_check(self):
        self.config.write_text("secret: [SECRET_SENTINEL", encoding="utf-8")
        with self.assertRaises(yaml.YAMLError):
            self.run_maintenance()
        self.assertNotIn(("check",), self.calls)
        self.assertEqual(list(self.config.parent.glob("*.bak")), [])


if __name__ == "__main__":
    unittest.main()

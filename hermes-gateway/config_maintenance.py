"""Fixed-purpose maintenance for the digest-pinned Hermes runtime, never a shell."""
import contextlib
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

CONFIG = Path("/data/config.yaml")
OPTIONS = Path("/data/options.json")
CLI = "/opt/hermes/.venv/bin/hermes"
TOOLSETS = ["memory", "session_search", "todo"]


class MaintenanceError(RuntimeError):
    """Messages are wrapper-owned constants, safe to include in the log."""


def cli(*args):
    # Migration's optional prompts catch EOF as No in the pinned source.
    # Never relay upstream output: warnings can contain configuration values.
    result = subprocess.run(
        [CLI, "config", *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False,
    )
    if result.returncode:
        raise MaintenanceError("native CLI failed; private backup retained if writes began")
    return result.stdout.decode("utf-8")


def raw_config():
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise MaintenanceError("configuration must be an existing regular file")
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MaintenanceError("configuration must be a mapping")
    return raw


def backup_config():
    original = CONFIG.read_bytes()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = CONFIG.with_name("config.yaml.haos-" + stamp + ".bak")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    if backup.is_symlink() or backup.read_bytes() != original:
        raise MaintenanceError("backup verification failed")
    return backup


def missing_names(defaults, raw, prefix=""):
    names = []
    for key, value in defaults.items():
        if key.startswith("_"):
            continue
        name = prefix + key
        if key not in raw:
            names.append(name)
        elif isinstance(value, dict) and isinstance(raw[key], dict):
            names.extend(missing_names(value, raw[key], name + "."))
    return names


def maintain(action, toolsets, native, platform_resolver, report):
    if action not in ("none", "check", "migrate"):
        raise MaintenanceError("unsupported maintenance action")
    if toolsets != TOOLSETS:
        raise MaintenanceError("maintenance requires the fixed approved toolsets")
    if action == "none":
        return
    # Deliberately narrower than a general toolset selector.
    if native.get_config_path() != CONFIG or cli("path").strip() != str(CONFIG):
        raise MaintenanceError("unexpected Hermes configuration path")
    raw = raw_config()  # Reject malformed YAML before native fallback/repair paths.
    current, latest = native.check_config_version()
    cli("check")
    report.append("config path: /data/config.yaml")
    report.append(f"config version before: {current}; runtime latest: {latest}")
    report.append("missing default key names: " + json.dumps(missing_names(native.DEFAULT_CONFIG, raw)))
    if action == "check":
        return
    # Required prompts do not catch EOF. Refuse if this pinned contract changes.
    if native.REQUIRED_ENV_VARS:
        raise MaintenanceError("runtime requires interactive migration")
    if "_config_version" in raw and current < 12:
        raise MaintenanceError("explicit schema predates native migration support floor")
    if current > latest:
        raise MaintenanceError("configuration is newer than runtime")
    if raw.get("mcp_servers") or raw.get("plugins"):
        raise MaintenanceError("additional tool sources require separate review")
    env_file = CONFIG.parent / ".env"
    if env_file.is_symlink():
        raise MaintenanceError("symlinked environment file requires separate review")
    candidate = dict(raw)
    candidate_platforms = dict(raw.get("platform_toolsets") or {})
    candidate_platforms["telegram"] = list(toolsets)
    candidate["platform_toolsets"] = candidate_platforms
    resolved = set(platform_resolver(candidate))
    if resolved != set(TOOLSETS):
        raise MaintenanceError("native tool resolution differs from approved tool names")
    saved = raw.get("platform_toolsets", {})
    if not isinstance(saved, dict):
        raise MaintenanceError("platform_toolsets must be a mapping")
    if current == latest and saved.get("telegram") == TOOLSETS:
        report.append("maintenance: already applied; no config writes or migration")
        return
    backup = backup_config()
    report.append("verified private backup: " + str(backup))
    # A retained backup is the recovery point if native migration fails midway.
    # Do not guess a rollback for native .env normalization/legacy env removals.
    if current < latest:
        cli("migrate")
        report.append("migration action: native config migrate; optional prompts declined")
    migrated = raw_config()
    after, expected = native.check_config_version()
    if after != expected:
        raise MaintenanceError("native migration did not reach current schema")
    if migrated.get("platform_toolsets", {}).get("telegram") != TOOLSETS:
        cli("set", "platform_toolsets.telegram", json.dumps(TOOLSETS))
    cli("check")
    # Native get validates the resolved config rather than just the YAML override.
    if json.loads(cli("get", "platform_toolsets.telegram", "--json")) != TOOLSETS:
        raise MaintenanceError("resolved Telegram override differs from approved list")
    if raw_config().get("platform_toolsets", {}).get("telegram") != TOOLSETS:
        raise MaintenanceError("Telegram override was not persisted")
    report.append(f"config version after: {after}")
    report.append("Telegram toolsets: memory, session_search, todo")
    report.append("native toolset resolution: memory, session_search, todo")


def main():
    report = []
    phase = "options"
    try:
        options = json.loads(OPTIONS.read_text(encoding="utf-8"))
        action = options.get("config_maintenance_action", "none")
        if action == "none":
            return 0
        phase = "maintenance"
        # Silence imports, native diagnostics and exception contents as well as CLI.
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            sys.path.insert(0, "/opt/hermes")
            from hermes_cli import config as native
            from hermes_cli.tools_config import _get_platform_tools
            maintain(action, TOOLSETS, native,
                     lambda config: _get_platform_tools(
                         config, "telegram", include_default_mcp_servers=False), report)
    except Exception as error:
        for line in report:
            print("[config-maintenance] " + line)
        print(f"[config-maintenance] FAILED during {phase}; raw diagnostics withheld; Gateway not started", file=sys.stderr)
        if isinstance(error, MaintenanceError):
            print("[config-maintenance] " + str(error), file=sys.stderr)
        return 1
    for line in report:
        print("[config-maintenance] " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

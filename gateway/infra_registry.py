"""Safe infrastructure target registry for Hermes.

The registry references local credential mechanisms (SSH config, AWS profiles,
Scalingo CLI) without reading or storing secret material.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_TARGETS = (
    "vps",
    "openclaw-contabo",
    "pratica",
    "e2r",
    "irb",
    "scalingo",
    "sami",
    "claude-openclaw",
    "jesus-openclaw",
)

SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(aws_secret_access_key|secret_access_key)\s*=\s*[^\s]+"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/\-=]{16,}"),
    re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key)=([^&\s]+)"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


@dataclass(frozen=True)
class InfraTarget:
    name: str
    kind: str
    command: str
    scope: tuple[str, ...] = ("read", "diagnose")
    risk: str = "low"
    source: str = "discovered"


def redact_infra_output(text: str, limit: int = 6000) -> str:
    """Redact common credentials from tool output before model exposure."""
    value = str(text or "")
    for pattern in SECRET_PATTERNS:
        def _replace(match: re.Match[str]) -> str:
            if match.lastindex and match.lastindex >= 1:
                return f"{match.group(1)}[REDACTED]"
            return "[REDACTED]"
        value = pattern.sub(_replace, value)
    value = re.sub(r"([?&](?:token|key|secret|password|signature)=)[^&\s]+", r"\1[REDACTED]", value, flags=re.I)
    return value[:limit]


def parse_ssh_aliases(config_path: Path | None = None) -> list[str]:
    """Return concrete Host aliases from ~/.ssh/config without reading keys."""
    path = config_path or Path.home() / ".ssh" / "config"
    if not path.exists():
        return []
    aliases: list[str] = []
    for raw in path.read_text(errors="replace").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if parts and parts[0].lower() == "host":
            for alias in parts[1:]:
                if "*" in alias or "?" in alias:
                    continue
                aliases.append(alias)
    return list(dict.fromkeys(aliases))


def aws_profiles(config_path: Path | None = None, credentials_path: Path | None = None) -> list[str]:
    """List AWS profile names without reading credential values."""
    names: set[str] = set()
    for path in (
        config_path or Path.home() / ".aws" / "config",
        credentials_path or Path.home() / ".aws" / "credentials",
    ):
        if not path.exists():
            continue
        parser = configparser.ConfigParser()
        parser.read(path)
        for section in parser.sections():
            if section == "default":
                names.add("default")
            elif section.startswith("profile "):
                names.add(section.replace("profile ", "", 1).strip())
            else:
                names.add(section)
    return sorted(names)


def discover_targets() -> list[InfraTarget]:
    targets: list[InfraTarget] = []
    ssh_aliases = parse_ssh_aliases()
    for alias in ssh_aliases:
        risk = "medium" if alias in DEFAULT_TARGETS else "low"
        targets.append(InfraTarget(name=alias, kind="ssh", command=f"ssh {alias}", risk=risk))

    for profile in aws_profiles():
        cmd = "aws" if profile == "default" else f"aws --profile {profile}"
        targets.append(InfraTarget(name=f"aws:{profile}", kind="aws", command=cmd, risk="medium"))

    if shutil.which("scalingo"):
        targets.append(InfraTarget(name="scalingo", kind="scalingo", command="scalingo", risk="medium"))
    elif "scalingo" in ssh_aliases:
        targets.append(InfraTarget(name="scalingo-ssh", kind="ssh", command="ssh scalingo", risk="medium"))

    return targets


def list_targets() -> list[dict[str, Any]]:
    return [asdict(target) for target in discover_targets()]


def _find_target(name: str) -> InfraTarget | None:
    for target in discover_targets():
        if target.name == name:
            return target
    return None


def _run_checked(cmd: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": redact_infra_output(proc.stdout),
            "stderr": redact_infra_output(proc.stderr),
        }
    except Exception as exc:
        return {"ok": False, "error": redact_infra_output(str(exc))}


def execute_readonly_action(action: str, target_name: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Execute approved read-only infra actions."""
    if action == "list_targets":
        return {"targets": list_targets()}

    if action == "ssh_check":
        target = _find_target(str(target_name or ""))
        if not target or target.kind != "ssh":
            return {"ok": False, "error": "unknown ssh target"}
        return _run_checked(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target.name, "true"], timeout=12)

    if action == "tail_logs":
        target = _find_target(str(target_name or ""))
        service = str(kwargs.get("service") or "hermes-gateway")
        lines = str(int(kwargs.get("lines") or 80))
        if not target or target.kind != "ssh":
            return {"ok": False, "error": "unknown ssh target"}
        cmd = f"journalctl -u {service} -n {lines} --no-pager 2>/dev/null || tail -n {lines} /var/log/syslog"
        return _run_checked(["ssh", target.name, cmd], timeout=25)

    if action == "service_status":
        target = _find_target(str(target_name or ""))
        service = str(kwargs.get("service") or "hermes-gateway")
        if not target or target.kind != "ssh":
            return {"ok": False, "error": "unknown ssh target"}
        return _run_checked(["ssh", target.name, f"systemctl is-active {service}; systemctl status {service} --no-pager -l | head -80"], timeout=20)

    if action == "aws_identity":
        profile = str(kwargs.get("profile") or target_name or "default").replace("aws:", "", 1)
        cmd = ["aws"]
        if profile and profile != "default":
            cmd.extend(["--profile", profile])
        cmd.extend(["sts", "get-caller-identity", "--output", "json"])
        return _run_checked(cmd, timeout=20)

    if action == "aws_list_resources":
        profile = str(kwargs.get("profile") or target_name or "default").replace("aws:", "", 1)
        service = str(kwargs.get("service") or "ec2")
        cmd = ["aws"]
        if profile and profile != "default":
            cmd.extend(["--profile", profile])
        if service == "lightsail":
            cmd.extend(["lightsail", "get-instances", "--output", "json"])
        else:
            cmd.extend(["ec2", "describe-instances", "--output", "json"])
        return _run_checked(cmd, timeout=30)

    if action == "scalingo_apps":
        return _run_checked(["scalingo", "apps"], timeout=20)

    if action == "scalingo_logs":
        app = str(kwargs.get("app") or "")
        if not app:
            return {"ok": False, "error": "app is required"}
        return _run_checked(["scalingo", "--app", app, "logs", "--lines", str(int(kwargs.get("lines") or 80))], timeout=30)

    return {"ok": False, "error": f"unknown action: {action}"}


def dumps_safe(data: Any) -> str:
    return redact_infra_output(json.dumps(data, ensure_ascii=False, indent=2), limit=12000)

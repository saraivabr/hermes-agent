"""Safe infrastructure operations tool.

This tool exposes read-only infrastructure actions through a sanitized registry.
Credentials stay in their native stores: SSH config/agent, AWS profiles, and
Scalingo CLI auth.
"""

from __future__ import annotations

from gateway.infra_registry import dumps_safe, execute_readonly_action, list_targets
from tools.registry import registry, tool_error


INFRA_SCHEMA = {
    "name": "infra",
    "description": (
        "List and inspect configured infrastructure targets using local credentials by reference. "
        "Read-only actions include SSH checks/logs/status, AWS identity/resource listing, and Scalingo apps/logs. "
        "Never returns raw secrets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_targets",
                    "ssh_check",
                    "tail_logs",
                    "service_status",
                    "aws_identity",
                    "aws_list_resources",
                    "scalingo_apps",
                    "scalingo_logs",
                ],
            },
            "target": {"type": "string", "description": "Infrastructure target name, e.g. jesus-openclaw or aws:default."},
            "service": {"type": "string", "description": "Service name for SSH service_status/tail_logs."},
            "profile": {"type": "string", "description": "AWS profile name."},
            "app": {"type": "string", "description": "Scalingo app name."},
            "lines": {"type": "integer", "description": "Log line count."},
        },
        "required": ["action"],
    },
}


def _check_infra() -> bool:
    return bool(list_targets())


def infra_tool(args, **kw):
    action = str(args.get("action") or "").strip()
    if not action:
        return tool_error("action is required")
    result = execute_readonly_action(
        action,
        target_name=args.get("target"),
        service=args.get("service"),
        profile=args.get("profile"),
        app=args.get("app"),
        lines=args.get("lines"),
    )
    return dumps_safe(result)


registry.register(
    name="infra",
    toolset="infra",
    schema=INFRA_SCHEMA,
    handler=infra_tool,
    check_fn=_check_infra,
    emoji="🖥️",
)

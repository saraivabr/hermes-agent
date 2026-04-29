"""Short WhatsApp-facing system status copy for gateway events."""

from __future__ import annotations

import os
import re
import shlex


BUSY_QUEUE = "🕓 Na fila."
BUSY_STEER = "↪️ Já vou nisso."
BUSY_INTERRUPT = "⚡ Trocando rota."
LONG_RUNNING = "⚡ Ainda nisso."
MODEL_WAITING = "🧠 IA pensando."
SHUTDOWN = "⚠️ Vai cortar aqui."
RESTART = "↻ Reiniciando a ponte."
SESSION_RESET = "🧼 Sessão limpa."
UPDATE = "↻ Atualizando EMPRESA.IA."
GENERIC_TOOL = "⚙️ Rodando."
RESTARTING_BUSY = "↻ Reiniciando. Já volto."
SHUTTING_DOWN_BUSY = "⚠️ Fechando a ponte."
INTERRUPTED_MODEL = "⚠️ Interrompido na resposta."
_MAX_STATUS_LEN = 45

_MODEL_ACTIVITY_HINTS = (
    "api",
    "model",
    "provider",
    "response",
    "stream",
    "thinking",
    "non-streaming",
)

_READ_TOOLS = {"read_file", "search_files", "list_files", "grep", "glob", "find", "ls", "cat", "sed", "rg"}
_EDIT_TOOLS = {"write_file", "patch", "edit_file", "apply_patch", "skill_manage"}
_WEB_TOOLS = {
    "web_search",
    "web_extract",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_snapshot",
    "browser_scroll",
    "fetch",
}
_SEND_TOOLS = {"send_message"}
_DELEGATE_TOOLS = {"delegate_task", "subagent", "spawn_agent"}
_TERMINAL_TOOLS = {"terminal", "process", "execute_code", "exec_command", "shell"}

_READ_COMMANDS = {"cat", "sed", "rg", "grep", "ls", "find", "fd", "head", "tail", "less", "nl", "wc"}
_EDIT_COMMANDS = {"apply_patch", "patch"}
_WEB_COMMANDS = {"curl", "wget"}
_RUN_COMMANDS = {
    "pytest",
    "python",
    "python3",
    "pip",
    "npm",
    "pnpm",
    "yarn",
    "node",
    "systemctl",
    "journalctl",
    "ssh",
    "scp",
    "git",
    "docker",
    "docker-compose",
    "make",
}
_SECRETISH_RE = re.compile(
    r"(?i)(token|secret|password|passwd|authorization|bearer|api[_-]?key|session|cookie|credential|private)"
)
_PHONE_OR_ID_RE = re.compile(r"(?:\+?\d[\d\s().-]{9,}\d|[0-9a-f]{24,}|[A-Za-z0-9_-]{32,})")
_WHATSAPP_ID_RE = re.compile(r"\b\d{8,}@[a-z.]+\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:~|/|\.{1,2}/)?[\w@.+-]+(?:/[\w@.+-]+)+")


def is_whatsapp_platform(platform: object) -> bool:
    """Return True for the gateway Platform.WHATSAPP enum or the string value."""
    return getattr(platform, "value", platform) == "whatsapp"


def render_busy_ack(mode: str, queue_depth: int | None = None) -> str:
    """Render a short busy acknowledgement for WhatsApp."""
    if mode == "steer":
        return BUSY_STEER
    if mode == "interrupt":
        return BUSY_INTERRUPT
    return BUSY_QUEUE


def compact_file_label(path: str | None) -> str | None:
    """Return a safe basename for user-facing progress."""
    if not path:
        return None
    value = str(path).strip().strip("\"'`")
    if not value or _SECRETISH_RE.search(value):
        return None
    value = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not value:
        return None
    base = os.path.basename(value)
    if not base or base in {".", "..", "~"}:
        return None
    if _PHONE_OR_ID_RE.fullmatch(base):
        return None
    if len(base) > 28:
        base = base[:25] + "..."
    return base


def compact_command_label(command: str | None) -> str | None:
    """Return a safe, compact command/action label."""
    if not command:
        return None
    text = str(command).strip()
    if not text or _SECRETISH_RE.search(text):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return None
    cmd = os.path.basename(parts[0])
    if cmd in {"sudo", "env", "command", "timeout"} and len(parts) > 1:
        cmd = os.path.basename(parts[1])
    if cmd in {"python", "python3"} and len(parts) >= 3 and parts[1] == "-m":
        return parts[2][:28]
    if cmd == "ssh":
        return "servidor"
    if cmd == "curl":
        return "curl"
    if cmd in {"npm", "pnpm", "yarn"} and len(parts) >= 2:
        return f"{cmd} {parts[1]}"[:28]
    return cmd[:28]


def sanitize_progress_target(preview: str | None) -> str | None:
    """Extract a safe object from a tool preview without leaking raw context."""
    if not preview:
        return None
    text = " ".join(str(preview).replace("\n", " ").split())
    if not text or _SECRETISH_RE.search(text):
        return None
    if _WHATSAPP_ID_RE.search(text) or _PHONE_OR_ID_RE.search(text):
        text = _WHATSAPP_ID_RE.sub("", text)
        text = _PHONE_OR_ID_RE.sub("", text).strip()
    if not text:
        return None

    for match in _PATH_RE.findall(text):
        label = compact_file_label(match)
        if label:
            return label

    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    for part in reversed(parts[1:]):
        if part.startswith("-"):
            continue
        label = compact_file_label(part)
        if label and (("." in label) or "/" in part or part.startswith("~")):
            return label
    return compact_command_label(text)


def classify_tool_action(tool_name: str | None, preview: str | None = None) -> tuple[str, str, str | None]:
    """Classify a tool event as icon, verb and optional safe target."""
    name = str(tool_name or "").lower()
    if preview and _SECRETISH_RE.search(str(preview)):
        return "⚙️", "Rodando", None
    target = sanitize_progress_target(preview)

    command = compact_command_label(preview)
    if name in _TERMINAL_TOOLS and command:
        if command in _READ_COMMANDS:
            return "🔎", "Lendo", target
        if command in _EDIT_COMMANDS:
            return "🩹", "Editando", target
        if command in _WEB_COMMANDS:
            return "🌐", "Abrindo", target or command
        if command in _RUN_COMMANDS or command:
            return "⌨️", "Rodando", command

    if name in _WEB_TOOLS or any(token in name for token in ("web", "browser", "fetch")):
        return "🌐", "Abrindo", target or "web"
    if name in _READ_TOOLS or any(token in name for token in ("read", "search", "list", "grep", "glob")):
        return "🔎", "Lendo", target
    if name in _EDIT_TOOLS or any(token in name for token in ("patch", "write", "edit")):
        return "🩹", "Editando", target
    if name in _SEND_TOOLS or "send" in name:
        return "📨", "Enviando", None
    if name in _DELEGATE_TOOLS or "agent" in name:
        return "↪️", "Puxando", "ajuda"
    if name in _TERMINAL_TOOLS:
        return "⌨️", "Rodando", command
    return "⚙️", "Rodando", None


def _compact_status(icon: str, verb: str, target: str | None) -> str:
    if not target:
        if icon == "📨":
            return "📨 Enviando."
        if icon == "↪️":
            return "↪️ Puxando ajuda."
        return GENERIC_TOOL if icon == "⚙️" else f"{icon} {verb}."
    if icon == "↪️" and target == "ajuda":
        return "↪️ Puxando ajuda."
    msg = f"{icon} {verb} {target}"
    if len(msg) > _MAX_STATUS_LEN:
        max_target = max(8, _MAX_STATUS_LEN - len(f"{icon} {verb} ") - 3)
        msg = f"{icon} {verb} {target[:max_target]}..."
    return msg


def render_tool_progress(tool_name: str | None, preview: str | None = None, mode: str = "new") -> str:
    """Render a short WhatsApp tool-progress label with a sanitized target."""
    icon, verb, target = classify_tool_action(tool_name, preview)
    return _compact_status(icon, verb, target)


def render_long_running(activity: str | None = None) -> str:
    """Render a heartbeat status from internal activity text."""
    text = str(activity or "").lower()
    if any(hint in text for hint in _MODEL_ACTIVITY_HINTS):
        return MODEL_WAITING
    return LONG_RUNNING


def render_shutdown(restarting: bool) -> str:
    """Render restart/shutdown alert copy."""
    return RESTART if restarting else SHUTDOWN


def render_session_reset(reason: str | None = None) -> str:
    """Render session reset copy."""
    return SESSION_RESET


def render_gateway_draining(restarting: bool) -> str:
    """Render a short gateway-draining notice."""
    return RESTARTING_BUSY if restarting else SHUTTING_DOWN_BUSY


def render_operation_interrupted(text: str | None = None) -> str:
    """Render a user-facing interruption notice."""
    return INTERRUPTED_MODEL

"""WhatsApp admin/control tool via the local Baileys bridge.

This tool exposes WhatsApp bridge group/admin and history endpoints to the
agent. It is intentionally direct-to-bridge because these operations are not
part of the generic cross-platform send_message interface.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict
from urllib import error, parse, request

from tools.registry import registry, tool_error


DEFAULT_BRIDGE_PORT = 3000


def _bridge_port() -> int:
    raw = os.getenv("WHATSAPP_BRIDGE_PORT") or os.getenv("HERMES_WHATSAPP_BRIDGE_PORT")
    try:
        return int(raw) if raw else DEFAULT_BRIDGE_PORT
    except ValueError:
        return DEFAULT_BRIDGE_PORT


def _base_url() -> str:
    return f"http://127.0.0.1:{_bridge_port()}"


def _check_whatsapp_admin() -> bool:
    try:
        with request.urlopen(f"{_base_url()}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _redact_bridge_error(text: str) -> str:
    # Bridge errors should not contain secrets, but keep output compact/safe.
    return (text or "").strip()[:1000]


def _http_json(method: str, path: str, payload: Dict[str, Any] | None = None, timeout: int = 30) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            parsed.setdefault("http_status", resp.status)
            return parsed
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"error": _redact_bridge_error(body)}
        parsed.setdefault("error", f"HTTP {exc.code}")
        parsed["http_status"] = exc.code
        return parsed
    except Exception as exc:
        return {"error": _redact_bridge_error(str(exc))}


def _normalize_participants(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("\n", ",").split(",")]
        return [part for part in parts if part]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _confirm_required(args: Dict[str, Any], action: str) -> str | None:
    if bool(args.get("confirm")):
        return None
    return f"Action {action} mutates WhatsApp state and requires confirm=true"


WHATSAPP_ADMIN_SCHEMA = {
    "name": "whatsapp_admin",
    "description": (
        "Operate the connected WhatsApp bridge: inspect health/history/chats and perform "
        "group admin actions such as create group, rename group, set photo/description, "
        "add/remove/promote/demote participants, group settings, and invite links. "
        "Use with policy guardrails: group/admin actions mutate WhatsApp state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "health",
                    "chat_info",
                    "history_chats",
                    "search",
                    "history",
                    "profile",
                    "presence",
                    "read",
                    "react",
                    "chat_modify",
                    "group_metadata",
                    "join_approval",
                    "create_group",
                    "set_subject",
                    "set_description",
                    "set_photo",
                    "participants",
                    "group_settings",
                    "invite",
                ],
                "description": "Operation to perform.",
            },
            "chatId": {"type": "string", "description": "WhatsApp chat/group JID, e.g. 120363...@g.us."},
            "jid": {"type": "string", "description": "WhatsApp contact/group JID for profile lookup."},
            "messageId": {"type": "string", "description": "WhatsApp message ID for read/react/chat_modify."},
            "emoji": {"type": "string", "description": "Emoji reaction text."},
            "presence": {
                "type": "string",
                "enum": ["available", "unavailable", "composing", "recording", "paused"],
                "description": "Presence state for presence action.",
            },
            "chatModifyAction": {
                "type": "string",
                "enum": ["archive", "unarchive", "mute", "unmute", "mark_unread", "mark_read", "star", "unstar"],
                "description": "Action for chat_modify.",
            },
            "durationSeconds": {"type": "integer", "description": "Duration for mute action."},
            "subject": {"type": "string", "description": "Group subject/name for create_group or set_subject."},
            "description": {"type": "string", "description": "Group description text for set_description."},
            "filePath": {"type": "string", "description": "Absolute local path to image for set_photo."},
            "participants": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Participant phone numbers or JIDs for group operations.",
            },
            "participant": {"type": "string", "description": "Single participant phone/JID; convenience alternative to participants[]."},
            "participantAction": {
                "type": "string",
                "enum": ["add", "remove", "promote", "demote", "approve", "reject"],
                "description": "Action for participants operation.",
            },
            "setting": {
                "type": "string",
                "enum": ["announcement", "not_announcement", "locked", "unlocked"],
                "description": "Group setting for group_settings operation.",
            },
            "revoke": {"type": "boolean", "description": "For invite: revoke current invite and return a new one."},
            "confirm": {"type": "boolean", "description": "Required for actions that mutate WhatsApp state."},
            "q": {"type": "string", "description": "Search query for local WhatsApp history."},
            "limit": {"type": "integer", "description": "History/search result limit."},
            "before": {"type": "integer", "description": "Only history before this Unix timestamp."},
            "after": {"type": "integer", "description": "Only history after this Unix timestamp."},
            "isGroup": {"type": "boolean", "description": "Filter history/search to group or DM messages."},
        },
        "required": ["action"],
    },
}


def whatsapp_admin_tool(args, **kw):
    action = str(args.get("action") or "").strip().lower()

    if action == "health":
        return json.dumps(_http_json("GET", "/health"), ensure_ascii=False)

    if action == "chat_info":
        chat_id = args.get("chatId")
        if not chat_id:
            return tool_error("chatId is required for chat_info")
        return json.dumps(_http_json("GET", f"/chat/{parse.quote(str(chat_id), safe='@._-')}", timeout=10), ensure_ascii=False)

    if action == "profile":
        jid = args.get("jid") or args.get("chatId")
        if not jid:
            return tool_error("jid or chatId is required for profile")
        return json.dumps(_http_json("GET", f"/profile/{parse.quote(str(jid), safe='@._-')}", timeout=15), ensure_ascii=False)

    if action == "presence":
        chat_id = args.get("chatId")
        if not chat_id:
            return tool_error("chatId is required for presence")
        return json.dumps(_http_json("POST", "/presence", {"chatId": chat_id, "presence": args.get("presence") or "composing"}, timeout=10), ensure_ascii=False)

    if action == "read":
        chat_id = args.get("chatId")
        if not chat_id:
            return tool_error("chatId is required for read")
        payload = {"chatId": chat_id, "messageId": args.get("messageId"), "participant": args.get("participant")}
        return json.dumps(_http_json("POST", "/read", payload, timeout=10), ensure_ascii=False)

    if action == "react":
        chat_id = args.get("chatId")
        message_id = args.get("messageId")
        if not chat_id or not message_id:
            return tool_error("chatId and messageId are required for react")
        payload = {"chatId": chat_id, "messageId": message_id, "participant": args.get("participant"), "emoji": args.get("emoji") or ""}
        return json.dumps(_http_json("POST", "/react", payload, timeout=10), ensure_ascii=False)

    if action == "chat_modify":
        chat_id = args.get("chatId")
        chat_action = args.get("chatModifyAction")
        if not chat_id or not chat_action:
            return tool_error("chatId and chatModifyAction are required for chat_modify")
        if chat_action in {"archive", "unarchive", "mute", "unmute", "mark_unread", "star", "unstar"}:
            err = _confirm_required(args, "chat_modify")
            if err:
                return tool_error(err)
        payload = {
            "chatId": chat_id,
            "action": chat_action,
            "messageId": args.get("messageId"),
            "participant": args.get("participant"),
            "durationSeconds": args.get("durationSeconds"),
        }
        return json.dumps(_http_json("POST", "/chat/modify", payload, timeout=15), ensure_ascii=False)

    if action == "history_chats":
        return json.dumps(_http_json("GET", "/history/chats", timeout=10), ensure_ascii=False)

    if action in {"search", "history"}:
        query: Dict[str, Any] = {}
        for key in ("chatId", "q", "limit", "before", "after", "isGroup"):
            if args.get(key) is not None:
                query[key] = args.get(key)
        path = f"/{action}"
        if query:
            path += "?" + parse.urlencode(query)
        return json.dumps(_http_json("GET", path, timeout=20), ensure_ascii=False)

    if action == "group_metadata":
        chat_id = args.get("chatId")
        if not chat_id:
            return tool_error("chatId is required for group_metadata")
        return json.dumps(_http_json("GET", f"/groups/metadata/{parse.quote(str(chat_id), safe='@._-')}", timeout=20), ensure_ascii=False)

    if action == "join_approval":
        chat_id = args.get("chatId")
        participant_action = str(args.get("participantAction") or "approve").lower()
        participants = _normalize_participants(args.get("participants"))
        if args.get("participant"):
            participants.append(str(args["participant"]).strip())
        if not chat_id or not participants:
            return tool_error("chatId and participants[] are required for join_approval")
        err = _confirm_required(args, "join_approval")
        if err:
            return tool_error(err)
        payload = {"chatId": chat_id, "participants": participants, "action": participant_action}
        return json.dumps(_http_json("POST", "/groups/join-approval", payload, timeout=60), ensure_ascii=False)

    if action == "create_group":
        subject = args.get("subject")
        participants = _normalize_participants(args.get("participants"))
        if args.get("participant"):
            participants.append(str(args["participant"]).strip())
        if not subject or not participants:
            return tool_error("subject and participants[] are required for create_group")
        err = _confirm_required(args, "create_group")
        if err:
            return tool_error(err)
        return json.dumps(_http_json("POST", "/groups/create", {"subject": subject, "participants": participants}, timeout=60), ensure_ascii=False)

    if action == "set_subject":
        chat_id = args.get("chatId")
        subject = args.get("subject")
        if not chat_id or not subject:
            return tool_error("chatId and subject are required for set_subject")
        err = _confirm_required(args, "set_subject")
        if err:
            return tool_error(err)
        return json.dumps(_http_json("POST", "/groups/subject", {"chatId": chat_id, "subject": subject}), ensure_ascii=False)

    if action == "set_description":
        chat_id = args.get("chatId")
        if not chat_id:
            return tool_error("chatId is required for set_description")
        err = _confirm_required(args, "set_description")
        if err:
            return tool_error(err)
        return json.dumps(_http_json("POST", "/groups/description", {"chatId": chat_id, "description": args.get("description") or ""}), ensure_ascii=False)

    if action == "set_photo":
        chat_id = args.get("chatId")
        file_path = args.get("filePath")
        if not chat_id or not file_path:
            return tool_error("chatId and filePath are required for set_photo")
        err = _confirm_required(args, "set_photo")
        if err:
            return tool_error(err)
        return json.dumps(_http_json("POST", "/groups/photo", {"chatId": chat_id, "filePath": file_path}, timeout=60), ensure_ascii=False)

    if action == "participants":
        chat_id = args.get("chatId")
        participant_action = str(args.get("participantAction") or "add").lower()
        participants = _normalize_participants(args.get("participants"))
        if args.get("participant"):
            participants.append(str(args["participant"]).strip())
        if not chat_id or not participants:
            return tool_error("chatId and participants[] are required for participants")
        err = _confirm_required(args, "participants")
        if err:
            return tool_error(err)
        payload = {"chatId": chat_id, "participants": participants, "action": participant_action}
        return json.dumps(_http_json("POST", "/groups/participants", payload, timeout=60), ensure_ascii=False)

    if action == "group_settings":
        chat_id = args.get("chatId")
        setting = args.get("setting")
        if not chat_id or not setting:
            return tool_error("chatId and setting are required for group_settings")
        err = _confirm_required(args, "group_settings")
        if err:
            return tool_error(err)
        return json.dumps(_http_json("POST", "/groups/settings", {"chatId": chat_id, "setting": setting}), ensure_ascii=False)

    if action == "invite":
        chat_id = args.get("chatId")
        if not chat_id:
            return tool_error("chatId is required for invite")
        if args.get("revoke"):
            err = _confirm_required(args, "invite revoke")
            if err:
                return tool_error(err)
        return json.dumps(_http_json("POST", "/groups/invite", {"chatId": chat_id, "revoke": bool(args.get("revoke"))}), ensure_ascii=False)

    return tool_error(f"Unknown action: {action}")


registry.register(
    name="whatsapp_admin",
    toolset="messaging",
    schema=WHATSAPP_ADMIN_SCHEMA,
    handler=whatsapp_admin_tool,
    check_fn=_check_whatsapp_admin,
    emoji="🟢",
)

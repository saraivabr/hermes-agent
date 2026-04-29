"""WhatsApp-specific human behavior policy.

This module is intentionally pure: it decides what the WhatsApp edge should do
without touching Baileys, credentials, or transport state.  The adapter and
bridge execute these decisions at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class WhatsAppPresence(str, Enum):
    AVAILABLE = "available"
    COMPOSING = "composing"
    PAUSED = "paused"
    RECORDING = "recording"
    UNAVAILABLE = "unavailable"


class WhatsAppChatKind(str, Enum):
    DM = "dm"
    GROUP = "group"
    CHANNEL = "channel"


@dataclass(frozen=True)
class WhatsAppHumanBehaviorPolicy:
    """Conservative WhatsApp behavior defaults.

    Defaults optimize for human-feeling feedback without creating endless
    typing indicators or leaking cross-chat context.
    """

    read_receipts: bool = True
    reactions: bool = True
    quote_group_replies: bool = True
    group_ambient_context: bool = True
    max_context_messages_dm: int = 12
    max_context_messages_group: int = 20

    def chat_kind(self, chat_type: str | None = None, chat_id: str | None = None) -> WhatsAppChatKind:
        value = str(chat_type or "").lower()
        jid = str(chat_id or "")
        if value in {"group", "forum"} or jid.endswith("@g.us"):
            return WhatsAppChatKind.GROUP
        if value == "channel" or jid.endswith("@newsletter"):
            return WhatsAppChatKind.CHANNEL
        return WhatsAppChatKind.DM

    def should_quote_reply(self, *, chat_type: str | None = None, chat_id: str | None = None, message_id: str | None = None) -> bool:
        if not message_id:
            return False
        return self.quote_group_replies and self.chat_kind(chat_type, chat_id) == WhatsAppChatKind.GROUP

    def should_inject_context(self, *, chat_type: str | None = None, chat_id: str | None = None, triggered: bool = False) -> bool:
        kind = self.chat_kind(chat_type, chat_id)
        if kind == WhatsAppChatKind.CHANNEL:
            return False
        if kind == WhatsAppChatKind.GROUP:
            return self.group_ambient_context and triggered
        return True

    def context_limit(self, *, chat_type: str | None = None, chat_id: str | None = None) -> int:
        return (
            self.max_context_messages_group
            if self.chat_kind(chat_type, chat_id) == WhatsAppChatKind.GROUP
            else self.max_context_messages_dm
        )

    def initial_reaction(self, *, chat_type: str | None = None, chat_id: str | None = None) -> str | None:
        if not self.reactions:
            return None
        if self.chat_kind(chat_type, chat_id) == WhatsAppChatKind.CHANNEL:
            return None
        return "👀"

    def completion_reaction(self, *, success: bool, cancelled: bool = False) -> str | None:
        if not self.reactions or cancelled:
            return None
        return "✅" if success else "⚠️"

    def typing_sequence(self, *, voice: bool = False) -> tuple[WhatsAppPresence, ...]:
        if voice:
            return (
                WhatsAppPresence.AVAILABLE,
                WhatsAppPresence.RECORDING,
                WhatsAppPresence.PAUSED,
                WhatsAppPresence.UNAVAILABLE,
            )
        return (
            WhatsAppPresence.AVAILABLE,
            WhatsAppPresence.COMPOSING,
            WhatsAppPresence.PAUSED,
            WhatsAppPresence.COMPOSING,
            WhatsAppPresence.UNAVAILABLE,
        )


def source_chat_fields(event_or_source: Any) -> dict[str, str | None]:
    """Extract normalized chat fields from a MessageEvent or SessionSource."""
    source = getattr(event_or_source, "source", event_or_source)
    return {
        "chat_id": getattr(source, "chat_id", None),
        "chat_type": getattr(source, "chat_type", None),
    }

import asyncio
from types import SimpleNamespace

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.whatsapp import WhatsAppAdapter
from gateway.session import SessionSource
from gateway.whatsapp_human_behavior import WhatsAppHumanBehaviorPolicy, WhatsAppPresence


def test_presence_sequence_is_finite_and_human():
    policy = WhatsAppHumanBehaviorPolicy()

    assert policy.typing_sequence() == (
        WhatsAppPresence.AVAILABLE,
        WhatsAppPresence.COMPOSING,
        WhatsAppPresence.PAUSED,
        WhatsAppPresence.COMPOSING,
        WhatsAppPresence.UNAVAILABLE,
    )
    assert WhatsAppPresence.RECORDING in policy.typing_sequence(voice=True)


def test_group_context_requires_same_group_trigger():
    policy = WhatsAppHumanBehaviorPolicy()

    assert policy.should_inject_context(chat_type="dm", chat_id="1@s.whatsapp.net", triggered=False)
    assert policy.should_inject_context(chat_type="group", chat_id="120@g.us", triggered=True)
    assert not policy.should_inject_context(chat_type="group", chat_id="120@g.us", triggered=False)
    assert not policy.should_inject_context(chat_type="channel", chat_id="x@newsletter", triggered=True)


def test_group_replies_quote_the_triggering_message_only():
    policy = WhatsAppHumanBehaviorPolicy()

    assert policy.should_quote_reply(chat_type="group", chat_id="120@g.us", message_id="ABC")
    assert not policy.should_quote_reply(chat_type="dm", chat_id="1@s.whatsapp.net", message_id="ABC")
    assert not policy.should_quote_reply(chat_type="group", chat_id="120@g.us", message_id=None)


class _HookAdapter(WhatsAppAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True))
        self.calls = []

    async def _bridge_request(self, method, path, *, json_payload=None, timeout_seconds=30):
        self.calls.append((method, path, json_payload))
        return {"success": True}


class _DigestAdapter(_HookAdapter):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    async def _bridge_request(self, method, path, *, json_payload=None, timeout_seconds=30):
        self.calls.append((method, path, json_payload))
        if method == "GET" and path.startswith("/history?"):
            return {"messages": self.messages}
        return {"success": True}


def test_processing_hooks_default_to_read_receipts_without_success_reactions():
    adapter = _HookAdapter()
    source = SessionSource(platform=Platform.WHATSAPP, chat_id="120@g.us", chat_type="group", user_id="u")
    event = MessageEvent(
        text="oi",
        message_type=MessageType.TEXT,
        source=source,
        message_id="MSG1",
        raw_message={"senderId": "5511999999999@s.whatsapp.net"},
    )

    async def _run():
        await adapter.on_processing_start(event)
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    asyncio.run(_run())

    assert ("POST", "/read", {"chatId": "120@g.us", "messageId": "MSG1", "participant": "5511999999999@s.whatsapp.net"}) in adapter.calls
    assert not [call for call in adapter.calls if call[1] == "/react"]


def test_processing_hooks_react_only_on_real_alert_by_default():
    adapter = _HookAdapter()
    source = SessionSource(platform=Platform.WHATSAPP, chat_id="120@g.us", chat_type="group", user_id="u")
    event = MessageEvent(
        text="oi",
        message_type=MessageType.TEXT,
        source=source,
        message_id="MSG1",
        raw_message={"senderId": "5511999999999@s.whatsapp.net"},
    )

    async def _run():
        await adapter.on_processing_start(event)
        await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    asyncio.run(_run())

    assert ("POST", "/react", {"chatId": "120@g.us", "messageId": "MSG1", "emoji": "⚠️", "participant": "5511999999999@s.whatsapp.net"}) in adapter.calls


def test_reaction_mode_all_preserves_legacy_lifecycle_reactions():
    adapter = _HookAdapter()
    adapter._human_policy = WhatsAppHumanBehaviorPolicy(reaction_mode="all")
    source = SessionSource(platform=Platform.WHATSAPP, chat_id="120@g.us", chat_type="group", user_id="u")
    event = MessageEvent(
        text="oi",
        message_type=MessageType.TEXT,
        source=source,
        message_id="MSG1",
        raw_message={"senderId": "5511999999999@s.whatsapp.net"},
    )

    async def _run():
        await adapter.on_processing_start(event)
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    asyncio.run(_run())

    assert ("POST", "/react", {"chatId": "120@g.us", "messageId": "MSG1", "emoji": "👀", "participant": "5511999999999@s.whatsapp.net"}) in adapter.calls
    assert ("POST", "/react", {"chatId": "120@g.us", "messageId": "MSG1", "emoji": "✅", "participant": "5511999999999@s.whatsapp.net"}) in adapter.calls


def test_context_digest_is_scoped_by_chat_and_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    adapter = _DigestAdapter([
        {
            "chatId": "120@g.us",
            "senderName": "Felipe",
            "body": "vamos vender pelo WhatsApp https://x.test?a=token",
        },
        {
            "chatId": "999@g.us",
            "senderName": "Outro",
            "body": "não pode entrar",
        },
        {
            "chatId": "120@g.us",
            "senderName": "5511999999999@s.whatsapp.net",
            "body": "meu telefone é +55 11 99999-9999",
        },
    ])

    asyncio.run(adapter._update_context_digest("120@g.us", "group"))
    prompt = adapter._render_context_digest_prompt("120@g.us")
    other_prompt = adapter._render_context_digest_prompt("999@g.us")

    assert "vender pelo WhatsApp" in prompt
    assert "[link]" in prompt
    assert "[telefone]" in prompt
    assert "não pode entrar" not in prompt
    assert other_prompt == ""


def test_processing_complete_updates_digest_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    adapter = _DigestAdapter([
        {"chatId": "120@g.us", "senderName": "Saraiva", "body": "continua o plano"},
    ])
    source = SessionSource(platform=Platform.WHATSAPP, chat_id="120@g.us", chat_type="group", user_id="u")
    event = MessageEvent(
        text="continua",
        message_type=MessageType.TEXT,
        source=source,
        message_id="MSG1",
        raw_message={"senderId": "5511999999999@s.whatsapp.net"},
    )

    asyncio.run(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))

    assert "continua o plano" in adapter._render_context_digest_prompt("120@g.us")


def test_whatsapp_keep_typing_stops_typing_after_finite_sequence(monkeypatch):
    adapter = _HookAdapter()
    adapter._running = True
    adapter._http_session = SimpleNamespace()
    calls = []

    async def fake_presence(chat_id, presence="composing"):
        calls.append(presence)

    async def fast_sleep(_delay):
        return None

    adapter.send_presence = fake_presence
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    stop_event = asyncio.Event()
    stop_event.set()

    asyncio.run(adapter._keep_typing("120@g.us", stop_event=stop_event))

    assert calls[-1] == "unavailable"
    assert calls.count("composing") <= 1

"""WhatsApp-facing gateway status microcopy."""

import pytest

from gateway.status_microcopy import (
    GENERIC_TOOL,
    compact_command_label,
    compact_file_label,
    is_whatsapp_platform,
    render_busy_ack,
    render_gateway_draining,
    render_long_running,
    render_operation_interrupted,
    render_session_reset,
    render_shutdown,
    render_tool_progress,
)


GENERIC_TOOL_VARIANTS = {
    "⚡ Tô nisso.",
    "🕯️ Com calma.",
    "🛠️ Preparando.",
    "📜 Conferindo.",
    "⚙️ Em obra.",
}
MODEL_WAITING_VARIANTS = {
    "🧠 IA pensando.",
    "🕯️ Pensando aqui.",
    "📜 Formando resposta.",
}
LONG_RUNNING_VARIANTS = {
    "⚡ Ainda nisso.",
    "🕯️ Sem pressa.",
    "📜 Discernindo.",
}
VALIDATING_VARIANTS = {
    "⚙️ Validando.",
    "📜 Conferindo.",
    "🕯️ Checando.",
}


class _DummyAdapter:
    def __init__(self):
        self._pending_messages = {}
        self.sent = []

    async def _send_with_retry(self, **kwargs):
        self.sent.append(kwargs)


class _DummyAgent:
    def __init__(self, steer_result=False):
        self.steer_result = steer_result
        self.steered_text = None

    def steer(self, text):
        self.steered_text = text
        return self.steer_result

    def get_activity_summary(self):
        return {
            "api_call_count": 9,
            "max_iterations": 90,
            "current_tool": "terminal",
        }


def test_busy_ack_copy_is_short_ptbr():
    assert render_busy_ack("queue") == "🕓 Na fila."
    assert render_busy_ack("steer") == "↪️ Já vou nisso."
    assert render_busy_ack("interrupt") == "⚡ Trocando rota."


def test_alert_and_reset_copy_is_ptbr():
    assert render_shutdown(restarting=True) == "↻ Reiniciando a ponte."
    assert render_shutdown(restarting=False) == "⚠️ Vai cortar aqui."
    assert render_session_reset("inactive for 24h") == "🧼 Sessão limpa."
    assert render_gateway_draining(restarting=True) == "↻ Reiniciando. Já volto."
    assert render_gateway_draining(restarting=False) == "⚠️ Fechando a ponte."
    assert render_operation_interrupted("Operation interrupted: waiting") == "⚠️ Interrompido na resposta."


def test_long_running_maps_model_wait_to_ptbr():
    assert render_long_running("waiting for non-streaming API response") in MODEL_WAITING_VARIANTS
    assert render_long_running("receiving stream response") in MODEL_WAITING_VARIANTS
    assert render_long_running("executing tool: terminal") in LONG_RUNNING_VARIANTS


def test_tool_progress_humanizes_tool_names():
    assert render_tool_progress("terminal", "ssh host") == "⌨️ Rodando servidor"
    assert render_tool_progress("read_file", "/tmp/a") == "🔎 Lendo a"
    assert render_tool_progress("web_search", "docs") == "🌐 Abrindo docs"
    assert render_tool_progress("patch", "file.py") == "🩹 Editando file.py"
    assert render_tool_progress("write_file", "file.py") == "🩹 Editando file.py"
    assert render_tool_progress("send_message", "whatsapp:x") == "📨 Enviando."
    assert render_tool_progress("delegate_task", "goal") == "↪️ Puxando ajuda."
    assert render_tool_progress("unknown_tool", "raw internals") in GENERIC_TOOL_VARIANTS


def test_tool_progress_extracts_practical_safe_targets():
    assert render_tool_progress("terminal", "cat gateway/run.py") == "🔎 Lendo run.py"
    assert render_tool_progress("apply_patch", "gateway/status_microcopy.py") == "🩹 Editando status_microcopy.py"
    assert render_tool_progress("terminal", "pytest tests/gateway/test_status_microcopy.py") == "⌨️ Rodando pytest"
    assert render_tool_progress("terminal", "python -m py_compile gateway/run.py") == "⌨️ Rodando py_compile"
    assert render_tool_progress("terminal", "curl https://example.com/docs?token=secret") in GENERIC_TOOL_VARIANTS


def test_tool_progress_hides_ugly_or_old_brand_commands():
    assert render_tool_progress("terminal", "hermes gateway status") == "⌨️ Rodando EMPRESA.IA"
    assert render_tool_progress("terminal", "python -m hermes_cli.main gateway status") == "⌨️ Rodando EMPRESA.IA"
    assert render_tool_progress("terminal", "true") in VALIDATING_VARIANTS
    assert render_tool_progress("terminal", "systemctl restart hermes-gateway.service") == "↻ Reiniciando ponte"


def test_tool_progress_sanitizes_private_or_noisy_values():
    msg = render_tool_progress("read_file", "/home/ubuntu/.hermes/hermes-agent/gateway/run.py")
    assert msg == "🔎 Lendo run.py"
    assert "/home/ubuntu" not in msg
    assert ".hermes" not in msg

    secret_msg = render_tool_progress("terminal", "curl https://api.local/path?api_key=abcd1234")
    assert secret_msg in GENERIC_TOOL_VARIANTS
    assert "api_key" not in secret_msg
    assert "abcd1234" not in secret_msg

    group_safe_msg = render_tool_progress(
        "terminal",
        "rg 'mensagem privada do grupo' /home/ubuntu/.hermes/logs/gateway.log 5511999999999@lid",
    )
    assert group_safe_msg == "🔎 Lendo gateway.log"
    assert "mensagem privada" not in group_safe_msg
    assert "5511999999999" not in group_safe_msg
    assert "/home/ubuntu" not in group_safe_msg


def test_compact_helpers_hide_paths_and_keep_safe_command_labels():
    assert compact_file_label("/var/www/app/gateway.log") == "gateway.log"
    assert compact_file_label("/tmp/session_token.txt") is None
    assert compact_command_label("ssh ubuntu@server.example") == "servidor"
    assert compact_command_label("python -m py_compile gateway/run.py") == "py_compile"


def test_normal_microcopy_stays_compact():
    messages = [
        render_busy_ack("queue"),
        render_busy_ack("steer"),
        render_busy_ack("interrupt"),
        render_tool_progress("terminal", "pytest tests/gateway/test_status_microcopy.py"),
        render_tool_progress("read_file", "/home/ubuntu/.hermes/hermes-agent/gateway/run.py"),
        render_tool_progress("web_search", "docs"),
        render_tool_progress("patch", "/home/ubuntu/.hermes/hermes-agent/gateway/status_microcopy.py"),
        render_tool_progress("unknown_tool"),
        render_long_running("waiting for provider response"),
        render_long_running("executing tool: terminal"),
        render_session_reset(),
    ]
    assert all(len(message) <= 35 for message in messages)


def test_whatsapp_platform_detection_accepts_enum_or_string():
    class PlatformLike:
        value = "whatsapp"

    assert is_whatsapp_platform("whatsapp")
    assert is_whatsapp_platform(PlatformLike())
    assert not is_whatsapp_platform("telegram")


async def _run_busy_ack(mode, steer_result=False):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    source = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id="5511999999999@lid",
        chat_type="dm",
        user_id="5511999999999",
    )
    event = MessageEvent(
        text="nova msg",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-1",
    )
    adapter = _DummyAdapter()
    agent = _DummyAgent(steer_result=steer_result)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._draining = False
    runner.adapters = {Platform.WHATSAPP: adapter}
    runner._running_agents = {"session-key": agent}
    runner._running_agents_ts = {"session-key": 1}
    runner._busy_ack_ts = {}
    runner._busy_input_mode = mode

    result = await runner._handle_active_session_busy_message(event, "session-key")
    assert result is True
    return adapter.sent[-1]["content"], agent


@pytest.mark.asyncio
async def test_whatsapp_busy_queue_ack_hides_runtime_telemetry(monkeypatch):
    content, _ = await _run_busy_ack("queue")
    assert content == "🕓 Na fila."
    assert "Queued" not in content
    assert "iteration" not in content
    assert "running:" not in content
    assert "elapsed" not in content


@pytest.mark.asyncio
async def test_whatsapp_busy_steer_ack_is_humanized(monkeypatch):
    content, agent = await _run_busy_ack("steer", steer_result=True)
    assert content == "↪️ Já vou nisso."
    assert agent.steered_text == "nova msg"
    assert "Steered" not in content

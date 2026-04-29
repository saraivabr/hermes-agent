"""Tests for the direct WhatsApp bridge admin tool."""

import json

from tools.whatsapp_admin_tool import _normalize_participants, whatsapp_admin_tool


def test_normalize_participants_accepts_csv_newlines_and_lists():
    assert _normalize_participants("+5511999999999, 120363@g.us\nabc@s.whatsapp.net") == [
        "+5511999999999",
        "120363@g.us",
        "abc@s.whatsapp.net",
    ]
    assert _normalize_participants([" a ", "", 123]) == ["a", "123"]
    assert _normalize_participants(None) == []


def test_history_search_builds_query(monkeypatch):
    calls = []

    def fake_http(method, path, payload=None, timeout=30):
        calls.append((method, path, payload, timeout))
        return {"ok": True}

    monkeypatch.setattr("tools.whatsapp_admin_tool._http_json", fake_http)

    result = json.loads(
        whatsapp_admin_tool(
            {
                "action": "search",
                "q": "bridge",
                "chatId": "120363@g.us",
                "limit": 5,
                "isGroup": True,
            }
        )
    )

    assert result == {"ok": True}
    assert calls == [("GET", "/search?chatId=120363%40g.us&q=bridge&limit=5&isGroup=True", None, 20)]


def test_create_group_requires_subject_and_participants():
    result = json.loads(whatsapp_admin_tool({"action": "create_group", "subject": "Sem gente"}))
    assert "error" in result
    assert "participants" in result["error"]


def test_participants_payload_accepts_single_participant(monkeypatch):
    calls = []

    def fake_http(method, path, payload=None, timeout=30):
        calls.append((method, path, payload, timeout))
        return {"ok": True}

    monkeypatch.setattr("tools.whatsapp_admin_tool._http_json", fake_http)

    result = json.loads(
        whatsapp_admin_tool(
            {
                "action": "participants",
                "chatId": "120363@g.us",
                "participant": "+5511999999999",
                "participantAction": "promote",
                "confirm": True,
            }
        )
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "POST",
            "/groups/participants",
            {"chatId": "120363@g.us", "participants": ["+5511999999999"], "action": "promote"},
            60,
        )
    ]


def test_invite_uses_revoke_flag(monkeypatch):
    calls = []

    def fake_http(method, path, payload=None, timeout=30):
        calls.append((method, path, payload, timeout))
        return {"invite": "https://chat.whatsapp.com/example"}

    monkeypatch.setattr("tools.whatsapp_admin_tool._http_json", fake_http)

    result = json.loads(whatsapp_admin_tool({"action": "invite", "chatId": "120363@g.us", "revoke": True, "confirm": True}))

    assert result["invite"].endswith("example")
    assert calls == [("POST", "/groups/invite", {"chatId": "120363@g.us", "revoke": True}, 30)]


def test_mutating_group_actions_require_confirmation():
    result = json.loads(
        whatsapp_admin_tool(
            {
                "action": "participants",
                "chatId": "120363@g.us",
                "participant": "+5511999999999",
                "participantAction": "remove",
            }
        )
    )

    assert "error" in result
    assert "confirm=true" in result["error"]

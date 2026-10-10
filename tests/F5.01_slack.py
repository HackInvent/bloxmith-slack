"""FB1/FB2/FB3/FB4/FB5/FB6/FB7: pure settings, inputs, provider envelopes, normalization, file policy and hooks.

No socket is opened and no provider is contacted. Temporary directories hold the
file-policy cases.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.slack import logic, transport
from blocs.slack.block import SlackBlock
from bloxsmith_app.block_api import BlockRuntimeContext, BlockRuntimePreparationContext
from slack_fixture import GENERAL, OTHER, RANDOM, REF, TOKEN

BASE = {"enabled": True, "secret_ref": REF, "allowed_channels": [GENERAL, RANDOM], "default_channel": GENERAL}
TS = "1712345678.123456"


def rejected(call, code=None):
    """A contract violation must raise a coded error, never fall back silently."""
    try:
        call()
    except logic.SlackError as exc:
        assert code is None or exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in str(exc)
        return exc
    raise AssertionError("Invalid value accepted")


def settings_contract():
    """# - FB1 - remote access stays off by default, the origin is fixed and every setting is bounded."""
    defaults = logic.configuration({})
    assert defaults["enabled"] is False and defaults["api_base_url"] == "https://slack.com"
    assert defaults["default_channel"] is None and defaults["allowed_channels"] == []
    config = logic.configuration({**BASE, "allowed_channels": [" " + GENERAL, RANDOM], "execution": {"inhibited": False}})
    assert config["allowed_channels"] == [GENERAL, RANDOM] and config["default_channel"] == GENERAL
    frozen = logic.configuration(MappingProxyType({**BASE, "allowed_channels": (GENERAL,)}))
    assert frozen["allowed_channels"] == [GENERAL]
    for raw in ({"enabled": True}, {"enabled": True, "secret_ref": REF}, {**BASE, "secret_ref": ""},
                {**BASE, "secret_ref": TOKEN}, {**BASE, "secret_ref": "env://SLACK"},
                {**BASE, "allowed_channels": ["#general"]}, {**BASE, "allowed_channels": ["c0lower"]},
                {**BASE, "allowed_channels": [GENERAL, GENERAL]}, {**BASE, "allowed_channels": GENERAL},
                {**BASE, "allowed_channels": [1]}, {**BASE, "allowed_channels": ["C" + str(index).zfill(9) for index in range(101)]},
                {**BASE, "default_channel": OTHER}, {**BASE, "default_channel": "general"},
                {**BASE, "enabled": "yes"}, {**BASE, "file_root": "relative/dir"}, {**BASE, "file_root": "/srv/../etc"},
                {**BASE, "ca_file": "ca.pem"}, {**BASE, "token": TOKEN},
                {**BASE, "api_base_url": "http://slack.com"}, {**BASE, "api_base_url": "https://slack.com/api"},
                {**BASE, "api_base_url": "https://example.com"}, {**BASE, "api_base_url": "https://slack.com:8443"},
                {**BASE, "api_base_url": "https://evil.slack.com.example"}, {**BASE, "api_base_url": "https://user:pw@slack.com"},
                {**BASE, "api_base_url": "https://slack.com/?x=1"}, {**BASE, "api_base_url": "ftp://slack.com"},
                {**BASE, "api_base_url": "https://sla ck.com"}, {**BASE, "api_base_url": "http://api.example.com:8080"}):
        error = rejected(lambda raw=raw: logic.configuration(raw), "config")
        assert TOKEN not in json.dumps(error.envelope())
    for key, (low, high) in logic.BOUNDS.items():
        for bad in (low - 1, high + 1, True, float(low), str(low)):
            rejected(lambda key=key, bad=bad: logic.configuration({**BASE, key: bad}), "config")
        assert logic.configuration({**BASE, key: low})[key] == low
        assert logic.configuration({**BASE, key: high})[key] == high
    origins = {"https://slack.com/": "https://slack.com", "https://SLACK.com:443": "https://slack.com",
               "http://127.0.0.1:8081": "http://127.0.0.1:8081", "http://localhost:9": "http://localhost:9",
               "http://[::1]:7": "http://[::1]:7", "https://127.0.0.1:8443": "https://127.0.0.1:8443"}
    for raw, expected in origins.items():
        assert logic.configuration({**BASE, "api_base_url": raw})["api_base_url"] == expected
    assert logic.bot_token(" " + TOKEN + "\n") == TOKEN
    for bad in ("", "xoxp-1234567890-abc", "xoxb-short", "Bearer " + TOKEN, TOKEN + "\r\nX: y", TOKEN + " y", None, 42):
        rejected(lambda bad=bad: logic.bot_token(bad), "secret")
    redacted = logic.redact("token " + TOKEN + " user xoxp-9999999999-secret", TOKEN)
    assert TOKEN not in redacted and "xoxp-9999999999" not in redacted and "***" in redacted


def message_contract():
    """# - FB2 - # - FB3 - one explicit operation for an allowlisted conversation only."""
    config = logic.configuration(BASE)
    plain = logic.message_request("Deploy *finished*\nsecond line", config)
    assert plain["method"] == "chat.postMessage" and plain["operation"] == "post_message"
    assert plain["params"] == {"channel": GENERAL, "text": "Deploy *finished*\nsecond line"}
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Build* ok"}}]
    rich = logic.message_request(json.dumps({"channel": RANDOM, "text": "Build ok", "blocks": blocks, "thread_ts": TS,
                                             "reply_broadcast": True, "unfurl_links": False, "unfurl_media": False,
                                             "mrkdwn": True, "request_id": "order-1"}), config)
    assert rich["params"] == {"channel": RANDOM, "text": "Build ok", "blocks": blocks, "thread_ts": TS,
                              "reply_broadcast": True, "unfurl_links": False, "unfurl_media": False, "mrkdwn": True}
    assert rich["request_id"] == "order-1" and rich["channel"] == RANDOM
    only_blocks = logic.message_request({"blocks": json.dumps(blocks)}, config)
    assert only_blocks["params"] == {"channel": GENERAL, "blocks": blocks}
    update = logic.message_request({"operation": "update_message", "ts": TS, "text": "edited"}, config)
    assert update["method"] == "chat.update" and update["params"] == {"channel": GENERAL, "text": "edited", "ts": TS}
    rejected(lambda: logic.message_request({"text": "hi", "channel": OTHER}, config), "channel_not_allowed")
    rejected(lambda: logic.message_request("hi", logic.configuration({**BASE, "default_channel": ""})), "channel_required")
    for bad in ("   ", "x" * 40001, "a\x00b", '{"text":"x"', '{"text":"x","text":"y"}', '{"text": NaN}',
                {"text": "x", "token": "y"}, {"text": 5}, {"text": "x", "channel": "#general"},
                {"text": "x", "thread_ts": "123"}, {"text": "x", "reply_broadcast": True},
                {"text": "x", "unfurl_links": "false"}, {"text": "x", "request_id": "bad id"},
                {"blocks": []}, {"blocks": [{"text": "no type"}]}, {"blocks": "[1"}, {"blocks": [{"type": "x"}] * 51},
                {"operation": "delete_message", "ts": TS}, {"operation": "update_message", "text": "no ts"},
                {"operation": "update_message", "ts": TS}, {"operation": "update_message", "ts": TS, "text": "x",
                                                            "thread_ts": TS},
                b"\xff", 42, "x" * 300000):
        rejected(lambda bad=bad: logic.message_request(bad, config), "invalid_input")
    assert logic.message_request("x" * 40000, config)["params"]["text"] == "x" * 40000
    assert logic.message_request("not {json", config)["params"]["text"] == "not {json"
    assert logic.message_request('["only", "text"]', config)["params"]["text"] == '["only", "text"]'
    # Request bodies are bounded independently of the input limit (form encoding can expand text).
    rejected(lambda: logic.request_body("chat.postMessage", {"channel": GENERAL, "text": "x" * 300000}), "request_too_large")
    rejected(lambda: logic.request_body("files.completeUploadExternal", {"initial_comment": "é" * 50000}), "request_too_large")
    history = logic.message_request({"operation": "read_history", "oldest": 1712000000, "latest": "1712345678.5",
                                     "inclusive": True, "limit": 30, "request_id": "r-2"}, config)
    assert history["method"] == "conversations.history" and history["limit"] == 30 and history["cursor"] is None
    assert history["params"] == {"channel": GENERAL, "oldest": "1712000000", "latest": "1712345678.5", "inclusive": True}
    assert logic.message_request({"operation": "read_history"}, config)["limit"] == config["max_messages"]
    replies = logic.message_request({"operation": "read_replies", "channel": RANDOM, "thread_ts": TS,
                                     "cursor": "bmV4dF90czoxNTEy"}, config)
    assert replies["method"] == "conversations.replies" and replies["params"] == {"channel": RANDOM, "ts": TS}
    assert replies["cursor"] == "bmV4dF90czoxNTEy"
    for bad in ({"operation": "read_history", "limit": 0}, {"operation": "read_history", "limit": 201},
                {"operation": "read_history", "limit": "5"}, {"operation": "read_history", "oldest": "yesterday"},
                {"operation": "read_history", "oldest": -1}, {"operation": "read_history", "cursor": "a b"},
                {"operation": "read_history", "inclusive": 1}, {"operation": "read_history", "text": "x"},
                {"operation": "read_replies"}, {"operation": "read_replies", "ts": TS, "thread_ts": TS}):
        rejected(lambda bad=bad: logic.message_request(bad, config), "invalid_input")
    rejected(lambda: logic.message_request({"operation": "read_history", "channel": OTHER}, config), "channel_not_allowed")
    assert logic.request_body("chat.postMessage", {"channel": GENERAL, "text": "é"}) == '{"channel":"C0FIXTURE01","text":"é"}'.encode()
    assert logic.request_body("conversations.history", {"channel": GENERAL, "inclusive": True, "limit": 2}) == \
        b"channel=C0FIXTURE01&inclusive=true&limit=2"
    assert logic.request_body("files.completeUploadExternal", {"files": [{"id": "F1X", "title": "Q3 ü"}]}) == \
        b"files=%5B%7B%22id%22%3A%22F1X%22%2C%22title%22%3A%22Q3+%C3%BC%22%7D%5D"
    assert logic.content_type("chat.update") == "application/json; charset=utf-8"
    assert logic.content_type("conversations.replies") == "application/x-www-form-urlencoded"


def file_contract():
    """# - FB4 - one explicit upload request for an allowlisted conversation only."""
    config = logic.configuration(BASE)
    upload = logic.file_request("reports/q3.pdf", config)
    assert upload["path"] == "reports/q3.pdf" and upload["channel"] == GENERAL and upload["operation"] == "upload_file"
    assert upload["filename"] is None and upload["title"] is None and upload["thread_ts"] is None
    upload = logic.file_request({"path": "a.png", "title": "Q3 chart", "filename": "résumé.png", "channel": RANDOM,
                                 "initial_comment": "Here it is", "thread_ts": TS, "alt_text": "A chart",
                                 "request_id": "f-1"}, config)
    assert (upload["title"], upload["filename"], upload["channel"], upload["initial_comment"], upload["thread_ts"],
            upload["alt_text"], upload["request_id"]) == ("Q3 chart", "résumé.png", RANDOM, "Here it is", TS, "A chart", "f-1")
    for bad in ({"path": ""}, {"path": "a\nb"}, {"path": "a", "title": "x" * 256}, {"path": "a", "filename": "../x"},
                {"path": "a", "filename": "a\\b"}, {"path": "a", "filename": "."}, {"path": "a", "extra": 1},
                {"title": "x"}, {"path": "a", "initial_comment": "x" * 4001}, {"path": "a", "thread_ts": "1"},
                {"path": "a", "alt_text": ""}):
        rejected(lambda bad=bad: logic.file_request(bad, config), "invalid_input")
    rejected(lambda: logic.file_request({"path": "a", "channel": OTHER}, config), "channel_not_allowed")
    official = logic.configuration(BASE)
    assert logic.upload_target("https://files.slack.com/upload/v1/CwAB?x=1", official) == {
        "origin": "https://files.slack.com", "path": "/upload/v1/CwAB?x=1"}
    for bad in ("http://files.slack.com/upload/v1/x", "https://files.slack.com.evil.example/upload",
                "https://evil.example/upload/v1/x", "https://files.slack.com:8443/x", "https://u:p@files.slack.com/x",
                "https://files.slack.com/x#frag", "https://files.slack.com/a b", 7, ""):
        rejected(lambda bad=bad: logic.upload_target(bad, official), "protocol")
    loopback = logic.configuration({**BASE, "api_base_url": "http://127.0.0.1:8123"})
    assert logic.upload_target("http://127.0.0.1:8123/upload/v1/k", loopback)["origin"] == "http://127.0.0.1:8123"
    for bad in ("http://127.0.0.1:8124/upload/v1/k", "https://files.slack.com/upload/v1/k", "http://localhost:8123/u"):
        rejected(lambda bad=bad: logic.upload_target(bad, loopback), "protocol")


def provider_contract():
    """# - FB5 - documented error envelopes map to stable codes, bounded fields and no token."""
    ok = {"ok": True, "channel": GENERAL, "ts": TS}
    assert logic.provider_result({"http_status": 200, "body": ok}) == ok
    cases = {"invalid_auth": "unauthorized", "not_authed": "unauthorized", "token_revoked": "unauthorized",
             "not_in_channel": "forbidden", "is_archived": "forbidden", "cant_update_message": "forbidden",
             "channel_not_found": "not_found", "message_not_found": "not_found", "thread_not_found": "not_found",
             "msg_too_long": "bad_request", "invalid_blocks": "bad_request", "invalid_cursor": "bad_request",
             "no_text": "bad_request", "ratelimited": "rate_limited", "fatal_error": "provider_unavailable",
             "internal_error": "provider_unavailable", "some_new_error": "provider_error"}
    for name, code in cases.items():
        error = rejected(lambda name=name: logic.provider_result({"http_status": 200, "body": {"ok": False, "error": name}},
                                                                 TOKEN, mutation=True), code)
        assert error.slack_error == name and error.http_status == 200
        assert error.uncertain == (code == "provider_unavailable")
    scope = rejected(lambda: logic.provider_result({"http_status": 200, "body": {
        "ok": False, "error": "missing_scope", "needed": "channels:history", "provided": "chat:write"}}), "forbidden")
    assert scope.envelope()["needed_scope"] == "channels:history" and "provided" not in scope.envelope()
    limited = rejected(lambda: logic.provider_result({"http_status": 429, "retry_after_header": 7,
                                                      "body": {"ok": False, "error": "ratelimited"}}), "rate_limited")
    assert limited.retry_after == 7 and limited.envelope()["retry_after"] == 7 and not limited.uncertain
    missing = rejected(lambda: logic.provider_result({"http_status": 404, "body": {"ok": False, "error": "unknown_method"}}),
                       "not_found")
    assert missing.http_status == 404 and missing.slack_error == "unknown_method"
    leaky = rejected(lambda: logic.provider_result({"http_status": 200, "body": {"ok": False, "error": "bad " + TOKEN}}),
                     "provider_error")
    assert leaky.slack_error is None and TOKEN not in json.dumps(leaky.envelope())
    assert not rejected(lambda: logic.provider_result({"http_status": 500, "body": {"ok": False, "error": "fatal_error"}},
                                                      mutation=False), "provider_unavailable").uncertain
    rejected(lambda: logic.provider_result({"http_status": 502, "body": None}, mutation=True), "provider_unavailable")
    rejected(lambda: logic.provider_result({"http_status": 200, "body": {"channel": GENERAL}}), "protocol")
    rejected(lambda: logic.provider_result({"http_status": "200", "body": {}}), "protocol")
    rejected(lambda: logic.provider_result({"http_status": 302, "body": {"ok": True}}), "protocol")
    # Worker outcomes keep the "nothing sent" versus "uncertain" distinction.
    for outcome, code, uncertain in (({"status": "error", "code": "timeout", "phase": "receive"}, "timeout", True),
                                     ({"status": "error", "code": "timeout", "phase": "send"}, "timeout", False),
                                     ({"status": "error", "code": "network", "phase": "connect"}, "network", False),
                                     ({"status": "error", "code": "network", "phase": "receive"}, "network", True),
                                     ({"status": "error", "code": "tls", "phase": "connect"}, "tls", False),
                                     ({"status": "error", "code": "file_changed", "phase": "send"}, "file_changed", False),
                                     ({"status": "error", "code": "file_empty", "phase": "connect"}, "file_empty", False),
                                     ({"status": "response", "http_status": 200, "body_error": "too_large"}, "protocol", True),
                                     ({"status": "response", "http_status": 503, "body_error": "not_json"}, "provider_unavailable", True),
                                     ({"status": "response", "http_status": 429, "retry_after_header": 4, "body_error": "not_json"}, "rate_limited", False),
                                     ({"status": "response", "http_status": 401, "body_error": "not_json"}, "unauthorized", False),
                                     ({"status": "bogus"}, "protocol", False)):
        error = rejected(lambda outcome=outcome: transport.interpret(outcome, TOKEN, mutation=True), code)
        assert error.uncertain is uncertain, (outcome, error.uncertain)
    assert not rejected(lambda: transport.interpret({"status": "error", "code": "network", "phase": "receive"}, TOKEN,
                                                    mutation=False), "network").uncertain
    config = logic.configuration({**BASE, "max_retries": 2, "max_retry_wait_sec": 10})
    wait = logic.SlackError("rate_limited", retry_after=3)
    assert logic.retry_delay(wait, 1, config, 30) == 3.0 and logic.retry_delay(wait, 2, config, 30) == 3.0
    assert logic.retry_delay(wait, 3, config, 30) is None and logic.retry_delay(wait, 1, config, 3.5) is None
    assert logic.retry_delay(logic.SlackError("rate_limited", retry_after=11), 1, config, 60) is None
    assert logic.retry_delay(logic.SlackError("rate_limited"), 1, config, 60) == 1.0
    assert logic.retry_delay(logic.SlackError("provider_unavailable"), 1, config, 60) is None
    assert logic.retry_delay(wait, 1, logic.configuration({**BASE, "max_retries": 0}), 60) is None


def normalize_contract():
    """# - FB3 - # - FB4 - normalized messages and receipts keep IDs and public fields only."""
    raw = {"type": "message", "user": "U0PERSON001", "text": "line 1\nline 2", "ts": TS, "thread_ts": TS,
           "reply_count": 2, "reply_users_count": 1, "latest_reply": "1712345999.000001", "team": "T1",
           "client_msg_id": "c1", "user_profile": {"real_name": "Ada Lovelace", "image_72": "https://example.invalid/a"},
           "edited": {"user": "U0PERSON001", "ts": "1712346000.000000"},
           "files": [{"id": "F0FILE0001", "name": "plan.pdf", "title": "Plan", "mimetype": "application/pdf",
                      "size": 12, "url_private": "https://files.slack.com/private"}, {"id": "bad"}],
           "reactions": [{"name": "eyes", "count": 2, "users": ["U1", "U2"]}], "blocks": [{"type": "rich_text"}]}
    message = logic.normalize_message(raw)
    assert message == {"ts": TS, "date": "2024-04-05T19:34:38Z", "type": "message", "subtype": None,
                       "user": "U0PERSON001", "bot_id": None, "text": "line 1\nline 2", "thread_ts": TS,
                       "reply_count": 2, "reply_users_count": 1, "latest_reply": "1712345999.000001",
                       "edited": {"user": "U0PERSON001", "ts": "1712346000.000000"},
                       "files": [{"id": "F0FILE0001", "name": "plan.pdf", "title": "Plan", "mimetype": "application/pdf",
                                  "size": 12}],
                       "reactions": [{"name": "eyes", "count": 2}], "has_blocks": True}
    text = json.dumps(message)
    assert "Ada" not in text and "url_private" not in text and "client_msg_id" not in text and "U2" not in text
    assert logic.normalize_message({"ts": "bad"}) is None and logic.normalize_message(None) is None
    messages, more, cursor = logic.page_messages({"messages": [raw, {"ts": 1}], "has_more": True,
                                                  "response_metadata": {"next_cursor": "bmV4dA=="}})
    assert len(messages) == 1 and more is True and cursor == "bmV4dA=="
    assert logic.page_messages({"messages": [], "response_metadata": {"next_cursor": ""}}) == ([], False, None)
    rejected(lambda: logic.page_messages({"ok": True}), "protocol")
    request = {"operation": "read_replies", "request_id": "r", "channel": GENERAL, "params": {"ts": TS}}
    read = logic.normalize_read(request, messages, True, "bmV4dA==", 2, 3)
    assert read == {"request_id": "r", "operation": "read_replies", "channel": GENERAL, "count": 1,
                    "messages": messages, "has_more": True, "next_cursor": "bmV4dA==", "pages": 2, "attempts": 3,
                    "thread_ts": TS}
    assert logic.normalize_read({**request, "operation": "read_history"}, [], False, None, 1, 1)["has_more"] is False
    sent = logic.normalize_sent({"ok": True, "channel": GENERAL, "ts": TS, "message": {"bot_id": "B0BOT00001",
                                 "thread_ts": "1712000000.000001", "text": "secret content"}},
                                {"operation": "post_message", "request_id": "r1", "params": {}}, 2)
    assert sent == {"request_id": "r1", "operation": "post_message", "channel": GENERAL, "ts": TS,
                    "thread_ts": "1712000000.000001", "date": "2024-04-05T19:34:38Z", "attempts": 2, "bot_id": "B0BOT00001"}
    error = rejected(lambda: logic.normalize_sent({"ok": True}, {"operation": "post_message", "params": {}}, 1), "protocol")
    assert error.uncertain
    assert logic.upload_slot({"ok": True, "upload_url": "https://files.slack.com/u", "file_id": "F0ABC12345"}) == (
        "https://files.slack.com/u", "F0ABC12345")
    rejected(lambda: logic.upload_slot({"ok": True, "upload_url": "x", "file_id": "nope"}), "protocol")
    upload_request = {"operation": "upload_file", "request_id": None, "channel": GENERAL, "thread_ts": None}
    shared = logic.normalize_upload({"ok": True, "files": [{"id": "F0ABC12345", "title": "Q3", "shares": {
        "private": {GENERAL: [{"ts": TS}]}}}]}, upload_request, "F0ABC12345", "q3.pdf", 10, 3)
    assert shared["ts"] == TS and shared["file"] == {"id": "F0ABC12345", "title": "Q3", "name": "q3.pdf", "size": 10}
    pending = logic.normalize_upload({"ok": True, "files": [{"id": "F0ABC12345"}]}, upload_request, "F0ABC12345", "a", 1, 3)
    assert pending["ts"] is None and pending["date"] is None and pending["attempts"] == 3
    assert rejected(lambda: logic.normalize_upload({"ok": True, "files": []}, upload_request, "F0ABC12345", "a", 1, 3),
                    "protocol").uncertain


def file_policy_contract():
    """# - FB4 - only single-link, non-empty regular files inside file_root can be uploaded."""
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        root, outside = base / "share", base / "private"
        (root / "reports").mkdir(parents=True)
        outside.mkdir()
        (root / "reports" / "q3.pdf").write_bytes(b"%PDF-1.4 fixture")
        (root / "notes.txt").write_bytes(b"notes")
        (root / "empty.txt").write_bytes(b"")
        (outside / "secret.txt").write_text("private")
        (root / "link.txt").symlink_to(outside / "secret.txt")
        (root / "linked-dir").symlink_to(outside)
        os.link(root / "notes.txt", root / "hardlink.txt")
        os.mkfifo(root / "pipe")
        (root / "big.bin").write_bytes(b"x" * 4096)
        config = logic.configuration({**BASE, "file_root": str(root), "max_file_bytes": 2048})
        found = transport.locate("reports/q3.pdf", config)
        assert found["parts"] == ["reports", "q3.pdf"] and found["size"] == 16 and found["name"] == "q3.pdf"
        assert transport.locate(str(root / "reports" / "q3.pdf"), config)["parts"] == ["reports", "q3.pdf"]
        for path, code in (("../private/secret.txt", "file_not_allowed"), (str(outside / "secret.txt"), "file_not_allowed"),
                           ("link.txt", "file_not_allowed"), ("linked-dir/secret.txt", "file_not_allowed"),
                           ("hardlink.txt", "file_not_allowed"), ("pipe", "file_not_allowed"), ("reports", "file_not_allowed"),
                           ("missing.pdf", "file_not_found"), ("big.bin", "file_too_large"), ("empty.txt", "file_empty"),
                           (".", "file_not_allowed")):
            rejected(lambda path=path: transport.locate(path, config), code)
        rejected(lambda: transport.locate("a.pdf", logic.configuration(BASE)), "file_root_required")
        rejected(lambda: transport.locate("a.pdf", logic.configuration({**BASE, "file_root": str(base / "missing")})), "file_root_required")
        rejected(lambda: transport.locate("etc/hostname", logic.configuration({**BASE, "file_root": "/"})), "file_root_required")


def block_contract():
    """# - FB5 - # - FB6 - # - FB7 - block hooks fail before side effects and keep surfaces bounded."""
    block = SlackBlock()
    ports_in = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["inputs"])
    ports_out = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["outputs"])
    assert [port.name for port in ports_out] == ["messages", "sent", "error"]
    assert block.model["config"] == logic.DEFAULTS and block.model["version"] == "0.1.0"
    calls = []

    def resolver(ref):
        calls.append(ref)
        return "not a token " + TOKEN

    def run(config, inputs):
        context = BlockRuntimeContext(config={**block.default_config(), **config}, inputs=inputs, input_ports=ports_in,
                                      output_ports=ports_out, services={"resolve_secret": resolver})
        result = block.execute_runtime(context)
        assert result.status in ("success", "skipped"), result
        return result, {output.port_name: json.loads(output.value) for output in result.outputs}

    assert run(BASE, {})[0].status == "skipped"
    for config, inputs, code in (({**BASE, "enabled": False}, {"message": "hi"}, "disabled"),
                                 (BASE, {"message": "hi", "file": "a.pdf"}, "one_operation"),
                                 (BASE, {"message": {"text": "hi", "channel": OTHER}}, "channel_not_allowed"),
                                 (BASE, {"message": {"operation": "read_history", "channel": OTHER}}, "channel_not_allowed"),
                                 (BASE, {"file": "a.pdf"}, "file_root_required"),
                                 (BASE, {"message": '{"text":'}, "invalid_input"),
                                 ({**BASE, "default_channel": ""}, {"message": "hi"}, "channel_required")):
        result, outputs = run(config, inputs)
        assert set(outputs) == {"error"} and outputs["error"]["code"] == code, outputs
        assert result.metadata["slack"]["code"] == code and result.metadata["slack"]["status"] == "error"
    assert not calls, "The wallet must not be read before input and policy validation"
    result, outputs = run(BASE, {"message": {"text": "hi", "request_id": "req-1"}})
    assert outputs["error"]["code"] == "secret" and outputs["error"]["request_id"] == "req-1" and calls == [REF]
    assert outputs["error"]["channel"] == GENERAL and outputs["error"]["operation"] == "post_message"
    assert TOKEN not in json.dumps(outputs) and TOKEN not in " ".join(result.logs)
    assert result.logs == ["[slack-error] post_message: secret"]

    def prepared(config, mode):
        context = BlockRuntimePreparationContext.from_context(SimpleNamespace(config=config, runtime_mode=mode))
        return block.prepare_runtime(context)

    assert prepared(BASE, "zeromq_active").listen_on_run is False
    rejected(lambda: prepared({**BASE, "secret_ref": TOKEN}, "zeromq_active"), "config")
    node = block.build_node_payload(node_id="slack", position={"x": 0, "y": 0})
    accepted = block.handle_ui_action(node=node, action="save_settings", values={"node_patch": {"config": BASE}})
    operation = accepted["graph_operations"][-1]
    assert operation["op"] == "update_node_config" and operation["replace"] is True
    assert operation["config"]["allowed_channels"] == BASE["allowed_channels"]
    refused = block.handle_ui_action(node=node, action="save_settings",
                                     values={"node_patch": {"config": {**BASE, "secret_ref": TOKEN}}})
    assert "secret_ref" in refused["error"] and TOKEN not in refused["error"] and "graph_operations" not in refused
    html = block.render_modal(node=node)["html"]
    assert 'data-block-config-field="secret_ref"' in html and 'data-block-config-field="allowed_channels"' in html
    assert 'data-block-value-type="json"' in html and "{{" not in html
    assert 'data-i18n="block.slack.label_max_messages"' in html and "<details class=\"owned-advanced\">" in html
    enabled = {**node, "config": {**node["config"], **BASE}}
    assert 'data-block-config-field="api_base_url"' in block.render_inspector_panel(node=enabled)["html"]
    assert block.render_node_card(node=node)["html"].count("Remote access off") == 1
    assert "Enabled · 2 channels" in block.render_node_card(node=enabled)["html"]
    single = {**node, "config": {**node["config"], **BASE, "allowed_channels": [GENERAL]}}
    assert "Enabled · 1 channel<" in block.render_node_card(node=single)["html"]
    # The preview keeps its catalog key and count so the editor can show it in the interface language.
    assert 'data-i18n="block.slack.card_disabled" data-i18n-params=""' in block.render_node_card(node=node)["html"]
    assert ('data-i18n="block.slack.card_enabled" data-i18n-params="{&quot;count&quot;: 2}"'
            in block.render_node_card(node=enabled)["html"])
    assert REF not in block.render_node_card(node=enabled)["html"] and GENERAL not in block.render_node_card(node=enabled)["html"]


def main():
    settings_contract()
    message_contract()
    file_contract()
    provider_contract()
    normalize_contract()
    file_policy_contract()
    block_contract()
    print("[ok] Slack settings, inputs, provider envelopes, normalization, file policy and hooks")


if __name__ == "__main__":
    main()

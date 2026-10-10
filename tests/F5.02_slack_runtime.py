"""FB2/FB3/FB4/FB5/FB6: loopback Web API protocol, pagination, uploads, Stop, and both runtimes in every package origin.

Every provider exchange targets the loopback double in slack_fixture.py. The
wallet and its fixture token exist only in disposable test storage.
"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.slack import logic, transport
from blocs.slack.block import SlackBlock
from block_test_packages import install_test_package, prepare_release_run, surface_payload
from slack_fixture import (BOT_ID, GENERAL, MISSING, OTHER, OUTSIDER, PARENT, RANDOM, REF, TOKEN, cursor_for, settings,
                           slack_api)
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload, http_json,
                             isolated_server, stop_run_api, text_node, wait_for_run_predicate, wait_for_run_terminal)


def failure(call, code):
    try:
        call()
    except logic.SlackError as exc:
        assert exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in json.dumps(exc.envelope())
        return exc
    raise AssertionError("Expected " + code)


class Tracking(transport.WorkerCall):
    """Record every owned network child so tests can prove it was reaped."""
    created = []

    def __init__(self, job):
        super().__init__(job)
        Tracking.created.append(self)


def calls(api, method):
    return [call for call in api.calls if call["method"] == method]


def post_protocol():
    """# - FB2 - # - FB5 - chat.postMessage/chat.update, coded errors, 429 retries, cancellation and TLS."""
    with slack_api() as api:
        config = logic.configuration(settings(api, max_retry_wait_sec=2))
        sent = transport.post(config, TOKEN, logic.message_request({"text": "Héllo *team*", "request_id": "r-1",
                                                                    "unfurl_links": False}, config))
        call = api.calls[-1]
        assert sent["channel"] == GENERAL and sent["request_id"] == "r-1" and sent["attempts"] == 1 and sent["bot_id"] == BOT_ID
        assert call["encoding"] == "json" and call["authorization"] == "Bearer " + TOKEN
        assert call["params"] == {"channel": GENERAL, "text": "Héllo *team*", "unfurl_links": False}
        assert call["accept_encoding"] == "identity" and api.history[GENERAL][-1]["ts"] == sent["ts"]
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Reply*"}}]
        reply = transport.post(config, TOKEN, logic.message_request(
            {"channel": GENERAL, "text": "fallback", "blocks": blocks, "thread_ts": PARENT}, config))
        assert reply["thread_ts"] == PARENT and api.calls[-1]["params"]["blocks"] == blocks
        edited = transport.post(config, TOKEN, logic.message_request(
            {"operation": "update_message", "ts": sent["ts"], "text": "Edited"}, config))
        assert edited["operation"] == "update_message" and edited["ts"] == sent["ts"]
        original = next(item for item in api.history[GENERAL] if item["ts"] == sent["ts"])
        assert api.calls[-1]["method"] == "chat.update" and original["text"] == "Edited" and original["edited"]["user"]
        text = logic.message_request("ping", config)
        for request, code, slack_error in ((logic.message_request({"text": "x", "channel": OUTSIDER}, config), "forbidden", "not_in_channel"),
                                           (logic.message_request({"text": "x", "channel": MISSING}, config), "not_found", "channel_not_found"),
                                           (logic.message_request({"operation": "update_message", "ts": PARENT, "text": "x"}, config), "forbidden", "cant_update_message"),
                                           (logic.message_request({"operation": "update_message", "ts": "1.000001", "text": "x"}, config), "not_found", "message_not_found")):
            error = failure(lambda request=request: transport.post(config, TOKEN, request), code)
            assert error.slack_error == slack_error and not error.uncertain and error.attempts == 1
        unauthorized = failure(lambda: transport.post(config, "xoxb-0000000000-wrong-token", text), "unauthorized")
        assert unauthorized.slack_error == "invalid_auth" and calls(api, "chat.postMessage")[-1]["token_ok"] is False
        api.script["chat.postMessage"] = [{"status": 200, "body": {"ok": False, "error": "missing_scope",
                                                                   "needed": "chat:write", "provided": "channels:read"}}]
        assert failure(lambda: transport.post(config, TOKEN, text), "forbidden").envelope()["needed_scope"] == "chat:write"
        limited = {"status": 429, "body": {"ok": False, "error": "ratelimited"}, "headers": {"Retry-After": "1"}}
        api.script["chat.postMessage"] = [limited]
        started, before = time.monotonic(), len(api.calls)
        assert transport.post(config, TOKEN, text)["attempts"] == 2 and time.monotonic() - started >= 1
        assert len(api.calls) == before + 2
        api.script["chat.postMessage"] = [{**limited, "headers": {"Retry-After": "30"}}]
        too_long = failure(lambda: transport.post(config, TOKEN, text), "rate_limited")
        assert too_long.retry_after == 30 and too_long.attempts == 1 and not too_long.uncertain
        api.script["chat.postMessage"] = [limited] * 3
        exhausted = failure(lambda: transport.post(logic.configuration({**settings(api), "max_retries": 0}), TOKEN, text),
                            "rate_limited")
        assert exhausted.attempts == 1
        api.script["chat.postMessage"] = []
        for scripted, code, uncertain in (({"status": 404, "body": {"ok": False, "error": "unknown_method"}}, "not_found", False),
                                          ({"status": 200, "body": {"ok": False, "error": "fatal_error"}}, "provider_unavailable", True),
                                          ({"status": 500, "body": {"ok": False, "error": "internal_error"}}, "provider_unavailable", True),
                                          ("html502", "provider_unavailable", True), ("oversized", "protocol", True),
                                          ("lost", "network", True)):
            api.script["chat.postMessage"] = [scripted]
            before = len(api.calls)
            error = failure(lambda: transport.post(logic.configuration({**settings(api), "max_response_bytes": 65536}), TOKEN, text), code)
            assert error.uncertain is uncertain and len(api.calls) == before + 1, (scripted, error.uncertain)
        Tracking.created.clear()
        with patch.object(transport, "WorkerCall", Tracking):
            api.script["chat.postMessage"] = ["hold"]
            api.release.clear()
            started = time.monotonic()
            cancelled = failure(lambda: transport.post(config, TOKEN, text, cancel=lambda: time.monotonic() - started > 0.4), "cancelled")
            assert cancelled.uncertain and time.monotonic() - started < 2.5
            api.release.set()
            api.script["chat.postMessage"] = ["hold"]
            api.release.clear()
            started = time.monotonic()
            timed = failure(lambda: transport.post(logic.configuration({**settings(api), "timeout_sec": 5}), TOKEN, text), "timeout")
            assert timed.uncertain and 4 <= time.monotonic() - started < 8
            api.release.set()
        assert Tracking.created and all(item.child.poll() is not None for item in Tracking.created)
        closed = logic.configuration({**settings(api), "api_base_url": "http://127.0.0.1:9"})
        assert not failure(lambda: transport.post(closed, TOKEN, text), "network").uncertain
    with slack_api(tls=True) as api:
        config = logic.configuration(settings(api))
        assert transport.post(config, TOKEN, logic.message_request("over TLS", config))["channel"] == GENERAL
        before = len(api.calls)
        untrusted = failure(lambda: transport.post(logic.configuration({**settings(api), "ca_file": ""}), TOKEN,
                                                   logic.message_request("x", config)), "tls")
        assert not untrusted.uncertain and len(api.calls) == before
    print("[ok] Slack post/update protocol, errors, 429 retries, cancellation and TLS", flush=True)


def read_protocol():
    """# - FB3 - # - FB5 - history and replies with bounded cursor pagination, filters and errors."""
    with slack_api() as api:
        config = logic.configuration(settings(api, page_size=2, max_pages=5, max_messages=50))
        result = transport.read(config, TOKEN, logic.message_request({"operation": "read_history", "limit": 5,
                                                                      "request_id": "h-1"}, config))
        pages = calls(api, "conversations.history")
        assert [call["encoding"] for call in pages] == ["form"] * 3 and all(call["token_ok"] for call in pages)
        assert [call["params"].get("limit") for call in pages] == ["2", "2", "1"]
        assert [call["params"].get("cursor") for call in pages] == [None, cursor_for(2), cursor_for(4)]
        assert result["count"] == 5 and result["pages"] == 3 and result["has_more"] is True and result["request_id"] == "h-1"
        assert result["next_cursor"] == cursor_for(5) and result["operation"] == "read_history"
        assert [item["text"].split("\n")[0] for item in result["messages"]] == ["note 6", "note 5", "note 4", "note 3", "note 2"]
        thread = result["messages"][3]
        assert thread["ts"] == PARENT and thread["reply_count"] == 3 and thread["reactions"] == [{"name": "eyes", "count": 2}]
        assert result["messages"][1]["files"] == [{"id": "F0SEEDFILE1", "name": "plan.pdf", "title": "Plan",
                                                   "mimetype": "application/pdf", "filetype": "pdf", "size": 1234}]
        text = json.dumps(result)
        assert "Ada Lovelace" not in text and "url_private" not in text and "client_msg_id" not in text
        rest = transport.read(config, TOKEN, logic.message_request({"operation": "read_history", "cursor": result["next_cursor"]}, config))
        assert [item["text"].split("\n")[0] for item in rest["messages"]] == ["note 1"] and rest["has_more"] is False
        assert rest["next_cursor"] is None and rest["pages"] == 1
        bounded = transport.read(logic.configuration({**settings(api), "page_size": 1, "max_pages": 2}), TOKEN,
                                 logic.message_request({"operation": "read_history"}, config))
        assert bounded["count"] == 2 and bounded["pages"] == 2 and bounded["has_more"] is True
        window = transport.read(config, TOKEN, logic.message_request({"operation": "read_history", "oldest": "1760000002.000200",
                                                                      "latest": 1760000005, "inclusive": True}, config))
        assert [item["ts"] for item in window["messages"]] == ["1760000004.000400", PARENT, "1760000002.000200"]
        assert calls(api, "conversations.history")[-1]["params"]["inclusive"] == "true"
        replies = transport.read(config, TOKEN, logic.message_request({"operation": "read_replies", "ts": PARENT}, config))
        assert replies["thread_ts"] == PARENT and replies["pages"] == 3
        assert [item["ts"] for item in replies["messages"]] == [PARENT, "1760000011.000001", "1760000012.000002", "1760000013.000003"]
        assert all(item["thread_ts"] == PARENT for item in replies["messages"]) and replies["has_more"] is False
        assert replies["messages"][1]["parent_user_id"] == "U0PERSON001"
        api.script["conversations.history"] = [{"status": 429, "body": {"ok": False, "error": "ratelimited"},
                                                "headers": {"Retry-After": "1"}}]
        retried = transport.read(config, TOKEN, logic.message_request({"operation": "read_history", "limit": 2,
                                                                       "channel": RANDOM}, config))
        assert retried["attempts"] == 2 and retried["count"] == 1 and retried["messages"][0]["bot_id"] == BOT_ID
        # A page failure after earlier pages returns an error, never a silently truncated read.
        api.script["conversations.history"] = [{"status": 200, "body": {"ok": True, "messages": [], "has_more": True,
                                                                        "response_metadata": {"next_cursor": "bogus-cursor"}}}]
        assert failure(lambda: transport.read(config, TOKEN, logic.message_request({"operation": "read_history"}, config)),
                       "bad_request").slack_error == "invalid_cursor"
        for request, code, slack_error in (({"operation": "read_replies", "ts": "1.000001"}, "not_found", "thread_not_found"),
                                           ({"operation": "read_history", "channel": MISSING}, "not_found", "channel_not_found"),
                                           ({"operation": "read_history", "cursor": "bogus"}, "bad_request", "invalid_cursor")):
            error = failure(lambda request=request: transport.read(config, TOKEN, logic.message_request(request, config)), code)
            assert error.slack_error == slack_error and not error.uncertain
        api.script["conversations.history"] = [{"status": 200, "body": {"ok": True, "has_more": False}}]
        failure(lambda: transport.read(config, TOKEN, logic.message_request({"operation": "read_history"}, config)), "protocol")
        api.script["conversations.history"] = ["lost"]
        assert not failure(lambda: transport.read(config, TOKEN, logic.message_request({"operation": "read_history"}, config)),
                           "network").uncertain
        # Cancelling a read is never reported as a possible change in Slack.
        api.script["conversations.history"] = ["hold"]
        api.release.clear()
        started = time.monotonic()
        stopped = failure(lambda: transport.read(config, TOKEN, logic.message_request({"operation": "read_history"}, config),
                                                 cancel=lambda: time.monotonic() - started > 0.4), "cancelled")
        assert not stopped.uncertain and "nothing was changed" in str(stopped) and time.monotonic() - started < 2.5
        api.release.set()
    print("[ok] Slack history/replies pagination, filters, retries and errors", flush=True)


def upload_protocol():
    """# - FB4 - # - FB5 - three-step external upload; the token never reaches the upload URL."""
    with slack_api() as api, tempfile.TemporaryDirectory() as folder:
        share = Path(folder) / "share"
        share.mkdir()
        payload = b"%PDF-1.4\n" + bytes(range(256)) * 40
        (share / "report.pdf").write_bytes(payload)
        config = logic.configuration(settings(api, file_root=str(share)))
        request = logic.file_request({"path": "report.pdf", "title": "Q3 report", "initial_comment": "Here it is",
                                      "thread_ts": PARENT, "alt_text": "Quarterly report", "request_id": "u-1"}, config)
        located = transport.locate(request["path"], config)
        sent = transport.upload(config, TOKEN, request, located)
        slot, upload, complete = calls(api, "files.getUploadURLExternal")[-1], calls(api, "upload")[-1], calls(api, "files.completeUploadExternal")[-1]
        assert slot["encoding"] == "form" and slot["params"] == {"filename": "report.pdf", "length": str(len(payload)),
                                                                 "alt_txt": "Quarterly report"}
        assert upload["authorization"] is None and upload["content_type"] == "application/octet-stream"
        assert upload["sha256"] == hashlib.sha256(payload).hexdigest() and upload["size"] == len(payload)
        assert complete["encoding"] == "form" and complete["token_ok"]
        assert json.loads(complete["params"]["files"]) == [{"id": sent["file"]["id"], "title": "Q3 report"}]
        assert (complete["params"]["channel_id"], complete["params"]["initial_comment"], complete["params"]["thread_ts"]) == (
            GENERAL, "Here it is", PARENT)
        assert sent["operation"] == "upload_file" and sent["channel"] == GENERAL and sent["attempts"] == 3
        assert sent["file"]["name"] == "report.pdf" and sent["file"]["size"] == len(payload) and sent["ts"] is None
        assert sent["request_id"] == "u-1" and sent["thread_ts"] == PARENT and api.files[sent["file"]["id"]]["channel"] == GENERAL
        api.share_ts = True
        renamed = transport.upload(config, TOKEN, logic.file_request({"path": "report.pdf", "filename": "Q3 – report.pdf"}, config),
                                   located)
        assert renamed["ts"] and renamed["date"] and renamed["file"]["title"] == "Q3 – report.pdf"
        assert calls(api, "files.getUploadURLExternal")[-1]["params"]["filename"] == "Q3 – report.pdf"
        # An upload URL outside the expected origin is refused before any byte leaves the host.
        before = len(calls(api, "upload"))
        api.script["files.getUploadURLExternal"] = [{"status": 200, "body": {
            "ok": True, "upload_url": "https://files.slack.com.evil.example/upload/v1/x", "file_id": "F0EVIL0001"}}]
        failure(lambda: transport.upload(config, TOKEN, request, located), "protocol")
        assert len(calls(api, "upload")) == before
        completed = len(calls(api, "files.completeUploadExternal"))
        api.script["upload"] = [{"status": 500, "raw": b"error", "content_type": "text/plain"}]
        rejected_upload = failure(lambda: transport.upload(config, TOKEN, request, located), "upload_rejected")
        assert rejected_upload.http_status == 500 and not rejected_upload.uncertain
        assert len(calls(api, "files.completeUploadExternal")) == completed, "A failed upload must never be shared"
        api.script["files.completeUploadExternal"] = ["lost"]
        assert failure(lambda: transport.upload(config, TOKEN, request, located), "network").uncertain
        outsider = logic.file_request({"path": "report.pdf", "channel": OUTSIDER}, config)
        assert failure(lambda: transport.upload(config, TOKEN, outsider, located), "forbidden").slack_error == "not_in_channel"
        # The worker re-checks the file: a replaced file is refused before upload.
        (share / "report.pdf").write_bytes(payload + b"tail")
        before = len(calls(api, "upload"))
        failure(lambda: transport.upload(config, TOKEN, request, located), "file_changed")
        assert len(calls(api, "upload")) == before
    print("[ok] Slack external upload flow, origin checks and failure isolation", flush=True)


def value(data, port):
    raw = data.get("output_values", {}).get("slack:" + str(port), {}).get("value")
    return json.loads(raw) if raw else {}


def wallet(server):
    http_json(server.base_url, "/api/application/secrets/init", method="POST", payload={"password": "disposable-slack-fixture"})
    http_json(server.base_url, "/api/application/secrets", method="POST", payload={"name": "slack_fixture", "value": TOKEN})


def graph_runs():
    """# - FB2 - # - FB3 - # - FB4 - # - FB6 - real graphs in both runtimes for bundled, managed and linked packages."""
    block = SlackBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            full = origin is None
            with slack_api() as api, isolated_server() as server:
                share = server.root_dir / "share"
                share.mkdir()
                (share / "invoice.pdf").write_bytes(b"%PDF-1.4 fixture invoice")
                node = block.build_node_payload(node_id="slack", position={"x": 380, "y": 160},
                                                config_overrides=settings(api, file_root=str(share), page_size=3))
                if origin:
                    model = install_test_package(server, "slack", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html and TOKEN not in html
                        if surface != "node_card":
                            assert 'data-block-config-field="allowed_channels"' in html
                wallet(server)
                sinks = [display_node("inbox", "Messages", 760, 40), display_node("receipt", "Receipt", 760, 200),
                         display_node("problem", "Problem", 760, 360)]
                edges = [data_edge("rx", "slack", 1, "inbox", 1), data_edge("tx", "slack", 2, "receipt", 1),
                         data_edge("err", "slack", 3, "problem", 1)]
                if mode == "centralized":
                    def run(port, text):
                        graph = graph_payload("Slack one shot", [text_node("source", "Source", text, 60, 160), node, *sinks],
                                              [data_edge("in", "source", 1, "slack", port), *edges])
                        project = create_project_api(server, document=graph)["project"]
                        created = create_run_api(server, graph, project_id=project["graph_id"], runtime_mode=mode)
                        data = wait_for_run_terminal(server, created["run_id"], timeout_sec=40)
                        assert data["status"] == "success", (origin, data.get("logs"))
                        assert TOKEN not in json.dumps(data)
                        return data
                    sent = value(run(1, "Build finished"), 2)
                    assert sent["channel"] == GENERAL and sent["operation"] == "post_message"
                    assert api.history[GENERAL][-1]["text"] == "Build finished" and api.history[GENERAL][-1]["ts"] == sent["ts"]
                    read = value(run(1, json.dumps({"operation": "read_history", "limit": 4})), 1)
                    assert read["count"] == 4 and read["pages"] == 2 and read["messages"][0]["text"] == "Build finished"
                    if full:
                        document = value(run(2, json.dumps({"path": "invoice.pdf", "title": "Invoice"})), 2)
                        assert document["operation"] == "upload_file" and calls(api, "upload")[-1]["size"] == 24
                        assert calls(api, "upload")[-1]["authorization"] is None
                        thread = value(run(1, json.dumps({"operation": "read_replies", "ts": PARENT})), 1)
                        assert thread["count"] == 4 and thread["thread_ts"] == PARENT
                        before = len(api.calls)
                        refused = value(run(1, json.dumps({"text": "leak", "channel": OTHER})), 3)
                        assert refused["code"] == "channel_not_allowed" and len(api.calls) == before
                        outside = value(run(2, str(server.root_dir / "user")), 3)
                        assert outside["code"] in ("file_not_allowed", "file_not_found") and len(api.calls) == before
                        missing = value(run(1, json.dumps({"text": "x", "channel": MISSING})), 3)
                        assert missing["code"] == "not_found" and missing["slack_error"] == "channel_not_found"
                        http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                        before = len(api.calls)
                        locked = value(run(1, "wallet locked"), 3)
                        assert locked["code"] == "secret" and len(api.calls) == before
                else:
                    graph = graph_payload("Slack bot", [text_node("cmd", "Command", "", 60, 160), node, *sinks],
                                          [data_edge("in", "cmd", 1, "slack", 1), *edges])
                    project = create_project_api(server, document=graph)["project"]

                    def start():
                        rid = prepare_release_run(server, project["graph_id"], graph)["run_id"]
                        http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                        return rid

                    def send(rid, text):
                        http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                            "action": "publish_output", "node_id": "cmd", "port_id": 1, "value": text,
                            "content_type": "text/plain"})

                    def wait(rid, predicate, label):
                        data = wait_for_run_predicate(server, rid, lambda item: item.get("status") == "failed" or predicate(item),
                                                      label, timeout_sec=30)
                        assert data.get("status") != "failed", data.get("logs")
                        assert TOKEN not in json.dumps(data)
                        return data

                    def stop(rid):
                        started = time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        return stopped

                    rid = start()
                    try:
                        assert not api.calls, "Preparation or Play contacted Slack without an input"
                        send(rid, "Reply from graph")
                        sent = value(wait(rid, lambda item: value(item, 2).get("ts"), "message not sent"), 2)
                        assert sent["channel"] == GENERAL and api.history[GENERAL][-1]["text"] == "Reply from graph"
                        send(rid, json.dumps({"operation": "read_history", "limit": 2, "request_id": "active-read"}))
                        read = value(wait(rid, lambda item: value(item, 1).get("request_id") == "active-read", "read missing"), 1)
                        assert read["count"] == 2 and read["messages"][0]["ts"] == sent["ts"]
                        if full:
                            send(rid, json.dumps({"operation": "update_message", "ts": sent["ts"], "text": "Edited in run",
                                                  "request_id": "active-edit"}))
                            edited = value(wait(rid, lambda item: value(item, 2).get("request_id") == "active-edit", "edit missing"), 2)
                            assert edited["operation"] == "update_message" and api.history[GENERAL][-1]["text"] == "Edited in run"
                            http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                            before = len(api.calls)
                            send(rid, "locked")
                            wait(rid, lambda item: value(item, 3).get("code") == "secret", "locked wallet not reported")
                            assert len(api.calls) == before
                    finally:
                        stop(rid)
                assert TOKEN not in "\n".join(server.logs)
            print("[ok] Slack graph " + mode + " " + str(origin), flush=True)


def main():
    post_protocol()
    read_protocol()
    upload_protocol()
    graph_runs()


if __name__ == "__main__":
    main()

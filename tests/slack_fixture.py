"""Loopback-only Slack Web API double. No real workspace, token, channel or provider is contacted.

It reproduces the documented request/response shapes used by the block:
chat.postMessage and chat.update (JSON bodies), conversations.history and
conversations.replies (form bodies, cursor pagination through
response_metadata.next_cursor), files.getUploadURLExternal, the pre-signed
upload URL (raw bytes, no token) and files.completeUploadExternal, plus the
{"ok": false, "error": "..."} envelope, HTTP 429 with Retry-After and HTTP 404
for an unknown method.
"""

import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl

# Built at runtime so secret scanners do not mistake this fake value for a real token.
TOKEN = "-".join(("xoxb", "fixture", "slack", "token", "not", "real"))
REF = "secret://workspace/slack_fixture"
GENERAL, RANDOM, OTHER, MISSING, OUTSIDER = "C0FIXTURE01", "C0FIXTURE02", "C0FIXTURE99", "C0MISSING00", "C0OUTSIDE01"
BOT_USER, BOT_ID, PERSON = "U0FIXBOT01", "B0FIXBOT01", "U0PERSON001"
PARENT = "1760000003.000300"
FORM_METHODS = {"conversations.history", "conversations.replies", "files.getUploadURLExternal",
                "files.completeUploadExternal"}


def settings(api, **extra):
    """Block settings aimed at this fixture only."""
    return {"enabled": True, "secret_ref": REF, "allowed_channels": [GENERAL, RANDOM, MISSING, OUTSIDER],
            "default_channel": GENERAL, "api_base_url": api.base_url, "ca_file": api.ca_file, "timeout_sec": 15,
            **extra}


def seed():
    """Six top-level messages in #general (one thread parent with three replies) and one in #random."""
    history = {GENERAL: [], RANDOM: [], OTHER: []}
    for index in range(1, 7):
        ts = f"176000000{index}.000{index}00"
        item = {"type": "message", "user": PERSON, "text": f"note {index}\nsecond line", "ts": ts,
                "team": "T0FIXTURE1", "client_msg_id": "c-" + str(index),
                "user_profile": {"real_name": "Ada Lovelace", "display_name": "ada", "image_72": "https://example.invalid/a.png"},
                "blocks": [{"type": "rich_text", "elements": []}]}
        if ts == PARENT:
            item.update(thread_ts=PARENT, reply_count=3, reply_users_count=1, latest_reply="1760000013.000003",
                        reactions=[{"name": "eyes", "count": 2, "users": [PERSON, BOT_USER]}])
        if index == 5:
            item["files"] = [{"id": "F0SEEDFILE1", "name": "plan.pdf", "title": "Plan", "mimetype": "application/pdf",
                              "filetype": "pdf", "size": 1234, "url_private": "https://files.slack.com/private/plan.pdf"}]
            item["edited"] = {"user": PERSON, "ts": "1760000100.000000"}
        history[GENERAL].append(item)
    for index in range(1, 4):
        history[GENERAL].append({"type": "message", "user": PERSON, "text": f"reply {index}",
                                 "ts": f"176000001{index}.00000{index}", "thread_ts": PARENT, "parent_user_id": PERSON})
    history[RANDOM].append({"type": "message", "subtype": "bot_message", "bot_id": BOT_ID, "text": "random",
                            "ts": "1760000020.000020"})
    return history


def cursor_for(offset):
    return base64.b64encode(f"next_ts:{offset}".encode("ascii")).decode("ascii")


def offset_of(cursor):
    try:
        text = base64.b64decode(cursor.encode("ascii"), validate=True).decode("ascii")
        assert text.startswith("next_ts:")
        return int(text.split(":", 1)[1])
    except Exception:
        return None


@contextmanager
def slack_api(tls=False):
    """Run the double on a dynamic loopback port; every owned thread is joined on exit."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        lock = threading.Lock()
        api = SimpleNamespace(calls=[], script={}, history=seed(), uploads={}, files={}, clock=1760001000,
                              release=threading.Event(), closing=threading.Event(), share_ts=False)

        def next_ts():
            with lock:
                api.clock += 1
                return f"{api.clock}.000100"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def reply(self, status, body, *, raw=None, content_type="application/json; charset=utf-8", headers=None):
                data = raw if raw is not None else json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionError, ssl.SSLError):
                    pass
                finally:
                    self.close_connection = True

            def do_GET(self):
                self.reply(405, {"ok": False, "error": "method_not_supported_for_channel_type"})

            def scripted(self, key):
                """Apply a scripted fault; return True when the request is fully handled."""
                actions = api.script.get(key)
                if not actions:
                    return False
                action = actions.pop(0)
                if action == "hold":
                    api.release.wait(20)
                    return False
                if action == "lost":
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return True
                if action == "html502":
                    self.reply(502, None, raw=b"<html>Bad Gateway</html>", content_type="text/html")
                    return True
                if action == "oversized":
                    self.reply(200, {"ok": True, "padding": "x" * 200000})
                    return True
                if isinstance(action, dict):
                    self.reply(action["status"], action.get("body"), raw=action.get("raw"),
                               content_type=action.get("content_type", "application/json; charset=utf-8"),
                               headers=action.get("headers"))
                    return True
                return False

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                assert 0 <= length <= 120 * 1024 * 1024
                body = self.rfile.read(length)
                upload = re.fullmatch(r"/upload/v1/([A-Za-z0-9_-]+)", self.path)
                if upload:
                    self.upload(upload.group(1), body)
                    return
                match = re.fullmatch(r"/api/([A-Za-z.]+)", self.path)
                if not match:
                    self.reply(404, {"ok": False, "error": "unknown_method"})
                    return
                method = match.group(1)
                media = self.headers.get("Content-Type", "")
                if media.startswith("application/json"):
                    params, encoding = json.loads(body or b"{}"), "json"
                else:
                    assert media == "application/x-www-form-urlencoded", media
                    params, encoding = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True)), "form"
                auth = self.headers.get("Authorization", "")
                call = {"method": method, "params": params, "encoding": encoding, "authorization": auth,
                        "token_ok": auth == "Bearer " + TOKEN, "accept_encoding": self.headers.get("Accept-Encoding"),
                        "at": time.monotonic()}
                api.calls.append(call)
                if self.scripted(method):
                    return
                if not auth:
                    self.reply(200, {"ok": False, "error": "not_authed"})
                    return
                if not call["token_ok"]:
                    self.reply(200, {"ok": False, "error": "invalid_auth"})
                    return
                if (method in FORM_METHODS) != (encoding == "form"):
                    self.reply(200, {"ok": False, "error": "invalid_arguments"})
                    return
                handler = {"chat.postMessage": self.post_message, "chat.update": self.update_message,
                           "conversations.history": self.history, "conversations.replies": self.replies,
                           "files.getUploadURLExternal": self.upload_url,
                           "files.completeUploadExternal": self.complete_upload}.get(method)
                if handler is None:
                    self.reply(404, {"ok": False, "error": "unknown_method"})
                    return
                handler(params)

            def channel(self, params, key="channel"):
                channel = params.get(key)
                if channel == OUTSIDER:
                    self.reply(200, {"ok": False, "error": "not_in_channel"})
                    return None
                if channel not in api.history:
                    self.reply(200, {"ok": False, "error": "channel_not_found"})
                    return None
                return channel

            def post_message(self, params):
                channel = self.channel(params)
                if channel is None:
                    return
                if not params.get("text") and not params.get("blocks"):
                    self.reply(200, {"ok": False, "error": "no_text"})
                    return
                if len(params.get("text") or "") > 40000:
                    self.reply(200, {"ok": False, "error": "msg_too_long"})
                    return
                ts = next_ts()
                message = {"type": "message", "subtype": "bot_message", "user": BOT_USER, "bot_id": BOT_ID,
                           "text": params.get("text", ""), "ts": ts}
                if params.get("blocks"):
                    message["blocks"] = params["blocks"]
                if params.get("thread_ts"):
                    message["thread_ts"] = params["thread_ts"]
                api.history[channel].append(message)
                self.reply(200, {"ok": True, "channel": channel, "ts": ts, "message": message,
                                 "warning": "superfluous_charset",
                                 "response_metadata": {"warnings": ["superfluous_charset"]}})

            def update_message(self, params):
                channel = self.channel(params)
                if channel is None:
                    return
                target = next((item for item in api.history[channel] if item["ts"] == params.get("ts")), None)
                if target is None:
                    self.reply(200, {"ok": False, "error": "message_not_found"})
                    return
                if target.get("user") != BOT_USER:
                    self.reply(200, {"ok": False, "error": "cant_update_message"})
                    return
                target["text"] = params.get("text", target["text"])
                target["edited"] = {"user": BOT_USER, "ts": next_ts()}
                self.reply(200, {"ok": True, "channel": channel, "ts": target["ts"], "text": target["text"],
                                 "message": dict(target)})

            def page(self, items, params):
                limit = int(params.get("limit") or 100)
                assert 1 <= limit <= 999, limit
                offset = 0
                if params.get("cursor"):
                    offset = offset_of(params["cursor"])
                    if offset is None:
                        self.reply(200, {"ok": False, "error": "invalid_cursor"})
                        return None
                chunk = items[offset:offset + limit]
                more = offset + limit < len(items)
                return chunk, more, cursor_for(offset + limit) if more else ""

            def history(self, params):
                channel = self.channel(params)
                if channel is None:
                    return
                inclusive = params.get("inclusive") in ("true", "1")
                oldest, latest = float(params.get("oldest") or 0), float(params.get("latest") or 9e11)
                items = [item for item in api.history[channel] if item.get("thread_ts") in (None, item["ts"])
                         and (oldest <= float(item["ts"]) <= latest if inclusive else oldest < float(item["ts"]) < latest)]
                items.sort(key=lambda item: float(item["ts"]), reverse=True)
                result = self.page(items, params)
                if result is None:
                    return
                chunk, more, cursor = result
                self.reply(200, {"ok": True, "messages": chunk, "has_more": more, "pin_count": 0,
                                 "channel_actions_ts": None, "channel_actions_count": 0,
                                 "response_metadata": {"next_cursor": cursor}})

            def replies(self, params):
                channel = self.channel(params)
                if channel is None:
                    return
                parent = next((item for item in api.history[channel] if item["ts"] == params.get("ts")), None)
                if parent is None:
                    self.reply(200, {"ok": False, "error": "thread_not_found"})
                    return
                replies = sorted((item for item in api.history[channel]
                                  if item.get("thread_ts") == parent["ts"] and item["ts"] != parent["ts"]),
                                 key=lambda item: float(item["ts"]))
                result = self.page(replies, {**params, "limit": max(1, int(params.get("limit") or 100) - 1)})
                if result is None:
                    return
                chunk, more, cursor = result
                # Like Slack, every page starts with the thread parent.
                body = {"ok": True, "messages": [parent, *chunk], "has_more": more}
                if more:
                    body["response_metadata"] = {"next_cursor": cursor}
                self.reply(200, body)

            def upload_url(self, params):
                try:
                    size = int(params.get("length", ""))
                except ValueError:
                    size = -1
                if not params.get("filename") or size <= 0:
                    self.reply(200, {"ok": False, "error": "invalid_arguments"})
                    return
                key = secrets.token_hex(8)
                with lock:
                    file_id = "F0UPLOAD" + str(len(api.uploads) + 1).zfill(3)
                    api.uploads[key] = {"file_id": file_id, "filename": params["filename"], "length": size,
                                        "alt_txt": params.get("alt_txt"), "received": None}
                self.reply(200, {"ok": True, "upload_url": api.base_url + "/upload/v1/" + key, "file_id": file_id})

            def upload(self, key, body):
                record = api.uploads.get(key)
                call = {"method": "upload", "key": key, "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"), "size": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(), "at": time.monotonic()}
                api.calls.append(call)
                if self.scripted("upload"):
                    return
                if record is None or len(body) != record["length"]:
                    self.reply(400, None, raw=b"Bad Request", content_type="text/plain")
                    return
                record["received"] = call
                self.reply(200, None, raw=("OK - " + str(len(body))).encode("ascii"), content_type="text/plain")

            def complete_upload(self, params):
                try:
                    files = json.loads(params.get("files", ""))
                except ValueError:
                    files = None
                if not isinstance(files, list) or len(files) != 1:
                    self.reply(200, {"ok": False, "error": "invalid_arguments"})
                    return
                record = next((item for item in api.uploads.values() if item["file_id"] == files[0].get("id")), None)
                if record is None or record["received"] is None:
                    self.reply(200, {"ok": False, "error": "file_not_found"})
                    return
                channel = self.channel(params, "channel_id")
                if channel is None:
                    return
                descriptor = {"id": record["file_id"], "name": record["filename"], "title": files[0].get("title"),
                              "mimetype": "application/pdf" if record["filename"].endswith(".pdf") else "text/plain",
                              "filetype": "pdf" if record["filename"].endswith(".pdf") else "text",
                              "size": record["length"], "url_private": "https://files.slack.com/private/x",
                              "shares": {}}
                if api.share_ts:
                    descriptor["shares"] = {"public": {channel: [{"ts": next_ts(), "channel_name": "general"}]}}
                api.files[record["file_id"]] = {**descriptor, "channel": channel, "comment": params.get("initial_comment"),
                                                "thread_ts": params.get("thread_ts")}
                self.reply(200, {"ok": True, "files": [descriptor]})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        ca = ""
        if tls:
            assert shutil.which("openssl"), "OpenSSL is required for the disposable HTTPS fixture"
            certificate, key = directory / "ca.pem", directory / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            ca = str(certificate)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        api.base_url = ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port)
        api.ca_file = ca
        try:
            yield api
        finally:
            api.closing.set()
            api.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()

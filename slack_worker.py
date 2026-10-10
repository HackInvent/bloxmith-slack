"""Short-lived Slack Web API exchange, run as an isolated child process.

Standard library only. The job, including the bot token, arrives on the private
stdin pipe, never in arguments or the environment. One JSON result is written
to stdout. The owning block kills this process group on cancellation, Stop or
deadline; a watchdog also exits if the parent disappears.

Two kinds of exchange exist: an authenticated Web API call (`api`), and the
upload of file bytes to a pre-signed upload URL (`upload`), which never carries
the token.
"""

import http.client
import json
import os
import re
import resource
import socket
import ssl
import stat
import sys
import threading
import time
from urllib.parse import urlsplit

METHODS = {"chat.postMessage", "chat.update", "conversations.history", "conversations.replies",
           "files.getUploadURLExternal", "files.completeUploadExternal"}
TOKEN = re.compile(r"xoxb-[A-Za-z0-9-]{10,250}\Z")
CHUNK = 65536


class Failure(Exception):
    """A content-free transport outcome reported to the parent."""

    def __init__(self, code, phase):
        super().__init__(code)
        self.code, self.phase = code, phase


def remaining(deadline, phase):
    left = deadline - time.monotonic()
    if left <= 0:
        raise Failure("timeout", phase)
    return max(0.001, left)


def open_inside(root, parts, limit):
    """Walk from the canonical root with O_NOFOLLOW so no symlink can be swapped in."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    try:
        directory = os.open(root, flags | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Failure("file_not_allowed", "connect") from None
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        raise Failure("file_not_found", "connect") from None
    except OSError:
        raise Failure("file_not_allowed", "connect") from None
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise Failure("file_not_allowed", "connect")
    if info.st_size > limit or info.st_size <= 0:
        os.close(descriptor)
        raise Failure("file_too_large" if info.st_size > limit else "file_empty", "connect")
    return descriptor, info


def connection(origin_text, ca_file, deadline):
    origin = urlsplit(origin_text)
    host, port = origin.hostname, origin.port
    if origin.scheme == "https":
        context = ssl.create_default_context(cafile=ca_file or None)
        return http.client.HTTPSConnection(host, port or 443, timeout=remaining(deadline, "connect"), context=context)
    if origin.scheme != "http":
        raise Failure("protocol", "connect")
    return http.client.HTTPConnection(host, port or 80, timeout=remaining(deadline, "connect"))


def send_request(conn, job, deadline, opened):
    """Send one complete request. Until the last byte is written nothing can be processed."""
    if job["kind"] == "api":
        body = job["body"].encode("utf-8")
        conn.putrequest("POST", "/api/" + job["method"], skip_host=False, skip_accept_encoding=True)
        headers = [("Authorization", "Bearer " + job["token"]), ("Content-Type", job["content_type"]),
                   ("Content-Length", str(len(body))), ("Accept", "application/json")]
        parts = [body]
    else:
        descriptor, info = opened
        conn.putrequest("POST", job["path"], skip_host=False, skip_accept_encoding=True)
        headers = [("Content-Type", "application/octet-stream"), ("Content-Length", str(info.st_size))]
        parts = [(descriptor, info)]
    for key, value in headers + [("Accept-Encoding", "identity"), ("User-Agent", "BloxSmith-Slack/0.1.0"),
                                 ("Connection", "close")]:
        conn.putheader(key, value)
    conn.sock.settimeout(remaining(deadline, "send"))
    conn.endheaders()
    for part in parts:
        if isinstance(part, tuple):
            handle, info = part
            sent = 0
            while sent < info.st_size:
                chunk = os.read(handle, min(CHUNK, info.st_size - sent))
                if not chunk:
                    raise Failure("file_changed", "send")
                conn.sock.settimeout(remaining(deadline, "send"))
                conn.send(chunk)
                sent += len(chunk)
            after = os.fstat(handle)
            if os.read(handle, 1) or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise Failure("file_changed", "send")
        else:
            conn.sock.settimeout(remaining(deadline, "send"))
            conn.send(part)


def read_body(conn, response, deadline, limit):
    chunks, total = [], 0
    while True:
        sock = conn.sock
        if sock is not None:
            sock.settimeout(remaining(deadline, "receive"))
        piece = response.read1(min(CHUNK, limit + 1 - total)) if total <= limit else b""
        if not piece:
            break
        total += len(piece)
        chunks.append(piece)
        if total > limit:
            break
    return b"".join(chunks), total > limit


def exchange(job):
    """One request/response; no redirect, retry, cookie or decompression."""
    if job.get("kind") == "api":
        if job.get("method") not in METHODS or not TOKEN.fullmatch(str(job.get("token", ""))):
            raise Failure("protocol", "connect")
    elif job.get("kind") == "upload":
        path = str(job.get("path", ""))
        if not path.startswith("/") or any(ord(char) <= 32 or ord(char) >= 127 for char in path):
            raise Failure("protocol", "connect")
    else:
        raise Failure("protocol", "connect")
    deadline = time.monotonic() + float(job["timeout_sec"])
    phase = "connect"
    conn, opened = None, None
    try:
        if job["kind"] == "upload":
            # Validate the local file before any connection; failures upload nothing.
            opened = open_inside(job["root"], job["parts"], int(job["max_bytes"]))
            if opened[1].st_size != int(job["length"]):
                raise Failure("file_changed", "connect")
        conn = connection(job["origin"], job.get("ca_file") or "", deadline)
        conn.connect()
        phase = "send"
        send_request(conn, job, deadline, opened)
        phase = "receive"
        conn.sock.settimeout(remaining(deadline, phase))
        response = conn.getresponse()
        data, oversized = read_body(conn, response, deadline, int(job["max_response_bytes"]))
        headers = {key.lower(): value for key, value in response.getheaders()}
        retry = headers.get("retry-after", "").strip()
        result = {"status": "response", "http_status": response.status,
                  "retry_after_header": int(retry) if retry.isdigit() and len(retry) <= 6 else None,
                  "body": None, "body_error": None}
        if job["kind"] == "upload":
            return result
        media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if oversized:
            result["body_error"] = "too_large"
        elif headers.get("content-encoding", "identity").strip().lower() not in ("identity", ""):
            result["body_error"] = "encoded"
        elif media != "application/json":
            result["body_error"] = "not_json"
        else:
            try:
                result["body"] = json.loads(data.decode("utf-8", errors="strict"), parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                result["body_error"] = "invalid_json"
        return result
    except Failure:
        raise
    except ssl.SSLCertVerificationError:
        raise Failure("tls", phase) from None
    except ssl.SSLError:
        raise Failure("tls" if phase == "connect" else "network", phase) from None
    except (socket.timeout, TimeoutError):
        raise Failure("timeout", phase) from None
    except (OSError, http.client.HTTPException, ValueError):
        raise Failure("network", phase) from None
    finally:
        if conn is not None:
            conn.close()
        if opened is not None:
            os.close(opened[0])


def reject_constant(_value):
    raise ValueError("Non-finite JSON number.")


def emit(value):
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    view = memoryview(data)
    while view:
        written = os.write(1, view)
        view = view[written:]


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    parent = os.getppid()
    raw = sys.stdin.buffer.read(2097153)
    if len(raw) > 2097152:
        emit({"status": "error", "code": "protocol", "phase": "connect"})
        return 2
    try:
        job = json.loads(raw)
        budget = float(job["timeout_sec"])
    except (ValueError, KeyError, TypeError):
        emit({"status": "error", "code": "protocol", "phase": "connect"})
        return 2
    box = {}

    def target():
        try:
            box["result"] = exchange(job)
        except Failure as failure:
            box["result"] = {"status": "error", "code": failure.code, "phase": failure.phase}
        except Exception:
            box["result"] = {"status": "error", "code": "network", "phase": "receive"}

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    hard_deadline = time.monotonic() + budget + 2
    while worker.is_alive():
        worker.join(0.2)
        if os.getppid() != parent:
            os._exit(3)
        if time.monotonic() > hard_deadline:
            emit({"status": "error", "code": "timeout", "phase": "receive"})
            os._exit(4)
    emit(box["result"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

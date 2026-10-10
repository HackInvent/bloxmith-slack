"""Own each Slack exchange in a killable child process and interpret its outcome.

Credentials travel only through the child's private stdin pipe. Cancellation,
Stop and deadlines terminate the whole child process group. Only HTTP 429
(rate limited) is retried, because Slack did not process that request;
ambiguous write failures are reported, never replayed.
"""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from . import logic

WORKER = Path(__file__).with_name("slack_worker.py")
ENVIRONMENT = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
OUTPUT_LIMIT = 3 * 8388608 + 65536


class WorkerCall:
    """One running exchange. poll() is non-blocking enough for Stop-aware loops."""

    def __init__(self, job):
        self.payload = json.dumps(job, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self.child = subprocess.Popen([sys.executable, "-I", "-B", "-u", str(WORKER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            env=dict(ENVIRONMENT))
        self.first, self.output = True, None

    def poll(self, timeout=0.05):
        """Return the worker result once finished, else None after at most timeout seconds."""
        if self.output is not None:
            return self.output
        try:
            output, _ = self.child.communicate(input=self.payload if self.first else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.first = False
            return None
        self.first = False
        if len(output) > OUTPUT_LIMIT:
            self.output = {"status": "error", "code": "protocol", "phase": "receive"}
        else:
            try:
                value = json.loads(output)
                self.output = value if isinstance(value, dict) else {"status": "error", "code": "protocol", "phase": "receive"}
            except (ValueError, UnicodeError):
                self.output = {"status": "error", "code": "protocol", "phase": "receive"}
        self.close()
        return self.output

    def close(self):
        """Terminate and reap only this process group; idempotent."""
        child = self.child
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
        for stream in (child.stdin, child.stdout):
            if stream and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


def interpret(outcome, token, *, mutation):
    """Map a worker outcome to the Web API body or a structured, token-free error."""
    if outcome.get("status") == "response":
        if outcome.get("body_error") is not None:
            status = outcome.get("http_status")
            if type(status) is int and status >= 500:
                logic.fail("provider_unavailable", uncertain=mutation, http_status=status)
            if type(status) is int and status in (400, 401, 403, 404, 410, 413, 429):
                header = outcome.get("retry_after_header")
                logic.fail(logic.map_status(status), http_status=status,
                           retry_after=header if status == 429 and type(header) is int else None)
            logic.fail("protocol", uncertain=mutation and type(status) is int and status < 300,
                       http_status=status if type(status) is int else None)
        return logic.provider_result(outcome, token, mutation=mutation)
    code, phase = outcome.get("code"), outcome.get("phase")
    after_send = phase == "receive"
    if code in ("file_not_found", "file_not_allowed", "file_too_large", "file_empty", "file_changed", "tls"):
        logic.fail(code)
    if code == "timeout":
        message = ("timeout_uncertain" if after_send else "timeout") if mutation else "timeout_safe"
        logic.fail("timeout", logic.MESSAGES[message], uncertain=after_send and mutation)
    if code == "network":
        logic.fail("network", logic.MESSAGES["network_uncertain" if after_send and mutation else "network"],
                   uncertain=after_send and mutation)
    logic.fail("protocol", uncertain=after_send and mutation)


def run_job(job, *, mutation, deadline, cancel):
    """Run one worker exchange while honouring the caller deadline and cancellation."""
    budget = deadline - time.monotonic()
    if budget <= 0.5:
        logic.fail("timeout")
    if cancel():
        logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
    worker = WorkerCall({**job, "timeout_sec": float(budget)})
    try:
        while True:
            outcome = worker.poll(0.05)
            if outcome is not None:
                return outcome
            if cancel():
                logic.fail("cancelled", None if mutation else logic.MESSAGES["cancelled_safe"], uncertain=mutation)
            if time.monotonic() >= deadline + 1:
                logic.fail("timeout", logic.MESSAGES["timeout_uncertain" if mutation else "timeout_safe"], uncertain=mutation)
    finally:
        worker.close()


def pause(seconds, cancel):
    """Cancellation-aware wait used only between a 429 response and its retry."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel():
            logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


class Session:
    """One block operation: a shared deadline, cancellation and the 429 retry budget per call."""

    def __init__(self, config, token, cancel=lambda: False):
        self.config, self.token, self.cancel = config, token, cancel
        self.deadline = time.monotonic() + config["timeout_sec"]
        self.attempts = 0

    def api(self, method, params):
        """One Web API method call, retried only after HTTP 429 within the configured bounds."""
        if method not in logic.METHODS:
            logic.fail("internal")
        mutation = method in logic.MUTATIONS
        body = logic.request_body(method, params).decode("utf-8")
        tries = 0
        while True:
            tries += 1
            self.attempts += 1
            job = {"kind": "api", "origin": self.config["api_base_url"], "ca_file": self.config["ca_file"],
                   "method": method, "token": self.token, "body": body, "content_type": logic.content_type(method),
                   "max_response_bytes": self.config["max_response_bytes"]}
            try:
                outcome = run_job(job, mutation=mutation, deadline=self.deadline, cancel=self.cancel)
                return interpret(outcome, self.token, mutation=mutation)
            except logic.SlackError as exc:
                wait = logic.retry_delay(exc, tries, self.config, self.deadline - time.monotonic())
                if wait is None:
                    exc.attempts = self.attempts
                    raise
                pause(wait, self.cancel)

    def upload(self, target, located, length):
        """Send the file bytes to the pre-signed URL without the token; nothing is shared yet."""
        self.attempts += 1
        job = {"kind": "upload", "origin": target["origin"], "path": target["path"], "ca_file": self.config["ca_file"],
               "root": located["root"], "parts": located["parts"], "length": length,
               "max_bytes": self.config["max_file_bytes"], "max_response_bytes": 65536}
        try:
            outcome = run_job(job, mutation=False, deadline=self.deadline, cancel=self.cancel)
            if outcome.get("status") == "response":
                status = outcome.get("http_status")
                if type(status) is int and 200 <= status < 300:
                    return
                logic.fail("upload_rejected", http_status=status if type(status) is int else None)
            interpret(outcome, "", mutation=False)
        except logic.SlackError as exc:
            exc.attempts = self.attempts
            raise


def post(config, token, request, cancel=lambda: False):
    """chat.postMessage or chat.update; returns the normalized receipt."""
    session = Session(config, token, cancel)
    body = session.api(request["method"], request["params"])
    return logic.normalize_sent(body, request, session.attempts)


def read(config, token, request, cancel=lambda: False):
    """conversations.history / conversations.replies with bounded cursor pagination."""
    session = Session(config, token, cancel)
    collected, seen, cursor = [], set(), request.get("cursor")
    pages, has_more, size = 0, False, 0
    while True:
        remaining = request["limit"] - len(collected)
        params = {**request["params"], "limit": min(config["page_size"], remaining)}
        if cursor:
            params["cursor"] = cursor
        body = session.api(request["method"], params)
        pages += 1
        messages, has_more, cursor = logic.page_messages(body)
        for item in messages:
            # conversations.replies repeats the thread parent on each page; keep each ts once.
            if item["ts"] in seen or len(collected) >= request["limit"]:
                continue
            seen.add(item["ts"])
            collected.append(item)
            size += len(json.dumps(item, ensure_ascii=False))
        if (not has_more or not cursor or len(collected) >= request["limit"] or pages >= config["max_pages"]
                or size >= logic.OUTPUT_LIMIT):
            break
    return logic.normalize_read(request, collected, has_more, cursor, pages, session.attempts)


def upload(config, token, request, located, cancel=lambda: False):
    """files.getUploadURLExternal, then the bytes to upload_url, then files.completeUploadExternal."""
    session = Session(config, token, cancel)
    filename = request["filename"] or located["name"]
    slot = {"filename": filename, "length": located["size"]}
    if request["alt_text"]:
        slot["alt_txt"] = request["alt_text"]
    url, file_id = logic.upload_slot(session.api("files.getUploadURLExternal", slot))
    target = logic.upload_target(url, config)
    session.upload(target, located, located["size"])
    complete = {"files": [{"id": file_id, "title": request["title"] or filename}], "channel_id": request["channel"]}
    if request["initial_comment"]:
        complete["initial_comment"] = request["initial_comment"]
    if request["thread_ts"]:
        complete["thread_ts"] = request["thread_ts"]
    body = session.api("files.completeUploadExternal", complete)
    return logic.normalize_upload(body, request, file_id, filename, located["size"], session.attempts)


def locate(path_text, config):
    """Resolve an input path strictly inside file_root, refusing links and special files."""
    if not config["file_root"]:
        logic.fail("file_root_required")
    configured = Path(os.path.normpath(config["file_root"]))
    try:
        root = configured.resolve(strict=True)
    except (OSError, RuntimeError):
        logic.fail("file_root_required", "file_root does not exist or cannot be read.")
    if not root.is_dir() or root == Path(root.anchor):
        logic.fail("file_root_required", "file_root must be an existing dedicated directory, not a filesystem root.")
    candidate = Path(path_text)
    if any(part in ("..", ".") for part in candidate.parts):
        logic.fail("file_not_allowed")
    if candidate.is_absolute():
        normalized = Path(os.path.normpath(candidate))
        for base in (configured, root):
            try:
                relative = normalized.relative_to(base)
                break
            except ValueError:
                continue
        else:
            logic.fail("file_not_allowed")
    else:
        relative = candidate
    parts = [part for part in relative.parts if part not in ("", "/")]
    if not parts:
        logic.fail("file_not_allowed")
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            logic.fail("file_not_found")
        except OSError:
            logic.fail("file_not_allowed")
        last = index == len(parts) - 1
        if stat.S_ISLNK(info.st_mode) or (not last and not stat.S_ISDIR(info.st_mode)):
            logic.fail("file_not_allowed")
        if last and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            logic.fail("file_not_allowed")
    if info.st_size > config["max_file_bytes"]:
        logic.fail("file_too_large")
    if info.st_size <= 0:
        logic.fail("file_empty")
    return {"root": str(root), "parts": parts, "size": info.st_size, "suffix": current.suffix, "name": current.name}

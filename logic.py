"""Pure Slack Web API contracts: settings, inputs, provider envelopes, normalization and errors.

No network, filesystem, framework or third-party import happens here. Every
function is deterministic, so the policy can be checked directly and the
isolated network worker never depends on the framework.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import ipaddress
import json
import math
import re
from urllib.parse import urlencode, urlsplit

OFFICIAL_API = "https://slack.com"
DEFAULTS = {
    "enabled": False, "secret_ref": "", "allowed_channels": [], "default_channel": "", "file_root": "",
    "timeout_sec": 30, "page_size": 100, "max_pages": 5, "max_messages": 200,
    "max_file_bytes": 10485760, "max_response_bytes": 2097152, "max_retries": 2, "max_retry_wait_sec": 30,
    "api_base_url": OFFICIAL_API, "ca_file": "",
}
FRAMEWORK_KEYS = {"execution", "runtime_path", "runtime_path_label"}
BOUNDS = {
    "timeout_sec": (5, 300), "page_size": (1, 200), "max_pages": (1, 20), "max_messages": (1, 1000),
    "max_file_bytes": (1024, 104857600), "max_response_bytes": (65536, 8388608),
    "max_retries": (0, 5), "max_retry_wait_sec": (1, 120),
}
OPERATIONS = ("post_message", "update_message", "read_history", "read_replies", "upload_file")
METHODS = {
    "chat.postMessage": "post_message", "chat.update": "update_message",
    "conversations.history": "read_history", "conversations.replies": "read_replies",
    "files.getUploadURLExternal": "upload_file", "files.completeUploadExternal": "upload_file",
}
# Methods whose success changes what people see in Slack; an ambiguous outcome is "uncertain".
MUTATIONS = {"chat.postMessage", "chat.update", "files.completeUploadExternal"}
# Slack documents application/x-www-form-urlencoded for these methods, not JSON bodies.
FORM_METHODS = {"conversations.history", "conversations.replies", "files.getUploadURLExternal",
                "files.completeUploadExternal"}
SECRET_REF = re.compile(r"secret://(?:workspace/[A-Za-z0-9_.-]{1,80}|project/[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80})\Z")
BOT_TOKEN = re.compile(r"xoxb-[A-Za-z0-9-]{10,250}\Z")
ANY_TOKEN = re.compile(r"xox[a-z]-[A-Za-z0-9-]{6,}")
CHANNEL = re.compile(r"[CDG][A-Z0-9]{2,30}\Z")
TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,6}\Z")
BOUND_TS = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,6})?\Z")
SLACK_ID = re.compile(r"[A-Z0-9]{2,40}\Z")
FILE_ID = re.compile(r"F[A-Z0-9]{2,40}\Z")
CURSOR = re.compile(r"[A-Za-z0-9+/=_.:-]{1,512}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
PROVIDER_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
SCOPES = re.compile(r"[a-z0-9:._,-]{1,256}\Z")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
TEXT_LIMIT = 40000
BLOCK_LIMIT = 50
COMMENT_LIMIT = 4000
INPUT_LIMIT = 262144
REQUEST_LIMIT = 262144
OUTPUT_LIMIT = 16 * 1024 * 1024
MESSAGE_KEYS = {
    "post_message": {"operation", "channel", "text", "blocks", "thread_ts", "reply_broadcast", "unfurl_links",
                     "unfurl_media", "mrkdwn", "request_id"},
    "update_message": {"operation", "channel", "ts", "text", "blocks", "request_id"},
    "read_history": {"operation", "channel", "oldest", "latest", "inclusive", "limit", "cursor", "request_id"},
    "read_replies": {"operation", "channel", "ts", "thread_ts", "oldest", "latest", "inclusive", "limit", "cursor",
                     "request_id"},
}
FILE_KEYS = {"path", "channel", "title", "filename", "initial_comment", "thread_ts", "alt_text", "request_id"}

PROVIDER_CODES = {
    "unauthorized": {"not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired",
                     "not_allowed_token_type", "invalid_token"},
    "forbidden": {"missing_scope", "not_in_channel", "is_archived", "channel_is_archived", "restricted_action",
                  "restricted_action_read_only_channel", "restricted_action_thread_only_channel",
                  "restricted_action_non_threadable_channel", "cant_update_message", "edit_window_closed",
                  "no_permission", "access_denied", "ekm_access_denied", "team_access_not_granted",
                  "posting_to_general_channel_denied", "not_allowed"},
    "not_found": {"channel_not_found", "message_not_found", "thread_not_found", "unknown_method", "file_not_found",
                  "user_not_found"},
    "bad_request": {"invalid_arguments", "invalid_arg_name", "invalid_array_arg", "invalid_charset",
                    "invalid_form_data", "invalid_post_type", "missing_post_type", "invalid_json", "json_not_object",
                    "invalid_blocks", "invalid_blocks_format", "invalid_attachments", "msg_too_long", "no_text",
                    "too_many_attachments", "invalid_cursor", "invalid_ts_latest", "invalid_ts_oldest",
                    "invalid_metadata_format", "invalid_metadata_schema", "metadata_too_large", "invalid_name",
                    "missing_file_data", "file_upload_size_restricted", "invalid_channel", "upload_failed"},
    "rate_limited": {"ratelimited", "rate_limited", "message_limit_exceeded"},
    "provider_unavailable": {"fatal_error", "internal_error", "service_unavailable"},
}

MESSAGES = {
    "config": "Invalid Slack settings.",
    "disabled": "Remote access is disabled; enable the block explicitly before calling Slack.",
    "secret": "Unlock the wallet and store a Slack bot token (xoxb-...) under the configured secret_ref.",
    "invalid_input": "The input does not follow the documented Slack request contract.",
    "request_too_large": "The Slack request body exceeds 256 KiB; nothing was sent.",
    "channel_required": "No channel was given and no default_channel is configured; nothing was sent.",
    "channel_not_allowed": "The channel is not listed in allowed_channels; nothing was sent.",
    "one_operation": "Send one message or one file per activation; nothing was sent.",
    "file_root_required": "Configure file_root before uploading local files; nothing was sent.",
    "file_not_found": "The file does not exist inside file_root; nothing was sent.",
    "file_not_allowed": "The file is outside file_root, is not a single-link regular file or passes through a symbolic link.",
    "file_too_large": "The file exceeds max_file_bytes; nothing was sent.",
    "file_empty": "The file is empty; Slack does not accept empty uploads, so nothing was sent.",
    "file_changed": "The file changed while it was uploaded; the upload was abandoned and nothing was shared.",
    "upload_rejected": "The Slack upload URL did not accept the file; nothing was shared.",
    "bad_request": "Slack rejected the request arguments; nothing was changed.",
    "unauthorized": "Slack rejected the bot token; check the wallet secret.",
    "forbidden": "The bot is not allowed to perform this operation (missing scope, not in channel or restricted).",
    "not_found": "Slack could not find the channel, message, thread or method.",
    "rate_limited": "Slack rate limit reached (HTTP 429); retry after the indicated delay.",
    "provider_error": "Slack returned an unexpected error; nothing is assumed changed.",
    "provider_unavailable": "Slack is temporarily unavailable; the outcome of a write is uncertain.",
    "network": "The Slack API could not be reached; nothing was sent.",
    "network_uncertain": "The connection failed after the request was sent; the outcome is uncertain.",
    "tls": "TLS verification failed for the Slack API origin; nothing was sent.",
    "timeout": "The Slack operation exceeded timeout_sec before the request was complete; nothing was sent.",
    "timeout_uncertain": "The Slack operation exceeded timeout_sec after the request was sent; the outcome is uncertain.",
    "cancelled": "The operation was cancelled; the outcome is uncertain.",
    "cancelled_before_send": "The operation was cancelled before any request was sent.",
    "cancelled_safe": "The operation was cancelled; nothing was changed in Slack.",
    "timeout_safe": "The Slack operation exceeded timeout_sec; nothing was changed in Slack.",
    "protocol": "Slack returned an invalid, compressed or oversized response.",
    "internal": "The Slack operation failed without a complete result.",
}


class SlackError(ValueError):
    """A content-free diagnostic with a stable machine code and an English message."""

    def __init__(self, code, detail=None, *, uncertain=False, http_status=None, retry_after=None,
                 slack_error=None, needed_scope=None):
        super().__init__(detail or MESSAGES.get(code, MESSAGES["internal"]))
        self.code, self.uncertain = code, bool(uncertain)
        self.http_status, self.retry_after = http_status, retry_after
        self.slack_error, self.needed_scope = slack_error, needed_scope
        self.attempts = 0

    def envelope(self, **context):
        """Return the public error document; callers add only identifiers, never content."""
        result = {"code": self.code, "message": str(self), "uncertain": self.uncertain}
        for key, value in (("http_status", self.http_status), ("retry_after", self.retry_after),
                           ("slack_error", self.slack_error), ("needed_scope", self.needed_scope)):
            if value is not None:
                result[key] = value
        if self.attempts:
            result["attempts"] = self.attempts
        for key, value in context.items():
            if value is not None:
                result[key] = value
        return result


def fail(code, detail=None, **extra):
    """Raise a structured failure before any side effect."""
    raise SlackError(code, detail, **extra)


def thaw(value):
    """Turn immutable preparation snapshots back into plain JSON containers."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def encoded(value, limit=INPUT_LIMIT, code="invalid_input"):
    """Serialize strict finite JSON with bounded depth, element count and bytes."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 50000:
            fail(code, "JSON exceeds its depth or element limit.")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                fail(code, "JSON object keys must be strings.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            fail(code, "A strict JSON value is required.")
        elif type(item) is float and not math.isfinite(item):
            fail(code, "Non-finite numbers are not accepted.")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail(code, "Finite UTF-8 JSON is required.")
    if len(data) > limit:
        fail(code, "JSON exceeds its byte limit.")
    return data


def strict_json(text, limit=INPUT_LIMIT, code="invalid_input"):
    """Parse JSON text, rejecting duplicate keys, non-finite numbers and oversized input."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="strict")
        except UnicodeError:
            fail(code, "UTF-8 JSON is required.")
    if not isinstance(text, str):
        fail(code, "JSON text is required.")
    if len(text.encode("utf-8", errors="replace")) > limit:
        fail(code, "JSON exceeds its byte limit.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                fail(code, "Duplicate JSON property.")
            result[key] = item
        return result

    def constant(_name):
        fail(code, "Non-finite numbers are not accepted.")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except SlackError:
        raise
    except (ValueError, RecursionError):
        fail(code, "Malformed JSON.")
    encoded(value, limit, code)
    return value


def boolean(value, name, code="config"):
    if type(value) is not bool:
        fail(code, f"{name} must be a boolean.")
    return value


def integer(value, name, low, high, code="config"):
    if type(value) is not int or not low <= value <= high:
        fail(code, f"{name} must be an integer between {low} and {high}.")
    return value


def channel_id(value, name="channel", code="invalid_input"):
    """Normalize one Slack conversation ID; names such as #general are not accepted as targets."""
    if not isinstance(value, str) or not CHANNEL.fullmatch(value.strip()):
        fail(code, f"{name} must be a Slack conversation ID such as C0123456789 (not a #name).")
    return value.strip()


def path_text(value, name, code="config"):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail(code, f"{name} must be a bounded path without control characters.")
    value = value.strip()
    if value and (not value.startswith("/") or ".." in value.split("/")):
        fail(code, f"{name} must be empty or an absolute path without '..' components.")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_origin(value, name="api_base_url"):
    """Parse a bare origin and return (scheme, host, port); no path, query, fragment or credentials."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        fail("config", f"{name} must be an ASCII URL without spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        fail("config", f"{name} has an invalid host or port.")
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("https", "http") or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.path not in ("", "/") or "\\" in value or "%" in host):
        fail("config", f"{name} must be a bare https:// origin without credentials, path, query or fragment.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or host.endswith(".") or any(not HOST_LABEL.fullmatch(label) for label in host.split(".")):
            fail("config", f"{name} has an invalid host name.")
    if port is not None and not 1 <= port <= 65535:
        fail("config", f"{name} has an invalid port.")
    return parsed.scheme, host, port


def origin_text(scheme, host, port):
    netloc = "[" + host + "]" if ":" in host else host
    return scheme + "://" + netloc + (":" + str(port) if port is not None else "")


def api_origin(value):
    """The official https://slack.com origin, or a loopback test double (HTTP or HTTPS)."""
    scheme, host, port = split_origin(value)
    if is_loopback(host):
        return origin_text(scheme, host, port)
    if scheme != "https" or host != "slack.com" or port not in (None, 443):
        fail("config", "api_base_url must stay https://slack.com; only a loopback test host may replace it.")
    return OFFICIAL_API


def configuration(raw):
    """Pure settings validation; no wallet, filesystem or network access."""
    raw = thaw(raw)
    if not isinstance(raw, Mapping):
        fail("config", "Settings must be a JSON object.")
    unknown = set(raw) - set(DEFAULTS) - FRAMEWORK_KEYS
    if unknown:
        fail("config", "Unknown setting: " + sorted(unknown)[0] + ". Credentials belong in the wallet.")
    config = {**{key: thaw(value) for key, value in DEFAULTS.items()},
              **{key: value for key, value in raw.items() if key in DEFAULTS}}
    boolean(config["enabled"], "enabled")
    for key, (low, high) in BOUNDS.items():
        integer(config[key], key, low, high)
    ref = config["secret_ref"]
    if not isinstance(ref, str) or len(ref) > 256:
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/slack_bot.")
    ref = ref.strip()
    if ref and not SECRET_REF.fullmatch(ref):
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/slack_bot, never the token.")
    config["secret_ref"] = ref
    channels = config["allowed_channels"]
    if not isinstance(channels, list) or len(channels) > 100:
        fail("config", "allowed_channels must be a JSON array of at most 100 Slack conversation IDs.")
    config["allowed_channels"] = [channel_id(item, "allowed_channels", "config") for item in channels]
    if len(set(config["allowed_channels"])) != len(config["allowed_channels"]):
        fail("config", "allowed_channels must not contain duplicates.")
    default = config["default_channel"]
    if default in ("", None):
        config["default_channel"] = None
    else:
        config["default_channel"] = channel_id(default, "default_channel", "config")
        if config["default_channel"] not in config["allowed_channels"]:
            fail("config", "default_channel must also be listed in allowed_channels.")
    config["file_root"] = path_text(config["file_root"], "file_root")
    config["ca_file"] = path_text(config["ca_file"], "ca_file")
    config["api_base_url"] = api_origin(config["api_base_url"])
    if config["enabled"]:
        if not config["secret_ref"]:
            fail("config", "Remote access requires secret_ref, a wallet reference holding the bot token.")
        if not config["allowed_channels"]:
            fail("config", "Remote access requires at least one conversation ID in allowed_channels.")
    return config


def bot_token(value):
    """Validate the wallet value before it reaches an HTTP header; never echo it."""
    if not isinstance(value, str) or not BOT_TOKEN.fullmatch(value.strip()):
        fail("secret", "The wallet secret is not a Slack bot token (xoxb- followed by token characters).")
    return value.strip()


def redact(text, token=""):
    """Mask the token and anything shaped like a Slack token wherever it could appear."""
    text = "" if text is None else str(text)
    if token:
        text = text.replace(token, "***")
    return ANY_TOKEN.sub("xox?-***", text)


def clean(value, limit):
    """Bounded single-line provider text with control characters removed."""
    if not isinstance(value, str):
        return None
    value = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).strip()
    return value[:limit] if value else None


def identifier(value):
    if value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail("invalid_input", "request_id needs 1-128 ASCII letters, digits or ._:- characters.")
    return value


def message_ts(value, name="ts", required=False):
    """A Slack message timestamp such as 1712345678.123456; it is the message ID in a channel."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or not TS.fullmatch(value.strip()):
        fail("invalid_input", f"{name} must be a Slack message timestamp such as 1712345678.123456.")
    return value.strip()


def bound_ts(value, name):
    """oldest/latest: Unix seconds as a JSON number or a Slack timestamp string."""
    if value is None:
        return None
    if type(value) is int and 0 <= value <= 10 ** 12:
        return str(value)
    if type(value) is float and math.isfinite(value) and 0 <= value <= 10 ** 12:
        return f"{value:.6f}"
    if isinstance(value, str) and BOUND_TS.fullmatch(value.strip()):
        return value.strip()
    fail("invalid_input", f"{name} must be Unix seconds or a Slack timestamp string.")


def optional_bool(document, key):
    value = document.get(key)
    if value is not None and type(value) is not bool:
        fail("invalid_input", f"{key} must be a JSON boolean.")
    return value


def structured(value, primary):
    """Read a JSON object, or plain text as {primary: text}; '{' must start valid JSON."""
    if isinstance(value, Mapping):
        document = thaw(value)
        encoded(document)
        return document
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeError:
            fail("invalid_input", "Inputs must be UTF-8 text or a JSON object.")
    if not isinstance(value, str):
        fail("invalid_input", "Inputs must be UTF-8 text or a JSON object.")
    if len(value.encode("utf-8", errors="replace")) > INPUT_LIMIT:
        fail("invalid_input", "The input exceeds 256 KiB.")
    if value.lstrip().startswith("{"):
        document = strict_json(value.strip())
        if not isinstance(document, dict):
            fail("invalid_input", "A value starting with '{' must be one JSON object.")
        return document
    return {primary: value}


def target_channel(document, config):
    raw = document.get("channel")
    value = config["default_channel"] if raw is None else channel_id(raw)
    if value is None:
        fail("channel_required")
    if value not in config["allowed_channels"]:
        fail("channel_not_allowed")
    return value


def blocks_value(value):
    """Block Kit layout: a JSON array (or JSON text of one) of 1-50 objects with a string type."""
    if value is None:
        return None
    if isinstance(value, str):
        value = strict_json(value)
    if (not isinstance(value, list) or not 1 <= len(value) <= BLOCK_LIMIT
            or any(not isinstance(item, dict) or not isinstance(item.get("type"), str) or not item["type"]
                   for item in value)):
        fail("invalid_input", "blocks must be a JSON array of 1-50 Block Kit objects, each with a type.")
    return value


def text_value(value, required):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > TEXT_LIMIT:
        fail("invalid_input", "text must contain 1-40000 characters and cannot be blank.")
    return value


def message_request(value, config):
    """Build one message operation; the channel must be allowlisted before any wallet access."""
    document = structured(value, "text")
    operation = document.get("operation", "post_message")
    if operation not in MESSAGE_KEYS:
        fail("invalid_input", "operation must be post_message, update_message, read_history or read_replies.")
    unknown = set(document) - MESSAGE_KEYS[operation]
    if unknown:
        fail("invalid_input", "Unknown " + operation + " field: " + sorted(unknown)[0] + ".")
    request_id = identifier(document.get("request_id"))
    if operation in ("post_message", "update_message"):
        blocks = blocks_value(document.get("blocks"))
        text = text_value(document.get("text"), blocks is None)
        channel = target_channel(document, config)
        params = {"channel": channel}
        if text is not None:
            params["text"] = text
        if blocks is not None:
            params["blocks"] = blocks
        if operation == "post_message":
            thread = message_ts(document.get("thread_ts"), "thread_ts")
            if thread is not None:
                params["thread_ts"] = thread
            for key in ("reply_broadcast", "unfurl_links", "unfurl_media", "mrkdwn"):
                flag = optional_bool(document, key)
                if flag is not None:
                    params[key] = flag
            if params.get("reply_broadcast") and thread is None:
                fail("invalid_input", "reply_broadcast needs thread_ts.")
            method = "chat.postMessage"
        else:
            params["ts"] = message_ts(document.get("ts"), "ts", required=True)
            method = "chat.update"
        body = request_body(method, params)
        return {"operation": operation, "method": method, "channel": channel, "request_id": request_id,
                "params": params, "body_bytes": len(body)}
    channel = target_channel(document, config)
    limit = document.get("limit", config["max_messages"])
    if type(limit) is not int or not 1 <= limit <= config["max_messages"]:
        fail("invalid_input", f"limit must be an integer between 1 and max_messages ({config['max_messages']}).")
    params = {"channel": channel}
    if operation == "read_replies":
        if document.get("ts") is not None and document.get("thread_ts") is not None:
            fail("invalid_input", "Give the thread parent as ts or thread_ts, not both.")
        params["ts"] = message_ts(document.get("ts", document.get("thread_ts")), "ts", required=True)
    for key in ("oldest", "latest"):
        bound = bound_ts(document.get(key), key)
        if bound is not None:
            params[key] = bound
    inclusive = optional_bool(document, "inclusive")
    if inclusive is not None:
        params["inclusive"] = inclusive
    cursor = document.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not CURSOR.fullmatch(cursor)):
        fail("invalid_input", "cursor must be a next_cursor value returned by a previous read.")
    method = "conversations.history" if operation == "read_history" else "conversations.replies"
    return {"operation": operation, "method": method, "channel": channel, "request_id": request_id,
            "params": params, "limit": limit, "cursor": cursor}


def file_request(value, config):
    """Build one file upload; the path is resolved later inside file_root only."""
    document = structured(value, "path")
    unknown = set(document) - FILE_KEYS
    if unknown:
        fail("invalid_input", "Unknown file field: " + sorted(unknown)[0] + ".")
    path = document.get("path")
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(ord(char) < 32 for char in path):
        fail("invalid_input", "path must be a non-empty file path without control characters.")
    request_id = identifier(document.get("request_id"))
    channel = target_channel(document, config)
    filename = document.get("filename")
    if filename is not None and (not isinstance(filename, str) or not 1 <= len(filename) <= 255
                                 or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in filename)
                                 or filename.strip() in ("", ".", "..")):
        fail("invalid_input", "filename must be 1-255 characters without slashes or control characters.")
    title = document.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 255 or "\x00" in title):
        fail("invalid_input", "title must be text of 1-255 characters.")
    comment = document.get("initial_comment")
    if comment is not None and (not isinstance(comment, str) or not comment.strip() or len(comment) > COMMENT_LIMIT
                                or "\x00" in comment):
        fail("invalid_input", "initial_comment must be text of 1-4000 characters.")
    alt = document.get("alt_text")
    if alt is not None and (not isinstance(alt, str) or not alt.strip() or len(alt) > 1000 or "\x00" in alt):
        fail("invalid_input", "alt_text must be text of 1-1000 characters.")
    return {"operation": "upload_file", "method": "files.completeUploadExternal", "channel": channel,
            "request_id": request_id, "path": path.strip(), "filename": filename, "title": title,
            "initial_comment": comment, "thread_ts": message_ts(document.get("thread_ts"), "thread_ts"),
            "alt_text": alt}


def form_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def request_body(method, params):
    """Encode one Web API call: JSON for chat.* writes, form encoding for documented form methods."""
    if method in FORM_METHODS:
        body = urlencode([(key, form_value(value)) for key, value in params.items()]).encode("ascii")
    else:
        body = encoded(params, REQUEST_LIMIT + 1, "request_too_large")
    if len(body) > REQUEST_LIMIT:
        fail("request_too_large")
    return body


def content_type(method):
    return ("application/x-www-form-urlencoded" if method in FORM_METHODS
            else "application/json; charset=utf-8")


def upload_target(url, config):
    """The pre-signed upload URL must be HTTPS on a slack.com host (or the loopback test origin)."""
    if not isinstance(url, str) or not 1 <= len(url) <= 4096 or any(ord(char) <= 32 or ord(char) >= 127 for char in url):
        fail("protocol", "Slack returned an invalid upload URL; nothing was uploaded.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        fail("protocol", "Slack returned an invalid upload URL; nothing was uploaded.")
    host = (parsed.hostname or "").lower()
    scheme = parsed.scheme
    if parsed.username is not None or parsed.password is not None or parsed.fragment or not host or "\\" in url:
        fail("protocol", "Slack returned an invalid upload URL; nothing was uploaded.")
    api_scheme, api_host, api_port = split_origin(config["api_base_url"])
    if is_loopback(api_host):
        allowed = (scheme, host, port) == (api_scheme, api_host, api_port)
    else:
        allowed = scheme == "https" and port in (None, 443) and (host == "slack.com" or host.endswith(".slack.com"))
    if not allowed:
        fail("protocol", "Slack returned an upload URL outside slack.com; nothing was uploaded.")
    path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    return {"origin": origin_text(scheme, host, port), "path": path}


def map_status(status):
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 410: "not_found",
            413: "request_too_large", 429: "rate_limited"}.get(status, "provider_unavailable" if status >= 500 else "provider_error")


def provider_code(error):
    for code, names in PROVIDER_CODES.items():
        if error in names:
            return code
    return "provider_error"


def provider_result(response, token="", *, mutation=False):
    """Interpret one worker HTTP response with the documented Web API {"ok", "error"} envelope."""
    status = response.get("http_status")
    if type(status) is not int or not 100 <= status <= 599:
        fail("protocol", uncertain=mutation)
    body = response.get("body")
    header = response.get("retry_after_header")
    retry_header = header if type(header) is int and 0 <= header <= 86400 else None
    if not isinstance(body, dict) or type(body.get("ok")) is not bool:
        if status >= 500:
            fail("provider_unavailable", uncertain=mutation, http_status=status)
        if status in (400, 401, 403, 404, 410, 413, 429):
            fail(map_status(status), http_status=status, retry_after=retry_header if status == 429 else None)
        fail("protocol", uncertain=mutation and status < 300, http_status=status)
    if body["ok"] is True and 200 <= status < 300:
        return body
    error = body.get("error") if isinstance(body.get("error"), str) and PROVIDER_CODE.fullmatch(body["error"]) else None
    if status == 429:
        code = "rate_limited"
    elif error is not None:
        code = provider_code(error)
        if code == "provider_error" and status >= 400:
            code = map_status(status)
    elif body["ok"] is True:
        fail("protocol", uncertain=mutation, http_status=status)
    else:
        code = map_status(status) if status >= 400 else "provider_error"
    needed = body.get("needed") if isinstance(body.get("needed"), str) and SCOPES.fullmatch(body["needed"]) else None
    fail(code, uncertain=mutation and code == "provider_unavailable", http_status=status,
         retry_after=retry_header if code == "rate_limited" else None,
         slack_error=error, needed_scope=needed if code == "forbidden" else None)


def iso(ts):
    """ISO 8601 UTC date of a Slack timestamp, or None."""
    if not isinstance(ts, str) or not BOUND_TS.fullmatch(ts):
        return None
    seconds = float(ts)
    if not 0 <= seconds <= 2 ** 41:
        return None
    moment = datetime.fromtimestamp(int(seconds), timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def slack_id(value):
    return value if isinstance(value, str) and SLACK_ID.fullmatch(value) else None


def file_descriptor(value):
    """Slack file metadata only; private download URLs and contents are never copied."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not FILE_ID.fullmatch(value["id"]):
        return None
    result = {"id": value["id"]}
    for key, limit in (("name", 255), ("title", 255), ("mimetype", 128), ("filetype", 32)):
        text = clean(value.get(key), limit)
        if text:
            result[key] = text
    if type(value.get("size")) is int and value["size"] >= 0:
        result["size"] = value["size"]
    return result


def normalize_message(value):
    """Public message fields: IDs, text, thread data, files and reactions. Profiles are never copied."""
    if not isinstance(value, dict) or not isinstance(value.get("ts"), str) or not TS.fullmatch(value["ts"]):
        return None
    text = value.get("text") if isinstance(value.get("text"), str) else ""
    result = {"ts": value["ts"], "date": iso(value["ts"]), "type": clean(value.get("type"), 32) or "message",
              "subtype": clean(value.get("subtype"), 64), "user": slack_id(value.get("user")),
              "bot_id": slack_id(value.get("bot_id")), "text": text[:TEXT_LIMIT],
              "thread_ts": value["thread_ts"] if isinstance(value.get("thread_ts"), str) and TS.fullmatch(value["thread_ts"]) else None}
    if isinstance(value.get("parent_user_id"), str) and slack_id(value["parent_user_id"]):
        result["parent_user_id"] = value["parent_user_id"]
    for key in ("reply_count", "reply_users_count"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    if isinstance(value.get("latest_reply"), str) and TS.fullmatch(value["latest_reply"]):
        result["latest_reply"] = value["latest_reply"]
    edited = value.get("edited")
    if isinstance(edited, dict) and isinstance(edited.get("ts"), str) and TS.fullmatch(edited["ts"]):
        result["edited"] = {"user": slack_id(edited.get("user")), "ts": edited["ts"]}
    files = [item for item in (file_descriptor(entry) for entry in (value.get("files") or [])[:20]
                                if isinstance(value.get("files"), list)) if item]
    if files:
        result["files"] = files
    reactions = []
    for item in (value.get("reactions") or [])[:50] if isinstance(value.get("reactions"), list) else []:
        name = clean(item.get("name"), 100) if isinstance(item, dict) else None
        if name and type(item.get("count")) is int and item["count"] >= 0:
            reactions.append({"name": name, "count": item["count"]})
    if reactions:
        result["reactions"] = reactions
    if isinstance(value.get("blocks"), list) and value["blocks"]:
        result["has_blocks"] = True
    return result


def page_messages(body):
    """Validate one conversations.* page: (messages, has_more, next_cursor)."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        fail("protocol", "Slack returned a page without a messages array.")
    normalized = [item for item in (normalize_message(entry) for entry in messages) if item]
    metadata = body.get("response_metadata") if isinstance(body.get("response_metadata"), dict) else {}
    cursor = metadata.get("next_cursor")
    cursor = cursor if isinstance(cursor, str) and CURSOR.fullmatch(cursor) else None
    return normalized, body.get("has_more") is True, cursor


def normalize_read(request, messages, has_more, cursor, pages, attempts):
    result = {"request_id": request.get("request_id"), "operation": request["operation"],
              "channel": request["channel"], "count": len(messages), "messages": messages,
              "has_more": bool(has_more and cursor), "next_cursor": cursor if has_more else None,
              "pages": pages, "attempts": attempts}
    if request["operation"] == "read_replies":
        result["thread_ts"] = request["params"]["ts"]
    return result


def normalize_sent(body, request, attempts):
    """Public delivery receipt for post/update: provider IDs and the allowlisted channel, never the token."""
    channel, ts = body.get("channel"), body.get("ts")
    if not isinstance(channel, str) or not CHANNEL.fullmatch(channel) or not isinstance(ts, str) or not TS.fullmatch(ts):
        fail("protocol", "Slack accepted the request but returned an unexpected receipt; the change is likely.",
             uncertain=True)
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    thread = message.get("thread_ts") if isinstance(message.get("thread_ts"), str) else request["params"].get("thread_ts")
    result = {"request_id": request.get("request_id"), "operation": request["operation"], "channel": channel,
              "ts": ts, "thread_ts": thread if isinstance(thread, str) and TS.fullmatch(thread) else None,
              "date": iso(ts), "attempts": attempts}
    if request["operation"] == "post_message":
        result["bot_id"] = slack_id(message.get("bot_id"))
    return result


def upload_slot(body):
    """files.getUploadURLExternal result: (upload_url, file_id)."""
    url, file_id = body.get("upload_url"), body.get("file_id")
    if not isinstance(url, str) or not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        fail("protocol", "Slack returned no usable upload URL; nothing was uploaded.")
    return url, file_id


def share_ts(descriptor, channel):
    """Best effort: the share timestamp when Slack already reports it (sharing is asynchronous)."""
    shares = descriptor.get("shares") if isinstance(descriptor, dict) else None
    if not isinstance(shares, dict):
        return None
    for scope in ("public", "private"):
        entries = shares.get(scope, {}).get(channel) if isinstance(shares.get(scope), dict) else None
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            ts = entries[0].get("ts")
            if isinstance(ts, str) and TS.fullmatch(ts):
                return ts
    return None


def normalize_upload(body, request, file_id, filename, size, attempts):
    files = body.get("files")
    first = files[0] if isinstance(files, list) and files and isinstance(files[0], dict) else None
    if first is None or first.get("id") != file_id:
        fail("protocol", "Slack completed the upload but returned an unexpected file list; the share is likely.",
             uncertain=True)
    descriptor = file_descriptor(first) or {"id": file_id}
    descriptor.setdefault("name", filename)
    descriptor.setdefault("size", size)
    ts = share_ts(first, request["channel"])
    return {"request_id": request.get("request_id"), "operation": "upload_file", "channel": request["channel"],
            "ts": ts, "thread_ts": request.get("thread_ts"), "date": iso(ts) if ts else None,
            "file": descriptor, "attempts": attempts}


def retry_delay(error, attempts, config, deadline_left):
    """Seconds to wait before retrying a 429, or None when the bounds forbid another attempt."""
    if error.code != "rate_limited" or attempts > config["max_retries"]:
        return None
    wait = error.retry_after if error.retry_after is not None else 1
    if wait > config["max_retry_wait_sec"] or wait + 1 >= deadline_left:
        return None
    return float(wait)

# Slack

Post, update and read Slack messages with a bot token. The block posts text or
Block Kit messages (including thread replies), edits messages it posted, reads
bounded channel history and thread replies, and uploads local files to
explicitly allowed conversations. The bot token never leaves the BloxSmith
wallet, and remote access stays off until you enable it.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![Slack — post, update and read workspace messages](media/thumbnail.webp)](media/cover.png)

## When to use it

- Notify a team channel when a workflow finishes, fails or needs a decision.
- Reply in a thread, or keep one status message up to date with `update_message`.
- Read the latest messages of a channel or a thread to summarize them, search them
  or feed them to another block.
- Share a generated report, CSV or image in a channel.

The block only calls Slack when an input arrives. It does not receive events:
there is no Events API webhook and no Socket Mode in this version (see
[Limits and warnings](#limits-and-warnings)).

## Ports

| Direction | Port | Value |
| --- | --- | --- |
| Input | `message` | Plain text to post, or a JSON object choosing one operation (see below) |
| Input | `file` | A file path inside `file_root`, or a JSON object `{path, channel, title, ...}` |
| Output | `messages` | Result of `read_history` / `read_replies`: normalized messages and paging data |
| Output | `sent` | Receipt of `post_message`, `update_message` or `upload_file`: channel, `ts`, attempts |
| Output | `error` | Machine code and English message for any failed operation |

Both inputs are optional, and each fresh value triggers exactly one operation.
A retained value is never used again, so a replayed or forced activation does not
repeat a post. Two fresh inputs in the same activation are refused with
`one_operation` and nothing is sent. Business failures are published on `error`
and the run continues; they never stop an Active Runtime worker.

### `message` input

Plain text is posted as-is to `default_channel` with `chat.postMessage`. A value
whose first non-blank character is `{` must be one JSON object. Its `operation`
field selects what happens (default `post_message`); unknown fields are refused.
`channel` defaults to `default_channel` and must be listed in `allowed_channels`.
`request_id` (1–128 of `A-Za-z0-9._:-`) is echoed in every output.

| `operation` | Slack method | Fields |
| --- | --- | --- |
| `post_message` | `chat.postMessage` | `text` (1–40,000 characters) and/or `blocks`; `thread_ts` to reply in a thread; `reply_broadcast`, `unfurl_links`, `unfurl_media`, `mrkdwn` (JSON booleans) |
| `update_message` | `chat.update` | `ts` of a message posted by the bot, and new `text` and/or `blocks` |
| `read_history` | `conversations.history` | `limit`, `oldest`, `latest`, `inclusive`, `cursor` |
| `read_replies` | `conversations.replies` | `ts` (or `thread_ts`) of the thread parent, then `limit`, `oldest`, `latest`, `inclusive`, `cursor` |

- `blocks` is a [Block Kit](https://api.slack.com/block-kit) array of 1–50 objects
  (or the same array as JSON text). Slack validates the layout; give `text` as well
  so notifications have a readable fallback.
- `ts` and `thread_ts` are Slack message timestamps such as `1712345678.123456`;
  they are the message IDs within a channel and are returned by `sent` and `messages`.
- `oldest` and `latest` are Unix seconds (number) or Slack timestamps (string).
- `limit` is the number of messages wanted, from 1 to `max_messages`
  (default: `max_messages`).
- `cursor` continues a previous read: pass the `next_cursor` it returned.

To post text that itself starts with `{`, wrap it: `{"text": "{literal}"}`.

Examples:

```json
{"text": "Deploy *finished* :white_check_mark:", "thread_ts": "1712345678.123456"}
{"operation": "update_message", "ts": "1712345678.123456", "text": "Deploy finished in 4 min"}
{"operation": "read_history", "channel": "C0123456789", "limit": 50, "oldest": 1712300000}
{"operation": "read_replies", "ts": "1712345678.123456"}
```

### `file` input

A plain value is a path. Relative paths resolve inside `file_root`; absolute
paths must lie inside it. The JSON form accepts `path`, `channel`, `title`
(1–255 characters, default: the file name), `filename` (name shown in Slack),
`initial_comment` (message posted with the file, up to 4,000 characters),
`thread_ts` (share in a thread), `alt_text` (image description) and `request_id`.

Only non-empty, single-link regular files up to `max_file_bytes` are accepted.
Symbolic links (anywhere in the path), hard links, directories, devices, FIFOs,
`..` components and files outside `file_root` are refused before any network
access. The isolated network process reopens the file component by component
without following links and abandons the upload if the file changed.

The upload uses Slack's current external upload flow:

1. `files.getUploadURLExternal` with `filename` and `length` returns a
   pre-signed `upload_url` and a file ID;
2. the file bytes are sent with `POST` to that URL. **The bot token is not sent
   to the upload URL**, and the URL must be HTTPS on `slack.com` or a subdomain
   such as `files.slack.com`, otherwise nothing is uploaded;
3. `files.completeUploadExternal` with `files`, `channel_id` and the optional
   `initial_comment` and `thread_ts` shares the file.

If step 1 or 2 fails, nothing is shared. Slack shares files asynchronously, so
`sent.ts` is the share timestamp only when Slack already reports it; otherwise
it is `null` and `sent.file.id` identifies the file.

### `messages` output

```json
{
  "request_id": "weekly", "operation": "read_history", "channel": "C0123456789",
  "count": 2, "pages": 1, "attempts": 1, "has_more": true, "next_cursor": "bmV4dF90czoxNzEy...",
  "messages": [
    {"ts": "1712345678.123456", "date": "2024-04-05T19:34:38Z", "type": "message", "subtype": null,
     "user": "U0123456789", "bot_id": null, "text": "Release is out", "thread_ts": "1712345678.123456",
     "reply_count": 3, "reply_users_count": 2, "latest_reply": "1712346000.000100",
     "reactions": [{"name": "tada", "count": 4}], "has_blocks": true},
    {"ts": "1712340000.000200", "date": "2024-04-05T18:00:00Z", "type": "message", "subtype": null,
     "user": "U0123456789", "bot_id": null, "text": "Plan attached", "thread_ts": null,
     "files": [{"id": "F0123456789", "name": "plan.pdf", "title": "Plan", "mimetype": "application/pdf",
                "filetype": "pdf", "size": 52344}]}
  ]
}
```

History is newest first, as Slack returns it; replies start with the thread
parent, then the replies oldest first (`thread_ts` is added to the result).
Messages keep their IDs, text (with Slack markup such as `<@U0123>`),
thread data, file metadata, edit marks and reaction counts. User profiles, real
names, avatars, private download URLs and the Block Kit structure are not
copied (`has_blocks` tells that a layout exists). `has_more` with `next_cursor`
means more messages are available: send them back as `cursor` to continue.

### `sent` and `error` outputs

`sent`: `{"request_id", "operation": "post_message|update_message|upload_file",
"channel", "ts", "thread_ts", "date", "attempts"}`, plus `bot_id` for posts and
`file` (`id`, `name`, `title`, `mimetype`, `size`) for uploads.

`error`: `{"code", "message", "uncertain", "operation", "request_id", "channel"}`
and, when known, `http_status`, `retry_after`, `slack_error` (Slack's own error
code, such as `channel_not_found`), `needed_scope` (for `missing_scope`) and
`attempts`. `uncertain: true` means Slack may have applied a write; check the
channel before posting again.

| Code | Meaning |
| --- | --- |
| `disabled`, `secret`, `invalid_input`, `request_too_large`, `channel_required`, `channel_not_allowed`, `one_operation` | Refused locally before any request |
| `file_root_required`, `file_not_found`, `file_not_allowed`, `file_too_large`, `file_empty`, `file_changed`, `upload_rejected` | File policy or upload step; nothing shared |
| `unauthorized` | `invalid_auth`, `not_authed`, `token_revoked`, `account_inactive`, ... |
| `forbidden` | `missing_scope` (see `needed_scope`), `not_in_channel`, `is_archived`, `cant_update_message`, `edit_window_closed`, ... |
| `not_found` | `channel_not_found`, `message_not_found`, `thread_not_found`, HTTP 404 (`unknown_method`) |
| `bad_request` | `invalid_arguments`, `invalid_blocks`, `msg_too_long`, `no_text`, `invalid_cursor`, ... |
| `rate_limited` | HTTP 429 (`ratelimited`) after the allowed retries, or a `Retry-After` above `max_retry_wait_sec` |
| `provider_error` | Any other Slack error code (kept in `slack_error`) |
| `provider_unavailable`, `network`, `timeout`, `cancelled`, `protocol`, `tls` | Transport outcome; `uncertain` tells whether a write may have been applied |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Remote access switch. While off, nothing is sent to or read from Slack |
| `secret_ref` | empty | Wallet reference such as `secret://workspace/slack_bot`, holding the bot token (`xoxb-...`) |
| `allowed_channels` | `[]` | Conversation IDs (`C...`, `G...`, `D...`, up to 100). Required when enabled; applies to every operation |
| `default_channel` | empty | Conversation used when an input has no `channel`; must be allowlisted |
| `file_root` | empty | Directory whose files can be uploaded. Empty disables the `file` input |
| `max_messages` | 200 | Upper bound and default `limit` of one read, 1–1000 |
| `timeout_sec` | 30 | Total deadline of one operation (all pages, upload steps and 429 waits), 5–300 |
| `page_size` | 100 | `limit` of one `conversations.*` page, 1–200 |
| `max_pages` | 5 | Pages fetched by one read, 1–20; the read then returns `next_cursor` |
| `max_retries` | 2 | Retries after HTTP 429 only, 0–5 |
| `max_retry_wait_sec` | 30 | Longest `Retry-After` the block will wait, 1–120 |
| `max_file_bytes` | 10485760 | Upload limit, 1 KiB–100 MiB |
| `max_response_bytes` | 2097152 | Limit of one Slack response, 64 KiB–8 MiB |
| `api_base_url` | `https://slack.com` | Web API origin. Only `https://slack.com`, or a loopback test double |
| `ca_file` | empty | Optional absolute PEM file, for an HTTPS loopback test double only |

Settings are validated on Apply and again before a Run starts; invalid settings
never reach Slack. Restart a loaded Run to use changed settings. The total size
of one read is bounded by `max_pages × max_response_bytes` (and 16 MiB of
normalized messages).

## Example

1. Create a Slack app, add the bot token scopes you need (see below), install it
   in your workspace and copy the **Bot User OAuth Token** (`xoxb-...`).
2. Store it in the BloxSmith wallet as `slack_bot`, and invite the bot to the
   channel (`/invite @your-bot`).
3. Copy the channel ID (channel details, at the bottom; it looks like
   `C0123456789`) and set `secret_ref = secret://workspace/slack_bot`,
   `allowed_channels = ["C0123456789"]`, `default_channel = C0123456789` and
   `enabled = true`.
4. Wire a Text block containing `Build finished` to `message`: One Shot posts it
   and `sent` returns `{"channel": "C0123456789", "ts": "1712345678.123456", ...}`.
5. To summarize the channel, send `{"operation": "read_history", "limit": 50}`
   and wire `messages` to an LLM or table block.

| Operation | Bot token scopes |
| --- | --- |
| `post_message`, `update_message` | `chat:write` |
| `read_history`, `read_replies` | `channels:history` (public), `groups:history` (private), `im:history` (DM), `mpim:history` (group DM) |
| `upload_file` | `files:write` |

When a scope is missing, Slack answers `missing_scope` and the `error` output
names it in `needed_scope`.

## Limits and warnings

- **Receiving events is not supported.** The block never opens a listening port, so
  the Events API (webhooks) cannot be used; Socket Mode is out of scope for this
  version. To react to new messages, read the history periodically with `oldest`.
- Conversations are addressed by ID only; `#channel-name` targets are refused, so a
  renamed channel cannot redirect messages.
- Only bot tokens (`xoxb-`) are accepted; user tokens (`xoxp-`) are refused.
- `update_message` works only on messages posted by the same bot (Slack rule).
- Deleting messages, reactions, user lookups, downloading files, scheduled messages,
  ephemeral messages and modals are not implemented.
- No HTTP proxy support; the host needs direct outbound HTTPS to `slack.com` and
  `files.slack.com`.
- Only HTTP 429 is retried. A write whose `error` reports `uncertain: true` is never
  retried automatically.
- Message text read from Slack is untrusted data. Do not feed it to tools that execute
  commands without review.

## Dependencies

Python standard library only (`http.client`, `ssl`, `json`); no `requirements.txt`.
Each HTTP exchange runs in a short-lived isolated Python process (`python -I`), so
Cancel and Stop terminate it immediately.

## Security

- The token is read from the wallet when an operation runs and is passed to the
  network process through a private pipe, never through arguments, environment,
  settings, outputs, logs or run metadata. It is sent only in the
  `Authorization: Bearer` header to `https://slack.com/api/...`, and anything shaped
  like a Slack token is redacted from errors.
- Every target conversation must be allowlisted; a refused conversation is reported
  before the wallet is read.
- The pre-signed upload URL receives the file bytes only, never the token, and must be
  HTTPS on `slack.com` or one of its subdomains.
- TLS certificates and host names are always verified; there is no insecure mode.
- Redirects, cookies and compression are not used.

## Slack Web API usage

Methods: `chat.postMessage` and `chat.update` (JSON bodies),
`conversations.history`, `conversations.replies`, `files.getUploadURLExternal` and
`files.completeUploadExternal` (form-encoded bodies), all with `POST` on
`https://slack.com/api/<method>` and `Authorization: Bearer <token>`, plus the
upload `POST` to the returned `upload_url`. Responses follow the documented
`{"ok": true, ...}` / `{"ok": false, "error": "..."}` envelope; HTTP 429 carries
`Retry-After`; reads paginate with `cursor` and `response_metadata.next_cursor`.
The deprecated `files.upload` method is not used. The Slack Web API has no version
number: the block relies on these method contracts as Slack documents them.

**Real-provider qualification is pending.** All tests use a loopback double that
reproduces these documented request and response shapes; no real workspace,
token or channel was contacted.

## Framing decisions

- One block, five operations: the `message` input selects `post_message`,
  `update_message`, `read_history` or `read_replies` with `operation` (plain text
  posts); the `file` input uploads.
- Remote access needs `enabled`, a wallet `secret_ref` and at least one allowed
  conversation ID. One allowlist governs writes and reads, so a graph cannot
  redirect messages or read other conversations.
- `api_base_url` is fixed to `https://slack.com`; only loopback hosts (test
  doubles) may replace it, so the token can never be sent to another server.
- Only HTTP 429 is retried (Slack did not process the request), honouring
  `Retry-After` within `max_retry_wait_sec`; every other failure after a write was
  sent is reported as possibly applied.
- Reads are bounded by `limit`, `max_messages`, `page_size`, `max_pages`, the
  response size and the deadline, and return `next_cursor` instead of silently
  stopping. A page failure returns an error, never a truncated result.
- No inbound listener: no Events API, no Socket Mode in this version.

## Tests

`tests/F5.01_slack.py` checks settings, origin rules, input contracts, request
encoding, provider error mapping, message normalization, upload URL policy, file
policy and hooks without a socket. `tests/F5.02_slack_runtime.py` runs the real
network process against the loopback double (HTTP and HTTPS): posts, threads,
updates, history and replies paging, auth errors (`invalid_auth`), `missing_scope`,
`not_in_channel`, 404, 429 with `Retry-After`, 5xx, lost connections, oversized
responses, cancellation, deadlines, the three-step upload without the token on the
upload URL, then real graphs in One Shot and Active Runtime for bundled, managed
and linked packages. `tests/F8.01_slack_ui.py` covers the English and French forms
at desktop and mobile widths. They run in the private BloxSmith workspace against a
pinned framework; the initial development version does not imply a release.

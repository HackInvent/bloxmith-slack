"""Slack Web API messages and files through a wallet-held bot token and owned child processes."""

import json
from bloxsmith_app.block_api import APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation, BlockRuntimeResult
from . import logic, transport, ui


# FB1 - Keep remote access off by default; require a wallet xoxb- bot token, the official https://slack.com origin (loopback only for tests) and allowlisted conversation IDs.
# FB2 - Post messages (chat.postMessage: text, Block Kit blocks, thread_ts replies) and update them (chat.update) in allowlisted channels only.
# FB3 - Read bounded channel history (conversations.history) and thread replies (conversations.replies) with cursor pagination and normalized messages.
# FB4 - Upload one bounded file from file_root through files.getUploadURLExternal, the pre-signed upload URL and files.completeUploadExternal, never sending the token to the upload host.
# FB5 - Bound request, response, time, pages and retries; honour Retry-After on HTTP 429, never replay ambiguous writes, report coded English errors without the token.
# FB6 - Apply one contract in both runtimes and every package origin; one fresh input triggers exactly one operation.
# FB7 - Own translated responsive settings with draft Apply and release-scoped assets.
class SlackBlock(BlockDefinition):
    """Message text read from Slack is untrusted data, never instructions for this block."""

    kind = "slack"

    def ui_assets(self, surface="modal"):
        """Every installation uses the same static release manifest."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        """Validate settings without wallet, network or storage access; no listener is ever declared."""
        logic.configuration(context.config)
        return BlockRuntimePreparation()

    def outputs(self, context, values):
        return [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, content_type=APPLICATION_JSON,
                                   value=json.dumps(values[port.name], ensure_ascii=False, allow_nan=False))
                for port in context.output_ports if port.name in values]

    def fresh_inputs(self, context):
        """Only fresh deliveries count: retained values never repeat an operation."""
        names = {int(port.id): str(port.name) for port in context.input_ports}
        if context.input_events:
            return [(names.get(event.input_port_id), event.value) for event in context.input_events]
        fresh = []
        for ident, name in names.items():
            attribute = context.input_attribute(name, str(ident))
            if attribute is not None and attribute.status == "updated" and attribute.value not in (None, ""):
                fresh.append((name, attribute.value))
        return fresh

    def execute_runtime(self, context):
        """One fresh message or file per activation; business failures go to the error port."""
        request, token = {}, ""
        try:
            config = logic.configuration(context.config)
            items = self.fresh_inputs(context)
            if not items:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(items) != 1:
                logic.fail("one_operation")
            name, value = items[0]
            if name not in ("message", "file"):
                logic.fail("invalid_input", "Unknown input port.")
            if not config["enabled"]:
                logic.fail("disabled")
            request = logic.message_request(value, config) if name == "message" else logic.file_request(value, config)
            located = transport.locate(request["path"], config) if name == "file" else None
            try:
                token = logic.bot_token(context.resolve_secret(config["secret_ref"]))
            except logic.SlackError:
                raise
            except Exception:
                logic.fail("secret")
            cancel = context.services.get("cancel_requested", lambda: False)
            operation = request["operation"]
            if operation in ("read_history", "read_replies"):
                result = transport.read(config, token, request, cancel)
                port, state = "messages", "read"
                log = f"[slack] {request['method']} returned {result['count']} message(s) in {result['pages']} page(s)."
            elif operation == "upload_file":
                result = transport.upload(config, token, request, located, cancel)
                port, state = "sent", "sent"
                log = f"[slack] file upload shared after {result['attempts']} request(s)."
            else:
                result = transport.post(config, token, request, cancel)
                port, state = "sent", "sent"
                log = f"[slack] {request['method']} delivered after {result['attempts']} attempt(s)."
            summary = {"status": state, "operation": operation, "attempts": result["attempts"]}
            if port == "messages":
                summary.update(count=result["count"], pages=result["pages"])
            return BlockRuntimeResult(status="success", outputs=self.outputs(context, {port: result}),
                content_type=APPLICATION_JSON, last_message=operation,
                metadata={self.kind: summary}, logs=[log])
        except logic.SlackError as exc:
            error = exc
        except (ValueError, TypeError, OSError, RuntimeError):
            # Raw exception text could carry paths or provider data: publish a fixed diagnostic.
            error = logic.SlackError("internal", uncertain=bool(token) and request.get("method") in logic.MUTATIONS)
        operation = request.get("operation")
        envelope = error.envelope(operation=operation, request_id=request.get("request_id"),
                                  channel=request.get("channel"))
        text = context.redact_secrets(logic.redact(json.dumps(envelope, ensure_ascii=False, allow_nan=False), token))
        return BlockRuntimeResult(status="success",
            outputs=[BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, value=text,
                                        content_type=APPLICATION_JSON)
                     for port in context.output_ports if port.name == "error"],
            content_type=APPLICATION_JSON, last_message=error.code,
            metadata={self.kind: {"status": "error", "operation": operation, "code": error.code,
                                  "uncertain": error.uncertain}},
            logs=[f"[slack-error] {operation or 'input'}: {error.code}"])

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Apply validates settings only; it never contacts Slack or reads the wallet."""
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    detail = str(exc) if isinstance(exc, logic.SlackError) else logic.MESSAGES["config"]
                    return {"error": self.translate("block.slack.error", {"detail": detail},
                                                    fallback="Slack settings rejected: " + detail)}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action,
                                          values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Show the access mode and allowlist size, never the token reference or channel IDs."""
        config = {**self.default_config(), **(node.get("config") or {})}
        channels = config.get("allowed_channels")
        count = len(channels) if isinstance(channels, list) else 0
        # The key and its parameters travel with the text so the editor re-renders it in the
        # interface language, even when a package process rendered it with the English pivot.
        if config.get("enabled") is not True:
            key, params, fallback = "block.slack.card_disabled", {}, "Remote access off"
        else:
            key, params = "block.slack.card_enabled", {"count": count}
            fallback = f"Enabled · {count} channel" + ("" if count == 1 else "s")
        return ui.card(self, node, key, params, self.translate(key, params, fallback=fallback))

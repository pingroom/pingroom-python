# Changelog

## 0.1.0 (2026-11-11)

First release. It covers the same surface as the TypeScript SDK (`@pingroom/sdk` 0.9.0 and its unreleased additions of 2026-11-11), for the calls listed here.

### Added

- `PingRoom` (blocking) and `AsyncPingRoom` (asynchronous) clients on `httpx`, sharing one implementation of every operation. Options: `token`, `base_url` (default `https://api.pingroom.io`, or the `PINGROOM_API_URL` / `PINGROOM_BASE_URL` environment variables), `timeout`, `transport`, `user_agent` (default `pingroom-python/<version>`) and `allow_insecure`.
- HTTPS is required for the API and for webhook URLs, except to loopback hosts. No redirect is ever followed (`redirect_refused`). The token is kept in an object that renders as `[redacted]` and never appears in `repr`, in an exception message or in a pickle.
- `PingRoomError` (`status`, `code`, `message`, `retry_after`, `body`) with `PingRoomTimeoutError`, `PingRoomNetworkError` and `PingRoomCancelledError`, and the TypeScript SDK's error-code tuples (`HANDOFF_ERROR_CODES`, `REPLACE_ERROR_CODES`, `WITHDRAW_ERROR_CODES`, `METRIC_ERROR_CODES`, `LIVE_CLAIM_ERROR_CODES`, `PING_ACTION_ERROR_CODES`, `ROOM_SCOPED_ERROR_CODES`, `COMMON_ERROR_CODES`, `REQUEST_LIMIT_ERROR_CODES`, `QUICK_ACTION_ERROR_CODES`, `AGENT_INBOX_ERROR_CODES`).
- `broadcast()` with `title`, `data`, `tags`, `correlation_id`, `reply_to`, `action_number`, `action_icon`, `attachment_ids`, `requires_ack`, `ack_mode`, `is_urgent`, `ack_timeout_seconds`, `replace_key` and `idempotency_key`; `link_ping()`, `location_ping()`, `extract_location_ping()` and the `ping_action.open_url()` / `ping_action.webhook()` button builders.
- `notifications`: `list`, `get`, `wait`, `listen`, `wait_for_acknowledgement`, `replace` and `withdraw`.
- `approvals` (`request`, `get`, `wait`, `wait_for_decision`), `questions` (`ask`, `get`, `wait`, `wait_for_answer`, `list`, `cancel`, with polls), `handoffs` (`request_ack`, `ask`, `get`, `wait_for_result`, `list`).
- Wait loops with the TypeScript SDK's timing: polls at least 2.1 s apart, exponential backoff capped at 30 s, `Retry-After` honoured, an outage bound of 5 minutes past `expires_at` (15 minutes when none was read), `on_retry`, and cancellation (`threading.Event` on the blocking client, `asyncio.Event` or task cancellation on the asynchronous one).
- `metrics` (`list`, `set`, `delete`), `attachments` (`upload`, `content`, `manifest`, `delete`) and `live` (`push`, `get`, `claim`) with the `live_status` builders for every template: `status`, `steps`, `progress`, `metrics`, `countdown`, `question`, `matchup`, `claim`, `timer`, `value`, `stats`, plus `alert`, `done` and `failed`.
- `send_incoming_webhook()` and `send_incoming_webhook_async()` with every field of `INCOMING_WEBHOOK_FIELDS` and `replace_key`, and the same local checks as `broadcast()`.
- `verify_webhook_signature()` for the V1 and V2 schemes (also used by a Ping's signed button deliveries): constant-time comparison, a replay window, `require_v2`, and raw bytes only.
- Local checks that match the server's rules and never go beyond them: message and title length, the `data` size (8 KB as PHP's `json_encode` measures it) and key count, tags, `replace_key`, action slots, idempotency keys, buttons (`data.actions`) and room metrics. They are tested against the cross-repo contract fixtures (`tests/contracts/`) and against the requests the TypeScript SDK sends (`tests/golden/`).
- Type hints throughout, with a `py.typed` marker. Python 3.9 to 3.13.

### Not included yet

Agent registration, pairing and OAuth helpers, rooms, incoming-webhook and quick-action management, the agent directory, the agent profile, the Agent Inbox, redeeming codes, and the MCP JSON-RPC client.

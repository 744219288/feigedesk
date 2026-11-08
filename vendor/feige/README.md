# Vendored eCan.ai Feige protocol modules

Source repository: https://github.com/scszcoder/eCan.ai

Fixed revision: `bb8db95c6b7f8fbde3fa55052a733ef27e438c77`.
See `LICENSE` (MIT, copyright scszcoder) and `UPSTREAM.json` for original
download URLs and SHA256 hashes. Only `ws_reader.py`, `ws_sender.py` and
`ws_protocol_watch.py` from that manifest are shipped here.

Local modification: `ws_reader.extract_messages` requires `talk_id`; it no
longer falls back to shop-level `pigeon_cid` as a conversation identifier.
No eCan cloud login, paid skill, prompt, credential or subscription is copied.
The reader additionally prefers the official `b_temp_track_message_id` as the
stable business message identity when present, while retaining field 3 as
`wire_msg_id` for exact UI/transport aliases. A valid client message ID is
also retained. Identical content with distinct identities is never collapsed.
The optional upstream protocol-watch journal imports fail closed when the
full eCan app is absent. Parsing and sending do not require that journal.

FeigeDesk owns the standalone lifecycle and transport adapter. Sending uses
the site's native composer. No manual-send bootstrap or 180-second template
expiry is required. A live WebSocket message binds talk_id to the exact source
message ID; the visible thread must contain that same last customer message
before filling and immediately before clicking Send. Display names are never
used to choose a recipient. Existing manual drafts are preserved.

Confirmation requires the browser's outgoing conversation/client message ID
and a matching inbound server echo on the same socket. Unknown outcomes are
never retried automatically. Missing source identity causes a definite refusal
before sending; missing echo after submission produces an unknown result.

Tests cover synthetic offline frames and local HTML fixtures in Chrome. A real
authenticated workspace and structural selectors have been observed; delivery
and live protocol confirmation still require a new test message from the user.

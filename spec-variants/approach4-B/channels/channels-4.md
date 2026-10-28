# Channels.4: search, pins, mentions and read state

Spec 4 of 4 for Component B (channels and message history). Sections: 5, 6, 7 (numbering matches the check output).
Check: `bash /spec/channels/checks/check_channels_4.sh`.

Do not start this spec until `check_channels_3.sh` prints `RESULT: ... 0 failed`.

Rule 7.2 gets the head from a WebSocket `subscribed` frame, so `channels-3.md` must pass first.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | What it does |
|---|---|
| `message_to_json(message)` | Search results and pins |
| `parse_limit / encode_cursor / decode_cursor` | Paged search and pin list |
| `can_read_channel(channel, user_id)` | Search access and the pin list |
| `head_event_id(channel_id)` | Capping `last_read_event_id` (7.3) |

## Adds

| Function | What it does |
|---|---|
| `resolve_mentions(body, channel, author_id)` | Section 6; call it when posting and editing |
| `search_messages(user, q, workspace_id, limit, cursor)` | Rules 5.1 to 5.3 |
| `pin_message / unpin_message / list_pins` | Rules 5.4 to 5.6 |
| `read_state(user_id, channel_id)` | Section 7; also used by `GET /api/workspaces/{slug}` |

## 5. Search and pins

5.1 **Search.** `GET /api/search?q=<text>` (paged) returns `200 {results, next_cursor}`:
    `MessageObj`s whose `body` contains the text (case-insensitive), newest-first, from every
    channel the caller can read (public channels of their workspaces, and private channels they
    are in).

5.2 **Search access.** Messages in a private channel the caller is not in are never returned.

5.3 **Search by workspace.** `&workspace_id=<id>` returns only messages from that workspace.
    Paging works as in section 0 (`limit=1` gives a `next_cursor`).

5.4 **Pin.** `POST /api/messages/{id}/pin` → `200 {pin: {message_id, pinned_by, pinned_at}}`.
    Pinning it again → `200`, and it is still listed only once.

5.5 **Pin list.** `GET /api/channels/{id}/pins` (paged) → `200 {pins: [{message, pinned_by,
    pinned_at}], next_cursor}`, most recently pinned first. A non-member of a private channel →
    `403`.

5.6 **Unpin.** `DELETE /api/messages/{id}/pin` → `200 {"unpinned": true}`, and the message is
    no longer in the pin list.

## 6. Mentions

`mentions` on a `MessageObj` is the list of user ids the body mentions, resolved when the
message is posted (and again when it is edited).

6.1 **Mention.** `@username` (preceded by the start of the body or a character that is not a
    letter, digit, `_`, `.` or `@`) adds that user's id. Punctuation after it is fine:
    `"hi @bob, see this"` mentions bob.

6.2 **Deduplicated, never self.** `"@bob @bob"` lists bob once. The author mentioning
    themselves is never listed.

6.3 **Not a mention.** `"mail bob@example.com"` and `"x@bob"` (in-word) mention nobody. A
    username that does not exist, or a user who is not a member of the channel's workspace, is
    left out.

6.4 **@channel.** `@channel` adds every current member of the channel except the author,
    deduplicated with any `@username` in the same body.

6.5 **Mention count.** A mention of bob raises bob's `mention_count` (section 7) for that
    channel by one. Mentioning yourself never raises your own.

## 7. Read state

`read_state` is `{channel_id, last_read_event_id, unread_count, mention_count}`.
`last_read_event_id` is an `event_id` of the channel's event sequence (section 8); it starts
at `0`. `unread_count` is the number of live messages (top-level and replies) in the channel
whose creation event is after `last_read_event_id` and whose author is not the caller.
`mention_count` is how many of those mention the caller.

7.1 **Get.** `GET /api/channels/{id}/read` → `200 {read_state}` with all four fields as
    integers (except `channel_id`). Three new messages from another user, one mentioning the
    caller, give `unread_count: 3`, `mention_count: 1`.

7.2 **Mark read.** `POST /api/channels/{id}/read` `{last_read_event_id: <head>}` (head from
    `subscribed`, section 8) → `200 {read_state}` with that value and `unread_count: 0`,
    `mention_count: 0`.

7.3 **Never backwards.** Posting a smaller `last_read_event_id` afterwards → `200` with the
    larger value kept. A value above the head is stored as the head. A missing, negative or
    non-integer value → `400`.

7.4 **Embedded.** `read_state` in `GET /api/workspaces/{slug}` has the same entry for that
    channel.

Every request must answer within 5 seconds.

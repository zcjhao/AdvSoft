# Channels.2: messages, threads and paging

Spec 2 of 4 for Component B (channels and message history). Sections: 3, 4 (numbering matches the check output).
Check: `bash /spec/channels/checks/check_channels_2.sh`.

Do not start this spec until `check_channels_1.sh` prints `RESULT: ... 0 failed`.

The paging rules (limit, cursor, next_cursor) are in section 0 of `channels-1.md`, under "Paging".

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | What it does |
|---|---|
| `get_channel(channel_id)` | 404 for unknown channels |
| `can_read_channel(channel, user_id)` | 403 for private channels |
| `append_event(...)` | One event per new message, reply, edit, delete and reaction change |

## Adds

| Function | What it does |
|---|---|
| `message_to_json(message)` | `MessageObj` with all twelve fields (`mentions` stays `[]` until `channels-4.md`) |
| `parse_limit(request)` | Default 50, 1..200, else answers `400` |
| `encode_cursor(position) / decode_cursor(text)` | Opaque base64 JSON; decode answers `400` for anything it did not issue |
| `reactions_of(message_id)` | `[{emoji, count, user_ids}]` |

## 3. Messages and threads

3.1 **Post.** `POST /api/channels/{id}/messages` `{body}` from a channel member returns
    `201 {message}`, a full `MessageObj`: `body` as sent, `author_id` the caller,
    `author.username` the caller's username, `parent_id: null`, `reply_count: 0`,
    `edited_at: null`, `files: []`, `reactions: []`, `mentions: []`, `created_at` an ISO UTC
    string ending in `Z`.

3.2 **Empty body.** `body` of `""`, `"   "`, or missing → `400`.

3.3 **Private channel.** A user who is not a member of a private channel gets `403` when
    posting to it or listing its messages.

3.4 **Reply.** `{body, parent_id: <top-level message id>}` → `201` with that `parent_id`. The
    parent's `reply_count` (in the channel listing) goes up by one per reply.

3.5 **Replies list.** `GET /api/messages/{parent_id}/replies` (paged) returns
    `200 {replies, next_cursor}` with the replies oldest-first. Replies are not in the channel's
    top-level listing (rule 4.1). Unknown parent → `404`.

3.6 **Edit.** `PATCH /api/messages/{id}` `{body}` by the author → `200 {message}` with the
    new `body` and `edited_at` set. By anyone else → `403`.

3.7 **Delete.** `DELETE /api/messages/{id}` by the author or the workspace owner →
    `200 {"deleted": true}`; again → `200 {"deleted": true}`. By anyone else → `403`. The
    message disappears from listings. Editing it afterwards → `409`.

3.8 **Reactions.** `POST /api/messages/{id}/reactions` `{emoji}` → `200 {reactions}` with
    `[{emoji, count: 1, user_ids: [caller]}]`; the same call again keeps `count: 1`. A second
    user → `count: 2`. `DELETE /api/messages/{id}/reactions/{emoji}` removes the caller's
    reaction and returns `200 {reactions}`.

## 4. History and paging

4.1 **List.** `GET /api/channels/{id}/messages` (paged) returns `200 {messages, next_cursor}`
    with top-level messages newest-first. When everything fits, `next_cursor` is `null`.

4.2 **Pages.** With 5 messages and `limit=2`: page 1 has the newest 2 and a string
    `next_cursor`; following the cursors gives 2, then 1 with `next_cursor: null`. No message
    appears twice and none is skipped.

4.3 **Stable cursor.** A cursor taken before new messages are posted returns exactly the same
    page afterwards.

4.4 **Any node.** A cursor issued by `8000` returns the same page on `8001` and `8002`.

4.5 **Bad cursor.** `cursor=not-a-cursor` → `400`.

4.6 **Limit.** `limit=0`, `limit=201`, `limit=-1` and `limit=abc` → `400`. `limit=200` →
    `200`. Without `limit`, a channel with 55 messages returns 50 and a `next_cursor`.

4.7 **No offset paging.** `?offset=2` and `?page=2` return the same first page as no
    parameter.

Every request must answer within 5 seconds.

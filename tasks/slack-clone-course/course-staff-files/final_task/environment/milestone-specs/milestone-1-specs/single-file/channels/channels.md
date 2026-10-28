# Component B: channels and message history

Routes covered:

- `POST /api/workspaces/{slug}/channels`, `GET /api/workspaces/{slug}`
- `PATCH /api/channels/{id}`, `POST /api/channels/{id}/join`, `POST /api/channels/{id}/leave`,
  `GET /api/channels/{id}/members`, `POST /api/channels/{id}/archive`,
  `POST /api/channels/{id}/unarchive`
- `POST /api/channels/{id}/messages`, `GET /api/channels/{id}/messages`,
  `GET /api/messages/{id}/replies`, `PATCH /api/messages/{id}`, `DELETE /api/messages/{id}`,
  `POST /api/messages/{id}/reactions`, `DELETE /api/messages/{id}/reactions/{emoji}`
- `GET /api/search`
- `POST /api/messages/{id}/pin`, `DELETE /api/messages/{id}/pin`, `GET /api/channels/{id}/pins`
- `GET /api/channels/{id}/read`, `POST /api/channels/{id}/read`
- WebSocket `/api/ws?token=<bearer>`: `subscribe`, `resume`, durable events

Check: `bash /spec/channels/checks/check_channels.sh`. The rule numbers below match its output.

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on
`127.0.0.1:8000`, `:8001`, and `:8002`.

## 0. Conventions (apply to every route in this file)

- All routes are under `/api/...` and return JSON with a single top-level resource key
  (`channel`, `channels`, `members`, `message`, `messages`, `replies`, `results`, `pins`, `pin`,
  `reactions`, `read_state`). `GET /api/workspaces/{slug}` is the one route with three keys.
- Every route needs `Authorization: Bearer <token>`. Missing or invalid token → `401`.
- Status codes: `200` read/update, `201` create, `400` validation, `401` no/bad auth, `403`
  authenticated but not allowed, `404` unknown id, `409` duplicate or editing a deleted message,
  `423` write to an archived channel.
- When several errors apply, answer in this order: `401` → `404` → `403` → `400` → `423` →
  `409`.
- Timestamps are ISO 8601 UTC strings ending in `Z`, for example `2026-05-16T08:07:34.123Z`.
- Ids are integers or strings, but always the same value on every node.
- **One shared store.** Anything created on one node must be visible on the other two
  immediately. Keep all state (including the event log of section 8) in one store the three
  node processes share, for example one SQLite file under `/app/data`, never in per-process
  memory.

### Objects

- `UserObj`: `{id, username, display_name, timezone, avatar_url, status_text, status_emoji}`.
  `display_name` defaults to the username, `timezone` to `"UTC"`, the other three to `""`.
- `ChannelObj`: `{id, workspace_id, name, is_private, is_dm, topic, is_archived}`. `topic`
  defaults to `""`. `is_dm` is `false` for every channel in this file.
- `MessageObj`: `{id, channel_id, author_id, author, body, parent_id, reply_count, created_at,
  edited_at, files, reactions, mentions}`. `author` is the author's `UserObj`. `parent_id` is
  `null` for a top-level message. `reply_count` is the number of live replies. `edited_at` is
  `null` until the first edit. `files` is `[]`. `reactions` is `[{emoji, count, user_ids}]`.
  `mentions` is a list of user ids (section 6).

### Paging (every list route marked "paged")

- Query `?limit=N&cursor=<string>`. The response adds a top-level `next_cursor`: an opaque
  string when more results exist, `null` on the last page. No `cursor` → first page.
- `limit` defaults to `50`, max `200`. `limit` of `0`, below `0`, above `200`, or not a number
  → `400`.
- The cursor is opaque: encode the position (for example the id of the last item returned)
  as base64 JSON. Any node must accept a cursor issued by any other node. A cursor keeps
  pointing at the same position when new items arrive later.
- A cursor that you did not issue (for example `not-a-cursor`) → `400`.
- `?offset=` and `?page=` are not supported: they are ignored, and never skip results.

## 1. Baseline and the setup this file relies on

Component B uses these routes from Component A. If they do not exist yet, build them first
(exact rules in `instruction.md`):

- `POST /api/auth/register` `{username, password}` → `201 {user, token}`.
- `POST /api/workspaces` `{slug, name}` → `201 {workspace, general_channel}`; the creator is
  the workspace `owner` and a member of `#general`.
- `POST /api/channels/{id}/join` → `200`; in an `open` workspace a non-member joining a public
  channel becomes a workspace `member`.
- `PATCH /api/workspaces/{slug}/members/{user_id}` `{role}` → `200` (used to make an admin).

1.1 **Health.** `GET /api/health` returns `200 {"status": "ok", "node_id": N}` with `N = 0`
    on port `8000`, `1` on `8001`, `2` on `8002`.

1.2 **WebSocket token.** A WebSocket upgrade to `/api/ws?token=<made-up token>` is refused
    (the HTTP response is not `101`; reject before accepting the socket). The same upgrade with
    a real token returns `101`.

1.3 **Setup works.** Register, create a workspace, and join its `#general` from another user
    all succeed (`201`, `201`, `200`). Every other rule depends on this.

1.4 **One node dies.** If the process on port `8001` is killed with `SIGKILL`, ports `8000`
    and `8002` keep answering, and `/app/start.sh` restarts the dead node within 60 seconds.
    (The check does not kill nodes; test this yourself once, killing only the PID that owns
    port `8001`, found with `ss -ltnp`.)

## 2. Channel management

2.1 **Create.** `POST /api/workspaces/{slug}/channels` `{name}` returns `201 {channel}`, a
    full `ChannelObj` with `name` as sent, `workspace_id` of the workspace, `is_private: false`,
    `is_dm: false`, `topic: ""`, `is_archived: false`. The creator is a channel member.

2.2 **Topic on create.** `{name, topic: "Release planning"}` → `201` with that topic. A
    `description` field is ignored: `{name, description: "x"}` → `201` with `topic: ""`.

2.3 **Duplicate name.** A second channel with the same `name` in the same workspace → `409`.
    The same name in a different workspace → `201`.

2.4 **Invalid name.** `name` is kebab-case (lowercase letters, digits, single hyphens between
    them), 1 to 32 characters. `"Bad Name"`, `"UPPER"`, `""` and a 33-character name → `400`.

2.5 **Topic length.** `topic` is at most 250 characters. 251 characters → `400`, both on
    create and on `PATCH /api/channels/{id}`. Exactly 250 → accepted.

2.6 **Change topic.** `PATCH /api/channels/{id}` `{topic}` from a channel member returns
    `200 {channel}` with the new topic. From a workspace member who is not in the channel →
    `403`. From the workspace owner, even when not in the channel → `200`.

2.7 **Workspace detail.** `GET /api/workspaces/{slug}` from a workspace member returns
    `200 {workspace, channels, read_state}`. `channels` holds every public channel (including
    `#general`) and the private channels the caller is a member of. A private channel the
    caller is not in is omitted. `read_state` is a list with one entry per channel in
    `channels` (shape in section 7).

2.8 **Archived in detail.** An archived channel is left out of `channels` unless
    `?include_archived=1` (or `?include_archived=true`) is given, which includes it with
    `is_archived: true`.

2.9 **Leave.** `POST /api/channels/{id}/leave` returns `200 {channel}`. Leaving again returns
    `200` too. Afterwards the user is not in the channel's members.

2.10 **Channel members.** `GET /api/channels/{id}/members` returns
     `200 {members: [UserObj, ...]}`, one per channel member, each with all seven `UserObj`
     fields.

2.11 **Archive.** `POST /api/channels/{id}/archive` from the workspace owner → `200 {channel}`
     with `is_archived: true`. From an admin or a member → `403`.

2.12 **Archived is read-only.** Posting a message or a reply to an archived channel → `423`.
     Reading its messages still works (`200`).

2.13 **Unarchive.** `POST /api/channels/{id}/unarchive` from the owner → `200 {channel}` with
     `is_archived: false`; posting works again (`201`). From a member → `403`.

2.14 **Unknown channel and no token.** Every `/api/channels/{id}/...` route with an unknown
     id → `404`. Without a token → `401`.

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

## 8. Events over WebSocket

`ws://127.0.0.1:8000/api/ws?token=<bearer>` (also `:8001`, `:8002`). Frames are JSON text,
one object per WebSocket message.

Every durable change to a channel appends one event to that channel's sequence. Event ids are
per channel, start at 1, and every event advances the sequence by exactly 1: no gaps, no
duplicates, the same ids on every node. Allocate the next id inside the same database
transaction that writes the event (for example `BEGIN IMMEDIATE`, then `MAX(event_id)+1`), so
concurrent writes on three nodes never share or skip an id. Store every event; each node
delivers new events to its local subscribers (for example by polling the shared table every
50 ms, or with Redis pub/sub on `127.0.0.1:6379`).

| Change | Event `type` | Extra fields |
|---|---|---|
| new top-level message | `message.created` | `message` (MessageObj) |
| new reply | `message.reply` | `message` |
| edit | `message.updated` | `message` |
| delete | `message.deleted` | `message_id` |
| reaction added | `reaction.added` | `message_id`, `emoji`, `user_id` |
| reaction removed | `reaction.removed` | `message_id`, `emoji`, `user_id` |
| user joins the channel | `member.joined` | `user_id` |
| user leaves the channel | `member.left` | `user_id` |
| topic change | `channel.updated` | `channel` |
| archive / unarchive | `channel.archived` / `channel.unarchived` | `channel` |

Every event frame is `{"type": <kind>, "event_id": <int>, "channel_id": <id>, ...extra}`.
Calls that change nothing (a repeated reaction, leaving twice, deleting twice) add no event.

8.1 **Subscribe.** Client sends `{"type": "subscribe", "channel_id": <id>}`; the server answers
    at once with `{"type": "subscribed", "channel_id": <id>, "head_event_id": <int>}`, where
    the head is the channel's latest event id (`0` if none). After that the socket gets only
    events with `event_id` greater than that head, each exactly once: an event that was
    committed before the subscribe must not be pushed to it afterwards (remember the head per
    socket and channel, and skip anything at or below it).

8.2 **Live event.** After subscribing, a message posted on any node arrives within 2 seconds
    as `message.created` with `event_id` = head + 1, `channel_id`, and `message.id` equal to
    the new message's id.

8.3 **Every node.** Subscribers on `8001` and `8002` receive the same event with the same
    `event_id` when the message is posted on `8000`.

8.4 **Event kinds.** Reply, edit, add reaction, remove reaction, delete, another user joins,
    that user leaves, topic change, archive, unarchive: each produces one event of the kind in
    the table, in that order, with consecutive event ids.

8.5 **Dense under concurrency.** Six messages posted at the same time across all three nodes
    produce six `message.created` events whose ids are exactly the next six numbers, as seen
    by a subscriber on each node.

8.6 **Resume.** On a different node, `{"type": "resume", "channel_id": <id>,
    "since_event_id": <n>}` replays every event after `n` in order, then sends
    `{"type": "resumed", "channel_id": <id>, "head_event_id": <head>}`.

8.7 **Access.** Subscribing to a private channel you are not in returns a frame
    `{"type": "error", ...}` and no events from it.

Every request must answer within 5 seconds.

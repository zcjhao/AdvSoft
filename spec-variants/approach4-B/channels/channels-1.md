# Channels.1: baseline and channel management

Spec 1 of 4 for Component B (channels and message history). Sections: 0, 1, 2 (numbering matches the check output).
Check: `bash /spec/channels/checks/check_channels_1.sh`.

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on `127.0.0.1:8000`, `:8001`, and `:8002`. Section 0 applies to every route in all four `channels-N.md` files.

In this spec `read_state` in `GET /api/workspaces/{slug}` (rule 2.7) may hold zero counts; `channels-4.md` fills in the real numbers. Record an event (`append_event`) for every join, leave, topic change, archive and unarchive from the start; `channels-3.md` delivers them over WebSocket.

## Uses

Nothing: this is the first spec.

## Adds

| Function | What it does |
|---|---|
| `current_user(request)` | From Component A if it exists (register, login, workspaces, join): reuse it; otherwise build the routes listed in section 1 |
| `get_channel(channel_id)` | The channel row, or answers `404` |
| `is_channel_member(channel_id, user_id)` | Channel membership |
| `can_read_channel(channel, user_id)` | Public channel of one of the user's workspaces, or a private channel they are in |
| `validate_channel_name(name) / validate_topic(topic)` | Rules 2.4 and 2.5, answer `400` |
| `channel_to_json(channel) / user_to_json(user)` | `ChannelObj` and `UserObj` with every field in section 0 |
| `append_event(channel_id, type, **fields)` | Inside the caller's `BEGIN IMMEDIATE` transaction: next dense `event_id` = MAX+1, store the full event frame |

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

Every request must answer within 5 seconds.

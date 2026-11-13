# Channels.3: events over WebSocket

Spec 3 of 4 for Component B (channels and message history). Sections: 8 (numbering matches the check output).
Check: `bash /spec/channels/checks/check_channels_3.sh`.

Do not start this spec until `check_channels_2.sh` prints `RESULT: ... 0 failed`.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | What it does |
|---|---|
| `append_event(...)` | Already called by every write; do not add a second event writer |
| `can_read_channel(channel, user_id)` | Rule 8.7 |
| `current_user / token lookup` | Rule 1.2 (reject bad tokens before the upgrade) |

## Adds

| Function | What it does |
|---|---|
| `head_event_id(channel_id)` | Latest event id of the channel, 0 if none |
| `ws_handler(request)` | subscribe / resume frames (8.1, 8.6) |
| `event_pump()` | Background task per node: poll new events every 50 ms and send each to local subscribers whose head is below it |

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

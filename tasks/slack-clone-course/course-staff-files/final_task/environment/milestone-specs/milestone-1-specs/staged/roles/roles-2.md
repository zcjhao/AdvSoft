# Roles.2: workspaces, joining and join mode

Spec 2 of 4 for Component A (roles and invitations). Sections: 3, 4 (numbering matches the check output).
Check: `bash /spec/roles/checks/check_roles_2.sh`.

Do not start this spec until `check_roles_1.sh` prints `RESULT: ... 0 failed`.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | What it does |
|---|---|
| `get_db()` | All reads and writes |
| `current_user(request)` | Every route here needs a token |
| `user_to_json(user)` | Member details |
| `error(status)` | Every error answer |

## Adds

| Function | What it does |
|---|---|
| `get_workspace(slug)` | The workspace row, or answers `404` |
| `workspace_to_json(ws)` | `{id, slug, name, owner_id, join_mode}` |
| `get_role(workspace_id, user_id)` | `owner`, `admin`, `member`, `guest`, or `None` |
| `add_workspace_member(workspace_id, user_id, role)` | Inserts a membership row |
| `create_channel(workspace_id, name, is_private, creator_id)` | Inserts a channel and makes the creator a member |
| `channel_to_json(channel)` | The `channel` object |
| `join_channel(user, channel)` | Rules 4.1, 4.3, 4.7: onboarding, private and invite-only checks |

## 3. Workspaces

3.1 **Create.** `POST /api/workspaces` `{slug, name}` returns
    `201 {workspace, general_channel}`. `workspace` has `id, slug, name, owner_id, join_mode`;
    `owner_id` is the caller's user id; `join_mode` is `"open"`. `general_channel` is a new
    channel named `"general"` with an `id`. The creator's workspace role is `owner`.

3.2 **Duplicate slug** → `409` (on any node).

3.3 **Invalid slug.** The slug must match `^[a-z0-9-]{2,32}$`. `"Bad_Slug"`, `"a"`, and a
    33-character slug → `400` each.

3.4 **Auth required.** `POST /api/workspaces` and `GET /api/workspaces` without a token →
    `401`.

3.5 **List.** `GET /api/workspaces` returns `200 {workspaces: [...]}` holding only workspaces
    the caller owns or is a member of. A user with no memberships gets a list without it.

3.6 **Members.** `GET /api/workspaces/{slug}/members` returns
    `200 {members: [{user_id, role, ...}]}` with one entry per workspace member. Right after
    creation the creator is listed with `role: "owner"`. Works from any node.

3.7 **Unknown workspace.** `GET /api/workspaces/{unknown-slug}/members` → `404`.

## 4. Joining channels and join mode

4.1 **Open join.** In a workspace with `join_mode: "open"`, a user who is not yet a workspace
    member calls `POST /api/channels/{general_channel.id}/join` and gets `200`. They are now a
    workspace member with `role: "member"` (in `/members`), and the workspace appears in their
    `GET /api/workspaces`.

4.2 **Unknown channel.** `POST /api/channels/999999999/join` → `404`.

4.3 **Private channel.** A workspace member who was not added to a private channel gets
    `403` on `POST /api/channels/{id}/join` for it.

4.4 **Auth required.** Channel join without a token → `401`.

4.5 **Only admin/owner may change the workspace.** `PATCH /api/workspaces/{slug}` from a
    plain `member` → `403`.

4.6 **Change workspace.** `PATCH /api/workspaces/{slug}` with any subset of
    `{name, join_mode}` from the owner returns `200 {workspace}` with the new values.

4.7 **Invite-only blocks strangers.** When `join_mode` is `"invite_only"`, a user who is not a
    workspace member gets `403` on `POST /api/channels/{id}/join` for a public channel there,
    until they accept an invitation (rule 8.4).

4.8 **Bad join mode.** `PATCH /api/workspaces/{slug}` with `join_mode` other than `"open"` or
    `"invite_only"` → `400`.

Every request must answer within 5 seconds.

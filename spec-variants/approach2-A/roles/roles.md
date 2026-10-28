# Component A: roles and invitations

Routes covered:

- `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`
- `POST /api/workspaces`, `GET /api/workspaces`, `PATCH /api/workspaces/{slug}`
- `GET /api/workspaces/{slug}/members`, `PATCH /api/workspaces/{slug}/members/{user_id}`
- `POST /api/workspaces/{slug}/transfer_ownership`
- `POST /api/workspaces/{slug}/channels` (only the role rules in section 7)
- `POST /api/channels/{id}/join`
- `POST /api/workspaces/{slug}/invitations`, `GET /api/workspaces/{slug}/invitations`,
  `DELETE /api/workspaces/{slug}/invitations/{code}`, `POST /api/invitations/{code}/accept`

Check: `bash /spec/roles/checks/check_roles.sh`. The rule numbers below match its output.

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on
`127.0.0.1:8000`, `:8001`, and `:8002`.

## 0. Conventions (apply to every route in this file)

- All routes are under `/api/...` and return JSON with a single top-level resource key
  (`user`, `workspace`, `workspaces`, `members`, `member`, `channel`, `invitation`,
  `invitations`).
- Every route except register and login needs `Authorization: Bearer <token>`. Missing or
  invalid token → `401`.
- Status codes: `200` read/update, `201` create, `400` validation, `401` no/bad auth, `403`
  authenticated but not allowed, `404` unknown id/slug/code, `409` duplicate.
- When several errors apply, answer in this order: `401` (auth) → `404` (unknown resource) →
  `403` (not allowed) → `400` (bad body) → `409` (duplicate). Exception: rule 5.6.
- Timestamps are ISO 8601 UTC strings, for example `2026-05-16T08:07:34.123Z`.
- **One shared store.** Any id, token, workspace, role or invitation created on one node must
  work on the other two immediately. Keep all state in one store the three node processes
  share (for example one SQLite file under `/app/data`), never in per-process memory.

## 1. Cluster baseline (milestone 0, must keep working)

1.1 **Health.** `GET /api/health` returns `200 {"status": "ok", "node_id": N}` with `N = 0`
    on port `8000`, `1` on `8001`, `2` on `8002`.

1.2 **WebSocket token.** A WebSocket upgrade to `/api/ws?token=<made-up token>` is refused
    (the HTTP response is not `101`; reject before accepting the socket). The same upgrade with
    a real token returns `101`.

1.3 **One node dies.** If the process on port `8001` is killed with `SIGKILL`, ports `8000`
    and `8002` keep answering `/api/health` with `200`, and `/app/start.sh` restarts the dead
    node within 60 seconds. (The check does not kill nodes; test this yourself once, killing
    only the PID that owns port `8001`, found with `ss -ltnp`.)

## 2. Auth

2.1 **Register.** `POST /api/auth/register` `{username, password, display_name?}` with a
    valid username and a password of 8 or more characters returns `201 {user, token}`.
    `user.username` is the username sent, `user.id` is set, `token` is at least 16 characters.

2.2 **Duplicate username** → `409`.

2.3 **Invalid username.** Usernames are letters, digits and underscore only. A username with
    a space (`"bad name"`) or another character (`"bad!name"`) → `400`.

2.4 **Short password.** A 7-character password → `400`.

2.5 **Login.** `POST /api/auth/login` `{username, password}` with the right password returns
    `200 {user, token}`.

2.6 **Wrong login.** Wrong password → `401`. Unknown username → `401`.

2.7 **Me.** `GET /api/auth/me` with a valid bearer token returns `200 {user}` for that user.

2.8 **Me without a valid token.** No `Authorization` header → `401`. Made-up token → `401`.

2.9 **Every node.** A user registered on `8000` can log in on `8002`, and its token is
    accepted by `GET /api/auth/me` on `8001` and `8002`.

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

## 5. Roles

Roles are per workspace: `owner`, `admin`, `member`, `guest`. Exactly one `owner`.
`PATCH /api/workspaces/{slug}/members/{user_id}` with body `{role}` changes a role.

5.1 **Owner promotes.** The owner sets a `member` to `admin` → `200 {member: {user_id,
    role: "admin", ...}}`.

5.2 **Change is visible.** `/members` on any node then shows the new role.

5.3 **Admin promotes.** An `admin` sets a `member` to `admin` → `200`.

5.4 **Admin cannot touch another admin.** An `admin` changing another `admin`'s role → `403`.

5.5 **Admin cannot touch the owner.** An `admin` changing the owner's role → `403`.

5.6 **No owner via PATCH.** `{role: "owner"}` from the owner or an admin → `400` (ownership
    moves only through `transfer_ownership`).

5.7 **Owner cannot change own role.** The owner PATCHing their own role → `403`.

5.8 **Members cannot change roles.** A `member` or `guest` changing anyone's role → `403`.

5.9 **Demote.** An `admin` sets a `member` to `guest` → `200` with `role: "guest"`. The owner
    sets an `admin` back to `member` → `200`.

5.10 **Bad role.** `{role: "superuser"}` → `400`.

5.11 **Not a member.** PATCH for a user id that is not a member of the workspace → `404`.

5.12 **Auth required.** PATCH without a token → `401`.

5.13 **Admin may change the workspace.** `PATCH /api/workspaces/{slug}` `{name}` from an
     `admin` → `200`.

## 6. Ownership transfer

`POST /api/workspaces/{slug}/transfer_ownership` with body `{new_owner_id}`.

6.1 **Target must be admin.** A target who is a `member`, or not in the workspace → `400`.

6.2 **Owner only.** An `admin` calling it → `403`.

6.3 **Transfer.** The owner transfers to an `admin` → `200`. Afterwards `/members` shows the
    target as `owner` and the old owner as `admin`, and the workspace's `owner_id` (in
    `GET /api/workspaces`) is the target.

6.4 **Old owner loses power.** After 6.3 the old owner calling `transfer_ownership` → `403`,
    and the old owner PATCHing the new owner's role → `403`.

6.5 **Auth required.** Without a token → `401`.

## 7. Role-based channel permissions

`POST /api/workspaces/{slug}/channels` with body `{name, is_private?}`, `name` kebab-case.

7.1 **Guest cannot join.** A `guest` calling `POST /api/channels/{id}/join` on a public
    channel → `403`.

7.2 **Guest cannot create.** A `guest` creating any channel → `403`.

7.3 **Member creates public.** A `member` creating a public channel → `201 {channel}` with
    `is_private: false`.

7.4 **Member cannot create private.** A `member` sending `{is_private: true}` → `403`.

7.5 **Admin creates private.** An `admin` (or the owner) sending `{is_private: true}` →
    `201` with `is_private: true`.

7.6 **Member joins public.** A `member` joining a public channel → `200`.

## 8. Invitations

An invitation has at least `{code, workspace_id, max_uses, uses, expires_at,
invited_user_id}`; `code` is a random URL-safe string.

8.1 **Create.** `POST /api/workspaces/{slug}/invitations` with body
    `{expires_in_seconds?, max_uses?, invited_username?, invited_user_id?}` (all optional,
    `{}` is valid) from the owner → `201 {invitation}` with a non-empty `code`.

8.2 **Admin/owner only.** An `admin` creating one → `201`; a `member` → `403`.

8.3 **List.** `GET /api/workspaces/{slug}/invitations` from the owner → `200
    {invitations: [...]}` holding every active invitation's `code`. From a `member` → `403`.

8.4 **Accept.** `POST /api/invitations/{code}/accept` (empty body) from a non-member → `200`.
    The user is now a workspace `member` (in `/members`) and can join the workspace's public
    channels even when it is `invite_only`. Works on any node.

8.5 **Single use by default.** `max_uses` defaults to `1`: a second user accepting the same
    code → `404`. A used-up invitation is no longer in the list.

8.6 **Max uses.** With `max_uses: 2`, two users accept (`200` each) and a third → `404`.

8.7 **Targeted by id.** With `invited_user_id: X`, another user accepting → `403`; user X →
    `200`; accepting again → `404` (targeted invitations are single use, whatever `max_uses`
    says).

8.8 **Targeted by username.** Same as 8.7 with `invited_username`: another user → `403`, the
    named user → `200`, again → `404`.

8.9 **Unknown code** → `404`.

8.10 **Expired.** With `expires_in_seconds: 1`, accepting after 2 seconds → `404`.

8.11 **Revoke.** `DELETE /api/workspaces/{slug}/invitations/{code}` from a `member` → `403`;
     from the owner → `200 {"deleted": true}`. Accepting it afterwards → `404`, and it is no
     longer in the list.

8.12 **Bad body.** `max_uses: 0` → `400`.

8.13 **Auth required.** Create and accept without a token → `401`.

Every request must answer within 5 seconds.

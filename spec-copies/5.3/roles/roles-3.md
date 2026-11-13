# Roles.3: roles, ownership transfer and channel permissions

Spec 3 of 4 for Component A (roles and invitations). Sections: 5, 6, 7 (numbering matches the check output).
Check: `bash /spec/roles/checks/check_roles_3.sh`.

Do not start this spec until `check_roles_2.sh` prints `RESULT: ... 0 failed`.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | What it does |
|---|---|
| `current_user(request)` | Auth |
| `get_workspace(slug)` | 404 for unknown slugs |
| `get_role(workspace_id, user_id)` | Every permission decision |
| `create_channel(...)` | Channel creation, now with role checks |
| `join_channel(user, channel)` | Add the guest rule (7.1) here, not in a second join function |

## Adds

| Function | What it does |
|---|---|
| `require_role(workspace, user, allowed)` | Answers `403` unless the caller's role is in `allowed` |
| `set_role(workspace, actor, target_id, new_role)` | Rules 5.1 to 5.11, checked in the order 404, 403, 400 |
| `transfer_ownership(workspace, actor, new_owner_id)` | Rules 6.1 to 6.4 |
| `can_create_channel(role, is_private)` | Rules 7.2 to 7.5 |

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

Every request must answer within 5 seconds.

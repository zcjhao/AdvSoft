# Roles.4: invitations

Spec 4 of 4 for Component A (roles and invitations). Sections: 8 (numbering matches the check output).
Check: `bash /spec/roles/checks/check_roles_4.sh`.

Do not start this spec until `check_roles_3.sh` prints `RESULT: ... 0 failed`.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | What it does |
|---|---|
| `current_user(request)` | Auth |
| `get_workspace(slug)` | 404 for unknown slugs |
| `require_role(workspace, user, ['owner', 'admin'])` | Create, list and revoke |
| `add_workspace_member(workspace_id, user_id, 'member')` | Accepting an invitation |
| `join_channel(user, channel)` | Members who accepted can join invite-only workspaces |

## Adds

| Function | What it does |
|---|---|
| `create_invitation(workspace, body)` | Validates the body, stores code, max_uses, uses, expires_at, invited_user_id |
| `invitation_to_json(inv)` | `{code, workspace_id, max_uses, uses, expires_at, invited_user_id}` |
| `invitation_is_active(inv)` | Not revoked, not expired, uses below max_uses |
| `accept_invitation(code, user)` | Rules 8.4 to 8.10, inside one transaction |

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

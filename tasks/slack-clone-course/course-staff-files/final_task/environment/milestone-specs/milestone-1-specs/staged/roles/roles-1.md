# Roles.1: cluster baseline and auth

Spec 1 of 4 for Component A (roles and invitations). Sections: 0, 1, 2 (numbering matches the check output).
Check: `bash /spec/roles/checks/check_roles_1.sh`.

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on `127.0.0.1:8000`, `:8001`, and `:8002`. Section 0 applies to every route in all four `roles-N.md` files.

## Uses

Nothing: this is the first spec.

## Adds

| Function | What it does |
|---|---|
| `get_db()` | Opens the shared SQLite file (WAL, busy_timeout); every node uses it |
| `hash_password(pw) / verify_password(pw, stored)` | Password storage and comparison |
| `issue_token(user_id)` | Creates and stores a random token of 16+ characters |
| `current_user(request)` | Returns the bearer-token holder or answers `401` |
| `user_to_json(user)` | The `user` object: `id, username, display_name` and the other profile fields |
| `error(status)` | JSON error response with that status |

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

Every request must answer within 5 seconds.

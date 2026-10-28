#!/usr/bin/env bash
# Self-check for Component A (roles and invitations). Prints PASS/FAIL per rule in /spec/roles/roles.md.
# Usage: bash /spec/roles/checks/check_roles.sh      Exit 0 only if every rule passes.
# Each run uses fresh usernames and slugs, so it can be rerun any number of times.
set -u
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_roles_body.json
pass=0; fail=0

# ONLY="2 3" limits output and counts to those sections (used by the staged checks); empty means all.
want() { [ -z "${ONLY:-}" ] && return 0; local s; for s in $ONLY; do [ "${1%%.*}" = "$s" ] && return 0; done; return 1; }
ok()  { want "$1" || return 0; echo "PASS $1 $2"; pass=$((pass+1)); }
bad() { want "$1" || return 0; echo "FAIL $1 $2"; fail=$((fail+1)); }
# expect <rule> <description> <expected-status> <actual-status>
expect() { if [ "$4" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2: expected $3, got $4"; fi; }
# same <expected> <value>...  -> true if every value equals expected
same() { local e=$1; shift; for v in "$@"; do [ "$v" = "$e" ] || return 1; done; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 5 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  : >"$BODY"
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 5s
}
# jget <python expression over the last body `d`>  -> strings raw, anything else as JSON, '' on error
jget() {
  python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1])); v=eval(sys.argv[2])
    print(v if isinstance(v,str) else json.dumps(v))
except Exception: pass' "$BODY" "$1"
}
# role_of <user_id>  -> that user's role in the last /members body, or ''
role_of() { jget "next((m.get('role') for m in d['members'] if str(m.get('user_id'))=='$1'), '')"; }
# has_code <code>  -> 'yes' if the last /invitations body lists that code
has_code() { jget "'yes' if any(i.get('code')=='$1' for i in d['invitations']) else 'no'"; }
# has_slug <slug>  -> 'yes' if the last /api/workspaces body lists that slug
has_slug() { jget "'yes' if any(w.get('slug')=='$1' for w in d['workspaces']) else 'no'"; }
# val <TOK|ID|NAME> <user>  -> that user's token, id or username
val() { local v="$1_$2"; printf '%s' "${!v}"; }
# lit <id>  -> the id as a JSON literal (numbers bare, anything else quoted)
lit() { if [[ "$1" =~ ^[0-9]+$ ]]; then printf '%s' "$1"; else printf '"%s"' "$1"; fi; }
# ws_status <port> <token>  -> HTTP status of a WebSocket upgrade to /api/ws, or 'none'
ws_status() {
  python3 -c 'import socket,sys,base64,os
try:
    p=int(sys.argv[1]); s=socket.create_connection(("127.0.0.1",p),timeout=3)
    k=base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET /api/ws?token=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
      "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (sys.argv[2],p,k)).encode())
    r=s.recv(1024).split(b"\r\n")[0].split()
    print(r[1].decode() if len(r)>1 else "none"); s.close()
except Exception: print("none")' "$1" "$2"
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}

# AIDEV-NOTE: startup wait is capped at 15s; the rules below take ~5-8s (incl. a 2s sleep for 8.10),
# so the whole script fits the agent's 30s command limit.
if ! healthy; then
  (cd /app && nohup /app/start.sh >/tmp/start.log 2>&1 &)
  for _ in $(seq 1 15); do healthy && break; sleep 1; done
fi
healthy || { echo "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

RUN="$(date +%s)$RANDOM"; PW="correct-horse-1"

# ---------- 1. cluster baseline ----------
s0=$(call GET $N0/api/health); a=$(jget "d['status']+':'+str(d['node_id'])")
s1=$(call GET $N1/api/health); b=$(jget "d['status']+':'+str(d['node_id'])")
s2=$(call GET $N2/api/health); c=$(jget "d['status']+':'+str(d['node_id'])")
if same 200 "$s0" "$s1" "$s2" && [ "$a $b $c" = "ok:0 ok:1 ok:2" ]; then ok 1.1 "health ok with node_id 0/1/2"
else bad 1.1 "health: expected 200 ok:0 ok:1 ok:2, got $s0 $s1 $s2 / $a $b $c"; fi

# ---------- 2. auth ----------
own="own_$RUN"
s=$(call POST $N0/api/auth/register "{\"username\":\"$own\",\"password\":\"$PW\"}")
TOK_own=$(jget "d['token']"); ID_own=$(jget "d['user']['id']"); NAME_own=$own
if [ "$s" = 201 ] && [ "${#TOK_own}" -ge 16 ] && [ -n "${ID_own}" ] && [ "$(jget "d['user']['username']")" = "$own" ]; then
  ok 2.1 "register returns 201 with user and token"
else bad 2.1 "register: expected 201 with user.id, user.username and 16+ char token, got $s, token length ${#TOK_own}"; fi

s=$(call POST $N0/api/auth/register "{\"username\":\"$own\",\"password\":\"$PW\"}")
expect 2.2 "duplicate username" 409 "$s"

a=$(call POST $N0/api/auth/register "{\"username\":\"bad name$RANDOM\",\"password\":\"$PW\"}")
b=$(call POST $N0/api/auth/register "{\"username\":\"bad!name$RANDOM\",\"password\":\"$PW\"}")
if same 400 "$a" "$b"; then ok 2.3 "invalid usernames rejected"
else bad 2.3 "invalid username: expected 400 for space and '!', got $a $b"; fi

s=$(call POST $N0/api/auth/register "{\"username\":\"${own}x\",\"password\":\"short77\"}")
expect 2.4 "7 character password" 400 "$s"

s=$(call POST $N0/api/auth/login "{\"username\":\"$own\",\"password\":\"$PW\"}")
if [ "$s" = 200 ] && [ -n "$(jget "d['token']")" ]; then ok 2.5 "login returns 200 with token"
else bad 2.5 "login: expected 200 with a token, got $s"; fi

a=$(call POST $N0/api/auth/login "{\"username\":\"$own\",\"password\":\"WRONG12345\"}")
b=$(call POST $N0/api/auth/login "{\"username\":\"nobody_$RUN\",\"password\":\"$PW\"}")
if same 401 "$a" "$b"; then ok 2.6 "wrong password and unknown user return 401"
else bad 2.6 "bad login: expected 401 for wrong password and unknown user, got $a $b"; fi

s=$(call GET $N0/api/auth/me "" "${TOK_own}")
if [ "$s" = 200 ] && [ "$(jget "d['user']['username']")" = "$own" ]; then ok 2.7 "me returns the token's user"
else bad 2.7 "me: expected 200 with username $own, got $s"; fi

a=$(call GET $N0/api/auth/me); b=$(call GET $N0/api/auth/me "" "not-a-real-token-000000")
if same 401 "$a" "$b"; then ok 2.8 "me without a valid token returns 401"
else bad 2.8 "me: expected 401 with no token and a made-up token, got $a $b"; fi

a=$(call POST $N2/api/auth/login "{\"username\":\"$own\",\"password\":\"$PW\"}")
b=$(call GET $N1/api/auth/me "" "${TOK_own}"); c=$(call GET $N2/api/auth/me "" "${TOK_own}")
if same 200 "$a" "$b" "$c"; then ok 2.9 "account and token work on all three nodes"
else bad 2.9 "cross-node: login on 8002 $a, me on 8001 $b, me on 8002 $c (expected 200 each)"; fi

# The other users this check needs (not rules on their own).
for r in adm1 adm2 mem gst out inv1 inv2 inv3 inv4 inv5 inv6; do
  printf -v "NAME_$r" '%s' "${r}_$RUN"
  call POST $N0/api/auth/register "{\"username\":\"${r}_$RUN\",\"password\":\"$PW\"}" >/dev/null
  printf -v "TOK_$r" '%s' "$(jget "d['token']")"; printf -v "ID_$r" '%s' "$(jget "d['user']['id']")"
done

a=$(ws_status 8000 "not-a-real-token-000000"); b=$(ws_status 8000 "${TOK_own}")
if [ "$a" != 101 ] && [ "$b" = 101 ]; then ok 1.2 "websocket refuses a bad token and accepts a real one"
else bad 1.2 "websocket upgrade: bad token got $a (must not be 101), real token got $b (expected 101)"; fi

# ---------- 3. workspaces ----------
W1="ws-$RUN"; W2="inv-$RUN"; W3="xfer-$RUN"
s=$(call POST $N0/api/workspaces "{\"slug\":\"$W1\",\"name\":\"Check WS\"}" "${TOK_own}")
G1=$(jget "d['general_channel']['id']")
got=$(jget "[d['workspace']['slug'], d['workspace']['name'], str(d['workspace']['owner_id']), d['workspace']['join_mode'], d['general_channel']['name'], bool(d['workspace']['id'])]")
want="[\"$W1\", \"Check WS\", \"${ID_own}\", \"open\", \"general\", true]"
if [ "$s" = 201 ] && [ "$got" = "$want" ] && [ -n "$G1" ]; then ok 3.1 "create workspace returns workspace and #general"
else bad 3.1 "create workspace: expected 201 with [slug,name,owner_id,join_mode,general name,id set] = $want, got $s $got"; fi

s=$(call POST $N1/api/workspaces "{\"slug\":\"$W1\",\"name\":\"Again\"}" "${TOK_own}")
expect 3.2 "duplicate slug" 409 "$s"

a=$(call POST $N0/api/workspaces '{"slug":"Bad_Slug","name":"x"}' "${TOK_own}")
b=$(call POST $N0/api/workspaces '{"slug":"a","name":"x"}' "${TOK_own}")
c=$(call POST $N0/api/workspaces '{"slug":"abcdefghij-abcdefghij-abcdefghijk","name":"x"}' "${TOK_own}")
if same 400 "$a" "$b" "$c"; then ok 3.3 "invalid slugs rejected"
else bad 3.3 "invalid slug: expected 400 for Bad_Slug, a, 33 chars, got $a $b $c"; fi

a=$(call POST $N0/api/workspaces "{\"slug\":\"no-auth-$RUN\",\"name\":\"x\"}"); b=$(call GET $N0/api/workspaces)
if same 401 "$a" "$b"; then ok 3.4 "workspace routes need a token"
else bad 3.4 "no token: expected 401 on POST and GET /api/workspaces, got $a $b"; fi

a=$(call GET $N0/api/workspaces "" "${TOK_own}"); x=$(has_slug "$W1")
b=$(call GET $N0/api/workspaces "" "${TOK_mem}"); y=$(has_slug "$W1")
if same 200 "$a" "$b" && [ "$x" = yes ] && [ "$y" = no ]; then ok 3.5 "workspace list is filtered to the caller"
else bad 3.5 "list: owner $a lists it=$x (want yes), non-member $b lists it=$y (want no)"; fi

s=$(call GET $N2/api/workspaces/$W1/members "" "${TOK_own}"); r=$(role_of "${ID_own}")
if [ "$s" = 200 ] && [ "$r" = owner ]; then ok 3.6 "members lists the creator as owner"
else bad 3.6 "members on 8002: expected 200 with creator role owner, got $s role '$r'"; fi

s=$(call GET $N0/api/workspaces/no-such-ws-$RUN/members "" "${TOK_own}")
expect 3.7 "members of unknown workspace" 404 "$s"

# ---------- 4. joining and join mode ----------
a=$(call POST $N1/api/channels/$G1/join "" "${TOK_adm1}")
call GET $N0/api/workspaces/$W1/members "" "${TOK_own}" >/dev/null; r=$(role_of "${ID_adm1}")
call GET $N0/api/workspaces "" "${TOK_adm1}" >/dev/null; x=$(has_slug "$W1")
b=$(call POST $N0/api/channels/$G1/join "" "${TOK_adm2}")
c=$(call POST $N0/api/channels/$G1/join "" "${TOK_mem}")
d=$(call POST $N2/api/channels/$G1/join "" "${TOK_gst}")
if same 200 "$a" "$b" "$c" "$d" && [ "$r" = member ] && [ "$x" = yes ]; then ok 4.1 "joining #general in an open workspace onboards as member"
else bad 4.1 "open join: joins got $a $b $c $d (want 200), role '$r' (want member), listed=$x (want yes)"; fi

s=$(call POST $N0/api/channels/999999999/join "" "${TOK_mem}")
expect 4.2 "join unknown channel" 404 "$s"

s=$(call POST $N0/api/workspaces/$W1/channels '{"name":"secret-room","is_private":true}' "${TOK_own}")
if [ "$s" = 201 ]; then
  P1=$(jget "d['channel']['id']")
  s=$(call POST $N0/api/channels/$P1/join "" "${TOK_mem}")
  expect 4.3 "member joining a private channel" 403 "$s"
else bad 4.3 "setup: owner creating a private channel got $s (expected 201)"; fi

s=$(call POST $N0/api/channels/$G1/join)
expect 4.4 "join without a token" 401 "$s"

s=$(call PATCH $N0/api/workspaces/$W1 '{"name":"Hacked"}' "${TOK_mem}")
expect 4.5 "member patching the workspace" 403 "$s"

call POST $N0/api/workspaces "{\"slug\":\"$W2\",\"name\":\"Invites\"}" "${TOK_own}" >/dev/null; G2=$(jget "d['general_channel']['id']")
s=$(call PATCH $N1/api/workspaces/$W2 '{"join_mode":"invite_only","name":"Invite Only"}' "${TOK_own}")
got=$(jget "[d['workspace']['join_mode'], d['workspace']['name']]")
if [ "$s" = 200 ] && [ "$got" = '["invite_only", "Invite Only"]' ]; then ok 4.6 "owner patches name and join_mode"
else bad 4.6 "patch workspace: expected 200 with [invite_only, Invite Only], got $s $got"; fi

s=$(call POST $N0/api/channels/$G2/join "" "${TOK_out}")
expect 4.7 "stranger joining an invite_only workspace" 403 "$s"

s=$(call PATCH $N0/api/workspaces/$W2 '{"join_mode":"secret"}' "${TOK_own}")
expect 4.8 "invalid join_mode" 400 "$s"

# ---------- 5. roles (in W1: adm1, adm2, mem, gst are members) ----------
M="$N0/api/workspaces/$W1/members"
s=$(call PATCH $M/${ID_adm1} '{"role":"admin"}' "${TOK_own}")
got=$(jget "[str(d['member']['user_id']), d['member']['role']]")
if [ "$s" = 200 ] && [ "$got" = "[\"${ID_adm1}\", \"admin\"]" ]; then ok 5.1 "owner promotes member to admin"
else bad 5.1 "promote: expected 200 {member:{user_id,role:admin}}, got $s $got"; fi

call GET $N2/api/workspaces/$W1/members "" "${TOK_mem}" >/dev/null; r=$(role_of "${ID_adm1}")
if [ "$r" = admin ]; then ok 5.2 "new role visible on another node"
else bad 5.2 "members on 8002: expected adm1 role admin, got '$r'"; fi

s=$(call PATCH $M/${ID_adm2} '{"role":"admin"}' "${TOK_adm1}")
expect 5.3 "admin promotes member to admin" 200 "$s"

s=$(call PATCH $M/${ID_adm2} '{"role":"member"}' "${TOK_adm1}")
expect 5.4 "admin changing another admin" 403 "$s"

s=$(call PATCH $M/${ID_own} '{"role":"member"}' "${TOK_adm1}")
expect 5.5 "admin changing the owner" 403 "$s"

a=$(call PATCH $M/${ID_adm2} '{"role":"owner"}' "${TOK_own}")
b=$(call PATCH $M/${ID_mem} '{"role":"owner"}' "${TOK_adm1}")
if same 400 "$a" "$b"; then ok 5.6 "role owner via PATCH rejected"
else bad 5.6 "role owner: expected 400 from owner and from admin, got $a $b"; fi

s=$(call PATCH $M/${ID_own} '{"role":"admin"}' "${TOK_own}")
expect 5.7 "owner changing own role" 403 "$s"

s=$(call PATCH $M/${ID_gst} '{"role":"admin"}' "${TOK_mem}")
expect 5.8 "member changing a role" 403 "$s"

a=$(call PATCH $M/${ID_gst} '{"role":"guest"}' "${TOK_adm1}"); r=$(jget "d['member']['role']")
b=$(call PATCH $M/${ID_adm2} '{"role":"member"}' "${TOK_own}")
if same 200 "$a" "$b" && [ "$r" = guest ]; then ok 5.9 "admin demotes to guest, owner demotes admin"
else bad 5.9 "demote: admin->guest got $a role '$r', owner admin->member got $b (want 200, guest)"; fi

s=$(call PATCH $M/${ID_mem} '{"role":"superuser"}' "${TOK_own}")
expect 5.10 "invalid role" 400 "$s"

s=$(call PATCH $M/${ID_out} '{"role":"admin"}' "${TOK_own}")
expect 5.11 "PATCH a non-member" 404 "$s"

s=$(call PATCH $M/${ID_mem} '{"role":"admin"}')
expect 5.12 "PATCH without a token" 401 "$s"

s=$(call PATCH $N0/api/workspaces/$W1 '{"name":"Renamed By Admin"}' "${TOK_adm1}")
expect 5.13 "admin patches the workspace" 200 "$s"

# ---------- 6. ownership transfer (in W3: adm1 admin, mem member) ----------
call POST $N0/api/workspaces "{\"slug\":\"$W3\",\"name\":\"Transfer\"}" "${TOK_own}" >/dev/null; G3=$(jget "d['general_channel']['id']")
call POST $N0/api/channels/$G3/join "" "${TOK_adm1}" >/dev/null
call POST $N0/api/channels/$G3/join "" "${TOK_mem}" >/dev/null
call PATCH $N0/api/workspaces/$W3/members/${ID_adm1} '{"role":"admin"}' "${TOK_own}" >/dev/null
T="$N0/api/workspaces/$W3/transfer_ownership"

a=$(call POST $T "{\"new_owner_id\":$(lit "${ID_mem}")}" "${TOK_own}")
b=$(call POST $T "{\"new_owner_id\":$(lit "${ID_out}")}" "${TOK_own}")
if same 400 "$a" "$b"; then ok 6.1 "transfer target must be an admin"
else bad 6.1 "transfer to member / non-member: expected 400 each, got $a $b"; fi

s=$(call POST $T "{\"new_owner_id\":$(lit "${ID_adm1}")}" "${TOK_adm1}")
expect 6.2 "admin calling transfer_ownership" 403 "$s"

s=$(call POST $T "{\"new_owner_id\":$(lit "${ID_adm1}")}" "${TOK_own}")
call GET $N1/api/workspaces/$W3/members "" "${TOK_own}" >/dev/null; r1=$(role_of "${ID_adm1}"); r2=$(role_of "${ID_own}")
call GET $N0/api/workspaces "" "${TOK_adm1}" >/dev/null; o=$(jget "next(str(w['owner_id']) for w in d['workspaces'] if w['slug']=='$W3')")
if [ "$s" = 200 ] && [ "$r1 $r2" = "owner admin" ] && [ "$o" = "${ID_adm1}" ]; then ok 6.3 "transfer swaps owner and admin"
else bad 6.3 "transfer: got $s, roles target='$r1' old='$r2' (want owner admin), owner_id '$o' (want ${ID_adm1})"; fi

a=$(call POST $T "{\"new_owner_id\":$(lit "${ID_own}")}" "${TOK_own}")
b=$(call PATCH $N0/api/workspaces/$W3/members/${ID_adm1} '{"role":"member"}' "${TOK_own}")
if same 403 "$a" "$b"; then ok 6.4 "old owner loses owner powers"
else bad 6.4 "after transfer: old owner transfer got $a, patching new owner got $b (want 403 each)"; fi

s=$(call POST $T "{\"new_owner_id\":$(lit "${ID_own}")}")
expect 6.5 "transfer without a token" 401 "$s"

# ---------- 7. role-based channel permissions (in W1: adm1 admin, mem member, gst guest) ----------
C="$N0/api/workspaces/$W1/channels"
s=$(call POST $C '{"name":"town-hall"}' "${TOK_own}")
if [ "$s" = 201 ]; then
  PUB=$(jget "d['channel']['id']")
  s=$(call POST $N0/api/channels/$PUB/join "" "${TOK_gst}")
  expect 7.1 "guest joining a public channel" 403 "$s"
else bad 7.1 "setup: owner creating a public channel got $s (expected 201)"; PUB=999999999; fi

s=$(call POST $C '{"name":"guest-room"}' "${TOK_gst}")
expect 7.2 "guest creating a channel" 403 "$s"

s=$(call POST $C '{"name":"member-room"}' "${TOK_mem}"); x=$(jget "d['channel']['is_private']")
if [ "$s" = 201 ] && [ "$x" = false ]; then ok 7.3 "member creates a public channel"
else bad 7.3 "member public channel: expected 201 with is_private false, got $s '$x'"; fi

s=$(call POST $C '{"name":"member-secret","is_private":true}' "${TOK_mem}")
expect 7.4 "member creating a private channel" 403 "$s"

s=$(call POST $C '{"name":"admin-secret","is_private":true}' "${TOK_adm1}"); x=$(jget "d['channel']['is_private']")
if [ "$s" = 201 ] && [ "$x" = true ]; then ok 7.5 "admin creates a private channel"
else bad 7.5 "admin private channel: expected 201 with is_private true, got $s '$x'"; fi

s=$(call POST $N0/api/channels/$PUB/join "" "${TOK_mem}")
expect 7.6 "member joining a public channel" 200 "$s"

# ---------- 8. invitations (W2 is invite_only; W1 has admin adm1 and member mem) ----------
I2="$N0/api/workspaces/$W2/invitations"; I1="$N0/api/workspaces/$W1/invitations"
# new_inv <json-body> -> prints the new code on W2 (as owner), '' on failure
new_inv() { call POST $I2 "$1" "${TOK_own}" >/dev/null; jget "d['invitation']['code']"; }
accept() { call POST $N0/api/invitations/$1/accept "" "$(val TOK "$2")"; }

s=$(call POST $I2 '{}' "${TOK_own}"); CODE1=$(jget "d['invitation']['code']")
if [ "$s" = 201 ] && [ -n "$CODE1" ]; then ok 8.1 "owner creates an invitation with a code"
else bad 8.1 "create invitation: expected 201 with invitation.code, got $s"; CODE1=none; fi

a=$(call POST $I1 '{}' "${TOK_adm1}"); b=$(call POST $I1 '{}' "${TOK_mem}")
if [ "$a" = 201 ] && [ "$b" = 403 ]; then ok 8.2 "admin can create invitations, member cannot"
else bad 8.2 "create invitation: admin got $a (want 201), member got $b (want 403)"; fi

a=$(call GET $I2 "" "${TOK_own}"); x=$(has_code "$CODE1"); b=$(call GET $I1 "" "${TOK_mem}")
if [ "$a" = 200 ] && [ "$x" = yes ] && [ "$b" = 403 ]; then ok 8.3 "owner lists invitations, member cannot"
else bad 8.3 "list invitations: owner got $a lists code=$x (want 200 yes), member got $b (want 403)"; fi

a=$(call POST $N1/api/invitations/$CODE1/accept "" "${TOK_inv1}")
call GET $N0/api/workspaces/$W2/members "" "${TOK_own}" >/dev/null; r=$(role_of "${ID_inv1}")
b=$(call POST $N2/api/channels/$G2/join "" "${TOK_inv1}")
if [ "$a" = 200 ] && [ "$r" = member ] && [ "$b" = 200 ]; then ok 8.4 "accepting makes the user a member who can join"
else bad 8.4 "accept on 8001 got $a (want 200), role '$r' (want member), join #general got $b (want 200)"; fi

a=$(accept "$CODE1" inv2); call GET $I2 "" "${TOK_own}" >/dev/null; x=$(has_code "$CODE1")
if [ "$a" = 404 ] && [ "$x" = no ]; then ok 8.5 "default invitation is single use"
else bad 8.5 "second accept got $a (want 404), used-up code still listed=$x (want no)"; fi

c2=$(new_inv '{"max_uses":2}'); a=$(accept "$c2" inv2); b=$(accept "$c2" inv3); c=$(accept "$c2" inv4)
if [ "$a $b $c" = "200 200 404" ]; then ok 8.6 "max_uses 2 allows two accepts"
else bad 8.6 "max_uses 2: accepts got $a $b $c (want 200 200 404)"; fi

c3=$(new_inv "{\"invited_user_id\":$(lit "${ID_inv4}"),\"max_uses\":5}")
a=$(accept "$c3" inv5); b=$(accept "$c3" inv4); c=$(accept "$c3" inv4)
if [ "$a $b $c" = "403 200 404" ]; then ok 8.7 "invitation targeted by user id"
else bad 8.7 "invited_user_id: other user $a, target $b, target again $c (want 403 200 404)"; fi

c4=$(new_inv "{\"invited_username\":\"${NAME_inv5}\",\"max_uses\":5}")
a=$(accept "$c4" inv6); b=$(accept "$c4" inv5); c=$(accept "$c4" inv5)
if [ "$a $b $c" = "403 200 404" ]; then ok 8.8 "invitation targeted by username"
else bad 8.8 "invited_username: other user $a, target $b, target again $c (want 403 200 404)"; fi

s=$(accept "nosuchcode$RUN" inv6)
expect 8.9 "accept unknown code" 404 "$s"

c5=$(new_inv '{"expires_in_seconds":1}'); sleep 2; s=$(accept "$c5" inv6)
if [ -n "$c5" ]; then expect 8.10 "accept expired invitation" 404 "$s"
else bad 8.10 "setup: creating an invitation with expires_in_seconds 1 failed"; fi

c6=$(new_inv '{}')
a=$(call DELETE $I2/$c6 "" "${TOK_inv1}")
b=$(call DELETE $I2/$c6 "" "${TOK_own}"); x=$(jget "d['deleted']")
c=$(accept "$c6" inv6); call GET $I2 "" "${TOK_own}" >/dev/null; y=$(has_code "$c6")
if [ "$a $b $x $c $y" = "403 200 true 404 no" ]; then ok 8.11 "revoke invitation"
else bad 8.11 "revoke: member $a (want 403), owner $b deleted=$x (want 200 true), accept $c (want 404), listed=$y (want no)"; fi

s=$(call POST $I2 '{"max_uses":0}' "${TOK_own}")
expect 8.12 "max_uses 0" 400 "$s"

c7=$(new_inv '{}'); a=$(call POST $I2 '{}'); b=$(call POST $N0/api/invitations/$c7/accept)
if same 401 "$a" "$b"; then ok 8.13 "invitation routes need a token"
else bad 8.13 "no token: create got $a, accept got $b (want 401 each)"; fi

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]

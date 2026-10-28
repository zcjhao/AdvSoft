#!/usr/bin/env python3
"""Self-check for Component B (channels and message history).

Prints PASS/FAIL per rule in /spec/channels/channels.md and a RESULT line. Exit 0 only if every rule passes.
Run it through check_channels.sh. ONLY="2 3" limits the output to those sections (used by the staged checks).
Each run uses fresh usernames and slugs, so it can be rerun any number of times. Python stdlib only.
"""
import base64, json, os, random, re, socket, subprocess, sys, threading, time, urllib.error, urllib.request

NODES = ["http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8002"]
N0, N1, N2 = NODES
ONLY = os.environ.get("ONLY", "").split()
RUN = "%d%d" % (int(time.time()) % 100000, random.randint(100, 999))
PW = "correct-horse-1"
T_START = time.time()
passed = failed = 0


# ---------------- output ----------------
def wanted(rule):
    return not ONLY or rule.split(".")[0] in ONLY


def ok(rule, desc):
    global passed
    if wanted(rule):
        print("PASS %s %s" % (rule, desc), flush=True)
        passed += 1


def bad(rule, desc):
    global failed
    if wanted(rule):
        print("FAIL %s %s" % (rule, desc), flush=True)
        failed += 1


def check(rule, cond, desc, detail):
    ok(rule, desc) if cond else bad(rule, "%s: %s" % (desc, detail))


class Setup(Exception):
    pass


# ---------------- HTTP ----------------
class R:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def get(self, *path, default=None):
        v = self.body
        try:
            for p in path:
                v = v[p]
            return v
        except Exception:
            return default


def call(method, url, body=None, token=None, raw=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            st, txt = r.status, r.read()
    except urllib.error.HTTPError as e:
        st, txt = e.code, e.read()
    except Exception:
        return R(0, None)
    try:
        return R(st, json.loads(txt or b"null"))
    except Exception:
        return R(st, None)


def healthy():
    for n in NODES:
        try:
            with urllib.request.urlopen(n + "/api/health", timeout=2) as r:
                if r.status != 200:
                    return False
        except Exception:
            return False
    return True


# ---------------- WebSocket (minimal RFC 6455 client) ----------------
class WS:
    def __init__(self, port, token):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(("GET /api/ws?token=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                        % (token, port, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        parts = head.split(b"\r\n")[0].split()
        self.status = parts[1].decode() if len(parts) > 1 else "none"
        self.frames = []

    def send(self, obj):
        payload = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += n.to_bytes(2, "big")
        else:
            hdr.append(0x80 | 127); hdr += n.to_bytes(8, "big")
        mask = os.urandom(4)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _need(self, n, deadline):
        while len(self.buf) < n:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError
            self.s.settimeout(left)
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError
            self.buf += chunk

    def recv(self, timeout):
        """Next JSON frame, or None on timeout/close."""
        deadline = time.time() + timeout
        try:
            while True:
                self._need(2, deadline)
                op, ln = self.buf[0] & 0x0F, self.buf[1] & 0x7F
                off = 2
                if ln == 126:
                    self._need(4, deadline); ln = int.from_bytes(self.buf[2:4], "big"); off = 4
                elif ln == 127:
                    self._need(10, deadline); ln = int.from_bytes(self.buf[2:10], "big"); off = 10
                self._need(off + ln, deadline)
                data, self.buf = self.buf[off:off + ln], self.buf[off + ln:]
                if op == 0x8:
                    return None
                if op == 0x1:
                    try:
                        return json.loads(data)
                    except Exception:
                        continue
        except Exception:
            return None

    def until(self, pred, timeout=2.0):
        """Collect frames until pred(frame) is true; returns (match or None, frames seen)."""
        seen = []
        end = time.time() + timeout
        while time.time() < end:
            f = self.recv(end - time.time())
            if f is None:
                break
            seen.append(f)
            if pred(f):
                return f, seen
        return None, seen

    def events(self, n, timeout=3.0):
        """Collect the next n frames that carry an event_id."""
        out = []
        end = time.time() + timeout
        while len(out) < n and time.time() < end:
            f = self.recv(end - time.time())
            if f is None:
                break
            if isinstance(f, dict) and "event_id" in f:
                out.append(f)
        return out

    def subscribe(self, cid):
        self.send({"type": "subscribe", "channel_id": cid})
        f, _ = self.until(lambda f: isinstance(f, dict) and f.get("type") in ("subscribed", "error"))
        return f

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------------- fixtures ----------------
SEQ = [0]


def user(tag):
    SEQ[0] += 1
    name = "%s%d_%s" % (tag, SEQ[0], RUN)
    r = call("POST", N0 + "/api/auth/register", {"username": name, "password": PW})
    if r.status != 201 or not r.get("token"):
        raise Setup("register %s got %s" % (name, r.status))
    return {"name": name, "tok": r.get("token"), "id": r.get("user", "id")}


def workspace(owner, tag):
    SEQ[0] += 1
    slug = "%s%d-%s" % (tag, SEQ[0], RUN)
    r = call("POST", N0 + "/api/workspaces", {"slug": slug, "name": "WS " + tag}, owner["tok"])
    if r.status != 201 or r.get("general_channel", "id") is None:
        raise Setup("create workspace got %s" % r.status)
    return {"slug": slug, "id": r.get("workspace", "id"), "general": r.get("general_channel", "id")}


def join(u, cid):
    r = call("POST", "%s/api/channels/%s/join" % (N0, cid), None, u["tok"])
    if r.status != 200:
        raise Setup("join channel %s got %s" % (cid, r.status))


def channel(u, ws, name, **kw):
    r = call("POST", "%s/api/workspaces/%s/channels" % (N0, ws["slug"]), dict(name=name, **kw), u["tok"])
    if r.status != 201 or r.get("channel", "id") is None:
        raise Setup("create channel %s got %s" % (name, r.status))
    return r.get("channel", "id")


def post(u, cid, body, node=N0, **kw):
    r = call("POST", "%s/api/channels/%s/messages" % (node, cid), dict(body=body, **kw), u["tok"])
    if r.status != 201 or r.get("message", "id") is None:
        raise Setup("post message got %s" % r.status)
    return r.get("message")


def section(rules, fn):
    """Run one section; if its setup breaks, fail every rule of it that has not been reported yet."""
    if ONLY and not any(r.split(".")[0] in ONLY for r in rules):
        return
    before = passed + failed
    try:
        fn()
    except Setup as e:
        reported = passed + failed - before
        for r in rules[reported:]:
            bad(r, "setup failed (%s); fix rule 1.3 and earlier sections first" % e)
    except Exception as e:  # never crash the whole check
        reported = passed + failed - before
        for r in rules[reported:]:
            bad(r, "check error: %r" % e)


ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
USER_FIELDS = ["id", "username", "display_name", "timezone", "avatar_url", "status_text", "status_emoji"]
CH_FIELDS = ["id", "workspace_id", "name", "is_private", "is_dm", "topic", "is_archived"]


def ids(items):
    return [m.get("id") for m in items or []]


# ---------------- 1. baseline ----------------
def s1():
    st = [call("GET", n + "/api/health") for n in NODES]
    got = [(r.status, r.get("status"), r.get("node_id")) for r in st]
    check("1.1", got == [(200, "ok", 0), (200, "ok", 1), (200, "ok", 2)], "health ok with node_id 0/1/2", "got %s" % got)
    a = user("own")
    w1 = WS(8000, "not-a-real-token-000000"); w2 = WS(8000, a["tok"])
    check("1.2", w1.status != "101" and w2.status == "101", "websocket refuses a bad token and accepts a real one",
          "bad token got %s (must not be 101), real token got %s (want 101)" % (w1.status, w2.status))
    w1.close(); w2.close()
    b = user("mem")
    ws = workspace(a, "base")
    r = call("POST", "%s/api/channels/%s/join" % (N1, ws["general"]), None, b["tok"])
    check("1.3", r.status == 200, "register, create workspace and join #general work", "join got %s" % r.status)


# ---------------- 2. channel management ----------------
def s2():
    own, adm, mem, out = user("own"), user("adm"), user("mem"), user("out")
    ws = workspace(own, "chan")
    for u in (adm, mem):
        join(u, ws["general"])
    call("PATCH", "%s/api/workspaces/%s/members/%s" % (N0, ws["slug"], adm["id"]), {"role": "admin"}, own["tok"])
    C = "%s/api/workspaces/%s/channels" % (N0, ws["slug"])

    r = call("POST", C, {"name": "release"}, mem["tok"])
    ch = r.get("channel") or {}
    want = {"name": "release", "workspace_id": ws["id"], "is_private": False, "is_dm": False, "topic": "", "is_archived": False}
    got = {k: ch.get(k) for k in want}
    check("2.1", r.status == 201 and got == want and all(k in ch for k in CH_FIELDS), "create channel returns a full ChannelObj",
          "got %s %s" % (r.status, got))
    rel = ch.get("id")

    a = call("POST", C, {"name": "planning", "topic": "Release planning"}, mem["tok"])
    b = call("POST", C, {"name": "described", "description": "x"}, mem["tok"])
    check("2.2", (a.status, a.get("channel", "topic"), b.status, b.get("channel", "topic")) == (201, "Release planning", 201, ""),
          "topic on create, description ignored",
          "topic create %s '%s', description create %s topic '%s'" % (a.status, a.get("channel", "topic"), b.status, b.get("channel", "topic")))

    ws2 = workspace(own, "other")
    a = call("POST", C, {"name": "release"}, own["tok"])
    b = call("POST", "%s/api/workspaces/%s/channels" % (N1, ws2["slug"]), {"name": "release"}, own["tok"])
    check("2.3", (a.status, b.status) == (409, 201), "duplicate name in workspace is 409, other workspace is fine",
          "same workspace %s (want 409), other workspace %s (want 201)" % (a.status, b.status))

    got = [call("POST", C, {"name": n}, own["tok"]).status for n in ("Bad Name", "UPPER", "", "a" * 33)]
    check("2.4", got == [400] * 4, "invalid channel names rejected", "'Bad Name','UPPER','',33 chars got %s (want 400 each)" % got)

    a = call("POST", C, {"name": "long-topic", "topic": "t" * 251}, own["tok"]).status
    b = call("PATCH", "%s/api/channels/%s" % (N0, rel), {"topic": "t" * 251}, mem["tok"]).status
    c = call("PATCH", "%s/api/channels/%s" % (N0, rel), {"topic": "t" * 250}, mem["tok"]).status
    check("2.5", (a, b, c) == (400, 400, 200), "topic longer than 250 rejected", "create 251 %s, patch 251 %s, patch 250 %s (want 400 400 200)" % (a, b, c))

    a = call("PATCH", "%s/api/channels/%s" % (N1, rel), {"topic": "Ship it"}, mem["tok"])
    b = call("PATCH", "%s/api/channels/%s" % (N0, rel), {"topic": "nope"}, adm["tok"])
    c = call("PATCH", "%s/api/channels/%s" % (N0, rel), {"topic": "Owner topic"}, own["tok"])
    check("2.6", (a.status, a.get("channel", "topic"), b.status, c.status, c.get("channel", "topic")) == (200, "Ship it", 403, 200, "Owner topic"),
          "topic patch by member, non-member and owner",
          "member %s '%s' (want 200), non-member %s (want 403), owner %s (want 200)" % (a.status, a.get("channel", "topic"), b.status, c.status))

    priv = channel(adm, ws, "admins-only", is_private=True)
    a = call("GET", "%s/api/workspaces/%s" % (N2, ws["slug"]), None, mem["tok"])
    names = [c.get("name") for c in a.get("channels") or []]
    rs = a.get("read_state")
    b = call("GET", "%s/api/workspaces/%s" % (N0, ws["slug"]), None, adm["tok"])
    bn = [c.get("name") for c in b.get("channels") or []]
    good = (a.status == 200 and a.get("workspace", "slug") == ws["slug"] and "general" in names and "release" in names
            and "admins-only" not in names and "admins-only" in bn and isinstance(rs, list) and len(rs) == len(names))
    check("2.7", good, "workspace detail lists visible channels and read_state",
          "status %s, member sees %s (want general, release, not admins-only), admin sees admins-only=%s, read_state entries %s for %d channels"
          % (a.status, names, "admins-only" in bn, len(rs) if isinstance(rs, list) else rs, len(names)))

    arch = channel(own, ws, "old-stuff")
    call("POST", "%s/api/channels/%s/archive" % (N0, arch), None, own["tok"])
    a = [c.get("name") for c in call("GET", "%s/api/workspaces/%s" % (N0, ws["slug"]), None, own["tok"]).get("channels") or []]
    r1 = call("GET", "%s/api/workspaces/%s?include_archived=1" % (N0, ws["slug"]), None, own["tok"])
    r2 = call("GET", "%s/api/workspaces/%s?include_archived=true" % (N0, ws["slug"]), None, own["tok"])
    f1 = [c.get("is_archived") for c in r1.get("channels") or [] if c.get("name") == "old-stuff"]
    f2 = [c.get("name") for c in r2.get("channels") or []]
    check("2.8", "old-stuff" not in a and f1 == [True] and "old-stuff" in f2, "archived channels only with include_archived",
          "default lists it=%s (want no), =1 gives %s (want [true]), =true lists it=%s (want yes)" % ("old-stuff" in a, f1, "old-stuff" in f2))

    join(adm, rel)
    a = call("POST", "%s/api/channels/%s/leave" % (N0, rel), None, adm["tok"])
    b = call("POST", "%s/api/channels/%s/leave" % (N1, rel), None, adm["tok"])
    m = call("GET", "%s/api/channels/%s/members" % (N0, rel), None, mem["tok"])
    still = adm["id"] in ids(m.get("members"))
    check("2.9", (a.status, b.status, a.get("channel", "id") is not None, still) == (200, 200, True, False), "leave is idempotent",
          "leave %s, again %s (want 200 200 with channel), still a member=%s" % (a.status, b.status, still))

    m = call("GET", "%s/api/channels/%s/members" % (N2, rel), None, own["tok"])
    mems = m.get("members") or []
    full = all(all(k in u for k in USER_FIELDS) for u in mems)
    check("2.10", m.status == 200 and mem["id"] in ids(mems) and full, "channel members are full UserObjs",
          "status %s, creator listed=%s, all 7 UserObj fields=%s" % (m.status, mem["id"] in ids(mems), full))

    a = call("POST", "%s/api/channels/%s/archive" % (N0, rel), None, adm["tok"]).status
    b = call("POST", "%s/api/channels/%s/archive" % (N0, rel), None, mem["tok"]).status
    c = call("POST", "%s/api/channels/%s/archive" % (N1, rel), None, own["tok"])
    check("2.11", (a, b, c.status, c.get("channel", "is_archived")) == (403, 403, 200, True), "only the owner archives",
          "admin %s, member %s (want 403), owner %s is_archived=%s (want 200 true)" % (a, b, c.status, c.get("channel", "is_archived")))

    a = call("POST", "%s/api/channels/%s/messages" % (N0, rel), {"body": "hello"}, mem["tok"]).status
    b = call("POST", "%s/api/channels/%s/messages" % (N2, rel), {"body": "hello"}, own["tok"]).status
    c = call("GET", "%s/api/channels/%s/messages" % (N0, rel), None, mem["tok"]).status
    check("2.12", (a, b, c) == (423, 423, 200), "archived channel is read-only", "post %s %s (want 423), read %s (want 200)" % (a, b, c))

    a = call("POST", "%s/api/channels/%s/unarchive" % (N0, rel), None, mem["tok"]).status
    b = call("POST", "%s/api/channels/%s/unarchive" % (N0, rel), None, own["tok"])
    c = call("POST", "%s/api/channels/%s/messages" % (N1, rel), {"body": "back"}, mem["tok"]).status
    check("2.13", (a, b.status, b.get("channel", "is_archived"), c) == (403, 200, False, 201), "owner unarchives and writes work again",
          "member %s (want 403), owner %s is_archived=%s (want 200 false), post %s (want 201)" % (a, b.status, b.get("channel", "is_archived"), c))

    U = "%s/api/channels/999999999" % N0
    got = [call("POST", U + "/join", None, mem["tok"]).status, call("POST", U + "/leave", None, mem["tok"]).status,
           call("PATCH", U, {"topic": "x"}, own["tok"]).status, call("GET", U + "/members", None, mem["tok"]).status,
           call("POST", U + "/archive", None, own["tok"]).status, call("GET", U + "/messages", None, mem["tok"]).status,
           call("POST", U + "/messages", {"body": "x"}, mem["tok"]).status, call("GET", U + "/read", None, mem["tok"]).status]
    na = [call("GET", "%s/api/channels/%s/members" % (N0, rel)).status, call("POST", "%s/api/channels/%s/messages" % (N0, rel), {"body": "x"}).status,
          call("GET", "%s/api/workspaces/%s" % (N0, ws["slug"])).status]
    check("2.14", got == [404] * 8 and na == [401] * 3, "unknown channel is 404, no token is 401",
          "unknown id on join/leave/patch/members/archive/list/post/read got %s (want 404), no token got %s (want 401)" % (got, na))


# ---------------- 3. messages and threads ----------------
def s3():
    own, bob, out = user("own"), user("bob"), user("out")
    ws = workspace(own, "msg")
    join(bob, ws["general"])
    cid = channel(own, ws, "talk")
    join(bob, cid)
    M = "%s/api/channels/%s/messages" % (N0, cid)

    r = call("POST", M, {"body": "hello world"}, bob["tok"])
    m = r.get("message") or {}
    want = {"body": "hello world", "author_id": bob["id"], "parent_id": None, "reply_count": 0, "edited_at": None,
            "files": [], "reactions": [], "mentions": [], "channel_id": cid}
    got = {k: m.get(k) for k in want}
    good = (r.status == 201 and got == want and (m.get("author") or {}).get("username") == bob["name"]
            and ISO.match(str(m.get("created_at"))) is not None and m.get("id") is not None)
    check("3.1", good, "post returns a full MessageObj", "got %s %s author=%s created_at=%s" % (r.status, got, (m.get("author") or {}).get("username"), m.get("created_at")))
    top = m.get("id")

    got = [call("POST", M, b, bob["tok"]).status for b in ({"body": ""}, {"body": "   "}, {})]
    check("3.2", got == [400] * 3, "empty body rejected", "'', '   ', missing got %s (want 400 each)" % got)

    priv = channel(own, ws, "secret", is_private=True)
    a = call("POST", "%s/api/channels/%s/messages" % (N0, priv), {"body": "hi"}, bob["tok"]).status
    b = call("GET", "%s/api/channels/%s/messages" % (N0, priv), None, bob["tok"]).status
    check("3.3", (a, b) == (403, 403), "non-member of a private channel cannot post or read", "post %s, list %s (want 403 403)" % (a, b))

    r1 = call("POST", "%s/api/channels/%s/messages" % (N1, cid), {"body": "reply one", "parent_id": top}, own["tok"])
    r2 = call("POST", "%s/api/channels/%s/messages" % (N2, cid), {"body": "reply two", "parent_id": top}, bob["tok"])
    lst = call("GET", M, None, bob["tok"]).get("messages") or []
    rc = [x.get("reply_count") for x in lst if x.get("id") == top]
    check("3.4", (r1.status, r2.status, r1.get("message", "parent_id"), rc) == (201, 201, top, [2]), "replies set parent_id and bump reply_count",
          "replies %s %s parent_id=%s (want %s), parent reply_count %s (want [2])" % (r1.status, r2.status, r1.get("message", "parent_id"), top, rc))

    a = call("GET", "%s/api/messages/%s/replies" % (N0, top), None, bob["tok"])
    bodies = [x.get("body") for x in a.get("replies") or []]
    in_top = any(x.get("parent_id") for x in lst)
    p1 = call("GET", "%s/api/messages/%s/replies?limit=1" % (N0, top), None, bob["tok"])
    p2 = call("GET", "%s/api/messages/%s/replies?limit=1&cursor=%s" % (N1, top, p1.get("next_cursor")), None, bob["tok"])
    unk = call("GET", "%s/api/messages/999999999/replies" % N0, None, bob["tok"]).status
    good = (a.status == 200 and bodies == ["reply one", "reply two"] and "next_cursor" in (a.body or {}) and a.get("next_cursor") is None
            and not in_top and [x.get("body") for x in p1.get("replies") or []] == ["reply one"]
            and [x.get("body") for x in p2.get("replies") or []] == ["reply two"] and unk == 404)
    check("3.5", good, "replies list oldest-first and paged",
          "status %s bodies %s (want reply one, reply two; next_cursor null), replies in channel listing=%s, limit=1 pages %s / %s, unknown parent %s (want 404)"
          % (a.status, bodies, in_top, [x.get("body") for x in p1.get("replies") or []], [x.get("body") for x in p2.get("replies") or []], unk))

    e = call("PATCH", "%s/api/messages/%s" % (N1, top), {"body": "hello again"}, bob["tok"])
    f = call("PATCH", "%s/api/messages/%s" % (N0, top), {"body": "hijack"}, own["tok"]).status
    good = e.status == 200 and e.get("message", "body") == "hello again" and ISO.match(str(e.get("message", "edited_at"))) is not None and f == 403
    check("3.6", good, "author edits, others cannot", "author %s body '%s' edited_at %s (want 200), other user %s (want 403)"
          % (e.status, e.get("message", "body"), e.get("message", "edited_at"), f))

    d1 = post(bob, cid, "delete me")
    d2 = post(bob, cid, "owner deletes me")
    a = call("DELETE", "%s/api/messages/%s" % (N0, d1["id"]), None, bob["tok"])
    b = call("DELETE", "%s/api/messages/%s" % (N2, d1["id"]), None, bob["tok"])
    c = call("DELETE", "%s/api/messages/%s" % (N0, d2["id"]), None, out["tok"]).status
    d = call("DELETE", "%s/api/messages/%s" % (N0, d2["id"]), None, own["tok"])
    e = call("PATCH", "%s/api/messages/%s" % (N0, d1["id"]), {"body": "zombie"}, bob["tok"]).status
    left = ids(call("GET", M, None, bob["tok"]).get("messages"))
    good = ((a.status, a.get("deleted"), b.status, b.get("deleted"), c, d.status, e) == (200, True, 200, True, 403, 200, 409)
            and d1["id"] not in left and d2["id"] not in left)
    check("3.7", good, "delete by author or owner, idempotent, then edit is 409",
          "author %s/%s again %s/%s, stranger %s (want 403), owner %s (want 200), edit deleted %s (want 409), still listed=%s"
          % (a.status, a.get("deleted"), b.status, b.get("deleted"), c, d.status, e, d1["id"] in left or d2["id"] in left))

    R_ = "%s/api/messages/%s/reactions" % (N0, top)
    a = call("POST", R_, {"emoji": "tada"}, bob["tok"])
    b = call("POST", R_, {"emoji": "tada"}, bob["tok"])
    c = call("POST", "%s/api/messages/%s/reactions" % (N1, top), {"emoji": "tada"}, own["tok"])
    d = call("DELETE", R_ + "/tada", None, bob["tok"])
    ra, rb, rc_, rd = (x.get("reactions") or [] for x in (a, b, c, d))
    good = ((a.status, b.status, c.status, d.status) == (200, 200, 200, 200)
            and [(x.get("emoji"), x.get("count"), x.get("user_ids")) for x in ra] == [("tada", 1, [bob["id"]])]
            and [x.get("count") for x in rb] == [1] and [x.get("count") for x in rc_] == [2]
            and [(x.get("count"), x.get("user_ids")) for x in rd] == [(1, [own["id"]])])
    check("3.8", good, "reactions are idempotent per user and removable",
          "statuses %s, counts after add/again/second user/remove %s %s %s %s"
          % ([a.status, b.status, c.status, d.status], ra, [x.get("count") for x in rb], [x.get("count") for x in rc_], rd))


# ---------------- 4. paging ----------------
def s4():
    own = user("own")
    ws = workspace(own, "page")
    cid = channel(own, ws, "history")
    M = "%s/api/channels/%s/messages" % (N0, cid)
    sent = [post(own, cid, "m%d" % i)["id"] for i in range(5)]
    newest = list(reversed(sent))

    a = call("GET", M, None, own["tok"])
    got = ids(a.get("messages"))
    check("4.1", a.status == 200 and got == newest and "next_cursor" in (a.body or {}) and a.get("next_cursor") is None,
          "list is newest-first with next_cursor null", "status %s order %s (want %s), next_cursor %r" % (a.status, got, newest, a.get("next_cursor")))

    p1 = call("GET", M + "?limit=2", None, own["tok"])
    c1 = p1.get("next_cursor")
    p2 = call("GET", M + "?limit=2&cursor=%s" % c1, None, own["tok"])
    c2 = p2.get("next_cursor")
    p3 = call("GET", M + "?limit=2&cursor=%s" % c2, None, own["tok"])
    pages = [ids(p.get("messages")) for p in (p1, p2, p3)]
    good = (pages == [newest[:2], newest[2:4], newest[4:]] and isinstance(c1, str) and isinstance(c2, str)
            and "next_cursor" in (p3.body or {}) and p3.get("next_cursor") is None)
    check("4.2", good, "limit=2 pages through 5 messages", "pages %s (want %s), cursors %r %r %r" % (pages, [newest[:2], newest[2:4], newest[4:]], c1, c2, p3.get("next_cursor")))

    post(own, cid, "late arrival 1"); post(own, cid, "late arrival 2")
    again = ids(call("GET", M + "?limit=2&cursor=%s" % c1, None, own["tok"]).get("messages"))
    check("4.3", again == newest[2:4], "cursor still resumes from the same position after new messages", "got %s (want %s)" % (again, newest[2:4]))

    o1 = ids(call("GET", "%s/api/channels/%s/messages?limit=2&cursor=%s" % (N1, cid, c1), None, own["tok"]).get("messages"))
    o2 = ids(call("GET", "%s/api/channels/%s/messages?limit=2&cursor=%s" % (N2, cid, c1), None, own["tok"]).get("messages"))
    check("4.4", o1 == o2 == newest[2:4], "cursor from 8000 works on 8001 and 8002", "8001 %s, 8002 %s (want %s)" % (o1, o2, newest[2:4]))

    s = call("GET", M + "?cursor=not-a-cursor", None, own["tok"]).status
    check("4.5", s == 400, "malformed cursor", "got %s (want 400)" % s)

    got = [call("GET", M + "?limit=" + v, None, own["tok"]).status for v in ("0", "201", "-1", "abc", "200")]
    cid2 = channel(own, ws, "busy")
    for i in range(55):
        post(own, cid2, "bulk %d" % i, node=NODES[i % 3])
    d = call("GET", "%s/api/channels/%s/messages" % (N0, cid2), None, own["tok"])
    check("4.6", got == [400, 400, 400, 400, 200] and len(d.get("messages") or []) == 50 and isinstance(d.get("next_cursor"), str),
          "limit validation and default 50", "limit 0/201/-1/abc/200 got %s (want 400 x4, 200); default returned %d with cursor %r (want 50 and a cursor)"
          % (got, len(d.get("messages") or []), d.get("next_cursor")))

    first = ids(call("GET", M + "?limit=2", None, own["tok"]).get("messages"))
    a = ids(call("GET", M + "?limit=2&offset=2", None, own["tok"]).get("messages"))
    b = ids(call("GET", M + "?limit=2&page=2", None, own["tok"]).get("messages"))
    check("4.7", a == b == first, "offset and page are ignored", "offset=2 %s, page=2 %s (want first page %s)" % (a, b, first))


# ---------------- 5. search and pins ----------------
def s5():
    own, bob, out = user("own"), user("bob"), user("out")
    ws = workspace(own, "srch")
    ws2 = workspace(bob, "srch2")
    join(bob, ws["general"])
    word = "zebra%s" % RUN
    m1 = post(own, ws["general"], "first %s here" % word)
    priv = channel(own, ws, "hidden", is_private=True)
    mp = post(own, priv, "secret %s" % word)
    m2 = post(bob, ws2["general"], "other workspace %s" % word.upper())
    m3 = post(bob, ws["general"], "last %s" % word)

    a = call("GET", "%s/api/search?q=%s" % (N1, word), None, bob["tok"])
    got = ids(a.get("results"))
    check("5.1", a.status == 200 and got == [m3["id"], m2["id"], m1["id"]] and "next_cursor" in (a.body or {}),
          "search finds matching messages newest-first, case-insensitive", "status %s ids %s (want %s)" % (a.status, got, [m3["id"], m2["id"], m1["id"]]))
    check("5.2", mp["id"] not in got and mp["id"] in ids(call("GET", "%s/api/search?q=%s" % (N0, word), None, own["tok"]).get("results")),
          "private messages only for members", "bob sees private message=%s (want no), owner sees it (want yes)" % (mp["id"] in got))
    b = call("GET", "%s/api/search?q=%s&workspace_id=%s" % (N0, word, ws["id"]), None, bob["tok"])
    c = call("GET", "%s/api/search?q=%s&workspace_id=%s&limit=1" % (N0, word, ws["id"]), None, bob["tok"])
    d = call("GET", "%s/api/search?q=%s&workspace_id=%s&limit=1&cursor=%s" % (N2, word, ws["id"], c.get("next_cursor")), None, bob["tok"])
    check("5.3", ids(b.get("results")) == [m3["id"], m1["id"]] and ids(c.get("results")) == [m3["id"]] and ids(d.get("results")) == [m1["id"]],
          "search filtered by workspace and paged", "filtered %s (want %s), pages %s %s" % (ids(b.get("results")), [m3["id"], m1["id"]], ids(c.get("results")), ids(d.get("results"))))

    a = call("POST", "%s/api/messages/%s/pin" % (N0, m1["id"]), None, bob["tok"])
    b = call("POST", "%s/api/messages/%s/pin" % (N1, m1["id"]), None, bob["tok"])
    pin = a.get("pin") or {}
    good = (a.status, b.status, pin.get("message_id"), pin.get("pinned_by")) == (200, 200, m1["id"], bob["id"]) and ISO.match(str(pin.get("pinned_at")))
    call("POST", "%s/api/messages/%s/pin" % (N0, m3["id"]), None, own["tok"])
    lst = call("GET", "%s/api/channels/%s/pins" % (N2, ws["general"]), None, bob["tok"])
    order = [(p.get("message") or {}).get("id") for p in lst.get("pins") or []]
    check("5.4", bool(good) and order.count(m1["id"]) == 1, "pin is idempotent",
          "pin %s again %s pin=%s, times listed %d (want 200 200, once)" % (a.status, b.status, pin, order.count(m1["id"])))
    pp = post(own, priv, "pin me")
    call("POST", "%s/api/messages/%s/pin" % (N0, pp["id"]), None, own["tok"])
    np_ = call("GET", "%s/api/channels/%s/pins" % (N0, priv), None, bob["tok"]).status
    first = (lst.get("pins") or [{}])[0]
    check("5.5", lst.status == 200 and order == [m3["id"], m1["id"]] and first.get("pinned_by") == own["id"] and "next_cursor" in (lst.body or {}) and np_ == 403,
          "pin list newest-first, private list is 403 for non-members", "status %s order %s (want %s), private list %s (want 403)" % (lst.status, order, [m3["id"], m1["id"]], np_))
    a = call("DELETE", "%s/api/messages/%s/pin" % (N0, m1["id"]), None, bob["tok"])
    order = [(p.get("message") or {}).get("id") for p in call("GET", "%s/api/channels/%s/pins" % (N1, ws["general"]), None, bob["tok"]).get("pins") or []]
    check("5.6", (a.status, a.get("unpinned"), order) == (200, True, [m3["id"]]), "unpin", "got %s %s, list %s (want 200 true, [%s])" % (a.status, a.get("unpinned"), order, m3["id"]))


# ---------------- 6. mentions ----------------
def s6():
    own, bob, cat, out = user("own"), user("bob"), user("cat"), user("out")
    ws = workspace(own, "ment")
    ws2 = workspace(out, "ment2")
    for u in (bob, cat):
        join(u, ws["general"])
    g = ws["general"]
    B, C = bob["name"], cat["name"]

    m = [post(own, g, t).get("mentions") for t in ("@%s hello" % B, "hi @%s, see this" % B, "(@%s)" % B)]
    check("6.1", m == [[bob["id"]]] * 3, "@username resolves to the user id", "got %s (want [[%s]] x3)" % (m, bob["id"]))
    a = post(own, g, "@%s @%s and @%s" % (B, B, own["name"])).get("mentions")
    check("6.2", a == [bob["id"]], "mentions deduplicated, no self-mention", "got %s (want [%s])" % (a, bob["id"]))
    got = [post(own, g, t).get("mentions") for t in ("mail %s@example.com" % B, "x@%s" % B, "@nobody_%s" % RUN, "@%s" % out["name"])]
    check("6.3", got == [[]] * 4, "email, in-word, unknown and outside users are not mentions", "got %s (want [] x4)" % got)
    a = post(bob, g, "@channel heads up @%s" % C).get("mentions")
    check("6.4", sorted(map(str, a or [])) == sorted(map(str, [own["id"], cat["id"]])), "@channel expands to channel members except author",
          "got %s (want %s in any order)" % (a, [own["id"], cat["id"]]))
    cid = channel(own, ws, "count")
    join(bob, cid)
    r0 = call("GET", "%s/api/channels/%s/read" % (N0, cid), None, bob["tok"]).get("read_state", "mention_count")
    post(own, cid, "ping @%s" % B)
    post(bob, cid, "note to self @%s" % B)
    r1 = call("GET", "%s/api/channels/%s/read" % (N1, cid), None, bob["tok"]).get("read_state", "mention_count")
    check("6.5", (r0, r1) == (0, 1), "mention_count counts mentions by others only", "before %s after %s (want 0 then 1)" % (r0, r1))


# ---------------- 7. read state ----------------
def s7():
    own, bob = user("own"), user("bob")
    ws = workspace(own, "read")
    cid = channel(own, ws, "unread")
    join(bob, cid)
    for t in ("one", "two @%s" % bob["name"], "three"):
        post(own, cid, t)
    post(bob, cid, "my own message")
    a = call("GET", "%s/api/channels/%s/read" % (N2, cid), None, bob["tok"])
    rs = a.get("read_state") or {}
    good = (a.status == 200 and (rs.get("unread_count"), rs.get("mention_count")) == (3, 1) and isinstance(rs.get("last_read_event_id"), int)
            and str(rs.get("channel_id")) == str(cid))
    check("7.1", good, "read state counts unread and mentions", "status %s read_state %s (want unread 3, mention 1)" % (a.status, rs))
    w = WS(8001, bob["tok"])
    sub = w.subscribe(cid) or {}
    w.close()
    h = sub.get("head_event_id")
    if not isinstance(h, int):
        raise Setup("subscribe did not return an integer head_event_id (rule 8.1): %s" % sub)
    b = call("POST", "%s/api/channels/%s/read" % (N0, cid), {"last_read_event_id": h}, bob["tok"])
    rs = b.get("read_state") or {}
    check("7.2", b.status == 200 and (rs.get("last_read_event_id"), rs.get("unread_count"), rs.get("mention_count")) == (h, 0, 0),
          "marking the head as read clears counts", "status %s read_state %s (want last_read %s, 0, 0)" % (b.status, rs, h))
    c = call("POST", "%s/api/channels/%s/read" % (N1, cid), {"last_read_event_id": 1}, bob["tok"]).get("read_state", "last_read_event_id")
    d = call("POST", "%s/api/channels/%s/read" % (N1, cid), {"last_read_event_id": h + 1000}, bob["tok"]).get("read_state", "last_read_event_id")
    e = [call("POST", "%s/api/channels/%s/read" % (N0, cid), body, bob["tok"]).status for body in ({}, {"last_read_event_id": -1}, {"last_read_event_id": "x"})]
    check("7.3", (c, d, e) == (h, h, [400] * 3), "read cursor never moves backwards and is capped at head",
          "after posting 1 got %s (want %s), after head+1000 got %s (want %s), bad values %s (want 400 x3)" % (c, h, d, h, e))
    post(own, cid, "new one")
    det = call("GET", "%s/api/workspaces/%s" % (N0, ws["slug"]), None, bob["tok"])
    ent = [x for x in det.get("read_state") or [] if isinstance(x, dict) and str(x.get("channel_id")) == str(cid)]
    direct = call("GET", "%s/api/channels/%s/read" % (N0, cid), None, bob["tok"]).get("read_state")
    check("7.4", ent == [direct] and (direct or {}).get("unread_count") == 1, "workspace detail embeds the same read_state",
          "detail entry %s, channel route %s (want equal, unread 1)" % (ent, direct))


# ---------------- 8. events ----------------
def s8():
    own, bob, cat = user("own"), user("bob"), user("cat")
    ws = workspace(own, "evt")
    cid = channel(own, ws, "events")
    join(bob, cid)
    join(cat, ws["general"])
    w1, w2 = WS(8001, bob["tok"]), WS(8002, own["tok"])
    try:
        s1_, s2_ = w1.subscribe(cid) or {}, w2.subscribe(cid) or {}
        h = s1_.get("head_event_id")
        good = (s1_.get("type") == "subscribed" and str(s1_.get("channel_id")) == str(cid) and isinstance(h, int)
                and s2_.get("head_event_id") == h)
        check("8.1", good, "subscribe answers with subscribed and head_event_id", "8001 got %s, 8002 got %s" % (s1_, s2_))
        if not isinstance(h, int):
            raise Setup("no head_event_id from subscribe")
        t0 = time.time()
        m = post(own, cid, "live one", node=N0)
        e1 = w1.events(1, 2.0)
        e2 = w2.events(1, 2.0)
        f = e1[0] if e1 else {}
        good = (f.get("type") == "message.created" and f.get("event_id") == h + 1 and str(f.get("channel_id")) == str(cid)
                and (f.get("message") or {}).get("id") == m["id"])
        check("8.2", good, "message.created arrives with event_id head+1",
              "got %s within %.1fs (want type message.created, event_id %d, message.id %s)" % (str(f)[:200], time.time() - t0, h + 1, m["id"]))
        g = e2[0] if e2 else {}
        check("8.3", bool(e1) and bool(e2) and (g.get("type"), g.get("event_id")) == (f.get("type"), f.get("event_id")) and good,
              "the same event reaches subscribers on 8001 and 8002", "8001 %s, 8002 %s" % (str(f)[:120], str(g)[:120]))

        rep = post(bob, cid, "a reply", node=N1, parent_id=m["id"])
        call("PATCH", "%s/api/messages/%s" % (N2, m["id"]), {"body": "edited"}, own["tok"])
        call("POST", "%s/api/messages/%s/reactions" % (N0, m["id"]), {"emoji": "fire"}, bob["tok"])
        call("DELETE", "%s/api/messages/%s/reactions/fire" % (N1, m["id"]), None, bob["tok"])
        call("DELETE", "%s/api/messages/%s" % (N2, rep["id"]), None, bob["tok"])
        call("POST", "%s/api/channels/%s/join" % (N0, cid), None, cat["tok"])
        call("POST", "%s/api/channels/%s/leave" % (N1, cid), None, cat["tok"])
        call("PATCH", "%s/api/channels/%s" % (N2, cid), {"topic": "new topic"}, own["tok"])
        call("POST", "%s/api/channels/%s/archive" % (N0, cid), None, own["tok"])
        call("POST", "%s/api/channels/%s/unarchive" % (N1, cid), None, own["tok"])
        kinds = ["message.reply", "message.updated", "reaction.added", "reaction.removed", "message.deleted",
                 "member.joined", "member.left", "channel.updated", "channel.archived", "channel.unarchived"]
        ev = w1.events(len(kinds), 3.0)
        got = [(e.get("type"), e.get("event_id")) for e in ev]
        want = [(k, h + 2 + i) for i, k in enumerate(kinds)]
        w2.events(len(kinds), 1.0)
        check("8.4", got == want, "each change produces one event of the right kind, ids consecutive", "got %s (want %s)" % (got, want))

        base = h + 1 + len(kinds)
        res = []
        th = [threading.Thread(target=lambda i=i: res.append(call("POST", "%s/api/channels/%s/messages" % (NODES[i % 3], cid),
                                                                   {"body": "burst %d" % i}, own["tok"]).status)) for i in range(6)]
        for t in th:
            t.start()
        for t in th:
            t.join()
        a = sorted(e.get("event_id") for e in w1.events(6, 3.0))
        b = sorted(e.get("event_id") for e in w2.events(6, 3.0))
        want = list(range(base + 1, base + 7))
        check("8.5", sorted(res) == [201] * 6 and a == want and b == want, "concurrent posts on three nodes get dense event ids",
              "statuses %s, 8001 saw %s, 8002 saw %s (want %s)" % (res, a, b, want))

        w3 = WS(8000, own["tok"])
        w3.send({"type": "resume", "channel_id": cid, "since_event_id": h})
        end, seen = w3.until(lambda f: isinstance(f, dict) and f.get("type") == "resumed", 3.0)
        w3.close()
        evs = [f.get("event_id") for f in seen if isinstance(f, dict) and "event_id" in f]
        want = list(range(h + 1, base + 7))
        check("8.6", evs == want and end is not None and end.get("head_event_id") == base + 6, "resume replays missed events then sends resumed",
              "replayed %s (want %s), final frame %s (want resumed with head %d)" % (evs, want, end, base + 6))

        priv = channel(own, ws, "private-evt", is_private=True)
        w4 = WS(8000, bob["tok"])
        f = w4.subscribe(priv) or {}
        post(own, priv, "you should not see this")
        leak = w4.events(1, 1.0)
        w4.close()
        check("8.7", f.get("type") == "error" and not leak, "private channel subscription is refused for non-members",
              "got %s, events leaked %s (want an error frame and no events)" % (f, leak))
    finally:
        w1.close(); w2.close()


def main():
    healthy_now = healthy()
    if not healthy_now:
        subprocess.Popen("cd /app && nohup /app/start.sh >/tmp/start.log 2>&1 &", shell=True)
        for _ in range(15):
            time.sleep(1)
            if healthy():
                healthy_now = True
                break
    if not healthy_now:
        print("FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)")
        print("RESULT: 0 passed, 1 failed")
        sys.exit(1)
    section(["1.1", "1.2", "1.3"], s1)
    section(["2.%d" % i for i in range(1, 15)], s2)
    section(["3.%d" % i for i in range(1, 9)], s3)
    section(["4.%d" % i for i in range(1, 8)], s4)
    section(["5.%d" % i for i in range(1, 7)], s5)
    section(["6.%d" % i for i in range(1, 6)], s6)
    section(["7.%d" % i for i in range(1, 5)], s7)
    section(["8.%d" % i for i in range(1, 8)], s8)
    print("RESULT: %d passed, %d failed" % (passed, failed))
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

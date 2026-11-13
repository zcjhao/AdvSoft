"""Shared helpers for the checks in this folder (Python standard library only).

Do not run or edit this file. The check_*.sh scripts next to it load it.
"""
import base64, json, os, random, re, socket, subprocess, sys, threading, time, urllib.error, urllib.request, uuid

NODES = ["http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8002"]
N0, N1, N2 = NODES
PORTS = [8000, 8001, 8002]
FEEDBACK = os.environ.get("FEEDBACK", "verbose")
RUN = "%d%d" % (int(time.time()) % 100000, random.randint(100, 999))
PW = "correct-horse-1"
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$")
USER_FIELDS = ["id", "username", "display_name", "timezone", "avatar_url", "status_text", "status_emoji"]
MSG_FIELDS = ["id", "channel_id", "author_id", "author", "body", "parent_id", "reply_count", "created_at",
              "edited_at", "files", "reactions", "mentions"]

passed = 0
failed = 0


# ---------------- output (the testing mode is set in lib.sh) ----------------
def ok(rule, desc):
    global passed
    passed += 1
    if FEEDBACK == "verbose":
        print("PASS %s %s" % (rule, desc), flush=True)


def bad(rule, desc):
    global failed
    failed += 1
    if FEEDBACK == "verbose":
        print("FAIL %s %s" % (rule, desc), flush=True)


def check(rule, cond, desc, detail):
    if cond:
        ok(rule, desc)
    else:
        bad(rule, "%s: %s" % (desc, detail))


def short(v, n=160):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + "..."


class Setup(Exception):
    pass


def section(rules, fn):
    """Run one group of rules; if its setup breaks, fail every rule of it not reported yet."""
    before = passed + failed
    try:
        fn()
    except Setup as e:
        for r in rules[passed + failed - before:]:
            bad(r, "setup failed (%s); make the earlier rules pass first" % e)
    except Exception as e:  # a check must never crash
        for r in rules[passed + failed - before:]:
            bad(r, "check error: %s" % short(repr(e), 200))


def finish():
    print("RESULT: %d passed, %d failed" % (passed, failed), flush=True)
    sys.exit(0 if failed == 0 else 1)


# ---------------- HTTP ----------------
class R:
    def __init__(self, status, body, headers=None, raw=b""):
        self.status, self.body, self.headers, self.raw = status, body, headers or {}, raw

    def get(self, *path, default=None):
        v = self.body
        try:
            for p in path:
                v = v[p]
            return v
        except Exception:
            return default


def call(method, url, body=None, token=None, data=None, ctype=None, timeout=5):
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        ctype = "application/json"
    if ctype:
        headers["Content-Type"] = ctype
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st, txt, hd = r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        st, txt, hd = e.code, e.read(), dict(e.headers)
    except Exception:
        return R(0, None)
    try:
        return R(st, json.loads(txt or b"null"), hd, txt)
    except Exception:
        return R(st, None, hd, txt)


def upload(url, token, filename, content, ctype="text/plain", timeout=10):
    """multipart/form-data POST with one part named `file`."""
    b = "----chk" + uuid.uuid4().hex
    data = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
             % (b, filename, ctype)).encode() + content + ("\r\n--%s--\r\n" % b).encode())
    return call("POST", url, None, token, data=data, ctype="multipart/form-data; boundary=" + b, timeout=timeout)


def healthy(nodes=NODES):
    for n in nodes:
        try:
            with urllib.request.urlopen(n + "/api/health", timeout=2) as r:
                if r.status != 200:
                    return False
        except Exception:
            return False
    return True


def boot():
    """Start /app/start.sh if the cluster is down (waits at most 15 s)."""
    if not healthy():
        subprocess.Popen("cd /app && nohup /app/start.sh >/tmp/start.log 2>&1 &", shell=True)
        for _ in range(30):
            time.sleep(0.5)
            if healthy():
                break
    if not healthy():
        if FEEDBACK == "verbose":
            state = []
            for port in PORTS:
                r = call("GET", "http://127.0.0.1:%d/api/health" % port, timeout=1)
                state.append("%d: %s" % (port, r.status if r.status else ("listening, no answer" if port_pid(port) else "nothing listening")))
            print("FAIL 0 cluster is not answering /api/health within 15 s (%s)" % "; ".join(state))
            try:
                tail = open("/tmp/start.log", errors="replace").read().splitlines()[-8:]
            except Exception:
                tail = []
            print("  last lines of /tmp/start.log: %s" % (" | ".join(tail) or "(empty)"))
            print("  hint: run one node in the foreground (python3 /app/server.py node 0 18000) and curl it; a node that"
                  " stays alive but never listens is usually awaiting a forever-running task (the event pump) in its startup hook")
        print("RESULT: 0 passed, 1 failed")
        sys.exit(1)


# ---------------- processes (only ever by the PID that owns a port) ----------------
def port_pid(port):
    try:
        out = subprocess.run(["ss", "-ltnpH", "sport = :%d" % port], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    m = re.search(r"pid=(\d+)", out)
    return int(m.group(1)) if m else None


def kill_port(port):
    pid = port_pid(port)
    if pid:
        try:
            os.kill(pid, 9)
        except Exception:
            return None
    return pid


def wait_for(pred, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ---------------- WebSocket (minimal RFC 6455 client) ----------------
class WS:
    def __init__(self, port, token, timeout=3):
        self.frames = []
        self.buf = b""
        self.status = "none"
        try:
            self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
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
        except Exception:
            self.s = None

    def send(self, obj):
        payload = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += n.to_bytes(2, "big")
        else:
            hdr.append(0x80 | 127)
            hdr += n.to_bytes(8, "big")
        mask = os.urandom(4)
        hdr += mask
        try:
            self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        except Exception:
            pass

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
        """Next JSON frame, or None on timeout/close. Answers pings."""
        if self.s is None:
            return None
        deadline = time.time() + timeout
        try:
            while True:
                self._need(2, deadline)
                op, ln = self.buf[0] & 0x0F, self.buf[1] & 0x7F
                off = 2
                if ln == 126:
                    self._need(4, deadline)
                    ln = int.from_bytes(self.buf[2:4], "big")
                    off = 4
                elif ln == 127:
                    self._need(10, deadline)
                    ln = int.from_bytes(self.buf[2:10], "big")
                    off = 10
                self._need(off + ln, deadline)
                data, self.buf = self.buf[off:off + ln], self.buf[off + ln:]
                if op == 0x8:
                    return None
                if op == 0x9:  # ping -> pong
                    try:
                        mask = os.urandom(4)
                        self.s.sendall(bytes([0x8A, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                    except Exception:
                        pass
                    continue
                if op == 0x1:
                    try:
                        f = json.loads(data)
                    except Exception:
                        continue
                    self.frames.append((time.time(), f))
                    return f
        except Exception:
            return None

    def until(self, pred, timeout=2.0):
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

    def events(self, n, timeout=3.0, kinds=None):
        """The next n frames that carry an event_id (optionally only of the given types)."""
        out = []
        end = time.time() + timeout
        while len(out) < n and time.time() < end:
            f = self.recv(end - time.time())
            if f is None:
                break
            if isinstance(f, dict) and "event_id" in f and (kinds is None or f.get("type") in kinds):
                out.append(f)
        return out

    def subscribe(self, cid):
        self.send({"type": "subscribe", "channel_id": cid})
        f, _ = self.until(lambda f: isinstance(f, dict) and f.get("type") in ("subscribed", "error"))
        return f or {}

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------------- IRC client ----------------
class IRC:
    def __init__(self, port=6667, timeout=3):
        self.lines = []
        self.buf = b""
        self.closed = False
        try:
            self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        except Exception:
            self.s = None
            self.closed = True

    def send(self, line):
        try:
            self.s.sendall((line + "\r\n").encode())
        except Exception:
            self.closed = True

    def recv(self, timeout):
        """Next line (without CRLF), or None on timeout/close."""
        if self.s is None:
            return None
        end = time.time() + timeout
        while b"\n" not in self.buf:
            left = end - time.time()
            if left <= 0:
                return None
            try:
                self.s.settimeout(left)
                chunk = self.s.recv(4096)
            except socket.timeout:
                return None
            except Exception:
                self.closed = True
                return None
            if not chunk:
                self.closed = True
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        line = line.decode("utf-8", "replace").rstrip("\r")
        self.lines.append(line)
        if line.startswith("PING "):
            self.send("PONG " + line[5:])
        return line

    def until(self, pred, timeout=3.0):
        seen = []
        end = time.time() + timeout
        while time.time() < end:
            ln = self.recv(end - time.time())
            if ln is None:
                if self.closed:
                    break
                continue
            seen.append(ln)
            if pred(ln):
                return ln, seen
        return None, seen

    def register(self, token, nick, timeout=3.0):
        """PASS/NICK/USER; returns (lines seen, True if 422 or 376 arrived)."""
        self.send("PASS " + token)
        self.send("NICK " + nick)
        self.send("USER %s 0 * :%s" % (nick, nick))
        ln, seen = self.until(lambda l: numeric(l) in ("422", "376", "464", "433"), timeout)
        return seen, ln is not None and numeric(ln) in ("422", "376")

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def numeric(line):
    p = line.split()
    if len(p) >= 2 and line.startswith(":"):
        return p[1]
    return p[0] if p else ""


# ---------------- fixtures ----------------
SEQ = [0]


def uname(tag):
    SEQ[0] += 1
    return "%s%d_%s" % (tag, SEQ[0], RUN)


def user(tag, node=N0):
    name = uname(tag)
    r = call("POST", node + "/api/auth/register", {"username": name, "password": PW})
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


def join(u, cid, node=N0):
    r = call("POST", "%s/api/channels/%s/join" % (node, cid), None, u["tok"])
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


def messages(u, cid, node=N0, limit=50):
    r = call("GET", "%s/api/channels/%s/messages?limit=%d" % (node, cid, limit), None, u["tok"])
    return r.get("messages") or []


def find(items, mid):
    for m in items or []:
        if isinstance(m, dict) and str(m.get("id")) == str(mid):
            return m
    return None


def same_id(a, b):
    return a is not None and str(a) == str(b)


# ---------------- section 1: the base every component needs ----------------
def base_rules():
    """Rules 1.1 to 1.6. Identical in every component."""
    def s1():
        st = [call("GET", n + "/api/health") for n in NODES]
        got = [(r.status, r.get("status"), r.get("node_id")) for r in st]
        check("1.1", got == [(200, "ok", 0), (200, "ok", 1), (200, "ok", 2)], "health ok with node_id 0/1/2", "got %s" % got)

        name = uname("base")
        r = call("POST", N0 + "/api/auth/register", {"username": name, "password": PW, "display_name": "Base User"})
        tok = r.get("token") or ""
        a = {"name": name, "tok": tok, "id": r.get("user", "id")}
        w1, w2 = WS(8000, "not-a-real-token-000000"), WS(8000, tok or "x")
        check("1.2", w1.status != "101" and w2.status == "101", "websocket refuses a bad token and accepts a real one",
              "bad token got %s (must not be 101), real token got %s (want 101)" % (w1.status, w2.status))
        w1.close(); w2.close()

        dup = call("POST", N1 + "/api/auth/register", {"username": name, "password": PW}).status
        lg = call("POST", N2 + "/api/auth/login", {"username": name, "password": PW})
        me = [call("GET", n + "/api/auth/me", None, tok) for n in NODES]
        anon = call("GET", N0 + "/api/auth/me").status
        good = (r.status == 201 and len(tok) >= 16 and a["id"] is not None and r.get("user", "username") == name
                and dup == 409 and lg.status == 200 and bool(lg.get("token")) and [m.status for m in me] == [200] * 3
                and all(same_id(m.get("user", "id"), a["id"]) for m in me) and anon == 401)
        check("1.3", good, "register, login and me work on every node",
              "register %s token len %d, duplicate %s (want 409), login on 8002 %s, me on 3 nodes %s, me without token %s (want 401)"
              % (r.status, len(tok), dup, lg.status, [m.status for m in me], anon))
        if not tok:
            raise Setup("register failed")

        b = user("base")
        slug = "base%d-%s" % (SEQ[0], RUN)
        w = call("POST", N0 + "/api/workspaces", {"slug": slug, "name": "Base"}, tok)
        gen = w.get("general_channel", "id")
        j = call("POST", "%s/api/channels/%s/join" % (N1, gen), None, b["tok"]).status if gen is not None else 0
        c = call("POST", "%s/api/workspaces/%s/channels" % (N2, slug), {"name": "base-room"}, tok)
        good = (w.status == 201 and w.get("workspace", "slug") == slug and same_id(w.get("workspace", "owner_id"), a["id"])
                and w.get("general_channel", "name") == "general" and j == 200 and c.status == 201
                and c.get("channel", "name") == "base-room")
        check("1.4", good, "create workspace (with #general), join #general, create channel",
              "workspace %s %s, join %s, create channel %s %s"
              % (w.status, short(w.body, 100), j, c.status, short(c.body, 100)))
        if gen is None:
            raise Setup("no workspace")

        m1 = call("POST", "%s/api/channels/%s/messages" % (N0, gen), {"body": "first base message"}, tok)
        m = m1.get("message") or {}
        m2 = call("POST", "%s/api/channels/%s/messages" % (N1, gen), {"body": "second base message"}, b["tok"])
        rep = call("POST", "%s/api/channels/%s/messages" % (N2, gen), {"body": "a base reply", "parent_id": m.get("id")}, b["tok"])
        empty = call("POST", "%s/api/channels/%s/messages" % (N0, gen), {"body": "   "}, tok).status
        lst = call("GET", "%s/api/channels/%s/messages" % (N2, gen), None, b["tok"])
        msgs = lst.get("messages") or []
        reps = call("GET", "%s/api/messages/%s/replies" % (N0, m.get("id")), None, tok)
        top = find(msgs, m.get("id")) or {}
        good = (m1.status == 201 and all(k in m for k in MSG_FIELDS) and m.get("body") == "first base message"
                and same_id(m.get("author_id"), a["id"]) and m.get("parent_id") is None and m.get("edited_at") is None
                and m.get("reactions") == [] and m.get("files") == [] and m.get("mentions") == []
                and bool(ISO.match(str(m.get("created_at")))) and m2.status == 201 and rep.status == 201
                and same_id(rep.get("message", "parent_id"), m.get("id")) and empty == 400 and lst.status == 200
                and "next_cursor" in (lst.body or {}) and len(msgs) >= 2
                and same_id(msgs[0].get("id"), m2.get("message", "id")) and top.get("reply_count") == 1
                and [x.get("body") for x in (reps.get("replies") or [])] == ["a base reply"])
        missing = [k for k in MSG_FIELDS if k not in m]
        check("1.5", good, "post, reply and list messages (MessageObj, newest first, reply_count)",
              "post %s (missing fields %s), reply %s, blank body %s (want 400), list %s with %d messages, "
              "first listed %s (want newest), reply_count %s (want 1), replies %s"
              % (m1.status, missing, rep.status, empty, lst.status, len(msgs), short(msgs[0].get("body") if msgs else None, 40),
                 top.get("reply_count"), short(reps.body, 120)))

        ws = WS(8002, b["tok"])
        try:
            sub = ws.subscribe(gen)
            h = sub.get("head_event_id")
            t0 = time.time()
            m3 = call("POST", "%s/api/channels/%s/messages" % (N0, gen), {"body": "live base message"}, tok)
            ev = ws.events(1, 2.0)
            f = ev[0] if ev else {}
            good = (sub.get("type") == "subscribed" and isinstance(h, int) and f.get("type") == "message.created"
                    and f.get("event_id") == (h or 0) + 1 and same_id(f.get("channel_id"), gen)
                    and same_id((f.get("message") or {}).get("id"), m3.get("message", "id")))
            check("1.6", good, "subscribe on 8002, message posted on 8000 arrives as message.created",
                  "subscribe answered %s; then got %s after %.1fs (want message.created with event_id head+1 and message.id)"
                  % (short(sub, 100), short(f, 160), time.time() - t0))
        finally:
            ws.close()

    section(["1.%d" % i for i in range(1, 7)], s1)


def step_from_env():
    try:
        return int(os.environ.get("STEP", "0"))
    except ValueError:
        return 0

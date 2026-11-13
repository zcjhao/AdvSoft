"""Rules of Component E, specs e-1.md to e-3.md. Run through check_e_N.sh, not directly."""
import os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chk import *  # noqa: F401,F403


# ---------------- 2. edit and delete ----------------
def s2():
    own, bob, cat = user("own"), user("bob"), user("cat")
    ws = workspace(own, "edit")
    cid = ws["general"]
    join(bob, cid)
    join(cat, cid)
    m = post(bob, cid, "original text")
    w = WS(8002, cat["tok"])
    try:
        h = w.subscribe(cid).get("head_event_id")
        if not isinstance(h, int):
            raise Setup("subscribe gave no head_event_id")
        U = "%s/api/messages/%s" % (N0, m["id"])
        r = call("PATCH", U, {"body": "new text"}, bob["tok"])
        e = r.get("message") or {}
        lm = find(messages(cat, cid, node=N1), m["id"]) or {}
        good = (r.status == 200 and e.get("body") == "new text" and bool(ISO.match(str(e.get("edited_at"))))
                and e.get("created_at") == m.get("created_at") and lm.get("body") == "new text" and lm.get("edited_at") is not None)
        check("2.1", good, "author edits: new body, edited_at set, listing updated",
              "got %s %s; listing on 8001 body %r edited_at %r" % (r.status, short(e, 160), lm.get("body"), lm.get("edited_at")))

        x = [call("PATCH", U, {"body": "owner edit"}, own["tok"]).status, call("PATCH", U, {"body": "cat edit"}, cat["tok"]).status,
             call("PATCH", U, {"body": "  "}, bob["tok"]).status, call("PATCH", "%s/api/messages/999999999" % N0, {"body": "x"}, bob["tok"]).status,
             call("PATCH", U, {"body": "anon"}).status]
        check("2.2", x == [403, 403, 400, 404, 401], "edit errors", "owner, other member, blank, unknown, no token got %s (want 403 403 400 404 401)" % x)

        parent = post(own, cid, "parent message")
        rep = post(bob, cid, "reply to delete", parent_id=parent["id"])
        d1 = call("DELETE", "%s/api/messages/%s" % (N1, rep["id"]), None, bob["tok"])
        d2 = call("DELETE", "%s/api/messages/%s" % (N2, rep["id"]), None, bob["tok"])
        reps = call("GET", "%s/api/messages/%s/replies" % (N0, parent["id"]), None, own["tok"]).get("replies")
        pc = (find(messages(own, cid), parent["id"]) or {}).get("reply_count")
        d3 = call("DELETE", U, None, bob["tok"])
        gone = find(messages(cat, cid, node=N2), m["id"]) is None
        good = (d1.status == 200 and d1.body == {"deleted": True} and d2.status == 200 and d2.body == {"deleted": True}
                and reps == [] and pc == 0 and d3.status == 200 and gone)
        check("2.3", good, "author deletes (idempotent), message and reply gone, reply_count drops",
              "delete reply %s %s, again %s %s, replies now %s, parent reply_count %s, delete message %s, still listed %s"
              % (d1.status, d1.body, d2.status, d2.body, short(reps, 60), pc, d3.status, not gone))

        m2 = post(bob, cid, "owner will delete this")
        m3 = post(bob, cid, "cat may not delete this")
        o = call("DELETE", "%s/api/messages/%s" % (N0, m2["id"]), None, own["tok"]).status
        c = call("DELETE", "%s/api/messages/%s" % (N0, m3["id"]), None, cat["tok"]).status
        u = call("DELETE", "%s/api/messages/999999999" % N0, None, own["tok"]).status
        check("2.4", (o, c, u) == (200, 403, 404), "owner may delete anyone's message, others 403, unknown 404",
              "owner %s, other member %s, unknown %s (want 200 403 404)" % (o, c, u))

        p = call("PATCH", U, {"body": "too late"}, bob["tok"]).status
        check("2.5", p == 409, "editing a deleted message returns 409", "got %s" % p)
        rr = call("POST", U + "/reactions", {"emoji": "fire"}, bob["tok"]).status
        check("2.6", rr == 404, "reacting to a deleted message returns 404", "got %s" % rr)

        ev = w.events(8, 2.5)
        got = [(f.get("type"), f.get("event_id")) for f in ev]
        mid, rid = str(m["id"]), str(rep["id"])
        want = [("message.updated", h + 1), ("message.created", h + 2), ("message.reply", h + 3), ("message.deleted", h + 4),
                ("message.deleted", h + 5), ("message.created", h + 6), ("message.created", h + 7), ("message.deleted", h + 8)]
        upd = ev[0] if ev else {}
        dels = [str(f.get("message_id")) for f in ev if f.get("type") == "message.deleted"]
        good = (got == want and (upd.get("message") or {}).get("body") == "new text" and dels[:2] == [rid, mid])
        check("2.7", good, "message.updated and message.deleted events, none for a repeated delete",
              "got %s (want %s); deleted ids %s" % (short(got, 300), short(want, 300), dels))
    finally:
        w.close()


# ---------------- IRC helpers ----------------
def irc_user(tok, nick):
    c = IRC()
    seen, ok_ = c.register(tok, nick)
    if not ok_:
        c.close()
        raise Setup("IRC registration for %s failed: %s" % (nick, short(seen, 160)))
    return c


def joined(c, chan):
    c.send("JOIN " + chan)
    ln, seen = c.until(lambda l: numeric(l) in ("366", "403"), 3.0)
    return ln is not None and numeric(ln) == "366", seen


# ---------------- 3. IRC protocol ----------------
def s3():
    a, b = user("ia"), user("ib")
    ws = workspace(a, "irc")
    join(b, ws["general"])
    nick = "n" + a["name"][-12:]
    c = IRC()
    try:
        if c.s is None:
            raise Setup("nothing listens on 127.0.0.1:6667")
        seen, done = c.register(a["tok"], nick)
        nums = [numeric(l) for l in seen if re.match(r"^\d{3}$", numeric(l))]
        first = next((l for l in seen if numeric(l) == "001"), "")
        check("3.1", done and nums[:6] == ["001", "002", "003", "004", "005", "422"] and nick in first,
              "registration sends 001-005 then 422", "numerics %s, 001 line %r" % (nums[:8], short(first, 80)))

        x = IRC()
        x.send("PASS not-a-token")
        x.send("NICK x" + RUN)
        x.send("USER x 0 * :x")
        ln, sx = x.until(lambda l: numeric(l) in ("464", "001"), 3.0)
        x.close()
        check("3.2", ln is not None and numeric(ln) == "464", "bad password gets 464", "got %s" % short(sx, 160))

        y = IRC()
        y.send("PASS " + b["tok"])
        y.send("NICK " + nick)
        y.send("USER y 0 * :y")
        ln, sy = y.until(lambda l: numeric(l) in ("433", "001"), 3.0)
        got433 = ln is not None and numeric(ln) == "433"
        y.send("NICK m" + b["name"][-12:])
        ln2, sy2 = y.until(lambda l: numeric(l) in ("422", "376"), 3.0)
        y.close()
        check("3.3", got433 and ln2 is not None and not y.closed, "nick in use gets 433, then another NICK registers",
              "first NICK got %s; second NICK got %s" % (short(sy, 120), short(sy2, 120)))

        c.send("PING abc123")
        ln, sp = c.until(lambda l: "PONG" in l, 3.0)
        check("3.4", ln is not None and "abc123" in ln, "PING gets PONG with the token", "got %s" % short(sp, 120))

        chan = "#%s/general" % ws["slug"]
        c.send("JOIN " + chan)
        ln, sj = c.until(lambda l: numeric(l) in ("366", "403"), 3.0)
        ij = next((i for i, l in enumerate(sj) if " JOIN " in l and chan in l and l.startswith(":" + nick + "!")), None)
        i353 = next((i for i, l in enumerate(sj) if numeric(l) == "353" and nick in l.split(":", 2)[-1].split()), None)
        i366 = next((i for i, l in enumerate(sj) if numeric(l) == "366"), None)
        check("3.5", None not in (ij, i353, i366) and ij < i353 < i366, "JOIN echoes JOIN, then 353 with the nick, then 366",
              "got %s" % short(sj, 300))

        r1 = joined(c, "#%s-general" % ws["slug"])[1]
        r2 = joined(c, "#%s/no-such-channel" % ws["slug"])[1]
        check("3.6", any(numeric(l) == "403" for l in r1) and any(numeric(l) == "403" for l in r2), "unknown channels get 403",
              "hyphen form got %s; unknown got %s" % (short(r1, 100), short(r2, 100)))

        c.send("FOOBAR")
        ln, sf = c.until(lambda l: numeric(l) == "421", 3.0)
        c.send("PING still")
        ln2, _ = c.until(lambda l: "PONG" in l and "still" in l, 3.0)
        check("3.7", ln is not None and ln2 is not None, "unknown command gets 421 and the connection stays open",
              "after FOOBAR got %s; PING answered %s" % (short(sf, 100), ln2 is not None))

        for cmd in ("NAMES " + chan, "WHO " + chan, "LIST", "TOPIC " + chan, "MODE " + chan, "PONG huddle"):
            c.send(cmd)
        c.send("PING done42")
        ln, sa = c.until(lambda l: "PONG" in l and "done42" in l, 3.0)
        bad421 = [l for l in sa if numeric(l) == "421"]
        check("3.8", ln is not None and not bad421, "NAMES, WHO, LIST, TOPIC, MODE, PONG are accepted",
              "421 replies %s, connection answered PING %s" % (short(bad421, 160), ln is not None))

        c.send("QUIT :bye")
        t0 = time.time()
        _, sq = c.until(lambda l: False, 2.0)
        errs = [l for l in sq if re.match(r"^4\d\d$", numeric(l))]
        check("3.9", c.closed and not errs and time.time() - t0 < 2.1, "QUIT closes the connection without an error numeric",
              "closed %s, lines after QUIT %s" % (c.closed, short(sq, 120)))
    finally:
        c.close()


# ---------------- 4. bridging ----------------
def s4():
    a, b = user("ba"), user("bb")
    ws = workspace(a, "bridge")
    cid = ws["general"]
    join(b, cid)
    chan = "#%s/general" % ws["slug"]
    ca = irc_user(a["tok"], "na" + a["name"][-12:])
    cb = irc_user(b["tok"], "nb" + b["name"][-12:])
    w = WS(8001, b["tok"])
    try:
        for c in (ca, cb):
            okj, sj = joined(c, chan)
            if not okj:
                raise Setup("IRC JOIN %s failed: %s" % (chan, short(sj, 120)))
        h = w.subscribe(cid).get("head_event_id")
        if not isinstance(h, int):
            raise Setup("subscribe gave no head_event_id")

        ca.send("PRIVMSG %s :hello from irc" % chan)
        got = None
        end = time.time() + 2.0
        while time.time() < end and got is None:
            got = next((m for m in messages(b, cid, node=N2) if m.get("body") == "hello from irc"), None)
            if got is None:
                time.sleep(0.2)
        check("4.1", got is not None and same_id(got.get("author_id"), a["id"]), "IRC PRIVMSG shows up in the web listing",
              "listing on 8002 has %s" % short(got, 120))
        ev = w.events(1, 2.0, kinds=["message.created"])
        f = ev[0] if ev else {}
        check("4.2", f.get("event_id") == h + 1 and (f.get("message") or {}).get("body") == "hello from irc",
              "IRC message reaches WebSocket subscribers with the next event_id", "got %s (want event_id %d)" % (short(f, 150), h + 1))
        ln4, s4b = cb.until(lambda l: "PRIVMSG %s :hello from irc" % chan in l, 2.0)

        r = call("POST", "%s/api/channels/%s/messages" % (N1, cid), {"body": "hello from web"}, b["tok"])
        ln, sw = ca.until(lambda l: l.startswith(":%s!" % b["name"]) and l.endswith("PRIVMSG %s :hello from web" % chan), 2.0)
        check("4.3", r.status == 201 and ln is not None, "web message reaches the IRC client as PRIVMSG",
              "post %s; IRC got %s" % (r.status, short(sw, 200)))
        check("4.4", ln4 is not None and ln4.startswith(":"), "a second IRC client receives the IRC PRIVMSG", "got %s" % short(s4b, 160))

        w.events(1, 0.5)
        bodies = []
        for i in range(3):
            ca.send("PRIVMSG %s :mix irc %d" % (chan, i))
            bodies.append("mix irc %d" % i)
            time.sleep(0.25)
            call("POST", "%s/api/channels/%s/messages" % (NODES[i], cid), {"body": "mix web %d" % i}, b["tok"])
            bodies.append("mix web %d" % i)
            time.sleep(0.1)
        ev = w.events(6, 3.0, kinds=["message.created"])
        ids_ = [f.get("event_id") for f in ev]
        bs = [(f.get("message") or {}).get("body") for f in ev]
        check("4.5", ids_ == list(range(h + 3, h + 9)) and bs == bodies, "mixed IRC and web writers stay dense and in order",
              "event ids %s (want %s), bodies %s" % (ids_, list(range(h + 3, h + 9)), short(bs, 200)))

        pid = kill_port(8001)
        t_kill = time.time()
        time.sleep(0.3)
        r = call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "web while 8001 is down"}, b["tok"])
        ln, sk = ca.until(lambda l: l.endswith("PRIVMSG %s :web while 8001 is down" % chan), 2.0)
        ca.send("PRIVMSG %s :irc while 8001 is down" % chan)
        got = None
        end = time.time() + 2.0
        while time.time() < end and got is None:
            got = next((m for m in messages(b, cid, node=N2) if m.get("body") == "irc while 8001 is down"), None)
            if got is None:
                time.sleep(0.2)
        check("4.6", pid is not None and r.status == 201 and ln is not None and got is not None,
              "bridging keeps working while 8001 is killed",
              "killed pid %s; post on 8000 %s; IRC got it %s; IRC message in 8002 listing %s" % (pid, r.status, ln is not None, got is not None))
        wait_for(lambda: call("GET", N1 + "/api/health", timeout=1).status == 200, 15 - (time.time() - t_kill), 0.3)
    finally:
        w.close(); ca.close(); cb.close()


STEPS = {
    1: [(["1.%d" % i for i in range(1, 7)], None), (["2.%d" % i for i in range(1, 8)], s2)],
    2: [(["3.%d" % i for i in range(1, 10)], s3)],
    3: [(["4.1", "4.2", "4.3", "4.4", "4.5", "4.6"], s4)],
}


def main():
    boot()
    step = step_from_env()
    for st in sorted(STEPS) if step == 0 else [step]:
        for rules, fn in STEPS[st]:
            if fn is None:
                base_rules()
            else:
                section(rules, fn)
    finish()


if __name__ == "__main__":
    main()

"""Rules of Component C (c-1.md to c-4.md). Run through check_c_N.sh, not directly."""
import os, sys, threading, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chk import *  # noqa: F401,F403
import chk

SHRUG = "¯\\_(ツ)_/¯"


def ids_set(lst):
    return sorted(str(x) for x in (lst or []))


# ---------------- 2. reactions ----------------
def s2():
    own, bob, out = user("own"), user("bob"), user("out")
    ws = workspace(own, "react")
    cid = ws["general"]
    join(bob, cid)
    m = post(own, cid, "react to me")
    U = "%s/api/messages/%s/reactions" % (N0, m["id"])
    w = WS(8001, bob["tok"])
    try:
        sub = w.subscribe(cid)
        h = sub.get("head_event_id")
        if not isinstance(h, int):
            raise Setup("subscribe gave no head_event_id")

        r = call("POST", U, {"emoji": "thumbsup"}, own["tok"])
        want = [{"emoji": "thumbsup", "count": 1, "user_ids": [own["id"]]}]
        check("2.1", r.status == 200 and r.get("reactions") == want, "first reaction returns 200 with count 1",
              "got %s %s (want 200 %s)" % (r.status, short(r.body), want))

        r = call("POST", "%s/api/messages/%s/reactions" % (N1, m["id"]), {"emoji": "thumbsup"}, own["tok"])
        check("2.2", r.status == 200 and r.get("reactions") == want, "repeating the same reaction keeps count 1",
              "got %s %s" % (r.status, short(r.body)))

        a = call("POST", "%s/api/messages/%s/reactions" % (N2, m["id"]), {"emoji": "thumbsup"}, bob["tok"])
        b = call("POST", U, {"emoji": "fire"}, bob["tok"])
        want3 = [{"emoji": "thumbsup", "count": 2, "user_ids": [own["id"], bob["id"]]},
                 {"emoji": "fire", "count": 1, "user_ids": [bob["id"]]}]
        got = b.get("reactions")
        norm = lambda lst: [(x.get("emoji"), x.get("count"), ids_set(x.get("user_ids"))) for x in (lst or [])]
        check("2.3", a.status == 200 and b.status == 200 and norm(got) == norm(want3),
              "second user and second emoji counted in order", "got %s %s / %s %s (want %s)" % (a.status, short(a.body), b.status, short(got), norm(want3)))

        lm = find(messages(bob, cid, node=N2), m["id"]) or {}
        check("2.4", norm(lm.get("reactions")) == norm(want3), "listing shows the same reactions",
              "message in listing has reactions %s" % short(lm.get("reactions")))

        d1 = call("DELETE", "%s/api/messages/%s/reactions/fire" % (N1, m["id"]), None, bob["tok"])
        d2 = call("DELETE", "%s/api/messages/%s/reactions/fire" % (N0, m["id"]), None, own["tok"])
        want5 = [("thumbsup", 2, ids_set([own["id"], bob["id"]]))]
        check("2.5", d1.status == 200 and norm(d1.get("reactions")) == want5 and d2.status == 200 and norm(d2.get("reactions")) == want5,
              "remove deletes only the caller's reaction; empty entries vanish",
              "bob removes fire %s %s; own removes a reaction it never added %s %s (want 200 and %s)"
              % (d1.status, short(d1.body), d2.status, short(d2.body), want5))

        u1 = call("POST", U, {"emoji": "\U0001F44D"}, bob["tok"])
        has = any(x.get("emoji") == "\U0001F44D" and x.get("count") == 1 for x in (u1.get("reactions") or []))
        u2 = call("DELETE", "%s/api/messages/%s/reactions/%s" % (N2, m["id"], urllib.parse.quote("\U0001F44D")), None, bob["tok"])
        gone = not any(x.get("emoji") == "\U0001F44D" for x in (u2.get("reactions") or []))
        check("2.6", u1.status == 200 and has and u2.status == 200 and gone, "unicode emoji can be added and removed (URL-encoded)",
              "add %s %s, remove %s %s" % (u1.status, short(u1.body, 100), u2.status, short(u2.body, 100)))

        priv = channel(own, ws, "secret-react", is_private=True)
        pm = post(own, priv, "private")
        e = [call("POST", U, {"emoji": "x"}).status,
             call("POST", "%s/api/messages/999999999/reactions" % N0, {"emoji": "x"}, own["tok"]).status,
             call("DELETE", "%s/api/messages/999999999/reactions/x" % N0, None, own["tok"]).status,
             call("POST", U, {}, own["tok"]).status,
             call("POST", U, {"emoji": ""}, own["tok"]).status,
             call("POST", U, {"emoji": "e" * 65}, own["tok"]).status,
             call("POST", "%s/api/messages/%s/reactions" % (N0, pm["id"]), {"emoji": "x"}, out["tok"]).status]
        check("2.7", e == [401, 404, 404, 400, 400, 400, 403], "reaction errors",
              "no token, unknown POST, unknown DELETE, missing emoji, empty emoji, 65 chars, outsider on private got %s (want 401 404 404 400 400 400 403)" % e)

        ev = w.events(8, 2.0)
        got = [(f.get("type"), f.get("event_id"), f.get("emoji"), str(f.get("message_id"))) for f in ev]
        mid = str(m["id"])
        want = [("reaction.added", h + 1, "thumbsup", mid), ("reaction.added", h + 2, "thumbsup", mid),
                ("reaction.added", h + 3, "fire", mid), ("reaction.removed", h + 4, "fire", mid),
                ("reaction.added", h + 5, "\U0001F44D", mid), ("reaction.removed", h + 6, "\U0001F44D", mid)]
        uid_ok = all(f.get("user_id") is not None for f in ev)
        check("2.8", got == want and uid_ok, "reaction.added/removed events, none for no-op calls",
              "got %s (want %s, each with user_id)" % (short(got, 400), short(want, 300)))
    finally:
        w.close()


# ---------------- 3. DMs ----------------
def s3():
    a, b, c = user("ann"), user("bob"), user("cat")
    D = N0 + "/api/dms"
    r1 = call("POST", D, {"user_id": b["id"]}, a["tok"])
    cid = r1.get("channel_id")
    check("3.1", r1.status == 200 and cid is not None, "POST /api/dms returns 200 with channel_id",
          "got %s %s" % (r1.status, short(r1.body)))
    if cid is None:
        raise Setup("no DM channel")
    r2 = call("POST", N1 + "/api/dms", {"user_id": b["id"]}, a["tok"])
    r3 = call("POST", N2 + "/api/dms", {"user_id": a["id"]}, b["tok"])
    r4 = call("POST", D, {"user_id": c["id"]}, a["tok"])
    good = (r2.status == 200 and r3.status == 200 and same_id(r2.get("channel_id"), cid) and same_id(r3.get("channel_id"), cid)
            and r4.status == 200 and r4.get("channel_id") is not None and not same_id(r4.get("channel_id"), cid))
    check("3.2", good, "same DM channel both ways and on repeat, another pair differs",
          "first %s, repeat %s, reverse %s, other pair %s" % (cid, r2.get("channel_id"), r3.get("channel_id"), r4.get("channel_id")))

    w = WS(8002, b["tok"])
    try:
        sub = w.subscribe(cid)
        p1 = call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "hi bob"}, a["tok"])
        p2 = call("POST", "%s/api/channels/%s/messages" % (N1, cid), {"body": "hi ann"}, b["tok"])
        l1 = call("GET", "%s/api/channels/%s/messages" % (N2, cid), None, a["tok"])
        ev = w.events(1, 2.0, kinds=["message.created"])
        x1 = call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "intruder"}, c["tok"]).status
        x2 = call("GET", "%s/api/channels/%s/messages" % (N0, cid), None, c["tok"]).status
        bodies = [m.get("body") for m in (l1.get("messages") or [])]
        good = (sub.get("type") == "subscribed" and p1.status == 201 and p2.status == 201 and l1.status == 200
                and bodies[:2] == ["hi ann", "hi bob"] and len(ev) == 1 and (x1, x2) == (403, 403))
        check("3.3", good, "both participants use the DM, a third user gets 403",
              "subscribe %s, posts %s %s, list %s %s, event %s, third user post %s list %s (want 403 403)"
              % (sub.get("type"), p1.status, p2.status, l1.status, bodies[:2], short(ev, 80), x1, x2))
    finally:
        w.close()
    e = [call("POST", D, {"user_id": b["id"]}).status, call("POST", D, {"user_id": 999999999}, a["tok"]).status,
         call("POST", D, {}, a["tok"]).status]
    check("3.4", e == [401, 404, 400], "DM errors", "no token, unknown user, missing user_id got %s (want 401 404 400)" % e)


# ---------------- 4. profiles ----------------
def s4():
    name = uname("prof")
    r = call("POST", N0 + "/api/auth/register", {"username": name, "password": PW, "display_name": "Pro File"})
    if r.status != 201:
        raise Setup("register got %s" % r.status)
    a = {"name": name, "tok": r.get("token"), "id": r.get("user", "id")}
    b = user("obs")
    g = call("GET", "%s/api/users/%s" % (N1, a["id"]), None, b["tok"])
    u = g.get("user") or {}
    want = {"id": a["id"], "username": name, "display_name": "Pro File", "timezone": "UTC", "avatar_url": "",
            "status_text": "", "status_emoji": ""}
    got = {k: u.get(k) for k in want}
    check("4.1", g.status == 200 and got == want, "GET /api/users/{id} returns the full UserObj with defaults",
          "got %s %s (want %s)" % (g.status, short(got, 250), want))
    e = [call("GET", "%s/api/users/999999999" % N0, None, b["tok"]).status, call("GET", "%s/api/users/%s" % (N0, a["id"])).status]
    check("4.2", e == [404, 401], "unknown user 404, no token 401", "got %s" % e)

    w = WS(8002, b["tok"])
    try:
        time.sleep(0.2)
        p = call("PATCH", N0 + "/api/users/me", {"status_text": "In a meeting", "status_emoji": ":calendar:"}, a["tok"])
        pu = p.get("user") or {}
        g1 = call("GET", "%s/api/users/%s" % (N2, a["id"]), None, b["tok"]).get("user") or {}
        me = call("GET", N1 + "/api/auth/me", None, a["tok"]).get("user") or {}
        good = (p.status == 200 and pu.get("status_text") == "In a meeting" and pu.get("status_emoji") == ":calendar:"
                and pu.get("display_name") == "Pro File" and g1.get("status_text") == "In a meeting"
                and me.get("status_emoji") == ":calendar:")
        check("4.3", good, "PATCH /api/users/me updates only the fields sent, visible on every node",
              "patch %s %s; other node sees %s / %s" % (p.status, short(pu, 160), g1.get("status_text"), me.get("status_emoji")))

        f, _ = w.until(lambda f: isinstance(f, dict) and f.get("type") == "user.updated", 2.0)
        f = f or {}
        good = (same_id((f.get("user") or {}).get("id"), a["id"]) and (f.get("user") or {}).get("status_text") == "In a meeting"
                and "channel_id" not in f and "event_id" not in f)

        t1 = call("PATCH", N1 + "/api/users/me", {"timezone": "America/New_York"}, a["tok"])
        t2 = call("PATCH", N0 + "/api/users/me", {"timezone": "Mars/Phobos"}, a["tok"]).status
        t3 = call("PATCH", N0 + "/api/users/me", {"timezone": ""}, a["tok"]).status
        tz = (call("GET", "%s/api/users/%s" % (N0, a["id"]), None, a["tok"]).get("user") or {}).get("timezone")
        check("4.4", t1.status == 200 and t1.get("user", "timezone") == "America/New_York" and (t2, t3) == (400, 400) and tz == "America/New_York",
              "valid IANA timezone accepted, invalid rejected", "valid %s, Mars/Phobos %s, empty %s, stored %s (want 200 400 400 America/New_York)"
              % (t1.status, t2, t3, tz))

        bad_bodies = [{"display_name": ""}, {"display_name": "d" * 81}, {"status_text": "s" * 141},
                      {"status_emoji": "e" * 65}, {"avatar_url": "a" * 1025}, {"status_text": 5},
                      {"display_name": "Valid Name", "status_text": "t" * 141}]
        st = [call("PATCH", N0 + "/api/users/me", bb, a["tok"]).status for bb in bad_bodies]
        okb = call("PATCH", N0 + "/api/users/me", {"display_name": "d" * 80, "status_text": "s" * 140}, a["tok"]).status
        dn = (call("GET", "%s/api/users/%s" % (N0, a["id"]), None, a["tok"]).get("user") or {}).get("display_name")
        check("4.5", st == [400] * 7 and okb == 200 and dn == "d" * 80, "out-of-range fields rejected without partial update",
              "bad bodies got %s (want 400 each), 80/140 chars got %s, display_name length now %s"
              % (st, okb, len(dn or "")))
        check("4.6", good, "user.updated broadcast without channel_id or event_id",
              "socket on 8002 got %s" % short(f, 200))
    finally:
        w.close()


# ---------------- 5. files ----------------
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def s5():
    a, b = user("fil"), user("oth")
    ws = workspace(a, "files")
    cid = ws["general"]
    join(b, cid)
    F = N0 + "/api/files"
    r = upload(F, a["tok"], "hello.txt", b"hello, files!", "text/plain")
    f = r.get("file") or {}
    big = upload(N1 + "/api/files", a["tok"], "two.bin", b"\0" * 2097152, "application/octet-stream")
    good = (r.status == 201 and f.get("filename") == "hello.txt" and f.get("content_type") == "text/plain"
            and f.get("size") == 13 and same_id(f.get("uploader_id"), a["id"]) and f.get("id") is not None
            and bool(ISO.match(str(f.get("created_at")))) and big.status == 201 and big.get("file", "size") == 2097152)
    check("5.1", good, "upload returns 201 with a FileObj (13 bytes and 2 MiB)",
          "small %s %s, 2 MiB %s %s" % (r.status, short(f, 200), big.status, short(big.body, 80)))
    if f.get("id") is None:
        raise Setup("upload failed")
    fid = f["id"]
    g = call("GET", "%s/api/files/%s" % (N2, fid), None, b["tok"])
    e = [call("GET", "%s/api/files/999999999" % N0, None, a["tok"]).status, call("GET", "%s/api/files/%s" % (N0, fid)).status,
         upload(F, None, "x.txt", b"x").status, call("GET", "%s/api/files/%s/download" % (N0, fid)).status]
    check("5.2", g.status == 200 and g.get("file") == f and e == [404, 401, 401, 401], "file metadata on any node, errors",
          "metadata %s %s, unknown/no-token get/no-token upload/no-token download %s (want 404 401 401 401)" % (g.status, short(g.body, 120), e))

    d = call("GET", "%s/api/files/%s/download" % (N1, fid), None, b["tok"])
    hd = {k.lower(): v for k, v in d.headers.items()}
    p = upload(F, a["tok"], "dot.png", PNG, "image/png")
    pd = call("GET", "%s/api/files/%s/download" % (N2, p.get("file", "id")), None, a["tok"])
    phd = {k.lower(): v for k, v in pd.headers.items()}
    good = (d.status == 200 and d.raw == b"hello, files!" and hd.get("content-type", "").split(";")[0].strip() == "text/plain"
            and "hello.txt" in hd.get("content-disposition", "") and pd.status == 200 and pd.raw == PNG
            and phd.get("content-type", "").split(";")[0].strip() == "image/png" and "dot.png" in phd.get("content-disposition", ""))
    check("5.3", good, "download returns the bytes with Content-Type and Content-Disposition",
          "text: %s %d bytes, type %s, disposition %s; png: %s %d bytes (want %d), type %s"
          % (d.status, len(d.raw or b""), hd.get("content-type"), hd.get("content-disposition"), pd.status,
             len(pd.raw or b""), len(PNG), phd.get("content-type")))

    t = upload(F, a["tok"], "big.bin", b"\0" * 10485761, "application/octet-stream", timeout=20)
    check("5.4", t.status == 413, "upload over 10 MiB returns 413", "10485761 bytes got %s" % t.status)

    bnd = "----chknofile"
    nb = ("--%s\r\nContent-Disposition: form-data; name=\"other\"\r\n\r\nvalue\r\n--%s--\r\n" % (bnd, bnd)).encode()
    n = call("POST", F, None, a["tok"], data=nb, ctype="multipart/form-data; boundary=" + bnd)
    check("5.5", n.status == 400, "multipart body without a file part returns 400", "got %s" % n.status)

    m = call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "see file", "file_ids": [fid]}, a["tok"])
    mf = m.get("message", "files") or []
    lf = (find(messages(b, cid, node=N1), m.get("message", "id")) or {}).get("files") or []
    good = (m.status == 201 and len(mf) == 1 and same_id(mf[0].get("id"), fid) and mf[0].get("filename") == "hello.txt"
            and len(lf) == 1 and same_id(lf[0].get("id"), fid))
    check("5.6", good, "file_ids attaches the file to the message", "post %s files %s, listing files %s" % (m.status, short(mf, 150), short(lf, 100)))

    bf = upload(F, b["tok"], "bobs.txt", b"bob", "text/plain").get("file", "id")
    before = len(messages(a, cid))
    x = [call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "steal", "file_ids": [bf]}, a["tok"]).status,
         call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "again", "file_ids": [fid]}, a["tok"]).status,
         call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "ghost", "file_ids": [999999999]}, a["tok"]).status]
    after = len(messages(a, cid))
    check("5.7", x == [400, 400, 400] and after == before, "bad file_ids rejected with 400 and no message",
          "someone else's file, already attached, unknown got %s (want 400 400 400); messages %d -> %d" % (x, before, after))


# ---------------- 6. slash commands ----------------
def s6():
    own, mem, carol = user("own"), user("mem"), user("car")
    ws = workspace(own, "slash")
    cid = channel(own, ws, "cmds")
    join(mem, cid)
    P = "%s/api/channels/%s/messages" % (N0, cid)

    r = call("POST", P, {"body": "/me waves"}, mem["tok"])
    check("6.1", r.status == 201 and r.get("message", "body") == "/me waves", "/me keeps the body literally",
          "got %s body %r" % (r.status, r.get("message", "body")))
    a = call("POST", P, {"body": "/shrug hello"}, mem["tok"])
    b = call("POST", P, {"body": "/shrug"}, mem["tok"])
    check("6.2", (a.status, a.get("message", "body"), b.status, b.get("message", "body")) == (201, "hello " + SHRUG, 201, SHRUG),
          "/shrug appends the shrug", "got %s %r and %s %r (want %r and %r)" % (a.status, a.get("message", "body"), b.status,
                                                                              b.get("message", "body"), "hello " + SHRUG, SHRUG))
    r = call("POST", P, {"body": "//literal"}, mem["tok"])
    check("6.3", r.status == 201 and r.get("message", "body") == "//literal", "//literal is posted as typed",
          "got %s body %r" % (r.status, r.get("message", "body")))
    before = len(messages(own, cid))
    e = [call("POST", P, {"body": "/frobnicate now"}, mem["tok"]).status, call("POST", P, {"body": "/me"}, mem["tok"]).status,
         call("POST", P, {"body": "/me   "}, mem["tok"]).status]
    after = len(messages(own, cid))
    check("6.4", e == [400, 400, 400] and before == after, "unknown command and empty /me return 400",
          "got %s (want 400 400 400), messages %d -> %d" % (e, before, after))

    t = call("POST", P, {"body": "/topic Launch day"}, mem["tok"])
    det = call("GET", "%s/api/workspaces/%s" % (N1, ws["slug"]), None, own["tok"])
    topic = next((c.get("topic") for c in (det.get("channels") or []) if same_id(c.get("id"), cid)), None)
    good = (t.status == 201 and t.body is not None and "message" in t.body and t.get("message") is None
            and t.get("channel", "topic") == "Launch day" and topic == "Launch day")
    check("6.5", good, "/topic returns message null and the updated channel", "got %s %s; workspace detail topic %r"
          % (t.status, short(t.body, 160), topic))

    i = call("POST", P, {"body": "/invite @%s" % carol["name"]}, mem["tok"])
    mems = call("GET", "%s/api/channels/%s/members" % (N2, cid), None, own["tok"]).get("members") or []
    cp = call("POST", "%s/api/channels/%s/messages" % (N1, cid), {"body": "carol here"}, carol["tok"]).status
    un = call("POST", P, {"body": "/invite @nobody_%s" % RUN}, mem["tok"]).status
    good = (i.status == 201 and "message" in (i.body or {}) and i.get("message") is None
            and same_id(i.get("invited_user", "id"), carol["id"]) and any(same_id(m.get("id"), carol["id"]) for m in mems)
            and cp == 201 and un == 404)
    check("6.6", good, "/invite adds the user to the channel", "got %s %s; carol listed %s, carol posts %s, unknown user %s (want 404)"
          % (i.status, short(i.body, 120), any(same_id(m.get("id"), carol["id"]) for m in mems), cp, un))

    x = call("POST", P, {"body": "/archive"}, mem["tok"]).status
    a = call("POST", P, {"body": "/archive"}, own["tok"])
    p1 = call("POST", P, {"body": "while archived"}, own["tok"]).status
    y = call("POST", P, {"body": "/unarchive"}, mem["tok"]).status
    u = call("POST", P, {"body": "/unarchive"}, own["tok"])
    p2 = call("POST", P, {"body": "back again"}, mem["tok"]).status
    good = (x == 403 and a.status == 201 and a.get("message", default=1) is None and a.get("channel", "is_archived") is True
            and p1 == 423 and y == 403 and u.status == 201 and u.get("channel", "is_archived") is False and p2 == 201)
    check("6.7", good, "/archive and /unarchive (owner only), 423 while archived",
          "member /archive %s (want 403), owner /archive %s %s, post %s (want 423), member /unarchive %s (want 403), "
          "owner /unarchive %s %s, post %s" % (x, a.status, short(a.body, 80), p1, y, u.status, short(u.body, 80), p2))


# ---------------- 7. groups ----------------
def s7():
    own, alice, bob, carol, out = user("own"), user("ali"), user("bob"), user("car"), user("out")
    ws = workspace(own, "groups")
    for u in (alice, bob, carol):
        join(u, ws["general"])
    G = "%s/api/workspaces/%s/groups" % (N0, ws["slug"])
    r = call("POST", G, {"handle": "designers", "name": "Design team", "user_ids": [bob["id"], carol["id"]]}, alice["tok"])
    g = r.get("group") or {}
    good = (r.status == 201 and g.get("handle") == "designers" and g.get("name") == "Design team" and g.get("id") is not None
            and ids_set(g.get("member_user_ids")) == ids_set([bob["id"], carol["id"]]) and "members" not in g)
    check("7.1", good, "a plain member creates a group (member_user_ids)", "got %s %s" % (r.status, short(r.body, 200)))

    l = call("GET", "%s/api/workspaces/%s/groups" % (N2, ws["slug"]), None, bob["tok"])
    listed = [x.get("handle") for x in (l.get("groups") or [])]
    e = [call("GET", G, None, out["tok"]).status, call("GET", G).status]
    check("7.2", l.status == 200 and "designers" in listed and e == [403, 401], "list groups; outsider 403; no token 401",
          "list %s %s; outsider, no token %s" % (l.status, listed, e))

    v = [call("POST", G, {"handle": "designers", "name": "Again"}, bob["tok"]).status,
         call("POST", G, {"handle": "bad handle", "name": "x"}, bob["tok"]).status,
         call("POST", G, {"handle": "", "name": "x"}, bob["tok"]).status,
         call("POST", G, {"handle": "noname"}, bob["tok"]).status,
         call("POST", G, {"handle": "strangers", "name": "x", "user_ids": [out["id"]]}, bob["tok"]).status]
    check("7.3", v == [409, 400, 400, 400, 400], "group validation", "duplicate, bad handle, empty handle, no name, outsider member got %s (want 409 400 400 400 400)" % v)

    H = G + "/designers"
    p1 = call("PATCH", H, {"name": "Designers", "user_ids": [bob["id"]]}, alice["tok"])
    p2 = call("PATCH", "%s/api/workspaces/%s/groups/designers" % (N1, ws["slug"]), {"user_ids": [bob["id"], carol["id"], alice["id"]]}, own["tok"])
    p3 = call("PATCH", H, {"name": "Hijack"}, carol["tok"]).status
    p4 = call("PATCH", G + "/nosuch", {"name": "x"}, alice["tok"]).status
    good = (p1.status == 200 and p1.get("group", "name") == "Designers" and ids_set(p1.get("group", "member_user_ids")) == ids_set([bob["id"]])
            and p2.status == 200 and len(p2.get("group", "member_user_ids") or []) == 3 and p3 == 403 and p4 == 404)
    check("7.4", good, "creator and owner may update, others 403, unknown 404",
          "creator %s %s, owner %s, other member %s, unknown %s" % (p1.status, short(p1.body, 120), p2.status, p3, p4))

    d1 = call("DELETE", H, None, carol["tok"]).status
    d2 = call("DELETE", H, None, alice["tok"])
    d3 = call("DELETE", H, None, alice["tok"])
    left = [x.get("handle") for x in (call("GET", G, None, alice["tok"]).get("groups") or [])]
    good = (d1 == 403 and d2.status == 200 and d2.body == {"deleted": True} and d3.status == 200 and d3.body == {"deleted": True}
            and "designers" not in left)
    check("7.5", good, "delete is creator/owner only and idempotent", "other member %s (want 403), creator %s %s, again %s %s, still listed %s"
          % (d1, d2.status, d2.body, d3.status, d3.body, "designers" in left))


# ---------------- 8. mentions ----------------
def s8():
    alice, bob, carol, dave = user("ali"), user("bob"), user("car"), user("dav")
    ws = workspace(alice, "ment")
    for u in (bob, carol, dave):
        join(u, ws["general"])
    cid = channel(alice, ws, "standup")
    join(bob, cid)
    join(carol, cid)
    handle = "designers"
    r = call("POST", "%s/api/workspaces/%s/groups" % (N0, ws["slug"]),
             {"handle": handle, "name": "Design", "user_ids": [alice["id"], bob["id"], carol["id"], dave["id"]]}, alice["tok"])
    if r.status != 201:
        raise Setup("create group got %s" % r.status)
    P = "%s/api/channels/%s/messages" % (N0, cid)

    m = call("POST", P, {"body": "hi @%s" % handle}, alice["tok"])
    check("8.1", m.status == 201 and ids_set(m.get("message", "mentions")) == ids_set([bob["id"], carol["id"]]),
          "@group-handle expands to group members in the channel, not the author",
          "got %s mentions %s (want bob, carol: %s)" % (m.status, m.get("message", "mentions"), [bob["id"], carol["id"]]))

    w = WS(8002, bob["tok"])
    try:
        time.sleep(0.3)
        m = call("POST", P, {"body": "@here standup"}, alice["tok"])
        check("8.2", m.status == 201 and ids_set(m.get("message", "mentions")) == ids_set([bob["id"]]),
              "@here mentions channel members with an open socket", "got %s mentions %s (want only bob %s)"
              % (m.status, m.get("message", "mentions"), bob["id"]))
        m = call("POST", P, {"body": "@%s @%s @here" % (bob["name"], handle)}, alice["tok"])
        ml = m.get("message", "mentions") or []
        check("8.4", m.status == 201 and len(ml) == len(set(str(x) for x in ml)) and ids_set(ml) == ids_set([bob["id"], carol["id"]]),
              "ids from all forms are deduplicated", "got %s mentions %s" % (m.status, ml))
        wa = WS(8001, alice["tok"])
        try:
            time.sleep(0.3)
            rs = call("GET", "%s/api/channels/%s/read" % (N0, cid), None, alice["tok"])
            before = rs.get("read_state", "mention_count")
            m = call("POST", P, {"body": "@%s @here @%s" % (handle, alice["name"])}, alice["tok"])
            ml = m.get("message", "mentions") or []
            after = call("GET", "%s/api/channels/%s/read" % (N0, cid), None, alice["tok"]).get("read_state", "mention_count")
            good = m.status == 201 and not any(same_id(x, alice["id"]) for x in ml) and ids_set(ml) == ids_set([bob["id"], carol["id"]])
            if rs.status == 200:
                good = good and before == after
            check("8.5", good, "the author is never mentioned", "got %s mentions %s (alice is %s); alice mention_count %s -> %s"
                  % (m.status, ml, alice["id"], before, after))
        finally:
            wa.close()
    finally:
        w.close()
    end = time.time() + 2.0
    ml = None
    while time.time() < end:
        m = call("POST", P, {"body": "@here again"}, alice["tok"])
        ml = m.get("message", "mentions")
        if ml == []:
            break
        time.sleep(0.4)
    check("8.3", ml == [], "@here no longer mentions a user whose socket closed", "last mentions %s (want [])" % ml)
    m = call("POST", P, {"body": "@nosuchgroup hi"}, alice["tok"])
    check("8.6", m.status == 201 and m.get("message", "mentions") == [], "unknown handle mentions nobody",
          "got %s mentions %s" % (m.status, m.get("message", "mentions")))


STEPS = {
    1: [(["1.%d" % i for i in range(1, 7)], None), (["2.%d" % i for i in range(1, 9)], s2)],
    2: [(["3.%d" % i for i in range(1, 5)], s3), (["4.%d" % i for i in range(1, 7)], s4)],
    3: [(["5.%d" % i for i in range(1, 8)], s5), (["6.%d" % i for i in range(1, 8)], s6)],
    4: [(["7.%d" % i for i in range(1, 6)], s7), (["8.1", "8.2", "8.4", "8.5", "8.3", "8.6"], s8)],
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

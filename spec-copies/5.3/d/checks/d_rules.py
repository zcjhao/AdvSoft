"""Rules of Component D (d-1.md to d-3.md). Run through check_d_N.sh, not directly."""
import os, socket, statistics, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chk import *  # noqa: F401,F403


def redis_ping():
    try:
        s = socket.create_connection(("127.0.0.1", 6379), timeout=1)
        s.sendall(b"PING\r\n")
        s.settimeout(1)
        r = s.recv(64)
        s.close()
        return r.startswith(b"+PONG")
    except Exception:
        return False


def evs(ws, n, timeout):
    return [f.get("event_id") for f in ws.events(n, timeout, kinds=["message.created"])]


# ---------------- 2. supervisor and Redis ----------------
def s2():
    own, bob = user("own"), user("bob")
    ws = workspace(own, "redis")
    cid = ws["general"]
    join(bob, cid)
    up = redis_ping()
    check("2.1", up, "redis answers PING on 127.0.0.1:6379", "no +PONG on 127.0.0.1:6379")
    w = WS(8001, bob["tok"])
    try:
        h = w.subscribe(cid).get("head_event_id")
        if not isinstance(h, int):
            raise Setup("subscribe gave no head_event_id")
        pid = kill_port(6379)
        t_kill = time.time()
        time.sleep(0.2)
        st = []
        for i, n in enumerate(NODES):
            t0 = time.time()
            r = call("POST", "%s/api/channels/%s/messages" % (n, cid), {"body": "no redis %d" % i}, own["tok"])
            st.append((r.status, round(time.time() - t0, 1)))
        check("2.2", pid is not None and [s for s, _ in st] == [201] * 3 and all(t < 5 for _, t in st), "writes on all nodes succeed while redis is dead",
              "killed redis pid %s; posts on 8000/8001/8002 got (status, seconds) %s" % (pid, st))
        got = evs(w, 3, 5.0)
        check("2.3", got == [h + 1, h + 2, h + 3], "cross-node fan-out continues while redis is dead",
              "subscriber on 8001 got event ids %s (want %s)" % (got, [h + 1, h + 2, h + 3]))
        back = wait_for(redis_ping, 10 - (time.time() - t_kill), 0.3)
        check("2.4", back, "start.sh restarts redis within 10 s", "redis still not answering PING %.1fs after the kill" % (time.time() - t_kill))
        w0, w2 = WS(8000, own["tok"]), WS(8002, own["tok"])
        try:
            w0.subscribe(cid); w2.subscribe(cid)
            r = call("POST", "%s/api/channels/%s/messages" % (N1, cid), {"body": "redis is back"}, bob["tok"])
            a, b = evs(w0, 1, 2.0), evs(w2, 1, 2.0)
            check("2.5", r.status == 201 and a == [h + 4] and b == [h + 4], "fan-out after redis is back",
                  "post %s; 8000 got %s, 8002 got %s (want [%d])" % (r.status, a, b, h + 4))
        finally:
            w0.close(); w2.close()
    finally:
        w.close()


# ---------------- 3. killed HTTP node ----------------
def s3():
    own, bob = user("own"), user("bob")
    ws = workspace(own, "kill")
    cid = ws["general"]
    join(bob, cid)
    w1, w2 = WS(8001, bob["tok"]), WS(8002, bob["tok"])
    try:
        h = w1.subscribe(cid).get("head_event_id")
        w2.subscribe(cid)
        if not isinstance(h, int):
            raise Setup("subscribe gave no head_event_id")
        post(own, cid, "before the kill")
        last = evs(w1, 1, 2.0)
        evs(w2, 1, 2.0)
        if last != [h + 1]:
            raise Setup("socket on 8001 did not get the first event (%s)" % last)
        pid = kill_port(8001)
        t_kill = time.time()
        if not pid:
            raise Setup("no process owns port 8001")
        time.sleep(0.3)
        hs = [call("GET", n + "/api/health", timeout=1) for n in (N0, N2)]
        got = [(r.status, r.get("node_id")) for r in hs]
        check("3.1", got == [(200, 0), (200, 2)] and time.time() - t_kill < 2.5, "8000 and 8002 keep answering after 8001 is killed",
              "health got %s (want [(200, 0), (200, 2)])" % got)

        lg = [call("POST", n + "/api/auth/login", {"username": own["name"], "password": PW}).status for n in (N0, N2)]
        p0 = call("POST", "%s/api/channels/%s/messages" % (N0, cid), {"body": "while 8001 is down"}, own["tok"])
        p2 = call("POST", "%s/api/channels/%s/messages" % (N2, cid), {"body": "still down"}, own["tok"])
        l = call("GET", "%s/api/channels/%s/messages" % (N2, cid), None, bob["tok"])
        bodies = [m.get("body") for m in (l.get("messages") or [])][:2]
        check("3.2", lg == [200, 200] and p0.status == 201 and p2.status == 201 and bodies == ["still down", "while 8001 is down"],
              "login, post and list work on 8000 and 8002", "logins %s, posts %s %s, listing %s" % (lg, p0.status, p2.status, bodies))
        got = evs(w2, 2, 2.0)
        check("3.3", got == [h + 2, h + 3], "a socket on 8002 gets messages posted while 8001 is down",
              "got event ids %s (want %s)" % (got, [h + 2, h + 3]))
        w1.close()
        w3 = WS(8002, bob["tok"])
        w3.send({"type": "resume", "channel_id": cid, "since_event_id": h + 1})
        end, seen = w3.until(lambda f: isinstance(f, dict) and f.get("type") == "resumed", 3.0)
        w3.close()
        got = [f.get("event_id") for f in seen if isinstance(f, dict) and "event_id" in f]
        check("3.4", got == [h + 2, h + 3] and end is not None and end.get("head_event_id") == h + 3,
              "resume on 8002 replays what the cut socket missed", "replayed %s then %s (want [%d, %d] then resumed head %d)"
              % (got, short(end, 100), h + 2, h + 3, h + 3))

        def back():
            r = call("GET", N1 + "/api/health", timeout=1)
            return r.status == 200 and r.get("node_id") == 1
        ok_back = wait_for(back, 15 - (time.time() - t_kill), 0.3)
        check("3.5", ok_back, "8001 comes back by itself within 15 s", "8001 not healthy %.1fs after the kill" % (time.time() - t_kill))
        if not ok_back:
            raise Setup("8001 did not come back")
        w0, w4 = WS(8000, own["tok"]), WS(8001, bob["tok"])
        try:
            w0.subscribe(cid); w4.subscribe(cid)
            a = call("POST", "%s/api/channels/%s/messages" % (N1, cid), {"body": "8001 is back"}, bob["tok"]).status
            b = call("POST", "%s/api/channels/%s/messages" % (N2, cid), {"body": "hello 8001"}, own["tok"]).status
            g0, g4 = evs(w0, 2, 2.0), evs(w4, 2, 2.0)
            want = [h + 4, h + 5]
            check("3.6", (a, b) == (201, 201) and g0 == want and g4 == want, "the restarted node is back in the cluster",
                  "posts %s %s; 8000 got %s, 8001 got %s (want %s)" % (a, b, g0, g4, want))
        finally:
            w0.close(); w4.close()
    finally:
        w1.close(); w2.close()


# ---------------- 4. performance ----------------
def s4():
    own = user("own")
    subs = [user("s%d" % i) for i in range(3)]
    ws = workspace(own, "perf")
    cid = channel(own, ws, "load")
    for u in subs:
        join(u, cid)
    socks = [WS(PORTS[i], subs[i]["tok"]) for i in range(3)]
    try:
        heads = [s.subscribe(cid).get("head_event_id") for s in socks]
        if not all(isinstance(x, int) for x in heads) or len(set(heads)) != 1:
            raise Setup("subscribe heads %s" % heads)
        h = heads[0]
        sent = {}
        statuses = []
        lock = threading.Lock()
        recv = [dict() for _ in socks]
        stop = [False]

        def reader(i):
            s = socks[i]
            while not stop[0]:
                f = s.recv(0.5)
                if isinstance(f, dict) and f.get("type") == "message.created":
                    body = (f.get("message") or {}).get("body", "")
                    recv[i].setdefault(body, (time.time(), f.get("event_id")))

        def writer(node, k):
            for j in range(10):
                body = "load %d-%d-%d %s" % (node, k, j, RUN)
                with lock:
                    sent[body] = time.time()
                r = call("POST", "%s/api/channels/%s/messages" % (NODES[node], cid), {"body": body}, own["tok"])
                with lock:
                    statuses.append(r.status)

        readers = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(3)]
        for t in readers:
            t.start()
        writers = [threading.Thread(target=writer, args=(n, k)) for n in range(3) for k in range(3)]
        for t in writers:
            t.start()
        for t in writers:
            t.join()
        end = time.time() + 5
        while time.time() < end and not all(len(r) >= len(sent) for r in recv):
            time.sleep(0.1)
        stop[0] = True
        for t in readers:
            t.join(1)

        check("4.1", statuses.count(201) == 90, "all 90 concurrent posts return 201",
              "%d of 90 returned 201 (others: %s)" % (statuses.count(201), sorted(set(s for s in statuses if s != 201))))
        want = list(range(h + 1, h + 91))
        dense = [sorted(e for _, e in r.values()) == want for r in recv]
        check("4.2", all(dense), "every subscriber sees event ids head+1..head+90 exactly once",
              "dense per subscriber (8000, 8001, 8002): %s; counts %s" % (dense, [len(r) for r in recv]))
        lat = [[(r[b][0] - sent[b]) * 1000 for b in sent if b in r] for r in recv]
        med = [round(statistics.median(l)) if l else None for l in lat]
        p95 = [round(sorted(l)[max(0, int(len(l) * 0.95) - 1)]) if l else None for l in lat]
        check("4.3", all(m is not None and m <= 300 for m in med), "median delivery latency at most 300 ms",
              "median ms per subscriber (8000, 8001, 8002): %s" % med)
        check("4.4", all(p is not None and p <= 800 for p in p95), "p95 delivery latency at most 800 ms",
              "p95 ms per subscriber (8000, 8001, 8002): %s" % p95)
        miss = [len(sent) - len([b for b in sent if b in r]) for r in recv]
        check("4.5", all(m <= len(sent) * 0.01 for m in miss), "at most 1% of events missing",
              "missing per subscriber (8000, 8001, 8002): %s of %d" % (miss, len(sent)))
    finally:
        for s in socks:
            s.close()


STEPS = {
    1: [(["1.%d" % i for i in range(1, 7)], None), (["2.%d" % i for i in range(1, 6)], s2)],
    2: [(["3.%d" % i for i in range(1, 7)], s3)],
    3: [(["4.%d" % i for i in range(1, 6)], s4)],
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

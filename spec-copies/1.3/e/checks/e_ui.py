"""Rules of Component E, specs e-4.md to e-6.md (the web UI). Run through check_e_N.sh, not directly.

Runs under /opt/test-venv/bin/python, which has Playwright and Chromium installed.
"""
import os, re, sys, time
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chk import *  # noqa: F401,F403
import chk

STATIC_IDS = ("auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace join-workspace-form "
              "join-workspace-submit channel-list dms-list new-channel-btn current-user logout-btn workspace-header "
              "workspace-settings-btn workspace-settings-close workspace-name-input workspace-join-mode workspace-general-submit "
              "channel-title channel-topic channel-settings-btn channel-settings-title channel-settings-close channel-members-list "
              "channel-add-member-form channel-add-member-input channel-add-member-submit channel-topic-input channel-topic-submit "
              "archive-channel-btn unarchive-channel-btn create-channel-form create-channel-private create-channel-submit "
              "create-channel-cancel message-list message-input send-btn thread-panel thread-input thread-send close-thread "
              "settings-tab-members settings-tab-invitations settings-tab-general settings-pane-members settings-pane-invitations "
              "settings-pane-general create-invitation-btn create-invitation-form create-invitation-submit invitations-list "
              "sidebar auth-error create-channel-error").split()
DOM_IDS = ["auth-modal", "create-channel-modal", "channel-settings-modal", "workspace-settings-modal", "create-invitation-form"]
ROLES = {
    "primary": ("rgb(0, 122, 90)", "auth-submit send-btn thread-send create-channel-submit join-workspace-submit workspace-general-submit "
                "create-invitation-submit channel-topic-submit channel-add-member-submit".split()),
    "danger": ("rgb(224, 30, 90)", ["archive-channel-btn", "logout-btn"]),
    "secondary": ("rgb(18, 100, 163)", "new-channel-btn create-invitation-btn unarchive-channel-btn create-channel-cancel close-thread "
                  "channel-settings-close workspace-settings-close channel-settings-btn workspace-settings-btn".split()),
}
WHITE = "rgb(255, 255, 255)"
T0 = time.time()
BUDGET = 26.0

# What the browser saw go wrong since the last reported rule: server errors (5xx) and uncaught page errors.
SEEN = []
_orig_bad = chk.bad


def _bad_with_browser(rule, msg):
    if SEEN:
        msg = "%s | browser saw: %s" % (msg, short("; ".join(SEEN[:3]), 300))
    del SEEN[:]
    _orig_bad(rule, msg)


chk.bad = _bad_with_browser
_orig_ok = chk.ok


def _ok_and_forget(rule, desc):
    del SEEN[:]
    _orig_ok(rule, desc)


chk.ok = _ok_and_forget
PAGE = [None]


def explain(e):
    """Turn a Playwright timeout into what is actually on the page."""
    msg = str(e)
    m = re.search(r"Locator\.(\w+): Timeout.*?waiting for (locator\(.*?\)(?:\.first)?(?:\.locator\(.*?\)(?:\.first)?)*)\n", msg, re.S)
    if not m or PAGE[0] is None:
        return "check error: %s" % short(repr(e), 200)
    action, loc = m.group(1), m.group(2)
    sels = re.findall(r'locator\("((?:[^"\\]|\\.)*)"\)', loc)
    sels = [x.replace('\\"', '"') for x in sels]
    parts = []
    try:
        cur = PAGE[0]
        for sel in sels:
            cur = cur.locator(sel)
            n = cur.count()
            vis = n > 0 and cur.first.is_visible()
            parts.append("%s: %d found%s" % (sel, n, ", visible" if vis else (", hidden" if n else "")))
            if n == 0:
                break
            cur = cur.first
        body = PAGE[0].locator("body").inner_text(timeout=500).replace("\n", " / ")[:120]
    except Exception:
        body = "?"
    return "could not %s within 3 s: %s; the page shows %r" % (action, " -> ".join(parts) or loc, body)


def section(rules, fn):
    before = chk.passed + chk.failed
    try:
        fn()
    except Setup as e:
        for r in rules[chk.passed + chk.failed - before:]:
            chk.bad(r, "setup failed (%s); make the earlier rules pass first" % e)
    except Exception as e:
        why = explain(e)
        for r in rules[chk.passed + chk.failed - before:]:
            chk.bad(r, why)


class Attrs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.testids, self.ids = {}, set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("data-testid"):
            self.testids.setdefault(a["data-testid"], a)
        if a.get("id"):
            self.ids.add(a["id"])


def pw():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception as e:
        raise Setup("Playwright is not importable (%s); run this check with bash, as /spec/README.md says" % e)


def launch(p):
    kw = {"headless": True, "args": ["--no-sandbox"]}
    if os.environ.get("CHK_CHROMIUM"):
        kw["executable_path"] = os.environ["CHK_CHROMIUM"]
    b = p.chromium.launch(**kw)
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.set_default_timeout(3000)
    dialogs = []

    def on_dialog(d):
        dialogs.append(d.type)
        try:
            d.dismiss()
        except Exception:
            pass
    pg.on("dialog", on_dialog)
    pg.on("response", lambda r: r.status >= 500 and SEEN.append("%s %s -> %d" % (r.request.method, r.url.split("?")[0], r.status)))
    pg.on("pageerror", lambda e: SEEN.append("JS error: %s" % str(e)[:120]))
    PAGE[0] = pg
    return b, pg, dialogs


def tid(pg, name):
    return pg.locator('[data-testid="%s"]' % name)


def visible(loc):
    try:
        return loc.first.is_visible()
    except Exception:
        return False


def text(loc):
    try:
        return (loc.first.inner_text(timeout=1500) or "").strip()
    except Exception:
        return ""


def wait_true(fn, timeout=3.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    try:
        return bool(fn())
    except Exception:
        return False


def out_of_time():
    return time.time() - T0 > BUDGET


def with_token(pg, tok):
    pg.goto(N0 + "/", wait_until="domcontentloaded")
    pg.evaluate("t => localStorage.setItem('huddle.token', t)", tok)
    pg.goto(N0 + "/", wait_until="domcontentloaded")


# ---------------- 5. shell ----------------
def s5():
    rs = [call("GET", n + "/", timeout=5) for n in NODES]
    ctypes = [(r.headers.get("Content-Type") or r.headers.get("content-type") or "") for r in rs]
    html = (rs[0].raw or b"").decode("utf-8", "replace")
    check("5.1", [r.status for r in rs] == [200] * 3 and all("text/html" in c for c in ctypes) and "<html" in html.lower()
          and rs[1].raw == rs[0].raw and rs[2].raw == rs[0].raw, "GET / serves the same HTML page on all nodes",
          "statuses %s, content types %s" % ([r.status for r in rs], ctypes))
    p = Attrs()
    try:
        p.feed(html)
    except Exception:
        pass
    miss = [t for t in STATIC_IDS if t not in p.testids]
    check("5.2", not miss, "every pinned data-testid is in the served HTML", "missing %s" % short(miss, 400))
    miss = [i for i in DOM_IDS if i not in p.ids]
    check("5.3", not miss, "the five pinned DOM ids are in the served HTML", "missing %s" % miss)
    wrong = []
    for role, (_, names) in ROLES.items():
        for n in names:
            got = (p.testids.get(n) or {}).get("data-button-role")
            if got != role:
                wrong.append("%s=%s" % (n, got))
    check("5.4", not wrong, "buttons carry the right data-button-role", "wrong or missing: %s" % short(wrong, 400))

    sp = pw()
    with sp() as pwr:
        b, pg, _ = launch(pwr)
        try:
            pg.goto(N0 + "/", wait_until="domcontentloaded")
            pg.evaluate("() => localStorage.clear()")
            pg.goto(N0 + "/", wait_until="load")
            pg.mouse.move(1270, 890)
            wrong = []
            for role, (bg, names) in ROLES.items():
                for n in names:
                    st = pg.evaluate("""n => { const e = document.querySelector('[data-testid="' + n + '"]');
                        if (!e) return null; const s = getComputedStyle(e); return [s.backgroundColor, s.color]; }""", n)
                    if st != [bg, WHITE]:
                        wrong.append("%s: %s" % (n, st))
            check("5.5", not wrong, "button colours match their roles", "wrong: %s" % short(wrong, 500))
            wait_true(lambda: visible(tid(pg, "auth-form")), 3)
            check("5.6", visible(tid(pg, "auth-form")) and not visible(tid(pg, "message-list")), "signed out shows only the auth form",
                  "auth-form visible %s, message-list visible %s" % (visible(tid(pg, "auth-form")), visible(tid(pg, "message-list"))))
        finally:
            b.close()


# ---------------- 6. auth, workspace, channels ----------------
def fill_auth(pg, mode, username, password):
    form = tid(pg, "auth-form")
    for _ in range(3):
        if form.first.get_attribute("data-mode") == mode:
            break
        tid(pg, "auth-toggle").first.click()
        time.sleep(0.1)
    form.locator('input[name="username"]').first.fill(username)
    form.locator('input[name="password"]').first.fill(password)
    tid(pg, "auth-submit").first.click()


def s6():
    name = uname("ui")
    rules = ["6.%d" % i for i in range(1, 9)]
    sp = pw()
    with sp() as pwr:
        b, pg, _ = launch(pwr)
        try:
            pg.goto(N0 + "/", wait_until="domcontentloaded")
            pg.evaluate("() => localStorage.clear()")
            pg.goto(N0 + "/", wait_until="load")
            wait_true(lambda: visible(tid(pg, "auth-form")), 3)
            fill_auth(pg, "register", "bad name", "short")
            shown = wait_true(lambda: visible(tid(pg, "auth-error")) and text(tid(pg, "auth-error")) != "", 3)
            tok = pg.evaluate("() => localStorage.getItem('huddle.token')")
            check("6.1", shown and not tok, "invalid sign-up shows auth-error and stores no token",
                  "auth-error visible with text %s (%r), token stored %r" % (shown, text(tid(pg, "auth-error"))[:60], tok))

            fill_auth(pg, "register", name, PW)
            okt = wait_true(lambda: len(pg.evaluate("() => localStorage.getItem('huddle.token') || ''")) >= 16, 4)
            cu = wait_true(lambda: name in text(tid(pg, "current-user")), 3)
            check("6.2", okt and cu, "valid sign-up logs in and shows current-user",
                  "token stored %s, current-user text %r" % (okt, text(tid(pg, "current-user"))[:60]))
            if not okt:
                raise Setup("sign-up did not log in")

            slug = "ui%d-%s" % (SEQ[0], RUN)
            wait_true(lambda: visible(tid(pg, "workspace-create-form")), 3)
            f = tid(pg, "workspace-create-form").first
            f.locator('input[name="slug"]').fill(slug)
            f.locator('input[name="name"]').fill("UI Space " + RUN)
            f.locator('[type="submit"]').first.click()
            hdr = wait_true(lambda: ("UI Space " + RUN) in text(tid(pg, "workspace-header")), 4)
            gen = pg.locator('[data-testid="channel-list"] [data-channel-name="general"]')
            okg = wait_true(lambda: gen.count() > 0 and "#" in text(gen), 3)
            check("6.3", hdr and okg, "create a workspace from the empty state",
                  "workspace-header %r, general entry found %s text %r" % (text(tid(pg, "workspace-header"))[:60], gen.count() > 0, text(gen)[:30]))
            if not okg:
                raise Setup("no workspace in the UI")

            tid(pg, "new-channel-btn").first.click()
            wait_true(lambda: visible(tid(pg, "create-channel-form")), 2)
            tid(pg, "create-channel-form").locator('input[name="name"]').first.fill("ui-room")
            tid(pg, "create-channel-submit").first.click()
            room = pg.locator('[data-testid="channel-list"] [data-channel-name="ui-room"]')
            okr = wait_true(lambda: room.count() == 1 and not visible(tid(pg, "create-channel-form"))
                            and "ui-room" in text(tid(pg, "channel-title")), 4)
            check("6.4", okr, "create a channel from the modal and select it",
                  "entries %d, modal still open %s, channel-title %r" % (room.count(), visible(tid(pg, "create-channel-form")), text(tid(pg, "channel-title"))[:40]))

            emp = tid(pg, "empty-channel")
            oke = wait_true(lambda: visible(emp) and text(emp) != "", 3)
            check("6.5", oke, "an empty channel shows non-blank empty-state text", "empty-channel visible %s text %r" % (visible(emp), text(emp)[:60]))

            tid(pg, "new-channel-btn").first.click()
            wait_true(lambda: visible(tid(pg, "create-channel-form")), 2)
            tid(pg, "create-channel-form").locator('input[name="name"]').first.fill("ui-room")
            tid(pg, "create-channel-submit").first.click()
            err = wait_true(lambda: visible(tid(pg, "create-channel-error")) and text(tid(pg, "create-channel-error")) != "", 3)
            still = visible(tid(pg, "create-channel-form"))
            check("6.6", err and still and room.count() == 1, "duplicate channel name shows an error in the open modal",
                  "error visible %s (%r), modal open %s, ui-room entries %d" % (err, text(tid(pg, "create-channel-error"))[:60], still, room.count()))
            if visible(tid(pg, "create-channel-cancel")):
                tid(pg, "create-channel-cancel").first.click()

            tok = pg.evaluate("() => localStorage.getItem('huddle.token')")
            rid = room.first.get_attribute("data-channel-id")
            call("PATCH", "%s/api/channels/%s" % (N1, rid), {"topic": "Topic " + RUN}, tok)
            gen.first.click()
            a = wait_true(lambda: "general" in text(tid(pg, "channel-title")), 3)
            room.first.click()
            c = wait_true(lambda: "ui-room" in text(tid(pg, "channel-title")) and ("Topic " + RUN) in text(tid(pg, "channel-topic")), 3)
            check("6.7", a and c, "switching channels updates channel-title and channel-topic",
                  "general selected %s; ui-room title %r topic %r" % (a, text(tid(pg, "channel-title"))[:40], text(tid(pg, "channel-topic"))[:40]))

            tid(pg, "logout-btn").first.click()
            out = wait_true(lambda: visible(tid(pg, "auth-form")) and not pg.evaluate("() => localStorage.getItem('huddle.token')"), 3)
            fill_auth(pg, "login", name, PW)
            back = wait_true(lambda: ("UI Space " + RUN) in text(tid(pg, "workspace-header")) and gen.count() > 0, 4)
            check("6.8", out and back, "logout clears the token; login restores the same workspace",
                  "after logout auth-form shown and token cleared %s; after login header %r" % (out, text(tid(pg, "workspace-header"))[:60]))
        finally:
            b.close()


# ---------------- 7. messages ----------------
def row_by_body(pg, body):
    return pg.locator('[data-testid="message-list"] [data-testid="message"]', has_text=body)


def row_by_id(pg, mid):
    return pg.locator('[data-testid="message-list"] [data-testid="message"][data-message-id="%s"]' % mid)


def s7():
    a, other = user("uim"), user("uio")
    ws = workspace(a, "uimsg")
    cid = ws["general"]
    join(other, cid)
    sp = pw()
    with sp() as pwr:
        b, pg, dialogs = launch(pwr)
        try:
            with_token(pg, a["tok"])
            if not wait_true(lambda: visible(tid(pg, "message-input")), 5):
                raise Setup("message-input not visible after loading with a token in localStorage['huddle.token']")
            gen = pg.locator('[data-testid="channel-list"] [data-channel-name="general"]')
            if gen.count():
                gen.first.click()
            rows = pg.locator('[data-testid="message-list"] [data-testid="message"]')
            n0 = rows.count()
            tid(pg, "message-input").first.fill("first ui message")
            tid(pg, "send-btn").first.click()
            r1 = row_by_body(pg, "first ui message")
            okp = wait_true(lambda: r1.count() == 1, 3)
            parts = okp and visible(r1.locator('[data-testid="message-avatar"]')) and visible(r1.locator('[data-testid="message-author"]')) \
                and text(r1.locator('[data-testid="message-time"]')) != "" and "first ui message" in text(r1.locator('[data-testid="message-body"]'))
            check("7.1", okp and parts, "posting adds a row with avatar, author, time and body",
                  "row found %s, avatar/author/time/body present %s" % (okp, parts))
            if not okp:
                raise Setup("posting from the UI does not work")

            n1 = rows.count()
            tid(pg, "message-input").first.fill("   ")
            tid(pg, "send-btn").first.click()
            time.sleep(0.6)
            check("7.2", rows.count() == n1 and n1 == n0 + 1, "a blank message is not posted", "rows %d -> %d" % (n1, rows.count()))

            mid1 = r1.first.get_attribute("data-message-id")
            if not mid1:
                raise Setup("message rows have no data-message-id")
            r1 = row_by_id(pg, mid1)
            r1.first.hover()
            r1.locator('[data-testid="message-edit-btn"]').first.click()
            ed = r1.locator('[data-testid="message-edit-input"]')
            wait_true(lambda: visible(ed), 2)
            ed.first.fill("first ui message (changed)")
            ed.first.press("Enter")
            oke = wait_true(lambda: "first ui message (changed)" in text(r1.locator('[data-testid="message-body"]'))
                            and visible(r1.locator('[data-testid="message-edited"]')), 3)
            check("7.3", oke, "edit from the hover toolbar shows the new body and (edited)", "edited row found %s" % (r1.count() == 1))

            tid(pg, "message-input").first.fill("second ui message")
            tid(pg, "send-btn").first.click()
            r2 = row_by_body(pg, "second ui message")
            wait_true(lambda: r2.count() == 1, 3)
            pg.reload(wait_until="load")
            if gen.count():
                gen.first.click()
            r1 = row_by_body(pg, "first ui message (changed)")
            okr = wait_true(lambda: r1.count() == 1 and visible(r1.locator('[data-testid="message-edited"]')), 4)
            check("7.4", okr, "the edit survives a hard reload", "edited row with (edited) after reload: %s" % okr)

            r2 = row_by_body(pg, "second ui message")
            wait_true(lambda: r2.count() == 1, 2)
            r2.first.hover()
            r2.locator('[data-testid="message-delete-btn"]').first.click()
            gone = wait_true(lambda: row_by_body(pg, "second ui message").count() == 0, 3)
            pg.reload(wait_until="load")
            if gen.count():
                gen.first.click()
            wait_true(lambda: row_by_body(pg, "first ui message").count() == 1, 3)
            still_gone = row_by_body(pg, "second ui message").count() == 0
            check("7.5", gone and still_gone and "confirm" not in dialogs, "delete from the hover toolbar removes the row for good",
                  "gone %s, gone after reload %s, dialogs %s" % (gone, still_gone, dialogs))

            r1 = row_by_body(pg, "first ui message (changed)")
            r1.first.hover()
            r1.locator('[data-testid="open-thread-btn"]').first.click()
            tp = tid(pg, "thread-panel")
            okt = wait_true(lambda: visible(tp), 3)
            right = False
            try:
                bt, bl = tp.first.bounding_box(), tid(pg, "message-list").first.bounding_box()
                right = bt["x"] >= bl["x"] + bl["width"] - 5 and visible(tid(pg, "message-list"))
            except Exception:
                pass
            tid(pg, "thread-input").first.fill("a ui reply")
            tid(pg, "thread-send").first.click()
            okrep = wait_true(lambda: tp.locator('[data-testid="thread-message"]', has_text="a ui reply").count() >= 1, 3)
            okcnt = wait_true(lambda: "1 repl" in text(r1.locator('[data-testid="reply-count"]')), 3)
            tid(pg, "close-thread").first.click()
            closed = wait_true(lambda: not visible(tp), 2)
            check("7.6", okt and right and okrep and okcnt and closed, "thread panel on the right, reply bumps the reply count",
                  "panel visible %s, right of message-list %s, reply shown %s, reply-count %r, closes %s"
                  % (okt, right, okrep, text(r1.locator('[data-testid="reply-count"]'))[:30], closed))

            r1.first.hover()
            r1.locator('[data-testid="reaction-button"]').first.click()
            opts = pg.locator('[data-testid="emoji-picker"] [data-testid="emoji-option"]')
            okpk = wait_true(lambda: visible(tid(pg, "emoji-picker")) and opts.count() >= 8, 3)
            nopts = opts.count()
            if nopts:
                opts.first.click()
            chip = r1.locator('[data-testid="reaction-chip"]')
            okc = wait_true(lambda: chip.count() >= 1 and "1" in text(chip), 3)
            check("7.7", okpk and okc and "prompt" not in dialogs, "emoji picker grid adds a reaction chip with count 1",
                  "picker visible with %d options, chip text %r, dialogs %s" % (nopts, text(chip)[:20], dialogs))

            call("POST", "%s/api/channels/%s/messages" % (N2, cid), {"body": "live from api " + RUN}, other["tok"])
            okl = wait_true(lambda: row_by_body(pg, "live from api " + RUN).count() == 1, 3)
            check("7.8", okl, "a message from another user appears without a reload", "row appeared %s" % okl)

            lay = pg.evaluate("""() => { const s = document.querySelector('[data-testid="sidebar"]'),
                    m = document.querySelector('[data-testid="message-list"]');
                if (!s || !m) return null; const a = s.getBoundingClientRect(), b = m.getBoundingClientRect();
                return [a.x + a.width <= b.x + 5, getComputedStyle(s).backgroundColor, a.width > 0]; }""")
            bg = (lay or [None, ""])[1] or ""
            good = bool(lay) and lay[0] and lay[2] and bg not in ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)", "transparent", "") \
                and visible(tid(pg, "workspace-header")) and visible(tid(pg, "channel-title"))
            check("7.9", good, "three-pane layout with a coloured sidebar", "sidebar left of messages / bg / visible: %s" % (lay,))
        finally:
            b.close()


STEPS = {4: (["5.%d" % i for i in range(1, 7)], s5), 5: (["6.%d" % i for i in range(1, 9)], s6), 6: (["7.%d" % i for i in range(1, 10)], s7)}


def main():
    boot()
    step = step_from_env()
    for st in sorted(STEPS) if step == 0 else [step]:
        rules, fn = STEPS[st]
        section(rules, fn)
    finish()


if __name__ == "__main__":
    main()

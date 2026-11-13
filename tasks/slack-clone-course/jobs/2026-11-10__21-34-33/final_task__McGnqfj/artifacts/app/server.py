import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB="/app/data/app.db"
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=="node" else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=="node" else 8000
sockets=[]

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def db():
    os.makedirs("/app/data",exist_ok=True)
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c
def init():
    c=db()
    c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));""")
    c.commit(); c.close()
def userobj(r):
    return {k:r[k] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}
def chanobj(r):
    return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
def message(c,r):
    u=c.execute("select * from users where id=?",(r["author_id"],)).fetchone()
    replies=c.execute("select count(*) n from messages where parent_id=? and deleted=0",(r["id"],)).fetchone()["n"]
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":userobj(u),"body":r["body"],"parent_id":r["parent_id"],"reply_count":replies,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":[],"mentions":[]}
def auth(req,c):
    h=req.headers.get("Authorization","")
    token = h[7:] if h.startswith("Bearer ") else req.query.get("token","")
    if not token: return None
    r=c.execute("select u.* from tokens t join users u on u.id=t.user_id where t.token=?",(token,)).fetchone()
    return r
def out(data,status=200): return web.json_response(data,status=status)
async def health(req): return out({"status":"ok","node_id":NODE})
async def register(req):
    c=db()
    try: d=await req.json()
    except: return out({"error":"invalid"},400)
    un=d.get("username",""); pw=d.get("password","")
    if not re.fullmatch(r"[A-Za-z0-9_]+",un) or len(pw)<8:return out({"error":"invalid"},400)
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("insert into users(username,password,display_name) values(?,?,?)",(un,pw,d.get("display_name") or un)); uid=c.execute("select last_insert_rowid()").fetchone()[0]; tok=secrets.token_urlsafe(24); c.execute("insert into tokens values(?,?)",(tok,uid)); c.commit()
    except sqlite3.IntegrityError: c.rollback(); return out({"error":"duplicate"},409)
    return out({"user":userobj(c.execute("select * from users where id=?",(uid,)).fetchone()),"token":tok},201)
async def login(req):
    c=db(); d=await req.json(); r=c.execute("select * from users where username=? and password=?",(d.get("username"),d.get("password"))).fetchone()
    if not r:return out({"error":"unauthorized"},401)
    tok=secrets.token_urlsafe(24); c.execute("insert into tokens values(?,?)",(tok,r["id"])); c.commit()
    return out({"user":userobj(r),"token":tok})
async def me(req):
    c=db(); u=auth(req,c)
    return out({"user":userobj(u)}) if u else out({"error":"unauthorized"},401)
async def need(req):
    c=db(); u=auth(req,c)
    return c,u
def append(c,cid,typ,**fields):
    n=c.execute("select coalesce(max(event_id),0)+1 n from events where channel_id=?",(cid,)).fetchone()["n"]
    f={"type":typ,"event_id":n,"channel_id":cid}; f.update(fields)
    c.execute("insert into events values(?,?,?)",(cid,n,json.dumps(f,separators=(",",":")))); return f
async def workspaces(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    rows=c.execute("select w.* from workspaces w join wm m on m.workspace_id=w.id where m.user_id=?",(u["id"],)).fetchall()
    return out({"workspaces":[{"id":r["id"],"slug":r["slug"],"name":r["name"],"owner_id":r["owner_id"],"join_mode":r["join_mode"]} for r in rows]})
async def create_ws(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    d=await req.json(); slug=d.get("slug","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not d.get("name"):return out({"error":"invalid"},400)
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("insert into workspaces(slug,name,owner_id) values(?,?,?)",(slug,d["name"],u["id"])); wid=c.execute("select last_insert_rowid()").fetchone()[0]; c.execute("insert into wm values(?,?,?)",(wid,u["id"],"owner")); c.execute("insert into channels(workspace_id,name) values(?,?)",(wid,"general")); cid=c.execute("select last_insert_rowid()").fetchone()[0]; c.execute("insert into cm values(?,?)",(cid,u["id"])); c.commit()
    except sqlite3.IntegrityError:c.rollback();return out({"error":"duplicate"},409)
    w=c.execute("select * from workspaces where id=?",(wid,)).fetchone(); ch=c.execute("select * from channels where id=?",(cid,)).fetchone()
    return out({"workspace":{"id":wid,"slug":slug,"name":d["name"],"owner_id":u["id"],"join_mode":"open"},"general_channel":chanobj(ch)},201)
async def workspace(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    w=c.execute("select * from workspaces where slug=?",(req.match_info["slug"],)).fetchone()
    if not w:return out({"error":"not found"},404)
    member=c.execute("select 1 from wm where workspace_id=? and user_id=?",(w["id"],u["id"])).fetchone()
    if not member:return out({"error":"forbidden"},403)
    cs=c.execute("select * from channels where workspace_id=? and is_archived=0",(w["id"],)).fetchall()
    return out({"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":w["join_mode"]},"channels":[chanobj(x) for x in cs],"read_state":[]})
async def create_channel(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    w=c.execute("select * from workspaces where slug=?",(req.match_info["slug"],)).fetchone()
    if not w:return out({"error":"not found"},404)
    if not c.execute("select 1 from wm where workspace_id=? and user_id=?",(w["id"],u["id"])).fetchone():return out({"error":"forbidden"},403)
    d=await req.json(); name=d.get("name","")
    if not re.fullmatch(r"[a-z0-9-]{1,32}",name):return out({"error":"invalid"},400)
    if c.execute("select 1 from channels where workspace_id=? and name=?",(w["id"],name)).fetchone():return out({"error":"duplicate"},409)
    c.execute("insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)",(w["id"],name,int(bool(d.get("is_private"))),d.get("topic",""))); cid=c.execute("select last_insert_rowid()").fetchone()[0]; c.execute("insert into cm values(?,?)",(cid,u["id"])); c.commit()
    ch = c.execute("select * from channels where id=?", (cid,)).fetchone()
    return out({"channel": chanobj(ch)}, 201)
async def join(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    ch=c.execute("select * from channels where id=?",(req.match_info["id"],)).fetchone()
    if not ch:return out({"error":"not found"},404)
    w=c.execute("select * from workspaces where id=?",(ch["workspace_id"],)).fetchone()
    c.execute("insert or ignore into wm values(?,?,?)",(w["id"],u["id"],"member")); c.execute("insert or ignore into cm values(?,?)",(ch["id"],u["id"])); c.commit()
    return out({"channel":chanobj(ch)})
async def members(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    ch=c.execute("select * from channels where id=?",(req.match_info["id"],)).fetchone()
    if not ch:return out({"error":"not found"},404)
    rs=c.execute("select u.* from users u join cm m on m.user_id=u.id where m.channel_id=?",(ch["id"],)).fetchall()
    return out({"members":[userobj(x) for x in rs]})
async def postmsg(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    ch=c.execute("select * from channels where id=?",(req.match_info["id"],)).fetchone()
    if not ch:return out({"error":"not found"},404)
    if not c.execute("select 1 from cm where channel_id=? and user_id=?",(ch["id"],u["id"])).fetchone():return out({"error":"forbidden"},403)
    if ch["is_archived"]:return out({"error":"archived"},423)
    d=await req.json(); body=d.get("body","")
    if not body or not body.strip():return out({"error":"invalid"},400)
    parent=d.get("parent_id")
    if parent and not c.execute("select 1 from messages where id=? and channel_id=? and deleted=0",(parent,ch["id"])).fetchone():return out({"error":"not found"},404)
    c.execute("BEGIN IMMEDIATE"); c.execute("insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)",(ch["id"],u["id"],body,parent,now())); mid=c.execute("select last_insert_rowid()").fetchone()[0]; r=c.execute("select * from messages where id=?",(mid,)).fetchone(); m=message(c,r); append(c,ch["id"],"message.reply" if parent else "message.created",message=m); c.commit()
    return out({"message":m},201)
async def listmsg(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    cid=int(req.match_info["id"]); rs=c.execute("select * from messages where channel_id=? and parent_id is null and deleted=0 order by id desc",(cid,)).fetchall()
    return out({"messages":[message(c,r) for r in rs],"next_cursor":None})
async def replies(req):
    c,u=await need(req)
    if not u:return out({"error":"unauthorized"},401)
    rs=c.execute("select * from messages where parent_id=? and deleted=0 order by id",(req.match_info["id"],)).fetchall()
    return out({"replies":[message(c,r) for r in rs],"next_cursor":None})
async def ws_handler(req):
    c=db(); u=auth(req,c)
    if not u:return web.Response(status=401)
    ws=web.WebSocketResponse(); await ws.prepare(req); state={"channels":set(),"heads":{},"ws":ws}; sockets.append(state)
    try:
        async for x in ws:
            print("WS_FRAME", x.type, repr(x.data), flush=True)
            try:
                if x.type!=WSMsgType.TEXT: continue
                d=json.loads(x.data)
                cid=int(d.get("channel_id"))
                if d.get("type")=="subscribe":
                    h=c.execute("select coalesce(max(event_id),0) n from events where channel_id=?",(cid,)).fetchone()["n"]
                    state["channels"].add(cid); state["heads"][cid]=h
                    await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":h})
                elif d.get("type")=="resume":
                    since=int(d.get("since_event_id",0)); state["channels"].add(cid)
                    for r in c.execute("select frame from events where channel_id=? and event_id>? order by event_id",(cid,since)):
                        await ws.send_str(r["frame"])
                    h=c.execute("select coalesce(max(event_id),0) n from events where channel_id=?",(cid,)).fetchone()["n"]
                    state["heads"][cid]=h
                    await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":h})
            except Exception as e:
                print("WS_HANDLER_ERROR",repr(e),flush=True)
                await ws.close()
                break
    except Exception as e:
        print("WS_OUTER_ERROR", repr(e), flush=True)
    finally:
        if state in sockets: sockets.remove(state)
    return ws
async def pump(app):
    last={}
    while True:
        try:
            c=db()
            for r in c.execute("select * from events order by channel_id,event_id"):
                cid=r["channel_id"]; eid=r["event_id"]
                if eid<=last.get(cid,0):continue
                last[cid]=eid
                for s in list(sockets):
                    if cid in s["channels"] and eid>s["heads"].get(cid,0):
                        try: await s["ws"].send_str(r["frame"]); s["heads"][cid]=eid
                        except:
                            if s in sockets: sockets.remove(s)
            c.close()
        except Exception: pass
        await asyncio.sleep(.05)
async def startup(app):
    init(); app["task"]=asyncio.create_task(pump(app))
async def cleanup(app): app["task"].cancel()
app=web.Application()
app.router.add_get("/api/health",health); app.router.add_post("/api/auth/register",register); app.router.add_post("/api/auth/login",login); app.router.add_get("/api/auth/me",me)
app.router.add_get("/api/workspaces",workspaces); app.router.add_post("/api/workspaces",create_ws); app.router.add_get("/api/workspaces/{slug}",workspace); app.router.add_post("/api/workspaces/{slug}/channels",create_channel)
app.router.add_post("/api/channels/{id}/join",join); app.router.add_get("/api/channels/{id}/members",members); app.router.add_post("/api/channels/{id}/messages",postmsg); app.router.add_get("/api/channels/{id}/messages",listmsg); app.router.add_get("/api/messages/{id}/replies",replies); app.router.add_get("/api/ws",ws_handler)
app.on_startup.append(startup); app.on_cleanup.append(cleanup)
if __name__=="__main__": web.run_app(app,host="127.0.0.1",port=PORT)

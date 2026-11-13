import asyncio, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiohttp import web, WSMsgType
class HTTP423(web.HTTPException):
    status_code=423
    reason="Locked"

DB="/app/data/app.db"
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=="node" else 0
sockets=set()

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def db():
    os.makedirs("/app/data",exist_ok=True)
    c=sqlite3.connect(DB,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL"); c.execute("PRAGMA busy_timeout=5000")
    return c
def init():
    c=db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS workspace_members(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT 'member',PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
    CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS presence(user_id INTEGER,node_id INTEGER,ws_id TEXT,PRIMARY KEY(user_id,node_id,ws_id));
    """); c.commit(); c.close()
def userj(r):
    return {k:r[k] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}
def chan(r):
    return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
def auth(req):
    h=req.headers.get("Authorization","")
    token = h[7:] if h.startswith("Bearer ") else req.query.get("token","")
    if not token: raise web.HTTPUnauthorized()
    c=db(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(token,)).fetchone(); c.close()
    if not r: raise web.HTTPUnauthorized()
    return r
def getchan(c,i):
    r=c.execute("SELECT * FROM channels WHERE id=?",(i,)).fetchone()
    if not r: raise web.HTTPNotFound()
    return r
def accessible(c,ch,uid):
    if ch["is_dm"]: return bool(c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch["id"],uid)).fetchone())
    if ch["is_private"]: return bool(c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch["id"],uid)).fetchone())
    return bool(c.execute("SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?",(ch["workspace_id"],uid)).fetchone())
def event(c,cid,typ,**kw):
    n=c.execute("SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?",(cid,)).fetchone()[0]
    x={"type":typ,"event_id":n,"channel_id":cid}; x.update(kw)
    c.execute("INSERT INTO events VALUES(?,?,?)",(cid,n,json.dumps(x,separators=(",",":")))); return x
def filej(r):
    return {"id":r["id"],"uploader_id":r["uploader_id"],"filename":r["filename"],"content_type":r["content_type"],"size":r["size"],"created_at":r["created_at"]}

def reactions(c,mid):
    rows=c.execute("SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid",(mid,)).fetchall()
    out=[]
    for r in rows:
        x=next((z for z in out if z["emoji"]==r["emoji"]),None)
        if not x: x={"emoji":r["emoji"],"count":0,"user_ids":[]}; out.append(x)
        x["count"]+=1; x["user_ids"].append(r["user_id"])
    return out
def msg(c,r):
    a=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":userj(a),"body":r["body"],"parent_id":r["parent_id"],"reply_count":c.execute("SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()[0],"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[filej(x) for x in c.execute("SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?",(r["id"],)).fetchall()],"reactions":reactions(c,r["id"]),"mentions":json.loads(r["mentions"] or "[]")}
async def pump(app):
    last={}
    while True:
        try:
            c=db(); rows=c.execute("SELECT * FROM events ORDER BY rowid").fetchall(); grows=c.execute("SELECT * FROM global_events ORDER BY id").fetchall(); c.close()
            for r in rows:
                key=(r["channel_id"],r["event_id"])
                if key in last: continue
                last[key]=1
                for ws in list(sockets):
                    if r["channel_id"] in ws.channels and r["event_id"] > ws.seen.get(r["channel_id"], 0):
                        ws.seen[r["channel_id"]] = r["event_id"]
                        try: await ws.send_str(r["frame"])
                        except: pass
            for r in grows:
                gid=r["id"]
                if gid <= app.get("global_last",0): continue
                app["global_last"]=gid
                for ws in list(sockets):
                    try: await ws.send_str(r["frame"])
                    except: pass
        except Exception: pass
        await asyncio.sleep(.05)

def groupj(c,r):
    return {"id":r["id"],"handle":r["handle"],"name":r["name"],"member_user_ids":[x["user_id"] for x in c.execute("SELECT user_id FROM group_members WHERE group_id=? ORDER BY rowid",(r["id"],)).fetchall()]}

def workspace_for(c,slug):
    r=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not r: raise web.HTTPNotFound()
    return r

def wm(c,wid,uid):
    return bool(c.execute("SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?",(wid,uid)).fetchone())

def group_access(req,c,w):
    u=auth(req)
    if not wm(c,w["id"],u["id"]): raise web.HTTPForbidden()
    return u

async def list_groups(req):
    c=db(); w=workspace_for(c,req.match_info["slug"]); group_access(req,c,w)
    rs=c.execute("SELECT * FROM groups WHERE workspace_id=? ORDER BY id",(w["id"],)).fetchall()
    out=[groupj(c,r) for r in rs]; c.close(); return web.json_response({"groups":out})

async def create_group(req):
    c=db(); w=workspace_for(c,req.match_info["slug"]); u=group_access(req,c,w); d=await req.json()
    h=d.get("handle",""); name=d.get("name")
    if not isinstance(h,str) or not re.fullmatch(r"[a-z0-9_-]{1,32}",h) or not isinstance(name,str) or not name: c.close(); raise web.HTTPBadRequest()
    ids=d.get("user_ids",[])
    if not isinstance(ids,list) or any(not wm(c,w["id"],int(x)) for x in ids): c.close(); raise web.HTTPBadRequest()
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)",(w["id"],h,name,u["id"])); gid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for x in dict.fromkeys(ids): c.execute("INSERT INTO group_members VALUES(?,?)",(gid,int(x)))
        c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); raise web.HTTPConflict()
    r=c.execute("SELECT * FROM groups WHERE id=?",(gid,)).fetchone(); out=groupj(c,r); c.close(); return web.json_response({"group":out},status=201)

def find_group(c,w,h):
    r=c.execute("SELECT * FROM groups WHERE workspace_id=? AND handle=?",(w["id"],h)).fetchone()
    if not r: raise web.HTTPNotFound()
    return r

async def edit_group(req):
    c=db(); w=workspace_for(c,req.match_info["slug"]); u=group_access(req,c,w); g=find_group(c,w,req.match_info["handle"])
    if g["creator_id"]!=u["id"] and w["owner_id"]!=u["id"]: c.close(); raise web.HTTPForbidden()
    d=await req.json(); ids=d.get("user_ids",None)
    if ids is not None and (not isinstance(ids,list) or any(not wm(c,w["id"],int(x)) for x in ids)): c.close(); raise web.HTTPBadRequest()
    if "name" in d and (not isinstance(d["name"],str) or not d["name"]): c.close(); raise web.HTTPBadRequest()
    c.execute("BEGIN IMMEDIATE")
    if "name" in d: c.execute("UPDATE groups SET name=? WHERE id=?",(d["name"],g["id"]))
    if ids is not None:
        c.execute("DELETE FROM group_members WHERE group_id=?",(g["id"],))
        for x in dict.fromkeys(ids): c.execute("INSERT INTO group_members VALUES(?,?)",(g["id"],int(x)))
    c.commit(); out=groupj(c,c.execute("SELECT * FROM groups WHERE id=?",(g["id"],)).fetchone()); c.close(); return web.json_response({"group":out})

async def delete_group(req):
    c=db(); w=workspace_for(c,req.match_info["slug"]); u=group_access(req,c,w)
    g=c.execute("SELECT * FROM groups WHERE workspace_id=? AND handle=?",(w["id"],req.match_info["handle"])).fetchone()
    if not g: c.close(); return web.json_response({"deleted":True})
    if g["creator_id"]!=u["id"] and w["owner_id"]!=u["id"]: c.close(); raise web.HTTPForbidden()
    c.execute("DELETE FROM groups WHERE id=?",(g["id"],)); c.commit(); c.close(); return web.json_response({"deleted":True})

def resolve_mentions(c,body,ch,author):
    if ch["is_dm"] or ch["workspace_id"] is None: return []
    members={x["user_id"] for x in c.execute("SELECT user_id FROM channel_members WHERE channel_id=?",(ch["id"],)).fetchall()}
    wmembers={x["user_id"] for x in c.execute("SELECT user_id FROM workspace_members WHERE workspace_id=?",(ch["workspace_id"],)).fetchall()}
    out=[]
    for m in re.finditer(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_-]+)',body):
        key=m.group(1)
        if key=="channel": ids=members
        elif key=="here": ids={x["user_id"] for x in c.execute("SELECT DISTINCT user_id FROM presence").fetchall()} & members
        else:
            g=c.execute("SELECT id FROM groups WHERE workspace_id=? AND handle=?",(ch["workspace_id"],key)).fetchone()
            ids=({x["user_id"] for x in c.execute("SELECT user_id FROM group_members WHERE group_id=?",(g["id"],)).fetchall()} & members) if g else set()
            if not ids:
                x=c.execute("SELECT id FROM users WHERE username=?",(key,)).fetchone()
                ids={x["id"]} if x and x["id"] in wmembers and x["id"] in members else set()
        for x in ids:
            if x!=author and x not in out: out.append(x)
    return out

async def health(req): return web.json_response({"status":"ok","node_id":NODE})

async def get_user(req):
    auth(req); c=db(); r=c.execute("SELECT * FROM users WHERE id=?",(int(req.match_info["id"]),)).fetchone(); c.close()
    if not r: raise web.HTTPNotFound()
    return web.json_response({"user":userj(r)})

def validate_profile(d):
    allowed={"display_name":(1,80),"status_text":(0,140),"status_emoji":(0,64),"avatar_url":(0,1024)}
    for k,v in d.items():
        if k=="timezone":
            if not isinstance(v,str) or not v:
                raise web.HTTPBadRequest()
            try: ZoneInfo(v)
            except Exception: raise web.HTTPBadRequest()
        elif k in allowed:
            if not isinstance(v,str) or not (allowed[k][0] <= len(v) <= allowed[k][1]):
                raise web.HTTPBadRequest()
        else:
            raise web.HTTPBadRequest()

async def update_me(req):
    u=auth(req); d=await req.json(); validate_profile(d)
    c=db(); c.execute("BEGIN IMMEDIATE")
    fields=["display_name","timezone","avatar_url","status_text","status_emoji"]
    vals=[d[k] for k in fields if k in d]
    if vals:
        sets=",".join(k+"=?" for k in fields if k in d)
        c.execute("UPDATE users SET "+sets+" WHERE id=?",tuple(vals+[u["id"]]))
    r=c.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone()
    c.execute("INSERT INTO global_events(frame) VALUES(?)",(json.dumps({"type":"user.updated","user":userj(r)},separators=(",",":")),))
    c.commit(); c.close()
    return web.json_response({"user":userj(r)})

async def create_dm(req):
    u=auth(req); d=await req.json()
    if "user_id" not in d: raise web.HTTPBadRequest()
    other=int(d["user_id"]); c=db()
    if not c.execute("SELECT id FROM users WHERE id=?",(other,)).fetchone():
        c.close(); raise web.HTTPNotFound()
    if other==u["id"]:
        c.close(); raise web.HTTPBadRequest()
    a,b=sorted((u["id"],other))
    r=c.execute("SELECT cm1.channel_id FROM channel_members cm1 JOIN channel_members cm2 ON cm1.channel_id=cm2.channel_id JOIN channels ch ON ch.id=cm1.channel_id WHERE ch.is_dm=1 AND cm1.user_id=? AND cm2.user_id=?",(a,b)).fetchone()
    if r: cid=r["channel_id"]
    else:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)",(f"dm-{a}-{b}",1)); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,a)); c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,b)); c.commit()
    c.close(); return web.json_response({"channel_id":cid})
async def register(req):
    d=await req.json(); u=d.get("username",""); p=d.get("password","")
    if not re.fullmatch(r"[A-Za-z0-9_]+",u) or len(p)<8: raise web.HTTPBadRequest()
    c=db()
    try:
        c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(u,p,d.get("display_name") or u)); uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,uid)); c.commit()
    except sqlite3.IntegrityError: c.close(); raise web.HTTPConflict()
    r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); c.close(); return web.json_response({"user":userj(r),"token":t},status=201)
async def login(req):
    d=await req.json(); c=db(); r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(d.get("username"),d.get("password"))).fetchone()
    if not r: c.close(); raise web.HTTPUnauthorized()
    t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,r["id"])); c.commit(); c.close(); return web.json_response({"user":userj(r),"token":t})
async def me(req): return web.json_response({"user":userj(auth(req))})
async def workspaces(req):
    u=auth(req); c=db(); rs=c.execute("SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall(); c.close()
    return web.json_response({"workspaces":[{"id":r["id"],"slug":r["slug"],"name":r["name"],"owner_id":r["owner_id"],"join_mode":r["join_mode"]} for r in rs]})
async def create_ws(req):
    u=auth(req); d=await req.json(); slug=d.get("slug","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not d.get("name"): raise web.HTTPBadRequest()
    c=db()
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,d["name"],u["id"])); wid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO workspace_members VALUES(?,?,?)",(wid,u["id"],"owner")); c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); raise web.HTTPConflict()
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=getchan(c,cid); c.close()
    return web.json_response({"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":w["join_mode"]},"general_channel":chan(ch)},status=201)
async def ws_detail(req):
    u=auth(req); c=db(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(req.match_info["slug"],)).fetchone()
    if not w: c.close(); raise web.HTTPNotFound()
    rs=c.execute("SELECT c.* FROM channels c JOIN channel_members cm ON cm.channel_id=c.id WHERE c.workspace_id=? AND cm.user_id=? UNION SELECT c.* FROM channels c WHERE c.workspace_id=? AND c.is_private=0",(w["id"],u["id"],w["id"])).fetchall(); c.close()
    return web.json_response({"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":w["join_mode"]},"channels":[chan(x) for x in rs],"read_state":[]})
async def create_ch(req):
    u=auth(req); d=await req.json(); c=db(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(req.match_info["slug"],)).fetchone()
    if not w: c.close(); raise web.HTTPNotFound()
    if not re.fullmatch(r"[a-z0-9-]{1,32}",d.get("name","")) or len(d.get("topic",""))>250: c.close(); raise web.HTTPBadRequest()
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],d["name"],int(bool(d.get("is_private"))),d.get("topic",""))); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); raise web.HTTPConflict()
    x=chan(getchan(c,cid)); c.close(); return web.json_response({"channel":x},status=201)
async def join(req):
    u=auth(req); c=db(); ch=getchan(c,int(req.match_info["id"]))
    c.execute("INSERT OR IGNORE INTO workspace_members(workspace_id,user_id) VALUES(?,?)",(ch["workspace_id"],u["id"])); c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(ch["id"],u["id"])); c.commit(); c.close(); return web.json_response({"channel":chan(ch)})
async def members(req):
    u=auth(req); c=db(); ch=getchan(c,int(req.match_info["id"]))
    if not accessible(c,ch,u["id"]): c.close(); raise web.HTTPForbidden()
    rs=c.execute("SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?",(ch["id"],)).fetchall(); c.close(); return web.json_response({"members":[userj(x) for x in rs]})
async def messages(req):
    u=auth(req); c=db(); ch=getchan(c,int(req.match_info["id"]))
    if not accessible(c,ch,u["id"]): c.close(); raise web.HTTPForbidden()
    lim=int(req.query.get("limit","50"))
    rs=c.execute("SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?",(ch["id"],min(200,max(1,lim)))).fetchall(); out=[msg(c,x) for x in rs]; c.close(); return web.json_response({"messages":out,"next_cursor":None})

async def upload_file(req):
    u=auth(req)
    if not req.content_type.startswith("multipart/"): raise web.HTTPBadRequest()
    reader=await req.multipart(); part=None
    while True:
        x=await reader.next()
        if x is None: break
        if x.name=="file": part=x; break
    if part is None: raise web.HTTPBadRequest()
    data=bytearray()
    while True:
        chunk=await part.read_chunk()
        if not chunk: break
        data.extend(chunk)
        if len(data)>10485760: raise web.HTTPRequestEntityTooLarge(max_size=10485760, actual_size=len(data))
    os.makedirs("/app/data/files",exist_ok=True)
    c=db(); c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)",(u["id"],part.filename or "file",part.headers.get("Content-Type","application/octet-stream"),len(data),now(),""))
    fid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; path=f"/app/data/files/{fid}"
    with open(path,"wb") as f: f.write(data)
    c.execute("UPDATE files SET path=? WHERE id=?",(path,fid)); c.commit()
    r=c.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone(); c.close()
    return web.json_response({"file":filej(r)},status=201)

async def file_meta(req):
    auth(req); c=db(); r=c.execute("SELECT * FROM files WHERE id=?",(int(req.match_info["id"]),)).fetchone(); c.close()
    if not r: raise web.HTTPNotFound()
    return web.json_response({"file":filej(r)})

async def file_download(req):
    auth(req); c=db(); r=c.execute("SELECT * FROM files WHERE id=?",(int(req.match_info["id"]),)).fetchone(); c.close()
    if not r: raise web.HTTPNotFound()
    if not os.path.exists(r["path"]): raise web.HTTPNotFound()
    return web.Response(body=open(r["path"],"rb").read(),content_type=r["content_type"],headers={"Content-Disposition":f'attachment; filename="{r["filename"]}"'})

def owner(c,ch,uid):
    return bool(c.execute("SELECT 1 FROM workspaces WHERE id=? AND owner_id=?",(ch["workspace_id"],uid)).fetchone())

def slash(c,u,ch,body):
    if body.startswith("//"): return None
    parts=body.split(None,1); cmd=parts[0]
    arg=parts[1] if len(parts)>1 else ""
    if cmd=="/me":
        if not arg.strip(): raise web.HTTPBadRequest()
        return None
    if cmd=="/shrug":
        return ("MSG", (arg+" " if arg else "")+"¯\\_(ツ)_/¯")
    if cmd=="/topic":
        if not accessible(c,ch,u["id"]): raise web.HTTPForbidden()
        if len(arg)>250: raise web.HTTPBadRequest()
        c.execute("BEGIN IMMEDIATE"); c.execute("UPDATE channels SET topic=? WHERE id=?",(arg,ch["id"]))
        x=getchan(c,ch["id"]); event(c,ch["id"],"channel.updated",channel=chan(x)); c.commit()
        return ("RESP",{"message":None,"channel":chan(x)})
    if cmd=="/invite":
        if not accessible(c,ch,u["id"]): raise web.HTTPForbidden()
        if not re.fullmatch(r"@[A-Za-z0-9_]+",arg.strip()): raise web.HTTPNotFound()
        target=c.execute("SELECT * FROM users WHERE username=?",(arg.strip()[1:],)).fetchone()
        if not target: raise web.HTTPNotFound()
        c.execute("BEGIN IMMEDIATE"); c.execute("INSERT OR IGNORE INTO workspace_members(workspace_id,user_id) VALUES(?,?)",(ch["workspace_id"],target["id"])); c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(ch["id"],target["id"])); event(c,ch["id"],"member.joined",user_id=target["id"]); c.commit()
        return ("RESP",{"message":None,"invited_user":userj(target)})
    if cmd in ("/archive","/unarchive"):
        if not owner(c,ch,u["id"]): raise web.HTTPForbidden()
        val=cmd=="/archive"; typ="channel.archived" if val else "channel.unarchived"
        c.execute("BEGIN IMMEDIATE"); c.execute("UPDATE channels SET is_archived=? WHERE id=?",(int(val),ch["id"]))
        x=getchan(c,ch["id"]); event(c,ch["id"],typ,channel=chan(x)); c.commit()
        return ("RESP",{"message":None,"channel":chan(x)})
    raise web.HTTPBadRequest()

async def postmsg(req):
    u=auth(req); d=await req.json(); c=db(); ch=getchan(c,int(req.match_info["id"]))
    if not accessible(c,ch,u["id"]): c.close(); raise web.HTTPForbidden()
    body=d.get("body","")
    if not isinstance(body,str) or not body.strip(): c.close(); raise web.HTTPBadRequest()
    special=None
    if body.startswith("/") and not body.startswith("//"):
        special=slash(c,u,ch,body)
        if special and special[0]=="RESP":
            c.close(); return web.json_response(special[1],status=201)
        if special and special[0]=="MSG": body=special[1]
        if body.startswith("/me") and not body.strip()[3:]: c.close(); raise web.HTTPBadRequest()
    if ch["is_archived"]: c.close(); raise HTTP423()
    parent=d.get("parent_id")
    if parent and not c.execute("SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0",(parent,ch["id"])).fetchone(): c.close(); raise web.HTTPNotFound()
    file_ids=d.get("file_ids",[])
    if not isinstance(file_ids,list): c.close(); raise web.HTTPBadRequest()
    for fid in file_ids:
        fr=c.execute("SELECT * FROM files WHERE id=? AND uploader_id=?",(fid,u["id"])).fetchone()
        if not fr or c.execute("SELECT 1 FROM attachments WHERE file_id=?",(fid,)).fetchone(): c.close(); raise web.HTTPBadRequest()
    c.execute("BEGIN IMMEDIATE"); mentions=resolve_mentions(c,body,ch,u["id"]); c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)",(ch["id"],u["id"],body,parent,now(),json.dumps(mentions,separators=(",",":")))); mid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for fid in file_ids: c.execute("INSERT INTO attachments VALUES(?,?)",(mid,fid))
    m=msg(c,c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()); event(c,ch["id"],"message.reply" if parent else "message.created",message=m); c.commit(); c.close(); return web.json_response({"message":m},status=201)
async def replies(req):
    u=auth(req); c=db(); r=c.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(int(req.match_info["id"]),)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    ch=getchan(c,r["channel_id"])
    if not accessible(c,ch,u["id"]): c.close(); raise web.HTTPForbidden()
    out=[msg(c,x) for x in c.execute("SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id",(r["id"],)).fetchall()]; c.close(); return web.json_response({"replies":out,"next_cursor":None})
async def react(req):
    u=auth(req); c=db(); r=c.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(int(req.match_info["id"]),)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    ch=getchan(c,r["channel_id"])
    if not accessible(c,ch,u["id"]): c.close(); raise web.HTTPForbidden()
    d=await req.json(); e=d.get("emoji","")
    if not isinstance(e,str) or not e or len(e)>64: c.close(); raise web.HTTPBadRequest()
    if not c.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(r["id"],u["id"],e)).fetchone():
        c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO reactions VALUES(?,?,?,?)",(r["id"],u["id"],e,now())); event(c,ch["id"],"reaction.added",message_id=r["id"],emoji=e,user_id=u["id"]); c.commit()
    out=reactions(c,r["id"]); c.close(); return web.json_response({"reactions":out})
async def unreact(req):
    u=auth(req); c=db(); r=c.execute("SELECT * FROM messages WHERE id=? AND deleted=0",(int(req.match_info["id"]),)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    ch=getchan(c,r["channel_id"])
    if not accessible(c,ch,u["id"]): c.close(); raise web.HTTPForbidden()
    e=req.match_info["emoji"]
    if c.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(r["id"],u["id"],e)).fetchone():
        c.execute("BEGIN IMMEDIATE"); c.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(r["id"],u["id"],e)); event(c,ch["id"],"reaction.removed",message_id=r["id"],emoji=e,user_id=u["id"]); c.commit()
    out=reactions(c,r["id"]); c.close(); return web.json_response({"reactions":out})
async def websocket(req):
    try: u=auth(req)
    except web.HTTPException: raise
    ws=web.WebSocketResponse(); await ws.prepare(req); ws.channels=set(); ws.seen={}; ws.sid=secrets.token_hex(8); sockets.add(ws); pc=db(); pc.execute("INSERT OR REPLACE INTO presence VALUES(?,?,?)",(u["id"],NODE,ws.sid)); pc.commit(); pc.close()
    try:
        async for x in ws:
            if x.type!=WSMsgType.TEXT: continue
            d=json.loads(x.data); cid=int(d.get("channel_id")); c=db(); ch=getchan(c,cid)
            if not accessible(c,ch,u["id"]): c.close(); continue
            if d.get("type")=="subscribe":
                ws.channels.add(cid); head=c.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?",(cid,)).fetchone()[0]; ws.seen[cid]=head; await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":head})
            elif d.get("type")=="resume":
                for r in c.execute("SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,int(d.get("since_event_id",0)))).fetchall(): await ws.send_str(r["frame"])
                head=c.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?",(cid,)).fetchone()[0]; ws.channels.add(cid); ws.seen[cid]=head; await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":head})
            c.close()
    finally:
        sockets.discard(ws)
        pc=db(); pc.execute("DELETE FROM presence WHERE user_id=? AND node_id=? AND ws_id=?",(u["id"],NODE,ws.sid)); pc.commit(); pc.close()
    return ws
async def start_pump(app):
    app["pump"] = asyncio.create_task(pump(app))

def app():
    init(); a=web.Application(); a["global_last"]=0; a.router.add_get("/api/health",health); a.router.add_post("/api/auth/register",register); a.router.add_post("/api/auth/login",login); a.router.add_get("/api/auth/me",me); a.router.add_post("/api/files",upload_file); a.router.add_get("/api/files/{id}",file_meta); a.router.add_get("/api/files/{id}/download",file_download); a.router.add_get("/api/users/{id}",get_user); a.router.add_patch("/api/users/me",update_me); a.router.add_post("/api/dms",create_dm); a.router.add_get("/api/workspaces",workspaces); a.router.add_post("/api/workspaces",create_ws); a.router.add_get("/api/workspaces/{slug}",ws_detail); a.router.add_get("/api/workspaces/{slug}/groups",list_groups); a.router.add_post("/api/workspaces/{slug}/groups",create_group); a.router.add_patch("/api/workspaces/{slug}/groups/{handle}",edit_group); a.router.add_delete("/api/workspaces/{slug}/groups/{handle}",delete_group); a.router.add_post("/api/workspaces/{slug}/channels",create_ch); a.router.add_post("/api/channels/{id}/join",join); a.router.add_get("/api/channels/{id}/members",members); a.router.add_get("/api/channels/{id}/messages",messages); a.router.add_post("/api/channels/{id}/messages",postmsg); a.router.add_get("/api/messages/{id}/replies",replies); a.router.add_post("/api/messages/{id}/reactions",react); a.router.add_delete("/api/messages/{id}/reactions/{emoji}",unreact); a.router.add_get("/api/ws",websocket); a.on_startup.append(start_pump); return a
if __name__=="__main__":
    web.run_app(app(),host="127.0.0.1",port=int(sys.argv[3]))

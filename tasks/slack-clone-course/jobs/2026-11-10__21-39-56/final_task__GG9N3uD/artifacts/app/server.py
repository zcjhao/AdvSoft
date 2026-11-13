import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
sockets=[]

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    os.makedirs('/app/data',exist_ok=True)
    c=sqlite3.connect(DB, timeout=5, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS wmembers(workspace_id INTEGER,user_id INTEGER,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    ''')
    c.commit(); c.close()
def user_obj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def chan_obj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msg_obj(c,r):
    u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
def auth(req):
    h=req.headers.get('Authorization','')
    tok=h[7:] if h.startswith('Bearer ') else req.query.get('token','')
    if not tok: return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close()
    return r
def jsonerr(text,status):
    return web.json_response({'error':text},status=status)
async def body(req):
    try: return await req.json()
    except: return {}
def require(req):
    u=auth(req)
    if not u: raise web.HTTPUnauthorized()
    return u
async def redis_notify(cid,eid):
    try:
        import redis
        x=redis.Redis(host='127.0.0.1',port=6379,socket_timeout=.1)
        x.publish('events',f'{cid}:{eid}')
    except Exception: pass
def append_event(c,cid,typ,**fields):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    frame={'type':typ,'event_id':n,'channel_id':cid,**fields}
    c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(frame)))
    return frame
async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
    d=await body(req); un=d.get('username',''); pw=d.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: return jsonerr('invalid registration',400)
    c=db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un,'UTC','','',''))
        uid=cur.lastrowid; tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
        u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return web.json_response({'user':user_obj(u),'token':tok},status=201)
    except sqlite3.IntegrityError: c.rollback(); return jsonerr('duplicate username',409)
    finally: c.close()
async def login(req):
    d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest())).fetchone()
    if not r: c.close(); return jsonerr('invalid credentials',401)
    tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id'])); c.commit(); c.close()
    return web.json_response({'user':user_obj(r),'token':tok})
async def me(req): return web.json_response({'user':user_obj(require(req))})
async def workspaces(req):
    u=require(req); c=db(); rs=c.execute('SELECT DISTINCT w.* FROM workspaces w JOIN wmembers m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
    return web.json_response({'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rs]})
async def create_ws(req):
    u=require(req); d=await body(req); slug=d.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'): return jsonerr('invalid workspace',400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(slug,d['name'],u['id'],'open')); wid=cur.lastrowid
        ch=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')).lastrowid
        c.execute('INSERT INTO wmembers VALUES(?,?)',(wid,u['id'])); c.execute('INSERT INTO members VALUES(?,?)',(ch,u['id'])); c.commit()
        r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone(); return web.json_response({'workspace':{'id':wid,'slug':slug,'name':d['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':chan_obj(r)},status=201)
    except sqlite3.IntegrityError: c.rollback(); return jsonerr('duplicate workspace',409)
    finally: c.close()
async def get_ws(req):
    u=require(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w: c.close(); raise web.HTTPNotFound()
    rs=c.execute('SELECT c.* FROM channels c JOIN members m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?',(w['id'],u['id'])).fetchall(); c.close()
    return web.json_response({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[chan_obj(x) for x in rs],'read_state':[]})
async def create_channel(req):
    u=require(req); d=await body(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w: c.close(); raise web.HTTPNotFound()
    if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')): c.close(); return jsonerr('invalid channel',400)
    try:
        cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic',''))); cid=cur.lastrowid; c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id'])); c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); return web.json_response({'channel':chan_obj(r)},status=201)
    except sqlite3.IntegrityError: return jsonerr('duplicate channel',409)
    finally: c.close()
async def channel(req):
    u=require(req); c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    c.execute('INSERT OR IGNORE INTO wmembers SELECT workspace_id,? FROM channels WHERE id=?',(u['id'],r['id'])); c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(r['id'],u['id'])); c.commit(); c.close(); return web.json_response({'channel':chan_obj(r)})
async def members(req):
    require(req); c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    us=c.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(r['id'],)).fetchall(); c.close(); return web.json_response({'members':[user_obj(x) for x in us]})
async def messages(req):
    u=require(req); d=await body(req); c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    if r['is_archived']: c.close(); return jsonerr('archived',423)
    if not d.get('body','').strip(): c.close(); return jsonerr('empty body',400)
    if not c.execute('SELECT 1 FROM members WHERE channel_id=? AND user_id=?',(r['id'],u['id'])).fetchone(): c.close(); return jsonerr('not a member',403)
    try:
        c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(r['id'],u['id'],d['body'],d.get('parent_id'),now())); mid=cur.lastrowid; mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); mo=msg_obj(c,mr); fr=append_event(c,r['id'],'message.reply' if d.get('parent_id') else 'message.created',message=mo); c.commit()
    finally: c.close()
    asyncio.create_task(redis_notify(r['id'],fr['event_id'])); return web.json_response({'message':mo},status=201)
async def list_messages(req):
    require(req); c=db(); lim=min(int(req.query.get('limit','50')),200); r=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(req.match_info['id'],lim)).fetchall(); out=[msg_obj(c,x) for x in r]; c.close(); return web.json_response({'messages':out,'next_cursor':None})
async def replies(req):
    require(req); c=db(); lim=min(int(req.query.get('limit','50')),200); r=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?',(req.match_info['id'],lim)).fetchall(); out=[msg_obj(c,x) for x in r]; c.close(); return web.json_response({'replies':out,'next_cursor':None})
async def ws_handler(req):
    if not auth(req): raise web.HTTPUnauthorized()
    ws=web.WebSocketResponse(); await ws.prepare(req); state={'ws':ws,'channels':set()}; sockets.append(state)
    try:
        async for m in ws:
            if m.type!=WSMsgType.TEXT: continue
            d=json.loads(m.data); cid=int(d.get('channel_id',0)); c=db(); head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
            if d.get('type')=='subscribe': state['channels'].add(cid); await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif d.get('type')=='resume':
                since=int(d.get('since_event_id',0)); rows=c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
                for x in rows: await ws.send_str(x['frame'])
                await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            c.close()
    finally: 
        if state in sockets: sockets.remove(state)
    return ws
async def pump():
    seen={}
    while True:
        try:
            c=db(); rows=c.execute('SELECT * FROM events ORDER BY channel_id,event_id').fetchall(); c.close()
            for r in rows:
                key=(r['channel_id'],r['event_id'])
                if key in seen: continue
                seen[key]=1; frame=json.loads(r['frame'])
                for s in list(sockets):
                    if r['channel_id'] in s['channels']:
                        try: await s['ws'].send_json(frame)
                        except: pass
        except Exception: pass
        await asyncio.sleep(.05)
async def on_start(app): app['pump']=asyncio.create_task(pump())
async def on_cleanup(app): app['pump'].cancel()
def app():
    init(); a=web.Application(); a.on_startup.append(on_start); a.on_cleanup.append(on_cleanup)
    a.router.add_get('/api/health',health); a.router.add_post('/api/auth/register',register); a.router.add_post('/api/auth/login',login); a.router.add_get('/api/auth/me',me)
    a.router.add_get('/api/workspaces',workspaces); a.router.add_post('/api/workspaces',create_ws); a.router.add_get('/api/workspaces/{slug}',get_ws); a.router.add_post('/api/workspaces/{slug}/channels',create_channel)
    a.router.add_post('/api/channels/{id}/join',channel); a.router.add_get('/api/channels/{id}/members',members); a.router.add_post('/api/channels/{id}/messages',messages); a.router.add_get('/api/channels/{id}/messages',list_messages)
    a.router.add_get('/api/messages/{id}/replies',replies); a.router.add_get('/api/ws',ws_handler)
    return a
if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='node': web.run_app(app(),host='127.0.0.1',port=PORT)

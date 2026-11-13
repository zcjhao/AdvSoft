import asyncio, json, os, re, secrets, sqlite3, sys, hashlib
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
os.makedirs('/app/data',exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    x=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    x.row_factory=sqlite3.Row
    x.execute('PRAGMA busy_timeout=5000'); x.execute('PRAGMA synchronous=NORMAL')
    return x
def init():
    x=db()
    x.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS members(user_id INTEGER,channel_id INTEGER,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    '''); x.commit(); x.close()
init()

def user_obj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chan_obj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def msg_obj(x,r):
    a=x.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=x.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    x=db(); r=x.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); x.close()
    return r
def bad(status,text='error'): return web.json_response({'error':text},status=status)
async def body(req):
    try:return await req.json()
    except:return {}

def append(x,cid,typ,**fields):
    n=x.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    frame={'type':typ,'event_id':n,'channel_id':cid}; frame.update(fields)
    x.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(frame,separators=(',',':'))))
    return frame

async def redis_notify(cid,eid):
    try:
        r=asyncio.open_connection('127.0.0.1',6379)
        rd,wr=await asyncio.wait_for(r,0.15)
        wr.write(('PUBLISH events %s %s\r\n'%(cid,eid)).encode()); await wr.drain(); wr.close()
    except: pass

async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
    b=await body(req); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return bad(400)
    x=db()
    try:
        x.execute('BEGIN IMMEDIATE')
        x.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un))
        uid=x.execute('SELECT last_insert_rowid() id').fetchone()['id']; tok=secrets.token_urlsafe(24)
        x.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); x.commit()
    except sqlite3.IntegrityError: x.rollback(); x.close(); return bad(409)
    u=x.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); x.close()
    return web.json_response({'user':user_obj(u),'token':tok},status=201)
async def login(req):
    b=await body(req); x=db(); u=x.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
    if not u:x.close();return bad(401)
    tok=secrets.token_urlsafe(24); x.execute('INSERT INTO tokens VALUES(?,?)',(tok,u['id']));x.commit();x.close()
    return web.json_response({'user':user_obj(u),'token':tok})
async def me(req):
    u=auth(req)
    return web.json_response({'user':user_obj(u)}) if u else bad(401)
async def create_ws(req):
    u=auth(req)
    if not u:return bad(401)
    b=await body(req); slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'):return bad(400)
    x=db()
    try:
        x.execute('BEGIN IMMEDIATE'); x.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],u['id']))
        wid=x.execute('SELECT last_insert_rowid() id').fetchone()['id']; x.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'))
        cid=x.execute('SELECT last_insert_rowid() id').fetchone()['id'];x.execute('INSERT INTO members VALUES(?,?)',(u['id'],cid));x.commit()
    except sqlite3.IntegrityError:x.rollback();x.close();return bad(409)
    w=x.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); c=x.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();x.close()
    return web.json_response({'workspace':dict(id=w['id'],slug=w['slug'],name=w['name'],owner_id=w['owner_id'],join_mode=w['join_mode']),'general_channel':chan_obj(c)},status=201)
async def workspaces(req):
    u=auth(req)
    if not u:return bad(401)
    x=db(); rows=x.execute('SELECT DISTINCT w.* FROM workspaces w JOIN channels c ON c.workspace_id=w.id JOIN members m ON m.channel_id=c.id WHERE m.user_id=? OR w.owner_id=?',(u['id'],u['id'])).fetchall();x.close()
    return web.json_response({'workspaces':[dict(id=r['id'],slug=r['slug'],name=r['name'],owner_id=r['owner_id'],join_mode=r['join_mode']) for r in rows]})
async def workspace(req):
    u=auth(req)
    if not u:return bad(401)
    x=db();w=x.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:x.close();return bad(404)
    cs=x.execute('SELECT c.* FROM channels c JOIN members m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?',(w['id'],u['id'])).fetchall();x.close()
    return web.json_response({'workspace':dict(id=w['id'],slug=w['slug'],name=w['name'],owner_id=w['owner_id'],join_mode=w['join_mode']),'channels':[chan_obj(c) for c in cs],'read_state':[]})
async def create_channel(req):
    u=auth(req)
    if not u:return bad(401)
    b=await body(req);x=db();w=x.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:x.close();return bad(404)
    name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name):x.close();return bad(400)
    try:
        x.execute('BEGIN IMMEDIATE');x.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')));cid=x.execute('SELECT last_insert_rowid() id').fetchone()['id'];x.execute('INSERT INTO members VALUES(?,?)',(u['id'],cid));x.commit()
    except sqlite3.IntegrityError:x.rollback();x.close();return bad(409)
    c=x.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();x.close();return web.json_response({'channel':chan_obj(c)},status=201)
async def get_channel(req):
    u=auth(req)
    if not u:return bad(401)
    x=db();c=x.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not c:x.close();return bad(404)
    x.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(u['id'],c['id']));x.commit();x.close();return web.json_response({'channel':chan_obj(c)})
async def members(req):
    u=auth(req)
    if not u:return bad(401)
    x=db();c=x.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not c:x.close();return bad(404)
    rows=x.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(c['id'],)).fetchall();x.close();return web.json_response({'members':[user_obj(r) for r in rows]})
async def post_message(req):
    u=auth(req)
    if not u:return bad(401)
    b=await body(req);x=db();c=x.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not c:x.close();return bad(404)
    if c['is_private'] and not x.execute('SELECT 1 FROM members WHERE user_id=? AND channel_id=?',(u['id'],c['id'])).fetchone():x.close();return bad(403)
    if c['is_archived']:x.close();return bad(423)
    text=b.get('body','')
    if not isinstance(text,str) or not text.strip():x.close();return bad(400)
    parent=b.get('parent_id'); 
    if parent and not x.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,c['id'])).fetchone():x.close();return bad(404)
    x.execute('BEGIN IMMEDIATE'); ts=now();x.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(c['id'],u['id'],text,parent,ts));mid=x.execute('SELECT last_insert_rowid() id').fetchone()['id'];r=x.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();m=msg_obj(x,r);fr=append(x,c['id'],'message.reply' if parent else 'message.created',message=m);x.commit();x.close();asyncio.create_task(redis_notify(c['id'],fr['event_id']));return web.json_response({'message':m},status=201)
async def list_messages(req):
    u=auth(req)
    if not u:return bad(401)
    x=db();cid=int(req.match_info['id']); lim=int(req.query.get('limit','50'));lim=max(1,min(200,lim));rows=x.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(cid,lim+1)).fetchall();out=[msg_obj(x,r) for r in rows[:lim]];x.close();return web.json_response({'messages':out,'next_cursor':None})
async def replies(req):
    u=auth(req)
    if not u:return bad(401)
    x=db();rows=x.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(req.match_info['id'],)).fetchall();out=[msg_obj(x,r) for r in rows];x.close();return web.json_response({'replies':out,'next_cursor':None})

class AppState:
    def __init__(self):self.socks=[];self.last=0
state=AppState()
async def pump(app):
    while True:
        try:
            x=db(); rows=x.execute('SELECT rowid,frame FROM events WHERE rowid>? ORDER BY rowid',(state.last,)).fetchall();x.close()
            for r in rows:
                state.last=r['rowid']; f=json.loads(r['frame'])
                for ws,cids in list(state.socks):
                    if f['channel_id'] in cids and f['event_id'] > cids[f['channel_id']]:
                        cids[f['channel_id']]=f['event_id']
                        try:await ws.send_json(f)
                        except:pass
        except Exception: pass
        await asyncio.sleep(.05)
async def ws_handler(req):
    tok=req.query.get('token',''); x=db();u=x.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone();x.close()
    if not u:return bad(401)
    ws=web.WebSocketResponse();await ws.prepare(req);cids={};state.socks.append((ws,cids))
    try:
        async for q in ws:
            if q.type!=WSMsgType.TEXT:continue
            try:b=json.loads(q.data)
            except:continue
            cid=int(b.get('channel_id',0));x=db();head=x.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
            if b.get('type')=='subscribe':
                cids[cid]=head;await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif b.get('type')=='resume':
                rows=x.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(b.get('since_event_id',0)))).fetchall()
                for r in rows:
                    await ws.send_str(r['frame'])
                cids[cid]=head
                head=x.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            x.close()
    finally:state.socks[:] = [(a,b) for a,b in state.socks if a is not ws]
    return ws
def app():
    a=web.Application();a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me)
    a.router.add_post('/api/workspaces',create_ws);a.router.add_get('/api/workspaces',workspaces);a.router.add_get('/api/workspaces/{slug}',workspace);a.router.add_post('/api/workspaces/{slug}/channels',create_channel)
    a.router.add_post('/api/channels/{id}/join',get_channel);a.router.add_get('/api/channels/{id}/members',members);a.router.add_post('/api/channels/{id}/messages',post_message);a.router.add_get('/api/channels/{id}/messages',list_messages);a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_get('/api/ws',ws_handler)
    async def start_pump(a):
        a['pump']=asyncio.create_task(pump(a))
    a.on_startup.append(start_pump);return a
web.run_app(app(),host='127.0.0.1',port=PORT)

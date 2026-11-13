import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web

DB='/app/data/app.db'
def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    os.makedirs('/app/data',exist_ok=True)
    c=sqlite3.connect(DB,timeout=5,isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS wmembers(workspace_id INTEGER,user_id INTEGER,UNIQUE(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    '''); c.close()
def userobj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chan(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msg(c,r):
    u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':None,'files':[],'reactions':[],'mentions':[]}
def token_user(req):
    h=req.headers.get('Authorization','')
    tok=h[7:] if h.startswith('Bearer ') else req.query.get('token','')
    if not tok: return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close(); return r
def err(status,text='error'): return web.json_response({'error':text},status=status)
async def health(req): return web.json_response({'status':'ok','node_id':req.app['node']})
async def register(req):
    try: x=await req.json()
    except: return err(400)
    un=x.get('username',''); pw=x.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err(400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),x.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); return err(409)
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return web.json_response({'user':userobj(r),'token':t},status=201)
async def login(req):
    try:x=await req.json()
    except:return err(400)
    c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),hashlib.sha256(x.get('password','').encode()).hexdigest())).fetchone()
    if not r:c.close();return err(401)
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.close();return web.json_response({'user':userobj(r),'token':t})
async def me(req):
    r=token_user(req); return web.json_response({'user':userobj(r)}) if r else err(401)
async def workspaces(req):
    u=token_user(req)
    if not u:return err(401)
    c=db(); rs=c.execute('SELECT DISTINCT w.* FROM workspaces w JOIN wmembers m ON m.workspace_id=w.id WHERE m.user_id=? OR w.owner_id=?',(u['id'],u['id'])).fetchall();c.close()
    return web.json_response({'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rs]})
async def create_ws(req):
    u=token_user(req)
    if not u:return err(401)
    try:x=await req.json()
    except:return err(400)
    if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return err(400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(x['slug'],x.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wmembers VALUES(?,?)',(wid,u['id']));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return err(409)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close()
    return web.json_response({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'general_channel':chan(ch)},status=201)
async def ws_detail(req):
    u=token_user(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err(404)
    rs=c.execute('SELECT c.* FROM channels c JOIN members m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?',(w['id'],u['id'])).fetchall();c.close()
    return web.json_response({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[chan(r) for r in rs],'read_state':[]})
async def create_channel(req):
    u=token_user(req)
    if not u:return err(401)
    try:x=await req.json()
    except:return err(400)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err(404)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')):c.close();return err(400)
    try:c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],x['name'],int(bool(x.get('is_private'))),x.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return err(409)
    r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return web.json_response({'channel':chan(r)},status=201)
def getchan(c,cid):return c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
async def join(req):
    u=token_user(req)
    if not u:return err(401)
    c=db();r=getchan(c,req.match_info['id'])
    if not r:c.close();return err(404)
    c.execute('INSERT OR IGNORE INTO wmembers SELECT workspace_id,? FROM channels WHERE id=?',(u['id'],r['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(r['id'],u['id']));c.close();return web.json_response({'channel':chan(r)})
async def members(req):
    u=token_user(req)
    if not u:return err(401)
    c=db();r=getchan(c,req.match_info['id'])
    if not r:c.close();return err(404)
    rows=c.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(r['id'],)).fetchall();c.close();return web.json_response({'members':[userobj(x) for x in rows]})
def allowed(c,r,uid):
    return c.execute('SELECT 1 FROM members WHERE channel_id=? AND user_id=?',(r['id'],uid)).fetchone() is not None
def add_event(c,cid,typ,**kw):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; frame={'type':typ,'event_id':n,'channel_id':cid,**kw};c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(frame)));return frame
async def post(req):
    u=token_user(req)
    if not u:return err(401)
    try:x=await req.json()
    except:return err(400)
    body=x.get('body','')
    if not isinstance(body,str) or not body.strip():return err(400)
    c=db();r=getchan(c,req.match_info['id'])
    if not r:c.close();return err(404)
    if not allowed(c,r,u['id']) and r['is_private']:c.close();return err(403)
    if r['is_archived']:c.close();return err(423)
    parent=x.get('parent_id')
    if parent and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,r['id'])).fetchone():c.close();return err(404)
    c.execute('BEGIN IMMEDIATE');t=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(r['id'],u['id'],body,parent,t));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();mo=msg(c,mr);add_event(c,r['id'],'message.reply' if parent else 'message.created',message=mo);c.commit();c.close();return web.json_response({'message':mo},status=201)
async def list_messages(req):
    u=token_user(req)
    if not u:return err(401)
    c=db();r=getchan(c,req.match_info['id'])
    if not r:c.close();return err(404)
    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT 200',(r['id'],)).fetchall();out=[msg(c,x) for x in rows];c.close();return web.json_response({'messages':out,'next_cursor':None})
async def replies(req):
    u=token_user(req)
    if not u:return err(401)
    c=db();p=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not p:c.close();return err(404)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(p['id'],)).fetchall();out=[msg(c,x) for x in rows];c.close();return web.json_response({'replies':out,'next_cursor':None})
async def ws_handler(req):
    if not token_user(req):return err(401)
    w=web.WebSocketResponse();await w.prepare(req); req.app['sockets'].append((w,set()))
    try:
        async for m in w:
            if m.type!=web.WSMsgType.TEXT:continue
            x=json.loads(m.data);cid=int(x.get('channel_id',0));c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
            if x.get('type')=='subscribe': req.app['sockets'][-1][1].add(cid);await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif x.get('type')=='resume':
                rows=c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(x.get('since_event_id',0)))).fetchall()
                for z in rows:await w.send_str(z['frame'])
                await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            c.close()
    finally:req.app['sockets'][:]=[(a,b) for a,b in req.app['sockets'] if a is not w]
    return w
async def pump(app):
    seen={}
    while True:
        try:
            c=db();rows=c.execute('SELECT channel_id,event_id,frame FROM events ORDER BY rowid').fetchall();c.close()
            for r in rows:
                if r['event_id']<=seen.get(r['channel_id'],0):continue
                seen[r['channel_id']]=r['event_id'];x=json.loads(r['frame'])
                for w,subs in list(app['sockets']):
                    if r['channel_id'] in subs and not w.closed:
                        try:await w.send_str(r['frame'])
                        except:pass
        except Exception:pass
        await asyncio.sleep(.05)
async def startup(app):app['sockets']=[];app['pump']=asyncio.create_task(pump(app))
async def cleanup(app):app['pump'].cancel()
def make(node):
    app=web.Application();app['node']=node
    app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
    app.router.add_get('/api/workspaces',workspaces);app.router.add_post('/api/workspaces',create_ws);app.router.add_get('/api/workspaces/{slug}',ws_detail);app.router.add_post('/api/workspaces/{slug}/channels',create_channel)
    app.router.add_post('/api/channels/{id}/join',join);app.router.add_get('/api/channels/{id}/members',members);app.router.add_post('/api/channels/{id}/messages',post);app.router.add_get('/api/channels/{id}/messages',list_messages);app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_get('/api/ws',ws_handler)
    app.on_startup.append(startup);app.on_cleanup.append(cleanup);return app
if __name__=='__main__':
    init()
    if len(sys.argv)>=4 and sys.argv[1]=='node':web.run_app(make(int(sys.argv[2])),host='127.0.0.1',port=int(sys.argv[3]))

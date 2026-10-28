import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
PORT=int(sys.argv[2]) if len(sys.argv)>2 else 8000
os.makedirs('/app/data',exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=15)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=15000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0,invited_user_id INTEGER,invited_username TEXT);
    '''); c.commit(); c.close()
init()

def userobj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def channelobj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def wsobj(r):
    return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def jerr(msg,status):
    return web.json_response({'error':msg},status=status)
async def body(req):
    try:return await req.json()
    except:return {}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '):
        q=req.query.get('token','')
        h='Bearer '+q if q else ''
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
    return r
def member(c,w,u):
    return c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone()
def require(req):
    u=auth(req)
    return u
async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
    d=await body(req); un=d.get('username',''); pw=d.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return jerr('invalid registration',400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,d.get('display_name') or un)); uid=cur.lastrowid
        tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
    except sqlite3.IntegrityError:c.rollback(); c.close(); return jerr('duplicate',409)
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
    return web.json_response({'user':userobj(r),'token':tok},status=201)
async def login(req):
    d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
    if not r:c.close(); return jerr('invalid credentials',401)
    tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id'])); c.commit(); c.close()
    return web.json_response({'user':userobj(r),'token':tok})
async def me(req):
    u=require(req)
    return jerr('unauthorized',401) if u is None else web.json_response({'user':userobj(u)})
async def create_ws(req):
    u=require(req)
    if not u:return jerr('unauthorized',401)
    d=await body(req); slug=d.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return jerr('invalid slug',400)
    if not d.get('name'):return jerr('invalid name',400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); w=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d['name'],u['id'])).lastrowid
        c.execute('INSERT INTO members VALUES(?,?,?)',(w,u['id'],'owner')); g=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general')).lastrowid;c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return jerr('duplicate',409)
    wr=c.execute('SELECT * FROM workspaces WHERE id=?',(w,)).fetchone(); cr=c.execute('SELECT * FROM channels WHERE id=?',(g,)).fetchone();c.close()
    return web.json_response({'workspace':wsobj(wr),'general_channel':channelobj(cr)},status=201)
async def list_ws(req):
    u=require(req)
    if not u:return jerr('unauthorized',401)
    c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close()
    return web.json_response({'workspaces':[wsobj(x) for x in rows]})
async def get_ws(req):
    u=require(req); slug=req.match_info['slug']
    if not u:return jerr('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    m=member(c,w['id'],u['id'])
    if not m:c.close();return jerr('forbidden',403)
    rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],req.query.get('include_archived') in ('1','true'))).fetchall();c.close()
    return web.json_response({'workspace':wsobj(w),'channels':[channelobj(x) for x in rows],'read_state':{}})
async def members(req):
    u=require(req);slug=req.match_info['slug']
    if not u:return jerr('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    if not member(c,w['id'],u['id']):c.close();return jerr('forbidden',403)
    rows=c.execute('SELECT m.user_id,m.role,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall();c.close()
    return web.json_response({'members':[{'user_id':x['user_id'],'role':x['role'],'username':x['username'],'display_name':x['display_name']} for x in rows]})
async def patch_ws(req):
    u=require(req);slug=req.match_info['slug']
    if not u:return jerr('unauthorized',401)
    d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    m=member(c,w['id'],u['id'])
    if not m or m['role'] not in ('owner','admin'):c.close();return jerr('forbidden',403)
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):c.close();return jerr('invalid join_mode',400)
    c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close()
    return web.json_response({'workspace':wsobj(w)})
async def patch_member(req):
    u=require(req);slug=req.match_info['slug']; target=int(req.match_info['user_id'])
    if not u:return jerr('unauthorized',401)
    d=await body(req); role=d.get('role')
    if role=='owner':return jerr('invalid role',400)
    if role not in ('admin','member','guest'):return jerr('invalid role',400)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    me=member(c,w['id'],u['id']); tm=member(c,w['id'],target)
    if not tm:c.close();return jerr('not member',404)
    if role=='owner' or target==w['owner_id'] or target==u['id'] or not me or me['role'] not in ('owner','admin') or (me['role']=='admin' and tm['role']=='admin'):c.close();return jerr('forbidden',403)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target));c.commit();c.close();return web.json_response({'member':{'user_id':target,'role':role}})
async def transfer(req):
    u=require(req);slug=req.match_info['slug']
    if not u:return jerr('unauthorized',401)
    d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    if u['id']!=w['owner_id']:c.close();return jerr('forbidden',403)
    t=member(c,w['id'],d.get('new_owner_id'))
    if not t or t['role']!='admin':c.close();return jerr('target must be admin',400)
    c.execute('UPDATE members SET role=CASE user_id WHEN ? THEN "owner" WHEN ? THEN "admin" ELSE role END WHERE workspace_id=?',(d['new_owner_id'],u['id'],w['id']));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(d['new_owner_id'],w['id']));c.commit();c.close();return web.json_response({'ok':True})
async def create_channel(req):
    u=require(req);slug=req.match_info['slug']
    if not u:return jerr('unauthorized',401)
    d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    m=member(c,w['id'],u['id'])
    if not m:c.close();return jerr('forbidden',403)
    private=bool(d.get('is_private',False))
    if m['role']=='guest' or (private and m['role'] not in ('owner','admin')):c.close();return jerr('forbidden',403)
    name=d.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name):c.close();return jerr('invalid name',400)
    try:
        c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),d.get('topic','')))
        c.commit();r=c.execute('SELECT * FROM channels WHERE id=last_insert_rowid()').fetchone()
    except sqlite3.IntegrityError:c.rollback();c.close();return jerr('duplicate',409)
    c.close();return web.json_response({'channel':channelobj(r)},status=201)
async def join(req):
    u=require(req)
    if not u:return jerr('unauthorized',401)
    cid=int(req.match_info['id']);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:c.close();return jerr('not found',404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();m=member(c,w['id'],u['id'])
    if ch['is_private'] and (not m or m['role']!='owner'):c.close();return jerr('forbidden',403)
    if m and m['role']=='guest':c.close();return jerr('forbidden',403)
    if not m:
        if w['join_mode']!='open':c.close();return jerr('forbidden',403)
        c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    c.commit();c.close();return web.json_response({'channel':channelobj(ch)})
async def create_inv(req):
    u=require(req);slug=req.match_info['slug']
    if not u:return jerr('unauthorized',401)
    d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    m=member(c,w['id'],u['id'])
    if not m or m['role'] not in ('owner','admin'):c.close();return jerr('forbidden',403)
    mx=d.get('max_uses',1)
    if not isinstance(mx,int) or mx<1:c.close();return jerr('invalid max_uses',400)
    code=secrets.token_urlsafe(18); exp=d.get('expires_in_seconds')
    expires=None
    if exp is not None:
        if not isinstance(exp,(int,float)) or exp<=0:c.close();return jerr('invalid expiry',400)
        expires=(datetime.now(timezone.utc).timestamp()+exp)
    c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],str(expires) if expires else None,mx,0,d.get('invited_user_id'),d.get('invited_username')));c.commit()
    r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone();c.close()
    return web.json_response({'invitation':invobj(r)},status=201)
def invobj(r):
    return {'code':r['code'],'workspace_id':r['workspace_id'],'creator_id':r['creator_id'],'expires_at':r['expires_at'],'max_uses':r['max_uses'],'uses':r['uses'],'invited_user_id':r['invited_user_id'],'invited_username':r['invited_username']}
async def list_inv(req):
    u=require(req);slug=req.match_info['slug']
    if not u:return jerr('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return jerr('not found',404)
    m=member(c,w['id'],u['id'])
    if not m or m['role'] not in ('owner','admin'):c.close();return jerr('forbidden',403)
    rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses',(w['id'],)).fetchall();c.close()
    valid=[invobj(x) for x in rs if not x['expires_at'] or float(x['expires_at'])>datetime.now(timezone.utc).timestamp()]
    return web.json_response({'invitations':valid})
async def delete_inv(req):
    u=require(req);code=req.match_info['code']
    if not u:return jerr('unauthorized',401)
    c=db();r=c.execute('SELECT i.*,m.role FROM invitations i JOIN members m ON m.workspace_id=i.workspace_id AND m.user_id=? WHERE i.code=?',(u['id'],code)).fetchone()
    if not r:c.close();return jerr('not found',404)
    if r['role'] not in ('owner','admin'):c.close();return jerr('forbidden',403)
    c.execute('DELETE FROM invitations WHERE code=?',(code,));c.commit();c.close();return web.json_response({'deleted':True})
async def accept_inv(req):
    u=require(req);code=req.match_info['code']
    if not u:return jerr('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
    if not r or r['uses']>=r['max_uses'] or r['invited_user_id'] and r['uses']>=1 or r['invited_username'] and r['uses']>=1 or (r['expires_at'] and float(r['expires_at'])<=datetime.now(timezone.utc).timestamp()):c.close();return jerr('not found',404)
    if r['invited_user_id'] and r['invited_user_id']!=u['id']:c.close();return jerr('forbidden',403)
    if r['invited_username'] and r['invited_username']!=u['username']:c.close();return jerr('forbidden',403)
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,));c.commit();c.close();return web.json_response({'ok':True})
async def ws_handler(req):
    u=auth(req)
    if not u:return jerr('unauthorized',401)
    ws=web.WebSocketResponse();await ws.prepare(req)
    await ws.send_json({'type':'connected'})
    async for msg in ws:
        if msg.type==WSMsgType.ERROR:break
    return ws

app=web.Application()
app.router.add_get('/api/health',health)
app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
app.router.add_post('/api/workspaces',create_ws);app.router.add_get('/api/workspaces',list_ws)
app.router.add_get('/api/workspaces/{slug}',get_ws);app.router.add_get('/api/workspaces/{slug}/members',members)
app.router.add_patch('/api/workspaces/{slug}',patch_ws);app.router.add_patch('/api/workspaces/{slug}/members/{user_id}',patch_member)
app.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer);app.router.add_post('/api/workspaces/{slug}/channels',create_channel)
app.router.add_post('/api/channels/{id}/join',join)
app.router.add_post('/api/workspaces/{slug}/invitations',create_inv);app.router.add_get('/api/workspaces/{slug}/invitations',list_inv)
app.router.add_delete('/api/workspaces/{slug}/invitations/{code}',delete_inv);app.router.add_post('/api/invitations/{code}/accept',accept_inv)
app.router.add_get('/api/ws',ws_handler)
web.run_app(app,host='127.0.0.1',port=PORT)

import sys, os, re, json, sqlite3, hashlib, secrets, datetime, asyncio
from aiohttp import web

NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)

def db():
    c=sqlite3.connect(DB,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0,expires_at TEXT,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
    '''); c.commit(); c.close()
init()

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def err(n,msg='error'): return web.json_response({'error':msg},status=n)
def uj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def wj(r): return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def cj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
async def body(req):
    try:return await req.json()
    except:return {}
def user(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
def ws(slug):
    c=db(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); c.close(); return r
def role(wid,uid):
    c=db(); r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone(); c.close(); return r['role'] if r else None
def require(req, allowed=None, w=None):
    u=user(req)
    if not u:return None,err(401)
    if allowed and role(w['id'],u['id']) not in allowed:return u,err(403)
    return u,None
def invj(r): return {'code':r['code'],'workspace_id':r['workspace_id'],'max_uses':r['max_uses'],'uses':r['uses'],'expires_at':r['expires_at'],'invited_user_id':r['invited_user_id']}
def active(r):
    return not r['revoked'] and r['uses'] < (1 if r['invited_user_id'] else r['max_uses']) and (not r['expires_at'] or r['expires_at']>now())

async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
    b=await body(req); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err(400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); uid=cur.lastrowid
        tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
    except sqlite3.IntegrityError:c.rollback(); c.close(); return err(409)
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return web.json_response({'user':uj(r),'token':tok},status=201)
async def login(req):
    b=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=?',(b.get('username',''),)).fetchone()
    if not r or r['password']!=hashlib.sha256(b.get('password','').encode()).hexdigest():c.close(); return err(401)
    t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return web.json_response({'user':uj(r),'token':t})
async def me(req):
    u,e=require(req)
    return e or web.json_response({'user':uj(u)})
async def create_ws(req):
    u,e=require(req)
    if e:return e
    b=await body(req); sl=b.get('slug',''); name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',sl) or not name:return err(400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); x=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(sl,name,u['id'])); wid=x.lastrowid
        c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); x=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=x.lastrowid; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
    except sqlite3.IntegrityError:c.rollback(); c.close(); return err(409)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
    return web.json_response({'workspace':wj(w),'general_channel':cj(ch)},status=201)
async def list_ws(req):
    u,e=require(req)
    if e:return e
    c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close(); return web.json_response({'workspaces':[wj(x) for x in rows]})
async def members(req):
    u,e=require(req); w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    if not role(w['id'],u['id']):return err(403)
    c=db(); rows=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall(); c.close()
    return web.json_response({'members':[{'user_id':x['user_id'],'role':x['role'],'username':x['username'],'display_name':x['display_name']} for x in rows]})
async def patch_ws(req):
    u,e=require(req); w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    if role(w['id'],u['id']) not in ('owner','admin'):return err(403)
    b=await body(req)
    if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return err(400)
    vals=[]; args=[]
    for k in ('name','join_mode'):
        if k in b: vals.append(k+'=?');args.append(b[k])
    if vals:
        c=db(); c.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',args+[w['id']]); c.commit(); c.close()
    w=ws(w['slug']); return web.json_response({'workspace':wj(w)})
async def create_channel(req):
    u,e=require(req); w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    r=role(w['id'],u['id']); b=await body(req); private=bool(b.get('is_private',False))
    if r not in ('owner','admin','member'):return err(403)
    if private and r not in ('owner','admin'):return err(403)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')):return err(400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); x=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(private),b.get('topic',''))); cid=x.lastrowid; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return err(409)
    x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return web.json_response({'channel':cj(x)},status=201)
async def join(req):
    u,e=require(req)
    if e:return e
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err(404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.close();return err(403)
    if r and r['role']=='guest':c.close();return err(403)
    if not r:
        if w['join_mode']!='open':c.close();return err(403)
        c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']));c.commit(); c.close();return web.json_response({'channel':cj(ch)})
async def patch_role(req):
    u,e=require(req); w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    tid=int(req.match_info['user_id']); b=await body(req); nr=b.get('role')
    tr=role(w['id'],tid); ar=role(w['id'],u['id'])
    if not tr:return err(404)
    if nr not in ('owner','admin','member','guest') :return err(400)
    if nr=='owner':return err(400)
    if ar not in ('owner','admin'):return err(403)
    if tid==u['id'] or tr=='owner' or (ar=='admin' and tr=='admin'):return err(403)
    c=db();c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(nr,w['id'],tid));c.commit();x=c.execute('SELECT u.* FROM users u WHERE id=?',(tid,)).fetchone();c.close()
    return web.json_response({'member':{'user_id':tid,'role':nr,'username':x['username'],'display_name':x['display_name']}})
async def transfer(req):
    u,e=require(req); w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    if role(w['id'],u['id'])!='owner':return err(403)
    tid=(await body(req)).get('new_owner_id'); 
    if role(w['id'],tid)!='admin':return err(400)
    c=db();c.execute('BEGIN IMMEDIATE');c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id']));c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],tid));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(tid,w['id']));c.commit();c.close();return web.json_response({'workspace':wj(ws(w['slug']))})
async def create_inv(req):
    u,e=require(req);w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    if role(w['id'],u['id']) not in ('owner','admin'):return err(403)
    b=await body(req); mu=b.get('max_uses',1)
    if not isinstance(mu,int) or mu<1:return err(400)
    target=b.get('invited_user_id')
    c=db()
    if b.get('invited_username'):
        x=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone()
        if x:target=x['id']
    exp=None
    if b.get('expires_in_seconds') is not None:
        exp=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=int(b['expires_in_seconds']))).isoformat(timespec='milliseconds').replace('+00:00','Z')
    code=secrets.token_urlsafe(16);c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,0)',(code,w['id'],mu,0,exp,target));c.commit();x=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone();c.close();return web.json_response({'invitation':invj(x)},status=201)
async def list_inv(req):
    u,e=require(req);w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    if role(w['id'],u['id']) not in ('owner','admin'):return err(403)
    c=db();a=[invj(x) for x in c.execute('SELECT * FROM invitations WHERE workspace_id=?',(w['id'],)).fetchall() if active(x)];c.close();return web.json_response({'invitations':a})
async def accept(req):
    u,e=require(req)
    if e:return e
    c=db();x=c.execute('SELECT * FROM invitations WHERE code=?',(req.match_info['code'],)).fetchone()
    if not x or not active(x):c.close();return err(404)
    if x['invited_user_id'] and x['invited_user_id']!=u['id']:c.close();return err(403)
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(x['code'],));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.commit();c.close();return web.json_response({'workspace':wj(ws((db().execute('SELECT slug FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()['slug'])))})
async def revoke(req):
    u,e=require(req);w=ws(req.match_info['slug'])
    if e:return e
    if not w:return err(404)
    if role(w['id'],u['id']) not in ('owner','admin'):return err(403)
    c=db();x=c.execute('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(req.match_info['code'],w['id'])).fetchone()
    if not x:c.close();return err(404)
    c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(x['code'],));c.commit();c.close();return web.json_response({'deleted':True})
async def wsock(req):
    u=user(req)
    if not u:
        tok=req.query.get('token','')
        if tok:
            c=db(); u=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close()
    if not u:return err(401)
    w=web.WebSocketResponse();await w.prepare(req);await w.send_json({'type':'ready'});await w.close();return w

app=web.Application()
app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
app.router.add_post('/api/workspaces',create_ws);app.router.add_get('/api/workspaces',list_ws);app.router.add_get('/api/workspaces/{slug}/members',members);app.router.add_patch('/api/workspaces/{slug}',patch_ws)
app.router.add_post('/api/workspaces/{slug}/channels',create_channel);app.router.add_post('/api/channels/{id}/join',join)
app.router.add_patch('/api/workspaces/{slug}/members/{user_id}',patch_role);app.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer)
app.router.add_post('/api/workspaces/{slug}/invitations',create_inv);app.router.add_get('/api/workspaces/{slug}/invitations',list_inv);app.router.add_delete('/api/workspaces/{slug}/invitations/{code}',revoke);app.router.add_post('/api/invitations/{code}/accept',accept)
app.router.add_get('/api/ws',wsock)
web.run_app(app,host='127.0.0.1',port=int(sys.argv[2]) if len(sys.argv)>2 else 8000)

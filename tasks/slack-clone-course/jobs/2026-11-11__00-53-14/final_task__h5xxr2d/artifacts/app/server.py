import asyncio, base64, hashlib, json, os, re, secrets, sqlite3, sys
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
sockets={}
ircnicks=set()
irc_clients={}
global_seen=0
local_users=set()

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=8); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=5000'); return c
def init():
    for attempt in range(30):
      try:
        c=db()
        c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '',created_at TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS read_state(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, uploader_id INTEGER, filename TEXT, content_type TEXT, size INTEGER, created_at TEXT, path TEXT);
    CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS presence(node_id INTEGER,user_id INTEGER,PRIMARY KEY(node_id,user_id));
    CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    '''); c.execute('DELETE FROM presence WHERE node_id=?',(NODE,)); c.commit(); c.close(); return
      except sqlite3.OperationalError:
        try: c.close()
        except: pass
        import time; time.sleep(.15)
    raise RuntimeError('database initialization failed')
def userj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def err(n,msg='error'): return web.json_response({'error':msg},status=n)
async def body(req):
    try:return await req.json()
    except:return {}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
def pag(req,items):
    try: lim=int(req.query.get('limit','50'))
    except: return None,None
    if lim<=0 or lim>200:return None,None
    cur=req.query.get('cursor'); start=0
    if cur:
        try:start=int(base64.urlsafe_b64decode(cur+'===').decode())+1
        except:return None,None
    out=items[start:start+lim]; nxt=None if start+lim>=len(items) else base64.urlsafe_b64encode(str(start+lim-1).encode()).decode().rstrip('=')
    return out,nxt
def channelj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def msgj(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids,MIN(rowid) first_row FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY first_row',(r['id'],)).fetchall()
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM message_files WHERE message_id=?)',(r['id'],)).fetchall()],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rs],'mentions':resolve_mentions(r['body'],r['channel_id'],r['author_id'])}
def emit(c,ch,kind,data):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
    frame={'type':kind,'event_id':n,'channel_id':ch}; frame.update(data)
    c.execute('INSERT INTO events VALUES(?,?,?)',(ch,n,json.dumps(frame))); return frame
async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
    d=await body(req); u=d.get('username','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(d.get('password',''))<8:return err(400)
    c=db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name,created_at) VALUES(?,?,?,?)',(u,hashlib.sha256(d['password'].encode()).hexdigest(),d.get('display_name') or u,now()))
        uid=cur.lastrowid; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return web.json_response({'user':userj(r),'token':t},status=201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
async def login(req):
    d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest())).fetchone()
    if not r:c.close(); return err(401)
    t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return web.json_response({'user':userj(r),'token':t})
async def me(req):
    r=auth(req); return web.json_response({'user':userj(r)}) if r else err(401)
async def workspaces(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
    return web.json_response({'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rows]})
async def create_ws(req):
    u=auth(req)
    if not u:return err(401)
    d=await body(req)
    if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return err(400)
    c=db()
    try:
        x=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',d['slug']),u['id'])).lastrowid
        c.execute('INSERT INTO members VALUES(?,?,?)',(x,u['id'],'owner')); g=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(x,'general')).lastrowid; c.execute('INSERT INTO channel_members VALUES(?,?)',(g,u['id'])); c.commit()
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(x,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(g,)).fetchone(); return web.json_response({'workspace':dict(w),'general_channel':channelj(ch)},status=201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
async def ws_detail(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    m=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m:return err(403)
    rows=c.execute('SELECT ch.* FROM channels ch LEFT JOIN channel_members cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE ch.workspace_id=? AND (ch.is_private=0 OR cm.user_id IS NOT NULL) AND (ch.is_archived=0 OR ?)',(u['id'],w['id'],req.query.get('include_archived') in ('1','true'))).fetchall()
    c.close(); return web.json_response({'workspace':dict(w),'channels':[channelj(x) for x in rows],'read_state':[]})
async def create_ch(req):
    u=auth(req)
    if not u:return err(401)
    d=await body(req); name=d.get('name','')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32:return err(400)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not mr:return err(403)
    private=bool(d.get('is_private')); 
    if private and mr['role'] not in ('owner','admin'):return err(403)
    try:
        x=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),d.get('topic',''))).lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(x,u['id']));c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(x,)).fetchone();return web.json_response({'channel':channelj(r)},status=201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
async def join(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); r=c.execute('SELECT ch.*,w.join_mode,w.id wid FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if r['is_private']:return err(403)
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['wid'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(r['id'],u['id']));c.commit();return web.json_response({'channel':channelj(r)})
async def channels(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); rows=c.execute('SELECT ch.* FROM channels ch JOIN channel_members cm ON cm.channel_id=ch.id WHERE cm.user_id=? AND ch.id=?',(u['id'],req.match_info['id'])).fetchall();c.close()
    if not rows:return err(404)
    return web.json_response({'channel':channelj(rows[0])})
async def messages(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return err(403)
    if req.method=='GET':
        rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(ch['id'],)).fetchall(); out,n=pag(req,rows)
        if out is None:return err(400)
        z=[msgj(c,x) for x in out];c.close();return web.json_response({'messages':z,'next_cursor':n})
    d=await body(req); b=d.get('body','')
    if isinstance(b,str) and b.startswith('/') and not b.startswith('//'):
        parts=b.split(' ',1); cmd=parts[0][1:]; arg=parts[1].strip() if len(parts)>1 else ''
        if cmd=='shrug': b=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
        elif cmd=='me':
            if not arg:return err(400)
        elif cmd=='topic':
            if not arg:return err(400)
            if len(arg)>250:return err(400)
            c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));emit(c,ch['id'],'channel.updated',{'channel':channelj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close();return web.json_response({'message':None,'channel':channelj(r)},status=201)
        elif cmd=='invite':
            if not arg.startswith('@'):return err(400)
            name=arg[1:]; target=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
            if not target:c.close();return err(404)
            c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],target['id'],'member'));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],target['id']));emit(c,ch['id'],'member.joined',{'user_id':target['id']});c.commit();c.close();return web.json_response({'message':None,'invited_user':userj(target)},status=201)
        elif cmd in ('archive','unarchive'):
            owner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            if owner['owner_id']!=u['id']:c.close();return err(403)
            val=1 if cmd=='archive' else 0
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();emit(c,ch['id'],'channel.archived' if val else 'channel.unarchived',{'channel':channelj(r)});c.commit();c.close();return web.json_response({'message':None,'channel':channelj(r)},status=201)
        else:return err(400)
    if ch['is_archived']:return err(423)

    if not b or not b.strip():return err(400)
    file_ids=d.get('file_ids',[])
    if not isinstance(file_ids,list):return err(400)
    for fid in file_ids:
        f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():return err(400)
    c.execute('BEGIN IMMEDIATE'); x=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],b,d.get('parent_id'),now())).lastrowid
    r=c.execute('SELECT * FROM messages WHERE id=?',(x,)).fetchone();
    for fid in file_ids:c.execute('INSERT INTO message_files VALUES(?,?)',(x,fid))
    f=emit(c,ch['id'],'message.reply' if d.get('parent_id') else 'message.created',{'message':msgj(c,r)}); c.commit();c.close();return web.json_response({'message':msgj(db(),r)},status=201)
async def event_poller():
    global global_seen
    global local_users
    seen={}
    while True:
        try:
            c=db()
            c.execute('DELETE FROM presence WHERE node_id=?',(NODE,))
            for uid in local_users: c.execute('INSERT OR IGNORE INTO presence VALUES(?,?)',(NODE,uid))
            c.commit()
            for ge in c.execute('SELECT id,frame FROM global_events WHERE id>? ORDER BY id',(global_seen,)):
                for w in list(sockets):
                    try: await w.send_str(ge['frame'])
                    except: pass
                global_seen=ge['id']
            for w,meta in list(sockets.items()):
                for ch in list(meta):
                    last=meta[ch]
                    rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last)).fetchall()
                    for r in rows:
                        try: await w.send_str(r['frame'])
                        except: pass
                        meta[ch]=r['event_id']
            c.close()
        except: pass
        await asyncio.sleep(.05)

async def ws(req):
    global local_users
    tok=req.query.get('token',''); c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone();c.close()
    if not r:return err(401)
    w=web.WebSocketResponse();await w.prepare(req);sockets[w]={}; local_users.add(r['user_id']); pc=db();pc.execute('INSERT OR REPLACE INTO presence VALUES(?,?)',(NODE,r['user_id']));pc.commit();pc.close()
    try:
        async for m in w:
            if m.type==WSMsgType.TEXT:
                d=json.loads(m.data); ch=d.get('channel_id'); c=db(); head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch,)).fetchone()[0]; sockets[w][ch]=d.get('since_event_id',0) if d.get('type')=='resume' else head; 
                if d.get('type')=='subscribe': await w.send_json({'type':'subscribed','channel_id':ch,'head_event_id':head})
                elif d.get('type')=='resume':
                    for x in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,d.get('since_event_id',0))): await w.send_str(x['frame'])
                    await w.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
                c.close()
    finally:
        sockets.pop(w,None)
        local_users.discard(r['user_id'])
        pc=db();pc.execute('DELETE FROM presence WHERE node_id=? AND user_id=?',(NODE,r['user_id']));pc.commit();pc.close()
    return w
HTML='''<!doctype html><html><head><title>Huddle</title><style>body{font:16px Arial;margin:0;background:#f8f8f8}button{border:0;padding:10px 16px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}</style></head><body><main id="app"><h1>Huddle</h1><section data-testid="auth-modal"><form data-testid="auth-form"><input name="username" placeholder="Username"><input name="password" type="password" placeholder="Password"><button data-testid="auth-submit" data-button-role="primary">Log in</button></form></section><button data-testid="logout-btn" data-button-role="danger">Logout</button><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><div data-testid="channel-list"></div><h2 data-testid="workspace-header">Workspace</h2><h2 data-testid="channel-title">#general</h2><div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary">Send</button></main></body></html>'''
async def root(req):
    return web.FileResponse('/app/static/index.html',headers={'Content-Type':'text/html'})

def app():
    init(); a=web.Application(client_max_size=11*1024*1024)
    a.router.add_get('/',root);a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me);a.router.add_get('/api/users/{id}',get_user);a.router.add_patch('/api/users/me',patch_me);a.router.add_post('/api/dms',create_dm);a.router.add_post('/api/files',upload_file);a.router.add_route('*','/api/workspaces/{slug}/groups',groups);a.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',groups);a.router.add_get('/api/files/{id}',file_meta);a.router.add_get('/api/files/{id}/download',file_download);a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',create_ws);a.router.add_get('/api/workspaces/{slug}',ws_detail);a.router.add_patch('/api/workspaces/{slug}',patch_workspace);a.router.add_patch('/api/workspaces/{slug}/members/{user_id}',patch_member);a.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer);a.router.add_post('/api/workspaces/{slug}/channels',create_ch);a.router.add_post('/api/channels/{id}/join',join);a.router.add_get('/api/channels/{id}',channels);a.router.add_patch('/api/channels/{id}',patch_channel);a.router.add_get('/api/channels/{id}/members',channel_members);a.router.add_get('/api/channels/{id}/messages',messages);a.router.add_post('/api/channels/{id}/messages',messages);a.router.add_patch('/api/messages/{id}',message_edit);a.router.add_delete('/api/messages/{id}',message_delete);a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_post('/api/messages/{id}/reactions',reaction);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',reaction);a.router.add_get('/api/ws',ws);return a


async def patch_workspace(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m or m['role'] not in ('owner','admin'):return err(403)
    d=await body(req)
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return err(400)
    c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit()
    r=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close();return web.json_response({'workspace':dict(r)})
async def patch_member(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],req.match_info['user_id'])).fetchone()
    if not target:return err(404)
    if not actor or actor['role'] not in ('owner','admin'):return err(403)
    d=await body(req); role=d.get('role')
    if role not in ('owner','admin','member','guest'):return err(400)
    if role=='owner' or target['role']=='owner' or (actor['role']=='admin' and target['role']=='admin'):return err(403)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],req.match_info['user_id']));c.commit();c.close();return web.json_response({'member':{'user_id':int(req.match_info['user_id']),'role':role}})
async def transfer(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if w['owner_id']!=u['id']:return err(403)
    d=await body(req); t=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],d.get('new_owner_id'))).fetchone()
    if not t or t['role']!='admin':return err(400)
    c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],u['id']));c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],d['new_owner_id']));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(d['new_owner_id'],w['id']));c.commit();c.close();return web.json_response({'workspace':dict(w)})
async def patch_channel(req):
    u=auth(req)
    if not u:return err(401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    m=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone();w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if not m and w['owner_id']!=u['id']:return err(403)
    d=await body(req)
    if len(d.get('topic',''))>250:return err(400)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',ch['topic']),ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close();return web.json_response({'channel':channelj(r)})
async def channel_members(req):
    u=auth(req)
    if not u:return err(401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    rows=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close();return web.json_response({'members':[userj(x) for x in rows]})
async def message_edit(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if r['deleted']:return err(409)
    if r['author_id']!=u['id']:return err(403)
    d=await body(req)
    if not d.get('body','').strip():return err(400)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),r['id']));r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();emit(c,r['channel_id'],'message.updated',{'message':msgj(c,r)});c.commit();z=msgj(c,r);c.close();return web.json_response({'message':z})
async def message_delete(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if r['deleted']:c.close();return web.json_response({'deleted':True})
    if r['author_id']!=u['id'] and r['owner_id']!=u['id']:return err(403)
    emit(c,r['channel_id'],'message.deleted',{'message_id':r['id']});c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));c.commit();c.close();return web.json_response({'deleted':True})
async def replies(req):
    u=auth(req)
    if not u:return err(401)
    c=db();p=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not p:return err(404)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(p['id'],)).fetchall();out,n=pag(req,rows)
    if out is None:return err(400)
    z=[msgj(c,x) for x in out];c.close();return web.json_response({'replies':z,'next_cursor':n})
async def reaction(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT m.* FROM messages m WHERE m.id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if r['deleted']:return err(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():c.close();return err(403)
    emoji=req.match_info.get('emoji') or (await body(req)).get('emoji')
    if not isinstance(emoji,str) or not emoji or len(emoji)>64:c.close();return err(400)
    exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji)).fetchone()
    changed=False;kind=''
    if req.method=='POST':
        if not exists:c.execute('INSERT INTO reactions VALUES(?,?,?)',(r['id'],u['id'],emoji));changed=True;kind='reaction.added'
    elif exists:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji));changed=True;kind='reaction.removed'
    if changed:emit(c,r['channel_id'],kind,{'message_id':r['id'],'emoji':emoji,'user_id':u['id']})
    c.commit();rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids,MIN(rowid) first_row FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY first_row',(r['id'],)).fetchall()
    out=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(q) for q in x['ids'].split(',')]} for x in rs];c.close();return web.json_response({'reactions':out})


async def get_user(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    return err(404) if not r else web.json_response({'user':userj(r)})

async def patch_me(req):
    u=auth(req)
    if not u:return err(401)
    d=await body(req)
    allowed={'display_name':(1,80),'timezone':(1,999),'avatar_url':(0,1024),'status_text':(0,140),'status_emoji':(0,64)}
    if any(k not in allowed or not isinstance(v,str) or not (allowed[k][0] <= len(v) <= allowed[k][1]) for k,v in d.items()): return err(400)
    if 'timezone' in d:
        try: ZoneInfo(d['timezone'])
        except Exception: return err(400)
    c=db(); sets=[]; vals=[]
    for k in allowed:
        if k in d: sets.append(k+'=?');vals.append(d[k])
    if sets:
        vals.append(u['id']);c.execute('UPDATE users SET '+','.join(sets)+' WHERE id=?',vals);c.commit()
    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); obj=userj(r);c.close()
    c=db();c.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps({'type':'user.updated','user':obj}),));c.commit();c.close()
    return web.json_response({'user':obj})

async def create_dm(req):
    u=auth(req)
    if not u:return err(401)
    d=await body(req); other=d.get('user_id')
    if other is None:return err(400)
    c=db(); target=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not target:c.close();return err(404)
    a,b=sorted((u['id'],int(other)))
    name=f'dm-{a}-{b}'
    r=c.execute('SELECT id FROM channels WHERE name=? AND is_dm=1',(name,)).fetchone()
    if r: cid=r['id']
    else:
        ws=c.execute('SELECT id FROM workspaces WHERE owner_id=? ORDER BY id LIMIT 1',(u['id'],)).fetchone()
        if not ws:
            ws=c.execute('SELECT id FROM workspaces ORDER BY id LIMIT 1').fetchone()
        if not ws:c.close();return err(400)
        cid=c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(?,?,1,1)',(ws['id'],name)).lastrowid
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,int(other)));c.commit();c.close()
    return web.json_response({'channel_id':cid})


async def upload_file(req):
    u=auth(req)
    if not u:return err(401)
    reader=await req.multipart(); part=None
    while True:
        q=await reader.next()
        if q is None:break
        if q.name=='file':part=q;break
    if part is None:return err(400)
    data=bytearray()
    while True:
        chunk=await part.read_chunk()
        if not chunk:break
        data.extend(chunk)
        if len(data)>10485760:return err(413)
    c=db(); fid=c.execute('SELECT COALESCE(MAX(id),0)+1 FROM files').fetchone()[0]
    path=f'/app/data/file-{fid}';open(path,'wb').write(data)
    c.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?)',(fid,u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),path));c.commit()
    r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone();c.close()
    return web.json_response({'file':dict(r)},status=201)
async def file_meta(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    return err(404) if not r else web.json_response({'file':dict(r)})
async def file_download(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    if not r:return err(404)
    return web.FileResponse(r['path'],headers={'Content-Type':r['content_type'],'Content-Disposition':f'attachment; filename="{r["filename"]}"'})

def resolve_mentions(body,channel_id,author_id):
    c=db();ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(channel_id,)).fetchone()
    if not ch:c.close();return []
    members={x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(channel_id,))}
    ws={x['user_id'] for x in c.execute('SELECT user_id FROM members WHERE workspace_id=?',(ch['workspace_id'],))}
    online={x['user_id'] for x in c.execute('SELECT DISTINCT user_id FROM presence')}
    for x in sockets:
        try:
            q=auth(type('R',(),{'headers':{}})())
        except: pass
    out=set()
    import re
    for token in re.findall(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_-]+)',body):
        if token=='channel':out.update(members)
        elif token=='here':out.update(members & online)
        else:
            u=c.execute('SELECT id FROM users WHERE username=?',(token,)).fetchone()
            if u and u['id'] in ws:out.add(u['id'])
            g=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(ch['workspace_id'],token)).fetchone()
            if g:out.update(x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)) if x['user_id'] in members)
    out.discard(author_id);c.close();return sorted(out)

def groupj(c,r):
    return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id',(r['id'],))]}

async def groups(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    m=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m:return err(403)
    if req.method=='GET':
        rows=c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall();out=[groupj(c,r) for r in rows];c.close();return web.json_response({'groups':out})
    if req.method=='DELETE':
        r=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
        if not r:return web.json_response({'deleted':True})
        if r['creator_id']!=u['id'] and w['owner_id']!=u['id']:c.close();return err(403)
        c.execute('DELETE FROM group_members WHERE group_id=?',(r['id'],));c.execute('DELETE FROM groups WHERE id=?',(r['id'],));c.commit();c.close();return web.json_response({'deleted':True})
    d=await body(req);handle=req.match_info.get('handle')
    if req.method=='POST':
        handle=d.get('handle','')
        if not re.fullmatch(r'[a-z0-9_-]{1,32}',handle) or not d.get('name'):return err(400)
        if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone():return err(409)
        uid=d.get('user_ids',[])
        if any(not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],x)).fetchone() for x in uid):return err(400)
        gid=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,d['name'],u['id'])).lastrowid
    else:
        r=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
        if not r:return err(404)
        if r['creator_id']!=u['id'] and w['owner_id']!=u['id']:return err(403)
        gid=r['id'];uid=d.get('user_ids',groupj(c,r)['member_user_ids']);c.execute('DELETE FROM group_members WHERE group_id=?',(gid,));c.execute('UPDATE groups SET name=? WHERE id=?',(d.get('name',r['name']),gid))
    for x in uid:c.execute('INSERT INTO group_members VALUES(?,?)',(gid,x))
    c.commit();r=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone();z=groupj(c,r);c.close();return web.json_response({'group':z},status=201 if req.method=='POST' else 200)


def irc_channel(name):
    if not name.startswith('#') or '/' not in name[1:]: return None
    slug,chan=name[1:].split('/',1)
    c=db(); r=c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,chan)).fetchone(); c.close()
    return r

async def irc_event_pump():
    last={}
    while True:
        try:
            c=db()
            for cid in [x['channel_id'] for x in c.execute('SELECT DISTINCT channel_id FROM events')]:
                n=last.get(cid,0)
                for x in c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,n)):
                    last[cid]=x['event_id']
                    try:
                        f=json.loads(x['frame'])
                        if f.get('type')!='message.created': continue
                        m=f.get('message',{}); a=m.get('author',{}); body=m.get('body','')
                        ch=c.execute('SELECT ch.name,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
                        if not ch: continue
                        name='#'+ch['slug']+'/'+ch['name']
                        for wr,meta in list(irc_clients.items()):
                            if cid in meta.get('joined',set()):
                                try:
                                    wr.write((f":{a.get('username',meta['nick'])}!{a.get('username',meta['username'])}@huddle PRIVMSG {name} :{body}\r\n").encode()); await wr.drain()
                                except: pass
                    except: pass
            c.close()
        except: pass
        await asyncio.sleep(.05)

async def irc_main(port):
    async def handle(reader,writer):
        token=None; nick=None; username='irc'; registered=False; joined=set()
        async def send(line):
            writer.write((line+'\r\n').encode()); await writer.drain()
        async def registration():
            nonlocal registered
            if registered or not token or not nick: return
            c=db(); ok=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone(); c.close()
            if not ok:
                await send(':huddle 464 * :Password incorrect'); writer.close(); return
            if nick in ircnicks:
                await send(f':huddle 433 * {nick} :Nickname is already in use'); return
            ircnicks.add(nick); registered=True; irc_clients[writer]={'nick':nick,'username':username,'token':token,'joined':set()}
            for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0'),('005','CHANTYPES=#')]:
                await send(f':huddle {code} {nick} :{text}')
            await send(f':huddle 422 {nick} :MOTD File is missing')
        try:
            while not reader.at_eof():
                raw=await reader.readline()
                if not raw: break
                line=raw.decode(errors='replace').strip('\r\n')
                if not line: continue
                bits=line.split(' ',1); cmd=bits[0].upper(); rest=bits[1] if len(bits)>1 else ''
                if cmd=='PASS': token=rest.lstrip(':').strip()
                elif cmd=='NICK':
                    nn=rest.split()[0] if rest.split() else ''
                    if nn and nn!=nick and nn in ircnicks:
                        await send(f':huddle 433 * {nn} :Nickname is already in use'); continue
                    nick=nn; await registration()
                elif cmd=='USER':
                    if rest: username=rest.split()[0]
                    await registration()
                elif cmd=='PING':
                    val=rest.split()[-1] if rest else ''
                    await send(f':huddle PONG huddle :{val}')
                elif cmd=='QUIT':
                    break
                elif not registered:
                    continue
                elif cmd=='JOIN':
                    cname=rest.split(',')[0].strip()
                    ch=irc_channel(cname)
                    if not ch:
                        await send(f':huddle 403 {nick} {cname} :No such channel'); continue
                    c=db(); u=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
                    member=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['user_id'])).fetchone()
                    if ch['is_private'] and not member:
                        c.close(); await send(f':huddle 403 {nick} {cname} :No such channel'); continue
                    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['user_id'],'member'))
                    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['user_id']));c.commit()
                    users=c.execute('SELECT u.id,u.username FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();topic=ch['topic'];c.close()
                    names=" ".join(nick if x['id']==u['user_id'] else x['username'] for x in users)
                    joined.add(ch['id']); irc_clients[writer]['joined'].add(ch['id']); await send(f':{nick}!{username}@huddle JOIN {cname}')
                    if topic: await send(f':huddle 332 {nick} {cname} :{topic}')
                    else: await send(f':huddle 331 {nick} {cname} :No topic is set')
                    await send(f':huddle 353 {nick} = {cname} :{names}')
                    await send(f':huddle 366 {nick} {cname} :End of /NAMES list')
                elif cmd=='PRIVMSG':
                    if ' :' not in rest: continue
                    target,body=rest.split(' :',1); ch=irc_channel(target.strip())
                    if not ch: await send(f':huddle 403 {nick} {target} :No such channel'); continue
                    c=db(); u=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
                    if not u: c.close(); continue
                    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['user_id'])).fetchone():
                        c.close(); await send(f':huddle 403 {nick} {target} :No such channel'); continue
                    c.execute('BEGIN IMMEDIATE')
                    mid=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],u['user_id'],body,now())).lastrowid
                    mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                    frame=emit(c,ch['id'],'message.created',{'message':msgj(c,mr)});c.commit();c.close()
                elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):
                    continue
                else:
                    await send(f':huddle 421 {nick or "*"} {cmd} :Unknown command')
        finally:
            if nick: ircnicks.discard(nick)
            irc_clients.pop(writer,None)
            writer.close()
            try: await writer.wait_closed()
            except: pass
    server=await asyncio.start_server(handle,'0.0.0.0',port)
    asyncio.create_task(irc_event_pump())
    async with server: await server.serve_forever()

async def serve():
    runner=web.AppRunner(app())
    await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',PORT)
    await site.start()
    asyncio.create_task(event_poller())
    print('LISTENING',PORT,flush=True)
    await asyncio.Event().wait()
if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='irc':
        asyncio.run(irc_main(int(sys.argv[2]) if len(sys.argv)>2 else 6667))
    else:
        asyncio.run(serve())

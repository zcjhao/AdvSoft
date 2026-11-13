import asyncio, json, os, re, secrets, sqlite3, sys, hashlib, mimetypes
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
sockets=[]

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    os.makedirs('/app/data/files',exist_ok=True)
    c=sqlite3.connect(DB,timeout=5,isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS workspace_members(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT 'member',PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,seq INTEGER,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
    CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS dm_pairs(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS user_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT);
    CREATE TABLE IF NOT EXISTS presence(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER);
    '''); c.close()
def U(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def C(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): raise web.HTTPUnauthorized()
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
    if not r: raise web.HTTPUnauthorized()
    return r
def user_by_id(i):
    c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(i,)).fetchone(); c.close()
    if not r: raise web.HTTPNotFound()
    return r
def reactions(mid):
    c=db(); rows=c.execute('SELECT emoji,user_id,seq FROM reactions WHERE message_id=? ORDER BY seq',(mid,)).fetchall(); c.close()
    out=[]
    for x in rows:
        if not out or out[-1]['emoji']!=x['emoji']: out.append({'emoji':x['emoji'],'count':0,'user_ids':[]})
        out[-1]['count']+=1; out[-1]['user_ids'].append(x['user_id'])
    return out
def msg(r):
    c=db(); replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    fs=[]
    for x in c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],)):
        fs.append({'id':x['id'],'uploader_id':x['uploader_id'],'filename':x['filename'],'content_type':x['content_type'],'size':x['size'],'created_at':x['created_at']})
    c.close()
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':U(user_by_id(r['author_id'])),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions(r['id']),'mentions':json.loads(r['mentions'] or '[]')}
def readable(c,cid,uid):
    r=c.execute('SELECT c.*,wm.user_id wm,cm.user_id cm FROM channels c LEFT JOIN workspace_members wm ON wm.workspace_id=c.workspace_id AND wm.user_id=? LEFT JOIN channel_members cm ON cm.channel_id=c.id AND cm.user_id=? WHERE c.id=?',(uid,uid,cid)).fetchone()
    if not r: raise web.HTTPNotFound()
    if r['is_private'] or r['is_dm']:
        if not r['cm']: raise web.HTTPForbidden()
    elif not r['wm']: raise web.HTTPForbidden()
    return r
def event(c,cid,typ,**kw):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    f={'type':typ,'event_id':n,'channel_id':cid}; f.update(kw)
    c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(f))); return f
async def pump(app):
    last={}; glast=0
    while True:
        try:
            c=db()
            rows=c.execute('SELECT channel_id,event_id,frame FROM events ORDER BY rowid').fetchall(); grows=c.execute('SELECT id,frame FROM user_events WHERE id>? ORDER BY id',(glast,)).fetchall(); c.close()
            for g in grows:
                glast=max(glast,g['id']); f=json.loads(g['frame'])
                for entry in list(sockets):
                    try: await entry[0].send_json(f)
                    except Exception: pass
            for r in rows:
                if r['event_id']<=last.get(r['channel_id'],0): continue
                last[r['channel_id']]=r['event_id']; f=json.loads(r['frame'])
                for ws,uid,subs,*rest in list(sockets):
                    cursor=rest[0] if rest else {}
                    if r['channel_id'] in subs and r['event_id'] > cursor.get(r['channel_id'],0):
                        cursor[r['channel_id']]=r['event_id']
                        try: await ws.send_json(f)
                        except: pass
        except Exception: pass
        await asyncio.sleep(.05)
async def startup(app): init(); app['task']=asyncio.create_task(pump(app))
async def cleanup(app): app['task'].cancel()
def j(x,status=200): return web.json_response(x,status=status)
async def health(req): return j({'status':'ok','node_id':NODE})
async def register(req):
    b=await req.json(); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: return j({'error':'invalid'},400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); return j({'error':'duplicate'},409)
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return j({'user':U(r),'token':tok},201)
async def login(req):
    b=await req.json(); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
    if not r: c.close(); raise web.HTTPUnauthorized()
    t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.close(); return j({'user':U(r),'token':t})
async def me(req): return j({'user':U(auth(req))})
async def workspaces(req):
    u=auth(req); c=db(); rs=c.execute('SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
    return j({'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rs]})
async def create_ws(req):
    u=auth(req); b=await req.json(); slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): return j({'error':'invalid'},400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO workspace_members VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); return j({'error':'duplicate'},409)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
    return j({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'general_channel':C(ch)},201)
async def ws_detail(req):
    u=auth(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w: c.close(); raise web.HTTPNotFound()
    if not c.execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): c.close(); raise web.HTTPForbidden()
    rs=c.execute('SELECT c.* FROM channels c JOIN channel_members cm ON cm.channel_id=c.id WHERE c.workspace_id=? AND cm.user_id=? UNION SELECT c.* FROM channels c JOIN workspace_members wm ON wm.workspace_id=c.workspace_id WHERE c.workspace_id=? AND c.is_private=0 AND wm.user_id=?',(w['id'],u['id'],w['id'],u['id'])).fetchall(); c.close()
    return j({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[C(x) for x in rs],'read_state':[]})
async def create_ch(req):
    u=auth(req); b=await req.json(); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w: c.close(); raise web.HTTPNotFound()
    if not c.execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): c.close(); raise web.HTTPForbidden()
    if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250: c.close(); return j({'error':'invalid'},400)
    try: c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); return j({'error':'duplicate'},409)
    r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return j({'channel':C(r)},201)
async def join(req):
    u=auth(req); c=db(); r=c.execute('SELECT c.*,w.join_mode FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(req.match_info['id'],)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    c.execute('INSERT OR IGNORE INTO workspace_members(workspace_id,user_id) VALUES(?,?)',(r['workspace_id'],u['id'])); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(r['id'],u['id'])); c.commit(); c.close(); return j({'channel':C(r)})
async def members(req):
    u=auth(req); c=db(); r=readable(c,int(req.match_info['id']),u['id']); rs=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(r['id'],)).fetchall(); c.close(); return j({'members':[U(x) for x in rs]})
async def messages(req):
    u=auth(req); c=db(); r=readable(c,int(req.match_info['id']),u['id']); limit=min(int(req.query.get('limit',50)),200)
    rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(r['id'],limit)).fetchall(); c.close(); return j({'messages':[msg(x) for x in rs],'next_cursor':None})
async def replies(req):
    u=auth(req); c=db(); m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not m: c.close(); raise web.HTTPNotFound()
    readable(c,m['channel_id'],u['id']); rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m['id'],)).fetchall(); c.close(); return j({'replies':[msg(x) for x in rs],'next_cursor':None})
def mentions(c,body,cid,uid):
    ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch: return []
    ids=[]
    online={z['user_id'] for z in c.execute('SELECT user_id FROM presence').fetchall()}
    for x in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
        if x in ('here','channel'):
            rs=c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(cid,)).fetchall()
            if x=='here': rs=[z for z in rs if z['user_id'] in online]
            ids += [z['user_id'] for z in rs]
            continue
        r=c.execute('SELECT id FROM users WHERE username=?',(x,)).fetchone()
        if r and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,r['id'])).fetchone():
            ids.append(r['id'])
        g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],x)).fetchone()
        if g:
            gids=json.loads(g['user_ids'])
            if gids:
                q=','.join('?'*len(gids))
                ids += [z['user_id'] for z in c.execute(
                    f'SELECT user_id FROM channel_members WHERE channel_id=? AND user_id IN ({q})',
                    (cid,*gids))]
    return list(dict.fromkeys(x for x in ids if x!=uid))

async def post_msg(req):
    u=auth(req); b=await req.json(); body=b.get('body','')
    if not body or not body.strip(): return j({'error':'blank'},400)
    c=db(); ch=readable(c,int(req.match_info['id']),u['id'])
    if ch['is_archived'] and not body.startswith('/unarchive'): c.close(); return j({'error':'archived'},423)
    if body.startswith('/') and not body.startswith('//'):
        p=body.split(' ',1); cmd=p[0]; arg=p[1].strip() if len(p)>1 else ''
        if cmd=='/me' and not arg or cmd not in ['/me','/shrug','/topic','/invite','/archive','/unarchive']: c.close(); return j({'error':'command'},400)
        if cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
        if cmd in ['/topic','/invite','/archive','/unarchive']:
            if cmd=='/topic':
                c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id'])); r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone(); event(c,ch['id'],'channel.updated',channel=C(r)); c.commit(); c.close(); return j({'message':None,'channel':C(r)},201)
            owner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()['owner_id']
            if cmd in ['/archive','/unarchive'] and owner!=u['id']: c.close(); raise web.HTTPForbidden()
            if cmd=='/invite':
                mm=re.search(r'@([A-Za-z0-9_]+)',arg); x=c.execute('SELECT * FROM users WHERE username=?',(mm.group(1) if mm else '',)).fetchone()
                if not x: c.close(); raise web.HTTPNotFound()
                c.execute('INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)',(ch['workspace_id'],x['id'],'member')); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],x['id'])); c.commit(); c.close(); return j({'message':None,'invited_user':U(x)},201)
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,ch['id'])); r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone(); event(c,ch['id'],'channel.archived' if cmd=='/archive' else 'channel.unarchived',channel=C(r)); c.commit(); c.close(); return j({'message':None,'channel':C(r)},201)
    if 'file_ids' in b:
        for fid in b['file_ids']:
            x=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
            if not x or x['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone(): c.close(); return j({'error':'file'},400)
    ment=mentions(c,body,ch['id'],u['id']); c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(ch['id'],u['id'],body,b.get('parent_id'),now(),json.dumps(ment))); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();
    for fid in b.get('file_ids',[]): c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
    c.commit(); f=msg(r); c=db(); c.execute('BEGIN IMMEDIATE'); event(c,ch['id'],'message.reply' if b.get('parent_id') else 'message.created',message=f); c.commit(); c.close(); return j({'message':f},201)
async def react(req):
    u=auth(req); c=db(); m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not m: c.close(); raise web.HTTPNotFound()
    readable(c,m['channel_id'],u['id']); b=await req.json() if req.method=='POST' else {}; emoji=b.get('emoji') if req.method=='POST' else req.match_info.get('emoji')
    if not emoji or len(emoji)>64: c.close(); return j({'error':'emoji'},400)
    old=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji)).fetchone()
    if req.method=='POST' and not old:
        c.execute('INSERT INTO reactions VALUES(?,?,?,(SELECT COALESCE(MAX(seq),0)+1 FROM reactions WHERE message_id=?))',(m['id'],u['id'],emoji,m['id'])); event(c,m['channel_id'],'reaction.added',message_id=m['id'],emoji=emoji,user_id=u['id']); c.commit()
    elif req.method=='DELETE' and old:
        c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji)); event(c,m['channel_id'],'reaction.removed',message_id=m['id'],emoji=emoji,user_id=u['id']); c.commit()
    c.close(); return j({'reactions':reactions(m['id'])})
async def dms(req):
    u=auth(req); b=await req.json(); tid=b.get('user_id')
    if tid is None: return j({'error':'user_id'},400)
    user_by_id(tid); a,bid=sorted((u['id'],int(tid))); c=db(); r=c.execute('SELECT channel_id FROM dm_pairs WHERE a=? AND b=?',(a,bid)).fetchone()
    if r: c.close(); return j({'channel_id':r['channel_id']})
    c.execute('INSERT INTO channels(name,is_dm) VALUES(?,1)',(f'dm-{a}-{bid}',)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO dm_pairs VALUES(?,?,?)',(a,bid,cid)); c.execute('INSERT INTO channel_members VALUES(?,?),(?,?)',(cid,a,cid,bid)); c.commit(); c.close(); return j({'channel_id':cid})
async def profile(req):
    u=auth(req); uid=req.match_info.get('id') or 'me'; c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(u['id'] if uid=='me' else uid,)).fetchone()
    if not r: c.close(); raise web.HTTPNotFound()
    if req.method=='GET': c.close(); return j({'user':U(r)})
    b=await req.json(); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']
    if any(k not in allowed for k in b): c.close(); return j({'error':'field'},400)
    if 'display_name' in b and (not isinstance(b['display_name'],str) or not 1<=len(b['display_name'])<=80): c.close(); return j({'error':'length'},400)
    if 'status_text' in b and (not isinstance(b['status_text'],str) or len(b['status_text'])>140): c.close(); return j({'error':'length'},400)
    if 'avatar_url' in b and (not isinstance(b['avatar_url'],str) or len(b['avatar_url'])>1024): c.close(); return j({'error':'length'},400)
    if 'status_emoji' in b and (not isinstance(b['status_emoji'],str) or len(b['status_emoji'])>64): c.close(); return j({'error':'length'},400)
    if 'timezone' in b:
        try:
            import zoneinfo
            if not isinstance(b['timezone'],str) or not b['timezone']: raise ValueError()
            zoneinfo.ZoneInfo(b['timezone'])
        except Exception:
            c.close(); return j({'error':'timezone'},400)
    sets=[x+'=?' for x in allowed if x in b]
    if sets: c.execute('UPDATE users SET '+','.join(sets)+' WHERE id=?',tuple(b[x] for x in allowed if x in b)+(u['id'],)); c.commit()
    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();
    payload={'type':'user.updated','user':U(r)}
    c.execute('INSERT INTO user_events(frame) VALUES(?)',(json.dumps(payload),)); c.commit()
    for entry in list(sockets):
        try: asyncio.create_task(entry[0].send_json(payload))
        except Exception: pass
    c.close(); return j({'user':U(r)})
async def upload(req):
    u=auth(req); rd=await req.multipart(); part=None
    async for p in rd:
        if p.name=='file': part=p; break
    if not part: return j({'error':'file'},400)
    data=await part.read()
    if len(data)>10485760: return j({'error':'large'},413)
    fid=None; c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],part.filename,part.headers.get('Content-Type','application/octet-stream'),len(data),now(),'')); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; path=f'/app/data/files/{fid}'; open(path,'wb').write(data); c.execute('UPDATE files SET path=? WHERE id=?',(path,fid)); r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone(); c.close(); return j({'file':{'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}},201)
async def file_route(req):
    auth(req); c=db(); r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    if not r: raise web.HTTPNotFound()
    f={'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
    if req.match_info.get('action')=='download': return web.Response(body=open(r['path'],'rb').read(),content_type=r['content_type'],headers={'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
    return j({'file':f})
async def groups(req):
    u=auth(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w: c.close(); raise web.HTTPNotFound()
    if req.method=='GET':
        if not c.execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): c.close(); raise web.HTTPForbidden()
        rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall(); c.close(); return j({'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rs]})
    b=await req.json(); h=b.get('handle','')
    if not re.fullmatch(r'[a-z0-9_-]{2,32}',h) or not b.get('name'): c.close(); return j({'error':'handle'},400)
    ids=b.get('user_ids',[])
    if any(not c.execute('SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?',(w['id'],x)).fetchone() for x in ids): c.close(); return j({'error':'member'},400)
    try: c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,b.get('name',''),u['id'],json.dumps(b.get('user_ids',[])))); c.commit()
    except sqlite3.IntegrityError: c.close(); return j({'error':'duplicate'},409)
    x=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone(); c.close(); return j({'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])}},201)
async def group_item(req):
    u=auth(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone(); x=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
    if not x:
        c.close()
        if req.method=='DELETE': return j({'deleted':True})
        raise web.HTTPNotFound()
    owner=w['owner_id']==u['id'] or x['creator_id']==u['id']
    if not owner: c.close(); raise web.HTTPForbidden()
    if req.method=='DELETE': c.execute('DELETE FROM groups_ WHERE id=?',(x['id'],)); c.commit(); c.close(); return j({'deleted':True})
    b=await req.json(); c.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(b.get('name',x['name']),json.dumps(b.get('user_ids',json.loads(x['user_ids']))),x['id'])); c.commit(); x=c.execute('SELECT * FROM groups_ WHERE id=?',(x['id'],)).fetchone(); c.close(); return j({'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])}})
async def ws_handler(req):
    tok=req.query.get('token',''); c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); c.close()
    if not r: raise web.HTTPUnauthorized()
    ws=web.WebSocketResponse(); await ws.prepare(req); pc=db(); pc.execute('INSERT INTO presence(user_id) VALUES(?)',(r['user_id'],)); presence_id=pc.execute('SELECT last_insert_rowid()').fetchone()[0]; pc.commit(); pc.close(); entry=[ws,r['user_id'],set(),{}]; sockets.append(entry)
    try:
        async for m in ws:
            if m.type!=WSMsgType.TEXT: continue
            b=json.loads(m.data); cid=int(b.get('channel_id')); c=db(); head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
            if b.get('type')=='subscribe': entry[2].add(cid); entry[3][cid]=head; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif b.get('type')=='resume':
                entry[2].add(cid); entry[3][cid]=b.get('since_event_id',0)
                for x in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,b.get('since_event_id',0))): await ws.send_str(x['frame'])
                await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            c.close()
    finally:
        if entry in sockets: sockets.remove(entry)
        pc=db(); pc.execute('DELETE FROM presence WHERE id=?',(presence_id,)); pc.commit(); pc.close()
    return ws
app=web.Application(client_max_size=11*1024*1024)
app.on_startup.append(startup); app.on_cleanup.append(cleanup)
app.router.add_get('/api/health',health); app.router.add_post('/api/auth/register',register); app.router.add_post('/api/auth/login',login); app.router.add_get('/api/auth/me',me)
app.router.add_get('/api/workspaces',workspaces); app.router.add_post('/api/workspaces',create_ws); app.router.add_get('/api/workspaces/{slug}',ws_detail); app.router.add_post('/api/workspaces/{slug}/channels',create_ch)
app.router.add_post('/api/channels/{id}/join',join); app.router.add_get('/api/channels/{id}/members',members); app.router.add_get('/api/channels/{id}/messages',messages); app.router.add_post('/api/channels/{id}/messages',post_msg)
app.router.add_get('/api/messages/{id}/replies',replies); app.router.add_post('/api/messages/{id}/reactions',react); app.router.add_delete('/api/messages/{id}/reactions/{emoji}',react)
app.router.add_post('/api/dms',dms); app.router.add_get('/api/users/{id}',profile); app.router.add_patch('/api/users/me',profile); app.router.add_post('/api/files',upload); app.router.add_get('/api/files/{id}',file_route); app.router.add_get('/api/files/{id}/{action}',file_route)
app.router.add_get('/api/workspaces/{slug}/groups',groups); app.router.add_post('/api/workspaces/{slug}/groups',groups); app.router.add_patch('/api/workspaces/{slug}/groups/{handle}',group_item); app.router.add_delete('/api/workspaces/{slug}/groups/{handle}',group_item); app.router.add_get('/api/ws',ws_handler)
if __name__=='__main__': web.run_app(app,host='127.0.0.1',port=PORT)

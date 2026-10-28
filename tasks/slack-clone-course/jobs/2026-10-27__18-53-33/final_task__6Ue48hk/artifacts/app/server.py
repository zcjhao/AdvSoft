import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
PORT=int(sys.argv[2]) if len(sys.argv)>2 else 8000
DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=10,isolation_level=None,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000')
    return c
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER,mentions TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,UNIQUE(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));''')
    c.close()
init()
def resp(x,status=200): return web.json_response(x,status=status)
def err(status): return resp({'error': 'error'},status)
def userj(u):
    return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '):
        q=req.query.get('token','')
        h='Bearer '+q if q else ''
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
async def au(req):
    u=auth(req)
    if not u: raise web.HTTPUnauthorized()
    return u
async def body(req):
    try:return await req.json()
    except:return {}
def wsrow(c,slug): return c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
def role(c,w,u):
    r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone()
    return r['role'] if r else None
def chan(c,cid): return c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
def canread(c,ch,uid):
    return not ch['is_private'] and role(c,ch['workspace_id'],uid) is not None or c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],uid)).fetchone() is not None
def chj(ch): return {k: (bool(ch[k]) if k in ('is_private','is_dm','is_archived') else ch[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def msgj(c,m):
    au=c.execute('SELECT * FROM users WHERE id=?',(m['author_id'],)).fetchone()
    rs=[]
    for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(m['id'],)):
        rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]})
    replies=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],)).fetchone()['n']
    return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':userj(au),'body':m['body'],'parent_id':m['parent_id'],'reply_count':replies,'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[],'reactions':rs,'mentions':json.loads(m['mentions'] or '[]')}
def page(args,total):
    raw=args.get('limit','50')
    try:n=int(raw)
    except: raise ValueError()
    if n<1 or n>200: raise ValueError()
    cur=args.get('cursor')
    if cur and not cur.isdigit(): raise ValueError()
    start=int(cur) if cur else 0
    return n,start
def event(c,cid,kind,payload):
    c.execute('BEGIN IMMEDIATE')
    h=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    c.execute('INSERT INTO events(channel_id,event_id,kind,payload,created_at) VALUES(?,?,?,?,?)',(cid,h,kind,json.dumps(payload),now()))
    c.execute('COMMIT'); return h
def mentions(c,body,ch,author):
    out=[]
    if '@channel' in body:
        out += [r['user_id'] for r in c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(ch['id'],author))]
    for name in re.findall(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_]+)',body):
        u=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
        if u and u['id']!=author and (role(c,ch['workspace_id'],u['id']) or c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()):
            out.append(u['id'])
    return list(dict.fromkeys(out))
async def health(req): return resp({'status':'ok','node_id':NODE})
async def register(req):
    b=await body(req); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err(400)
    c=db()
    try:
        c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un))
    except sqlite3.IntegrityError:c.close();return err(409)
    uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close()
    return resp({'user':userj(u),'token':t},201)
async def login(req):
    b=await body(req); c=db();u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
    if not u:c.close();return err(401)
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.close();return resp({'user':userj(u),'token':t})
async def me(req): return resp({'user':userj(await au(req))})
async def workspaces(req):
    u=await au(req);c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return resp({'workspaces':[dict(r) for r in rows]})
async def create_ws(req):
    u=await au(req);b=await body(req)
    if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return err(400)
    c=db()
    try:
        c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',''),u['id']))
    except sqlite3.IntegrityError:c.close();return err(409)
    wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=chan(c,cid);c.close();return resp({'workspace':dict(w),'general_channel':chj(ch)},201)
async def get_ws(req):
    u=await au(req);c=db();w=wsrow(c,req.match_info['slug'])
    if not w:c.close();return err(404)
    inc=req.query.get('include_archived') in ('1','true')
    rows=[]
    for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],int(inc))):
        if canread(c,x,u['id']): rows.append(chj(x))
    rs=[]
    for x in c.execute('SELECT id FROM channels WHERE workspace_id=?',(w['id'],)):
        ch=chan(c,x['id'])
        if not canread(c,ch,u['id']): continue
        rr=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone()
        last=rr['last_read_event_id'] if rr else 0
        unread=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND author_id!=? AND event_id>?',(x['id'],u['id'],last)).fetchall()
        mc=sum(1 for m in unread if u['id'] in json.loads(m['mentions'] or '[]'))
        rs.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':len(unread),'mention_count':mc})
    c.close();return resp({'workspace':dict(w),'channels':rows,'read_state':rs})
async def members(req):
    u=await au(req);c=db();w=wsrow(c,req.match_info['slug'])
    if not w:c.close();return err(404)
    out=[dict(x) for x in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))];c.close();return resp({'members':out})
async def patch_ws(req):
    u=await au(req);b=await body(req);c=db();w=wsrow(c,req.match_info['slug'])
    if not w:return err(404)
    if role(c,w['id'],u['id']) not in ('owner','admin'):return err(403)
    if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return err(400)
    c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(b.get('name'),b.get('join_mode'),w['id']));w=wsrow(c,w['slug']);c.close();return resp({'workspace':dict(w)})
async def channels(req):
    u=await au(req);b=await body(req);c=db();w=wsrow(c,req.match_info['slug'])
    if not w:return err(404)
    r=role(c,w['id'],u['id'])
    if r not in ('owner','admin','member') or (b.get('is_private',False) and r not in ('owner','admin')):return err(403)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return err(400)
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')))
    except sqlite3.IntegrityError:return err(409)
    cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));x=chan(c,cid);c.close();return resp({'channel':chj(x)},201)
async def join(req):
    u=await au(req);c=db();x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    r=role(c,x['workspace_id'],u['id']);w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
    if x['is_private'] or r=='guest' or (not r and w['join_mode']=='invite_only'):return err(403)
    if not r:c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone()
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(x['id'],u['id']))
    if not existed:
        event(c,x['id'],'member.joined',{'user_id':u['id'],'channel_id':x['id']})
    c.close();return resp({'channel':chj(x)})
async def leave(req):
    u=await au(req);c=db();x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone()
    c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id']))
    if existed:
        event(c,x['id'],'member.left',{'user_id':u['id'],'channel_id':x['id']})
    c.close();return resp({'channel':chj(x)})
async def patch_channel(req):
    u=await au(req); c=db(); x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    if 'topic' not in (b:=await body(req)) or len(b.get('topic',''))>250:return err(400)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone() and role(c,x['workspace_id'],u['id'])!='owner':return err(403)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),x['id']))
    x=chan(c,x['id']); event(c,x['id'],'channel.updated',{'channel':chj(x)}); c.close()
    return resp({'channel':chj(x)})
async def channel_members(req):
    u=await au(req); c=db(); x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    if not canread(c,x,u['id']):return err(403)
    out=[userj(z) for z in c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(x['id'],))]
    c.close(); return resp({'members':out})
async def archive_channel(req):
    u=await au(req); c=db(); x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    if role(c,x['workspace_id'],u['id'])!='owner':return err(403)
    c.execute('UPDATE channels SET is_archived=1 WHERE id=?',(x['id'],)); x=chan(c,x['id'])
    event(c,x['id'],'channel.archived',{'channel':chj(x)}); c.close(); return resp({'channel':chj(x)})
async def unarchive_channel(req):
    u=await au(req); c=db(); x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    if role(c,x['workspace_id'],u['id'])!='owner':return err(403)
    c.execute('UPDATE channels SET is_archived=0 WHERE id=?',(x['id'],)); x=chan(c,x['id'])
    event(c,x['id'],'channel.unarchived',{'channel':chj(x)}); c.close(); return resp({'channel':chj(x)})
async def messages(req):
    u=await au(req); c=db(); x=chan(c,int(req.match_info['id']))
    if not x or not canread(c,x,u['id']): c.close(); return err(404 if not x else 403)
    try: n,_=page(req.query,None)
    except: c.close(); return err(400)
    cur=req.query.get('cursor')
    if cur is not None and not cur.isdigit(): c.close(); return err(400)
    if cur is None:
        rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(x['id'],n+1)).fetchall()
    else:
        rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(x['id'],int(cur),n+1)).fetchall()
    out=[msgj(c,r) for r in rows[:n]]
    nxt=str(rows[n-1]['id']) if len(rows)>n else None
    c.close(); return resp({'messages':out,'next_cursor':nxt})
async def postmsg(req):
    u=await au(req);b=await body(req);c=db();x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    if not canread(c,x,u['id']):return err(403)
    if x['is_archived']:return err(423)
    text=b.get('body','')
    if not isinstance(text,str) or not text.strip():return err(400)
    if text.startswith('/') and not text.startswith('//') and text.split()[0] not in ('/me','/shrug'):return err(400)
    if text.startswith('/shrug'):text=(text[6:].lstrip()+' ¯\\\\_(ツ)_/¯').strip()
    mn=mentions(c,text,x,u['id']); parent=b.get('parent_id');ts=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(x['id'],u['id'],text,parent,ts,json.dumps(mn)));mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();kind='message.reply' if parent else 'message.created';e=event(c,x['id'],kind,{'message':msgj(c,m),'channel_id':x['id']});c.execute('UPDATE messages SET event_id=? WHERE id=?',(e,mid));m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close();return resp({'message':msgj(c if False else db(),m)},201)
async def edit(req):
    u=await au(req); b=await body(req); c=db()
    m=c.execute('SELECT m.*,ch.workspace_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(req.match_info['id'],)).fetchone()
    if not m:return err(404)
    if m['deleted']:return err(409)
    if m['author_id']!=u['id']:return err(403)
    if not isinstance(b.get('body'),str) or not b['body'].strip():return err(400)
    x=chan(c,m['channel_id']); mn=mentions(c,b['body'],x,u['id'])
    c.execute('UPDATE messages SET body=?,edited_at=?,mentions=? WHERE id=?',(b['body'],now(),json.dumps(mn),m['id']))
    m=c.execute('SELECT * FROM messages WHERE id=?',(m['id'],)).fetchone()
    event(c,x['id'],'message.updated',{'message':msgj(c,m)}); out=msgj(c,m); c.close()
    return resp({'message':out})
async def delete(req):
    u=await au(req);c=db();m=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not m:return resp({'deleted':True})
    x=chan(c,m['channel_id']);r=role(c,x['workspace_id'],u['id'])
    if m['author_id']!=u['id'] and r!='owner':return err(403)
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(m['id'],));event(c,x['id'],'message.deleted',{'message_id':m['id'],'channel_id':x['id']});c.close();return resp({'deleted':True})
async def replies(req):
    u=await au(req); c=db()
    parent=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not parent:c.close();return err(404)
    x=chan(c,parent['channel_id'])
    if not canread(c,x,u['id']):c.close();return err(403)
    try:n,_=page(req.query,None)
    except:c.close();return err(400)
    cur=req.query.get('cursor')
    if cur is not None and not cur.isdigit():c.close();return err(400)
    if cur is None: rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?',(parent['id'],n+1)).fetchall()
    else: rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id LIMIT ?',(parent['id'],int(cur),n+1)).fetchall()
    out=[msgj(c,r) for r in rows[:n]]
    nxt=str(rows[n-1]['id']) if len(rows)>n else None
    c.close();return resp({'replies':out,'next_cursor':nxt})
async def react(req):
    u=await au(req);b=await body(req);c=db();m=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not m:return err(404)
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(m['id'],u['id'],b.get('emoji','')));x=chan(c,m['channel_id']);event(c,x['id'],'reaction.added',{'message_id':m['id'],'emoji':b.get('emoji','')});fresh=c.execute('SELECT * FROM messages WHERE id=?',(m['id'],)).fetchone();out=msgj(c,fresh)['reactions'];c.close();return resp({'reactions':out})
async def unreact(req):
    u=await au(req);c=db();m=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],req.match_info['emoji']));event(c,m['channel_id'],'reaction.removed',{'message_id':m['id'],'emoji':req.match_info['emoji']});fresh=c.execute('SELECT * FROM messages WHERE id=?',(m['id'],)).fetchone();out=msgj(c,fresh)['reactions'];c.close();return resp({'reactions':out})
async def pins(req):
    u=await au(req);c=db();x=chan(c,int(req.match_info['id']))
    if not x:return err(404)
    if not canread(c,x,u['id']):return err(403)
    out=[]
    for p in c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(x['id'],)):
        out.append({'message':msgj(c,p),'pinned_by':p['pinned_by'],'pinned_at':p['pinned_at']})
    c.close();return resp({'pins':out,'next_cursor':None})
async def pin(req):
    u=await au(req);c=db();m=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not m:return err(404)
    t=now();c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(m['id'],u['id'],t));p=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone();c.close();return resp({'pin':dict(p)})
async def unpin(req):
    u=await au(req);c=db();c.execute('DELETE FROM pins WHERE message_id=?',(req.match_info['id'],));c.close();return resp({'unpinned':True})
async def search(req):
    u=await au(req); q=req.query.get('q','').lower(); c=db()
    try:n,_=page(req.query,None)
    except:c.close();return err(400)
    cur=req.query.get('cursor')
    if cur is not None and not cur.isdigit():c.close();return err(400)
    wid=req.query.get('workspace_id')
    params=[f'%{q}%']; where='lower(m.body) LIKE ? AND m.deleted=0'
    if wid:
        where+=' AND ch.workspace_id=?'; params.append(int(wid))
    if cur is not None:
        where+=' AND m.id<?'; params.append(int(cur))
    rows=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE '+where+' ORDER BY m.id DESC',params).fetchall()
    visible=[r for r in rows if canread(c,chan(c,r['channel_id']),u['id'])]
    out=[msgj(c,r) for r in visible[:n]]
    nxt=str(visible[n-1]['id']) if len(visible)>n else None
    c.close();return resp({'results':out,'next_cursor':nxt})
async def patch_member(req):
    u=await au(req);b=await body(req);c=db();w=wsrow(c,req.match_info['slug']);target=int(req.match_info['user_id']);rr=role(c,w['id'],u['id']) if w else None
    if not w:return err(404)
    old=role(c,w['id'],target)
    if old is None:return err(404)
    if b.get('role') not in ('owner','admin','member','guest'):return err(400)
    if b['role']=='owner':return err(400)
    if rr not in ('owner','admin') or (rr=='admin' and old in ('owner','admin')) or target==u['id'] and old=='owner':return err(403)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(b['role'],w['id'],target));c.close();return resp({'member':{'user_id':target,'role':b['role']}})
async def transfer(req):
    u=await au(req);b=await body(req);c=db();w=wsrow(c,req.match_info['slug']);t=int(b.get('new_owner_id',0))
    if not w:return err(404)
    if role(c,w['id'],u['id'])!='owner':return err(403)
    if role(c,w['id'],t)!='admin':return err(400)
    c.execute('UPDATE members SET role=CASE WHEN user_id=? THEN "owner" ELSE "admin" END WHERE workspace_id=? AND user_id IN (?,?)',(t,w['id'],u['id'],t));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(t,w['id']));c.close();return resp({'workspace':dict(w)})
async def invite(req):
    u=await au(req);b=await body(req);c=db();w=wsrow(c,req.match_info['slug'])
    if not w:return err(404)
    if role(c,w['id'],u['id']) not in ('owner','admin'):return err(403)
    if b.get('max_uses',1)<1:return err(400)
    code=secrets.token_urlsafe(10); exp=b.get('expires_in_seconds'); ea=(datetime.now(timezone.utc).timestamp()+exp if exp else 0)
    tid=b.get('invited_user_id'); named=b.get('invited_username')
    if named:
        z=c.execute('SELECT id FROM users WHERE username=?',(named,)).fetchone();tid=z['id'] if z else None
    uses=1 if tid else b.get('max_uses',1)
    c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],str(ea),uses,0,tid,0));r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone();c.close();return resp({'invitation':dict(r)},201)
async def invlist(req):
    u=await au(req);c=db();w=wsrow(c,req.match_info['slug'])
    if not w:return err(404)
    if role(c,w['id'],u['id']) not in ('owner','admin'):return err(403)
    r=[dict(x) for x in c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses',(w['id'],))];c.close();return resp({'invitations':r})
async def accept(req):
    u=await au(req);c=db();r=c.execute('SELECT * FROM invitations WHERE code=?',(req.match_info['code'],)).fetchone()
    if not r or r['revoked'] or r['uses']>=r['max_uses'] or (float(r['expires_at']) and float(r['expires_at'])<datetime.now(timezone.utc).timestamp()):return err(404)
    if r['invited_user_id'] and r['invited_user_id']!=u['id']:return err(403)
    c.execute('BEGIN IMMEDIATE')
    fresh=c.execute('SELECT * FROM invitations WHERE code=?',(r['code'],)).fetchone()
    if not fresh or fresh['revoked'] or fresh['uses']>=fresh['max_uses']:
        c.execute('ROLLBACK'); c.close(); return err(404)
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(fresh['workspace_id'],u['id'],'member'))
    c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(fresh['code'],))
    c.execute('COMMIT'); c.close(); return resp({'workspace_id':fresh['workspace_id']})
async def revoke_invitation(req):
    u=await au(req); c=db(); w=wsrow(c,req.match_info['slug'])
    if not w:return err(404)
    if role(c,w['id'],u['id']) not in ('owner','admin'): c.close(); return err(403)
    r=c.execute('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(req.match_info['code'],w['id'])).fetchone()
    if not r:c.close();return err(404)
    c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(r['code'],));c.close()
    return resp({'deleted':True})
async def readstate(req):
    u=await au(req)
    c=db()
    cid=int(req.match_info['id'])
    if not chan(c,cid):
        c.close()
        return err(404)
    r=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    v=r['last_read_event_id'] if r else 0
    head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    unread=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND author_id!=? AND event_id>?',(cid,u['id'],v)).fetchall()
    mc=sum(1 for m in unread if u['id'] in json.loads(m['mentions'] or '[]'))
    c.close()
    return resp({'read_state':{'channel_id':cid,'last_read_event_id':v,'unread_count':len(unread),'mention_count':mc}})
async def markread(req):
    u=await au(req)
    b=await body(req)
    c=db()
    cid=int(req.match_info['id'])
    if not chan(c,cid):
        c.close()
        return err(404)
    v=b.get('last_read_event_id')
    if not isinstance(v,int) or v<0:
        c.close()
        return err(400)
    head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    v=min(v,head)
    old=c.execute('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    v=max(v,old['last_read_event_id'] if old else 0)
    c.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(u['id'],cid,v))
    c.close()
    return resp({'read_state':{'channel_id':cid,'last_read_event_id':v,'unread_count':0,'mention_count':0}})
async def ws_handler(req):
    if not auth(req):return err(401)
    ws=web.WebSocketResponse();await ws.prepare(req); last=0; subs=set()
    async def poll():
        nonlocal last
        while not ws.closed:
            c=db()
            for e in c.execute('SELECT * FROM events WHERE id>? ORDER BY id',(last,)):
                last=e['id']
                if e['channel_id'] in subs: await ws.send_json({'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],**json.loads(e['payload'])})
            c.close();await asyncio.sleep(.05)
    task=asyncio.create_task(poll())
    try:
        async for m in ws:
            if m.type==WSMsgType.TEXT:
                b=json.loads(m.data);cid=int(b.get('channel_id',0));c=db();x=chan(c,cid);u=auth(req)
                if not x or not canread(c,x,u['id']):await ws.send_json({'type':'error'});continue
                if b.get('type')=='subscribe':
                    subs.add(cid);h=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
                elif b.get('type')=='resume':
                    since=int(b.get('since_event_id',0)); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since,)).fetchall()
                    for e in rows: await ws.send_json({'type':e['kind'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
                    h=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':h})
                c.close()
    finally:task.cancel()
    return ws
app=web.Application()
app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
app.router.add_get('/api/workspaces',workspaces);app.router.add_post('/api/workspaces',create_ws);app.router.add_get('/api/workspaces/{slug}',get_ws);app.router.add_get('/api/workspaces/{slug}/members',members);app.router.add_patch('/api/workspaces/{slug}',patch_ws);app.router.add_post('/api/workspaces/{slug}/channels',channels);app.router.add_patch('/api/workspaces/{slug}/members/{user_id}',patch_member);app.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer)
app.router.add_post('/api/workspaces/{slug}/invitations',invite);app.router.add_get('/api/workspaces/{slug}/invitations',invlist);app.router.add_delete('/api/workspaces/{slug}/invitations/{code}',revoke_invitation);app.router.add_post('/api/invitations/{code}/accept',accept)
app.router.add_post('/api/channels/{id}/join',join);app.router.add_post('/api/channels/{id}/leave',leave);app.router.add_patch('/api/channels/{id}',patch_channel);app.router.add_get('/api/channels/{id}/members',channel_members);app.router.add_post('/api/channels/{id}/archive',archive_channel);app.router.add_post('/api/channels/{id}/unarchive',unarchive_channel);app.router.add_get('/api/channels/{id}/messages',messages);app.router.add_post('/api/channels/{id}/messages',postmsg);app.router.add_get('/api/channels/{id}/pins',pins);app.router.add_get('/api/channels/{id}/read',readstate);app.router.add_post('/api/channels/{id}/read',markread)
app.router.add_patch('/api/messages/{id}',edit);app.router.add_delete('/api/messages/{id}',delete);app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_post('/api/messages/{id}/reactions',react);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);app.router.add_post('/api/messages/{id}/pin',pin);app.router.add_delete('/api/messages/{id}/pin',unpin);app.router.add_get('/api/search',search);app.router.add_get('/api/ws',ws_handler)
web.run_app(app,host='127.0.0.1',port=PORT)

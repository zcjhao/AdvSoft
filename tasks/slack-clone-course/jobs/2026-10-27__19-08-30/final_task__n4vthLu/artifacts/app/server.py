import asyncio, base64, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
PORT=int(sys.argv[2]) if len(sys.argv)>2 else 8000
DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=10,isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    '''); c.close()
init()

def resp(data,status=200): return web.json_response(data,status=status)
def userrow(uid):
    c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return r
def ujson(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(h[7:],)).fetchone(); c.close()
    return r['user_id'] if r else None
def channel(cid):
    c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return r
def member(cid,uid):
    c=db(); x=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.close(); return bool(x)
def wsrole(wid,uid):
    c=db(); x=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone(); c.close(); return x['role'] if x else None
def canread(ch,uid):
    return bool(ch and (not ch['is_private'] and wsrole(ch['workspace_id'],uid) or member(ch['id'],uid)))
def cj(ch):
    return {'id':ch['id'],'workspace_id':ch['workspace_id'],'name':ch['name'],'is_private':bool(ch['is_private']),'is_dm':bool(ch['is_dm']),'topic':ch['topic'] or '','is_archived':bool(ch['is_archived'])}
def err(status,msg='error'): return resp({'error':msg},status)
def lim(req):
    s=req.query.get('limit')
    if s is None:return 50
    try:n=int(s)
    except: raise web.HTTPBadRequest()
    if n<1 or n>200: raise web.HTTPBadRequest()
    return n
def enc(pos): return base64.urlsafe_b64encode(json.dumps({'p':pos}).encode()).decode().rstrip('=')
def dec(s):
    try:
        x=json.loads(base64.urlsafe_b64decode(s+'='*((4-len(s)%4)%4)))
        if not isinstance(x.get('p'),int): raise ValueError()
        return x['p']
    except: raise web.HTTPBadRequest()
def event(cid,kind,payload):
    c=db(); c.execute('BEGIN IMMEDIATE')
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    frame={'type':kind,'event_id':n,'channel_id':cid}; frame.update(payload)
    c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,kind,json.dumps(frame))); c.commit(); c.close()
    return n
def msgjson(r):
    c=db(); a=userrow(r['author_id'])
    rr=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY emoji,user_id',(r['id'],)).fetchall()
    groups={}
    for x in rr: groups.setdefault(x['emoji'],[]).append(x['user_id'])
    cnt=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    c.close()
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':ujson(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':cnt,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in groups.items()],'mentions':json.loads(r['mentions'] or '[]')}
def resolve(body,ch,author):
    c=db(); out=[]; users=c.execute('SELECT u.id,u.username FROM users u JOIN wm ON wm.user_id=u.id WHERE wm.workspace_id=?',(ch['workspace_id'],)).fetchall()
    names={x['username']:x['id'] for x in users}
    for n in re.findall(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_]+)',body):
        if n=='channel':
            ids=[x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(ch['id'],)).fetchall()]
        else: ids=[names[n]] if n in names and member(ch['id'],names[n]) else []
        for i in ids:
            if i!=author and i not in out: out.append(i)
    c.close(); return out
async def body(req):
    try:return await req.json()
    except:return {}
async def register(req):
    b=await body(req); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err(400)
    c=db()
    try:
        c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(un,pw,b.get('display_name') or un,'UTC','','',''))
        uid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.close()
        return resp({'user':ujson(userrow(uid)),'token':tok},201)
    except sqlite3.IntegrityError:c.close(); return err(409)
async def login(req):
    b=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
    if not r:c.close(); return err(401)
    t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.close(); return resp({'user':ujson(r),'token':t})
async def health(req): return resp({'status':'ok','node_id':NODE})
async def workspaces(req):
    uid=auth(req)
    if not uid:return err(401)
    c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN wm ON wm.workspace_id=w.id WHERE wm.user_id=?',(uid,)).fetchall(); c.close()
    return resp({'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rows]})
async def create_ws(req):
    uid=auth(req)
    if not uid:return err(401)
    b=await body(req)
    if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return err(400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',''),uid)); wid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,uid,'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO cm VALUES(?,?)',(cid,uid)); c.commit(); c.close()
        return resp({'workspace':{'id':wid,'slug':b['slug'],'name':b.get('name',''),'owner_id':uid,'join_mode':'open'},'general_channel':cj(channel(cid))},201)
    except sqlite3.IntegrityError:c.rollback();c.close();return err(409)
async def workspace_detail(req, slug=None):
    uid=auth(req)
    if not uid:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug if slug is not None else req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if not wsrole(w['id'],uid):return err(403)
    rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],req.query.get('include_archived') in ('1','true'))).fetchall(); c.close()
    rows=[x for x in rows if (not x['is_private'] or member(x['id'],uid))]
    states=[readstate(uid,x['id']) for x in rows]
    return resp({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[cj(x) for x in rows],'read_state':states})
async def create_channel(req, slug=None):
    uid=auth(req)
    if not uid:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug if slug is not None else req.match_info['slug'],)).fetchone(); c.close()
    if not w:return err(404)
    role=wsrole(w['id'],uid)
    if not role:return err(403)
    b=await body(req); name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32:return err(400)
    if b.get('is_private') and role not in ('owner','admin'):return err(403)
    if len(b.get('topic',''))>250:return err(400)
    c=db()
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']; c.execute('INSERT INTO cm VALUES(?,?)',(cid,uid)); c.close(); return resp({'channel':cj(channel(cid))},201)
    except sqlite3.IntegrityError:c.close();return err(409)
async def join(req, cid=None):
    uid=auth(req); ch=channel(cid if cid is not None else req.match_info['id'])
    if not uid:return err(401)
    if not ch:return err(404)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); c.close()
    if ch['is_private'] and not member(ch['id'],uid):return err(403)
    if not wsrole(w['id'],uid):
        if w['join_mode']!='open':return err(403)
        c=db();c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(w['id'],uid,'member'));c.close()
    c=db();c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],uid));c.close();event(ch['id'],'member.joined',{'user_id':uid});return resp({'channel':cj(channel(ch['id']))})
async def channel_action(req):
    uid=auth(req); ch=channel(req.match_info['id'])
    if not uid:return err(401)
    if not ch:return err(404)
    if req.match_info['action']=='members':
        c=db(); xs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall();c.close();return resp({'members':[ujson(x) for x in xs]})
    if not canread(ch,uid):return err(403)
    act=req.match_info['action']
    if act=='patch':
        b=await body(req)
        if len(b.get('topic',''))>250:return err(400)
        if not member(ch['id'],uid) and wsrole(ch['workspace_id'],uid)!='owner':return err(403)
        c=db(); c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),ch['id'])); c.close(); event(ch['id'],'channel.updated',{'channel':cj(channel(ch['id']))}); return resp({'channel':cj(channel(ch['id']))})
    if act=='leave':
        c=db();c.execute('DELETE FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],uid));c.close();event(ch['id'],'member.left',{'user_id':uid});return resp({'channel':cj(channel(ch['id']))})
    if act=='topic':
        b=await body(req)
        if len(b.get('topic',''))>250:return err(400)
        c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),ch['id']));c.close();event(ch['id'],'channel.updated',{'channel':cj(channel(ch['id']))});return resp({'channel':cj(channel(ch['id']))})
    if act in ('archive','unarchive'):
        if wsrole(ch['workspace_id'],uid)!='owner':return err(403)
        val=act=='archive';c=db();c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));c.close();event(ch['id'],'channel.archived' if val else 'channel.unarchived',{'channel':cj(channel(ch['id']))});return resp({'channel':cj(channel(ch['id']))})
async def messages(req):
    uid=auth(req); ch=channel(req.match_info['id'])
    if not uid:return err(401)
    if not ch:return err(404)
    if not canread(ch,uid):return err(403)
    n=lim(req); cur=dec(req.query['cursor']) if req.query.get('cursor') else None
    c=db(); q='SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC'; rows=c.execute(q,(ch['id'],)).fetchall();c.close()
    start=next((i for i,x in enumerate(rows) if cur is None or x['id']<cur),len(rows)); page=rows[start:start+n]; nxt=enc(page[-1]['id']) if len(page)==n and start+n<len(rows) else None
    return resp({'messages':[msgjson(x) for x in page],'next_cursor':nxt})
async def post_message(req):
    uid=auth(req); ch=channel(req.match_info['id'])
    if not uid:return err(401)
    if not ch:return err(404)
    if not canread(ch,uid):return err(403)
    if ch['is_archived']:return err(423)
    b=await body(req); text=b.get('body')
    if not isinstance(text,str) or not text.strip():return err(400)
    parent=b.get('parent_id'); c=db()
    if parent and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,ch['id'])).fetchone():c.close();return err(404)
    mentions=resolve(text,ch,uid); t=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(ch['id'],uid,text,parent,t,json.dumps(mentions))); mid=c.execute('SELECT last_insert_rowid() x').fetchone()['x'];c.close(); eid=event(ch['id'],'message.reply' if parent else 'message.created',{'message':msgjson(db().execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())}); return resp({'message':msgjson(db().execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())},201)
async def message_ops(req):
    uid=auth(req); c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    if not uid:return err(401)
    if not r:return err(404)
    ch=channel(r['channel_id'])
    if req.method=='GET' and req.match_info.get('op')=='replies':
        if not canread(ch,uid):return err(403)
        n=lim(req); cur=dec(req.query['cursor']) if req.query.get('cursor') else None;c=db(); rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(r['id'],)).fetchall();c.close(); start=0 if cur is None else next((i for i,x in enumerate(rows) if x['id']>cur),len(rows));p=rows[start:start+n];return resp({'replies':[msgjson(x) for x in p],'next_cursor':enc(p[-1]['id']) if len(p)==n and start+n<len(rows) else None})
    if req.method=='PATCH':
        if r['deleted']:return err(409)
        if r['author_id']!=uid:return err(403)
        b=await body(req)
        if not isinstance(b.get('body'),str) or not b['body'].strip():return err(400)
        mentions=resolve(b['body'],ch,uid);c=db();c.execute('UPDATE messages SET body=?,edited_at=?,mentions=? WHERE id=?',(b['body'],now(),json.dumps(mentions),r['id']));c.close(); x=db().execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();event(ch['id'],'message.updated',{'message':msgjson(x)});return resp({'message':msgjson(x)})
    if req.method=='DELETE':
        if r['author_id']!=uid and wsrole(ch['workspace_id'],uid)!='owner':return err(403)
        c=db();c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));c.close();event(ch['id'],'message.deleted',{'message_id':r['id']});return resp({'deleted':True})
    return err(404)
async def reactions(req):
    uid=auth(req); c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    if not uid:return err(401)
    if not r:return err(404)
    emoji=req.match_info.get('emoji')
    if req.method=='POST': emoji=(await body(req)).get('emoji')
    c=db()
    if req.method=='POST':c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(r['id'],uid,emoji));kind='reaction.added'
    else:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],uid,emoji));kind='reaction.removed'
    c.close(); x=db().execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();event(r['channel_id'],kind,{'message_id':r['id'],'emoji':emoji});return resp({'reactions':msgjson(x)['reactions']})

def readstate(uid,cid):
    c=db(); row=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone()
    last=row['last_read'] if row else 0
    ev=c.execute("SELECT payload FROM events WHERE channel_id=? AND event_id>? AND kind IN ('message.created','message.reply') ORDER BY event_id",(cid,last)).fetchall()
    unread=mentions=0
    for e in ev:
        try:
            m=json.loads(e['payload']).get('message') or {}
            if m.get('author_id')!=uid:
                unread+=1
                if uid in (m.get('mentions') or []): mentions+=1
        except: pass
    c.close(); return {'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
async def search_api(req):
    uid=auth(req)
    if not uid:return err(401)
    q=req.query.get('q','').lower(); n=lim(req); cur=dec(req.query['cursor']) if req.query.get('cursor') else None
    wid=req.query.get('workspace_id'); c=db()
    rows=c.execute('SELECT m.*,ch.workspace_id,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE ? ORDER BY m.id DESC',('%'+q+'%',)).fetchall(); c.close()
    rows=[r for r in rows if canread(channel(r['channel_id']),uid) and (wid is None or str(r['workspace_id'])==str(wid))]
    if cur is not None: rows=[r for r in rows if r['id']<cur]
    page=rows[:n]; return resp({'results':[msgjson(x) for x in page],'next_cursor':enc(page[-1]['id']) if len(page)==n and len(rows)>n else None})
async def pin_api(req):
    uid=auth(req); mid=int(req.match_info['id']); c=db(); m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone(); c.close()
    if not uid:return err(401)
    if not m:return err(404)
    ch=channel(m['channel_id'])
    if not canread(ch,uid):return err(403)
    if req.method=='POST':
        t=now(); c=db(); c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,uid,t)); x=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone(); c.close()
        return resp({'pin':{'message_id':mid,'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}})
    c=db(); c.execute('DELETE FROM pins WHERE message_id=?',(mid,)); c.close(); return resp({'unpinned':True})
async def pins_list(req):
    uid=auth(req); ch=channel(req.match_info['id'])
    if not uid:return err(401)
    if not ch:return err(404)
    if not canread(ch,uid):return err(403)
    n=lim(req); cur=dec(req.query['cursor']) if req.query.get('cursor') else None; c=db()
    rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0 ORDER BY p.pinned_at DESC,p.message_id DESC',(ch['id'],)).fetchall(); c.close()
    if cur is not None: rows=[x for x in rows if x['message_id']<cur]
    page=rows[:n]; return resp({'pins':[{'message':msgjson(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in page],'next_cursor':enc(page[-1]['message_id']) if len(page)==n and len(rows)>n else None})

async def api(req):
    path=req.path
    if path=='/api/health':return await health(req)
    if path=='/api/auth/register':return await register(req)
    if path=='/api/auth/login':return await login(req)
    uid=auth(req)
    if path=='/api/auth/me':
        return err(401) if not uid else resp({'user':ujson(userrow(uid))})
    if path=='/api/workspaces' and req.method=='GET':return await workspaces(req)
    if path=='/api/workspaces' and req.method=='POST':return await create_ws(req)
    if path.startswith('/api/workspaces/') and path.count('/')==3:return await workspace_detail(req, path.split('/')[3])
    if path.startswith('/api/workspaces/') and path.endswith('/channels'):return await create_channel(req, path.split('/')[3])
    if path=='/api/search':
        return await search_api(req)
    m=re.match(r'/api/channels/(\d+)/pins$',path)
    if m:
        req.match_info['id']=m.group(1); return await pins_list(req)
    m=re.match(r'/api/messages/(\d+)/pin$',path)
    if m:
        req.match_info['id']=m.group(1); return await pin_api(req)
    if path.startswith('/api/channels/') and path.endswith('/messages'):
        req.match_info['id']=path.split('/')[3]
        return await (post_message(req) if req.method=='POST' else messages(req))
    m=re.match(r'/api/channels/(\d+)/read$',path)
    if m:
        req.match_info['id']=m.group(1)
        ch=channel(m.group(1))
        if not uid:return err(401)
        if not ch:return err(404)
        if not canread(ch,uid):return err(403)
        if req.method=='GET': return resp({'read_state':readstate(uid,ch['id'])})
        b=await body(req); v=b.get('last_read_event_id')
        if not isinstance(v,int) or v<0:return err(400)
        c=db(); head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']; c.execute('INSERT INTO reads(user_id,channel_id,last_read) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read=MAX(last_read,excluded.last_read)',(uid,ch['id'],min(v,head))); c.commit(); c.close()
        return resp({'read_state':readstate(uid,ch['id'])})
    if path.startswith('/api/messages/') and path.endswith('/replies'):
        req.match_info['id']=path.split('/')[3]; req.match_info['op']='replies'; return await message_ops(req)
    if '/reactions/' in path or path.endswith('/reactions'):
        req.match_info['id']=path.split('/')[3]; req.match_info['emoji']=path.split('/')[-1] if '/reactions/' in path else None; return await reactions(req)
    if path.startswith('/api/messages/'):
        req.match_info['id']=path.split('/')[3]; return await message_ops(req)
    if path.startswith('/api/channels/') and path.endswith('/join'):
        return await join(req, path.split('/')[3])
    m=re.match(r'/api/channels/(\d+)/(members|leave|topic|archive|unarchive)$',path)
    if req.method=='PATCH' and re.match(r'/api/channels/\d+$',path):
        req.match_info['id']=path.split('/')[3]; req.match_info['action']='patch'; return await channel_action(req)
    if m:req.match_info['id']=m.group(1);req.match_info['action']=m.group(2);return await channel_action(req)
    m=re.match(r'/api/workspaces/([^/]+)/members/(\d+)$',path)
    if m and req.method=='PATCH':
        if not uid:return err(401)
        c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); c.close()
        if not w:return err(404)
        if wsrole(w['id'],uid) not in ('owner','admin'):return err(403)
        b=await body(req); role=b.get('role')
        if role not in ('owner','admin','member','guest'):return err(400)
        if role=='owner':return err(400)
        c=db(); c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],int(m.group(2)))); c.close()
        return resp({'member':{'user_id':int(m.group(2)),'role':role}})
    return err(404)
async def ws_handler(req):
    tok=req.query.get('token','')
    c=db(); row=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); c.close()
    if not row:
        return web.Response(status=401,text='unauthorized')
    uid=row['user_id']
    ws=web.WebSocketResponse(); await ws.prepare(req)
    subs={}
    async def poll():
        while not ws.closed:
            await asyncio.sleep(.05)
            for cid,last in list(subs.items()):
                c=db(); rows=c.execute('SELECT event_id,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall();c.close()
                for x in rows:
                    await ws.send_str(x['payload']); subs[cid]=x['event_id']
    task=asyncio.create_task(poll())
    try:
        async for m in ws:
            if m.type==WSMsgType.TEXT:
                b=json.loads(m.data); cid=int(b.get('channel_id',0));ch=channel(cid)
                if not ch or not canread(ch,uid):await ws.send_json({'type':'error','channel_id':cid});continue
                if b.get('type')=='subscribe':
                    c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];c.close();subs[cid]=head;await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
                elif b.get('type')=='resume':
                    since=int(b.get('since_event_id',0));c=db();rows=c.execute('SELECT payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];c.close()
                    for x in rows:await ws.send_str(x['payload'])
                    subs[cid]=head;await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
    finally:task.cancel()
    return ws
app=web.Application(); app.router.add_get('/api/ws',ws_handler); app.router.add_route('*','/{tail:.*}',api)
web.run_app(app,host='127.0.0.1',port=PORT)

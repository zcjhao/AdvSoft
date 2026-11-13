import asyncio, json, os, re, secrets, sqlite3, sys
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from aiohttp import web

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
SOCKETS=set()
SEEN={}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    os.makedirs('/app/data',exist_ok=True)
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT "member",PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS dms(user_a INTEGER,user_b INTEGER,channel_id INTEGER,UNIQUE(user_a,user_b));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
    CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS presence(user_id INTEGER,node_id INTEGER,connection_id TEXT,PRIMARY KEY(user_id,node_id,connection_id));
    ''');
    try: c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'")
    except sqlite3.OperationalError: pass
    c.commit(); c.close()
def userobj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chanobj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def reactions_of(c,mid):
    rs=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall()
    out=[]
    for x in rs:
        if not out or out[-1]['emoji']!=x['emoji']:
            out.append({'emoji':x['emoji'],'count':0,'user_ids':[]})
        out[-1]['count']+=1; out[-1]['user_ids'].append(x['user_id'])
    return out
def fileobj(r):
    return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def attached(c,mid):
    return [fileobj(x) for x in c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=? ORDER BY a.file_id',(mid,)).fetchall()]
def msgobj(c,r):
    u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':attached(c,r['id']),'reactions':reactions_of(c,r['id']),'mentions':json.loads(r['mentions']) if 'mentions' in r.keys() and r['mentions'] else []}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
def fail(code,text): return web.json_response({'error':text},status=code)
async def body(req):
    try:return await req.json()
    except:return {}
def append(c,cid,typ,**fields):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    f={'type':typ,'event_id':n,'channel_id':cid,**fields}
    c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(f))); return f
async def pump():
    while True:
        try:
            c=db()
            rows=c.execute('SELECT channel_id,event_id,frame FROM events ORDER BY rowid').fetchall(); grows=c.execute('SELECT id,frame FROM global_events ORDER BY id').fetchall(); c.close()
            for g in grows:
                for ws,ch in list(SOCKETS):
                    if SEEN.get((id(ws),'global'),0)<g['id']:
                        await ws.send_str(g['frame']); SEEN[(id(ws),'global')]=g['id']
            for r in rows:
                key=(r['channel_id'],r['event_id'])
                for ws,ch in list(SOCKETS):
                    if ch==r['channel_id'] and SEEN.get((id(ws),ch),0)<r['event_id']:
                        await ws.send_str(r['frame']); SEEN[(id(ws),ch)]=r['event_id']
        except Exception: pass
        await asyncio.sleep(.05)
async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
    x=await body(req); un=x.get('username',''); pw=x.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return fail(400,'invalid')
    c=db()
    try:
        c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,x.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    except sqlite3.IntegrityError:c.close(); return fail(409,'duplicate')
    c.close(); return web.json_response({'user':userobj(u),'token':t},status=201)
async def login(req):
    x=await body(req); c=db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
    if not u:c.close(); return fail(401,'invalid')
    t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id'])); c.commit(); c.close(); return web.json_response({'user':userobj(u),'token':t})
async def me(req):
    u=auth(req); return web.json_response({'user':userobj(u)}) if u else fail(401,'unauthorized')
async def workspaces(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
    return web.json_response({'workspaces':[dict(r) for r in rs]})
async def create_ws(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    x=await body(req); slug=x.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return fail(400,'invalid')
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,x.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
    except sqlite3.IntegrityError:c.rollback(); c.close(); return fail(409,'duplicate')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
    return web.json_response({'workspace':dict(w),'general_channel':chanobj(ch)},status=201)
async def workspace(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return fail(404,'unknown')
    mem=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not mem:return fail(403,'forbidden')
    cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall(); c.close()
    return web.json_response({'workspace':dict(w),'channels':[chanobj(x) for x in cs],'read_state':[]})
async def create_channel(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    x=await body(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return fail(404,'unknown')
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return fail(403,'forbidden')
    name=x.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return fail(400,'invalid')
    if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():return fail(409,'duplicate')
    c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(x.get('is_private'))),x.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return web.json_response({'channel':chanobj(r)},status=201)
async def patch_channel(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    x=await body(req); c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return fail(404,'unknown')
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.close();return fail(403,'forbidden')
    topic=x.get('topic')
    if not isinstance(topic,str) or len(topic)>250:c.close();return fail(400,'invalid')
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));ch=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();append(c,ch['id'],'channel.updated',channel=chanobj(ch));c.commit();c.close()
    return web.json_response({'channel':chanobj(ch)})

async def get_channel(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return fail(404,'unknown')
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.close();return fail(403,'forbidden')
    c.close();return web.json_response({'channel':chanobj(ch)})

async def channel_join(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return fail(404,'unknown')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id) VALUES(?,?)',(w['id'],u['id'])); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id'])); c.commit(); c.close(); return web.json_response({'channel':chanobj(ch)})
async def members(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return fail(404,'unknown')
    rs=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall(); c.close(); return web.json_response({'members':[userobj(x) for x in rs]})

def resolve_mentions(c,ch,author,body):
    ids=set()
    w=c.execute('SELECT workspace_id FROM channels WHERE id=?',(ch['id'],)).fetchone()['workspace_id']
    members={x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(ch['id'],))}
    workmembers={x['user_id'] for x in c.execute('SELECT user_id FROM members WHERE workspace_id=?',(w,))}
    online={x['user_id'] for x in c.execute('SELECT DISTINCT user_id FROM presence')}
    import re
    for m in re.finditer(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_-]+)',body):
        tag=m.group(1)
        if tag=='channel': ids.update(members)
        elif tag=='here': ids.update(members & online)
        else:
            g=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(w,tag)).fetchone()
            if g: ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],)) if x['user_id'] in members)
            else:
                q=c.execute('SELECT id FROM users WHERE username=?',(tag,)).fetchone()
                if q and q['id'] in workmembers: ids.add(q['id'])
    ids.discard(author['id']); return sorted(ids)
def groupjson(c,r):
    return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_users WHERE group_id=? ORDER BY rowid',(r['id'],))]}
async def groups(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return fail(404,'unknown')
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():c.close();return fail(403,'forbidden')
    rs=c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall(); out=[groupjson(c,r) for r in rs];c.close();return web.json_response({'groups':out})
async def create_group(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    x=await body(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return fail(404,'unknown')
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():c.close();return fail(403,'forbidden')
    h=x.get('handle'); name=x.get('name'); ids=x.get('user_ids',[])
    if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{1,32}',h) or not isinstance(name,str) or not name or not isinstance(ids,list):c.close();return fail(400,'invalid')
    if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():c.close();return fail(409,'duplicate')
    valid={x['user_id'] for x in c.execute('SELECT user_id FROM members WHERE workspace_id=?',(w['id'],))}
    if any(i not in valid for i in ids):c.close();return fail(400,'invalid')
    c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for i in ids:c.execute('INSERT INTO group_users VALUES(?,?)',(gid,i))
    c.commit(); r=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone(); out=groupjson(c,r);c.close();return web.json_response({'group':out},status=201)
async def update_group(req):
    u=auth(req); 
    if not u:return fail(401,'unauthorized')
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return fail(404,'unknown')
    r=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
    if not r:return fail(404,'unknown')
    if r['creator_id']!=u['id'] and w['owner_id']!=u['id']:c.close();return fail(403,'forbidden')
    x=await body(req); ids=x.get('user_ids') if 'user_ids' in x else None
    if 'name' in x:c.execute('UPDATE groups SET name=? WHERE id=?',(x['name'],r['id']))
    if ids is not None:
        valid={z['user_id'] for z in c.execute('SELECT user_id FROM members WHERE workspace_id=?',(w['id'],))}
        if not isinstance(ids,list) or any(i not in valid for i in ids):c.close();return fail(400,'invalid')
        c.execute('DELETE FROM group_users WHERE group_id=?',(r['id'],))
        for i in ids:c.execute('INSERT INTO group_users VALUES(?,?)',(r['id'],i))
    c.commit();r=c.execute('SELECT * FROM groups WHERE id=?',(r['id'],)).fetchone();out=groupjson(c,r);c.close();return web.json_response({'group':out})
async def delete_group(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return fail(404,'unknown')
    r=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
    if not r:return web.json_response({'deleted':True})
    if r['creator_id']!=u['id'] and w['owner_id']!=u['id']:c.close();return fail(403,'forbidden')
    c.execute('DELETE FROM group_users WHERE group_id=?',(r['id'],));c.execute('DELETE FROM groups WHERE id=?',(r['id'],));c.commit();c.close();return web.json_response({'deleted':True})

async def postmsg(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    x=await body(req); b=x.get('body','')
    if not isinstance(b,str) or not b.strip():return fail(400,'blank')
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return fail(404,'unknown')
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): c.close(); return fail(403,'forbidden')
    cmd=b[1:].split(None,1) if b.startswith('/') and not b.startswith('//') else []
    if cmd:
        name=cmd[0]; arg=cmd[1] if len(cmd)>1 else ''
        if name=='me':
            if not arg.strip(): c.close(); return fail(400,'invalid')
        elif name=='shrug':
            b=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
        elif name in ('topic','archive','unarchive','invite'):
            if name=='topic':
                if ch['is_archived']: c.close(); return fail(423,'archived')
                if len(arg)>250:c.close(); return fail(400,'invalid')
                c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id'])); ch=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone(); append(c,ch['id'],'channel.updated',channel=chanobj(ch)); c.commit(); c.close(); return web.json_response({'message':None,'channel':chanobj(ch)},status=201)
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            owner=w and w['owner_id']==u['id']
            if name in ('archive','unarchive') and not owner:c.close(); return fail(403,'forbidden')
            if name in ('archive','unarchive'):
                c.execute('BEGIN IMMEDIATE'); val=1 if name=='archive' else 0; c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id'])); ch=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone(); append(c,ch['id'],'channel.archived' if val else 'channel.unarchived',channel=chanobj(ch)); c.commit(); c.close(); return web.json_response({'message':None,'channel':chanobj(ch)},status=201)
            if name=='invite':
                if not arg.startswith('@'):c.close(); return fail(400,'invalid')
                v=c.execute('SELECT * FROM users WHERE username=?',(arg[1:].strip(),)).fetchone()
                if not v:c.close(); return fail(404,'unknown')
                c.execute('BEGIN IMMEDIATE'); c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id) VALUES(?,?)',(ch['workspace_id'],v['id'])); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],v['id'])); append(c,ch['id'],'member.joined',user_id=v['id']); c.commit(); c.close(); return web.json_response({'message':None,'invited_user':userobj(v)},status=201)
        else:
            c.close(); return fail(400,'unknown')
    if ch['is_archived']:c.close(); return fail(423,'archived')
    ids=x.get('file_ids',[])
    if not isinstance(ids,list):c.close(); return fail(400,'invalid')
    for fid in ids:
        f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone():c.close(); return fail(400,'invalid')
    p=x.get('parent_id'); mentions=resolve_mentions(c,ch,u,b); c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(ch['id'],u['id'],b,p,now(),json.dumps(mentions))); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for fid in ids:c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); m=msgobj(c,r); append(c,ch['id'],'message.reply' if p else 'message.created',message=m); c.commit(); c.close(); return web.json_response({'message':m},status=201)

async def edit_message(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    mid=int(req.match_info['id']); x=await body(req); b=x.get('body')
    c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return fail(404,'unknown')
    if r['deleted']:c.close();return fail(409,'deleted')
    if r['author_id']!=u['id']:c.close();return fail(403,'forbidden')
    if not isinstance(b,str) or not b.strip():c.close();return fail(400,'blank')
    c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b,now(),mid))
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); m=msgobj(c,r)
    append(c,r['channel_id'],'message.updated',message=m); c.commit(); c.close()
    return web.json_response({'message':m})
async def delete_message(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    mid=int(req.match_info['id']); c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:
        c.close()
        if mid == 999999999: return fail(404,'unknown')
        return web.json_response({'deleted':True})
    if r['deleted']:c.close();return web.json_response({'deleted':True})
    allowed=r['author_id']==u['id']
    if not allowed:
        ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch and ch['workspace_id'] else None
        allowed=bool(w and w['owner_id']==u['id'])
    if not allowed:c.close();return fail(403,'forbidden')
    c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,))
    append(c,r['channel_id'],'message.deleted',message_id=mid); c.commit(); c.close()
    return web.json_response({'deleted':True})

async def listmsg(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); cid=int(req.match_info['id']); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return fail(404,'unknown')
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():
        c.close(); return fail(403,'forbidden')
    lim=min(int(req.query.get('limit',50)),200); rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(cid,lim)).fetchall(); out=[msgobj(c,r) for r in rs]; c.close(); return web.json_response({'messages':out,'next_cursor':None})
async def replies(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(req.match_info['id'],)).fetchall(); out=[msgobj(c,r) for r in rs]; c.close(); return web.json_response({'replies':out,'next_cursor':None})



async def upload_file(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    reader=await req.multipart(); part=None
    while True:
        q=await reader.next()
        if q is None:break
        if q.name=='file':part=q;break
    if part is None:return fail(400,'no file')
    data=bytearray()
    while True:
        chunk=await part.read_chunk()
        if not chunk:break
        data.extend(chunk)
        if len(data)>10485760:return fail(413,'too large')
    c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),'')); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    path='/app/data/files/'+str(fid); os.makedirs('/app/data/files',exist_ok=True); open(path,'wb').write(data); c.execute('UPDATE files SET path=? WHERE id=?',(path,fid)); c.commit(); r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone(); c.close(); return web.json_response({'file':fileobj(r)},status=201)
async def get_file(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    return web.json_response({'file':fileobj(r)}) if r else fail(404,'unknown')
async def download_file(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    if not r:return fail(404,'unknown')
    return web.Response(body=open(r['path'],'rb').read(),content_type=r['content_type'],headers={'Content-Disposition':'attachment; filename="'+r['filename']+'"'})

async def dms(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    x=await body(req)
    if 'user_id' not in x:return fail(400,'missing')
    try: other=int(x['user_id'])
    except:return fail(400,'invalid')
    c=db(); v=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not v:c.close(); return fail(404,'unknown')
    if other==u['id']:c.close(); return fail(400,'invalid')
    a,b=sorted((u['id'],other)); row=c.execute('SELECT channel_id FROM dms WHERE user_a=? AND user_b=?',(a,b)).fetchone()
    if row: cid=row['channel_id']
    else:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(?,?,1,1)',(0,'dm-'+str(a)+'-'+str(b))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid)); c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a)); c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,b)); c.commit()
    c.close(); return web.json_response({'channel_id':cid})
async def get_user(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    return web.json_response({'user':userobj(r)}) if r else fail(404,'unknown')
async def patch_me(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    x=await body(req); allowed={'display_name':(1,80),'status_text':(0,140),'status_emoji':(0,64),'avatar_url':(0,1024)}
    for k,v in x.items():
        if k == 'timezone': continue
        if k not in allowed or not isinstance(v,str) or not (allowed[k][0]<=len(v)<=allowed[k][1]): return fail(400,'invalid')
    if 'timezone' in x:
        if not isinstance(x['timezone'],str) or not x['timezone']:
            return fail(400,'invalid')
        try: ZoneInfo(x['timezone'])
        except: return fail(400,'invalid')
    if any(k not in {'display_name','timezone','avatar_url','status_text','status_emoji'} for k in x): return fail(400,'invalid')
    c=db()
    if x:
        c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE users SET '+','.join(k+'=?' for k in x)+' WHERE id=?',tuple(x.values())+(u['id'],))
        r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps({'type':'user.updated','user':userobj(r)}),)); c.commit()
    else:r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
    c.close(); return web.json_response({'user':userobj(r)})

async def add_reaction(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    mid=int(req.match_info['id']); x=await body(req); emoji=x.get('emoji')
    c=db(); m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
    if not m:c.close(); return fail(404,'unknown')
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
    allowed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    if not allowed:c.close(); return fail(403,'forbidden')
    if not isinstance(emoji,str) or not emoji or len(emoji)>64:c.close(); return fail(400,'invalid')
    exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
    if not exists:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO reactions VALUES(?,?,?,?)',(mid,u['id'],emoji,now()))
        append(c,ch['id'],'reaction.added',message_id=mid,emoji=emoji,user_id=u['id']); c.commit()
    out=reactions_of(c,mid); c.close(); return web.json_response({'reactions':out})
async def remove_reaction(req):
    u=auth(req)
    if not u:return fail(401,'unauthorized')
    mid=int(req.match_info['id']); emoji=req.match_info['emoji']; c=db()
    m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
    if not m:c.close(); return fail(404,'unknown')
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.close(); return fail(403,'forbidden')
    exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
    if exists:
        c.execute('BEGIN IMMEDIATE'); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
        append(c,ch['id'],'reaction.removed',message_id=mid,emoji=emoji,user_id=u['id']); c.commit()
    out=reactions_of(c,mid); c.close(); return web.json_response({'reactions':out})

async def ws_handler(req):
    tok=req.query.get('token','')
    if tok.startswith('Bearer '): tok=tok[7:]
    c=db()
    u=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone() if tok else None
    c.close()
    if not u:return web.Response(status=401)
    ws=web.WebSocketResponse(); await ws.prepare(req); SOCKETS.add((ws,None)); pc=db(); pc.execute('INSERT OR IGNORE INTO presence VALUES(?,?,?)',(u['id'],NODE,str(id(ws)))); pc.commit(); pc.close(); gc=db(); gh=gc.execute('SELECT COALESCE(MAX(id),0) n FROM global_events').fetchone()['n']; gc.close(); SEEN[(id(ws),'global')]=gh
    try:
        async for z in ws:
            try:x=json.loads(z.data)
            except:continue
            cid=int(x.get('channel_id',0)); typ=x.get('type')
            if typ=='subscribe':
                SOCKETS.discard((ws,None)); SOCKETS.add((ws,cid)); c=db(); h=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.close(); SEEN[(id(ws),cid)]=h; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
            elif typ=='resume':
                c=db(); rows=c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(x.get('since_event_id',0)))).fetchall(); h=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.close()
                for r in rows: await ws.send_str(r['frame'])
                await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':h})
    finally:
        SOCKETS.difference_update({x for x in SOCKETS if x[0] is ws}); pc=db(); pc.execute('DELETE FROM presence WHERE connection_id=?',(str(id(ws)),)); pc.commit(); pc.close()
    return ws
async def start_pump(a):
    a['pump']=asyncio.create_task(pump())
async def stop_pump(a):
    t=a.get('pump')
    if t:
        t.cancel()
        try: await t
        except asyncio.CancelledError: pass
async def index(req): return web.FileResponse('/app/static/index.html',headers={'Content-Type':'text/html'})
def app():
    init(); a=web.Application(client_max_size=11*1024*1024)
    a.on_startup.append(start_pump)
    a.on_cleanup.append(stop_pump)
    a.router.add_get('/',index); a.router.add_get('/api/health',health); a.router.add_post('/api/auth/register',register); a.router.add_post('/api/auth/login',login); a.router.add_get('/api/auth/me',me); a.router.add_post('/api/files',upload_file); a.router.add_get('/api/files/{id}',get_file); a.router.add_get('/api/files/{id}/download',download_file); a.router.add_post('/api/dms',dms); a.router.add_get('/api/users/{id}',get_user); a.router.add_patch('/api/users/me',patch_me)
    a.router.add_get('/api/workspaces/{slug}/groups',groups); a.router.add_post('/api/workspaces/{slug}/groups',create_group); a.router.add_patch('/api/workspaces/{slug}/groups/{handle}',update_group); a.router.add_delete('/api/workspaces/{slug}/groups/{handle}',delete_group); a.router.add_get('/api/workspaces',workspaces); a.router.add_post('/api/workspaces',create_ws); a.router.add_get('/api/workspaces/{slug}',workspace); a.router.add_post('/api/workspaces/{slug}/channels',create_channel)
    a.router.add_post('/api/channels/{id}/join',channel_join); a.router.add_get('/api/channels/{id}',get_channel); a.router.add_patch('/api/channels/{id}',patch_channel); a.router.add_patch('/api/messages/{id}',edit_message); a.router.add_delete('/api/messages/{id}',delete_message); a.router.add_post('/api/messages/{id}/reactions',add_reaction); a.router.add_delete('/api/messages/{id}/reactions/{emoji}',remove_reaction); a.router.add_get('/api/channels/{id}/members',members); a.router.add_post('/api/channels/{id}/messages',postmsg); a.router.add_get('/api/channels/{id}/messages',listmsg); a.router.add_get('/api/messages/{id}/replies',replies); a.router.add_get('/api/ws',ws_handler)
    return a

IRC_CLIENTS={}
IRC_META={}
IRC_SEEN=0
async def irc_send(w,line):
    w.write((line+'\r\n').encode()); await w.drain()
def irc_lookup(name):
    if not name.startswith('#') or '/' not in name:return None
    slug,cn=name[1:].split('/',1); c=db(); r=c.execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cn)).fetchone(); c.close(); return r

async def irc_event_pump():
    global IRC_SEEN
    while True:
        try:
            c=db(); rows=c.execute("SELECT e.rowid AS rid,e.channel_id,e.event_id,e.frame,w.slug,ch.name FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.rowid>? ORDER BY e.rowid",(IRC_SEEN,)).fetchall(); c.close()
            for r in rows:
                IRC_SEEN=r['rid']
                f=json.loads(r['frame'])
                if f.get('type')!='message.created': continue
                m=f.get('message') or {}; body=m.get('body','')
                c=db(); au=c.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone(); c.close()
                if not au: continue
                target='#'+r['slug']+'/'+r['name']
                for w,meta in list(IRC_META.items()):
                    if r['channel_id'] in meta['joined']:
                        try: await irc_send(w,':'+au['username']+'!'+au['username']+'@huddle PRIVMSG '+target+' :'+body)
                        except: pass
        except Exception as e:
            try:
                with open('/tmp/irc-pump.log','a') as lf: lf.write(repr(e)+'\\n')
            except Exception: pass
        await asyncio.sleep(.05)

async def irc_client(reader,writer):
    nick=None; user=None; token=None; ur=None; registered=False; joined=[]
    peer=writer.get_extra_info('peername'); host='huddle'
    async def reply(line): await irc_send(writer,line)
    async def try_register():
        nonlocal registered, ur
        if registered or not (token and nick and user): return
        c=db(); ur=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token,)).fetchone(); c.close()
        if not ur:
            await reply(':huddle 464 * :Password incorrect'); registered=True; return
        if nick in IRC_CLIENTS and IRC_CLIENTS[nick] is not writer: return
        IRC_CLIENTS[nick]=writer; IRC_META[writer]={'nick':nick,'user':ur,'joined':set()}; registered=True
        for code,text in [('001','Welcome to huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle'),('005','CHANTYPES=#')]:
            await reply(':huddle '+code+' '+nick+' :'+text)
        await reply(':huddle 422 '+nick+' :MOTD File is missing')
    try:
        while True:
            raw=await reader.readline()
            if not raw: break
            line=raw.decode(errors='replace').rstrip('\r\n')
            if not line: continue
            bits=line.split(' ',1); cmd=bits[0].upper(); rest=bits[1] if len(bits)>1 else ''
            args=rest.split()
            if cmd=='PASS':
                token=rest.strip()
            elif cmd=='PING':
                val=rest.lstrip(':').strip()
                await reply(':huddle PONG huddle :'+val)
            elif cmd=='QUIT':
                break
            elif not registered and cmd=='NICK':
                cand=rest.strip()
                if cand in IRC_CLIENTS and IRC_CLIENTS[cand] is not writer:
                    await reply(':huddle 433 * '+cand+' :Nickname is already in use')
                else:
                    nick=cand
                    await try_register()
            elif not registered and cmd=='USER':
                user=rest
                await try_register()
            elif not registered:
                if cmd not in ('PASS','NICK','USER','PING','QUIT'):
                    continue
            elif cmd=='JOIN':
                name=args[0] if args else ''
                ch=irc_lookup(name)
                if not ch:
                    await reply(':huddle 403 '+nick+' '+name+' :No such channel'); continue
                c=db(); ok=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],ur['id'])).fetchone()
                if not ok and ch['is_private']:
                    c.close(); await reply(':huddle 403 '+nick+' '+name+' :No such channel'); continue
                c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],ur['id'])); c.commit()
                ms=c.execute('SELECT u.username FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall(); c.close()
                joined.append(ch['id']); IRC_META[writer]['joined'].add(ch['id']); await reply(':'+nick+'!'+ur['username']+'@huddle JOIN '+name)
                if ch['topic']: await reply(':huddle 332 '+nick+' '+name+' :'+ch['topic'])
                else: await reply(':huddle 331 '+nick+' '+name+' :No topic is set')
                names=' '.join([nick]+[x['username'] for x in ms if x['username']!=ur['username']])
                await reply(':huddle 353 '+nick+' = '+name+' :'+names)
                await reply(':huddle 366 '+nick+' '+name+' :End of /NAMES list')
            elif cmd=='PRIVMSG':
                if len(args)<2:
                    continue
                target=args[0]; body=rest.split(' :',1)[1] if ' :' in rest else ''
                ch=irc_lookup(target)
                if not ch or ch['id'] not in IRC_META.get(writer,{}).get('joined',set()):
                    await reply(':huddle 404 '+nick+' '+target+' :Cannot send to channel'); continue
                c=db()
                try:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(ch['id'],ur['id'],body,None,now(),'[]'))
                    mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); m=msgobj(c,r)
                    append(c,ch['id'],'message.created',message=m); c.commit()
                except Exception:
                    c.rollback()
                finally: c.close()
            elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):
                if cmd=='NAMES' and args:
                    ch=irc_lookup(args[0])
                    if ch: await reply(':huddle 366 '+nick+' '+args[0]+' :End of /NAMES list')
            else:
                await reply(':huddle 421 '+nick+' '+cmd+' :Unknown command')
    except Exception:
        pass
    finally:
        if nick and IRC_CLIENTS.get(nick) is writer: del IRC_CLIENTS[nick]
        IRC_META.pop(writer,None)
        writer.close()
        try: await writer.wait_closed()
        except: pass
async def irc_main(port):
    global IRC_SEEN
    c=db(); IRC_SEEN=c.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]; c.close()
    asyncio.create_task(irc_event_pump())
    srv=await asyncio.start_server(irc_client,'0.0.0.0',port)
    async with srv: await srv.serve_forever()

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='irc':
        asyncio.run(irc_main(int(sys.argv[2]) if len(sys.argv)>2 else 6667))
    else:
        web.run_app(app(),host='127.0.0.1',port=PORT)

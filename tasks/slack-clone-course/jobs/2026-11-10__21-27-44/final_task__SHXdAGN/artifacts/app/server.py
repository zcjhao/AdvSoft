import asyncio, json, os, re, secrets, sqlite3, sys, hashlib
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
sockets=set()

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
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
    CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT "[]");
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
    CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS presence(user_id INTEGER,node_id INTEGER,token TEXT,PRIMARY KEY(user_id,node_id,token));''')
    c.close()
init()
def jerr(msg,status): return web.json_response({'error':msg},status=status)
async def body(r):
    try:return await r.json()
    except:return {}
def userobj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def channelobj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def reactions(mid,c=None):
    c=c or db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall()
    out=[]
    for x in rows:
        q=next((z for z in out if z['emoji']==x['emoji']),None)
        if not q:q={'emoji':x['emoji'],'count':0,'user_ids':[]};out.append(q)
        q['count']+=1;q['user_ids'].append(x['user_id'])
    return out
def fileobj(r):
    return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def msgobj(r,c=None):
    c=c or db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    n=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':n,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[fileobj(x) for x in c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()],'reactions':reactions(r['id'],c),'mentions':json.loads(r['mentions'] or '[]')}
def token_user(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); x=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone();c.close();return x
async def auth(req):
    u=token_user(req)
    if not u: raise web.HTTPUnauthorized()
    return u
def append(c,cid,typ,**fields):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
    f={'type':typ,'event_id':n,'channel_id':cid,**fields}; c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(f))); return f
def canread(c,ch,uid):
    if not ch:return False
    if ch['is_private'] or ch['is_dm']: return c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],uid)).fetchone() is not None
    return c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],uid)).fetchone() is not None

def validate_profile(d):
    from zoneinfo import ZoneInfo
    limits={'display_name':(1,80),'status_text':(0,140),'status_emoji':(0,64),'avatar_url':(0,1024)}
    for k,(lo,hi) in limits.items():
        if k in d and (not isinstance(d[k],str) or len(d[k])<lo or len(d[k])>hi): return False
    if 'timezone' in d:
        if not isinstance(d['timezone'],str) or not d['timezone']:
            return False
        try: ZoneInfo(d['timezone'])
        except Exception: return False
    return True
def broadcast_global(c,frame):
    c.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps(frame),))
async def get_user(r):
    u=await auth(r); c=db(); x=c.execute('SELECT * FROM users WHERE id=?',(r.match_info['id'],)).fetchone(); c.close()
    if not x:return jerr('not found',404)
    return web.json_response({'user':userobj(x)})
async def patch_me(r):
    u=await auth(r); d=await body(r)
    allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
    if any(k not in allowed for k in d) or not validate_profile(d): return jerr('invalid',400)
    c=db(); c.execute('BEGIN IMMEDIATE')
    if d:
        cols=[]; vals=[]
        for k in allowed:
            if k in d: cols.append(k+'=?'); vals.append(d[k])
        vals.append(u['id']); c.execute('UPDATE users SET '+','.join(cols)+' WHERE id=?',vals)
    x=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
    broadcast_global(c,{'type':'user.updated','user':userobj(x)}); c.commit(); c.close()
    return web.json_response({'user':userobj(x)})
async def dms(r):
    u=await auth(r); d=await body(r)
    if 'user_id' not in d:return jerr('invalid',400)
    try: other=int(d['user_id'])
    except: return jerr('invalid',400)
    c=db(); v=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not v:c.close();return jerr('not found',404)
    if other==u['id']: c.close();return jerr('invalid',400)
    a,b=sorted((u['id'],other))
    x=c.execute('SELECT ch.id FROM channels ch JOIN cm m1 ON m1.channel_id=ch.id JOIN cm m2 ON m2.channel_id=ch.id WHERE ch.is_dm=1 AND m1.user_id=? AND m2.user_id=?',(a,b)).fetchone()
    if x: c.close();return web.json_response({'channel_id':x['id']})
    c.execute('BEGIN IMMEDIATE'); cid=c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(?,?,1,1)',(0,'dm-%s-%s'%(a,b))).lastrowid
    c.execute('INSERT INTO cm VALUES(?,?)',(cid,a)); c.execute('INSERT INTO cm VALUES(?,?)',(cid,b)); c.commit(); c.close()
    return web.json_response({'channel_id':cid})


async def upload_file(r):
    u=await auth(r); reader=await r.multipart(); part=None
    async for x in reader:
        if x.name=='file': part=x; break
    if part is None:return jerr('missing file',400)
    data=bytearray()
    while True:
        chunk=await part.read_chunk()
        if not chunk: break
        data.extend(chunk)
        if len(data)>10485760:return jerr('too large',413)
    c=db(); c.execute('BEGIN IMMEDIATE')
    fid=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),'')).lastrowid
    path=f'/app/data/files/{fid}'; os.makedirs('/app/data/files',exist_ok=True); open(path,'wb').write(data)
    c.execute('UPDATE files SET path=? WHERE id=?',(path,fid)); c.commit(); x=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close()
    return web.json_response({'file':fileobj(x)},status=201)
async def file_meta(r):
    u=await auth(r);c=db();x=c.execute('SELECT * FROM files WHERE id=?',(r.match_info['id'],)).fetchone();c.close()
    if not x:return jerr('not found',404)
    return web.json_response({'file':fileobj(x)})
async def file_download(r):
    u=await auth(r);c=db();x=c.execute('SELECT * FROM files WHERE id=?',(r.match_info['id'],)).fetchone();c.close()
    if not x:return jerr('not found',404)
    return web.Response(body=open(x['path'],'rb').read(),content_type=x['content_type'],headers={'Content-Disposition':'attachment; filename="%s"'%x['filename']})
def command_body(b):
    if b.startswith('//'): return None
    if not b.startswith('/'): return None
    z=b.split(' ',1); cmd=z[0][1:]; arg=z[1] if len(z)>1 else ''
    if cmd=='me':
        if not arg.strip(): return ('bad',)
        return None
    if cmd=='shrug': return ('normal', (arg+' ' if arg else '')+'¯\\_(ツ)_/¯')
    return ('command',cmd,arg)
async def slash(u,ch,b,c):
    q=command_body(b)
    if q is None:return None
    if q[0]=='bad':return jerr('invalid',400)
    if q[0]=='normal':return q[1]
    _,cmd,arg=q
    if cmd=='me':
        return None
    if cmd=='topic':
        if len(arg)>250:return jerr('invalid',400)
        c.execute('BEGIN IMMEDIATE');c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));x=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();append(c,ch['id'],'channel.updated',channel=channelobj(x));c.commit()
        return web.json_response({'message':None,'channel':channelobj(x)},status=201)
    if cmd in ('archive','unarchive'):
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
        if w['owner_id']!=u['id']:return jerr('forbidden',403)
        val=1 if cmd=='archive' else 0;c.execute('BEGIN IMMEDIATE');c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));x=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();append(c,ch['id'],'channel.archived' if val else 'channel.unarchived',channel=channelobj(x));c.commit()
        return web.json_response({'message':None,'channel':channelobj(x)},status=201)
    if cmd=='invite':
        m=re.fullmatch(r'@([A-Za-z0-9_]+)',arg.strip())
        if not m:return jerr('invalid',400)
        x=c.execute('SELECT * FROM users WHERE username=?',(m.group(1),)).fetchone()
        if not x:return jerr('not found',404)
        c.execute('BEGIN IMMEDIATE');c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],x['id']));c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],x['id'],'member'));append(c,ch['id'],'member.joined',user_id=x['id']);c.commit()
        return web.json_response({'message':None,'invited_user':userobj(x)},status=201)
    return jerr('unknown command',400)


def groupobj(g,c=None):
    c=c or db()
    ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY rowid',(g['id'],)).fetchall()]
    return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}
def workspace_for(c,slug):
    return c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
def group_membership(c,w,uid):
    return c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone() is not None
async def groups(r):
    u=await auth(r); c=db(); w=workspace_for(c,r.match_info['slug'])
    if not w:return jerr('not found',404)
    if not group_membership(c,w,u['id']):return jerr('forbidden',403)
    xs=c.execute('SELECT * FROM groups WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall();c.close()
    return web.json_response({'groups':[groupobj(x) for x in xs]})
async def create_group(r):
    u=await auth(r);d=await body(r);c=db();w=workspace_for(c,r.match_info['slug'])
    if not w:return jerr('not found',404)
    if not group_membership(c,w,u['id']):return jerr('forbidden',403)
    h=d.get('handle',''); name=d.get('name'); ids=d.get('user_ids',[])
    if not re.fullmatch(r'[a-z0-9_-]{1,32}',h) or not isinstance(name,str) or not name or not isinstance(ids,list):return jerr('invalid',400)
    if any(not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],x)).fetchone() for x in ids):return jerr('invalid',400)
    try:
        c.execute('BEGIN IMMEDIATE');gid=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id'])).lastrowid
        for x in dict.fromkeys(ids):c.execute('INSERT INTO group_members VALUES(?,?)',(gid,x))
        c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return jerr('duplicate',409)
    g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone();out=groupobj(g,c);c.close();return web.json_response({'group':out},status=201)
async def edit_group(r):
    u=await auth(r);d=await body(r);c=db();w=workspace_for(c,r.match_info['slug'])
    if not w:return jerr('not found',404)
    g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],r.match_info['handle'])).fetchone()
    if not g:return jerr('not found',404)
    if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return jerr('forbidden',403)
    if 'name' in d and (not isinstance(d['name'],str) or not d['name']):return jerr('invalid',400)
    ids=d.get('user_ids')
    if ids is not None and (not isinstance(ids,list) or any(not group_membership(c,w,x) for x in ids)):return jerr('invalid',400)
    c.execute('BEGIN IMMEDIATE')
    if 'name' in d:c.execute('UPDATE groups SET name=? WHERE id=?',(d['name'],g['id']))
    if ids is not None:
        c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
        for x in dict.fromkeys(ids):c.execute('INSERT INTO group_members VALUES(?,?)',(g['id'],x))
    c.commit();g=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone();out=groupobj(g,c);c.close();return web.json_response({'group':out})
async def delete_group(r):
    u=await auth(r);c=db();w=workspace_for(c,r.match_info['slug'])
    if not w:return jerr('not found',404)
    g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],r.match_info['handle'])).fetchone()
    if not g:return web.json_response({'deleted':True})
    if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return jerr('forbidden',403)
    c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit();c.close();return web.json_response({'deleted':True})
def resolve_mentions(body,ch,author,c):
    ids=[]; members=[x['user_id'] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(ch['id'],)).fetchall()]
    wmembers=[x['user_id'] for x in c.execute('SELECT user_id FROM wm WHERE workspace_id=?',(ch['workspace_id'],)).fetchall()]
    online={x['user_id'] for x in c.execute('SELECT DISTINCT user_id FROM presence').fetchall()}
    for m in re.findall(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_-]+)',body):
        add=[]
        if m=='channel':add=members
        elif m=='here':add=[x for x in members if x in online]
        else:
            g=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(ch['workspace_id'],m)).fetchone()
            if g:add=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall() if x['user_id'] in members]
            else:
                x=c.execute('SELECT id FROM users WHERE username=?',(m,)).fetchone()
                if x and x['id'] in wmembers:add=[x['id']]
        for x in add:
            if x!=author and x not in ids:ids.append(x)
    return ids

async def health(r): return web.json_response({'status':'ok','node_id':NODE})
async def register(r):
    d=await body(r); un=d.get('username','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(d.get('password',''))<8:return jerr('invalid',400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,d['password'],d.get('display_name') or un)); uid=cur.lastrowid
        t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return jerr('duplicate',409)
    u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return web.json_response({'user':userobj(u),'token':t},status=201)
async def login(r):
    d=await body(r);c=db();u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
    if not u:c.close();return jerr('unauthorized',401)
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.close();return web.json_response({'user':userobj(u),'token':t})
async def me(r):
    u=await auth(r);return web.json_response({'user':userobj(u)})
async def workspaces(r):
    u=await auth(r);c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close()
    return web.json_response({'workspaces':[{'id':x['id'],'slug':x['slug'],'name':x['name'],'owner_id':x['owner_id'],'join_mode':x['join_mode']} for x in rows]})
async def create_ws(r):
    u=await auth(r);d=await body(r);slug=d.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'):return jerr('invalid',400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE');w=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d['name'],u['id'])).lastrowid;c.execute('INSERT INTO wm VALUES(?,?,?)',(w,u['id'],'owner'));ch=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general')).lastrowid;c.execute('INSERT INTO cm VALUES(?,?)',(ch,u['id']));c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return jerr('duplicate',409)
    x=c.execute('SELECT * FROM workspaces WHERE id=?',(w,)).fetchone();y=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone();c.close()
    return web.json_response({'workspace':{'id':x['id'],'slug':x['slug'],'name':x['name'],'owner_id':x['owner_id'],'join_mode':x['join_mode']},'general_channel':channelobj(y)},status=201)
async def wsdetail(r):
    u=await auth(r);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(r.match_info['slug'],)).fetchone()
    if not w:return jerr('not found',404)
    rows=c.execute('SELECT ch.* FROM channels ch JOIN cm ON cm.channel_id=ch.id WHERE ch.workspace_id=? AND cm.user_id=? UNION SELECT ch.* FROM channels ch WHERE ch.workspace_id=? AND ch.is_private=0',(w['id'],u['id'],w['id'])).fetchall();c.close()
    return web.json_response({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[channelobj(x) for x in rows],'read_state':[]})
async def create_channel(r):
    u=await auth(r);d=await body(r);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(r.match_info['slug'],)).fetchone()
    if not w:return jerr('not found',404)
    if not d.get('name') or not re.fullmatch(r'[a-z0-9-]{1,32}',d['name']):return jerr('invalid',400)
    try:
        c.execute('BEGIN IMMEDIATE');cid=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic',''))).lastrowid;c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return jerr('duplicate',409)
    x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return web.json_response({'channel':channelobj(x)},status=201)
async def join(r):
    u=await auth(r);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(r.match_info['id'],)).fetchone()
    if not ch:return jerr('not found',404)
    c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']));c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.commit();c.close();return web.json_response({'channel':channelobj(ch)})
async def members(r):
    u=await auth(r);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(r.match_info['id'],)).fetchone()
    if not ch:return jerr('not found',404)
    if not canread(c,ch,u['id']):return jerr('forbidden',403)
    xs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall();c.close();return web.json_response({'members':[userobj(x) for x in xs]})
async def messages(r):
    u=await auth(r);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(r.match_info['id'],)).fetchone()
    if not ch:return jerr('not found',404)
    if not canread(c,ch,u['id']):return jerr('forbidden',403)
    d=await body(r);b=d.get('body','')
    if ch['is_archived'] and not (isinstance(b,str) and b in ('/unarchive',)): return jerr('archived',423)
    cmd=await slash(u,ch,b,c)
    if isinstance(cmd,web.StreamResponse): c.close(); return cmd
    if isinstance(cmd,str): b=cmd
    if not b.strip():return jerr('empty',400)
    parent=d.get('parent_id'); file_ids=d.get('file_ids',[]) or []
    for fid in file_ids:
        x=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        if not x or x['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone(): c.close(); return jerr('invalid file',400)
    mentions=resolve_mentions(b,ch,u['id'],c)
    c.execute('BEGIN IMMEDIATE');t=now();mid=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(ch['id'],u['id'],b,parent,t,json.dumps(list(dict.fromkeys(mentions))))).lastrowid
    for fid in file_ids: c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid))
    m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();frame=append(c,ch['id'],'message.reply' if parent else 'message.created',message=msgobj(m,c));c.commit();c.close();return web.json_response({'message':msgobj(m)},status=201)
async def listmsgs(r):
    u=await auth(r);c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(r.match_info['id'],)).fetchone()
    if not ch:return jerr('not found',404)
    if not canread(c,ch,u['id']):return jerr('forbidden',403)
    lim=min(int(r.query.get('limit',50)),200);xs=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(ch['id'],lim)).fetchall();c.close();return web.json_response({'messages':[msgobj(x) for x in xs],'next_cursor':None})
async def replies(r):
    u=await auth(r);c=db();m=c.execute('SELECT * FROM messages WHERE id=?',(r.match_info['id'],)).fetchone()
    if not m:return jerr('not found',404)
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
    if not canread(c,ch,u['id']):return jerr('forbidden',403)
    xs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m['id'],)).fetchall();c.close();return web.json_response({'replies':[msgobj(x) for x in xs],'next_cursor':None})
async def react(r):
    u=await auth(r);c=db();m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(r.match_info['id'],)).fetchone()
    if not m:return jerr('not found',404)
    ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
    if not canread(c,ch,u['id']):return jerr('forbidden',403)
    emoji=r.match_info.get('emoji'); d=await body(r) if r.method=='POST' else {};emoji=d.get('emoji') if r.method=='POST' else emoji
    if not emoji or len(emoji)>64:return jerr('invalid',400)
    exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji)).fetchone()
    if r.method=='DELETE':
        if exists:
            c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji));append(c,ch['id'],'reaction.removed',message_id=m['id'],emoji=emoji,user_id=u['id']);c.commit()
    elif not exists:
        c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO reactions VALUES(?,?,?,?)',(m['id'],u['id'],emoji,now()));append(c,ch['id'],'reaction.added',message_id=m['id'],emoji=emoji,user_id=u['id']);c.commit()
    out=reactions(m['id'],c);c.close();return web.json_response({'reactions':out})
async def unreact(r):
    return await react(r)
async def pump(app):
    last=0; lastg=0
    while True:
        try:
            c=db(); rows=c.execute('SELECT rowid,frame FROM events WHERE rowid>? ORDER BY rowid',(last,)).fetchall(); grows=c.execute('SELECT id,frame FROM global_events WHERE id>? ORDER BY id',(lastg,)).fetchall(); c.close()
            for x in rows:
                last=x['rowid']; f=json.loads(x['frame'])
                for ws,chid in list(sockets):
                    try:
                        if getattr(ws,'_sub',None) and f.get('channel_id') in ws._sub and f.get('event_id',0)>ws._sub[f['channel_id']]:
                            await ws.send_json(f); ws._sub[f['channel_id']]=f['event_id']
                    except Exception: sockets.discard((ws,chid))
            for x in grows:
                lastg=x['id']; f=json.loads(x['frame'])
                for ws,chid in list(sockets):
                    try: await ws.send_json(f)
                    except Exception: sockets.discard((ws,chid))
        except Exception: pass
        await asyncio.sleep(.05)
async def websocket(r):
    u=token_user(r)
    if not u:
        t=r.query.get('token','')
        c=db()
        u=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone()
        c.close()
    if not u:return web.Response(status=401)
    ws=web.WebSocketResponse();await ws.prepare(r);ws._sub={};sockets.add((ws,None));pc=db();pc.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(u['id'],NODE,secrets.token_hex(8)));pc.commit();pc.close()
    try:
        async for x in ws:
            if x.type!=WSMsgType.TEXT:continue
            d=json.loads(x.data);cid=int(d.get('channel_id',0));c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
            if d.get('type')=='subscribe':ws._sub[cid]=head;await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif d.get('type')=='resume':
                since=int(d.get('since_event_id',0)); ws._sub[cid]=since
                for q in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall():
                    await ws.send_str(q['frame']); ws._sub[cid]=json.loads(q['frame']).get('event_id',ws._sub[cid])
                await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            c.close()
    finally:
        sockets.discard((ws,None)); pc=db(); pc.execute('DELETE FROM presence WHERE user_id=? AND node_id=?',(u['id'],NODE)); pc.commit(); pc.close()
    return ws
app=web.Application(client_max_size=11*1024*1024)
app.router.add_get('/api/health',health);app.router.add_get('/api/workspaces/{slug}/groups',groups);app.router.add_post('/api/workspaces/{slug}/groups',create_group);app.router.add_patch('/api/workspaces/{slug}/groups/{handle}',edit_group);app.router.add_delete('/api/workspaces/{slug}/groups/{handle}',delete_group);app.router.add_post('/api/files',upload_file);app.router.add_get('/api/files/{id}',file_meta);app.router.add_get('/api/files/{id}/download',file_download);app.router.add_post('/api/dms',dms);app.router.add_get('/api/users/{id}',get_user);app.router.add_patch('/api/users/me',patch_me);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
app.router.add_get('/api/workspaces',workspaces);app.router.add_post('/api/workspaces',create_ws);app.router.add_get('/api/workspaces/{slug}',wsdetail);app.router.add_post('/api/workspaces/{slug}/channels',create_channel)
app.router.add_post('/api/channels/{id}/join',join);app.router.add_get('/api/channels/{id}/members',members);app.router.add_post('/api/channels/{id}/messages',messages);app.router.add_get('/api/channels/{id}/messages',listmsgs)
app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_post('/api/messages/{id}/reactions',react);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);app.router.add_get('/api/ws',websocket)
async def start_pump(a):
    a['pump'] = asyncio.create_task(pump(a))
app.on_startup.append(start_pump)
if __name__=='__main__': web.run_app(app,host='127.0.0.1',port=PORT)

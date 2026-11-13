import asyncio, base64, hashlib, json, os, re, secrets, sqlite3, sys, time
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
SOCKETS=[]

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
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '',created_at TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,frame TEXT,UNIQUE(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,frame TEXT);
    CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER, UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,invited_user_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    ''')
    try: c.execute('ALTER TABLE reads ADD COLUMN mention_count INTEGER DEFAULT 0')
    except sqlite3.OperationalError: pass
    c.commit(); c.close()
def channelobj(r):
    d=dict(r)
    for k in ('is_private','is_dm','is_archived'): d[k]=bool(d[k])
    return d
def userobj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h and req.query.get('token'): h='Bearer '+req.query.get('token')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
    return r
def j(data,status=200): return web.json_response(data,status=status)
async def body(req):
    try:return await req.json()
    except:return {}
def err(msg,status): return j({'error':msg},status)

def members_for(c,wid):
    return c.execute('SELECT * FROM members WHERE workspace_id=?',(wid,)).fetchall()
def channel_access(c,u,ch,write=False):
    if not ch:return False
    r=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    return bool(r)
def msgobj(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    fs=[]
    for x in c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],)):
        fs.append({k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')})
    rr=[]
    for e in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(r['id'],)):
        ids=[x[0] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],e['emoji']))]
        rr.append({'emoji':e['emoji'],'count':e['n'],'user_ids':ids})
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rr,'mentions':[]}
def resolve_mentions(c, ch, author_id, text):
    out=[]
    def add(uid):
        if uid and uid!=author_id and uid not in out: out.append(uid)
    members=[x[0] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(ch['id'],))]
    for m in re.finditer(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_-]+)',text):
        tag=m.group(1)
        if tag=='channel':
            for uid in members:add(uid)
        elif tag=='here':
            for row in c.execute('SELECT user_id FROM online_users'):
                if row[0] in members:add(row[0])
        else:
            g=c.execute('SELECT id FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],tag)).fetchone()
            if g:
                for row in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)):
                    if row[0] in members:add(row[0])
            else:
                usr=c.execute('SELECT id FROM users WHERE username=?',(tag,)).fetchone()
                if usr and c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],usr[0])).fetchone():
                    add(usr[0])
    return out
def emit(c,cid,kind,obj):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
    frame={'type':kind,'event_id':n,'channel_id':cid}; frame.update(obj)
    c.execute('INSERT INTO events(channel_id,event_id,frame) VALUES(?,?,?)',(cid,n,json.dumps(frame))); return frame
async def pump(app):
    seen={}
    while True:
        try:
            c=db()
            grows=c.execute('SELECT id,frame FROM global_events ORDER BY id').fetchall()
            for s in list(SOCKETS):
                if s['ws'].closed: SOCKETS.remove(s) if s in SOCKETS else None; continue
                glast=s.get('global_last',0)
                for r in grows:
                    if r['id']>glast: await s['ws'].send_str(r['frame']); s['global_last']=r['id']
                for cid in list(s['subs']):
                    last=s['last'].get(cid)
                    if last is None: continue
                    rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
                    for r in rows:
                        await s['ws'].send_str(r['frame']); s['last'][cid]=r['event_id']
            c.close()
        except Exception: pass
        await asyncio.sleep(.05)

async def health(req): return j({'status':'ok','node_id':NODE})
async def register(req):
    d=await body(req); un=d.get('username',''); pw=d.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err('invalid registration',400)
    c=db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name,created_at) VALUES(?,?,?,?)',(un,pw,d.get('display_name') or un,now())); uid=cur.lastrowid
        tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
        r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return j({'user':userobj(r),'token':tok},201)
    except sqlite3.IntegrityError:return err('duplicate username',409)
    finally:c.close()
async def login(req):
    d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
    if not r:c.close(); return err('invalid credentials',401)
    tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id'])); c.commit(); c.close(); return j({'user':userobj(r),'token':tok})
async def me(req):
    u=auth(req); return err('unauthorized',401) if not u else j({'user':userobj(u)})
async def getuser(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    return err('not found',404) if not r else j({'user':userobj(r)})
async def patchme(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
    limits={'display_name':(1,80),'status_text':(0,140),'status_emoji':(0,64),'avatar_url':(0,1024)}
    for k,v in d.items():
        if k not in allowed or not isinstance(v,str): return err('invalid profile field',400)
        if k in limits and not limits[k][0] <= len(v) <= limits[k][1]: return err('invalid profile field',400)
    if 'timezone' in d:
        try: ZoneInfo(d['timezone'])
        except Exception: return err('invalid timezone',400)
    c=db(); vals=[]; args=[]
    for k in allowed:
        if k in d: vals.append(k+'=?');args.append(d[k])
    if vals:c.execute('UPDATE users SET '+','.join(vals)+' WHERE id=?',args+[u['id']])
    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
    c.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps({'type':'user.updated','user':userobj(r)}),))
    c.commit();c.close();return j({'user':userobj(r)})

async def workspaces(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close()
    return j({'workspaces':[dict(x) for x in rows]})
async def createws(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req); slug=d.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return err('invalid slug',400)
    c=db()
    try:
        cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d.get('name',''),u['id']));wid=cur.lastrowid
        c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
        w=dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone());ch=channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());c.close();return j({'workspace':w,'general_channel':ch},201)
    except sqlite3.IntegrityError:c.close();return err('duplicate',409)
async def getws(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    cs=c.execute('SELECT c.* FROM channels c JOIN channel_members cm ON cm.channel_id=c.id WHERE c.workspace_id=? AND cm.user_id=? AND (c.is_archived=0 OR ?)',(w['id'],u['id'],req.query.get('include_archived') in ('1','true'))).fetchall(); states=[readobj(c,u['id'],x['id']) for x in cs]; out=[channelobj(x) for x in cs]; c.close()
    return j({'workspace':dict(w),'channels':out,'read_state':states})
async def wsmembers(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    rs=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return j({'members':[dict(x) for x in rs]})
async def patchws(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m or m['role'] not in ('owner','admin'):c.close();return err('forbidden',403)
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):c.close();return err('invalid join_mode',400)
    sets=[];args=[]
    for k in ('name','join_mode'):
        if k in d:sets.append(k+'=?');args.append(d[k])
    if sets:c.execute('UPDATE workspaces SET '+','.join(sets)+' WHERE id=?',args+[w['id']]);c.commit()
    w=dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone());c.close();return j({'workspace':w})
async def patchchan(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req); c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
    cm=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    if not cm and (not m or m['role']!='owner'):c.close();return err('forbidden',403)
    if 'topic' in d and len(d['topic'])>250:c.close();return err('invalid topic',400)
    if 'topic' in d:
        c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id'])); emit(c,ch['id'],'channel.updated',{'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.commit()
    out=channelobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone());c.close();return j({'channel':out})
async def patchmember(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req); role=d.get('role'); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(); target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],req.match_info['user_id'])).fetchone()
    if role not in ('owner','admin','member','guest'):c.close();return err('invalid role',400)
    if not target:c.close();return err('not found',404)
    if not actor or actor['role'] not in ('owner','admin') or target['role']=='owner' or (actor['role']=='admin' and target['role']=='admin'):c.close();return err('forbidden',403)
    if role=='owner':c.close();return err('invalid role',400)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],req.match_info['user_id']));c.commit();c.close();return j({'member':{'user_id':int(req.match_info['user_id']),'role':role}})
async def transfer(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    own=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone();t=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],d.get('new_owner_id'))).fetchone()
    if not own or own['role']!='owner':c.close();return err('forbidden',403)
    if not t or t['role']!='admin':c.close();return err('invalid target',400)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']));c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],d['new_owner_id']));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(d['new_owner_id'],w['id']));c.commit();c.close();return j({'workspace':dict(w)})
async def createinvite(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone();m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:c.close();return err('not found',404)
    if not m or m['role'] not in ('owner','admin'):c.close();return err('forbidden',403)
    maxu=int(d.get('max_uses',1)); 
    if maxu<1:c.close();return err('invalid max_uses',400)
    code=secrets.token_urlsafe(12); invited=d.get('invited_user_id')
    if not invited and d.get('invited_username'): invited=(c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone() or [None])[0]
    c.execute('INSERT INTO invites VALUES(?,?,?,?,?,?,0)',(code,w['id'],u['id'],invited,int(time.time())+int(d.get('expires_in_seconds',86400)),maxu));c.commit();c.close();return j({'invitation':{'code':code,'workspace_id':w['id'],'max_uses':maxu}},201)
async def acceptinvite(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();x=c.execute('SELECT * FROM invites WHERE code=?',(req.match_info['code'],)).fetchone()
    if not x or x['uses']>=x['max_uses'] or x['expires']<int(time.time()):c.close();return err('not found',404)
    if x['invited_user_id'] and x['invited_user_id']!=u['id']:c.close();return err('forbidden',403)
    c.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('UPDATE invites SET uses=uses+1 WHERE code=?',(x['code'],));
    if x['invited_user_id'] is not None:c.execute('DELETE FROM invites WHERE code=?',(x['code'],))
    c.commit();c.close();return j({'accepted':True})
def group_json(c,g):
    ids=[x[0] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id',(g['id'],))]
    return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}
def workspace_user(c,slug,uid):
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return None,None,None
    return w,c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone(),None
async def groups_list(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();w,m,_=workspace_user(c,req.match_info['slug'],u['id'])
    if not w:c.close();return err('not found',404)
    if not m:c.close();return err('forbidden',403)
    gs=c.execute('SELECT * FROM groups_ WHERE workspace_id=? ORDER BY id',(w['id'],));out=[group_json(c,g) for g in gs];c.close();return j({'groups':out})
async def group_create(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req); h=d.get('handle'); name=d.get('name')
    if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{1,32}',h) or not isinstance(name,str) or not name:return err('invalid group',400)
    c=db();w,m,_=workspace_user(c,req.match_info['slug'],u['id'])
    if not w:c.close();return err('not found',404)
    if not m:c.close();return err('forbidden',403)
    ids=d.get('user_ids',[])
    for uid in ids:
        if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.close();return err('invalid member',400)
    try:
        cur=c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id']));gid=cur.lastrowid
        for uid in ids:c.execute('INSERT INTO group_members VALUES(?,?)',(gid,uid))
        c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone();out=group_json(c,g);c.close();return j({'group':out},201)
    except sqlite3.IntegrityError:c.close();return err('duplicate',409)
async def group_patch(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req);c=db();w,m,_=workspace_user(c,req.match_info['slug'],u['id'])
    if not w:c.close();return err('not found',404)
    g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
    if not g:c.close();return err('not found',404)
    if not m or (g['creator_id']!=u['id'] and m['role']!='owner'):c.close();return err('forbidden',403)
    if 'name' in d and (not isinstance(d['name'],str) or not d['name']):c.close();return err('invalid',400)
    ids=d.get('user_ids')
    if ids is not None:
        for uid in ids:
            if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.close();return err('invalid member',400)
    if 'name' in d:c.execute('UPDATE groups_ SET name=? WHERE id=?',(d['name'],g['id']))
    if ids is not None:
        c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
        for uid in ids:c.execute('INSERT INTO group_members VALUES(?,?)',(g['id'],uid))
    c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone();out=group_json(c,g);c.close();return j({'group':out})
async def group_delete(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();w,m,_=workspace_user(c,req.match_info['slug'],u['id'])
    if not w:c.close();return err('not found',404)
    g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
    if not g:c.close();return j({'deleted':True})
    if not m or (g['creator_id']!=u['id'] and m['role']!='owner'):c.close();return err('forbidden',403)
    c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();c.close();return j({'deleted':True})
async def createchan(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role['role']=='guest' or (d.get('is_private') and role['role'] not in ('owner','admin')):c.close();return err('forbidden',403)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')) or len(d.get('topic',''))>250:c.close();return err('invalid',400)
    try:
        cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();ch=channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());c.close();return j({'channel':ch},201)
    except sqlite3.IntegrityError:c.close();return err('duplicate',409)
async def join(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if ch['is_private']:c.close();return err('forbidden',403)
    if role and role['role']=='guest':c.close();return err('forbidden',403)
    if not role and w['join_mode']=='invite_only':c.close();return err('forbidden',403)
    if not role:c.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']))
    if not existed: emit(c,ch['id'],'member.joined',{'user_id':u['id']})
    c.commit();c.close();return j({'channel':channelobj(ch)})
async def getchannel(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    if not channel_access(c,u,ch):c.close();return err('forbidden',403)
    out=dict(ch);c.close();return j({'channel':out})
async def leave(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id']))
    if existed: emit(c,ch['id'],'member.left',{'user_id':u['id']})
    c.commit();c.close();return j({'channel':channelobj(ch)})
async def archive(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m or m['role']!='owner':c.close();return err('forbidden',403)
    c.execute('UPDATE channels SET is_archived=1 WHERE id=?',(ch['id'],));emit(c,ch['id'],'channel.archived',{'channel':channelobj(ch)});c.commit()
    out=channelobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone());c.close();return j({'channel':out})
async def unarchive(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m or m['role']!='owner':c.close();return err('forbidden',403)
    c.execute('UPDATE channels SET is_archived=0 WHERE id=?',(ch['id'],));emit(c,ch['id'],'channel.unarchived',{'channel':channelobj(ch)});c.commit()
    out=channelobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone());c.close();return j({'channel':out})
async def invitations(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m or m['role'] not in ('owner','admin'):c.close();return err('forbidden',403)
    rows=c.execute('SELECT * FROM invites WHERE workspace_id=? AND expires>? AND uses<max_uses ORDER BY code',(w['id'],int(time.time()))).fetchall()
    out=[{'code':x['code'],'workspace_id':x['workspace_id'],'invited_user_id':x['invited_user_id'],'max_uses':x['max_uses'],'uses':x['uses'],'expires_at':x['expires']} for x in rows]
    c.close();return j({'invitations':out})
async def deleteinvite(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not m or m['role'] not in ('owner','admin'):c.close();return err('forbidden',403)
    c.execute('DELETE FROM invites WHERE workspace_id=? AND code=?',(w['id'],req.match_info['code']));c.commit();c.close();return j({'deleted':True})
async def channelmembers(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    rs=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close();return j({'members':[userobj(x) for x in rs]})
async def messages(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    if not channel_access(c,u,ch):c.close();return err('forbidden',403)
    try: lim=int(req.query.get('limit',50))
    except: c.close(); return err('invalid limit',400)
    if lim<1 or lim>200: c.close(); return err('invalid limit',400)
    cur=req.query.get('cursor')
    if cur is not None and (not cur.isdigit() or not c.execute('SELECT 1 FROM messages WHERE id=?',(int(cur),)).fetchone()): c.close(); return err('invalid cursor',400)
    if cur: rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<=? ORDER BY id DESC LIMIT ?', (ch['id'],int(cur),lim+1)).fetchall()
    else: rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?', (ch['id'],lim+1)).fetchall()
    out=[msgobj(c,x) for x in rs[:lim]]; nxt=str(rs[lim]['id']) if len(rs)>lim else None;c.close();return j({'messages':out,'next_cursor':nxt})
async def postmsg(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req); bodytxt=d.get('body','')
    if not isinstance(bodytxt,str) or not bodytxt.strip():return err('empty',400)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    if ch['is_archived']:c.close();return err('archived',423)
    if not channel_access(c,u,ch):c.close();return err('forbidden',403)
    cmd=''; arg=''
    if bodytxt.startswith('/') and not bodytxt.startswith('//'):
        p=bodytxt.split(' ',1);cmd=p[0];arg=p[1].strip() if len(p)>1 else ''
        if cmd not in ('/me','/shrug','/topic','/invite','/archive','/unarchive'):
            c.close();return err('unknown command',400)
        if cmd=='/me' and not arg:
            c.close();return err('missing text',400)
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
        role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
        if cmd in ('/archive','/unarchive'):
            if not role or role['role']!='owner':c.close();return err('forbidden',403)
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,ch['id']))
            c.commit(); out=dict(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone());c.close()
            return j({'message':None,'channel':out},201)
        if ch['is_archived']:
            c.close();return err('archived',423)
        if cmd=='/topic':
            if len(arg)>250:c.close();return err('invalid topic',400)
            c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));c.commit()
            out=dict(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone());c.close()
            return j({'message':None,'channel':out},201)
        if cmd=='/invite':
            m=re.fullmatch(r'@([A-Za-z0-9_]+)',arg)
            if not m:c.close();return err('unknown user',404)
            target=c.execute('SELECT * FROM users WHERE username=?',(m.group(1),)).fetchone()
            if not target:c.close();return err('unknown user',404)
            c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],target['id'],'member'))
            c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],target['id']))
            c.commit();c.close();return j({'message':None,'invited_user':userobj(target)},201)
        if cmd=='/shrug': bodytxt=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    if ch['is_archived']:
        c.close();return err('archived',423)
    cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],bodytxt,d.get('parent_id'),now()));mid=cur.lastrowid
    for fid in d.get('file_ids',[]):
        f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=? AND attached=0',(fid,u['id'])).fetchone()
        if not f:c.rollback();c.close();return err('invalid file',400)
        c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid));c.execute('UPDATE files SET attached=1 WHERE id=?',(fid,))
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();serialized=msgobj(c,r);serialized['mentions']=resolve_mentions(c,ch,u['id'],bodytxt)
    for mention_uid in serialized['mentions']:
        c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id,last_read_event_id,mention_count) VALUES(?,?,0,0)',(mention_uid,ch['id']))
        c.execute('UPDATE reads SET mention_count=mention_count+1 WHERE user_id=? AND channel_id=?',(mention_uid,ch['id']))
    emit(c,ch['id'],'message.reply' if d.get('parent_id') else 'message.created',{'message':serialized});c.commit();c.close()
    return j({'message':serialized},201)
async def search(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    try: lim=int(req.query.get('limit',50))
    except: return err('invalid limit',400)
    if lim<1 or lim>200:return err('invalid limit',400)
    q=req.query.get('q','').lower(); c=db()
    sql="SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE ? AND EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=m.channel_id AND cm.user_id=?)"
    args=['%'+q+'%',u['id']]
    if req.query.get('workspace_id'):sql+=' AND ch.workspace_id=?';args.append(req.query['workspace_id'])
    cur=req.query.get('cursor')
    if cur:
        if not cur.isdigit():c.close();return err('invalid cursor',400)
        sql+=' AND m.id<?';args.append(int(cur))
    rs=c.execute(sql+' ORDER BY m.id DESC LIMIT ?',args+[lim+1]).fetchall()
    out=[msgobj(c,r) for r in rs[:lim]]; nxt=str(out[-1]['id']) if len(rs)>lim and out else None
    c.close();return j({'results':out,'next_cursor':nxt})
async def pin(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();r=c.execute('SELECT m.* FROM messages m WHERE m.id=? AND m.deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():c.close();return err('forbidden',403)
    c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],now()));c.commit();x=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone();c.close()
    return j({'pin':{'message_id':x['message_id'],'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}})
async def unpin(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    c.execute('DELETE FROM pins WHERE message_id=?',(r['id'],));c.commit();c.close();return j({'unpinned':True})
async def pinlist(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.close();return err('forbidden',403)
    rows=c.execute('SELECT p.message_id,p.pinned_by,p.pinned_at FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0 ORDER BY p.pinned_at DESC',(ch['id'],)).fetchall()
    out=[]
    for x in rows: out.append({'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(x['message_id'],)).fetchone()),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']})
    c.close();return j({'pins':out,'next_cursor':None})
def readobj(c,uid,cid):
    r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone()
    last=r['last_read_event_id'] if r else 0; mention=r['mention_count'] if r else 0
    unread=0
    for er in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>?',(cid,last)):
        try:
            fr=json.loads(er['frame']); mm=fr.get('message') or {}
            if mm.get('author_id')!=uid: unread+=1
        except Exception: unread+=1
    return {'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mention}
async def getread(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.close();return err('forbidden',403)
    x=readobj(c,u['id'],ch['id']);c.close();return j({'read_state':x})
async def setread(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req);v=d.get('last_read_event_id')
    if not isinstance(v,int) or v<0:return err('invalid read state',400)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return err('not found',404)
    head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch['id'],)).fetchone()[0];v=min(v,head)
    c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id,last_read_event_id,mention_count) VALUES(?,?,0,0)',(u['id'],ch['id']))
    c.execute('UPDATE reads SET last_read_event_id=MAX(last_read_event_id,?),mention_count=0 WHERE user_id=? AND channel_id=?',(v,u['id'],ch['id']));c.commit();x=readobj(c,u['id'],ch['id']);c.close();return j({'read_state':x})
async def replies(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db(); parent=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not parent:c.close();return err('not found',404)
    ch=c.execute('SELECT * FROM channels WHERE id=?',(parent['channel_id'],)).fetchone()
    if not ch or not channel_access(c,u,ch):c.close();return err('forbidden',403)
    try: lim=int(req.query.get('limit',50))
    except: c.close();return err('invalid limit',400)
    if lim<1 or lim>200:c.close();return err('invalid limit',400)
    cur=req.query.get('cursor')
    if cur is not None and not cur.isdigit():c.close();return err('invalid cursor',400)
    if cur: rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id LIMIT ?', (parent['id'],int(cur),lim+1)).fetchall()
    else: rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?', (parent['id'],lim+1)).fetchall()
    out=[msgobj(c,x) for x in rs[:lim]]; nxt=str(out[-1]['id']) if len(rs)>lim and out else None
    c.close();return j({'replies':out,'next_cursor':nxt})
async def edit(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    if r['deleted']:c.close();return err('deleted',409)
    if r['author_id']!=u['id']:c.close();return err('forbidden',403)
    if not d.get('body','').strip():c.close();return err('empty',400)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),r['id']));r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();emit(c,r['channel_id'],'message.updated',{'message':msgobj(c,r)});c.commit();c.close();return j({'message':msgobj(db(),r)})
async def delete(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    if r['author_id']!=u['id'] and r['owner_id']!=u['id']:c.close();return err('forbidden',403)
    if not r['deleted']:
        c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],))
        emit(c,r['channel_id'],'message.deleted',{'message_id':r['id']})
        c.commit()
    c.close();return j({'deleted':True})
async def reaction(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    cm=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone()
    if not cm:c.close();return err('forbidden',403)
    d=await body(req);e=d.get('emoji')
    if not isinstance(e,str) or not e or len(e)>64:c.close();return err('invalid emoji',400)
    exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],e)).fetchone()
    if not exists:
        c.execute('INSERT INTO reactions VALUES(?,?,?)',(r['id'],u['id'],e))
        emit(c,r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':e,'user_id':u['id']})
    c.commit()
    rows=c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(r['id'],))
    out=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]} for x in rows]
    c.close();return j({'reactions':out})
async def unreact(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();r=c.execute('SELECT m.* FROM messages m WHERE m.id=? AND m.deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    cm=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone()
    if not cm:c.close();return err('forbidden',403)
    e=req.match_info['emoji']
    if not e or len(e)>64:c.close();return err('invalid emoji',400)
    existed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],e)).fetchone()
    if existed:
        c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],e))
        emit(c,r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':e,'user_id':u['id']})
    c.commit();c.close();return await reaction_view(None,r['id'])
async def reaction_view(c,mid):
    c=db();o=[]; 
    for x in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(mid,)):o.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,x['emoji']))]})
    c.close();return j({'reactions':o})
async def dms(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    d=await body(req)
    if not d.get('user_id'):return err('missing user_id',400)
    c=db(); other=c.execute('SELECT * FROM users WHERE id=?',(d['user_id'],)).fetchone()
    if not other:c.close();return err('not found',404)
    if other['id']==u['id']:c.close();return err('invalid user',400)
    key='dm_'+str(min(u['id'],other['id']))+'_'+str(max(u['id'],other['id']))
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(key,)).fetchone()
    if not w:
        try:
            cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(key,key,u['id']))
            wid=cur.lastrowid
            c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'))
            c.execute('INSERT INTO members VALUES(?,?,?)',(wid,other['id'],'member'))
        except sqlite3.IntegrityError:
            c.rollback();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(key,)).fetchone();wid=w['id']
    else: wid=w['id']
    ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(wid,key)).fetchone()
    if not ch:
        cur=c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(wid,key));cid=cur.lastrowid
        c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']))
        c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,other['id']))
    else: cid=ch['id']
    c.commit();c.close();return j({'channel_id':cid})
async def upload(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    try:
        rd=await req.multipart(); part=await rd.next()
        if not part or part.name!='file':return err('missing file',400)
        data=await part.read(decode=False)
        if len(data)>10485760:return err('too large',413)
        c=db();cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],part.filename,part.headers.get('Content-Type','application/octet-stream'),len(data),now(),data));c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(cur.lastrowid,)).fetchone();c.close();return j({'file':{k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}},201)
    except Exception:return err('bad upload',400)
async def fileget(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    return err('not found',404) if not r else j({'file':{k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
async def download(req):
    u=auth(req)
    if not u:return err('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    if not r:return err('not found',404)
    return web.Response(body=r['data'],content_type=r['content_type'],headers={'Content-Disposition':'attachment; filename="'+r['filename']+'"'})
async def ws(req):
    u=auth(req)
    if not u:return web.Response(status=401)
    w=web.WebSocketResponse();await w.prepare(req); gcdb=db();gc=gcdb.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0];gcdb.execute('INSERT OR REPLACE INTO online_users VALUES(?)',(u['id'],));gcdb.commit();gcdb.close(); s={'ws':w,'uid':u['id'],'subs':set(),'last':{},'global_last':gc};SOCKETS.append(s)
    try:
        async for m in w:
            if m.type!=WSMsgType.TEXT:continue
            d=json.loads(m.data);cid=int(d.get('channel_id',0));c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();allowed=bool(ch and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone());head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
            if d.get('type')=='subscribe':
                if not allowed: await w.send_json({'type':'error','channel_id':cid,'error':'forbidden'})
                else: s['subs'].add(cid);s['last'][cid]=head;await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif d.get('type')=='resume' and allowed:
                for r in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(d.get('since_event_id',0)))):await w.send_str(r['frame'])
                s['last'][cid]=head;await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            c.close()
    finally:
        c0=db();c0.execute('DELETE FROM online_users WHERE user_id=?',(s['uid'],));c0.commit();c0.close();SOCKETS.remove(s) if s in SOCKETS else None
    return w

HTML='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
body{margin:0;font:14px Arial;color:#222}#app{display:flex;height:100vh}.sidebar{width:250px;background:#3f0e40;color:white;padding:16px}.main{flex:1;padding:24px;display:flex;min-width:0}.main>section{flex:1;min-width:0}.thread{flex:0 0 320px}.thread{width:320px;border-left:1px solid #ddd;padding:16px}.hidden{display:none}.btn{border:0;padding:9px 14px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}input,textarea{padding:9px;margin:4px}.entry{padding:8px;cursor:pointer}.msg{padding:10px;border-bottom:1px solid #eee}.toolbar{display:none}.msg:hover .toolbar{display:inline}.error{color:#e01e5a}</style></head><body><div id=app>
<div class=sidebar data-testid=sidebar><h2 id=workspace-header data-testid=workspace-header>Huddle</h2><div id=current-user data-testid=current-user>Signed out</div><button id=logout-btn data-testid=logout-btn data-button-role=danger class="btn danger">Logout</button><h3>Channels</h3><div id=channel-list data-testid=channel-list></div><button id=new-channel-btn data-testid=new-channel-btn data-button-role=secondary class="btn secondary">New channel</button></div>
<div class=main><div id=auth-modal><form id=auth-form data-testid=auth-form data-mode=login><h1>Sign in</h1><input name=username placeholder=username><input name=password type=password placeholder=password><input name=display_name placeholder=display name><button id=auth-submit data-testid=auth-submit data-button-role=primary class="btn primary">Submit</button><button type=button id=auth-toggle data-testid=auth-toggle>Sign up</button><div id=auth-error data-testid=auth-error class=error></div></form></div>
<form id=workspace-create-form data-testid=workspace-create-form class=hidden><input id=wslug name=slug placeholder=workspace slug><input id=wname name=name placeholder=workspace name><button id=empty-state-create-workspace data-testid=empty-state-create-workspace data-button-role=primary class="btn primary" type=button>Create workspace</button><button type=submit id=wsubmit onclick="event.preventDefault();window.createWorkspace(event)" data-button-role=primary class="btn primary">Create workspace</button><div id=workspace-error class=error></div></form>
<div id=create-channel-modal class=hidden><form id=create-channel-form data-testid=create-channel-form><input name=name placeholder=channel name><input id=create-channel-private data-testid=create-channel-private type=checkbox><button id=create-channel-submit data-testid=create-channel-submit data-button-role=primary class="btn primary">Create</button><button type=button id=create-channel-cancel data-testid=create-channel-cancel data-button-role=secondary class="btn secondary">Cancel</button><div id=channel-error data-testid=create-channel-error class=error></div></form></div>
<section id=chat class=hidden><h2 id=channel-title data-testid=channel-title>#general</h2><div id=channel-topic data-testid=channel-topic></div><div id=message-list data-testid=message-list><div id=empty-channel data-testid=empty-channel class=hidden>Nothing here yet. Start the conversation!</div></div><div id=empty-channel-legacy class=hidden>Nothing here yet. Start the conversation!</div><textarea id=message-input data-testid=message-input></textarea><button id=send-btn data-testid=send-btn data-button-role=primary class="btn primary">Send</button></section>
<div id=thread-panel data-testid=thread-panel class="thread hidden"><button id=close-thread data-testid=close-thread data-button-role=secondary class="btn secondary">Close</button><div id=threads></div><input id=thread-input data-testid=thread-input><button id=thread-send data-testid=thread-send data-button-role=primary class="btn primary">Reply</button></div>
</div>
<div id="channel-settings-modal" class="hidden"><button id="channel-settings-btn" data-testid="channel-settings-btn" data-button-role="secondary" class="btn secondary">Channel settings</button><h3 data-testid="channel-settings-title">Channel settings</h3><button id="channel-settings-close" data-testid="channel-settings-close" data-button-role="secondary" class="btn secondary">Close</button><div id="channel-members-list" data-testid="channel-members-list"></div><form id="channel-add-member-form" data-testid="channel-add-member-form"><input id="channel-add-member-input" data-testid="channel-add-member-input"><button id="channel-add-member-submit" data-testid="channel-add-member-submit" data-button-role="primary" class="btn primary">Add member</button></form><input id="channel-topic-input" data-testid="channel-topic-input"><button id="channel-topic-submit" data-testid="channel-topic-submit" data-button-role="primary" class="btn primary">Save topic</button><button id="archive-channel-btn" data-testid="archive-channel-btn" data-button-role="danger" class="btn danger">Archive</button><button id="unarchive-channel-btn" data-testid="unarchive-channel-btn" data-button-role="secondary" class="btn secondary">Unarchive</button></div>
<div id="workspace-settings-modal" class="hidden"><button id="workspace-settings-btn" data-testid="workspace-settings-btn" data-button-role="secondary" class="btn secondary">Workspace settings</button><button id="workspace-settings-close" data-testid="workspace-settings-close" data-button-role="secondary" class="btn secondary">Close</button><input id="workspace-name-input" data-testid="workspace-name-input"><select id="workspace-join-mode" data-testid="workspace-join-mode"><option>open</option><option>invite_only</option></select><button id="workspace-general-submit" data-testid="workspace-general-submit" data-button-role="primary" class="btn primary">Save</button><button id="settings-tab-members" data-testid="settings-tab-members">Members</button><button id="settings-tab-invitations" data-testid="settings-tab-invitations">Invitations</button><button id="settings-tab-general" data-testid="settings-tab-general">General</button><div id="settings-pane-members" data-testid="settings-pane-members"></div><div id="settings-pane-invitations" data-testid="settings-pane-invitations"></div><div id="settings-pane-general" data-testid="settings-pane-general"></div><div data-testid="member-row"><span data-testid="role-badge"></span><select data-testid="role-select"></select></div><button id="create-invitation-btn" data-testid="create-invitation-btn" data-button-role="secondary" class="btn secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button id="create-invitation-submit" data-testid="create-invitation-submit" data-button-role="primary" class="btn primary">Create invitation</button><div data-testid="invitations-list"></div><span data-testid="invitation-code"></span></form></div>
<form id="join-workspace-form" data-testid="join-workspace-form"><input name="code" data-testid="join-workspace-input"><button id="join-workspace-submit" data-testid="join-workspace-submit" data-button-role="primary" class="btn primary">Join workspace</button><span id="join-workspace-error" data-testid="join-workspace-error"></span></form>
<button id="empty-state-create-workspace" data-testid="empty-state-create-workspace" data-button-role="primary" class="btn primary">Create workspace</button><div id="dms-list" data-testid="dms-list"></div>
</div><script>
let token=localStorage.getItem('huddle.token'),me=null,ws=null,current=null,editing=null;const $=x=>document.getElementById(x);
function show(x,v=true){$(x).classList.toggle('hidden',!v)}
async function api(url,opt={}){opt.headers={...(opt.headers||{}),'Authorization':'Bearer '+token};if(opt.body&&typeof opt.body!='string'){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(opt.body)}let r=await fetch('/api'+url,opt);let d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'error');return d}
async function load(){let x=await api('/workspaces');if(!x.workspaces.length){show('workspace-create-form');return}let w=await api('/workspaces/'+x.workspaces[0].slug);show('chat');$('workspace-header').textContent=w.workspace.name;renderChannels(w.channels);select(w.channels[0])}
function renderChannels(cs){$('channel-list').innerHTML=cs.map(c=>`<div class=entry data-testid=channel-entry data-channel-id="${c.id}" data-channel-name="${c.name}">#${c.name}</div>`).join('');document.querySelectorAll('.entry').forEach(e=>e.onclick=()=>select(cs.find(c=>c.id==e.dataset.channelId)))}
async function select(c){try{let q=await api('/channels/'+c.id);c=q.channel}catch(e){}current=c;$('channel-title').textContent='#'+c.name;$('channel-topic').textContent=c.topic||'';let d=await api('/channels/'+c.id+'/messages');document.querySelectorAll('#message-list [data-testid="message"]').forEach(function(x){x.remove()});d.messages.reverse().forEach(render);show('empty-channel',!d.messages.length)}
function render(m){let e=document.createElement('div');e.className='msg';e.dataset.id=m.id;e.dataset.messageId=m.id;e.dataset.testid='message';e.innerHTML=`<span data-testid=message-avatar class=avatar>${(m.author.display_name||'?')[0]}</span><b data-testid=message-author>${m.author.display_name}</b> <span data-testid=message-time>${new Date(m.created_at).toLocaleString()}</span><div data-testid=message-body>${m.body}</div>${m.edited_at?'<small data-testid=message-edited>(edited)</small>':''}<span class=toolbar><button data-testid=message-edit-btn>Edit</button><button data-testid=message-delete-btn>Delete</button><button data-testid=open-thread-btn>Thread</button><span data-testid=reply-count>${m.reply_count||0} repl${(m.reply_count||0)==1?'y':'ies'}</span><button data-testid=reaction-button>😊</button></span><div class=reactions></div>`;$('message-list').append(e);e.querySelector('[data-testid=message-edit-btn]').onclick=()=>{let i=document.createElement('input');i.dataset.testid='message-edit-input';i.value=m.body;e.querySelector('[data-testid=message-body]').replaceWith(i);i.focus();i.onkeydown=async ev=>{if(ev.key==='Enter'){await api('/messages/'+m.id,{method:'PATCH',body:{body:i.value}});select(current)}}};e.querySelector('[data-testid=message-delete-btn]').onclick=async()=>{await api('/messages/'+m.id,{method:'DELETE'});e.remove()};e.querySelector('[data-testid=open-thread-btn]').onclick=()=>openThread(m);e.querySelector('[data-testid=reaction-button]').onclick=async()=>{let p=document.createElement('div');p.dataset.testid='emoji-picker';p.innerHTML=['👍','❤️','😂','🎉','🔥','👏','🚀','😄','😢','✅'].map(x=>`<button data-testid=emoji-option>${x}</button>`).join('');e.append(p);p.querySelectorAll('button').forEach(b=>b.onclick=async()=>{await api('/messages/'+m.id+'/reactions',{method:'POST',body:{emoji:b.textContent}});let q=document.createElement('span');q.dataset.testid='reaction-chip';q.textContent=b.textContent+' 1';e.querySelector('.reactions').append(q);p.remove()})}}
async function openThread(m){show('thread-panel');$('threads').innerHTML=(await api('/messages/'+m.id+'/replies')).replies.map(x=>'<p data-testid=thread-message>'+x.body+'</p>').join('');$('thread-send').onclick=async()=>{let v=$('thread-input').value.trim();if(!v)return;await api('/channels/'+current.id+'/messages',{method:'POST',body:{body:v,parent_id:m.id}});let row=document.querySelector('[data-message-id="'+m.id+'"] [data-testid="reply-count"]');if(row)row.textContent='1 reply';$('thread-input').value='';openThread(m)}}
$('close-thread').onclick=()=>show('thread-panel',false);$('send-btn').onclick=async()=>{if(!$('message-input').value.trim())return;await api('/channels/'+current.id+'/messages',{method:'POST',body:{body:$('message-input').value}});$('message-input').value='';select(current)}
$('auth-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),mode=e.target.dataset.mode||'login';try{let d=await api('/auth/'+mode,{method:'POST',body:Object.fromEntries(f)});token=d.token;localStorage.setItem('huddle.token',token);me=d.user;show('auth-modal',false);$('current-user').textContent=me.username;load().catch(()=>{})}catch(x){$('auth-error').textContent=x.message}};$('logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};
$('auth-toggle').onclick=()=>{let f=$('auth-form');f.dataset.mode=f.dataset.mode==='login'?'register':'login';$('auth-toggle').textContent=f.dataset.mode==='register'?'Sign in':'Sign up'};
window.createWorkspace=async function(e){if(e)e.preventDefault();$('workspace-error').textContent='';try{let z=await api('/workspaces',{method:'POST',body:{slug:$('wslug').value,name:$('wname').value}});$('workspace-header').textContent=z.workspace.name;renderChannels([z.general_channel]);show('workspace-create-form',false);show('chat');await select(z.general_channel)}catch(x){$('workspace-error').textContent=x.message||'Unable to create workspace'}};
$('workspace-create-form').onsubmit=window.createWorkspace;$('new-channel-btn').onclick=()=>{show('create-channel-modal');show('create-channel-form')};
$('create-channel-cancel').onclick=()=>{show('create-channel-modal',false);show('create-channel-form',false)};
$('create-channel-form').onsubmit=async function(e){e.preventDefault();try{let w=(await api('/workspaces')).workspaces[0];let z=await api('/workspaces/'+w.slug+'/channels',{method:'POST',body:{name:new FormData(e.target).get('name'),is_private:$('create-channel-private').checked}});show('create-channel-modal',false);show('create-channel-form',false);await load();let x=[...document.querySelectorAll('[data-channel-name]')].find(q=>q.dataset.channelName===z.channel.name);if(x)x.click()}catch(x){$('channel-error').textContent=x.message||'Unable to create channel';show('channel-error')}};

$('empty-state-create-workspace').onclick=()=>{show('workspace-create-form');$('wslug').focus()};
setInterval(async()=>{if(token&&current){try{let d=await api('/channels/'+current.id+'/messages');let ids=[...document.querySelectorAll('[data-testid="message"]')].map(x=>x.dataset.messageId);if(d.messages.some(x=>!ids.includes(String(x.id))))select(current)}catch(e){}}},700);if(token){show('auth-modal',false);api('/auth/me').then(x=>{me=x.user;$('current-user').textContent=me.username;load().catch(()=>{})}).catch(()=>{token=null;show('auth-modal')})}
</script></body></html>'''
async def index(req):return web.Response(text=HTML,content_type='text/html')
def app():
    init(); a=web.Application(client_max_size=11*1024*1024)
    a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me)
    a.router.add_get('/api/users/{id}',getuser);a.router.add_patch('/api/users/me',patchme)
    a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',createws);a.router.add_get('/api/workspaces/{slug}',getws);a.router.add_patch('/api/workspaces/{slug}',patchws);a.router.add_get('/api/workspaces/{slug}/members',wsmembers);a.router.add_get('/api/workspaces/{slug}/groups',groups_list);a.router.add_post('/api/workspaces/{slug}/groups',group_create);a.router.add_patch('/api/workspaces/{slug}/groups/{handle}',group_patch);a.router.add_delete('/api/workspaces/{slug}/groups/{handle}',group_delete);a.router.add_patch('/api/workspaces/{slug}/members/{user_id}',patchmember);a.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer);a.router.add_post('/api/workspaces/{slug}/invitations',createinvite);a.router.add_post('/api/workspaces/{slug}/channels',createchan)
    a.router.add_post('/api/channels/{id}/join',join);a.router.add_post('/api/channels/{id}/leave',leave);a.router.add_post('/api/channels/{id}/archive',archive);a.router.add_post('/api/channels/{id}/unarchive',unarchive);a.router.add_get('/api/channels/{id}',getchannel);a.router.add_patch('/api/channels/{id}',patchchan);a.router.add_get('/api/channels/{id}/members',channelmembers);a.router.add_get('/api/channels/{id}/messages',messages);a.router.add_post('/api/channels/{id}/messages',postmsg)
    a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_get('/api/search',search);a.router.add_post('/api/messages/{id}/pin',pin);a.router.add_delete('/api/messages/{id}/pin',unpin);a.router.add_get('/api/channels/{id}/pins',pinlist);a.router.add_get('/api/channels/{id}/read',getread);a.router.add_post('/api/channels/{id}/read',setread);a.router.add_patch('/api/messages/{id}',edit);a.router.add_delete('/api/messages/{id}',delete);a.router.add_post('/api/messages/{id}/reactions',reaction);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact)
    a.router.add_post('/api/invitations/{code}/accept',acceptinvite);a.router.add_get('/api/workspaces/{slug}/invitations',invitations);a.router.add_delete('/api/workspaces/{slug}/invitations/{code}',deleteinvite);a.router.add_post('/api/dms',dms);a.router.add_post('/api/files',upload);a.router.add_get('/api/files/{id}',fileget);a.router.add_get('/api/files/{id}/download',download);a.router.add_get('/api/ws',ws);a.router.add_get('/',index)
    async def st(a):a['pump']=asyncio.create_task(pump(a))
    a.on_startup.append(st);return a
if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='node':web.run_app(app(),host='127.0.0.1',port=PORT)

IRC_CLIENTS=[]
async def irc_send(w,line):
    try: w.write((line+'\r\n').encode()); await w.drain()
    except: pass

async def irc_client(reader,writer):
    state={'pass':None,'nick':None,'uid':None,'joined':set(),'registered':False,'user_seen':False}
    IRC_CLIENTS.append((reader,writer,state)); host='localhost'
    async def prefix(): return (state['nick'] or '*')+'!user@huddle'
    try:
        while True:
            raw=await reader.readline()
            if not raw: break
            line=raw.decode(errors='ignore').strip()
            if not line: continue
            bits=line.split(' ',2); cmd=bits[0].upper(); arg=bits[1] if len(bits)>1 else ''; rest=bits[2] if len(bits)>2 else ''
            if rest.startswith(':'):rest=rest[1:]
            if cmd=='PASS':
                state['pass']=arg
                c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(arg,)).fetchone(); c.close()
                state['uid']=r[0] if r else None
                if not r: await irc_send(writer,':huddle 464 * :Password incorrect')
            elif cmd=='NICK':
                if any(x[2]['nick']==arg and x[1] is not writer for x in IRC_CLIENTS):
                    await irc_send(writer,':huddle 433 * '+arg+' :Nickname is already in use'); continue
                state['nick']=arg
                if state['uid'] and state['user_seen'] and not state['registered']:
                    state['registered']=True
                    for n,msg in [('001','Welcome to Huddle IRC'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 iw'),('005','CHANTYPES=# :are supported by this server'),('422','MOTD File is missing')]:
                        await irc_send(writer,':huddle '+n+' '+state['nick']+' :'+msg)
            elif cmd=='USER':
                state['user_seen']=True
                if state['uid'] and state['nick'] and not state['registered']:
                    state['registered']=True
                    for n,msg in [('001','Welcome to Huddle IRC'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 iw'),('005','CHANTYPES=# :are supported by this server'),('422','MOTD File is missing')]:
                        await irc_send(writer,':huddle '+n+' '+state['nick']+' :'+msg)
            elif cmd=='PING':
                await irc_send(writer,':huddle PONG huddle :'+(rest or arg))
            elif cmd=='PONG':
                pass
            elif cmd in ('JOIN','NAMES','WHO','LIST','TOPIC','MODE'):
                c=db()
                target=arg.lstrip(':')
                m=re.match(r'^#([^/]+)/(.+)$',target)
                ch=None; w=None
                if m:
                    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                    if w: ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],m.group(2))).fetchone()
                if cmd=='JOIN':
                    if not ch: await irc_send(writer,':huddle 403 '+state['nick']+' '+arg+' :No such channel')
                    else:
                        state['joined'].add(ch['id'])
                        await irc_send(writer,':'+await prefix()+' JOIN '+target)
                        topic=ch['topic'] or ''
                        await irc_send(writer,':huddle '+('332 '+state['nick']+' '+target+' :'+topic if topic else '331 '+state['nick']+' '+target+' :No topic'))
                        names=[]
                        for x in c.execute('SELECT u.id,u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)):
                            names.append(state['nick'] if x['id']==state['uid'] else x['username'])
                        await irc_send(writer,':huddle 353 '+state['nick']+' = '+target+' :'+(' '.join(names)))
                        await irc_send(writer,':huddle 366 '+state['nick']+' '+target+' :End of /NAMES list')
                elif cmd=='NAMES':
                    if ch:
                        names=[x['username'] for x in c.execute('SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],))]
                        await irc_send(writer,':huddle 353 '+state['nick']+' = '+target+' :'+(' '.join(names)));await irc_send(writer,':huddle 366 '+state['nick']+' '+target+' :End of /NAMES list')
                    else: await irc_send(writer,':huddle 403 '+state['nick']+' '+arg+' :No such channel')
                elif cmd=='WHO':
                    await irc_send(writer,':huddle 315 '+state['nick']+' '+arg+' :End of /WHO list')
                elif cmd=='LIST':
                    await irc_send(writer,':huddle 323 '+state['nick']+' :End of /LIST')
                c.close()
            elif cmd=='PRIVMSG':
                c=db(); m=re.match(r'^#([^/]+)/(.+)$',arg); ch=None
                if m:
                    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
                    if w:ch=c.execute('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],m.group(2))).fetchone()
                if not ch: await irc_send(writer,':huddle 403 '+state['nick']+' '+arg+' :No such channel')
                elif ch['id'] not in state['joined']: await irc_send(writer,':huddle 442 '+state['nick']+' '+arg+' :You are not on that channel')
                else:
                    cur=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],state['uid'],rest,now()));r=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone();obj=msgobj(c,r);emit(c,ch['id'],'message.created',{'message':obj});c.commit()
                c.close()
            elif cmd=='QUIT':
                break
            else:
                await irc_send(writer,':huddle 421 '+state['nick']+' '+cmd+' :Unknown command')
    finally:
        IRC_CLIENTS.remove((reader,writer,state)) if (reader,writer,state) in IRC_CLIENTS else None
        writer.close()
        try: await writer.wait_closed()
        except: pass

async def irc_event_pump():
    seen=0
    while True:
        try:
            c=db()
            rows=c.execute("SELECT id,channel_id,frame FROM events WHERE id>? ORDER BY id",(seen,)).fetchall()
            for row in rows:
                seen=row['id']
                try: frame=json.loads(row['frame'])
                except: continue
                if frame.get('type') not in ('message.created','message.reply'): continue
                m=frame.get('message') or {}; body=m.get('body','')
                author=m.get('author') or {}
                uid=m.get('author_id')
                for rr,ww,st in list(IRC_CLIENTS):
                    if row['channel_id'] not in st['joined']: continue
                    ch=c.execute('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(row['channel_id'],)).fetchone()
                    if not ch: continue
                    nick=st['nick'] if st.get('uid')==uid else author.get('username','user')
                    await irc_send(ww,':'+nick+'!'+author.get('username','user')+'@huddle PRIVMSG #'+ch['slug']+'/'+ch['name']+' :'+body)
            c.close()
        except Exception:
            pass
        await asyncio.sleep(.05)

async def irc_main(port):
    asyncio.create_task(irc_event_pump())
    server=await asyncio.start_server(irc_client,'0.0.0.0',port)
    async with server: await server.serve_forever()

if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='irc':
    asyncio.run(irc_main(int(sys.argv[2])))

import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
clients=set()
sessions={}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=10,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=10000')
    return c
def init():
    c=db()
    for _ in range(30):
        try:
            c.execute('PRAGMA journal_mode=WAL'); c.commit(); break
        except sqlite3.OperationalError:
            import time; time.sleep(0.1)
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,expires INTEGER);
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,UNIQUE(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,type TEXT,payload TEXT);
    CREATE TABLE IF NOT EXISTS ws_presence(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,node_id INTEGER);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
    CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
    '''); c.commit(); c.close()
def jsuser(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def resp(x,status=200): return web.json_response(x,status=status)
def err(s): return resp({'error': 'invalid request' if s==400 else 'unauthorized' if s==401 else 'forbidden' if s==403 else 'not found'},s)
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
async def body(req):
    try:return await req.json()
    except:return {}
def require(req):
    u=auth(req)
    return u
def channel_obj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def member(c,ch,uid):
    return c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone() is not None
def workspace_role(c,wid,uid):
    r=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone(); return r['role'] if r else None
def append_event(c, channel_id, kind, payload):
    row=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(channel_id,)).fetchone()
    eid=row['n']
    c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(channel_id,eid,kind,json.dumps(payload)))
    return eid
async def event_pump():
    while True:
        await asyncio.sleep(.05)
        for w, sub in list(sessions.items()):
            if w.closed: continue
            c0=db(); grows=c0.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(sub.get('_global',0),)).fetchall(); c0.close()
            for gr in grows:
                try: await w.send_json(json.loads(gr['payload'])); sub['_global']=gr['id']
                except: pass
            for cid, last in list(sub.items()):
                if cid == '_global': continue
                c=db()
                rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
                c.close()
                for r in rows:
                    try:
                        x=json.loads(r['payload']); x.update(type=r['type'],event_id=r['event_id'],channel_id=cid)
                        await w.send_json(x); sub[cid]=r['event_id']
                    except: pass

def msg_obj(c,r):
    author=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    rr={}
    for x in c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],)): rr.setdefault(x['emoji'],[]).append(x['user_id'])
    mentions=[]
    for uname in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_]+)(?![A-Za-z0-9_.@])',r['body'] or ''):
        if uname in ('channel','here'): continue
        x=c.execute('SELECT u.id FROM users u JOIN wm ON wm.user_id=u.id JOIN cm ON cm.user_id=u.id WHERE u.username=? AND cm.channel_id=?',(uname,r['channel_id'])).fetchone()
        if x and x['id']!=r['author_id'] and x['id'] not in mentions: mentions.append(x['id'])
    if '@channel' in (r['body'] or ''):
        for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(r['channel_id'],r['author_id'])):
            if x['user_id'] not in mentions: mentions.append(x['user_id'])
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':jsuser(author),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM message_files WHERE message_id=?)',(r['id'],)).fetchall()],'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in rr.items()],'mentions':mentions}
async def health(req): return resp({'status':'ok','node_id':NODE})
async def register(req):
    b=await body(req); u=b.get('username','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(b.get('password',''))<8:return err(400)
    c=db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or u)); uid=cur.lastrowid
        tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return resp({'user':jsuser(r),'token':tok},201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
async def login(req):
    b=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
    if not r:c.close(); return err(401)
    tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id'])); c.commit(); c.close(); return resp({'user':jsuser(r),'token':tok})
async def me(req):
    u=auth(req); return resp({'user':jsuser(u)}) if u else err(401)

async def workspace_members(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if not workspace_role(c,w['id'],u['id']):return err(403)
    rows=c.execute('SELECT u.id user_id,m.role,u.username,u.display_name FROM wm m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall();c.close()
    return resp({'members':[dict(x) for x in rows]})
async def change_role(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    actor=workspace_role(c,w['id'],u['id']); target_id=int(req.match_info['user_id'])
    target=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],target_id)).fetchone()
    if not target:return err(404)
    if actor not in ('owner','admin') or target_id==u['id'] or target['role']=='owner' or (actor=='admin' and target['role']=='admin'):return err(403)
    b=await body(req); role=b.get('role')
    if role not in ('admin','member','guest'):return err(400)
    c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target_id));c.commit()
    return resp({'member':{'user_id':target_id,'role':role}})

async def transfer(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if w['owner_id']!=u['id']:return err(403)
    b=await body(req); tid=b.get('new_owner_id')
    target=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],tid)).fetchone()
    if not target or target['role']!='admin':return err(400)
    c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
    c.execute('UPDATE wm SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],tid))
    c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(tid,w['id']));c.commit()
    return resp({'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})

async def patch_workspace(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if workspace_role(c,w['id'],u['id']) not in ('owner','admin'):return err(403)
    b=await body(req)
    if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return err(400)
    vals=[];args=[]
    for k in ('name','join_mode'):
        if k in b: vals.append(k+'=?');args.append(b[k])
    if vals:c.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',(*args,w['id']));c.commit()
    r=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();return resp({'workspace':dict(r)})


async def create_invitation(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if workspace_role(c,w['id'],u['id']) not in ('owner','admin'):return err(403)
    b=await body(req); maxu=b.get('max_uses',1)
    if not isinstance(maxu,int) or maxu<1:return err(400)
    target=b.get('invited_user_id')
    if target is None and b.get('invited_username'):
        x=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone()
        if x: target=x['id']
    if target is not None: maxu=1
    code=secrets.token_urlsafe(18); exp=int(__import__('time').time()+b.get('expires_in_seconds',30*86400))
    c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?)',(code,w['id'],target,maxu,0,exp));c.commit()
    r={'code':code,'workspace_id':w['id'],'invited_user_id':target,'max_uses':maxu,'uses':0,'expires_at':datetime.fromtimestamp(exp,timezone.utc).isoformat().replace('+00:00','Z')}
    return resp({'invitation':r},201)
async def list_invitations(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if workspace_role(c,w['id'],u['id']) not in ('owner','admin'):return err(403)
    import time
    rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses AND (expires IS NULL OR expires>?)',(w['id'],int(time.time()))).fetchall()
    return resp({'invitations':[dict(x) for x in rows]})
async def accept_invitation(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM invitations WHERE code=?',(req.match_info['code'],)).fetchone()
    import time
    if not r or r['uses']>=r['max_uses'] or (r['expires'] and r['expires']<=int(time.time())):return err(404)
    if r['invited_user_id'] is not None and r['invited_user_id']!=u['id']:return err(403)
    c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(r['workspace_id'],u['id'],'member'))
    c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(r['code'],));c.commit()
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
    return resp({'workspace':dict(w)})
async def revoke_invitation(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if workspace_role(c,w['id'],u['id']) not in ('owner','admin'):return err(403)
    c.execute('DELETE FROM invitations WHERE code=? AND workspace_id=?',(req.match_info['code'],w['id']));c.commit()
    return resp({'deleted':True})

async def create_ws(req):
    u=auth(req)
    if not u:return err(401)
    b=await body(req); slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return err(400)
    c=db()
    try:
        wid=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',''),u['id'])).lastrowid
        c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner')); cid=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')).lastrowid; c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id'])); c.commit()
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); return resp({'workspace':dict(w),'general_channel':channel_obj(ch)},201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
async def list_ws(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close(); return resp({'workspaces':[dict(x) for x in rows]})
async def workspace(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if not workspace_role(c,w['id'],u['id']):return err(403)
    inc=req.query.get('include_archived') in ('1','true')
    rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_private=0 OR id IN (SELECT channel_id FROM cm WHERE user_id=?))'+('' if inc else ' AND is_archived=0'),(w['id'],u['id'])).fetchall()
    out=[channel_obj(x) for x in rows]
    rs=[]
    for x in rows:
        z=c.execute('SELECT last_read FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone()
        last=z['last_read'] if z else 0
        unread=c.execute('SELECT count(*) n FROM messages WHERE channel_id=? AND deleted=0 AND event_id>? AND author_id!=?',(x['id'],last,u['id'])).fetchone()['n']
        mention=sum(1 for m in c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND event_id>? AND author_id!=?',(x['id'],last,u['id'])).fetchall() if u['id'] in msg_obj(c,m)['mentions'])
        rs.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mention})
    c.close(); return resp({'workspace':dict(w),'channels':out,'read_state':rs})
async def create_channel(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    role=workspace_role(c,w['id'],u['id'])
    if not role or role=='guest':return err(403)
    b=await body(req); name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32 or len(b.get('topic',''))>250:return err(400)
    if b.get('is_private') and role not in ('owner','admin'):return err(403)
    if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone(): return err(409)
    try:
        cid=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic',''))).lastrowid;c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return resp({'channel':channel_obj(r)},201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
async def join(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); role=workspace_role(c,w['id'],u['id'])
    if ch['is_private'] and not member(c,ch['id'],u['id']): return err(403)
    if role is None:
        if w['join_mode'] != 'open' or ch['is_private']: return err(403)
        role='member'
        c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(w['id'],u['id'],'member'))
    if role == 'guest' and not member(c,ch['id'],u['id']): return err(403)
    cur=c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id']))
    if cur.rowcount: append_event(c,ch['id'],'member.joined',{'user_id':u['id']})
    c.commit();return resp({'channel':channel_obj(ch)})
async def messages(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    if not member(c,ch['id'],u['id']):return err(403)
    lim=req.query.get('limit','50')
    try: lim=int(lim)
    except: return err(400)
    if lim<1 or lim>200:return err(400)
    cur=req.query.get('cursor')
    if cur is None:
        rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?',(ch['id'],lim+1)).fetchall()
    elif not re.fullmatch(r'[0-9]+',cur):
        return err(400)
    else:
        anchor=int(cur)
        rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL AND id<? ORDER BY id DESC LIMIT ?',(ch['id'],anchor,lim+1)).fetchall()
    more=len(rows)>lim; rows=rows[:lim]; out=[msg_obj(c,x) for x in rows];c.close()
    return resp({'messages':out,'next_cursor':str(rows[-1]['id']) if more and rows else None})
async def replies(req):
    u=auth(req); 
    if not u:return err(401)
    c=db(); parent=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not parent:return err(404)
    if not member(c,parent['channel_id'],u['id']):return err(403)
    lim=req.query.get('limit','50')
    try:lim=int(lim)
    except:return err(400)
    if lim<1 or lim>200:return err(400)
    cur=req.query.get('cursor')
    if cur is not None and not re.fullmatch(r'[0-9]+',cur):return err(400)
    off=int(cur or 0)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC LIMIT ? OFFSET ?',(parent['id'],lim+1,off)).fetchall()
    more=len(rows)>lim;rows=rows[:lim]
    out=[msg_obj(c,x) for x in rows];c.close()
    return resp({'replies':out,'next_cursor':str(off+lim) if more else None})
async def edit_message(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if r['deleted']:return err(409)
    if r['author_id']!=u['id']:return err(403)
    b=await body(req)
    if not isinstance(b.get('body'),str) or not b['body'].strip():return err(400)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),r['id']))
    r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();o=msg_obj(c,r); append_event(c,r['channel_id'],'message.updated',{'message':o}); c.commit(); c.close()
    return resp({'message':o})
async def delete_message(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if r['deleted']:return resp({'deleted':True})
    role=workspace_role(c,c.execute('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],)).fetchone()['workspace_id'],u['id'])
    if r['author_id']!=u['id'] and role!='owner':return err(403)
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],)); append_event(c,r['channel_id'],'message.deleted',{'message_id':r['id']}); c.commit();return resp({'deleted':True})
async def add_reaction(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if not member(c,r['channel_id'],u['id']):return err(403)
    b=await body(req); emoji=b.get('emoji','')
    if not isinstance(emoji,str) or not emoji or len(emoji)>64:return err(400)
    cur=c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(r['id'],u['id'],emoji))
    if cur.rowcount: append_event(c,r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':emoji,'user_id':u['id']})
    c.commit()
    rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],)).fetchall();d={}
    for x in rows:d.setdefault(x['emoji'],[]).append(x['user_id'])
    return resp({'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in d.items()]})
async def remove_reaction(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    cur=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],req.match_info['emoji']))
    if cur.rowcount: append_event(c,r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':req.match_info['emoji'],'user_id':u['id']})
    c.commit()
    rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],)).fetchall();d={}
    for x in rows:d.setdefault(x['emoji'],[]).append(x['user_id'])
    return resp({'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in d.items()]})
async def post_message(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    b=await body(req); text=b.get('body','')
    if not isinstance(text,str) or not text.strip():return err(400)
    if ch['is_archived'] and not (text.startswith('/unarchive') and not text.startswith('//')): return err(423)
    if not member(c,ch['id'],u['id']):return err(403)
    if text.startswith('/') and not text.startswith('//'):
        z=await slash(req,u,ch,text)
        if isinstance(z,web.StreamResponse): return z
        if isinstance(z,tuple): text=z[1]
    parent=b.get('parent_id')
    file_ids=b.get('file_ids',[])
    if file_ids:
        for fid in file_ids:
            f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=?',(fid,u['id'])).fetchone()
            if not f or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone(): return err(400)
    mentions=[]
    for uname in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_]+)(?![A-Za-z0-9_.@])', text):
        if uname in ('channel','here'): continue
        x=c.execute('SELECT u.id FROM users u JOIN wm ON wm.user_id=u.id JOIN channels cc ON cc.workspace_id=wm.workspace_id WHERE u.username=? AND cc.id=?',(uname,ch['id'])).fetchone()
        if x and x['id']!=u['id'] and x['id'] not in mentions: mentions.append(x['id'])
    for gh in re.findall(r'(?<![A-Za-z0-9_.@])@([a-z0-9_-]+)(?![A-Za-z0-9_-])', text):
        g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],gh)).fetchone()
        if g:
            for gid in json.loads(g['user_ids'] or '[]'):
                if gid!=u['id'] and gid not in mentions and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],gid)).fetchone(): mentions.append(gid)
    if '@channel' in text:
        for x in c.execute('SELECT user_id FROM cm WHERE channel_id=? AND user_id!=?',(ch['id'],u['id'])):
            if x['user_id'] not in mentions: mentions.append(x['user_id'])
    if re.search(r'(?<![A-Za-z0-9_.@])@here(?![A-Za-z0-9_-])', text):
        for x in c.execute('SELECT DISTINCT cm.user_id FROM cm WHERE cm.channel_id=? AND cm.user_id!=? AND EXISTS (SELECT 1 FROM ws_presence p WHERE p.user_id=cm.user_id)',(ch['id'],u['id'])):
            if x['user_id'] not in mentions: mentions.append(x['user_id'])
    c.execute('BEGIN IMMEDIATE')
    mid=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],text,parent,now())).lastrowid
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    for fid in file_ids:c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid))
    obj=msg_obj(c,r); obj['mentions']=mentions
    kind='message.reply' if parent else 'message.created'
    eid=append_event(c,ch['id'],kind,{'message':obj})
    c.execute('UPDATE messages SET event_id=? WHERE id=?',(eid,mid))
    c.commit();c.close()
    return resp({'message':obj},201)
async def ws(req):
    token=req.query.get('token',''); c=db(); u=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
    if not u:c.close();return err(401)
    uid=u['user_id']; c.close()
    w=web.WebSocketResponse();await w.prepare(req); clients.add(w)
    gc=db(); global_head=gc.execute('SELECT COALESCE(MAX(id),0) n FROM global_events').fetchone()['n']; gc.close()
    sessions[w]={'_global':global_head}
    pc=db(); presence_id=pc.execute('INSERT INTO ws_presence(user_id,node_id) VALUES(?,?)',(uid,NODE)).lastrowid; pc.commit(); pc.close()
    try:
        async for m in w:
            if m.type!=WSMsgType.TEXT: continue
            x=json.loads(m.data); cid=int(x.get('channel_id',0)); c=db()
            ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            allowed=ch and (not ch['is_private'] or member(c,cid,uid))
            if x.get('type')=='subscribe':
                if not allowed: await w.send_json({'type':'error','channel_id':cid,'error':'forbidden'}); c.close(); continue
                head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
                sessions[w][cid]=head; await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif x.get('type')=='resume':
                if not allowed: await w.send_json({'type':'error','channel_id':cid,'error':'forbidden'}); c.close(); continue
                since=int(x.get('since_event_id',0)); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
                for r in rows:
                    z=json.loads(r['payload']);z.update(type=r['type'],event_id=r['event_id'],channel_id=cid);await w.send_json(z)
                head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];sessions[w][cid]=head
                await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            c.close()
    finally:
        pc=db(); pc.execute('DELETE FROM ws_presence WHERE id=?',(presence_id,)); pc.commit(); pc.close()
        clients.discard(w);sessions.pop(w,None)
    return w
async def broadcast(x):
    for w in list(clients):
        try: await w.send_json(x)
        except: clients.discard(w)

async def patch_channel(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    role=workspace_role(c,ch['workspace_id'],u['id'])
    if not role:return err(403)
    if not member(c,ch['id'],u['id']) and role!='owner':return err(403)
    b=await body(req); topic=b.get('topic','')
    if not isinstance(topic,str) or len(topic)>250:return err(400)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']))
    r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone(); append_event(c,ch['id'],'channel.updated',{'channel':channel_obj(r)}); c.commit(); c.close()
    return resp({'channel':channel_obj(r)})
async def leave(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    cur=c.execute('DELETE FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],u['id']))
    if cur.rowcount: append_event(c,ch['id'],'member.left',{'user_id':u['id']})
    c.commit();return resp({'channel':channel_obj(ch)})
async def channel_members(req):
    u=auth(req)
    if not u:return err(401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    if not member(c,ch['id'],u['id']) and not workspace_role(c,ch['workspace_id'],u['id']):return err(403)
    rows=c.execute('SELECT u.* FROM users u JOIN cm m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close()
    return resp({'members':[jsuser(x) for x in rows]})
async def archive(req):
    u=auth(req)
    if not u:return err(401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    if workspace_role(c,ch['workspace_id'],u['id'])!='owner':return err(403)
    val=1 if req.path.endswith('/archive') else 0
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone(); append_event(c,ch['id'],'channel.archived' if val else 'channel.unarchived',{'channel':channel_obj(r)}); c.commit()
    return resp({'channel':channel_obj(r)})


async def search(req):
    u=auth(req)
    if not u:return err(401)
    q=req.query.get('q',''); lim=req.query.get('limit','50')
    try: lim=int(lim)
    except:return err(400)
    if lim<1 or lim>200:return err(400)
    cur=req.query.get('cursor')
    if cur is not None and not re.fullmatch(r'[0-9]+',cur):return err(400)
    off=int(cur or 0); c=db()
    wid=req.query.get('workspace_id')
    if wid is not None:
        rows=c.execute("SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE lower(?) AND ch.workspace_id=? AND m.channel_id IN (SELECT channel_id FROM cm WHERE user_id=?) ORDER BY m.id DESC LIMIT ? OFFSET ?",('%'+q+'%',int(wid),u['id'],lim+1,off)).fetchall()
    else:
        rows=c.execute("SELECT m.* FROM messages m WHERE m.deleted=0 AND lower(m.body) LIKE lower(?) AND m.channel_id IN (SELECT channel_id FROM cm WHERE user_id=?) ORDER BY m.id DESC LIMIT ? OFFSET ?",('%'+q+'%',u['id'],lim+1,off)).fetchall()
    more=len(rows)>lim; rows=rows[:lim]; out=[msg_obj(c,x) for x in rows]; c.close()
    return resp({'results':out,'next_cursor':str(off+lim) if more else None})
async def pin_message(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); r=c.execute('SELECT m.* FROM messages m WHERE m.id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if not member(c,r['channel_id'],u['id']):return err(403)
    c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],now())); c.commit()
    x=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone()
    return resp({'pin':{'message_id':x['message_id'],'pinned_by':x['user_id'],'pinned_at':x['pinned_at']}})
async def unpin_message(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); r=c.execute('SELECT m.* FROM messages m WHERE m.id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    if not member(c,r['channel_id'],u['id']):return err(403)
    c.execute('DELETE FROM pins WHERE message_id=?',(r['id'],)); c.commit()
    return resp({'unpinned':True})
async def list_pins(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    if not member(c,ch['id'],u['id']):return err(403)
    rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(ch['id'],)).fetchall()
    return resp({'pins':[{'message':msg_obj(c,x),'pinned_by':x['user_id'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':None})
async def read_get(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    if not member(c,ch['id'],u['id']):return err(403)
    x=c.execute('SELECT last_read FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone(); last=x['last_read'] if x else 0
    head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']
    pending=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND event_id>? AND author_id!=?',(ch['id'],last,u['id'])).fetchall()
    unread=len(pending); mention=sum(1 for m in pending if u['id'] in msg_obj(c,m)['mentions'])
    return resp({'read_state':{'channel_id':ch['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mention}})
async def read_post(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:return err(404)
    b=await body(req); val=b.get('last_read_event_id')
    if not isinstance(val,int) or val<0:return err(400)
    head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']; val=min(val,head)
    old=c.execute('SELECT last_read FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
    val=max(val,old['last_read'] if old else 0); c.execute('INSERT OR REPLACE INTO readstate VALUES(?,?,?)',(u['id'],ch['id'],val)); c.commit()
    return await read_get(req)


async def create_dm(req):
    u=auth(req)
    if not u:return err(401)
    b=await body(req)
    if 'user_id' not in b or not isinstance(b.get('user_id'),int): return err(400)
    other=b.get('user_id')
    c=db(); target=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not target:return err(404)
    a,bid=sorted((u['id'],int(other)))
    name='dm-'+str(a)+'-'+str(bid)
    r=c.execute('SELECT id FROM channels WHERE is_dm=1 AND name=?',(name,)).fetchone()
    if r:return resp({'channel_id':r['id']})
    cid=c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(NULL,?,1,1)',(name,)).lastrowid
    c.execute('INSERT INTO cm VALUES(?,?)',(cid,a)); c.execute('INSERT INTO cm VALUES(?,?)',(cid,bid)); c.commit()
    return resp({'channel_id':cid})
async def get_user(req):
    u=auth(req)
    if not u:return err(401)
    c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    if not r:return err(404)
    return resp({'user':jsuser(r)})
async def patch_me(req):
    u=auth(req)
    if not u:return err(401)
    b=await body(req)
    allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
    if any(k not in allowed for k in b): return err(400)
    if 'timezone' in b:
        from zoneinfo import ZoneInfo
        try: ZoneInfo(b['timezone'])
        except: return err(400)
        if not b['timezone']: return err(400)
    limits={'display_name':(1,80),'status_text':(0,140),'status_emoji':(0,64),'avatar_url':(0,1024),'timezone':(1,64)}
    for k,v in b.items():
        if not isinstance(v,str) or not (limits[k][0] <= len(v) <= limits[k][1]): return err(400)
    c=db()
    if b:
        sets=','.join(k+'=?' for k in b); c.execute('UPDATE users SET '+sets+' WHERE id=?',(*b.values(),u['id'])); c.commit()
    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); frame=json.dumps({'type':'user.updated','user':jsuser(r)}); c.execute('INSERT INTO global_events(type,payload) VALUES(?,?)',('user.updated',frame)); c.commit(); c.close()
    return resp({'user':jsuser(r)})


def file_json(r):
    return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
async def upload_file(req):
    u=auth(req)
    if not u:return err(401)
    try: rd=await req.multipart()
    except:return err(400)
    part=None
    while True:
        x=await rd.next()
        if x is None: break
        if x.name=='file': part=x; break
    if part is None:return err(400)
    data=bytearray()
    while True:
        chunk=await part.read_chunk()
        if not chunk:break
        data.extend(chunk)
        if len(data)>10485760:return err(413)
    c=db(); os.makedirs('/app/data/files',exist_ok=True)
    fid=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),'')).lastrowid
    path=f'/app/data/files/{fid}';open(path,'wb').write(data);c.execute('UPDATE files SET path=? WHERE id=?',(path,fid));c.commit()
    r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return resp({'file':file_json(r)},201)
async def file_meta(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    return err(404) if not r else resp({'file':file_json(r)})
async def file_download(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
    if not r:return err(404)
    return web.FileResponse(r['path'],headers={'Content-Type':r['content_type'],'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
async def slash(req,u,ch,b):
    if b.startswith('//'): return None
    cmd=b.split(' ',1)[0][1:]; arg=b[len(cmd)+2:].strip() if len(b)>len(cmd)+1 else ''
    c=db()
    if cmd=='me':
        if not arg:return err(400)
        return None
    if cmd=='shrug':
        return None, (arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    if cmd=='topic':
        if len(arg)>250:return err(400)
        c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();append_event(c,ch['id'],'channel.updated',{'channel':channel_obj(r)});c.commit();return resp({'message':None,'channel':channel_obj(r)},201)
    if cmd=='invite':
        m=re.fullmatch(r'@([A-Za-z0-9_]+)',arg)
        if not m:return err(400)
        x=c.execute('SELECT * FROM users WHERE username=?',(m.group(1),)).fetchone()
        if not x:return err(404)
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
        c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(w['id'],x['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],x['id']));append_event(c,ch['id'],'member.joined',{'user_id':x['id']});c.commit();return resp({'message':None,'invited_user':jsuser(x)},201)
    if cmd in ('archive','unarchive'):
        if workspace_role(c,ch['workspace_id'],u['id'])!='owner':return err(403)
        val=cmd=='archive';c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();append_event(c,ch['id'],'channel.archived' if val else 'channel.unarchived',{'channel':channel_obj(r)});c.commit();return resp({'message':None,'channel':channel_obj(r)},201)
    return err(400)


def group_json(r):
    return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'] or '[]')}
def group_load(c,wid,handle):
    return c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(wid,handle)).fetchone()
async def groups(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    if not workspace_role(c,w['id'],u['id']):return err(403)
    rows=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall()
    if req.method=='GET':
        c.close();return resp({'groups':[group_json(x) for x in rows]})
    b=await body(req); handle=b.get('handle','');name=b.get('name')
    if not isinstance(handle,str) or not re.fullmatch(r'[a-z0-9_-]{1,32}',handle) or not isinstance(name,str) or not name:return err(400)
    ids=b.get('user_ids',[])
    if not isinstance(ids,list):return err(400)
    for uid in ids:
        if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():return err(400)
    if group_load(c,w['id'],handle):return err(409)
    r=c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],handle,name,u['id'],json.dumps(ids))).lastrowid;c.commit()
    out=c.execute('SELECT * FROM groups_ WHERE id=?',(r,)).fetchone();c.close();return resp({'group':group_json(out)},201)
async def group_edit(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    g=group_load(c,w['id'],req.match_info['handle'])
    if not g:return err(404)
    if g['creator_id']!=u['id'] and workspace_role(c,w['id'],u['id'])!='owner':return err(403)
    b=await body(req); sets=[];args=[]
    if 'name' in b:
        if not isinstance(b['name'],str) or not b['name']:return err(400)
        sets += ['name=?'];args += [b['name']]
    if 'user_ids' in b:
        if not isinstance(b['user_ids'],list):return err(400)
        for uid in b['user_ids']:
            if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():return err(400)
        sets += ['user_ids=?'];args += [json.dumps(b['user_ids'])]
    if sets:c.execute('UPDATE groups_ SET '+','.join(sets)+' WHERE id=?',(*args,g['id']));c.commit()
    return resp({'group':group_json(c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone())})
async def group_delete(req):
    u=auth(req)
    if not u:return err(401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return err(404)
    g=group_load(c,w['id'],req.match_info['handle'])
    if not g:return resp({'deleted':True}) if workspace_role(c,w['id'],u['id']) else err(403)
    if g['creator_id']!=u['id'] and workspace_role(c,w['id'],u['id'])!='owner':return err(403)
    c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();return resp({'deleted':True})

async def get_channel(req):
    u=auth(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:return err(404)
    return resp({'channel':channel_obj(r)})
async def index(req):
    return web.Response(text=open("/app/index.html").read(), content_type="text/html")
async def main():
    init(); app=web.Application(client_max_size=11*1024*1024)
    app.router.add_get('/',index);app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
    app.router.add_post('/api/workspaces',create_ws);app.router.add_post('/api/dms',create_dm);app.router.add_get('/api/users/{id}',get_user);app.router.add_patch('/api/users/me',patch_me);app.router.add_post('/api/workspaces/{slug}/invitations',create_invitation);app.router.add_get('/api/workspaces/{slug}/invitations',list_invitations);app.router.add_delete('/api/workspaces/{slug}/invitations/{code}',revoke_invitation);app.router.add_post('/api/invitations/{code}/accept',accept_invitation);app.router.add_get('/api/workspaces',list_ws);app.router.add_get('/api/workspaces/{slug}/groups',groups);app.router.add_post('/api/workspaces/{slug}/groups',groups);app.router.add_patch('/api/workspaces/{slug}/groups/{handle}',group_edit);app.router.add_delete('/api/workspaces/{slug}/groups/{handle}',group_delete);app.router.add_get('/api/workspaces/{slug}',workspace);app.router.add_patch('/api/workspaces/{slug}',patch_workspace);app.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer);app.router.add_get('/api/workspaces/{slug}/members',workspace_members);app.router.add_patch('/api/workspaces/{slug}/members/{user_id}',change_role);app.router.add_post('/api/workspaces/{slug}/channels',create_channel)
    app.router.add_post('/api/channels/{id}/join',join);app.router.add_post('/api/channels/{id}/leave',leave);app.router.add_get('/api/channels/{id}/members',channel_members);app.router.add_patch('/api/channels/{id}',patch_channel);app.router.add_post('/api/channels/{id}/archive',archive);app.router.add_post('/api/channels/{id}/unarchive',archive);app.router.add_get('/api/channels/{id}',get_channel);app.router.add_get('/api/channels/{id}/messages',messages);app.router.add_post('/api/channels/{id}/messages',post_message);app.router.add_post('/api/files',upload_file);app.router.add_get('/api/files/{id}',file_meta);app.router.add_get('/api/files/{id}/download',file_download);app.router.add_get('/api/search',search);app.router.add_post('/api/messages/{id}/pin',pin_message);app.router.add_delete('/api/messages/{id}/pin',unpin_message);app.router.add_get('/api/channels/{id}/pins',list_pins);app.router.add_get('/api/channels/{id}/read',read_get);app.router.add_post('/api/channels/{id}/read',read_post);app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_patch('/api/messages/{id}',edit_message);app.router.add_delete('/api/messages/{id}',delete_message);app.router.add_post('/api/messages/{id}/reactions',add_reaction);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',remove_reaction);app.router.add_get('/api/ws',ws)
    asyncio.create_task(event_pump())
    await web._run_app(app,host='127.0.0.1',port=PORT)
irc_clients={}
irc_joined={}
async def irc_send(w,line):
    try:w.write((line+'\r\n').encode());await w.drain()
    except:pass
def irc_channel(name):
    if not name.startswith('#') or '/' not in name:return None
    slug,chname=name[1:].split('/',1); c=db()
    r=c.execute('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,chname)).fetchone();c.close();return r
async def irc_client(reader,writer):
    token=None; nick=None; username=None; registered=False; uid=None; joined=[]
    irc_joined[writer]=joined
    async def close():
        if nick and irc_clients.get(nick) is writer: irc_clients.pop(nick,None)
        irc_joined.pop(writer,None)
        writer.close()
        try: await writer.wait_closed()
        except: pass
    async def register():
        nonlocal registered,uid
        if registered or not token or not nick or not username:return
        c=db();r=c.execute('SELECT u.id,u.username FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token,)).fetchone();c.close()
        if not r:
            await irc_send(writer,':huddle 464 * :Password incorrect');await close();return
        uid=r['id'];registered=True;irc_clients[nick]=writer
        for code,text in [('001',f':Welcome to huddle, {nick}'),('002',':Your host is huddle'),('003',':This server was created today'),('004',':huddle huddle 1.0 oi'),('005',':CHANTYPES=#')]:
            await irc_send(writer,f':huddle {code} {nick} {text}')
        await irc_send(writer,f':huddle 422 {nick} :MOTD File is missing')
    try:
        while not reader.at_eof():
            raw=await reader.readline()
            if not raw:break
            line=raw.decode(errors='replace').strip('\r\n')
            if not line:continue
            parts=line.split(' ',1);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else ''
            if cmd=='PASS':
                token=arg.lstrip(':').strip()
                if token and not auth_token_exists(token):
                    await irc_send(writer,':huddle 464 * :Password incorrect');await close();return
            elif cmd=='NICK':
                n=arg.lstrip(':').strip()
                if n in irc_clients and irc_clients[n] is not writer:
                    await irc_send(writer,f':huddle 433 * {n} :Nickname is already in use');continue
                nick=n
                await register()
            elif cmd=='USER':
                username=(arg.split(' ',1)[0] if arg else 'irc')
                await register()
            elif cmd=='PING':
                x=arg.lstrip(':').strip();await irc_send(writer,f':huddle PONG huddle :{x}')
            elif cmd=='QUIT':
                await close();return
            elif not registered:
                continue
            elif cmd=='JOIN':
                name=arg.split()[0] if arg else ''
                ch=irc_channel(name)
                if not ch:
                    await irc_send(writer,f':huddle 403 {nick} {name} :No such channel');continue
                c=db()
                if ch['is_private'] and not member(c,ch['id'],uid):
                    c.close();await irc_send(writer,f':huddle 403 {nick} {name} :No such channel');continue
                c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],uid));c.commit()
                names=[(nick if x['id']==uid else x['username']) for x in c.execute('SELECT u.id,u.username FROM users u JOIN cm m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],))]
                c.close();joined.append(ch['id'])
                await irc_send(writer,f':{nick}!{username}@huddle JOIN {name}')
                if ch['topic']:await irc_send(writer,f':huddle 332 {nick} {name} :{ch["topic"]}')
                else:await irc_send(writer,f':huddle 331 {nick} {name} :No topic is set')
                await irc_send(writer,f':huddle 353 {nick} = {name} :{" ".join(names)}')
                await irc_send(writer,f':huddle 366 {nick} {name} :End of /NAMES list')
            elif cmd=='PRIVMSG':
                if ' :' not in arg:
                    continue
                target, text = arg.split(' :',1)
                ch=irc_channel(target)
                if not ch or ch['id'] not in joined:
                    await irc_send(writer,f':huddle 404 {nick} {target} :Cannot send to channel')
                    continue
                c=db()
                try:
                    c.execute('BEGIN IMMEDIATE')
                    mid=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],uid,text,now())).lastrowid
                    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                    obj=msg_obj(c,r)
                    eid=append_event(c,ch['id'],'message.created',{'message':obj})
                    c.execute('UPDATE messages SET event_id=? WHERE id=?',(eid,mid))
                    c.commit()
                finally:
                    c.close()
            elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):
                continue
            else:
                await irc_send(writer,f':huddle 421 {nick} {cmd} :Unknown command')
    finally:
        if nick and irc_clients.get(nick) is writer:irc_clients.pop(nick,None)
        try:writer.close();await writer.wait_closed()
        except:pass
def auth_token_exists(token):
    c=db();r=c.execute('SELECT 1 FROM tokens WHERE token=?',(token,)).fetchone();c.close();return r is not None
async def irc_event_pump():
    seen={}
    while True:
        await asyncio.sleep(.05)
        c=db()
        rows=c.execute("SELECT e.*,ch.name cname,w.slug FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.type='message.created' ORDER BY e.id").fetchall()
        c.close()
        for r in rows:
            if r['id']<=seen.get(r['channel_id'],0): continue
            seen[r['channel_id']]=r['id']
            payload=json.loads(r['payload']); m=payload.get('message') or {}
            body=m.get('body','')
            author=m.get('author') or {}
            prefix=author.get('username','irc')
            target='#'+r['slug']+'/'+r['cname']
            line=f':{prefix}!{prefix}@huddle PRIVMSG {target} :{body}'
            for w, chans in list(irc_joined.items()):
                if r['channel_id'] in chans:
                    await irc_send(w,line)

async def irc_main(port):
    asyncio.create_task(irc_event_pump())
    server=await asyncio.start_server(irc_client,'0.0.0.0',port)
    async with server:await server.serve_forever()

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='irc':
        init(); asyncio.run(irc_main(int(sys.argv[2]) if len(sys.argv)>2 else 6667))
    else: asyncio.run(main())

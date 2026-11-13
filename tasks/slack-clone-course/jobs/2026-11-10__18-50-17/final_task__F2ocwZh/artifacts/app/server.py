import asyncio, json, os, re, sys, sqlite3, secrets, hashlib, mimetypes, datetime, email
from zoneinfo import ZoneInfo
from aiohttp import web, WSMsgType
DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def conn():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=conn()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,files TEXT DEFAULT '[]',mentions TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,frame TEXT);\nCREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));''')
 c.execute('CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY)');c.execute('CREATE TABLE IF NOT EXISTS deleted_groups(workspace_id INTEGER,handle TEXT,creator_id INTEGER,PRIMARY KEY(workspace_id,handle));');c.commit();c.close()
def user_obj(r):
 return dict(r) if r else None
def auth(req,c):
 h=req.headers.get('Authorization','')
 if not h.startswith('Bearer '): return None
 r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone()
 return r
def out(obj,status=200): return web.json_response(obj,status=status)
async def body(req):
 try:return await req.json()
 except:return {}
def msg_obj(c,r):
 if not r:return None
 d=dict(r); d['author']=user_obj(c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone())
 d['parent_id']=r['parent_id']; d['reply_count']=c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
 d['files']=[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT value FROM json_each(?))',(r['files'],))]
 d['reactions']=[]
 for x in c.execute('SELECT emoji,count(*) n,min(rowid) first FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY first',(r['id'],)):
  ids=[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]
  d['reactions'].append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 d['mentions']=json.loads(r['mentions'] or '[]'); d.pop('deleted',None); return d
def emit(c,cid,kind,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
 frame={'type':kind,'event_id':n,'channel_id':cid};frame.update(payload)
 c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(frame)));return frame
async def health(req): return out({'status':'ok','node_id':int(req.app['node'])})
async def register(req):
 b=await body(req); u=b.get('username','');p=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(p)<8:return out({'error':'invalid'},400)
 c=conn()
 try:
  c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,p,b.get('display_name') or u)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid));c.commit()
 except sqlite3.IntegrityError:c.close();return out({'error':'duplicate'},409)
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return out({'user':user_obj(r),'token':tok},201)
async def login(req):
 b=await body(req);c=conn();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
 if not r:c.close();return out({'error':'unauthorized'},401)
 t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return out({'user':user_obj(r),'token':t})
async def me(req):
 c=conn();r=auth(req,c)
 if not r:c.close();return out({'error':'unauthorized'},401)
 c.close();return out({'user':user_obj(r)})
async def get_user(req):
 c=conn();u=auth(req,c)
 if not u:c.close();return out({},401)
 r=c.execute("SELECT * FROM users WHERE id=?",(req.match_info["id"],)).fetchone();c.close()
 return out({"user":user_obj(r)} if r else {},200 if r else 404)

async def dms(req):
 c=conn();u=auth(req,c)
 if not u:c.close();return out({},401)
 b=await body(req);other=b.get("user_id")
 if other is None:c.close();return out({},400)
 x=c.execute("SELECT id FROM users WHERE id=?",(other,)).fetchone()
 if not x:c.close();return out({},404)
 a,z=sorted((u["id"],int(other))); name="dm-%s-%s"%(a,z)
 ch=c.execute("SELECT id FROM channels WHERE name=? AND is_dm=1",(name,)).fetchone()
 if not ch:
  c.execute("INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(?,?,1,1)",(0,name));cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
  c.execute("INSERT INTO cmembers VALUES(?,?)",(cid,a));c.execute("INSERT INTO cmembers VALUES(?,?)",(cid,z));c.commit()
 else: cid=ch["id"]
 c.close();return out({"channel_id":cid})

async def profile(req):
 c=conn();r=auth(req,c)
 if not r:c.close();return out({},401)
 b=await body(req)
 limits={'display_name':80,'timezone':64,'avatar_url':1024,'status_text':140,'status_emoji':64}
 for k,v in b.items():
  if k not in limits or not isinstance(v,str) or len(v)>limits[k] or (k=='display_name' and not v.strip()):
   c.close();return out({},400)
 if 'timezone' in b:
  try: ZoneInfo(b['timezone'])
  except Exception: c.close();return out({},400)
 sets=[k+'=?' for k in limits if k in b]; vals=[b[k] for k in limits if k in b]
 if sets:
  c.execute('UPDATE users SET '+','.join(sets)+' WHERE id=?',vals+[r['id']])
  x=c.execute('SELECT * FROM users WHERE id=?',(r['id'],)).fetchone()
  c.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps({'type':'user.updated','user':user_obj(x)}),))
  c.commit()
 else: x=c.execute('SELECT * FROM users WHERE id=?',(r['id'],)).fetchone()
 c.close();return out({'user':user_obj(x)})
def ws_role(c,slug,uid):
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 return (w,c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone()) if w else (None,None)

async def groups(req):
 c=conn();u=auth(req,c);slug=req.match_info['slug']
 if not u:c.close();return out({},401)
 w,role=ws_role(c,slug,u['id'])
 if not w:c.close();return out({},404)
 if not role:c.close();return out({},403)
 handle=req.match_info.get('handle')
 if req.method=='GET':
  rows=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall()
  ans=[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'] or '[]')} for x in rows]
  c.close();return out({'groups':ans})
 if req.method=='POST':
  b=await body(req);h=b.get('handle','');name=b.get('name','');ids=b.get('user_ids',[])
  if not re.fullmatch(r'[a-z0-9_-]{2,32}',h) or not name or not isinstance(ids,list):c.close();return out({},400)
  if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():c.close();return out({},409)
  valid=[x[0] for x in c.execute('SELECT id FROM users WHERE id IN (%s)'%(','.join('?'*len(ids)) or 'NULL'),ids)] if ids else []
  if len(valid)!=len(ids) or any(not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],x)).fetchone() for x in ids):c.close();return out({},400)
  c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,name,u['id'],json.dumps(ids)));c.commit();x=c.execute('SELECT * FROM groups_ WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone();c.close()
  return out({'group':{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':ids}},201)
 x=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
 if not x:
  if req.method=='DELETE':
   tomb=c.execute('SELECT creator_id FROM deleted_groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
   if tomb and (int(tomb['creator_id'])==int(u['id']) or role['role']=='owner'):
    c.close();return out({'deleted':True})
  c.close();return out({},404)
 allowed=x['creator_id']==u['id'] or role['role']=='owner'
 if req.method=='DELETE':
  if not allowed:c.close();return out({},403)
  c.execute('INSERT OR REPLACE INTO deleted_groups VALUES(?,?,?)',(w['id'],handle,x['creator_id']));c.execute('DELETE FROM groups_ WHERE id=?',(x['id'],));c.commit();c.close();return out({'deleted':True})
 if not allowed:c.close();return out({},403)
 b=await body(req);name=b.get('name',x['name']);ids=b.get('user_ids',json.loads(x['user_ids'] or '[]'))
 if not name or not isinstance(ids,list):c.close();return out({},400)
 c.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(name,json.dumps(ids),x['id']));c.commit();c.close();return out({'group':{'id':x['id'],'handle':x['handle'],'name':name,'member_user_ids':ids}})
async def workspace(req):
 c=conn();u=auth(req,c)
 if not u:c.close();return out({},401)
 b=await body(req);slug=req.match_info.get('slug')
 if req.method=='POST' and not slug:
  if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return out({},400)
  try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit()
  except sqlite3.IntegrityError:return out({},409)
  w=dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone());ch=dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());c.close();return out({'workspace':w,'general_channel':ch},201)
 if req.method=='GET' and not slug:
  rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return out({'workspaces':[dict(x) for x in rows]})
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:c.close();return out({},404)
 if req.method=='PATCH':
  b=await body(req);c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(b.get('name'),b.get('join_mode'),w['id']));c.commit();x=dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone());c.close();return out({'workspace':x})
 chans=[dict(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],req.query.get('include_archived') in ('1','true')))]
 c.close();return out({'workspace':dict(w),'channels':chans,'read_state':[]})
async def channels(req):
 c=conn();u=auth(req,c)
 if not u:c.close();return out({},401)
 slug=req.match_info['slug'];w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:c.close();return out({},404)
 if req.method=='POST':
  b=await body(req); name=b.get('name','')
  if not re.fullmatch(r'[a-z0-9-]{1,32}',name):c.close();return out({},400)
  if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():c.close();return out({},409)
  c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'));c.commit();x=dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());c.close();return out({'channel':x},201)
async def channel_members(req):
 c=conn();u=auth(req,c);cid=int(req.match_info['id'])
 if not u:c.close();return out({},401)
 ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:c.close();return out({},404)
 rows=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall()
 c.close();return out({'members':[user_obj(x) for x in rows]})
async def channel_action(req):
 c=conn();u=auth(req,c);cid=int(req.match_info['id'])
 if not u:c.close();return out({},401)
 ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:c.close();return out({},404)
 if req.path.endswith('/join'):
  c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.commit();c.close();return out({'channel':dict(ch)})
 if req.path.endswith('/leave'):
  c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']));c.commit();c.close();return out({'channel':dict(ch)})
 b=await body(req)
 if req.method=='PATCH':
  c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),cid));c.commit();x=dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());c.close();return out({'channel':x})
 c.close();return out({},400)
async def messages(req):
 c=conn();u=auth(req,c);cid=int(req.match_info['id'])
 if not u:c.close();return out({},401)
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():c.close();return out({},403)
 if req.method=='GET':
  rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(cid,)).fetchall();c.close();return out({'messages':[msg_obj(conn(),x) for x in rows],'next_cursor':None})
 b=await body(req);text=b.get('body','')
 if not text.strip():c.close();return out({},400)
 ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 fids=b.get('file_ids',[])
 if fids:
  for fid in fids:
   fr=c.execute('SELECT uploader_id FROM files WHERE id=?',(fid,)).fetchone()
   if not fr or fr['uploader_id']!=u['id'] or c.execute("SELECT 1 FROM messages WHERE EXISTS (SELECT 1 FROM json_each(messages.files) WHERE value=?)",(fid,)).fetchone():
    c.close();return out({},400)
 if text.startswith('/') and not text.startswith('//'):
  parts=text.split(' ',1);cmd=parts[0];arg=parts[1] if len(parts)>1 else ''
  role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
  isowner=bool(role and role['role']=='owner')
  if cmd=='/shrug':text=(arg+' ¯\\_(ツ)_/¯').strip()
  elif cmd=='/me':
   if not arg.strip():c.close();return out({},400)
  elif cmd=='/topic':
   if not arg.strip():c.close();return out({},400)
   c.execute('UPDATE channels SET topic=? WHERE id=?',(arg.strip(),cid));c.commit();x=dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());x['is_archived']=bool(x['is_archived']);c.close();return out({'message':None,'channel':x},201)
  elif cmd=='/invite':
   if not arg.startswith('@'):c.close();return out({},400)
   target=c.execute('SELECT * FROM users WHERE username=?',(arg[1:].strip(),)).fetchone()
   if not target:c.close();return out({},404)
   c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],target['id'],'member'));c.commit();c.close();return out({'message':None,'invited_user':user_obj(target)},201)
  elif cmd in ('/archive','/unarchive'):
   if not isowner:c.close();return out({},403)
   val=1 if cmd=='/archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid));c.commit();x=dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());x['is_archived']=bool(x['is_archived']);c.close();return out({'message':None,'channel':x},201)
  else:c.close();return out({},400)
 if ch['is_archived']:c.close();return out({},423)
 mentions=[]
 for name in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',text):
  if name=='here':
   rows=c.execute('SELECT m.user_id FROM cmembers m JOIN online_users o ON o.user_id=m.user_id WHERE m.channel_id=? AND m.user_id!=?',(cid,u['id'])).fetchall()
   mentions += [x[0] for x in rows]
  elif name=='channel':
   rows=c.execute('SELECT user_id FROM cmembers WHERE channel_id=? AND user_id!=?',(cid,u['id'])).fetchall()
   mentions += [x[0] for x in rows]
  else:
   g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],name)).fetchone()
   if g:
    for uid in json.loads(g['user_ids'] or '[]'):
     if uid!=u['id'] and c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(): mentions.append(uid)
   else:
    x=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
    if x and x['id']!=u['id'] and c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,x['id'])).fetchone(): mentions.append(x['id'])
 mentions=list(dict.fromkeys(mentions))
 c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,files,mentions) VALUES(?,?,?,?,?,?,?)',(cid,u['id'],text,b.get('parent_id'),now(),json.dumps(b.get('file_ids',[])),json.dumps(list(dict.fromkeys(mentions)))))
 mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();frame=emit(c,cid,'message.reply' if b.get('parent_id') else 'message.created',{'message':msg_obj(c,r)});c.commit();c.close();return out({'message':frame['message']},201)
async def message_edit(req):
 c=conn();u=auth(req,c);mid=int(req.match_info['id']);r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not u:c.close();return out({},401)
 if not r:c.close();return out({},404)
 ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
 owner=c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=? AND role='owner'",(ch['workspace_id'],u['id'])).fetchone()
 if req.method=='DELETE':
  if r['deleted']:
   c.close();return out({'deleted':True})
  if r['author_id']!=u['id'] and not owner:c.close();return out({},403)
  c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,))
  f=emit(c,r['channel_id'],'message.deleted',{'message_id':mid})
  c.commit();c.close();return out({'deleted':True})
 if r['deleted']:c.close();return out({},409)
 if r['author_id']!=u['id']:c.close();return out({},403)
 b=await body(req)
 if not isinstance(b.get('body'),str) or not b['body'].strip():c.close();return out({},400)
 c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),mid))
 x=msg_obj(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())
 emit(c,r['channel_id'],'message.updated',{'message':x})
 c.commit();c.close();return out({'message':x})
async def replies(req):
 c=conn();u=auth(req,c);r=c.execute('SELECT * FROM messages WHERE id=?',(int(req.match_info['id']),)).fetchone()
 if not u:c.close();return out({},401)
 rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(r['id'],)).fetchall();ans=[msg_obj(c,x) for x in rows];c.close();return out({'replies':ans,'next_cursor':None})
async def reactions(req):
 c=conn();u=auth(req,c);mid=int(req.match_info['id']);r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
 if not u:c.close();return out({},401)
 if not r:c.close();return out({},404)
 ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():
  c.close();return out({},403)
 b=await body(req);emoji=b.get('emoji') if req.method=='POST' else req.match_info.get('emoji')
 if not isinstance(emoji,str) or not emoji or len(emoji)>64:
  c.close();return out({},400)
 changed=False
 if req.method=='POST':
  c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji));changed=c.execute('SELECT changes()').fetchone()[0]==1
  if changed:
   f=emit(c,r['channel_id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id']})
 else:
  c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));changed=c.execute('SELECT changes()').fetchone()[0]==1
  if changed:
   f=emit(c,r['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id']})
 c.commit();arr=[]
 for x in c.execute('SELECT emoji,count(*) n, min(rowid) first FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY first',(mid,)):
  arr.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=? ORDER BY rowid',(mid,x['emoji']))]})
 c.close();return out({'reactions':arr})
async def ws(req):
 c=conn();u=auth(type('R',(),{'headers':{'Authorization':'Bearer '+req.query.get('token','')}})(),c)
 if not u:c.close();return web.Response(status=401)
 c.execute('INSERT OR REPLACE INTO online_users VALUES(?)',(u['id'],));global_last=c.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0];c.commit();c.close();sock=web.WebSocketResponse();await sock.prepare(req);subs=set();last={}
 async def pump():
  nonlocal global_last
  while not sock.closed:
   c=conn()
   grows=c.execute('SELECT id,frame FROM global_events WHERE id>? ORDER BY id',(global_last,)).fetchall()
   for x in grows: await sock.send_str(x['frame']);global_last=x['id']
   for cid in list(subs):
    rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last.get(cid,0))).fetchall()
    for x in rows: await sock.send_str(x['frame']);last[cid]=x['event_id']
   c.close();await asyncio.sleep(.05)
 task=asyncio.create_task(pump())
 try:
  async for m in sock:
   if m.type==WSMsgType.TEXT:
    x=json.loads(m.data);cid=int(x.get('channel_id',0))
    if x['type'] in ('subscribe','resume'):
     subs.add(cid);c=conn();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];c.close()
     if x['type']=='subscribe':
      last[cid]=head;await sock.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
     else:
      since=int(x.get('since_event_id',0));c=conn();rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall();c.close()
      for row in rows: await sock.send_str(row['frame'])
      last[cid]=head;await sock.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
 finally:
  task.cancel();c=conn();c.execute('DELETE FROM online_users WHERE user_id=?',(u['id'],));c.commit();c.close()
 return sock
async def files(req):
 c=conn();u=auth(req,c)
 if not u:c.close();return out({},401)
 if req.method=='GET':
  r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(int(req.match_info['id']),)).fetchone();c.close();return out({'file':dict(r)} if r else {},200 if r else 404)
 rd=await req.multipart();part=await rd.next()
 if not part or part.name!='file':c.close();return out({},400)
 data=await part.read(decode=False)
 if len(data)>10*1024*1024:c.close();return out({},413)
 fn=part.filename or 'file';ct=part.headers.get('Content-Type','application/octet-stream');c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],fn,ct,len(data),now(),data));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();r=dict(c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone());c.close();return out({'file':r},201)
async def download(req):
 c=conn();u=auth(req,c);r=c.execute('SELECT * FROM files WHERE id=?',(int(req.match_info['id']),)).fetchone();c.close()
 if not u:return out({},401)
 if not r:return out({},404)
 return web.Response(body=r['data'],content_type=r['content_type'],headers={'Content-Disposition':'attachment; filename="'+r['filename']+'"'})
def main():
 init();node=sys.argv[2] if len(sys.argv)>2 else '0';port=int(sys.argv[3]) if len(sys.argv)>3 else 8000
 app=web.Application(client_max_size=11*1024*1024);app['node']=node
 app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me);app.router.add_patch('/api/users/me',profile);app.router.add_get('/api/users/{id}',get_user);app.router.add_post('/api/dms',dms)
 app.router.add_post('/api/workspaces',workspace);app.router.add_get('/api/workspaces',workspace);app.router.add_post('/api/workspaces/{slug}/channels',channels);app.router.add_route('*','/api/workspaces/{slug}/groups',groups);app.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',groups);app.router.add_route('*','/api/workspaces/{slug}',workspace)
 app.router.add_post('/api/channels/{id}/join',channel_action);app.router.add_get('/api/channels/{id}/members',channel_members);app.router.add_post('/api/channels/{id}/leave',channel_action);app.router.add_patch('/api/channels/{id}',channel_action);app.router.add_route('*','/api/channels/{id}/messages',messages)
 app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_patch('/api/messages/{id}',message_edit);app.router.add_delete('/api/messages/{id}',message_edit);app.router.add_post('/api/messages/{id}/reactions',reactions);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',reactions)
 app.router.add_post('/api/files',files);app.router.add_get('/api/files/{id}',files);app.router.add_get('/api/files/{id}/download',download);app.router.add_get('/api/ws',ws)
 app.router.add_get('/',lambda r:web.FileResponse('/app/static/index.html'))
 web.run_app(app,host='127.0.0.1',port=port)
if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=="node":main()

IRC_CLIENTS=[]
async def irc_send(w,line):
 try:w.write((line+'\r\n').encode());await w.drain()
 except:pass

async def irc_client(reader,writer):
 state={'token':None,'uid':None,'nick':None,'registered':False,'channels':set()}
 IRC_CLIENTS.append((state,writer))
 async def reg():
  state['registered']=True
  n=state['nick']; host='huddle'
  for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 1'),('005','CHANTYPES=# CASEMAPPING=rfc1459'),('422','MOTD File is missing')]:
   await irc_send(writer,':huddle %s %s %s :%s'%(code,n,n,text))
 async def channel_info(name):
  if not name.startswith('#') or '/' not in name[1:]:return None
  slug,cname=name[1:].split('/',1);c=conn();r=c.execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cname)).fetchone();c.close();return r
 try:
  while True:
   line=await reader.readline()
   if not line:break
   line=line.decode(errors='replace').strip('\r\n')
   if not line:break
   parts=line.split(' ',2);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else '';trail=parts[2] if len(parts)>2 else ''
   if cmd=='PASS':
    state['token']=arg
    c=conn();r=c.execute('SELECT user_id FROM tokens WHERE token=?',(arg,)).fetchone();state['uid']=r['user_id'] if r else None;c.close()
    if not state['uid']:await irc_send(writer,':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    nick=arg.lstrip(':')
    collision=any(x[0]['nick']==nick and x[0] is not state for x in IRC_CLIENTS)
    if collision: await irc_send(writer,':huddle 433 * %s :Nickname is already in use')
    else:
     state['nick']=nick
     if state['uid'] and state.get('userline'):await reg()
   elif cmd=='USER':
    state['userline']=True
    if state['uid'] and state.get('nick'):await reg()
   elif not state['registered']:
    if cmd not in ('PING','PASS','NICK','USER'):await irc_send(writer,':huddle 451 * :You have not registered')
   elif cmd=='PING':await irc_send(writer,':huddle PONG huddle :'+(arg or trail.lstrip(':')))
   elif cmd=='PONG':pass
   elif cmd=='JOIN':
    name=arg.lstrip(':');ch=await channel_info(name)
    if not ch:await irc_send(writer,':huddle 403 %s %s :No such channel'%(state['nick'],name));continue
    state['channels'].add(name);await irc_send(writer,':%s!%s@huddle JOIN :%s'%(state['nick'],state['uid'],name))
    c=conn();rows=c.execute('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close()
    names=' '.join([x['username'] for x in rows]+[state['nick']])
    await irc_send(writer,':huddle 353 %s = %s :%s'%(state['nick'],name,names));await irc_send(writer,':huddle 366 %s %s :End of /NAMES list.'%(state['nick'],name))
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'):
    await irc_send(writer,':huddle 366 %s %s :End of /NAMES list.'%(state['nick'],arg or '*') if cmd=='NAMES' else ':huddle 461 %s %s :OK'%(state['nick'],cmd))
   elif cmd=='PRIVMSG':
    name=arg; text=trail[1:] if trail.startswith(':') else trail
    ch=await channel_info(name)
    if not ch:await irc_send(writer,':huddle 403 %s %s :No such channel'%(state['nick'],name));continue
    c=conn();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,files,mentions) VALUES(?,?,?,?,?,?,?)',(ch['id'],state['uid'],text,None,now(),'[]','[]'));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();f=emit(c,ch['id'],'message.created',{'message':msg_obj(c,r)});c.commit();c.close()
   elif cmd=='QUIT':break
   else:await irc_send(writer,':huddle 421 %s %s :Unknown command'%(state['nick'] or '*',cmd))
 finally:
  IRC_CLIENTS[:] = [x for x in IRC_CLIENTS if x[1] is not writer];writer.close()
  try:await writer.wait_closed()
  except:pass

async def irc_pump():
 seen={}
 while True:
  try:
   c=conn()
   for state,w in list(IRC_CLIENTS):
    for name in list(state['channels']):
     ch=awaitable=None
     slug,cname=name[1:].split('/',1);r=c.execute('SELECT c.id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cname)).fetchone()
     if not r:continue
     rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(r['id'],seen.get((id(w),r['id']),0))).fetchall()
     for row in rows:
      f=json.loads(row['frame']);seen[(id(w),r['id'])]=row['event_id']
      if f.get('type')=='message.created':
       m=f.get('message') or {}; author=c.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone();nick=author['username'] if author else 'user'
       await irc_send(w,':%s!%s@huddle PRIVMSG %s :%s'%(nick,m.get('author_id'),name,m.get('body','')))
   c.close()
  except Exception:pass
  await asyncio.sleep(.05)

def irc_main():
 loop=asyncio.new_event_loop();asyncio.set_event_loop(loop)
 server=loop.run_until_complete(asyncio.start_server(irc_client,'0.0.0.0',6667))
 loop.create_task(irc_pump())
 try:loop.run_forever()
 finally:server.close();loop.run_until_complete(server.wait_closed())

if len(sys.argv)>1 and sys.argv[1]=='irc': irc_main()

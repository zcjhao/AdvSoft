import asyncio, hashlib, time, urllib.parse, json, os, re, secrets, sqlite3, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web, WSMsgType
try:
 from zoneinfo import ZoneInfo
except: ZoneInfo=None

DB='/app/data/app.db'; FILES='/app/data/files'
os.makedirs('/app/data',exist_ok=True); os.makedirs(FILES,exist_ok=True)
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
conns=set()

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=db()
 c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT "member",PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
CREATE TABLE IF NOT EXISTS attached(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT);
CREATE TABLE IF NOT EXISTS presence(user_id INTEGER PRIMARY KEY,seen_at REAL);
CREATE TABLE IF NOT EXISTS gm(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
'''); c.commit(); c.close()
def j(row): return dict(row) if row else None
def user(u): return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chan(c): return {'id':c['id'],'workspace_id':c['workspace_id'],'name':c['name'],'is_private':bool(c['is_private']),'is_dm':bool(c['is_dm']),'topic':c['topic'],'is_archived':bool(c['is_archived'])}
def reactions(mid,c):
 rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,)).fetchall(); out=[]
 for r in rows:
  x=next((x for x in out if x['emoji']==r['emoji']),None)
  if not x: x={'emoji':r['emoji'],'count':0,'user_ids':[]}; out.append(x)
  x['count']+=1; x['user_ids'].append(r['user_id'])
 return out
def msg(r,c):
 u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 fs=[]
 for x in c.execute('SELECT f.* FROM files f JOIN attached a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],)):
  fs.append({k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions(r['id'],c),'mentions':mentions(r['body'],r['channel_id'],r['author_id'],c)}
def append(c,cid,typ,**kw):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
 frame={'type':typ,'event_id':n,'channel_id':cid,**kw}; c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(frame))); return frame
def mentions(body,cid,author,c):
 ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); members=[x[0] for x in c.execute('SELECT user_id FROM cm WHERE channel_id=?',(cid,))]
 out=[]
 for x in re.findall(r'(?<!\w)@([A-Za-z0-9_-]+)',body):
  if x=='here':
   # sockets are tracked by token user attribute
   ids=[x[0] for x in c.execute('SELECT user_id FROM presence WHERE seen_at>?',(time.time()-5,))]
   vals=[i for i in members if i in ids]
  elif x=='channel': vals=members
  else:
   u=c.execute('SELECT id FROM users WHERE username=?',(x,)).fetchone()
   vals=[u[0]] if u and u[0] in members else []
   g=c.execute('SELECT id FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],x)).fetchone()
   if g: vals=[z[0] for z in c.execute('SELECT user_id FROM gm WHERE group_id=?',(g[0],)) if z[0] in members]
  for i in vals:
   if i!=author and i not in out: out.append(i)
 return out
def auth(req,c):
 h=req.headers.get('Authorization','')
 if not h.startswith('Bearer '): raise web.HTTPUnauthorized()
 r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone()
 if not r: raise web.HTTPUnauthorized()
 return r
async def body(req):
 try:return await req.json()
 except:return {}
def getch(c,cid):
 r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not r: raise web.HTTPNotFound()
 return r
def readable(c,ch,uid):
 return (not ch['is_private'] and not ch['is_dm'] and c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],uid)).fetchone()) or c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(ch['id'],uid)).fetchone()
def checkread(c,ch,uid):
 if not readable(c,ch,uid): raise web.HTTPForbidden()

async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
 b=await body(req); un=b.get('username',''); pw=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise web.HTTPBadRequest()
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
 except sqlite3.IntegrityError: c.rollback(); raise web.HTTPConflict()
 return web.json_response({'user':user(c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()),'token':t},status=201)
async def login(req):
 b=await body(req); c=db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
 if not u: raise web.HTTPUnauthorized()
 t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id'])); c.commit(); return web.json_response({'user':user(u),'token':t})
async def me(req):
 c=db(); return web.json_response({'user':user(auth(req,c))})
async def profile(req):
 c=db(); u=auth(req,c); b=await body(req)
 allowed=('display_name','status_text','status_emoji','avatar_url','timezone')
 for k in b:
  if k not in allowed: continue
  if not isinstance(b[k],str): raise web.HTTPBadRequest()
  if k=='display_name' and not 1<=len(b[k])<=80: raise web.HTTPBadRequest()
  if k=='status_text' and len(b[k])>140: raise web.HTTPBadRequest()
  if k=='status_emoji' and len(b[k])>64: raise web.HTTPBadRequest()
  if k=='avatar_url' and len(b[k])>1024: raise web.HTTPBadRequest()
  if k=='timezone':
   try: ZoneInfo(b[k])
   except: raise web.HTTPBadRequest()
 for k in allowed:
  if k in b: c.execute(f'UPDATE users SET {k}=? WHERE id=?',(b[k],u['id']))
 c.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps({'type':'user.updated','user':user(c.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone())}),))
 c.commit(); u=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
 return web.json_response({'user':user(u)})
async def getuser(req):
 c=db(); auth(req,c); u=c.execute('SELECT * FROM users WHERE id=?',(int(req.match_info['id']),)).fetchone()
 if not u: raise web.HTTPNotFound()
 return web.json_response({'user':user(u)})
async def workspaces(req):
 c=db(); u=auth(req,c); rows=c.execute('SELECT w.* FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
 return web.json_response({'workspaces':[{'id':x['id'],'slug':x['slug'],'name':x['name'],'owner_id':x['owner_id'],'join_mode':x['join_mode']} for x in rows]})
async def createws(req):
 c=db(); u=auth(req,c); b=await body(req)
 if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')): raise web.HTTPBadRequest()
 try:
  c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id'])); c.commit()
 except sqlite3.IntegrityError: c.rollback(); raise web.HTTPConflict()
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); return web.json_response({'workspace':{'id':wid,'slug':w['slug'],'name':w['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())},status=201)
async def wsdetail(req):
 c=db(); u=auth(req,c); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w: raise web.HTTPNotFound()
 rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall(); rows=[x for x in rows if readable(c,x,u['id'])]
 return web.json_response({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[chan(x) for x in rows],'read_state':[]})
async def createchan(req):
 c=db(); u=auth(req,c); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone(); b=await body(req)
 if not w: raise web.HTTPNotFound()
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],b.get('name'))).fetchone(): raise web.HTTPConflict()
 if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250: raise web.HTTPBadRequest()
 c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id'])); c.commit()
 return web.json_response({'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())},status=201)
async def join(req):
 c=db(); u=auth(req,c); ch=getch(c,int(req.match_info['id'])); c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],u['id'])); c.execute('INSERT OR IGNORE INTO wm(workspace_id,user_id) VALUES(?,?)',(ch['workspace_id'],u['id'])); c.commit(); return web.json_response({'channel':chan(ch)})
async def members(req):
 c=db(); u=auth(req,c); ch=getch(c,int(req.match_info['id'])); checkread(c,ch,u['id']); return web.json_response({'members':[user(x) for x in c.execute('SELECT u.* FROM users u JOIN cm m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],))]})
async def postmsg(req):
 c=db(); u=auth(req,c); ch=getch(c,int(req.match_info['id'])); checkread(c,ch,u['id'])
 b=await body(req); raw=b.get('body','')
 if ch['is_archived'] and not str(raw).startswith('/unarchive'): return web.Response(status=423)
 if not str(raw).strip(): raise web.HTTPBadRequest()
 if raw.startswith('/') and not raw.startswith('//'):
  p=raw.split(' ',1); cmd=p[0][1:]; arg=p[1] if len(p)>1 else ''
  if cmd=='shrug': raw=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd=='me' and not arg.strip(): raise web.HTTPBadRequest()
  elif cmd=='me': pass
  elif cmd=='topic':
   if not arg: raise web.HTTPBadRequest()
   c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id'])); c.commit(); return web.json_response({'message':None,'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())},status=201)
  elif cmd in ('archive','unarchive'):
   owner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if not owner or owner[0] != u['id']: raise web.HTTPForbidden()
   c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='archive' else 0,ch['id'])); c.commit()
   return web.json_response({'message':None,'channel':chan(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())},status=201)
  elif cmd=='invite':
   target=arg.lstrip('@'); x=c.execute('SELECT * FROM users WHERE username=?',(target,)).fetchone()
   if not x: raise web.HTTPNotFound()
   c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(ch['id'],x['id'])); c.commit(); return web.json_response({'message':None,'invited_user':user(x)},status=201)
  else: raise web.HTTPBadRequest()
 parent=b.get('parent_id'); c.execute('BEGIN IMMEDIATE')
 if parent and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,ch['id'])).fetchone(): c.rollback(); raise web.HTTPBadRequest()
 c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],raw,parent,now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 for fid in b.get('file_ids',[]):
  f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM attached WHERE file_id=?',(fid,)).fetchone(): c.rollback(); raise web.HTTPBadRequest()
  c.execute('INSERT INTO attached VALUES(?,?)',(mid,fid))
 r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); fr=msg(r,c); append(c,ch['id'],'message.reply' if parent else 'message.created',message=fr); c.commit()
 return web.json_response({'message':fr},status=201)
async def messages(req):
 c=db(); u=auth(req,c); ch=getch(c,int(req.match_info['id'])); checkread(c,ch,u['id']); lim=int(req.query.get('limit',50))
 if lim<1 or lim>200: raise web.HTTPBadRequest()
 rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(ch['id'],lim)).fetchall()
 return web.json_response({'messages':[msg(x,c) for x in rows],'next_cursor':None})
async def replies(req):
 c=db(); u=auth(req,c); r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(int(req.match_info['id']),)).fetchone()
 if not r: raise web.HTTPNotFound()
 checkread(c,getch(c,r['channel_id']),u['id']); rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(r['id'],)).fetchall(); return web.json_response({'replies':[msg(x,c) for x in rows],'next_cursor':None})
async def react(req):
 c=db(); u=auth(req,c); r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(int(req.match_info['id']),)).fetchone()
 if not r: raise web.HTTPNotFound()
 checkread(c,getch(c,r['channel_id']),u['id']); b=await body(req); e=b.get('emoji')
 if not isinstance(e,str) or not e or len(e)>64: raise web.HTTPBadRequest()
 if not c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],e)).fetchone():
  c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO reactions VALUES(?,?,?,?)',(r['id'],u['id'],e,now())); append(c,r['channel_id'],'reaction.added',message_id=r['id'],emoji=e,user_id=u['id']); c.commit()
 return web.json_response({'reactions':reactions(r['id'],c)})
async def unreact(req):
 c=db(); u=auth(req,c); r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(int(req.match_info['id']),)).fetchone()
 if not r: raise web.HTTPNotFound()
 checkread(c,getch(c,r['channel_id']),u['id']); e=urllib.parse.unquote(req.match_info['emoji']) if False else req.match_info['emoji']
 if c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],e)).fetchone():
  c.execute('BEGIN IMMEDIATE'); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],e)); append(c,r['channel_id'],'reaction.removed',message_id=r['id'],emoji=e,user_id=u['id']); c.commit()
 return web.json_response({'reactions':reactions(r['id'],c)})
async def dm(req):
 c=db(); u=auth(req,c); b=await body(req); tid=b.get('user_id')
 if not tid: raise web.HTTPBadRequest()
 if not c.execute('SELECT 1 FROM users WHERE id=?',(tid,)).fetchone(): raise web.HTTPNotFound()
 a,bid=sorted((u['id'],int(tid))); x=c.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,bid)).fetchone()
 if x: return web.json_response({'channel_id':x[0]})
 c.execute('INSERT INTO channels(name,is_dm,is_private) VALUES(?,?,?)',(f'dm-{a}-{bid}',1,1)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO dms VALUES(?,?,?)',(a,bid,cid)); c.execute('INSERT INTO cm VALUES(?,?),(?,?)',(cid,a,cid,bid)); c.commit(); return web.json_response({'channel_id':cid})
async def upload(req):
 c=db(); u=auth(req,c); rd=await req.multipart(); part=None
 async for p in rd:
  if p.name=='file': part=p; break
 if not part: raise web.HTTPBadRequest()
 data=await part.read()
 if len(data)>10485760: raise web.HTTPRequestEntityTooLarge(max_size=10485760, actual_size=len(data))
 fid=secrets.randbits(60); path=f'{FILES}/{fid}'; open(path,'wb').write(data); c.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?)',(fid,u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),path)); c.commit()
 return web.json_response({'file':{k:c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}},status=201)
async def filemeta(req):
 c=db(); auth(req,c); f=c.execute('SELECT * FROM files WHERE id=?',(int(req.match_info['id']),)).fetchone()
 if not f: raise web.HTTPNotFound()
 return web.json_response({'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
async def download(req):
 c=db(); auth(req,c); f=c.execute('SELECT * FROM files WHERE id=?',(int(req.match_info['id']),)).fetchone()
 if not f: raise web.HTTPNotFound()
 return web.Response(body=open(f['path'],'rb').read(),content_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
async def groups(req):
 c=db(); u=auth(req,c); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w: raise web.HTTPNotFound()
 member=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not member: raise web.HTTPForbidden()
 handle=req.match_info.get('handle')
 if req.method=='GET':
  gs=[]
  for g in c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)):
   gs.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x[0] for x in c.execute('SELECT user_id FROM gm WHERE group_id=?',(g['id'],))]})
  return web.json_response({'groups':gs})
 if handle:
  g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
  if not g:
   if req.method=='DELETE': return web.json_response({'deleted':True})
   raise web.HTTPNotFound()
  if req.method=='DELETE':
   if g['creator_id']!=u['id'] and member['role']!='owner': raise web.HTTPForbidden()
   c.execute('DELETE FROM gm WHERE group_id=?',(g['id'],)); c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],)); c.commit()
   return web.json_response({'deleted':True})
  if g['creator_id']!=u['id'] and member['role']!='owner': raise web.HTTPForbidden()
  b=await body(req)
  if 'name' in b: c.execute('UPDATE groups_ SET name=? WHERE id=?',(b['name'],g['id']))
  if 'user_ids' in b:
   ids=b['user_ids']
   if any(not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],x)).fetchone() for x in ids): raise web.HTTPBadRequest()
   c.execute('DELETE FROM gm WHERE group_id=?',(g['id'],))
   for x in ids: c.execute('INSERT INTO gm VALUES(?,?)',(g['id'],x))
  c.commit(); g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone()
  return web.json_response({'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x[0] for x in c.execute('SELECT user_id FROM gm WHERE group_id=?',(g['id'],))]}})
 b=await body(req); h=b.get('handle','')
 if not b.get('name') or not re.fullmatch(r'[a-z0-9-]+',h): raise web.HTTPBadRequest()
 if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone(): raise web.HTTPConflict()
 ids=b.get('user_ids',[])
 if any(not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],x)).fetchone() for x in ids): raise web.HTTPBadRequest()
 c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,b['name'],u['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 for x in ids: c.execute('INSERT INTO gm VALUES(?,?)',(gid,x))
 c.commit(); return web.json_response({'group':{'id':gid,'handle':h,'name':b['name'],'member_user_ids':ids}},status=201)
async def ws_handler(req):
 c=db(); tok=req.query.get('token',''); u=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone()
 if not u: raise web.HTTPUnauthorized()
 w=web.WebSocketResponse(); await w.prepare(req); w.uid=u['id']; w.subs={}; w.gseq=c.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0]; conns.add(w); c.execute('INSERT OR REPLACE INTO presence VALUES(?,?)',(u['id'],time.time())); c.commit()
 try:
  async for m in w:
   if m.type==WSMsgType.TEXT:
    b=json.loads(m.data); cid=b.get('channel_id')
    if b.get('type')=='subscribe':
     h=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; w.subs[cid]=h; await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
    elif b.get('type')=='resume':
     n=b.get('since_event_id',0)
     for x in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,n)): await w.send_str(x[0])
     h=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':h}); w.subs[cid]=h
 finally:
  conns.discard(w); c.execute('DELETE FROM presence WHERE user_id=?',(u['id'],)); c.commit()
 return w
async def pump(app):
 while True:
  c=None
  try:
   c=db()
   for w in list(conns):
    subs=getattr(w,'subs',{})
    if not hasattr(w,'gseq'): w.gseq=0
    for row in c.execute('SELECT id,frame FROM global_events WHERE id>? ORDER BY id',(w.gseq,)).fetchall():
     if not w.closed:
      await w.send_str(row['frame']); w.gseq=row['id']
    for cid,n in list(subs.items()):
     for row in c.execute('SELECT frame,event_id FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,n)).fetchall():
      if not w.closed: await w.send_str(row['frame']); subs[cid]=row['event_id']
  except Exception:
   pass
  finally:
   if c:
    try: c.close()
    except Exception: pass
  await asyncio.sleep(.05)

def setup(app):
 init()
 app.router.add_get('/api/health',health); app.router.add_post('/api/auth/register',register); app.router.add_post('/api/auth/login',login); app.router.add_get('/api/auth/me',me)
 app.router.add_patch('/api/users/me',profile); app.router.add_get('/api/users/{id}',getuser); app.router.add_get('/api/workspaces',workspaces); app.router.add_post('/api/workspaces',createws); app.router.add_get('/api/workspaces/{slug}',wsdetail); app.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',groups); app.router.add_route('*','/api/workspaces/{slug}/groups',groups); app.router.add_post('/api/workspaces/{slug}/channels',createchan)
 app.router.add_post('/api/channels/{id}/join',join); app.router.add_get('/api/channels/{id}/members',members); app.router.add_post('/api/channels/{id}/messages',postmsg); app.router.add_get('/api/channels/{id}/messages',messages); app.router.add_get('/api/messages/{id}/replies',replies)
 app.router.add_post('/api/messages/{id}/reactions',react); app.router.add_delete('/api/messages/{id}/reactions/{emoji:.*}',unreact); app.router.add_post('/api/dms',dm); app.router.add_post('/api/files',upload); app.router.add_get('/api/files/{id}',filemeta); app.router.add_get('/api/files/{id}/download',download); app.router.add_get('/api/ws',ws_handler)
 async def start_pump(a):
  a['pump']=asyncio.create_task(pump(a))
 app.on_startup.append(start_pump)
if __name__=='__main__':
 app=web.Application(client_max_size=11*1024*1024); setup(app); web.run_app(app,host='127.0.0.1',port=int(sys.argv[3]) if len(sys.argv)>3 else 8000)

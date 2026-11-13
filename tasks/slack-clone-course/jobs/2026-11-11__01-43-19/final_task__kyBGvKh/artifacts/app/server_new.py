import asyncio, json, os, re, sqlite3, sys, time, uuid, hashlib, secrets
from aiohttp import web, WSMsgType
DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id)); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT); CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE); CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle)); CREATE TABLE IF NOT EXISTS online(channel_id INTEGER,user_id INTEGER,updated_at REAL DEFAULT 0,PRIMARY KEY(channel_id,user_id)); CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY,updated_at REAL DEFAULT 0);''');
 try: c.execute('alter table online add column updated_at REAL DEFAULT 0')
 except sqlite3.OperationalError: pass
 try: c.execute('alter table online_users add column updated_at REAL DEFAULT 0')
 except sqlite3.OperationalError: pass
 c.commit(); c.close()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+('%03d'%(int(time.time()*1000)%1000))+'Z'
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def msgobj(c,r):
 a=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 rs=[]
 for x in c.execute('select emoji,count(*) n,min(rowid) first from reactions where message_id=? group by emoji order by first',(r['id'],)):
  ids=[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]
  rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 replies=c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('select f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at from files f join attachments a on a.file_id=f.id where a.message_id=?',(r['id'],))],'reactions':rs,'mentions':[]}
async def auth(req):
 t=req.headers.get('Authorization','').replace('Bearer ','') or req.query.get('token','').replace('Bearer ','')
 if not t: raise web.HTTPUnauthorized()
 c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone(); c.close()
 if not r: raise web.HTTPUnauthorized()
 return r
def out(x,status=200): return web.json_response(x,status=status)
async def health(req): return out({'status':'ok','node_id':int(req.app['node'])})
async def register(req):
 d=await req.json(); n=d.get('username',''); p=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',n) or len(p)<8: return out({'error':'invalid'},400)
 c=db()
 try:
  c.execute('insert into users(username,password,display_name) values(?,?,?)',(n,p,d.get('display_name') or n)); uid=c.execute('select last_insert_rowid()').fetchone()[0]; tok=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(tok,uid)); c.commit(); r=c.execute('select * from users where id=?',(uid,)).fetchone(); return out({'user':userobj(r),'token':tok},201)
 except sqlite3.IntegrityError: return out({'error':'duplicate'},409)
 finally:c.close()
async def login(req):
 d=await req.json(); c=db(); r=c.execute('select * from users where username=? and password=?',(d.get('username'),d.get('password'))).fetchone()
 if not r:c.close(); return out({'error':'bad'},401)
 t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,r['id'])); c.commit(); c.close(); return out({'user':userobj(r),'token':t})
async def me(req): return out({'user':userobj(await auth(req))})
async def profile(req):
 u=await auth(req); c=db()
 if req.method=='GET': r=c.execute('select * from users where id=?',(req.match_info['id'],)).fetchone(); c.close(); return out({'user':userobj(r)} if r else {},200 if r else 404)
 d=await req.json(); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
 if any(k not in allowed for k in d): return out({'error':'invalid'},400)
 from zoneinfo import ZoneInfo
 limits={'display_name':80,'status_text':140,'status_emoji':64,'avatar_url':1024}
 if any(not isinstance(v,str) or not v or len(v)>limits.get(k,100) for k,v in d.items()): return out({'error':'invalid'},400)
 if 'timezone' in d:
  try: ZoneInfo(d['timezone'])
  except Exception: return out({'error':'invalid'},400)
 for k,v in d.items(): c.execute('update users set %s=? where id=?'%(k,), (v,u['id']))
 c.execute('insert into global_events(frame) values(?)',(json.dumps({'type':'user.updated','user':userobj(c.execute("select * from users where id=?",(u['id'],)).fetchone())}),)); c.commit(); r=c.execute('select * from users where id=?',(u['id'],)).fetchone(); c.close(); return out({'user':userobj(r)})
async def workspaces(req):
 u=await auth(req); c=db()
 if req.method=='GET':
  a=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall(); c.close(); return out({'workspaces':[dict(x) for x in a]})
 d=await req.json()
 if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')): return out({},400)
 try:
  c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(d['slug'],d.get('name',''),u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,u['id'])); c.commit(); w=dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()); ch=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); c.close(); return out({'workspace':w,'general_channel':ch},201)
 except sqlite3.IntegrityError:c.close(); return out({},409)
async def workspace_detail(req):
 u=await auth(req); c=db(); w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return out({},404)
 a=c.execute('select c.* from channels c join cmembers m on m.channel_id=c.id where c.workspace_id=? and m.user_id=?',(w['id'],u['id'])).fetchall(); c.close(); return out({'workspace':dict(w),'channels':[dict(x) for x in a],'read_state':{}})
async def channels(req):
 u=await auth(req); slug=req.match_info['slug']; c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w:return out({},404)
 d=await req.json(); n=d.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',n):return out({},400)
 if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],n)).fetchone(): c.close(); return out({},409)
 try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],n,int(bool(d.get('is_private'))),d.get('topic',''))); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,u['id'])); c.commit(); r=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); c.close(); return out({'channel':r},201)
 except sqlite3.IntegrityError:c.close();return out({},409)
async def join(req):
 u=await auth(req); c=db(); ch=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
 if not ch:return out({},404)
 c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],u['id'],'member')); c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],u['id'])); c.commit(); c.close(); return out({'channel':dict(ch)})
async def messages(req):
 u=await auth(req); cid=int(req.match_info['id']); c=db(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
 if not ch:return out({},404)
 if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone(): c.close(); return out({},403)
 if req.method=='GET':
  a=c.execute('select * from messages where channel_id=? and parent_id is null and deleted=0 order by id desc limit 200',(cid,)).fetchall(); z=[msgobj(c,x) for x in a]; c.close(); return out({'messages':z,'next_cursor':None})
 d=await req.json(); body=d.get('body','')
 if not body.strip():c.close();return out({},400)
 if body.startswith('/me') and not body[3:].strip(): c.close(); return out({},400)
 if body.startswith('/') and not body.startswith('//'):
  if body.startswith('/shrug'): body=(body[6:].strip()+' ' if body[6:].strip() else '')+'¯\\_(ツ)_/¯'
  elif body.startswith('/me '):
   if not body[4:].strip(): c.close(); return out({},400)
  elif body=='/me' or body.startswith('/me   '): c.close(); return out({},400)
  elif body.startswith('/invite '):
   name=body[8:].strip().lstrip('@'); target=c.execute('select id from users where username=?',(name,)).fetchone()
   if not target: c.close(); return out({},404)
   c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],target['id'],'member')); c.execute('insert or ignore into cmembers values(?,?)',(cid,target['id'])); c.commit(); invited=c.execute('select * from users where id=?',(target['id'],)).fetchone(); c.close(); return out({'message':None,'invited_user':userobj(invited)},201)
  elif body=='/archive' or body=='/unarchive':
   owner=c.execute('select 1 from workspaces w where w.id=? and w.owner_id=?',(ch['workspace_id'],u['id'])).fetchone()
   if not owner: c.close(); return out({},403)
   val=1 if body=='/archive' else 0; c.execute('update channels set is_archived=? where id=?',(val,cid)); c.commit(); ch=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); ch['is_archived']=bool(ch['is_archived']); c.close(); return out({'message':None,'channel':ch},201)
  elif body.startswith('/topic '):
   c.execute('update channels set topic=? where id=?',(body[7:],cid)); c.commit(); ch=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); ch['is_archived']=bool(ch['is_archived']); c.close(); return out({'message':None,'channel':ch},201)
  elif body.startswith('/'):c.close();return out({},400)
 fileids=d.get('file_ids') or []
 if fileids:
  for fid in fileids:
   fr=c.execute('select * from files where id=?',(fid,)).fetchone()
   if not fr or fr['uploader_id']!=u['id'] or c.execute('select 1 from attachments where file_id=?',(fid,)).fetchone(): c.close(); return out({},400)
 if ch['is_archived']: c.close(); return out({},423)
 mentions=[]
 for q in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
  if q in ('here','channel'):
   for z in c.execute('select m.user_id from cmembers m join online_users o on o.user_id=m.user_id where m.channel_id=? and o.updated_at>?',(cid,time.time()-5.0)):
    if z['user_id']!=u['id']: mentions.append(z['user_id'])
  else:
   gr=c.execute('select user_ids from groups where workspace_id=? and handle=?',(ch['workspace_id'],q)).fetchone()
   if gr:
    for x in json.loads(gr['user_ids']): 
     if x!=u['id'] and c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,x)).fetchone(): mentions.append(x)
   else:
    z=c.execute('select id from users where username=?',(q,)).fetchone()
    if z and z['id']!=u['id'] and c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,z['id'])).fetchone(): mentions.append(z['id'])
 mentions=list(dict.fromkeys(mentions))
 c.execute('begin immediate'); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),now())); mid=c.execute('select last_insert_rowid()').fetchone()[0]; r=c.execute('select * from messages where id=?',(mid,)).fetchone(); eid=(c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0]); frame={'type':'message.reply' if d.get('parent_id') else 'message.created','event_id':eid,'channel_id':cid,'message':msgobj(c,r)}; c.execute('insert into events values(?,?,?)',(cid,eid,json.dumps(frame))); [c.execute('insert into attachments values(?,?)',(mid,fid)) for fid in fileids]; frame['message']=msgobj(c,r); c.commit(); c.close(); return out({'message':dict(frame['message'],mentions=mentions)},201)
async def replies(req):
 u=await auth(req); c=db(); a=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(req.match_info['id'],)).fetchall(); z=[msgobj(c,x) for x in a]; c.close(); return out({'replies':z,'next_cursor':None})
async def message_edit(req):
 u=await auth(req); mid=int(req.match_info['id']); c=db()
 r=c.execute('select m.*,ch.workspace_id from messages m join channels ch on ch.id=m.channel_id where m.id=?',(mid,)).fetchone()
 if not r:c.close();return out({},404)
 owner=c.execute('select owner_id from workspaces where id=?',(r['workspace_id'],)).fetchone()['owner_id']==u['id']
 if req.method=='PATCH':
  if r['deleted']:c.close();return out({},409)
  if r['author_id']!=u['id']:c.close();return out({},403)
  d=await req.json(); body=d.get('body','')
  if not isinstance(body,str) or not body.strip():c.close();return out({},400)
  c.execute('begin immediate'); edited=now(); c.execute('update messages set body=?,edited_at=? where id=?',(body,edited,mid))
  r=c.execute('select * from messages where id=?',(mid,)).fetchone(); eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(r['channel_id'],)).fetchone()[0]
  frame={'type':'message.updated','event_id':eid,'channel_id':r['channel_id'],'message':msgobj(c,r)}; c.execute('insert into events values(?,?,?)',(r['channel_id'],eid,json.dumps(frame)));c.commit();c.close();return out({'message':frame['message']})
 if not owner and r['author_id']!=u['id']:c.close();return out({},403)
 if not r['deleted']:
  c.execute('begin immediate');c.execute('update messages set deleted=1 where id=?',(mid,));eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(r['channel_id'],)).fetchone()[0];frame={'type':'message.deleted','event_id':eid,'channel_id':r['channel_id'],'message_id':mid};c.execute('insert into events values(?,?,?)',(r['channel_id'],eid,json.dumps(frame)));c.commit()
 c.close();return out({'deleted':True})

async def react(req):
 u=await auth(req); mid=int(req.match_info['id']); c=db()
 m=c.execute('select m.*,ch.workspace_id,ch.is_private from messages m join channels ch on ch.id=m.channel_id where m.id=? and m.deleted=0',(mid,)).fetchone()
 if not m:c.close();return out({},404)
 member=c.execute('select 1 from cmembers where channel_id=? and user_id=?',(m['channel_id'],u['id'])).fetchone()
 if not member:c.close();return out({},403)
 em=req.match_info.get('emoji')
 if req.method=='POST':
  d=await req.json(); em=d.get('emoji')
  if not isinstance(em,str) or not em or len(em)>64:c.close();return out({},400)
 else:
  if not isinstance(em,str) or not em or len(em)>64:c.close();return out({},400)
 c.execute('begin immediate')
 before=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],em)).fetchone()
 changed=False
 if req.method=='POST' and not before:
  c.execute('insert into reactions values(?,?,?)',(mid,u['id'],em)); changed=True; kind='reaction.added'
 elif req.method=='DELETE' and before:
  c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],em)); changed=True; kind='reaction.removed'
 if changed:
  eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(m['channel_id'],)).fetchone()[0]
  frame={'type':kind,'event_id':eid,'channel_id':m['channel_id'],'message_id':mid,'emoji':em,'user_id':u['id']}
  c.execute('insert into events values(?,?,?)',(m['channel_id'],eid,json.dumps(frame)))
 c.commit()
 a=[]
 for x in c.execute('select emoji,count(*) n,min(rowid) first from reactions where message_id=? group by emoji order by first',(mid,)):
  ids=[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=? order by rowid',(mid,x['emoji']))]
  a.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 c.close();return out({'reactions':a})

async def files(req):
 u=await auth(req)
 if req.method=='POST':
  try: f=await req.multipart(); part=await f.next()
  except: return out({},400)
  if not part or part.name!='file': return out({},400)
  data=await part.read(decode=False)
  if len(data)>10485760:return out({},413)
  c=db(); c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],part.filename,part.headers.get('Content-Type','application/octet-stream'),len(data),now(),data)); fid=c.execute('select last_insert_rowid()').fetchone()[0]; r=c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone();c.commit();c.close();return out({'file':dict(r)},201)
 fid=req.match_info.get('id'); c=db(); r=c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone(); c.close(); return out({'file':dict(r)} if r else {},200 if r else 404)
async def download(req):
 u=await auth(req);c=db();r=c.execute('select * from files where id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({},404)
 resp=web.Response(body=r['data'],content_type=r['content_type']);resp.headers['Content-Disposition']='attachment; filename="'+r['filename']+'"';c.close();return resp

async def channel_update(req):
 u=await auth(req); c=db(); ch=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
 if not ch:c.close();return out({},404)
 if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():c.close();return out({},403)
 d=await req.json(); topic=d.get('topic','')
 if not isinstance(topic,str) or len(topic)>250:c.close();return out({},400)
 c.execute('update channels set topic=? where id=?',(topic,ch['id']));c.commit(); r=dict(c.execute('select * from channels where id=?',(ch['id'],)).fetchone());c.close();return out({'channel':r})

async def channel_members(req):
 u=await auth(req); c=db(); ch=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
 if not ch:c.close();return out({},404)
 if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():c.close();return out({},403)
 a=c.execute('select u.* from users u join cmembers m on m.user_id=u.id where m.channel_id=? order by u.id',(ch['id'],)).fetchall();c.close();return out({'members':[userobj(x) for x in a]})

def groupobj(r):
 return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'] or '[]')}
async def groups_api(req):
 u=await auth(req); c=db(); w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return out({},404)
 role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
 if not role:c.close();return out({},403)
 if req.method=='GET':
  a=c.execute('select * from groups where workspace_id=?',(w['id'],)).fetchall();c.close();return out({'groups':[groupobj(x) for x in a]})
 d=await req.json();h=d.get('handle','');name=d.get('name')
 if not re.fullmatch(r'[a-z0-9_-]{1,32}',h) or not name or not isinstance(name,str):c.close();return out({},400)
 ids=d.get('user_ids') or []
 if any(not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],x)).fetchone() for x in ids):c.close();return out({},400)
 try:c.execute('insert into groups(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],h,name,u['id'],json.dumps(ids)));c.commit();r=c.execute('select * from groups where id=last_insert_rowid()').fetchone();c.close();return out({'group':groupobj(r)},201)
 except sqlite3.IntegrityError:c.close();return out({},409)
async def group_item(req):
 u=await auth(req);c=db();w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone();g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],req.match_info['handle'])).fetchone()
 if not g:
  if req.method=='DELETE': c.close(); return out({'deleted':True})
  c.close();return out({},404)
 if req.method=='DELETE':
  if u['id'] not in (g['creator_id'],w['owner_id']):c.close();return out({},403)
  c.execute('delete from groups where id=?',(g['id'],));c.commit();c.close();return out({'deleted':True})
 if u['id'] not in (g['creator_id'],w['owner_id']):c.close();return out({},403)
 d=await req.json();ids=d.get('user_ids',json.loads(g['user_ids']));name=d.get('name',g['name'])
 if any(not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],x)).fetchone() for x in ids):c.close();return out({},400)
 c.execute('update groups set name=?,user_ids=? where id=?',(name,json.dumps(ids),g['id']));c.commit();g=c.execute('select * from groups where id=?',(g['id'],)).fetchone();c.close();return out({'group':groupobj(g)})

async def dms(req):
 u=await auth(req); d=await req.json(); other=d.get('user_id')
 if not other:return out({},400)
 c=db(); target=c.execute('select id from users where id=?',(other,)).fetchone()
 if not target:c.close();return out({},404)
 a,b=sorted((int(u['id']),int(other)))
 row=c.execute('select c.id from channels c join cmembers x on x.channel_id=c.id and x.user_id=? join cmembers y on y.channel_id=c.id and y.user_id=? where c.is_dm=1 and x.user_id<>y.user_id',(a,b)).fetchone()
 if row:c.close();return out({'channel_id':row['id']})
 c.execute('insert into channels(workspace_id,name,is_dm) values(NULL,?,?)',(f'dm-{a}-{b}',1)); cid=c.execute('select last_insert_rowid()').fetchone()[0]
 c.execute('insert into cmembers values(?,?),(?,?)',(cid,a,cid,b)); c.commit();c.close();return out({'channel_id':cid})

async def ws_handler(req):
 try:u=await auth(req)
 except: raise web.HTTPUnauthorized()
 ws=web.WebSocketResponse(); await ws.prepare(req); c0=db(); c0.execute('insert into online_users(user_id,updated_at) values(?,?) on conflict(user_id) do update set updated_at=excluded.updated_at',(u['id'],time.time())); c0.commit(); last={'_global':c0.execute('select coalesce(max(id),0) from global_events').fetchone()[0]}; c0.close()
 async def pump():
  while not ws.closed:
   await asyncio.sleep(.05); c=db()
   c.execute('update online_users set updated_at=? where user_id=?',(time.time(),u['id']))
   for pcid in [x for x in last if x != '_global']:
    c.execute('update online set updated_at=? where channel_id=? and user_id=?',(time.time(),pcid,u['id']))
   c.commit()
   for g in c.execute('select id,frame from global_events where id>? order by id',(last.get('_global',0),)):
    await ws.send_str(g['frame']); last['_global']=g['id']
   for cid in list(last):
    rows=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last[cid])).fetchall()
    for r in rows: await ws.send_str(r['frame']);last[cid]=r['event_id']
   c.close()
 task=asyncio.create_task(pump())
 try:
  async for m in ws:
   if m.type==WSMsgType.TEXT:
    d=json.loads(m.data); cid=int(d['channel_id']); c=db(); c.execute('insert into online(channel_id,user_id,updated_at) values(?,?,?) on conflict(channel_id,user_id) do update set updated_at=excluded.updated_at',(cid,u['id'],time.time())); c.commit(); c.close(); c=db(); h=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(cid,)).fetchone()[0]; c.close()
    if d['type']=='resume':
     for r in db().execute('select frame from events where channel_id=? and event_id>? order by event_id',(cid,int(d.get('since_event_id',0)))):
      await ws.send_str(r['frame'])
     await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':h})
    else: await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
    last[cid]=h
 finally:
  c=db(); c.execute('delete from online where user_id=?',(u['id'],)); c.execute('delete from online_users where user_id=?',(u['id'],)); c.commit(); c.close(); task.cancel()
 return ws
IRC_NICKS=set()
async def irc_client(reader,writer):
 nick=None; tok=None; uid=None; chans={}; user_sent=False; registered=False; pending_uid=None; seen={}; stop=False
 def send(s): writer.write((s+'\r\n').encode())
 async def event_pump():
  while not stop:
   await asyncio.sleep(.05)
   if not chans: continue
   c=db()
   for cid,target in list(chans.items()):
    rows=c.execute('select event_id,frame from events where channel_id=? and event_id>? order by event_id',(cid,seen.get(cid,0))).fetchall()
    for rr in rows:
     seen[cid]=rr['event_id']; f=json.loads(rr['frame'])
     if f.get('type') in ('message.created','message.reply'):
      mm=f.get('message') or {}; au=c.execute('select username from users where id=?',(mm.get('author_id'),)).fetchone()
      if au:
       writer.write((':%s!%s@h PRIVMSG #%s :%s\r\n'%(au['username'],au['username'],target,mm.get('body',''))).encode()); await writer.drain()
   c.close()
 task=asyncio.create_task(event_pump())
 while True:
  line=await reader.readline()
  if not line:break
  x=line.decode(errors='ignore').strip(); p=x.split(); cmd=p[0].upper() if p else ''
  if cmd=='PASS':tok=p[1] if len(p)>1 else ''
  elif cmd=='NICK':
   nn=p[1] if len(p)>1 else 'guest'
   if nn in IRC_NICKS and nn != nick: send(':huddle 433 * '+nn+' :Nickname is already in use')
   else:
    nick=nn
    if tok and user_sent and not registered:
     c=db(); r=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(tok,)).fetchone(); uid=r['id'] if r else None; c.close()
     if uid:
      IRC_NICKS.add(nick); registered=True
      for n in ['001','002','003','004','005','422']:send(':huddle '+n+' '+nick+' :welcome')
  elif cmd=='USER':
   user_sent=True
   if not tok:
    send(':huddle 464 * :Password incorrect')
   elif not registered:
    c=db(); r=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(tok,)).fetchone(); uid=r['id'] if r else None;c.close()
    if not uid: send(':huddle 464 * :Password incorrect')
    else:
     if not nick or nick in IRC_NICKS:
      pending_uid=uid
     else:
      IRC_NICKS.add(nick); registered=True
      for n in ['001','002','003','004','005','422']:send(':huddle '+n+' '+nick+' :welcome')
  elif cmd=='USER' and tok:
   c=db(); r=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(tok,)).fetchone(); uid=r['id'] if r else None;c.close()
   if uid:
    for n in ['001','002','003','004','005','422']:send(':huddle '+n+' '+nick+' :welcome')
  elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'): pass
  elif cmd=='PING':send(':'+(' '.join(p[1:]) if len(p)>1 else 'x')+' PONG')
  elif cmd=='JOIN' and uid:
   target=p[1].lstrip('#'); wsname,cn=target.split('/',1) if '/' in target else ('','')
   c=db(); ch=c.execute('select c.* from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(wsname,cn)).fetchone()
   if not ch:send('403 '+nick+' '+p[1]+' :No such channel')
   else: chans[ch['id']]=target; seen[ch['id']]=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(ch['id'],)).fetchone()[0]; send(':'+nick+'!u@h JOIN #'+target);send(':huddle 353 '+nick+' = #'+target+' :'+nick);send(':huddle 366 '+nick+' #'+target+' :End')
   c.close()
  elif cmd=='PRIVMSG' and len(p)>2:
   target=p[1].lstrip('#'); body=x.split(' :',1)[1] if ' :' in x else ' '.join(p[2:]); c=db(); q=target.split('/',1); ch=c.execute('select c.* from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(q[0],q[1])).fetchone()
   if ch:
    c.execute('begin immediate'); c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(ch['id'],uid,body,now())); mid=c.execute('select last_insert_rowid()').fetchone()[0]; mr=c.execute('select * from messages where id=?',(mid,)).fetchone(); eid=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch['id'],)).fetchone()[0]; frame={'type':'message.created','event_id':eid,'channel_id':ch['id'],'message':msgobj(c,mr)}; c.execute('insert into events values(?,?,?)',(ch['id'],eid,json.dumps(frame))); c.commit()
   c.close()
  elif cmd=='QUIT':break
  elif cmd not in ('PASS','NICK','USER','PING','JOIN','PRIVMSG'): send(':huddle 421 '+(nick or '*')+' '+cmd+' :Unknown command')
 stop=True; task.cancel(); writer.close()
async def run_http(node,port):
 init(); app=web.Application(client_max_size=11*1024*1024);app['node']=node
 app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
 app.router.add_route('*','/api/users/{id}',profile);app.router.add_route('*','/api/users/me',profile)
 app.router.add_route('*','/api/workspaces',workspaces); app.router.add_route('*','/api/workspaces/{slug}/groups',groups_api); app.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',group_item); app.router.add_get('/api/workspaces/{slug}',workspace_detail);app.router.add_post('/api/workspaces/{slug}/channels',channels)
 app.router.add_post('/api/channels/{id}/join',join); app.router.add_patch('/api/channels/{id}',channel_update); app.router.add_get('/api/channels/{id}/members',channel_members); app.router.add_post('/api/dms',dms); app.router.add_route('*','/api/files',files); app.router.add_get('/api/files/{id}',files); app.router.add_get('/api/files/{id}/download',download);app.router.add_route('*','/api/channels/{id}/messages',messages);app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_route('*','/api/messages/{id}',message_edit);app.router.add_route('*','/api/messages/{id}/reactions',react);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',react)
 app.router.add_get('/api/ws',ws_handler);app.router.add_get('/',lambda r:web.FileResponse('/app/static/index.html'))
 await web._run_app(app,host='127.0.0.1',port=port)
async def main():
 if sys.argv[1]=='irc':srv=await asyncio.start_server(irc_client,'0.0.0.0',int(sys.argv[2]));await srv.serve_forever()
 else:await run_http(int(sys.argv[2]),int(sys.argv[3]))
if __name__=='__main__':asyncio.run(main())

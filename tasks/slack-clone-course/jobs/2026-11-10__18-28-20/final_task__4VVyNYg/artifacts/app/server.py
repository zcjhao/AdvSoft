import asyncio,base64,hashlib,json,os,re,secrets,sqlite3,sys,datetime, pathlib
from aiohttp import web,WSMsgType
DB='/app/data/app.db'; FILES='/app/data/files'
os.makedirs(os.path.dirname(DB),exist_ok=True); os.makedirs(FILES,exist_ok=True)
def db():
 c=sqlite3.connect(DB,timeout=30,isolation_level=None); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=5000'); return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def init():
 c=db()
 for _ in range(60):
  try:
   c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); break
  except sqlite3.OperationalError:
   import time; time.sleep(.1)
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS wmembers(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT "member",UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT,attached INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS groupmembers(group_id INTEGER,user_id INTEGER,UNIQUE(group_id,user_id));
CREATE TABLE IF NOT EXISTS presence(user_id INTEGER,node_id INTEGER,UNIQUE(user_id,node_id));'''); c.commit(); c.close()
def user(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('Authorization',''); t=h[7:] if h.startswith('Bearer ') else ''
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def js(x): return json.dumps(x,separators=(',',':'))
def out(data,status=200): return web.json_response(data,status=status)
def event(c,cid,typ,**kw):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
 frame={'type':typ,'event_id':n,'channel_id':cid,**kw}; c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,js(frame))); return frame
def chan(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def mentions(c,r):
 ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
 if not ch:return []
 members={x['user_id'] for x in c.execute('SELECT user_id FROM members WHERE channel_id=?',(r['channel_id'],))}
 ws=ch['workspace_id']; ids=set()
 online={x['user_id'] for x in c.execute('SELECT DISTINCT user_id FROM presence')}
 text=r['body'] or ''
 for m in re.finditer(r'(?<![A-Za-z0-9_.@])@([A-Za-z0-9_-]+)',text):
  h=m.group(1)
  if h=='channel': ids.update(members)
  elif h=='here': ids.update(members & online)
  else:
   u=c.execute('SELECT id FROM users WHERE username=?',(h,)).fetchone()
   if u and u['id'] in members: ids.add(u['id'])
   g=c.execute('SELECT id FROM groups_ WHERE workspace_id=? AND handle=?',(ws,h)).fetchone()
   if g: ids.update(x[0] for x in c.execute('SELECT user_id FROM groupmembers WHERE group_id=?',(g['id'],)) if x[0] in members)
 ids.discard(r['author_id']); return list(ids)
def groupjson(c,g):
 return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x[0] for x in c.execute('SELECT user_id FROM groupmembers WHERE group_id=? ORDER BY rowid',(g['id'],))]}
def workspace_for(c,slug):
 return c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
def ws_member(c,wid,uid): return c.execute('SELECT * FROM wmembers WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone()

def msg(c,r):
 u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rx=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,MIN(rowid) first FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY first',(r['id'],)):
  ids=[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=? ORDER BY rowid',(r['id'],x['emoji']))]
  rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM message_files WHERE message_id=?)',(r['id'],)).fetchall()] if 'message_files' in [x['name'] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")] else [],'reactions':rx,'mentions':mentions(c,r)}
async def body(req):
 try:return await req.json()
 except:return {}
def require(fn):
 async def wrap(req):
  req['user']=auth(req)
  if not req['user']: return out({'error':'unauthorized'},401)
  return await fn(req)
 return wrap
async def health(req): return out({'status':'ok','node_id':int(req.app['node'])})
async def register(req):
 b=await body(req); un=b.get('username',''); pw=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return out({'error':'invalid'},400)
 c=db()
 try:
  c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
 except sqlite3.IntegrityError:return out({'error':'duplicate'},409)
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return out({'user':user(r),'token':t},201)
async def login(req):
 b=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
 if not r:c.close();return out({'error':'invalid'},401)
 t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return out({'user':user(r),'token':t})
@require
async def me(req): return out({'user':user(req['user'])})
@require
async def profile(req):
 b=await body(req); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
 if 'display_name' in b and not isinstance(b['display_name'],str) or 'display_name' in b and not 1<=len(b['display_name'])<=80:return out({'error':'length'},400)
 if 'status_text' in b and (not isinstance(b['status_text'],str) or len(b['status_text'])>140):return out({'error':'length'},400)
 if 'avatar_url' in b and (not isinstance(b['avatar_url'],str) or len(b['avatar_url'])>2048):return out({'error':'length'},400)
 if 'status_emoji' in b and (not isinstance(b['status_emoji'],str) or len(b['status_emoji'])>32):return out({'error':'length'},400)
 if 'avatar_url' in b and (not isinstance(b['avatar_url'],str) or len(b['avatar_url'])>512):return out({'error':'length'},400)
 if 'timezone' in b:
  try:
   from zoneinfo import ZoneInfo
   if not b['timezone']: raise ValueError()
   ZoneInfo(b['timezone'])
  except Exception:return out({'error':'timezone'},400)
 c=db(); sets=[]; vals=[]
 for k in allowed:
  if k in b: sets.append(k+'=?'); vals.append(b[k])
 if sets:
  c.execute('UPDATE users SET '+','.join(sets)+' WHERE id=?',(*vals,req['user']['id']))
  r=c.execute('SELECT * FROM users WHERE id=?',(req['user']['id'],)).fetchone()
  c.execute('INSERT INTO events(channel_id,event_id,frame) VALUES(0,COALESCE((SELECT MAX(event_id)+1 FROM events WHERE channel_id=0),1),?)',(js({'type':'user.updated','user':user(r)}),))
  c.commit()
 else:r=c.execute('SELECT * FROM users WHERE id=?',(req['user']['id'],)).fetchone()
 c.close();return out({'user':user(r)})

@require
async def getuser(req):
 c=db();r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 return out({'user':user(r)} if r else {'error':'not found'},200 if r else 404)
@require
async def createws(req):
 b=await body(req); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'):return out({'error':'invalid'},400)
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],req['user']['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO wmembers VALUES(?,?,?)',(wid,req['user']['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?)',(cid,req['user']['id']));c.commit()
 except sqlite3.IntegrityError:c.rollback();c.close();return out({'error':'duplicate'},409)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return out({'workspace':dict(w),'general_channel':chan(ch)},201)
@require
async def workspaces(req):
 c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN wmembers m ON m.workspace_id=w.id WHERE m.user_id=?',(req['user']['id'],)).fetchall();c.close();return out({'workspaces':[dict(x) for x in rows]})
@require
async def workspace(req):
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return out({'error':'not found'},404)
 rows=c.execute('SELECT c.* FROM channels c JOIN members m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?',(w['id'],req['user']['id'])).fetchall();c.close();return out({'workspace':dict(w),'channels':[chan(x) for x in rows],'read_state':[]})
@require
async def createchan(req):
 b=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return out({'error':'not found'},404)
 if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')):c.close();return out({'error':'invalid'},400)
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],b.get('name'))).fetchone():c.close();return out({'error':'duplicate'},409)
 try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?)',(cid,req['user']['id']));c.commit()
 except sqlite3.IntegrityError:c.close();return out({'error':'duplicate'},409)
 r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return out({'channel':chan(r)},201)
@require
async def getchannel(req):
 c=db(); r=c.execute("SELECT * FROM channels WHERE id=?",(req.match_info["id"],)).fetchone()
 if not r:c.close();return out({"error":"not found"},404)
 c.close();return out({"channel":chan(r)})
@require
async def patchchannel(req):
 c=db(); r=c.execute("SELECT * FROM channels WHERE id=?",(req.match_info["id"],)).fetchone()
 if not r:c.close();return out({"error":"not found"},404)
 if not member(c,r["id"],req["user"]["id"]):c.close();return out({"error":"forbidden"},403)
 b=await body(req)
 if not isinstance(b.get("topic"),str) or len(b["topic"])>250:c.close();return out({"error":"invalid"},400)
 c.execute("BEGIN IMMEDIATE");c.execute("UPDATE channels SET topic=? WHERE id=?",(b["topic"],r["id"]));r=c.execute("SELECT * FROM channels WHERE id=?",(r["id"],)).fetchone();event(c,r["id"],"channel.updated",channel=chan(r));c.commit();c.close();return out({"channel":chan(r)})
@require
async def join(req):
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({'error':'not found'},404)
 c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(r['id'],req['user']['id']));c.execute('INSERT OR IGNORE INTO wmembers(workspace_id,user_id) VALUES(?,?)',(r['workspace_id'],req['user']['id']));c.commit();c.close();return out({'channel':chan(r)})
@require
async def members(req):
 c=db();r=c.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(req.match_info['id'],)).fetchall();c.close();return out({'members':[user(x) for x in r]})
def member(c,cid,uid):return c.execute('SELECT 1 FROM members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone()
@require
async def messages(req):
 c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not ch:c.close();return out({'error':'not found'},404)
 if not member(c,ch['id'],req['user']['id']):
  if ch['is_dm']:c.close();return out({'error':'forbidden'},403)
  c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(ch['id'],req['user']['id']))
 b=await body(req); text=b.get('body','')
 if not isinstance(text,str) or not text.strip():c.close();return out({'error':'blank'},400)
 if ch['is_archived'] and not text.startswith('/unarchive'):c.close();return out({'error':'archived'},423)
 if text.startswith('/') and not text.startswith('//'):
  p=text.split(' ',1);cmd=p[0];arg=p[1] if len(p)>1 else ''
  if cmd=='/me':
   if not arg.strip():c.close();return out({'error':'invalid'},400)
  elif cmd=='/shrug':text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd=='/topic':
   if not arg.strip():c.close();return out({'error':'invalid'},400)
   c.execute('BEGIN IMMEDIATE');c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));ch=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();event(c,ch['id'],'channel.updated',channel=chan(ch));c.commit();c.close();return out({'message':None,'channel':chan(ch)},201)
  elif cmd=='/invite':
   if not re.fullmatch(r'@[A-Za-z0-9_]+',arg.strip()):c.close();return out({'error':'invalid'},400)
   u=c.execute('SELECT * FROM users WHERE username=?',(arg.strip()[1:],)).fetchone()
   if not u:c.close();return out({'error':'not found'},404)
   c.execute('BEGIN IMMEDIATE');c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(ch['id'],u['id']));c.execute('INSERT OR IGNORE INTO wmembers(workspace_id,user_id) VALUES(?,?)',(ch['workspace_id'],u['id']));event(c,ch['id'],'member.joined',user_id=u['id']);c.commit();c.close();return out({'message':None,'invited_user':user(u)},201)
  elif cmd in ['/archive','/unarchive']:
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if not w or w['owner_id']!=req['user']['id']:c.close();return out({'error':'forbidden'},403)
   archived=cmd=='/archive';c.execute('BEGIN IMMEDIATE');c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(archived),ch['id']));ch=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();event(c,ch['id'],'channel.archived' if archived else 'channel.unarchived',channel=chan(ch));c.commit();c.close();return out({'message':None,'channel':chan(ch)},201)
  else:c.close();return out({'error':'unknown'},400)
 fids=b.get('file_ids') or []
 if not isinstance(fids,list):c.close();return out({'error':'invalid files'},400)
 for fid in fids:
  f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=req['user']['id'] or f['attached']:c.close();return out({'error':'invalid file'},400)
 c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],req['user']['id'],text,b.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 for fid in fids:c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid));c.execute('UPDATE files SET attached=1 WHERE id=?',(fid,))
 r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();mx=msg(c,r);event(c,ch['id'],'message.reply' if b.get('parent_id') else 'message.created',message=mx);c.commit();c.close();return out({'message':mx},201)

@require
async def listmsg(req):
 c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not ch:c.close();return out({'error':'not found'},404)
 if ch['is_dm'] and not member(c,ch['id'],req['user']['id']):c.close();return out({'error':'forbidden'},403)
 rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC',(req.match_info['id'],)).fetchall();a=[msg(c,r) for r in rs];c.close();return out({'messages':a,'next_cursor':None})
@require
async def replies(req):
 c=db();rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(req.match_info['id'],)).fetchall();a=[msg(c,r) for r in rs];c.close();return out({'replies':a,'next_cursor':None})
@require
async def react(req):
 c=db(); r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({'error':'not found'},404)
 if r['is_private'] and not member(c,r['channel_id'],req['user']['id']):c.close();return out({'error':'forbidden'},403)
 b=await body(req); emoji=b.get('emoji')
 if not isinstance(emoji,str) or not emoji or len(emoji)>64:c.close();return out({'error':'invalid emoji'},400)
 exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],req['user']['id'],emoji)).fetchone()
 if not exists:
  c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO reactions VALUES(?,?,?)',(r['id'],req['user']['id'],emoji)); event(c,r['channel_id'],'reaction.added',message_id=r['id'],emoji=emoji,user_id=req['user']['id']); c.commit()
 x=msg(c,r); c.close(); return out({'reactions':x['reactions']})

@require
async def unreact(req):
 c=db(); r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({'error':'not found'},404)
 if r['is_private'] and not member(c,r['channel_id'],req['user']['id']):c.close();return out({'error':'forbidden'},403)
 emoji=req.match_info['emoji']
 if not emoji or len(emoji)>64:c.close();return out({'error':'invalid emoji'},400)
 exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],req['user']['id'],emoji)).fetchone()
 if exists:
  c.execute('BEGIN IMMEDIATE'); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],req['user']['id'],emoji)); event(c,r['channel_id'],'reaction.removed',message_id=r['id'],emoji=emoji,user_id=req['user']['id']); c.commit()
 x=msg(c,r); c.close(); return out({'reactions':x['reactions']})

@require
async def edit(req):
 c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({'error':'not found'},404)
 if r['deleted']:c.close();return out({'error':'deleted'},409)
 if r['author_id']!=req['user']['id']:c.close();return out({'error':'forbidden'},403)
 b=await body(req)
 if not isinstance(b.get('body'),str) or not b['body'].strip():c.close();return out({'error':'blank'},400)
 c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),r['id']));r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();x=msg(c,r);event(c,r['channel_id'],'message.updated',message=x);c.commit();c.close();return out({'message':x})

@require
async def delete(req):
 c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({'error':'not found'},404)
 if r['deleted']:c.close();return out({'deleted':True})
 w=c.execute('SELECT w.owner_id FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(r['channel_id'],)).fetchone()
 if r['author_id']!=req['user']['id'] and (not w or w['owner_id']!=req['user']['id']):c.close();return out({'error':'forbidden'},403)
 c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));event(c,r['channel_id'],'message.deleted',message_id=r['id']);c.commit();c.close();return out({'deleted':True})

@require
async def dms(req):
 b=await body(req)
 if 'user_id' not in b or b.get('user_id') in (None,''):return out({'error':'user_id required'},400)
 c=db();o=c.execute('SELECT * FROM users WHERE id=?',(b['user_id'],)).fetchone()
 if not o:c.close();return out({'error':'not found'},404)
 ids=sorted([req['user']['id'],o['id']]);name='dm-'+ '-'.join(map(str,ids));r=c.execute('SELECT * FROM channels WHERE name=? AND is_dm=1',(name,)).fetchone()
 if not r:
  c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,name));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?),(?,?)',(cid,ids[0],cid,ids[1]));c.commit()
 else:cid=r['id']
 c.close();return out({'channel_id':cid})

@require
async def groups(req):
 c=db();w=workspace_for(c,req.match_info['slug'])
 if not w:c.close();return out({'error':'not found'},404)
 if not ws_member(c,w['id'],req['user']['id']):c.close();return out({'error':'forbidden'},403)
 rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall();a=[groupjson(c,x) for x in rs];c.close();return out({'groups':a})
@require
async def creategroup(req):
 c=db();w=workspace_for(c,req.match_info['slug'])
 if not w:c.close();return out({'error':'not found'},404)
 if not ws_member(c,w['id'],req['user']['id']):c.close();return out({'error':'forbidden'},403)
 b=await body(req); h=b.get('handle',''); name=b.get('name')
 if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{1,32}',h) or not isinstance(name,str) or not name:c.close();return out({'error':'invalid'},400)
 if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():c.close();return out({'error':'duplicate'},409)
 uids=b.get('user_ids',[])
 if any(not ws_member(c,w['id'],x) for x in uids):c.close();return out({'error':'invalid member'},400)
 c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,req['user']['id']));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 for uid in uids:c.execute('INSERT INTO groupmembers VALUES(?,?)',(gid,uid))
 c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone();x=groupjson(c,g);c.close();return out({'group':x},201)
@require
async def updategroup(req):
 c=db();w=workspace_for(c,req.match_info['slug'])
 if not w:c.close();return out({'error':'not found'},404)
 g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
 if not g:c.close();return out({'error':'not found'},404)
 if req['user']['id']!=g['creator_id'] and req['user']['id']!=w['owner_id']:c.close();return out({'error':'forbidden'},403)
 b=await body(req)
 if 'name' in b:
  if not isinstance(b['name'],str) or not b['name']:c.close();return out({'error':'invalid'},400)
  c.execute('UPDATE groups_ SET name=? WHERE id=?',(b['name'],g['id']))
 if 'user_ids' in b:
  if any(not ws_member(c,w['id'],x) for x in b['user_ids']):c.close();return out({'error':'invalid member'},400)
  c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],))
  for uid in b['user_ids']:c.execute('INSERT INTO groupmembers VALUES(?,?)',(g['id'],uid))
 c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone();x=groupjson(c,g);c.close();return out({'group':x})
@require
async def deletegroup(req):
 c=db();w=workspace_for(c,req.match_info['slug'])
 if not w:c.close();return out({'error':'not found'},404)
 g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
 if not g:c.close();return out({'deleted':True})
 if req['user']['id']!=g['creator_id'] and req['user']['id']!=w['owner_id']:c.close();return out({'error':'forbidden'},403)
 c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();c.close();return out({'deleted':True})
@require
async def upload(req):
 rd=await req.multipart();part=await rd.next()
 if not part or part.name!='file':return out({'error':'file required'},400)
 data=await part.read(decode=False)
 if len(data)>10485760:return out({'error':'too large'},413)
 fid=secrets.randbelow(10**9);path=f'{FILES}/{fid}';open(path,'wb').write(data);c=db();c.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,0)',(fid,req['user']['id'],part.filename,part.headers.get('Content-Type','application/octet-stream'),len(data),now(),path));c.commit();r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone();c.close();return out({'file':dict(r)},201)
@require
async def getfile(req):
 c=db();r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close();return out({'file':dict(r)} if r else {'error':'not found'},200 if r else 404)
@require
async def download(req):
 c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not r:return out({'error':'not found'},404)
 return web.Response(body=open(r['path'],'rb').read(),content_type=r['content_type'],headers={'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
async def ws(req):
 t=req.query.get('token','')
 if t.startswith('Bearer '): t=t[7:]
 c=db(); ok=c.execute('SELECT 1 FROM tokens WHERE token=?',(t,)).fetchone(); c.close()
 if not ok: return web.Response(status=401,text='unauthorized',headers={'X-Token-Len':str(len(t)),'X-Token-Found':'0'})
 ws=web.WebSocketResponse()
 await ws.prepare(req);req.app['sockets'].append(ws)
 c=db(); ur=c.execute('SELECT user_id FROM tokens WHERE token=?',(t,)).fetchone();
 if ur: c.execute('INSERT OR IGNORE INTO presence VALUES(?,?)',(ur['user_id'],int(req.app['node']))); c.commit()
 ws._huddle_uid=ur['user_id'] if ur else None
 try:
  async for m in ws:
   if m.type==WSMsgType.TEXT:
    b=json.loads(m.data);cid=b.get('channel_id');c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
    if b['type']=='subscribe':
     req.app['subs'].setdefault(id(ws),{})[int(cid)]=head; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif b['type']=='resume':
     for x in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,b.get('since_event_id',0))):await ws.send_str(x['frame'])
     await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
    c.close()
 finally:
  if ws in req.app['sockets']:req.app['sockets'].remove(ws)
  req.app['subs'].pop(id(ws),None)
  c=db();
  if getattr(ws,'_huddle_uid',None): c.execute('DELETE FROM presence WHERE user_id=? AND node_id=?',(ws._huddle_uid,int(req.app['node']))); c.commit()
  c.close()
 return ws
async def start_pump(app):
 c=db(); app['seen']=c.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]; c.close()
 app['pump']=asyncio.create_task(pump(app))
async def pump(app):
 seen=app['seen']
 while True:
  try:
   c=db(); rows=c.execute('SELECT frame FROM events WHERE rowid>? ORDER BY rowid',(seen,)).fetchall()
   for r in rows:
    seen+=1; f=json.loads(r['frame'])
    for w in list(app['sockets']):
     fc=int(f.get('channel_id',-1)); baseline=app['subs'].get(id(w),{}).get(fc)
     if f.get('type')=='user.updated':
      try: await w.send_str(r['frame'])
      except: pass
      continue
     if baseline is None or int(f.get('event_id',0)) <= baseline: continue
     try: await w.send_str(r['frame'])
     except: pass
   c.close()
  except Exception: pass
  await asyncio.sleep(.05)
async def root(req): return web.FileResponse('/app/static/index.html')
def app(node):
 init();a=web.Application(client_max_size=11*1024*1024);a['node']=node;a['sockets']=[];a['subs']={};a['seen']=0
 a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me)
 a.router.add_get('/api/users/{id}',getuser);a.router.add_get('/api/workspaces/{slug}/groups',groups);a.router.add_post('/api/workspaces/{slug}/groups',creategroup);a.router.add_patch('/api/workspaces/{slug}/groups/{handle}',updategroup);a.router.add_delete('/api/workspaces/{slug}/groups/{handle}',deletegroup);a.router.add_patch('/api/users/me',profile);a.router.add_post('/api/workspaces',createws);a.router.add_get('/api/workspaces',workspaces);a.router.add_get('/api/workspaces/{slug}',workspace);a.router.add_post('/api/workspaces/{slug}/channels',createchan)
 a.router.add_post('/api/channels/{id}/join',join);a.router.add_get('/api/channels/{id}',getchannel);a.router.add_patch('/api/channels/{id}',patchchannel);a.router.add_get('/api/channels/{id}/members',members);a.router.add_post('/api/channels/{id}/messages',messages);a.router.add_get('/api/channels/{id}/messages',listmsg);a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_post('/api/messages/{id}/reactions',react);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);a.router.add_patch('/api/messages/{id}',edit);a.router.add_delete('/api/messages/{id}',delete);a.router.add_post('/api/dms',dms);a.router.add_post('/api/files',upload);a.router.add_get('/api/files/{id}',getfile);a.router.add_get('/api/files/{id}/download',download);a.router.add_get('/api/ws',ws);a.router.add_get('/',root);a.on_startup.append(start_pump);return a

IRC_CLIENTS=set()
async def irc_event_pump():
 c=db(); seen=c.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]; c.close()
 while True:
  try:
   c=db(); rows=c.execute('SELECT rowid,frame FROM events WHERE rowid>? ORDER BY rowid',(seen,)).fetchall()
   for row in rows:
    seen=row['rowid']; f=json.loads(row['frame'])
    if f.get('type') not in ('message.created','message.reply'): continue
    m=f.get('message') or {}; cid=f.get('channel_id')
    ch=c.execute('SELECT ch.name,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
    if not ch: continue
    target='#'+ch['slug']+'/'+ch['name']; author=(m.get('author') or {}).get('username','huddle'); text=m.get('body','')
    for w in list(IRC_CLIENTS):
     if cid not in getattr(w,'_huddle_joined',set()): continue
     await irc_send(w,':'+author+'!'+str((m.get('author') or {}).get('id','user'))+'@huddle PRIVMSG '+target+' :'+text)
   c.close()
  except Exception: pass
  await asyncio.sleep(.05)

async def irc_send(w,line):
 try: w.write((line+'\r\n').encode()); await w.drain()
 except: pass
async def irc_gateway(port=6667):
 async def client(reader,writer):
  IRC_CLIENTS.add(writer); nick=None; uid=None; token=None; joined=set(); writer._huddle_joined=joined; registered=False
  async def reply(line): await irc_send(writer,line)
  try:
   while not reader.at_eof():
    raw=await reader.readline()
    if not raw: break
    line=raw.decode(errors='replace').strip('\r\n')
    if not line: continue
    parts=line.split(' ',1); cmd=parts[0].upper(); rest=parts[1] if len(parts)>1 else ''
    args=rest.split(' :',1); words=args[0].split(); trailing=args[1] if len(args)>1 else ''
    if cmd=='PASS': token=rest.strip()
    elif cmd=='NICK':
     nn=words[0] if words else ''
     if nn and any(getattr(x,'_huddle_nick',None)==nn and x is not writer for x in IRC_CLIENTS):
      await reply(':huddle 433 * '+nn+' :Nickname is already in use'); continue
     nick=nn
    elif cmd=='USER':
     c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token or '',)).fetchone(); c.close()
     if not r: await reply(':huddle 464 * :Password incorrect'); continue
     uid=r['id']; writer._huddle_nick=nick; writer._huddle_uid=uid; registered=True
     for n,msgx in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle oiwt'),('005','CHANTYPES=# PREFIX=(o)+')]:
      await reply(':huddle '+n+' '+(nick or '*')+' :'+msgx)
     await reply(':huddle 422 '+(nick or '*')+' :MOTD File is missing')
    elif cmd=='PING': await reply(':huddle PONG huddle :'+(trailing or (words[0] if words else '')))
    elif cmd=='PONG': pass
    elif cmd=='QUIT': break
    elif not registered:
     if cmd not in ('PASS','NICK','USER'): await reply(':huddle 451 * :You have not registered')
    elif cmd in ('JOIN','NAMES','WHO','LIST','TOPIC','MODE'):
     target=(words[0] if words else trailing).lstrip(':')
     if cmd=='LIST':
      await reply(':huddle 321 '+nick+' Channel :Users Name'); continue
     if not target.startswith('#') or '/' not in target:
      if cmd in ('JOIN','NAMES','WHO','TOPIC'): await reply(':huddle 403 '+nick+' '+target+' :No such channel')
      continue
     slug,chname=target[1:].split('/',1); c=db(); ch=c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,chname)).fetchone()
     if not ch:
      c.close(); await reply(':huddle 403 '+nick+' '+target+' :No such channel'); continue
     if cmd=='JOIN':
      c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(ch['id'],uid));c.commit();joined.add(ch['id']); writer._huddle_joined=joined
      names=' '.join((x['nick'] if 'nick' in x.keys() else '') for x in [])
      users=[c.execute('SELECT username FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall()]
      nicks=' '.join(x[0] for x in c.execute('SELECT u.username FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)))
      if nick not in nicks.split(): nicks=(nick+' '+nicks).strip()
      await reply(':'+nick+'!'+str(uid)+'@localhost JOIN '+target)
      await reply(':huddle 353 '+nick+' = '+target+' :'+nicks)
      await reply(':huddle 366 '+nick+' '+target+' :End of NAMES list')
     elif cmd=='NAMES':
      nicks=' '.join(x[0] for x in c.execute('SELECT u.username FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)))
      await reply(':huddle 353 '+nick+' = '+target+' :'+nicks); await reply(':huddle 366 '+nick+' '+target+' :End of NAMES list')
     elif cmd=='WHO':
      await reply(':huddle 352 '+nick+' '+target+' localhost huddle '+nick+' H :0 '+nick); await reply(':huddle 315 '+nick+' '+target+' :End of WHO list')
     c.close()
    elif cmd=='PRIVMSG':
     target=words[0] if words else ''; text=trailing
     if not target.startswith('#') or '/' not in target: await reply(':huddle 401 '+nick+' '+target+' :No such nick'); continue
     slug,chname=target[1:].split('/',1); c=db();ch=c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,chname)).fetchone()
     if not ch or ch['id'] not in joined: c.close();await reply(':huddle 404 '+nick+' '+target+' :Cannot send to channel');continue
     c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],uid,text,now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; rr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); event(c,ch['id'],'message.created',message=msg(c,rr)); c.commit();c.close()
    else: await reply(':huddle 421 '+nick+' '+cmd+' :Unknown command')
  except Exception: pass
  finally:
   IRC_CLIENTS.discard(writer)
   try: writer.close(); await writer.wait_closed()
   except: pass
 server=await asyncio.start_server(client,'0.0.0.0',port)
 asyncio.create_task(irc_event_pump())
 async with server: await server.serve_forever()

if __name__=='__main__':
 if sys.argv[1]=='node':web.run_app(app(sys.argv[2]),host='127.0.0.1',port=int(sys.argv[3]))
 elif sys.argv[1]=='irc':asyncio.run(irc_gateway(int(sys.argv[2])))

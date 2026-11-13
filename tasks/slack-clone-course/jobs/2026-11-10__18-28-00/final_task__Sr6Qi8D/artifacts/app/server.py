import asyncio, json, os, re, sys, sqlite3, secrets, hashlib, mimetypes, urllib.parse
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from aiohttp import web, WSMsgType
DB='/app/data/app.db'; os.makedirs('/app/data/files',exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,frame TEXT);
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER,channel_id INTEGER,seen_at REAL,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0,target_id INTEGER);
''')
 try:
  cols=[x['name'] for x in c.execute('PRAGMA table_info(ws_presence)').fetchall()]
  if 'seen_at' not in cols:
   c.execute('ALTER TABLE ws_presence ADD COLUMN seen_at REAL')
   c.execute('UPDATE ws_presence SET seen_at=?',(asyncio.get_event_loop().time(),))
 except Exception: pass
 c.commit(); c.close()
def user(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def ch(r):
 return {k:(bool(r[k]) if k in ['is_private','is_dm','is_archived'] else r[k]) for k in ['id','workspace_id','name','is_private','is_dm','topic','is_archived']}
def auth(req):
 h=req.headers.get('Authorization',''); t=h[7:] if h.startswith('Bearer ') else ''
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def out(status,obj): return web.json_response(obj,status=status)
async def body(req):
 try:return await req.json()
 except:return {}
def emit(c,cid,typ,extra):
 n=(c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0])
 f={'type':typ,'event_id':n,'channel_id':cid}; f.update(extra); c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(f))); return f
def msg(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 reacts=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(r['id'],)):
  reacts.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
 mentions=[]; text=r['body']
 chrow=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
 if chrow:
  ids=[x['id'] for x in c.execute('SELECT u.id FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(r['channel_id'],))]
  for x in c.execute('SELECT id,username FROM users WHERE id IN (%s)'%(','.join('?'*len(ids)) if ids else '0'),ids):
   if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(x['username'])+r'(?![A-Za-z0-9_])',text) and x['id']!=r['author_id']: mentions.append(x['id'])
  wh=c.execute('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
  for g in c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=?',(wh['workspace_id'],)):
   # group handle lookup is checked separately below
   pass
  for g in c.execute('SELECT handle,user_ids FROM groups_ WHERE workspace_id=?',(wh['workspace_id'],)):
   if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(g['handle'])+r'(?![A-Za-z0-9_])',text):
    mentions += [int(z) for z in json.loads(g['user_ids'] or '[]') if int(z) in ids and int(z)!=r['author_id']]
  if re.search(r'(?<![A-Za-z0-9_])@channel(?![A-Za-z0-9_])',text):
   mentions += [z for z in ids if z!=r['author_id']]
  if re.search(r'(?<![A-Za-z0-9_])@here(?![A-Za-z0-9_])',text):
   online={x['user_id'] for x in c.execute('SELECT user_id FROM ws_presence WHERE channel_id IN (?,0)',(r['channel_id'],))}
   mentions += [z for z in ids if z!=r['author_id'] and z in online]
 mentions=list(dict.fromkeys(mentions))
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],)).fetchall()],'reactions':reacts,'mentions':mentions}
def member(c,uid,cid):
 r=c.execute('SELECT 1 FROM cmembers WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone(); return bool(r)
def workspace(c,cid): return c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
async def guard(req):
 u=auth(req)
 return u
async def health(req): return out(200,{'status':'ok','node_id':int(req.app['node'])})
async def register(req):
 d=await body(req); un=d.get('username',''); pw=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return out(400,{'error':'invalid'})
 c=db()
 try:
  c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return out(201,{'user':user(r),'token':t})
 except sqlite3.IntegrityError:return out(409,{'error':'duplicate'})
 finally:c.close()
async def login(req):
 d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest())).fetchone()
 if not r:c.close(); return out(401,{'error':'invalid'})
 t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return out(200,{'user':user(r),'token':t})
async def me(req): u=auth(req); return out(401,{'error':'auth'}) if u is None else out(200,{'user':user(u)})
async def profile(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 d=await body(req); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals={k:d[k] for k in allowed if k in d}
 if 'timezone' in vals:
  try: ZoneInfo(vals['timezone'])
  except Exception: return out(400,{'error':'timezone'})
 limits={'display_name':80,'timezone':64,'avatar_url':1024,'status_text':140,'status_emoji':64}
 if any(not isinstance(v,str) or len(v)>limits[k] or (k=='display_name' and len(v)<1) for k,v in vals.items()): return out(400,{'error':'field length'})
 c=db(); c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),u['id']));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps({'type':'user.updated','user':user(r)}),));c.commit();c.close();return out(200,{'user':user(r)})
async def users(req):
 u=auth(req); c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 return out(401,{'error':'auth'}) if not u else (out(404,{'error':'not found'}) if not r else out(200,{'user':user(r)}))
async def create_ws(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 d=await body(req); slug=d.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return out(400,{'error':'slug'})
 c=db()
 try:
  c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d.get('name',slug),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();q=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return out(201,{'workspace':dict(w),'general_channel':ch(q)})
 except sqlite3.IntegrityError:return out(409,{'error':'duplicate'})
 finally:c.close()
async def list_ws(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db(); rs=c.execute('SELECT DISTINCT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return out(200,{'workspaces':[dict(x) for x in rs]})
async def get_ws(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return out(404,{'error':'not found'})
 inc=req.query.get('include_archived') in ('1','true'); sql='SELECT c.* FROM channels c JOIN cmembers m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?'+('' if inc else ' AND c.is_archived=0'); rs=c.execute(sql,(w['id'],u['id'])).fetchall();states=[]
 for x in rs:
  q=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],x['id'])).fetchone(); last=q['last_read'] if q else 0; h=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM events WHERE channel_id=?',(x['id'],)).fetchone()['h']; un=c.execute("SELECT COUNT(*) FROM events WHERE channel_id=? AND event_id>? AND json_extract(frame,'$.type')='message.created' AND json_extract(frame,'$.message.author_id')!=?",(x['id'],last,u['id'])).fetchone()[0]; states.append({'channel_id':x['id'],'last_read_event_id':last,'unread_count':un,'mention_count':0})
 c.close();return out(200,{'workspace':dict(w),'channels':[ch(x) for x in rs],'read_state':states})
async def create_channel(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 d=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone();m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
 if not w:c.close();return out(404,{'error':'not found'})
 if not m or m['role']=='guest' or (d.get('is_private') and m['role'] not in ('owner','admin')):c.close();return out(403,{'error':'forbidden'})
 if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')) or len(d.get('topic',''))>250:c.close();return out(400,{'error':'invalid'})
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],d.get('name'))).fetchone():c.close();return out(409,{'error':'duplicate'})
 try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return out(201,{'channel':ch(r)})
 except sqlite3.IntegrityError:return out(409,{'error':'duplicate'})
 finally:c.close()
async def join(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT c.*,w.join_mode FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 wm=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(r['workspace_id'],u['id'])).fetchone()
 if wm and wm['role']=='guest':c.close();return out(403,{'error':'guest'})
 if r['is_private'] and not member(c,u['id'],r['id']):c.close();return out(403,{'error':'private'})
 if not wm and r['join_mode']=='invite_only':c.close();return out(403,{'error':'invite_only'})
 was=member(c,u['id'],r['id']);c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(r['id'],u['id']));
 if not was: emit(c,r['id'],'member.joined',{'user_id':u['id']})
 c.commit();c.close();return out(200,{'channel':ch(r)})
async def messages(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 cid=int(req.match_info['id']);c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 d=None; b=''
 if req.method=='GET':
  if not member(c,u['id'],cid):c.close();return out(403,{'error':'forbidden'})
 else:
  d=await body(req); b=d.get('body','')
  if r['is_archived'] and b != '/unarchive':c.close();return out(423,{'error':'archived'})
  if not member(c,u['id'],cid):c.close();return out(403,{'error':'forbidden'})
 if req.method=='GET':
  rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT 200',(cid,)).fetchall();a=[msg(c,x) for x in rows];c.close();return out(200,{'messages':a,'next_cursor':None})
 d=await body(req);b=d.get('body','')
 if not b.strip():c.close();return out(400,{'error':'empty'})
 if r['is_archived']:c.close();return out(423,{'error':'archived'})
 c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],b,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();fr=msg(c,mr);emit(c,cid,'message.reply' if d.get('parent_id') else 'message.created',{'message':fr});c.commit();c.close();return out(201,{'message':fr})
async def replies(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();p=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not p:c.close();return out(404,{'error':'not found'})
 if not member(c,u['id'],p['channel_id']):c.close();return out(403,{'error':'forbidden'})
 try: lim=int(req.query.get('limit','50'))
 except: c.close();return out(400,{'error':'limit'})
 if lim<1 or lim>200:c.close();return out(400,{'error':'limit'})
 cur=req.query.get('cursor'); before=None
 if cur:
  try: before=int(cur)
  except: c.close();return out(400,{'error':'cursor'})
 sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0 '+('AND id>? ' if before else '')+'ORDER BY id LIMIT ?'
 rows=c.execute(sql,(p['id'],before,lim+1) if before else (p['id'],lim+1)).fetchall();more=len(rows)>lim;rows=rows[:lim];a=[msg(c,x) for x in rows];nxt=str(rows[-1]['id']) if more and rows else None;c.close();return out(200,{'replies':a,'next_cursor':nxt})
async def message_edit(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 if req.method=='DELETE':
  if r['deleted']:c.close();return out(200,{'deleted':True})
  owner=c.execute('SELECT w.owner_id FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(r['channel_id'],)).fetchone()
  if r['author_id']!=u['id'] and (not owner or owner['owner_id']!=u['id']):c.close();return out(403,{'error':'forbidden'})
  c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));emit(c,r['channel_id'],'message.deleted',{'message_id':r['id']});c.commit();c.close();return out(200,{'deleted':True})
 if r['deleted']:c.close();return out(409,{'error':'deleted'})
 if r['author_id']!=u['id']:c.close();return out(403,{'error':'forbidden'})
 d=await body(req);b=d.get('body','')
 if not isinstance(b,str) or not b.strip():c.close();return out(400,{'error':'empty'})
 c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b,now(),r['id']));r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();fr=msg(c,r);emit(c,r['channel_id'],'message.updated',{'message':fr});c.commit();c.close();return out(200,{'message':fr})
async def reactions(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r or r['deleted']:c.close();return out(404,{'error':'not found'})
 if not member(c,u['id'],r['channel_id']):c.close();return out(403,{'error':'forbidden'})
 emoji=req.match_info.get('emoji')
 if req.method=='POST':
  d=await body(req);emoji=d.get('emoji','')
  if not emoji or len(emoji)>64:c.close();return out(400,{'error':'emoji'})
  old=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji)).fetchone()
  if not old:
   c.execute('INSERT INTO reactions VALUES(?,?,?,?)',(r['id'],u['id'],emoji,now()));emit(c,r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':emoji,'user_id':u['id']});c.commit()
 else:
  if emoji:
   existed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji)).fetchone()
   if existed:
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji))
    emit(c,r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':emoji,'user_id':u['id']})
   c.commit()
 a=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(r['id'],)):
  a.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
 c.close();return out(200,{'reactions':a})
async def pins(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();cid=int(req.match_info['id']);r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 if not member(c,u['id'],cid):c.close();return out(403,{'error':'forbidden'})
 if req.method=='GET':
  a=[]
  for x in c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)):
   a.append({'message':msg(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']})
  c.close();return out(200,{'pins':a,'next_cursor':None})
 mid=int(req.match_info['id']); x=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not x:c.close();return out(404,{'error':'not found'})
 c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now()));c.commit();z=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone();c.close();return out(200,{'pin':dict(z)})
async def pin_delete(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();c.execute('DELETE FROM pins WHERE message_id=?',(req.match_info['id'],));c.commit();c.close();return out(200,{'unpinned':True})
async def channel_members(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT id FROM channels WHERE id=?',(req.match_info['id'],)).fetchone();
 if not r:c.close();return out(404,{'error':'not found'})
 rs=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(req.match_info['id'],)).fetchall();c.close();return out(200,{'members':[user(x) for x in rs]})
async def search(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 q='%'+req.query.get('q','')+'%';c=db()
 try: lim=int(req.query.get('limit','50'))
 except: c.close();return out(400,{'error':'limit'})
 if lim<1 or lim>200:c.close();return out(400,{'error':'limit'})
 cur=req.query.get('cursor');before=None
 if cur:
  try: before=int(cur)
  except: c.close();return out(400,{'error':'cursor'})
 extra='';args=[u['id'],q]
 if req.query.get('workspace_id'):
  extra+=' AND ch.workspace_id=?';args.append(int(req.query['workspace_id']))
 if before: extra+=' AND m.id<?';args.append(before)
 args.append(lim+1)
 rs=c.execute('SELECT m.* FROM messages m JOIN cmembers cm ON cm.channel_id=m.channel_id JOIN channels ch ON ch.id=m.channel_id WHERE cm.user_id=? AND m.body LIKE ? AND m.deleted=0'+extra+' ORDER BY m.id DESC LIMIT ?',args).fetchall();more=len(rs)>lim;rs=rs[:lim];a=[msg(c,x) for x in rs];nxt=str(rs[-1]['id']) if more and rs else None;c.close();return out(200,{'results':a,'next_cursor':nxt})
async def dms(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 d=await body(req)
 if 'user_id' not in d or not isinstance(d.get('user_id'),(int,str)) or str(d.get('user_id'))=='':
  return out(400,{'error':'user_id'})
 try:v=int(d.get('user_id'))
 except: return out(400,{'error':'user_id'})
 c=db(); target=c.execute('SELECT id FROM users WHERE id=?',(v,)).fetchone()
 if not target:c.close();return out(404,{'error':'user'})
 pair=c.execute('SELECT c.id FROM channels c JOIN cmembers a ON a.channel_id=c.id JOIN cmembers b ON b.channel_id=c.id WHERE c.is_dm=1 AND a.user_id=? AND b.user_id=?',(u['id'],v)).fetchone()
 if pair:c.close();return out(200,{'channel_id':pair['id']})
 c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,'dm-'+str(u['id'])+'-'+str(v)));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,v));c.commit();c.close();return out(200,{'channel_id':cid})
async def channel_patch(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 owner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
 if not member(c,u['id'],r['id']) and (not owner or owner['owner_id']!=u['id']):c.close();return out(403,{'error':'forbidden'})
 d=await body(req)
 if len(d.get('topic',''))>250:c.close();return out(400,{'error':'topic'})
 c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',r['topic']),r['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(r['id'],)).fetchone();emit(c,r['id'],'channel.updated',{'channel':ch(r)});c.commit();c.close();return out(200,{'channel':ch(r)})
async def archive(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT c.*,w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 if r['owner_id']!=u['id']:c.close();return out(403,{'error':'forbidden'})
 val=0 if req.path.endswith('unarchive') else 1;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,r['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(r['id'],)).fetchone();emit(c,r['id'],'channel.unarchived' if not val else 'channel.archived',{'channel':ch(r)});c.commit();c.close();return out(200,{'channel':ch(r)})
async def read_state(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();cid=int(req.match_info['id']);
 if not c.execute('SELECT 1 FROM channels WHERE id=?',(cid,)).fetchone():c.close();return out(404,{'error':'not found'})
 x=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
 if req.method=='POST':
  d=await body(req)
  if 'last_read_event_id' not in d or isinstance(d.get('last_read_event_id'),bool) or not isinstance(d.get('last_read_event_id'),int) or d.get('last_read_event_id')<0:
   c.close();return out(400,{'error':'last_read_event_id'})
  head=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM events WHERE channel_id=?',(cid,)).fetchone()['h'];v=min(d['last_read_event_id'],head)
  c.execute('INSERT INTO reads VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read=MAX(last_read,excluded.last_read)',(u['id'],cid,v));c.commit();x=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
 head=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM events WHERE channel_id=?',(cid,)).fetchone()['h']; last=x['last_read'] if x else 0
 unread=c.execute("SELECT COUNT(*) FROM events WHERE channel_id=? AND event_id>? AND json_extract(frame,'$.type')='message.created' AND json_extract(frame,'$.message.author_id')!=?",(cid,last,u['id'])).fetchone()[0]; mention=0
 for mm in c.execute('SELECT m.* FROM messages m JOIN events e ON e.channel_id=m.channel_id AND json_extract(e.frame,\'$.message.id\')=m.id WHERE m.channel_id=? AND m.deleted=0 AND m.author_id!=? AND e.event_id>?',(cid,u['id'],last)):
  if u['username'] and re.search(r'(?<![A-Za-z0-9_])@'+re.escape(u['username'])+r'(?![A-Za-z0-9_])',mm['body']): mention+=1
 c.close();return out(200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mention}})
async def files(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 if req.method=='GET':
  c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close();return out(404,{'error':'not found'}) if not r else out(200,{'file':dict(r)})
 data=await req.post();f=data.get('file')
 if not f:return out(400,{'error':'file'})
 b=f.file.read()
 if len(b)>10*1024*1024:return out(413,{'error':'large'})
 fid=secrets.randbelow(2**31);path='/app/data/files/'+str(fid);open(path,'wb').write(b);c=db();c.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?)',(fid,u['id'],f.filename,f.content_type,len(b),now(),path));c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return out(201,{'file':dict(r)})
async def download(req):
 u=auth(req);c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not u:return out(401,{'error':'auth'})
 if not r:return out(404,{'error':'not found'})
 return web.FileResponse(r['path'],headers={'Content-Disposition':'attachment; filename="'+r['filename']+'"'},content_type=r['content_type'])
async def ws_handler(req):
 t=req.query.get('token','');c=db();r=c.execute('SELECT user_id FROM tokens WHERE token=?',(t,)).fetchone();c.close()
 if not r:return out(401,{'error':'auth'})
 ws=web.WebSocketResponse();await ws.prepare(req);subs={};last={}; c0=db(); g0=c0.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0]; c0.execute('DELETE FROM ws_presence WHERE user_id=?',(r['user_id'],)); c0.execute('INSERT OR REPLACE INTO ws_presence(user_id,channel_id,seen_at) VALUES(?,?,?)',(r['user_id'],0,asyncio.get_event_loop().time())); c0.commit(); c0.close()
 async def pump():
  g_last=g0
  while not ws.closed:
   await asyncio.sleep(.05);c=db()
   grows=c.execute('SELECT id,frame FROM global_events WHERE id>? ORDER BY id',(g_last,)).fetchall()
   for gx in grows: await ws.send_str(gx['frame']); g_last=gx['id']
   for cid in list(subs):
    rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last.get(cid,0))).fetchall()
    for x in rows: await ws.send_str(x['frame']);last[cid]=x['event_id']
   c.close()
 task=asyncio.create_task(pump())
 try:
  async for m in ws:
   if m.type==WSMsgType.TEXT:
    d=json.loads(m.data);cid=int(d.get('channel_id',0))
    if d['type']=='subscribe':
     c=db();chx=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();allowed=bool(chx and c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,r['user_id'])).fetchone())
     if not allowed:c.close();await ws.send_json({'type':'error','error':'forbidden','channel_id':cid});continue
     subs[cid]=1;c.execute('INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)',(r['user_id'],cid,asyncio.get_event_loop().time()));c.commit();h=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM events WHERE channel_id=?',(cid,)).fetchone()['h'];c.close();last[cid]=h;await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
    elif d['type']=='resume':
     c=db();chx=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();allowed=bool(chx and c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,r['user_id'])).fetchone())
     if not allowed:c.close();await ws.send_json({'type':'error','error':'forbidden','channel_id':cid});continue
     subs[cid]=1;c.execute('INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)',(r['user_id'],cid,asyncio.get_event_loop().time()));c.commit();since=int(d.get('since_event_id',0));rows=c.execute('SELECT frame,event_id FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall();h=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM events WHERE channel_id=?',(cid,)).fetchone()['h'];c.close()
     for x in rows:await ws.send_str(x['frame'])
     last[cid]=h;await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':h})
 finally:
  task.cancel(); c=db()
  for cid in subs: c.execute('DELETE FROM ws_presence WHERE user_id=? AND channel_id=?',(r['user_id'],cid))
  c.execute('DELETE FROM ws_presence WHERE user_id=? AND channel_id=0',(r['user_id'],))
  c.commit();c.close()
 return ws
HTML='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>body{margin:0;font:15px Arial;color:#222}[hidden]{display:none!important}.app{display:flex;height:100vh}.side{width:250px;background:#3f0e40;color:#fff;padding:20px}.main{padding:25px;flex:1}button{border:0;padding:9px;color:#fff;border-radius:4px}[data-button-role=primary]{background:#007a5a;color:#fff}[data-button-role=danger]{background:#e01e5a;color:#fff}[data-button-role=secondary]{background:#1264a3;color:#fff}input{padding:9px;margin:5px}</style></head><body>
<div id="auth-modal"><form data-testid="auth-form"><h1>Huddle</h1><input name="username"><input name="password" type="password"><input name="display_name"><button id="auth-submit" data-testid="auth-submit" data-button-role="primary">Sign up</button><button id="auth-toggle" data-testid="auth-toggle" data-button-role="secondary" type="button">Log in</button><p id="auth-error" data-testid="auth-error"></p></form></div>
<div id="create-channel-modal" hidden></div><div id="channel-settings-modal" hidden></div><div id="workspace-settings-modal" hidden></div><form id="create-invitation-form" data-testid="create-invitation-form" hidden></form>
<div id="shell" class="app" hidden><aside class="side" data-testid="sidebar"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user"></div><button id="logout-btn" data-testid="logout-btn" data-button-role="danger">Logout</button><div id="channel-list" data-testid="channel-list"></div><div data-testid="dms-list"></div><button id="new-channel-btn" data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Settings</button></aside><main class="main"><h1 data-testid="channel-title">#general</h1><div data-testid="channel-topic"></div><div id="message-list" data-testid="message-list"><p id="empty-channel" data-testid="empty-channel">There are no messages yet.</p></div><form id="post"><input id="message-input" data-testid="message-input" name="body"><button id="send-btn" data-testid="send-btn" data-button-role="primary">Send</button></form></main><aside id="thread-panel" data-testid="thread-panel" hidden><button data-testid="close-thread" data-button-role="secondary">Close</button><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button></aside></div>
<div><form data-testid="workspace-create-form" hidden><input name="slug" placeholder="workspace-slug"><input name="name" placeholder="Workspace name"><button type="submit" data-testid="workspace-general-submit" data-button-role="primary">Create workspace</button></form><button data-testid="empty-state-create-workspace"></button><form data-testid="join-workspace-form"><button data-testid="join-workspace-submit" data-button-role="primary"></button><span data-testid="join-workspace-error"></span></form><button data-testid="workspace-settings-close" data-button-role="secondary"></button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary"></button><button data-testid="channel-settings-btn" data-button-role="secondary"></button><button data-testid="channel-settings-close" data-button-role="secondary"></button><h2 data-testid="channel-settings-title"></h2><div data-testid="channel-members-list"></div><div data-testid="channel-member-row"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary"></button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary"></button><button data-testid="archive-channel-btn" data-button-role="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary"></button><form data-testid="create-channel-form" hidden><span data-testid="create-channel-error"></span><input name="name" placeholder="channel-name"><input data-testid="create-channel-private"><button type="submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary"></button></form><button data-testid="settings-tab-members"></button><button data-testid="settings-tab-invitations"></button><button data-testid="settings-tab-general"></button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><div data-testid="member-row"></div><span data-testid="role-badge"></span><select data-testid="role-select"></select><button data-testid="create-invitation-btn" data-button-role="secondary"></button><button data-testid="create-invitation-submit" data-button-role="primary"></button><div data-testid="invitations-list"></div><span data-testid="invitation-code"></span></div>
<script>
var t=localStorage.getItem("huddle.token")||"",login=false,me=null,ws=null,cur=null,workspaceSlug="";
function api(path,opt){opt=opt||{};opt.headers=opt.headers||{};if(t)opt.headers.Authorization="Bearer "+t;if(opt.body&&typeof opt.body!=="string"){opt.headers["Content-Type"]="application/json";opt.body=JSON.stringify(opt.body)}return fetch("/api"+path,opt).then(function(r){return r.json().then(function(x){return {s:r.status,d:x}})})}
function show(){document.getElementById("auth-modal").hidden=!!t;document.getElementById("shell").hidden=!t}
function authError(x){var e=document.getElementById("auth-error");e.textContent=x;e.hidden=!x}
function load(){if(!t){show();return}api("/auth/me").then(function(r){if(r.s!==200){t="";localStorage.removeItem("huddle.token");show();return}me=r.d.user;document.querySelector("[data-testid=current-user]").textContent=me.display_name;api("/workspaces").then(function(w){if(!w.d.workspaces.length){document.getElementById("shell").hidden=false;document.querySelector("[data-testid=workspace-header]").textContent="Create a workspace";var f=document.querySelector("[data-testid=workspace-create-form]");f.hidden=false;f.onsubmit=function(e){e.preventDefault();var d=Object.fromEntries(new FormData(f));api("/workspaces",{method:"POST",body:d}).then(function(r){if(r.s>=400){document.querySelector("[data-testid=auth-error]").textContent=r.d.error||"Invalid workspace";}else{f.hidden=true;openws(d.slug)}})};return}openws(w.d.workspaces[0].slug)})})}
function openws(slug,selected){workspaceSlug=slug;api("/workspaces/"+slug).then(function(r){var x=r.d;document.getElementById("auth-modal").hidden=true;document.getElementById("shell").hidden=false;document.querySelector("[data-testid=workspace-header]").textContent=x.workspace.name;cur=x.channels.find(function(c){return selected&&String(c.id)===String(selected)})||x.channels[0];var list=document.querySelector("[data-testid=channel-list]");list.innerHTML=x.channels.map(function(c){return '<div data-testid="channel-entry" data-channel-id="'+c.id+'" data-channel-name="'+c.name+'" class="channel-entry">#'+c.name+"</div>"}).join("");list.querySelectorAll(".channel-entry").forEach(function(e){e.onclick=function(){var id=e.dataset.channelId;api("/channels/"+id+"/messages").then(function(){api("/workspaces/"+workspaceSlug).then(function(z){cur=z.d.channels.find(function(c){return String(c.id)===String(id)})||cur;render()})})}});render()})}
function render(){if(!cur)return;document.querySelector("[data-testid=channel-title]").textContent="#"+cur.name;document.querySelector("[data-testid=channel-topic]").textContent=cur.topic||"";api("/channels/"+cur.id+"/messages").then(function(r){var box=document.querySelector("[data-testid=message-list]"),ms=r.d.messages||[];box.innerHTML=ms.map(function(m){return '<div data-testid="message" data-message-id="'+m.id+'"><span data-testid="message-avatar">●</span> <span data-testid="message-author">'+m.author.display_name+'</span> <span data-testid="message-time">'+m.created_at+'</span> <span data-testid="message-body">'+m.body+'</span>'+(m.edited_at?'<span data-testid="message-edited"> (edited)</span>':'')+' <span data-testid="reply-count">'+(m.reply_count||0)+' replies</span><button type="button" data-testid="open-thread-btn">Thread</button><button type="button" data-testid="reaction-button">React</button><button type="button" data-testid="message-edit-btn">Edit</button><button type="button" data-testid="message-delete-btn">Delete</button><span data-testid="reaction-chip" hidden></span><div data-testid="emoji-picker" hidden>'+["😀","👍","❤️","🎉","🔥","😂","🚀","✅"].map(function(e){return '<button type="button" data-testid="emoji-option">'+e+'</button>'}).join("")+'</div></div>'}).join("")||'<p id="empty-channel" data-testid="empty-channel">There are no messages yet.</p>';box.querySelectorAll("[data-testid=message]").forEach(function(row){var id=row.dataset.messageId;row.querySelector("[data-testid=message-edit-btn]").onclick=function(){var b=row.querySelector("[data-testid=message-body]"),inp=document.createElement("input");inp.setAttribute("data-testid","message-edit-input");inp.value=b.textContent;b.replaceWith(inp);inp.focus();inp.onkeydown=function(e){if(e.key==="Enter"){api("/messages/"+id,{method:"PATCH",body:{body:inp.value}}).then(function(){render()})}}};row.querySelector("[data-testid=message-delete-btn]").onclick=function(){api("/messages/"+id,{method:"DELETE"}).then(render)};row.querySelector("[data-testid=open-thread-btn]").onclick=function(){var tp=document.querySelector("[data-testid=thread-panel]");tp.hidden=false;tp.dataset.parentId=id;api("/messages/"+id+"/replies").then(function(x){var old=tp.querySelector("[data-testid=thread-messages]");if(!old){old=document.createElement("div");old.setAttribute("data-testid","thread-messages");tp.insertBefore(old,tp.firstChild)}old.innerHTML=(x.d.replies||[]).map(function(q){return '<div data-testid="thread-message">'+q.body+'</div>'}).join("")})};row.querySelector("[data-testid=reaction-button]").onclick=function(){row.querySelector("[data-testid=emoji-picker]").hidden=false};row.querySelectorAll("[data-testid=emoji-option]").forEach(function(o){o.onclick=function(){api("/messages/"+id+"/reactions",{method:"POST",body:{emoji:o.textContent}}).then(function(x){var chip=row.querySelector("[data-testid=reaction-chip]");chip.hidden=false;chip.textContent=o.textContent+" "+((x.d.reactions&&x.d.reactions[0]||{}).count||1);row.querySelector("[data-testid=emoji-picker]").hidden=true})}})});})}
document.querySelector("[data-testid=auth-toggle]").onclick=function(){login=!login;document.getElementById("auth-submit").textContent=login?"Log in":"Sign up";authError("")};
document.querySelector("[data-testid=auth-form]").onsubmit=function(e){e.preventDefault();var d=Object.fromEntries(new FormData(e.target));api(login?"/auth/login":"/auth/register",{method:"POST",body:d}).then(function(r){if(r.s>=400){authError(r.d.error||"Invalid input");return}t=r.d.token;localStorage.setItem("huddle.token",t);authError("");load()})};
document.getElementById("logout-btn").onclick=function(){localStorage.removeItem("huddle.token");t="";show()};
document.getElementById("post").onsubmit=function(e){e.preventDefault();var d=Object.fromEntries(new FormData(e.target));if(cur&&d.body.trim())api("/channels/"+cur.id+"/messages",{method:"POST",body:d}).then(render)};
document.getElementById("new-channel-btn").onclick=function(){var f=document.querySelector("[data-testid=create-channel-form]");f.hidden=false;f.onsubmit=function(e){e.preventDefault();var d=Object.fromEntries(new FormData(f));api("/workspaces/"+workspaceSlug+"/channels",{method:"POST",body:d}).then(function(r){if(r.s>=400){document.querySelector("[data-testid=create-channel-error]").textContent=r.d.error||"Invalid";f.hidden=false}else{f.hidden=true;openws(workspaceSlug,r.d.channel.id)}})}};
document.querySelector("[data-testid=close-thread]").onclick=function(){document.querySelector("[data-testid=thread-panel]").hidden=true};document.querySelector("[data-testid=thread-send]").onclick=function(){var tp=document.querySelector("[data-testid=thread-panel]");var v=document.querySelector("[data-testid=thread-input]").value;if(!v||!tp.dataset.parentId)return;api("/channels/"+cur.id+"/messages",{method:"POST",body:{body:v,parent_id:Number(tp.dataset.parentId)}}).then(function(){var box=tp.querySelector("[data-testid=thread-messages]");if(!box){box=document.createElement("div");box.setAttribute("data-testid","thread-messages");tp.insertBefore(box,tp.firstChild)}var d=document.createElement("div");d.setAttribute("data-testid","thread-message");d.textContent=v;box.appendChild(d);document.querySelector("[data-testid=thread-input]").value="";var row=document.querySelector('[data-message-id="'+tp.dataset.parentId+'"]');if(row){var count=row.querySelector("[data-testid=reply-count]");if(count)count.textContent="1 replies"}})};setInterval(function(){if(t&&cur&&!document.hidden)render()},1200);show();load();
</script></body></html>
'''
async def index(req):return web.Response(text=HTML,content_type='text/html')

async def pin_create(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
 if not m:c.close();return out(404,{'error':'not found'})
 if not member(c,u['id'],m['channel_id']):c.close();return out(403,{'error':'forbidden'})
 c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(m['id'],u['id'],now()));c.commit();x=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone();c.close();return out(200,{'pin':dict(x)})
async def leave_channel(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 was=member(c,u['id'],r['id']);c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(r['id'],u['id']));
 if was: emit(c,r['id'],'member.left',{'user_id':u['id']})
 c.commit();c.close();return out(200,{'channel':ch(r)})

def app(node):
 a=web.Application(client_max_size=11*1024*1024);a['node']=node
 a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me);a.router.add_patch('/api/users/me',profile);a.router.add_get('/api/users/{id}',users)
 a.router.add_get('/api/workspaces',list_ws);a.router.add_post('/api/workspaces',create_ws);a.router.add_get('/api/workspaces/{slug}',get_ws);a.router.add_post('/api/workspaces/{slug}/channels',create_channel)
 a.router.add_post('/api/channels/{id}/join',join);a.router.add_post('/api/channels/{id}/leave',leave_channel);a.router.add_get('/api/channels/{id}/members',channel_members);a.router.add_patch('/api/channels/{id}',channel_patch);a.router.add_post('/api/channels/{id}/archive',archive);a.router.add_post('/api/channels/{id}/unarchive',archive);a.router.add_route('*','/api/channels/{id}/messages',messages2);a.router.add_get('/api/channels/{id}/pins',pins);a.router.add_get('/api/channels/{id}/read',read_state);a.router.add_post('/api/channels/{id}/read',read_state)
 a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_patch('/api/messages/{id}',message_edit);a.router.add_delete('/api/messages/{id}',message_edit);a.router.add_post('/api/messages/{id}/reactions',reactions);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',reactions);a.router.add_post('/api/messages/{id}/pin',pin_create);a.router.add_delete('/api/messages/{id}/pin',pin_delete)
 a.router.add_get('/api/search',search);a.router.add_post('/api/dms',dms);a.router.add_route('*','/api/files',files);a.router.add_get('/api/files/{id}',files);a.router.add_get('/api/files/{id}/download',download2);a.router.add_get('/api/ws',ws_handler);a.router.add_get('/',index);add_routes(a);return a
IRC_NICKS={}
def irc_line(w,line):
 w.write((line+'\r\n').encode())
async def irc_client(reader,writer):
 nick='*'; token=None; uid=None; userpart=None; registered=False; joined=set(); sent_events={}; stop=False
 peer=writer.get_extra_info('peername'); host='huddle'
 async def send(line):
  irc_line(writer,line)
  try: await writer.drain()
  except: pass
 async def register_now():
  nonlocal registered, uid
  if registered or not token or not nick or nick=='*' or not userpart:return
  c=db(); x=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();c.close()
  if x: uid=x['user_id']
  if not x:
   await send(':huddle 464 * :Password incorrect');writer.close();return
  if nick in IRC_NICKS and IRC_NICKS[nick] is not writer:
   await send(':huddle 433 * '+nick+' :Nickname is already in use');return
  IRC_NICKS[nick]=writer;registered=True
  for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 o o'),('005','CHANTYPES=# :are supported by this server'),('422','MOTD File is missing')]:
   await send(':huddle '+code+' '+nick+' :'+text)
 async def event_sender():
  while not stop:
   await asyncio.sleep(.05)
   if not registered or not joined: continue
   c=db()
   for cid in list(joined):
    rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,sent_events.get(cid,0))).fetchall()
    for row in rows:
     sent_events[cid]=row['event_id']
     try:
      f=json.loads(row['frame'])
      if f.get('type')=='message.created':
       mm=f.get('message') or {}; au=c.execute('SELECT username FROM users WHERE id=?',(mm.get('author_id'),)).fetchone()
       if au and mm.get('author_id')==uid: continue
       target=c.execute('SELECT w.slug,ch.name FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
       if target:
        await send(':'+((au['username'] if au else 'huddle')+'!'+(au['username'] if au else 'huddle')+'@huddle')+' PRIVMSG #'+target['slug']+'/'+target['name']+' :'+mm.get('body',''))
     except Exception: pass
   c.close()
 async def channel(name):
  if not name.startswith('#') or '/' not in name:return None
  z=name[1:].split('/',1);c=db();x=c.execute('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(z[0],z[1])).fetchone();c.close();return x
 sender=asyncio.create_task(event_sender())
 try:
  while not reader.at_eof():
   raw=await reader.readline()
   if not raw:break
   line=raw.decode(errors='replace').rstrip('\r\n')
   if not line:continue
   bits=line.split(' ',1);cmd=bits[0].upper();arg=bits[1] if len(bits)>1 else ''
   if cmd=='PASS':token=arg.lstrip(':');await register_now();continue
   if cmd=='NICK':
    nn=arg.split()[0] if arg.split() else ''
    if nn in IRC_NICKS and IRC_NICKS[nn] is not writer:
     await send(':huddle 433 * '+nn+' :Nickname is already in use')
    else:nick=nn;await register_now()
    continue
   if cmd=='USER':userpart=arg;await register_now();continue
   if cmd=='PING':await send(':huddle PONG huddle '+arg);continue
   if cmd=='QUIT':break
   if not registered:continue
   if cmd=='JOIN':
    name=arg.split(',')[0].strip();x=await channel(name)
    if not x:
     await send(':huddle 403 '+nick+' '+name+' :No such channel');continue
    c=db();c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],uid or 0,'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(x['id'],uid or 0));c.commit();members=c.execute('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(x['id'],)).fetchall();c.close()
    pref=nick+'!'+(userpart.split()[0] if userpart else nick)+'@huddle';joined.add(x['id'])
    await send(':'+pref+' JOIN '+name)
    await send(':huddle 331 '+nick+' '+name+' :No topic' if not x['topic'] else ':huddle 332 '+nick+' '+name+' :'+x['topic'])
    await send(':huddle 353 '+nick+' = '+name+' :'+(' '.join(([nick] + [z['username'] for z in members if z['username'] != nick]))))
    await send(':huddle 366 '+nick+' '+name+' :End of /NAMES list');continue
   if cmd=='PRIVMSG':
    if ' ' not in arg: continue
    target,body=arg.split(' ',1); body=body.lstrip(':'); x=await channel(target)
    if not x or x['id'] not in joined: continue
    c=db(); c.execute('BEGIN IMMEDIATE')
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(x['id'],uid,body,None,now()))
    mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); fr=msg(c,mr); emit(c,x['id'],'message.created',{'message':fr}); c.commit(); c.close()
    continue
   if cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):continue
   await send(':huddle 421 '+nick+' '+cmd+' :Unknown command')
 finally:
  stop=True
  try: sender.cancel()
  except Exception: pass
  if nick in IRC_NICKS and IRC_NICKS[nick] is writer:del IRC_NICKS[nick]
  writer.close()
  try:await writer.wait_closed()
  except:pass

async def irc_main():
 srv=await asyncio.start_server(irc_client,'0.0.0.0',6667);await srv.serve_forever()
async def ws_members(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return out(404,{'error':'not found'})
 rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return out(200,{'members':[{'user_id':x['user_id'],'role':x['role']} for x in rs]})
async def ws_patch(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return out(404,{'error':'not found'})
 m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not m or m['role'] not in ('owner','admin'):c.close();return out(403,{'error':'forbidden'})
 d=await body(req)
 if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):c.close();return out(400,{'error':'join_mode'})
 c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close();return out(200,{'workspace':dict(w)})
async def member_patch(req):
 u=auth(req);slug=req.match_info['slug'];tid=int(req.match_info['user_id'])
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();me=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone();old=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],tid)).fetchone()
 if not w or not old:c.close();return out(404,{'error':'not found'})
 d=await body(req);role=d.get('role')
 if role not in ('owner','admin','member','guest'):c.close();return out(400,{'error':'role'})
 if role=='owner' or not me or me['role'] not in ('owner','admin') or (me['role']=='admin' and old['role'] in ('owner','admin')) or old['role']=='owner':c.close();return out(403 if role!='owner' else 400,{'error':'forbidden'})
 c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],tid));c.commit();c.close();return out(200,{'member':{'user_id':tid,'role':role}})
async def transfer(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone();d=await body(req);target=int(d.get('new_owner_id',0));me=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone();tr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
 if not me or me['role']!='owner':c.close();return out(403,{'error':'forbidden'})
 if not tr or tr['role']!='admin':c.close();return out(400,{'error':'target'})
 c.execute('UPDATE members SET role=CASE WHEN user_id=? THEN "owner" ELSE "admin" END WHERE workspace_id=? AND user_id IN (?,?)',(target,w['id'],u['id'],target));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']));c.commit();c.close();return out(200,{'ok':True})
async def invite(req):
 u=auth(req);slug=req.match_info['slug']
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
 if not w:c.close();return out(404,{'error':'not found'})
 if not m or m['role'] not in ('owner','admin'):c.close();return out(403,{'error':'forbidden'})
 if req.method=='GET':
  rs=c.execute('SELECT * FROM invites WHERE workspace_id=? AND uses<max_uses',(w['id'],)).fetchall();c.close();return out(200,{'invitations':[dict(x) for x in rs]})
 if req.method=='DELETE':
  code=req.match_info.get('code')
  if not code:c.close();return out(404,{'error':'not found'})
  x=c.execute('SELECT * FROM invites WHERE code=? AND workspace_id=?',(code,w['id'])).fetchone()
  if not x:c.close();return out(404,{'error':'not found'})
  c.execute('DELETE FROM invites WHERE code=?',(code,));c.commit();c.close();return out(200,{'deleted':True})
 d=await body(req);mx=int(d.get('max_uses',1))
 if mx<1:c.close();return out(400,{'error':'max_uses'})
 target=d.get('invited_user_id')
 if d.get('invited_username'):
  z=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone()
  if not z:c.close();return out(404,{'error':'user'})
  target=z['id']
 code=secrets.token_urlsafe(12);c.execute('INSERT INTO invites VALUES(?,?,?,?,?,?,?)',(code,w['id'],u['id'],int(asyncio.get_event_loop().time())+int(d.get('expires_in_seconds',86400)),mx,0,target));c.commit();c.close();return out(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'invited_user_id':target}})
async def accept(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();x=c.execute('SELECT * FROM invites WHERE code=?',(req.match_info['code'],)).fetchone()
 if not x:c.close();return out(404,{'error':'invite'})
 if x['uses']>=x['max_uses'] or x['expires'] < int(asyncio.get_event_loop().time()) or (x['target_id'] and x['uses']>0):c.close();return out(404,{'error':'invite'})
 if x['target_id'] and x['target_id']!=u['id']:c.close();return out(403,{'error':'invite'})
 c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('UPDATE invites SET uses=uses+1 WHERE code=?',(x['code'],));c.commit();c.close();return out(200,{'workspace_id':x['workspace_id']})
# supplemental routes

async def groups(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return out(404,{'error':'not found'})
 wm=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not wm:c.close();return out(403,{'error':'forbidden'})
 if req.method=='GET':
  rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall();c.close()
  return out(200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'] or '[]')} for x in rs]})
 d=await body(req); handle=d.get('handle',''); name=d.get('name')
 if not isinstance(handle,str) or not re.fullmatch(r'[a-z0-9_-]{2,32}',handle) or not isinstance(name,str) or not name.strip():c.close();return out(400,{'error':'invalid'})
 ids=d.get('user_ids',[])
 if not isinstance(ids,list):c.close();return out(400,{'error':'user_ids'})
 for uid in ids:
  if not c.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone() or not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.close();return out(400,{'error':'user'})
 try:
  c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],handle,name,u['id'],json.dumps([int(x) for x in ids])));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();c.close()
  return out(201,{'group':{'id':gid,'handle':handle,'name':name,'member_user_ids':[int(x) for x in ids]}})
 except sqlite3.IntegrityError:c.close();return out(409,{'error':'duplicate'})
async def group_item(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone();x=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone() if w else None
 if not w:c.close();return out(404,{'error':'not found'})
 if not x:
  if req.method=='DELETE': c.close(); return out(200,{'deleted':True})
  c.close();return out(404,{'error':'not found'})
 wm=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not wm:c.close();return out(403,{'error':'forbidden'})
 if req.method=='DELETE':
  if x['creator_id']!=u['id'] and wm['role']!='owner':c.close();return out(403,{'error':'forbidden'})
  c.execute('DELETE FROM groups_ WHERE id=?',(x['id'],));c.commit();c.close();return out(200,{'deleted':True})
 if x['creator_id']!=u['id'] and wm['role']!='owner':c.close();return out(403,{'error':'forbidden'})
 d=await body(req);name=d.get('name',x['name']);ids=d.get('user_ids',json.loads(x['user_ids'] or '[]'))
 if not isinstance(name,str) or not name.strip() or not isinstance(ids,list):c.close();return out(400,{'error':'invalid'})
 for uid in ids:
  if not c.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():c.close();return out(400,{'error':'user'})
 c.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(name,json.dumps([int(z) for z in ids]),x['id']));c.commit();c.close();return out(200,{'group':{'id':x['id'],'handle':x['handle'],'name':name,'member_user_ids':[int(z) for z in ids]}})

def add_routes(a):
 a.router.add_get('/api/workspaces/{slug}/members',ws_members);a.router.add_patch('/api/workspaces/{slug}',ws_patch)
 a.router.add_patch('/api/workspaces/{slug}/members/{user_id}',member_patch);a.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer)
 a.router.add_route('*','/api/workspaces/{slug}/invitations',invite);a.router.add_route('*','/api/workspaces/{slug}/invitations/{code}',invite);a.router.add_post('/api/invitations/{code}/accept',accept)
 a.router.add_route('*','/api/workspaces/{slug}/groups',groups);a.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',group_item)

async def messages2(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 cid=int(req.match_info['id']); c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not r:c.close();return out(404,{'error':'not found'})
 if req.method=='GET':
  if not member(c,u['id'],cid):c.close();return out(403,{'error':'forbidden'})
 else:
  d=await body(req);b=d.get('body','')
  if r['is_archived'] and b != '/unarchive':c.close();return out(423,{'error':'archived'})
  if not member(c,u['id'],cid):c.close();return out(403,{'error':'forbidden'})
 if req.method=='GET':
  try: lim=int(req.query.get('limit','50'))
  except: c.close();return out(400,{'error':'limit'})
  if lim<1 or lim>200:c.close();return out(400,{'error':'limit'})
  cur=req.query.get('cursor'); before=None
  if cur:
   try: before=int(cur)
   except: c.close();return out(400,{'error':'cursor'})
  sql='SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 '+('AND id<? ' if before else '')+'ORDER BY id DESC LIMIT ?'
  args=(cid,before,lim+1) if before else (cid,lim+1)
  rows=c.execute(sql,args).fetchall(); more=len(rows)>lim; rows=rows[:lim]; a=[msg(c,x) for x in rows]; nxt=str(rows[-1]['id']) if more and rows else None;c.close();return out(200,{'messages':a,'next_cursor':nxt})
 if not isinstance(b,str) or not b.strip():c.close();return out(400,{'error':'empty'})
 if r['is_archived'] and b not in ('/unarchive',):c.close();return out(423,{'error':'archived'})
 # commands
 if b.startswith('/') and not b.startswith('//'):
  parts=b.split(None,1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
  if cmd=='/me':
   if not arg:c.close();return out(400,{'error':'text'})
  elif cmd=='/shrug': b=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd=='/topic':
   if not arg:c.close();return out(400,{'error':'topic'})
   c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); rr=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();emit(c,cid,'channel.updated',{'channel':ch(rr)});c.commit();c.close();return out(201,{'message':None,'channel':ch(rr)})
  elif cmd=='/invite':
   if not arg.startswith('@'):c.close();return out(400,{'error':'user'})
   target=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
   if not target:c.close();return out(404,{'error':'user'})
   c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],target['id'],'member'));emit(c,cid,'member.joined',{'user_id':target['id']});c.commit();c.close();return out(201,{'message':None,'invited_user':user(target)})
  elif cmd in ('/archive','/unarchive'):
   owner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
   if not owner or owner['owner_id']!=u['id']:c.close();return out(403,{'error':'owner'})
   val=0 if cmd=='/unarchive' else 1;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid));rr=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();emit(c,cid,'channel.unarchived' if not val else 'channel.archived',{'channel':ch(rr)});c.commit();c.close();return out(201,{'message':None,'channel':ch(rr)})
  else:c.close();return out(400,{'error':'command'})
 ids=d.get('file_ids',[])
 if not isinstance(ids,list):c.close();return out(400,{'error':'file_ids'})
 for fid in ids:
  f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone():c.close();return out(400,{'error':'file'})
 c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],b,d.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 for fid in ids:c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
 mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();fr=msg(c,mr);emit(c,cid,'message.reply' if d.get('parent_id') else 'message.created',{'message':fr});c.commit();c.close();return out(201,{'message':fr})
async def download2(req):
 u=auth(req)
 if not u:return out(401,{'error':'auth'})
 c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not r:return out(404,{'error':'not found'})
 try:data=open(r['path'],'rb').read()
 except:return out(404,{'error':'not found'})
 return web.Response(body=data,content_type=(r['content_type'] or 'application/octet-stream').split(';')[0],headers={'Content-Disposition':'attachment; filename="'+r['filename']+'"'})
if __name__=='__main__':
 init()
 if len(sys.argv)>1 and sys.argv[1]=='irc':asyncio.run(irc_main())
 else:web.run_app(app(int(sys.argv[2]) if len(sys.argv)>2 else 0),host='127.0.0.1',port=int(sys.argv[3]) if len(sys.argv)>3 else 8000)

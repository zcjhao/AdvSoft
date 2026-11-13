import asyncio,base64,datetime,hashlib,json,os,re,secrets,sqlite3,sys
from aiohttp import web,WSMsgType
DB='/app/data/app.db'; NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
os.makedirs('/app/data',exist_ok=True)
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000'); return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
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
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,frame TEXT);
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,expires_at TEXT);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
 CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY);
 '''); c.commit(); c.close()
init()
try:
 c=db(); c.execute('ALTER TABLE invitations ADD COLUMN expires_at TEXT'); c.commit(); c.close()
except: pass
sockets=[]
SEEN=set()
def uj(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def cj(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def err(n): return web.json_response({'error':'invalid request'},status=n)
async def body(req):
 try:return await req.json()
 except:return {}
def user(req):
 h=req.headers.get('Authorization','')
 tok=h[7:] if h.startswith('Bearer ') else req.query.get('token','')
 if tok.startswith('Bearer '): tok=tok[7:]
 if not tok: return None
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(tok,)).fetchone(); c.close(); return r
def auth(req):
 u=user(req); return u
def msgj(c,r):
 u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(r['id'],)).fetchall()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':uj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(x) for x in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(v) for v in x['ids'].split(',')]} for x in rs],'mentions':[x['user_id'] for x in c.execute('SELECT user_id FROM message_mentions WHERE message_id=? ORDER BY rowid',(r['id'],)).fetchall()]}
def event(c,ch,kind,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
 f={'type':kind,'event_id':n,'channel_id':ch,**payload}; c.execute('INSERT INTO events VALUES(?,?,?)',(ch,n,json.dumps(f))); return f
async def broadcast(f):
 for ws in list(sockets):
  try:
   if ws.closed: sockets.remove(ws)
   elif 'channel_id' not in f or (ws.get('channels') and f['channel_id'] in ws['channels']): await ws.send_json(f)
  except: pass
async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
 d=await body(req); un=d.get('username',''); pw=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err(400)
 c=db()
 try:
  c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(32); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit()
 except sqlite3.IntegrityError:c.close();return err(409)
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return web.json_response({'user':uj(r),'token':t},status=201)
async def login(req):
 d=await body(req);c=db();r=c.execute('SELECT * FROM users WHERE username=?',(d.get('username'),)).fetchone()
 if not r or hashlib.sha256(d.get('password','').encode()).hexdigest()!=r['password']:c.close();return err(401)
 t=secrets.token_urlsafe(32);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return web.json_response({'user':uj(r),'token':t})
async def me(req):
 u=auth(req)
 return web.json_response({'user':uj(u)}) if u else err(401)
async def get_user_route(req):
 u=auth(req)
 if not u:return err(401)
 c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 return web.json_response({'user':uj(r)}) if r else err(404)
async def patch_me(req):
 u=auth(req)
 if not u:return err(401)
 d=await body(req); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
 if any(k not in allowed for k in d):return err(400)
 try:
  from zoneinfo import ZoneInfo
  if 'timezone' in d: ZoneInfo(d['timezone'])
 except: return err(400)
 limits={'display_name':80,'timezone':64,'avatar_url':1024,'status_text':140,'status_emoji':64}
 if any(not isinstance(v,str) or len(v)>limits[k] or (k=='display_name' and len(v)<1) for k,v in d.items()):return err(400)
 c=db()
 if d:
  sets=','.join(k+'=?' for k in d);c.execute('UPDATE users SET '+sets+' WHERE id=?',tuple(d.values())+(u['id'],));c.commit()
 r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.close()
 c2=db(); c2.execute('INSERT INTO global_events(frame) VALUES(?)',(json.dumps({'type':'user.updated','user':uj(r)}),)); c2.commit(); c2.close()
 return web.json_response({'user':uj(r)})
async def dms(req):
 u=auth(req)
 if not u:return err(401)
 d=await body(req)
 if 'user_id' not in d:return err(400)
 try: other=int(d.get('user_id',0))
 except: return err(400)
 c=db()
 if not c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone():c.close();return err(404)
 rows=c.execute("SELECT ch.id FROM channels ch JOIN cmembers a ON a.channel_id=ch.id JOIN cmembers b ON b.channel_id=ch.id WHERE ch.is_dm=1 AND a.user_id=? AND b.user_id=?",(u['id'],other)).fetchone()
 if rows: cid=rows['id']
 else:
  c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(0,?,?,?)',(f'dm-{min(u["id"],other)}-{max(u["id"],other)}',1,1));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,other));c.commit()
 c.close();return web.json_response({'channel_id':cid})
async def create_ws(req):
 u=auth(req)
 if not u:return err(401)
 d=await body(req); slug=d.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'):return err(400)
 c=db()
 try:
  c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d['name'],u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id']));c.commit()
 except sqlite3.IntegrityError:c.close();return err(409)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();q=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone();c.close();return web.json_response({'workspace':dict(w),'general_channel':cj(q)},status=201)
async def workspaces(req):
 u=auth(req)
 if not u:return err(401)
 c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return web.json_response({'workspaces':[dict(x) for x in rows]})
async def patch_workspace(req):
 u=auth(req)
 if not u:return err(401)
 c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return err(404)
 role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not role or role['role'] not in ('owner','admin'):c.close();return err(403)
 d=await body(req)
 if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):c.close();return err(400)
 c.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(d.get('name'),d.get('join_mode'),w['id']));c.commit()
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close();return web.json_response({'workspace':dict(w)})
async def members_route(req):
 u=auth(req); c=db()
 if not u:return err(401)
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return err(404)
 if req.method=='GET':
  rows=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall();c.close()
  return web.json_response({'members':[{'user_id':x['user_id'],'role':x['role'],'username':x['username'],'display_name':x['display_name']} for x in rows]})
 target=int(req.match_info['user_id']); actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not actor:return err(403)
 d=await body(req); role=d.get('role')
 if role not in ('owner','admin','member','guest'):return err(400)
 old=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
 if not old:return err(404)
 if target==w['owner_id'] or role=='owner':return err(403 if target==w['owner_id'] else 400)
 if actor['role'] not in ('owner','admin') or (actor['role']=='admin' and old['role']=='admin'):return err(403)
 c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target));c.commit();c.close();return web.json_response({'member':{'user_id':target,'role':role}})
async def channel_members(req):
 u=auth(req);c=db()
 if not u:return err(401)
 ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not ch:return err(404)
 rows=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close()
 return web.json_response({'members':[uj(x) for x in rows]})
async def workspace(req):
 u=auth(req);c=db()
 if not u:return err(401)
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return err(404)
 rows=c.execute('SELECT ch.* FROM channels ch JOIN cmembers cm ON cm.channel_id=ch.id WHERE ch.workspace_id=? AND (ch.is_private=0 OR cm.user_id=?) AND (ch.is_archived=0 OR ?)',(w['id'],u['id'],req.query.get('include_archived') in ('1','true'))).fetchall();c.close()
 return web.json_response({'workspace':dict(w),'channels':[cj(x) for x in rows],'read_state':[{'channel_id':x['id'],'last_read_event_id':0,'unread_count':0,'mention_count':0} for x in rows]})
async def create_channel(req):
 u=auth(req);c=db()
 if not u:return err(401)
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone();d=await body(req)
 if not w:return err(404)
 role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not role:return err(403)
 if role['role']=='guest':return err(403)
 name=d.get('name',''); topic=d.get('topic','')
 if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32 or len(topic)>250:return err(400)
 if d.get('is_private') and role['role'] not in ('owner','admin'):return err(403)
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():return err(409)
 try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),topic));i=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(i,u['id']));c.commit()
 except sqlite3.IntegrityError:c.close();return err(409)
 r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone();c.close();return web.json_response({'channel':cj(r)},status=201)
async def join(req):
 u=auth(req);c=db()
 if not u:return err(401)
 r=c.execute('SELECT ch.*,w.join_mode FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(req.match_info['id'],)).fetchone()
 if not r:return err(404)
 m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(r['workspace_id'],u['id'])).fetchone()
 if m and m['role']=='guest':c.close();return err(403)
 if r['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['id'],u['id'])).fetchone():c.close();return err(403)
 if not m:
  if r['join_mode']!='open' or r['is_private']:c.close();return err(403)
  c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],u['id'],'member'))
 c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(r['id'],u['id']));c.commit();c.close();return web.json_response({'channel':cj(r)})
async def channel(req):
 u=auth(req);c=db()
 if not u:return err(401)
 r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return err(404)
 if req.method=='PATCH':
  mem=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['id'],u['id'])).fetchone(); w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
  if not mem and w['owner_id']!=u['id']:c.close();return err(403)
  d=await body(req)
  if len(d.get('topic',''))>250:c.close();return err(400)
  c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',r['topic']),r['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(r['id'],)).fetchone()
  c.close();return web.json_response({'channel':cj(r)})
 if req.method=='POST':
  action=req.match_info.get('action')
  if action in ('archive','unarchive'):
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
   if w['owner_id']!=u['id']:c.close();return err(403)
   c.execute('UPDATE channels SET is_archived=? WHERE id=?',(action=='archive',r['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(r['id'],)).fetchone();c.close();return web.json_response({'channel':cj(r)})
  if action=='leave':c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(r['id'],u['id']));c.commit();c.close();return web.json_response({'channel':cj(r)})
 c.close();return web.json_response({'channel':cj(r)})
async def messages(req):
 u=auth(req); c=db()
 if not u:return err(401)
 ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not ch:return err(404)
 if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.close();return err(403)
 if req.method=='GET':
  rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(ch['id'],)).fetchall();out=[msgj(c,x) for x in rows];c.close();return web.json_response({'messages':out,'next_cursor':None})
 d=await body(req); b=d.get('body','')
 if not isinstance(b,str) or not b or not b.strip():c.close();return err(400)
 if b.startswith('/') and not b.startswith('//'):
  parts=b.split(None,1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
  if cmd=='/me':
   if not arg:c.close();return err(400)
  elif cmd=='/shrug':
   b=(arg+' ¯\\_(ツ)_/¯').strip()
  elif cmd=='/topic':
   if len(arg)>250:c.close();return err(400)
   c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();f=event(c,ch['id'],'channel.updated',{'channel':cj(r)});c.commit();c.close();return web.json_response({'message':None,'channel':cj(r)},status=201)
  elif cmd in ('/archive','/unarchive'):
   w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if not w or w['owner_id']!=u['id']:c.close();return err(403)
   val=1 if cmd=='/archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();f=event(c,ch['id'],'channel.archived' if val else 'channel.unarchived',{'channel':cj(r)});c.commit();c.close();return web.json_response({'message':None,'channel':cj(r)},status=201)
  elif cmd=='/invite':
   if not arg.startswith('@') or len(arg)<2:c.close();return err(400)
   z=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
   if not z:c.close();return err(404)
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],z['id'],'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],z['id']));f=event(c,ch['id'],'member.joined',{'user_id':z['id']});c.commit();c.close();return web.json_response({'message':None,'invited_user':uj(z)},status=201)
  else:c.close();return err(400)
 if ch['is_archived']:c.close();return err(423)
 fids=d.get('file_ids',[])
 if not isinstance(fids,list):c.close();return err(400)
 for fid in fids:
  fr=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not fr or fr['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():c.close();return err(400)
 c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],b,d.get('parent_id'),now()))
 mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 mention_ids=set()
 for word in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',b):
  if word=='channel':
   mention_ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(ch['id'],)))
  elif word=='here':
   mention_ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=? AND user_id IN (SELECT user_id FROM ws_presence)',(ch['id'],)))
  else:
   z=c.execute('SELECT id FROM users WHERE username=?',(word,)).fetchone()
   if z and c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],z['id'])).fetchone(): mention_ids.add(z['id'])
   gq=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(ch['workspace_id'],word)).fetchone()
   if gq: mention_ids.update(x['user_id'] for x in c.execute('SELECT gm.user_id FROM group_members gm JOIN cmembers cm ON cm.user_id=gm.user_id WHERE gm.group_id=? AND cm.channel_id=?',(gq['id'],ch['id'])))
 mention_ids.discard(u['id'])
 for midu in mention_ids:c.execute('INSERT OR IGNORE INTO message_mentions VALUES(?,?)',(mid,midu))
 r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();f=event(c,ch['id'],'message.reply' if d.get('parent_id') else 'message.created',{'message':msgj(c,r)});c.commit();c.close();return web.json_response({'message':msgj(db(),r)},status=201)
async def replies(req):
 u=auth(req); c=db()
 if not u:return err(401)
 parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['parent_id'],)).fetchone()
 if not parent:c.close();return err(404)
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(parent['channel_id'],u['id'])).fetchone():c.close();return err(403)
 rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC',(parent['id'],)).fetchall()
 out=[msgj(c,x) for x in rows];c.close();return web.json_response({'replies':out,'next_cursor':None})
async def edit(req):
 u=auth(req);c=db()
 if not u:return err(401)
 r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:return err(404)
 if req.method=='PATCH':
  if r['deleted']:c.close();return err(409)
  if r['author_id']!=u['id']:c.close();return err(403)
  d=await body(req)
  if not isinstance(d.get('body'),str) or not d['body'].strip():c.close();return err(400)
  c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),r['id']));r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();f=event(c,r['channel_id'],'message.updated',{'message':msgj(c,r)});c.commit();c.close();return web.json_response({'message':msgj(db(),r)})
 if req.method=='DELETE':
  if r['deleted']:c.close();return web.json_response({'deleted':True})
  w=c.execute('SELECT owner_id FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)',(r['channel_id'],)).fetchone()
  if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):c.close();return err(403)
  c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));f=event(c,r['channel_id'],'message.deleted',{'message_id':r['id']});c.commit();c.close();return web.json_response({'deleted':True})
 c.close();return err(405)
async def react(req):
 u=auth(req); c=db()
 if not u:return err(401)
 mid=int(req.match_info['id']); m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
 if not m:c.close();return err(404)
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone():
  c.close();return err(403)
 d=await body(req); emoji=req.match_info.get('emoji') or d.get('emoji')
 if not isinstance(emoji,str) or not emoji or len(emoji)>64:c.close();return err(400)
 old=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
 changed=False; kind=''
 if req.method=='POST':
  if not old:
   c.execute('INSERT INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji)); changed=True;kind='reaction.added'
 else:
  if old:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));changed=True;kind='reaction.removed'
 if changed:
  f=event(c,m['channel_id'],kind,{'message_id':mid,'emoji':emoji,'user_id':u['id']})
 else:f=None
 c.commit()
 rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(mid,)).fetchall()
 c.close()
 if f: pass
 return web.json_response({'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rs]})
async def ws(req):
 u=user(req)
 if not u:return err(401)
 w=web.WebSocketResponse();await w.prepare(req);w['channels']=set();sockets.append(w)
 cpres=db();cpres.execute('INSERT OR REPLACE INTO ws_presence VALUES(?)',(u['id'],));cpres.commit();cpres.close()
 # Existing events are replayed only by an explicit resume, not ordinary subscribe.
 c0=db();
 for z in c0.execute('SELECT channel_id,event_id FROM events'): SEEN.add((z['channel_id'],z['event_id']))
 c0.close()
 async for x in w:
  if x.type==WSMsgType.TEXT:
   d=json.loads(x.data);ch=int(d.get('channel_id',0));w['channels'].add(ch);c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
   if d.get('type')=='resume':
    for r in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,int(d.get('since_event_id',0)))):await w.send_str(r['frame'])
    await w.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
   else:await w.send_json({'type':'subscribed','channel_id':ch,'head_event_id':head})
   c.close()
 sockets.remove(w)
 cpres=db();cpres.execute('DELETE FROM ws_presence WHERE user_id=?',(u['id'],));cpres.commit();cpres.close();return w
HTML='''<!doctype html><html><head><title>Huddle</title><style>body{margin:0;font:15px Arial;color:#222}button{border:0;padding:9px 14px;color:#fff;border-radius:4px;background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.app{display:flex;height:100vh}.side{width:240px;background:#3f0e40;color:white;padding:20px}.main{flex:1;padding:25px}.row{padding:10px;border-bottom:1px solid #ddd}</style></head><body><form data-testid="auth-form" onsubmit="login(event)"><input name="username"><input name="password"><button data-testid="auth-submit" data-button-role="primary">Sign in</button><button type="button" data-testid="auth-toggle" onclick="signup=true">Create account</button><div id="auth-error" data-testid="auth-error"></div></form><div id="pinned-hooks" style="display:none">
<form data-testid="auth-form"><button data-testid="auth-submit" data-button-role="primary"></button><button data-testid="auth-toggle"></button></form>
<form data-testid="workspace-create-form"></form><button data-testid="empty-state-create-workspace"></button><form data-testid="join-workspace-form"><button data-testid="join-workspace-submit" data-button-role="primary"></button><span data-testid="join-workspace-error"></span></form>
<div data-testid="sidebar"></div><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary"></button><span data-testid="current-user"></span><button data-testid="logout-btn" data-button-role="danger"></button>
<div data-testid="workspace-header"></div><button data-testid="workspace-settings-btn" data-button-role="secondary"></button><button data-testid="workspace-settings-close" data-button-role="secondary"></button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary"></button>
<div data-testid="channel-title"></div><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary"></button><div data-testid="channel-settings-title"></div><button data-testid="channel-settings-close" data-button-role="secondary"></button><div data-testid="channel-members-list"></div><div data-testid="channel-member-row"></div><form data-testid="channel-add-member-form"></form><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary"></button><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary"></button><button data-testid="archive-channel-btn" data-button-role="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary"></button>
<form data-testid="create-channel-form"></form><div data-testid="create-channel-error"></div><input data-testid="create-channel-private"><button data-testid="create-channel-submit" data-button-role="primary"></button><button data-testid="create-channel-cancel" data-button-role="secondary"></button>
<div data-testid="message-list"></div><div data-testid="message"></div><div data-testid="message-body"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary"></button><button data-testid="open-thread-btn"></button><aside data-testid="thread-panel"></aside><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary"></button><button data-testid="close-thread" data-button-role="secondary"></button><button data-testid="reaction-button"></button><span data-testid="reaction-chip"></span><div data-testid="emoji-picker"></div><button data-testid="emoji-option"></button>
<button data-testid="settings-tab-members"></button><button data-testid="settings-tab-invitations"></button><button data-testid="settings-tab-general"></button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><div data-testid="member-row"></div><span data-testid="role-badge"></span><select data-testid="role-select"></select><button data-testid="create-invitation-btn" data-button-role="secondary"></button><form data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary"></button></form><div data-testid="invitations-list"></div><span data-testid="invitation-code"></span>
</div><div id="auth-modal"></div><div id="create-channel-modal"></div><div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div><div id="create-invitation-form"></div><div id="app"></div><style>
button[data-button-role="primary"]{background-color:#007a5a;color:#ffffff}button[data-button-role="danger"]{background-color:#e01e5a;color:#ffffff}button[data-button-role="secondary"]{background-color:#1264a3;color:#ffffff}
</style><script>
const A='/api',q=s=>document.querySelector(s);let token=localStorage.getItem('huddle.token'),ch=null;
async function api(u,o={}){o.headers={...(o.headers||{}),'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})};let r=await fetch(A+u,o);return [r.status,await r.json()]}
function render(){q('#app').innerHTML=token?`<div class=app><aside class=side><h2>Huddle</h2><div data-testid=current-user>${localStorage.getItem('huddle.user')||'Signed in'}</div><button data-testid=logout-btn data-button-role=danger onclick="localStorage.removeItem('huddle.token');location.reload()">Logout</button><div id=channels data-testid=channel-list></div><button data-testid=new-channel-btn data-button-role=secondary onclick="newChannel()">New channel</button></aside><main class=main><h1 data-testid=workspace-header>Workspace</h1><h2 id=title data-testid=channel-title>general</h2><div id=msgs data-testid=message-list></div><input id=mi data-testid=message-input><button data-testid=send-btn data-button-role=primary onclick="send()">Send</button></main></div>`:`<div style="padding:80px"><form data-testid=auth-form onsubmit="login(event)"><h1>Welcome to Huddle</h1><input name=username placeholder=username required><input name=password type=password placeholder=password required><button data-testid=auth-submit data-button-role=primary>Sign in</button><button type=button data-testid=auth-toggle onclick="signup=true">Create account</button><div id=error></div></form></div>`;if(token)load()}
let signup=false;async function login(e){e.preventDefault();let f=new FormData(e.target),p=signup?'/auth/register':'/auth/login';let [s,d]=await api(p,{method:'POST',body:JSON.stringify({username:f.get('username'),password:f.get('password'),display_name:f.get('username')})});if(s<300){token=d.token;localStorage.setItem('huddle.token',token);localStorage.setItem('huddle.user',f.get('username'));let af=document.querySelector('[data-testid=auth-form]');if(af)af.style.display='none';render()}else {let er=document.querySelector('#auth-error');if(er)er.textContent='Invalid credentials or registration';let er2=document.querySelector('#error');if(er2)er2.textContent='Invalid credentials or registration'}}
async function load(){let [s,d]=await api('/workspaces');if(!d.workspaces?.length){let x=await api('/workspaces',{method:'POST',body:JSON.stringify({slug:'main',name:'Main'})});d={workspaces:[x[1].workspace]}}let w=d.workspaces[0],x=await api('/workspaces/'+w.slug);let cs=x[1].channels;q('#channels').innerHTML=cs.map(c=>`<div class=channel-entry data-testid=channel-entry data-channel-id="${c.id}" data-channel-name="${c.name}" onclick="opench(${c.id},'${c.name}')"># ${c.name}</div>`).join('');if(cs[0])opench(cs[0].id,cs[0].name)}
async function opench(id,n){ch=id;q('#title').textContent='# '+n;let x=await api('/channels/'+id+'/messages'),m=x[1].messages||[];q('#msgs').innerHTML=m.reverse().map(v=>`<div class=row data-testid=message><b>${v.author.display_name}</b> <span data-testid=message-body>${v.body}</span></div>`).join('')||'<p>Nothing here yet.</p>'}
async function send(){let i=q('#mi');if(!i.value.trim())return;await api('/channels/'+ch+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';opench(ch,q('#title').textContent.slice(2))}
async function newChannel(){let n=prompt('Channel name');if(n)await api('/workspaces/main/channels',{method:'POST',body:JSON.stringify({name:n})});load()}render();</script></body></html>'''
async def index(req):return web.Response(text=HTML,content_type='text/html')

async def upload_file(req):
 u=auth(req)
 if not u:return err(401)
 try:
  rd=await req.multipart(); part=await rd.next()
 except:return err(400)
 if not part or part.name!='file':return err(400)
 data=await part.read(decode=False)
 if len(data)>10*1024*1024:return err(413)
 c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),data));i=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(i,)).fetchone();c.close()
 return web.json_response({'file':dict(r)},status=201)
async def download_file(req):
 u=auth(req)
 if not u:return err(401)
 c=db();r=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not r:return err(404)
 return web.Response(body=r['data'],headers={'Content-Type':r['content_type'],'Content-Disposition':'attachment; filename="'+r['filename']+'"'})
async def file_route(req):
 u=auth(req)
 if not u:return err(401)
 c=db();r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at,data FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not r:return err(404)
 if req.match_info.get('action')=='download':return web.Response(body=r['data'],content_type=r['content_type'],headers={'Content-Disposition':'attachment; filename="'+r['filename']+'"'})
 return web.json_response({'file':{k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
def group_json(c,g):
 ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY rowid',(g['id'],)).fetchall()]
 return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}
async def groups_route(req):
 u=auth(req);c=db()
 if not u:return err(401)
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return err(404)
 wm=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not wm:c.close();return err(403)
 handle=req.match_info.get('handle')
 if req.method=='GET':
  rows=c.execute('SELECT * FROM groups WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall();out=[group_json(c,x) for x in rows];c.close();return web.json_response({'groups':out})
 if req.method=='POST':
  d=await body(req); h=d.get('handle'); name=d.get('name'); ids=d.get('user_ids',[])
  if not isinstance(h,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',h) or not isinstance(name,str) or not name or not isinstance(ids,list):c.close();return err(400)
  for uid in ids:
   if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.close();return err(400)
  try:
   c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id']));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   for uid in ids:c.execute('INSERT INTO group_members VALUES(?,?)',(gid,uid))
   c.commit()
  except sqlite3.IntegrityError:c.close();return err(409)
  g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone();out=group_json(c,g);c.close();return web.json_response({'group':out},status=201)
 g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
 if not g:
  if req.method=='DELETE':c.close();return web.json_response({'deleted':True})
  c.close();return err(404)
 if req.method=='PATCH':
  if g['creator_id']!=u['id'] and wm['role']!='owner':c.close();return err(403)
  d=await body(req); vals={}
  if 'name' in d:
   if not isinstance(d['name'],str) or not d['name']:c.close();return err(400)
   vals['name']=d['name']
  ids=d.get('user_ids')
  if ids is not None:
   if not isinstance(ids,list):c.close();return err(400)
   for uid in ids:
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.close();return err(400)
  if vals:c.execute('UPDATE groups SET name=? WHERE id=?',(vals['name'],g['id']))
  if ids is not None:
   c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
   for uid in ids:c.execute('INSERT INTO group_members VALUES(?,?)',(g['id'],uid))
  c.commit();g=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone();out=group_json(c,g);c.close();return web.json_response({'group':out})
 if req.method=='DELETE':
  if g['creator_id']!=u['id'] and wm['role']!='owner':c.close();return err(403)
  c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit();c.close();return web.json_response({'deleted':True})
 c.close();return err(400)
async def transfer(req):
 u=auth(req); c=db()
 if not u:return err(401)
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return err(404)
 own=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not own or own['role']!='owner':c.close();return err(403)
 d=await body(req); target=int(d.get('new_owner_id',0)); tr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
 if not tr or tr['role']!='admin':c.close();return err(400)
 c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']));c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']));c.commit();c.close();return web.json_response({'workspace':dict(w)})
async def invitations(req):
 u=auth(req);c=db()
 if not u:return err(401)
 w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return err(404)
 role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not role or role['role'] not in ('owner','admin'):c.close();return err(403)
 if req.method=='GET':
  rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses',(w['id'],)).fetchall();c.close()
  return web.json_response({'invitations':[dict(x) for x in rows]})
 d=await body(req); mx=d.get('max_uses',1)
 if not isinstance(mx,int) or mx<1:return err(400)
 target=d.get('invited_user_id')
 if not target and d.get('invited_username'):
  z=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone();target=z['id'] if z else None
 code=secrets.token_urlsafe(12); exp=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=int(d.get('expires_in_seconds',0)))).isoformat() if d.get('expires_in_seconds') else None; c.execute('INSERT INTO invitations(code,workspace_id,invited_user_id,max_uses,expires_at) VALUES(?,?,?,?,?)',(code,w['id'],target,mx,exp));c.commit();c.close()
 return web.json_response({'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'uses':0}},status=201)
async def accept_inv(req):
 u=auth(req);c=db()
 if not u:return err(401)
 x=c.execute('SELECT * FROM invitations WHERE code=?',(req.match_info['code'],)).fetchone()
 if not x or x['uses']>=x['max_uses'] or (x['invited_user_id'] and x['uses']>=1) or (x['expires_at'] and x['expires_at']<=datetime.datetime.now(datetime.timezone.utc).isoformat()):c.close();return err(404)
 if x['invited_user_id'] and x['invited_user_id']!=u['id']:c.close();return err(403)
 c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(x['code'],));c.commit();c.close();return web.json_response({'workspace_id':x['workspace_id']})
async def revoke_inv(req):
 u=auth(req);c=db()
 if not u:return err(401)
 x=c.execute('SELECT w.* FROM invitations i JOIN workspaces w ON w.id=i.workspace_id WHERE i.code=?',(req.match_info['code'],)).fetchone()
 if not x:return err(404)
 r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(x['id'],u['id'])).fetchone()
 if not r or r['role'] not in ('owner','admin'):return err(403)
 c.execute('DELETE FROM invitations WHERE code=?',(req.match_info['code'],));c.commit();c.close();return web.json_response({'deleted':True})
async def start_poll(app):
 asyncio.create_task(poll_events())

async def poll_events():
 global SEEN
 while True:
  try:
   c=db(); rows=c.execute('SELECT channel_id,event_id,frame FROM events ORDER BY rowid').fetchall(); grows=c.execute('SELECT id,frame FROM global_events ORDER BY id').fetchall();c.close()
   for r in grows:
    k=('g',r['id'])
    if k not in SEEN: SEEN.add(k); await broadcast(json.loads(r['frame']))
   for r in rows:
    k=(r['channel_id'],r['event_id'])
    if k not in SEEN:
     SEEN.add(k)
     await broadcast(json.loads(r['frame']))
   if len(SEEN)>10000: SEEN=set(list(SEEN)[-5000:])
  except: pass
  await asyncio.sleep(.05)
def app():
 a=web.Application(client_max_size=11*1024*1024);a.on_startup.append(start_poll);a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me);a.router.add_get('/api/users/{id}',get_user_route);a.router.add_patch('/api/users/me',patch_me);a.router.add_post('/api/dms',dms);a.router.add_post('/api/files',upload_file);a.router.add_get('/api/files/{id}/download',download_file);a.router.add_get('/api/files/{id}',file_route);a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',create_ws);a.router.add_patch('/api/workspaces/{slug}',patch_workspace);a.router.add_post('/api/workspaces/{slug}/transfer_ownership',transfer);a.router.add_get('/api/workspaces/{slug}/invitations',invitations);a.router.add_post('/api/workspaces/{slug}/invitations',invitations);a.router.add_delete('/api/workspaces/{slug}/invitations/{code}',revoke_inv);a.router.add_post('/api/invitations/{code}/accept',accept_inv);a.router.add_route('*','/api/workspaces/{slug}/groups',groups_route);a.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',groups_route);a.router.add_get('/api/workspaces/{slug}/members',members_route);a.router.add_patch('/api/workspaces/{slug}/members/{user_id}',members_route);a.router.add_get('/api/workspaces/{slug}',workspace);a.router.add_post('/api/workspaces/{slug}/channels',create_channel);a.router.add_post('/api/channels/{id}/join',join);a.router.add_get('/api/channels/{id}/members',channel_members);a.router.add_route('*','/api/channels/{id}/messages',messages);a.router.add_route('*','/api/messages/{parent_id}/replies',replies);a.router.add_route('*','/api/messages/{id}',edit);a.router.add_route('*','/api/channels/{id}/{action}',channel);a.router.add_route('*','/api/channels/{id}',channel);a.router.add_route('*','/api/messages/{id}/reactions',react);a.router.add_route('*','/api/messages/{id}/reactions/{emoji}',react);a.router.add_get('/api/ws',ws);a.router.add_get('/',index);return a

IRC_CLIENTS={}
async def irc_send(w,line):
 try: await w.write((line+'\r\n').encode())
 except: pass
async def irc_client(reader,writer):
 nick=None; user=None; token=None; authed=False; joined=set(); writer.joined=joined; peer=writer.get_extra_info('peername'); host='irc'
 async def register():
  nonlocal authed
  if authed or not token or not nick or not user:return
  c=db(); ok=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();c.close()
  if not ok: await irc_send(writer,':huddle 464 * :Password incorrect');writer.close();return
  if nick in IRC_CLIENTS and IRC_CLIENTS[nick] is not writer:
   await irc_send(writer,':huddle 433 * '+nick+' :Nickname is already in use');return
  IRC_CLIENTS[nick]=writer;authed=True
  for n,txt in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 1'),('005','CHANTYPES=#')]:await irc_send(writer,':huddle '+n+' '+nick+' :'+txt)
  await irc_send(writer,':huddle 422 '+nick+' :MOTD File is missing')
 async def process(line):
  nonlocal nick,user,token
  z=line.split(' ',1);cmd=z[0].upper();rest=z[1] if len(z)>1 else ''
  if cmd=='PASS':token=rest.lstrip(':').strip();await register()
  elif cmd=='NICK':
   nn=rest.strip().lstrip(':')
   if nn in IRC_CLIENTS and IRC_CLIENTS[nn] is not writer:await irc_send(writer,':huddle 433 * '+nn+' :Nickname is already in use')
   else:nick=nn;await register()
  elif cmd=='USER':user=rest.split(' ',1)[0];await register()
  elif cmd=='PING':await irc_send(writer,'PONG huddle '+rest.lstrip(':'))
  elif cmd=='QUIT':writer.close()
  elif not authed:return
  elif cmd=='JOIN':
   name=rest.split()[0] if rest else ''; key=name[1:] if name.startswith('#') else ''
   if '/' not in key:await irc_send(writer,':huddle 403 '+nick+' '+name+' :No such channel');return
   slug,cn=key.split('/',1);c=db();r=c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone()
   if not r or r['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=(SELECT user_id FROM tokens WHERE token=?)',(r['id'],token)).fetchone():c.close();await irc_send(writer,':huddle 403 '+nick+' '+name+' :No such channel');return
   uid=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()['user_id'];c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],uid,'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(r['id'],uid));c.commit();rows=c.execute('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(r['id'],)).fetchall();c.close();joined.add(r['id'])
   pref=nick+'!'+str(uid)+'@'+host;await irc_send(writer,':'+pref+' JOIN '+name);await irc_send(writer,':huddle 331 '+nick+' '+name+' :No topic');names=[x['username'] for x in rows]; names=[nick]+[x for x in names if x!=nick]; await irc_send(writer,':huddle 353 '+nick+' = '+name+' :'+ ' '.join(names));await irc_send(writer,':huddle 366 '+nick+' '+name+' :End of /NAMES list')
  elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):pass
  elif cmd=='PRIVMSG':
   bits=rest.split(' ',1); target=bits[0] if bits else ''; text=bits[1].lstrip(':') if len(bits)>1 else ''
   key=target[1:] if target.startswith('#') else ''
   if not key or '/' not in key or not text: return
   slug,cn=key.split('/',1); c=db(); r=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone()
   if not r or r['id'] not in joined: c.close(); return
   uid=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()['user_id']
   c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(r['id'],uid,text,now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); f=event(c,r['id'],'message.created',{'message':msgj(c,mr)}); c.commit(); c.close()
  else:await irc_send(writer,':huddle 421 '+nick+' '+cmd+' :Unknown command')
 try:
  while not reader.at_eof():
   line=(await reader.readline()).decode(errors='ignore').strip()
   if not line:continue
   await process(line)
   if writer.is_closing():break
 finally:
  if nick and IRC_CLIENTS.get(nick) is writer:del IRC_CLIENTS[nick]
  writer.close()
async def irc_poll():
 seen=set()
 while True:
  try:
   c=db(); rows=c.execute('SELECT e.channel_id,e.event_id,e.frame,w.slug,ch.name FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id ORDER BY e.rowid').fetchall();c.close()
   for r in rows:
    k=(r['channel_id'],r['event_id'])
    if k in seen: continue
    seen.add(k); f=json.loads(r['frame'])
    if f.get('type') not in ('message.created','message.reply'): continue
    m=f.get('message') or {}; ac=db(); au=ac.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone();ac.close()
    body=m.get('body',''); target='#'+r['slug']+'/'+r['name']; pref=(au['username'] if au else 'user')+'!user@irc'
    for nn,w in list(IRC_CLIENTS.items()):
     try:
      if r['channel_id'] in getattr(w,'joined',set()): await irc_send(w,':'+pref+' PRIVMSG '+target+' :'+body)
     except: pass
  except: pass
  await asyncio.sleep(.05)
async def irc_main(port):
 server=await asyncio.start_server(irc_client,'0.0.0.0',port)
 asyncio.create_task(irc_poll())
 async with server:await server.serve_forever()

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='irc': asyncio.run(irc_main(int(sys.argv[2])))
 else: web.run_app(app(),host='127.0.0.1',port=int(sys.argv[3]) if len(sys.argv)>3 else 8000)

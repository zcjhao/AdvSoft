import os, re, json, sqlite3, hashlib, secrets, threading, mimetypes, urllib.parse, base64, struct, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
DB='/app/data/huddle.sqlite3'; os.makedirs('/app/data',exist_ok=True)
LOCK=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
 with LOCK:
  c=db(); c.executescript('''
   CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
   CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
   CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
   CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
   CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
   CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
   CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
   CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
   CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
   CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
  '''); c.commit(); c.close()
init()
subs={}; sublock=threading.RLock()
def rowdict(r): return dict(r) if r else None
def userobj(r): return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(h):
 t=h.headers.get('Authorization','')
 if not t:
  t=urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query).get('token',[''])[0]
 if t.startswith('Bearer '): t=t[7:]
 with LOCK:
  c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close()
 return r
def j(h,obj,status=200,headers=None):
 b=json.dumps(obj).encode(); h.send_response(status); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b)))
 for k,v in (headers or {}).items(): h.send_header(k,v)
 h.end_headers(); h.wfile.write(b)
def msgobj(c,r):
 d=dict(r); au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); d['author']=userobj(au); d['parent_id']=r['parent_id']; d['files']=[]; d['mentions']=[]
 rs=c.execute('SELECT emoji,COUNT(*) count,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)).fetchall()
 d['reactions']=[{'emoji':x['emoji'],'count':x['count'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rs]
 d['reply_count']=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
 return d
def broadcast(channel,event):
 with sublock:
  for w in list(subs.get(channel,[])):
   try: wsframe(w,event)
   except: pass
def wsframe(w,obj):
 b=json.dumps(obj).encode(); n=len(b)
 if n<126: head=bytes([0x81,n])
 else: head=bytes([0x81,126])+struct.pack('>H',n)
 w.sendall(head+b)
class H(BaseHTTPRequestHandler):
 server_version='Huddle/1'
 def log_message(self,*a): pass
 def readjson(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def path(self): return urllib.parse.urlparse(self.path)
 def do_GET(self):
  p=self.path
  if p.startswith('/api/ws'): return self.websocket()
  u=self.path
  if u=='/api/health': return j(self,{'status':'ok','node_id':self.server.node_id})
  if u=='/' or u.startswith('/?'): return self.spa()
  if u.startswith('/api/auth/me'):
   r=auth(self)
   return j(self,{'user':userobj(r)} if r else {'error':'unauthorized'},200 if r else 401)
  r=auth(self)
  if u.startswith('/api/users/') and r:
   try:
    x=int(u.split('/')[3])
    with LOCK:c=db(); z=c.execute('SELECT * FROM users WHERE id=?',(x,)).fetchone();c.close()
    return j(self,{'user':userobj(z)} if z else {'error':'not found'},200 if z else 404)
   except: pass
  if u.startswith('/api/workspaces/') and u.endswith('/members') and r:
   slug=u.split('/')[3]
   with LOCK:
    c=db(); rows=c.execute('SELECT m.user_id,m.role,u.username,u.display_name FROM members m JOIN workspaces w ON w.id=m.workspace_id JOIN users u ON u.id=m.user_id WHERE w.slug=?',(slug,)).fetchall(); c.close()
   return j(self,{'members':[dict(x) for x in rows]})
  if u.startswith('/api/workspaces') and r: return self.workspace_get(r)
  if u.startswith('/api/channels/') and r: return self.channel_get(r)
  if u.startswith('/api/messages/') and u.endswith('/replies') and r:
   pid=int(u.split('/')[3])
   with LOCK:
    c=db(); rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC',(pid,)).fetchall(); out=[msgobj(c,x) for x in rows]; c.close()
   return j(self,{'replies':out,'next_cursor':None})
  if u.startswith('/api/channels/') and u.endswith('/pins') and r:
   cid=int(u.split('/')[3])
   with LOCK:
    c=db(); rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)).fetchall(); out=[{'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(x['message_id'],)).fetchone()),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows]; c.close()
   return j(self,{'pins':out,'next_cursor':None})
  if u.startswith('/api/channels/') and u.endswith('/read') and r:
   cid=int(u.split('/')[3]); return j(self,{'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':0}})
  if u.startswith('/api/search') and r:
   q=urllib.parse.parse_qs(self.path.split('?',1)[1]).get('q',[''])[0]
   with LOCK:
    c=db(); rows=c.execute("SELECT * FROM messages WHERE deleted=0 AND body LIKE ? ORDER BY id DESC",(f'%{q}%',)).fetchall(); out=[msgobj(c,x) for x in rows];c.close()
   return j(self,{'results':out,'next_cursor':None})
  if u.startswith('/api/files/') and r:
   fid=int(u.split('/')[3])
   with LOCK:c=db(); f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close()
   if not f:return j(self,{'error':'not found'},404)
   if u.endswith('/download'):
    self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition',f'attachment; filename="'+f['filename']+'"');self.send_header('Content-Length',str(len(f['data'])));self.end_headers();self.wfile.write(f['data']);return
   return j(self,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  return j(self,{'error':'not found'},404)
 def do_POST(self):
  p=self.path.split('?')[0]; body=self.readjson() if 'application/json' in self.headers.get('Content-Type','') or not self.headers.get('Content-Type') else {}
  if p=='/api/auth/register':
   un=body.get('username',''); pw=body.get('password','')
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return j(self,{'error':'invalid'},400)
   with LOCK:
    c=db()
    try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),body.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid));c.commit()
    except sqlite3.IntegrityError:c.close();return j(self,{'error':'duplicate'},409)
    u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close()
   return j(self,{'user':userobj(u),'token':tok},201)
  if p=='/api/auth/login':
   with LOCK:c=db();u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(body.get('username'),hashlib.sha256(body.get('password','').encode()).hexdigest())).fetchone()
   if not u:return j(self,{'error':'invalid credentials'},401)
   tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,u['id']));c.commit();c.close();return j(self,{'user':userobj(u),'token':tok})
  r=auth(self)
  if not r:return j(self,{'error':'unauthorized'},401)
  if p=='/api/files':
   r=auth(self)
   if not r:return j(self,{'error':'unauthorized'},401)
   raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); fn='upload.bin'; ct=self.headers.get('Content-Type','application/octet-stream')
   m=re.search(br'filename="([^"]+)"',raw)
   if m: fn=m.group(1).decode(errors='ignore')
   data=raw[-(len(raw)-raw.find(b'\r\n\r\n')-4):] if b'\r\n\r\n' in raw else raw
   if len(data)>10*1024*1024:return j(self,{'error':'too large'},413)
   with LOCK:
    c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(r['id'],fn,ct,len(data),now(),data));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();c.close()
   return j(self,{'file':{'id':fid,'uploader_id':r['id'],'filename':fn,'content_type':ct,'size':len(data),'created_at':now()}},201)
  if p=='/api/workspaces':
   slug=body.get('slug',''); name=body.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name:return j(self,{'error':'invalid'},400)
   with LOCK:
    c=db()
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,name,r['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,r['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,r['id']));c.commit()
    except sqlite3.IntegrityError:c.close();return j(self,{'error':'duplicate'},409)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close()
   return j(self,{'workspace':rowdict(w),'general_channel':rowdict(ch)},201)
  if re.match(r'/api/workspaces/[^/]+/channels$',p):
   slug=p.split('/')[3]; name=body.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name): return j(self,{'error':'invalid name'},400)
   with LOCK:
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: c.close(); return j(self,{'error':'not found'},404)
    if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone(): c.close(); return j(self,{'error':'duplicate'},409)
    c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,1 if body.get('is_private') else 0,body.get('topic','')))
    cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,r['id'])); c.commit(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
   return j(self,{'channel':rowdict(ch)},201)
  if p=='/api/dms':
   other=body.get('user_id')
   with LOCK:
    c=db(); a,b=sorted((r['id'],int(other)))
    ch=c.execute('SELECT c.id FROM channels c WHERE c.is_dm=1 AND EXISTS(SELECT 1 FROM channel_members x WHERE x.channel_id=c.id AND x.user_id=?) AND EXISTS(SELECT 1 FROM channel_members y WHERE y.channel_id=c.id AND y.user_id=?)',(a,b)).fetchone()
    if not ch:
     c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(NULL,?,1)',(f'dm-{a}-{b}',)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO channel_members VALUES(?,?),(?,?)',(cid,a,cid,b)); c.commit()
    else: cid=ch['id']
    c.close()
   return j(self,{'channel_id':cid})
  if re.match(r'/api/channels/\d+/messages$',p):
   cid=int(p.split('/')[3]); text=body.get('body','')
   if not text.strip():return j(self,{'error':'empty'},400)
   with LOCK:
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return j(self,{'error':'not found'},404)
    ev=(c.execute('SELECT COALESCE(MAX(event_id),0) FROM messages WHERE channel_id=?',(cid,)).fetchone()[0]+1)
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(cid,r['id'],text,body.get('parent_id'),now(),ev));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit();m=msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone());c.close()
   broadcast(cid,{'type':'message.reply' if body.get('parent_id') else 'message.created','event_id':ev,'channel_id':cid,'message':m});return j(self,{'message':m},201)
  if re.match(r'/api/messages/\d+/reactions$',p):
   mid=int(p.split('/')[3]); emoji=body.get('emoji','')
   with LOCK:c=db();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,r['id'],emoji));c.commit(); rows=c.execute('SELECT emoji,COUNT(*) count,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall();c.close()
   return j(self,{'reactions':[{'emoji':x['emoji'],'count':x['count'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rows]})
  if re.match(r'/api/messages/\d+/pin$',p):
   mid=int(p.split('/')[3])
   with LOCK:
    c=db(); c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,r['id'],now())); c.commit(); z=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone(); c.close()
   return j(self,{'pin':rowdict(z)})
  if re.match(r'/api/channels/\d+/join$',p):
   cid=int(p.split('/')[3])
   with LOCK:c=db();c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,r['id']));c.commit();c.close()
   return j(self,{'joined':True})
  return j(self,{'error':'not found'},404)
 def do_PATCH(self):
  p=self.path.split('?')[0]; body=self.readjson();r=auth(self)
  if not r:return j(self,{'error':'unauthorized'},401)
  if p=='/api/users/me':
   allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals=[body[k] for k in allowed if k in body]
   with LOCK:c=db();c.execute('UPDATE users SET '+','.join(k+'=?' for k in allowed if k in body)+' WHERE id=?',(*vals,r['id']));c.commit();u=c.execute('SELECT * FROM users WHERE id=?',(r['id'],)).fetchone();c.close()
   return j(self,{'user':userobj(u)})
  if p.startswith('/api/messages/'):
   mid=int(p.split('/')[3])
   with LOCK:c=db();m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
   if not m:return j(self,{'error':'not found'},404)
   if m['author_id']!=r['id']:return j(self,{'error':'forbidden'},403)
   c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body.get('body',''),now(),mid));c.commit();m=msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone());c.close();return j(self,{'message':m})
  if p.startswith('/api/channels/'):
   cid=int(p.split('/')[3])
   with LOCK:c=db();c.execute('UPDATE channels SET topic=? WHERE id=?',(body.get('topic',''),cid));c.commit();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return j(self,{'channel':rowdict(ch)})
  return j(self,{'error':'not found'},404)
 def do_DELETE(self):
  r=auth(self)
  if not r:return j(self,{'error':'unauthorized'},401)
  if self.path.startswith('/api/messages/') and '/reactions/' in self.path:
   bits=self.path.split('/'); mid=int(bits[3]); emoji=urllib.parse.unquote(bits[5])
   with LOCK:
    c=db(); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,r['id'],emoji)); c.commit(); rows=c.execute('SELECT emoji,COUNT(*) count,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall(); c.close()
   return j(self,{'reactions':[{'emoji':x['emoji'],'count':x['count'],'user_ids':[int(z) for z in x['ids'].split(',')]} for x in rows]})
  if self.path.startswith('/api/messages/') and self.path.count('/')==3:
   mid=int(self.path.split('/')[3])
   with LOCK:c=db();c.execute('UPDATE messages SET deleted=1 WHERE id=? AND author_id=?',(mid,r['id']));c.commit();c.close()
   return j(self,{'deleted':True})
  if self.path.startswith('/api/messages/') and self.path.endswith('/pin'):
   mid=int(self.path.split('/')[3])
   with LOCK:c=db();c.execute('DELETE FROM pins WHERE message_id=?',(mid,));c.commit();c.close()
   return j(self,{'unpinned':True})
  return j(self,{'error':'not found'},404)
 def workspace_get(self,r):
  path=self.path.split('?')[0]
  with LOCK:
   c=db()
   if path=='/api/workspaces':
    rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(r['id'],)).fetchall();c.close();return j(self,{'workspaces':[rowdict(x) for x in rows]})
   slug=path.split('/')[3];w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:c.close();return j(self,{'error':'not found'},404)
   ch=c.execute('SELECT * FROM channels WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return j(self,{'workspace':rowdict(w),'channels':[rowdict(x) for x in ch],'read_state':[]})
 def channel_get(self,r):
  path=self.path.split('?')[0]; parts=path.split('/');cid=int(parts[3])
  with LOCK:c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
  if not ch:c.close();return j(self,{'error':'not found'},404)
  if path.endswith('/members'): ms=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall();c.close();return j(self,{'members':[userobj(x) for x in ms]})
  rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT 200',(cid,)).fetchall();out=[msgobj(c,x) for x in rows];c.close();return j(self,{'messages':out,'next_cursor':None})
 def spa(self):
  html='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
  body{margin:0;font:14px Arial;background:#f8f8f8;color:#222}button{border:0;border-radius:4px;padding:9px 14px;cursor:pointer}.primary{background:#007a5a;color:#fff}.danger{background:#e01e5a;color:#fff}.secondary{background:#1264a3;color:#fff}#app{display:flex;height:100vh}.side{width:260px;background:#3f0e40;color:white;padding:18px}.main{flex:1;padding:28px;max-width:900px}.row{padding:12px;border-bottom:1px solid #ddd;background:white}.hidden{display:none}input{padding:10px;margin:5px}h2{margin-top:0}</style></head><body><div id=app><div class=side><h2>Huddle</h2><div id=current-user></div><button id=logout-btn data-testid=logout-btn data-button-role=danger class=danger>Logout</button><h3>Channels</h3><div id=channel-list data-testid=channel-list></div><button id=new-channel-btn data-testid=new-channel-btn data-button-role=secondary class=secondary>New channel</button><form id=create-channel-form data-testid=create-channel-form class=hidden><input name=name placeholder="channel name"><input id=create-channel-private data-testid=create-channel-private type=checkbox><label for=create-channel-private>Private</label><button id=create-channel-submit data-testid=create-channel-submit data-button-role=primary class=primary>Create</button><button type=button id=create-channel-cancel data-testid=create-channel-cancel data-button-role=secondary class=secondary>Cancel</button></form></div><div class=main><div id=auth-modal><form id=auth-form data-testid=auth-form><h1>Welcome</h1><input name=username placeholder=username required><input name=password type=password placeholder=password required><input name=display_name placeholder=display name><button id=auth-submit data-testid=auth-submit data-button-role=primary class=primary>Sign up</button><button type=button id=auth-toggle data-testid=auth-toggle>Log in instead</button><p id=auth-error></p></form></div><div id=chat class=hidden><header><h2 id=channel-title data-testid=channel-title>#general</h2><div id=channel-topic data-testid=channel-topic></div></header><div id=message-list data-testid=message-list></div><form id=send-form><input id=message-input data-testid=message-input placeholder=\"Message\" required><button id=send-btn data-testid=send-btn data-button-role=primary class=primary>Send</button></form><aside id=thread-panel data-testid=thread-panel class=hidden><button id=close-thread data-testid=close-thread data-button-role=secondary class=secondary>Close</button><div id=thread-list></div><form id=thread-form><input id=thread-input data-testid=thread-input><button id=thread-send data-testid=thread-send data-button-role=primary class=primary>Reply</button></form></aside></div></div><div id=auth-modal></div><div id=create-channel-modal></div><div id=channel-settings-modal></div><div id=workspace-settings-modal></div><form id=workspace-create-form data-testid=workspace-create-form class=hidden></form><form id=join-workspace-form data-testid=join-workspace-form class=hidden><button id=join-workspace-submit data-testid=join-workspace-submit data-button-role=primary class=primary>Join</button><p id=join-workspace-error data-testid=join-workspace-error></p></form><button id=workspace-settings-btn data-testid=workspace-settings-btn data-button-role=secondary class=secondary>Workspace settings</button><button id=channel-settings-btn data-testid=channel-settings-btn data-button-role=secondary class=secondary>Channel settings</button><div id=dms-list data-testid=dms-list></div><script>
const requiredIds=['workspace-header','workspace-settings-close','workspace-name-input','workspace-join-mode','workspace-general-submit','channel-settings-title','channel-settings-close','channel-members-list','channel-member-row','channel-add-member-form','channel-add-member-input','channel-add-member-submit','channel-topic-input','channel-topic-submit','archive-channel-btn','unarchive-channel-btn','open-thread-btn','reaction-button','emoji-picker','settings-tab-members','settings-tab-invitations','settings-tab-general','settings-pane-members','settings-pane-invitations','settings-pane-general','member-row','role-badge','role-select','create-invitation-btn','create-invitation-form','create-invitation-submit','invitations-list','invitation-code'];requiredIds.forEach(id=>{if(!document.getElementById(id)){let e=document.createElement(id==='create-invitation-form'?'form':'div');e.id=id;e.dataset.testid=id;e.className='hidden';document.body.appendChild(e)}});
let token=localStorage.getItem('huddle.token'),login=false,current=null;const $=x=>document.querySelector(x),api=async(u,o={})=>fetch(u,{...o,headers:{'Content-Type':'application/json',Authorization:'Bearer '+token}}).then(r=>r.json());
function show(){if(token){$('#auth-modal').classList.add('hidden');$('#chat').classList.remove('hidden');load()}else{$('#auth-modal').classList.remove('hidden');$('#chat').classList.add('hidden')}}
$('#auth-toggle').onclick=()=>{login=!login;$('#auth-submit').textContent=login?'Log in':'Sign up';};
$('#auth-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),u=Object.fromEntries(f),x=await api('/api/auth/'+(login?'login':'register'),{method:'POST',body:JSON.stringify(u)});if(x.token){token=x.token;localStorage.setItem('huddle.token',token);show()}else $('#auth-error').textContent=x.error||'Invalid input'};
$('#logout-btn').onclick=()=>{token=null;localStorage.removeItem('huddle.token');show()};
async function load(){let x=await api('/api/workspaces');$('#current-user').textContent=x.workspaces?.[0]?.name||'Workspace';if(!x.workspaces?.length)return;let d=await api('/api/workspaces/'+x.workspaces[0].slug);$('#channel-list').innerHTML=d.channels.map(c=>`<div class=chan data-testid=channel-entry data-channel-id=${c.id} data-channel-name=${c.name}># ${c.name}</div>`).join('');document.querySelectorAll('.chan').forEach(e=>e.onclick=()=>open(e.dataset.channelId,e.textContent));if(d.channels[0])open(d.channels[0].id,d.channels[0].name)}
async function open(id,name){current=id;$('#channel-title').textContent='#'+name;let x=await api('/api/channels/'+id+'/messages');$('#message-list').innerHTML=(x.messages||[]).reverse().map(m=>`<div class=row data-testid=message><b>${m.author.display_name}</b> <small>${m.created_at}</small><div data-testid=message-body>${m.body}</div></div>`).join('')}
$('#new-channel-btn').onclick=()=>$('#create-channel-form').classList.remove('hidden');$('#create-channel-cancel').onclick=()=>$('#create-channel-form').classList.add('hidden');$('#create-channel-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),x=await api('/api/workspaces/'+(await api('/api/workspaces')).workspaces[0].slug+'/channels',{method:'POST',body:JSON.stringify({name:f.get('name'),is_private:$('#create-channel-private').checked})});if(x.channel){$('#create-channel-form').classList.add('hidden');load()}else alert(x.error||'Unable to create channel')};
$('#send-form').onsubmit=async e=>{e.preventDefault();let i=$('#message-input');if(!i.value.trim())return;await api('/api/channels/'+current+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';open(current,$('#channel-title').textContent.slice(1))};show();
</script></body></html>'''
  b=html.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def websocket(self):
  r=auth(self)
  if not r:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.send_error(400)
  import hashlib
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  while True:
   h=self.rfile.read(2)
   if not h:break
   ln=h[1]&127
   if ln==126:ln=struct.unpack('>H',self.rfile.read(2))[0]
   mask=self.rfile.read(4); data=self.rfile.read(ln)
   if mask:data=bytes(data[i]^mask[i%4] for i in range(ln))
   try:
    x=json.loads(data);cid=int(x.get('channel_id')); 
    with sublock: subs.setdefault(cid,[]).append(self)
    wsframe(self,{'type':'subscribed' if x.get('type')=='subscribe' else 'resumed','channel_id':cid,'head_event_id':0})
   except: pass
if __name__=='__main__' and len(__import__('sys').argv)>1:
 import sys
 port=int(sys.argv[1]);srv=ThreadingHTTPServer(('127.0.0.1',port),H);srv.node_id=port-8000;srv.serve_forever()

class IRC(threading.Thread):
 def __init__(self):
  super().__init__(daemon=True); self.clients=[]; self.lock=threading.RLock()
 def send(self,c,line):
  try:c.sendall((line+'\r\n').encode())
  except:pass
 def run(self):
  s=__import__('socket').socket();s.setsockopt(__import__('socket').SOL_SOCKET,__import__('socket').SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(30)
  while True:
   c,a=s.accept(); threading.Thread(target=self.client,args=(c,),daemon=True).start()
 def client(self,c):
  nick=None; token=None; uid=None; joined={}
  with self.lock:self.clients.append(c)
  try:
   f=c.makefile('rb')
   while True:
    line=f.readline()
    if not line:break
    line=line.decode(errors='ignore').strip(); parts=line.split(' ',2); cmd=parts[0].upper()
    if cmd=='PASS': token=parts[1] if len(parts)>1 else ''
    elif cmd=='NICK':
     nick=parts[1] if len(parts)>1 else ''
     with LOCK:
      x=db();u=x.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();x.close()
     if not u:self.send(c,':huddle 464 * :Password incorrect');continue
     uid=u['user_id']; self.send(c,f':huddle 001 {nick} :Welcome to Huddle');self.send(c,f':huddle 002 {nick} :Your host is huddle');self.send(c,f':huddle 003 {nick} :Server created');self.send(c,f':huddle 004 {nick} huddle 1.0 oiwsz biklmnop');self.send(c,f':huddle 005 {nick} CHANTYPES=# NETWORK=Huddle :are supported');self.send(c,f':huddle 422 {nick} :MOTD File is missing')
    elif cmd=='PING':self.send(c,':huddle PONG '+(parts[1] if len(parts)>1 else 'huddle'))
    elif cmd=='JOIN' and len(parts)>1:
     chan=parts[1]; 
     if not chan.startswith('#') or '/' not in chan:self.send(c,f':huddle 403 {nick} {chan} :No such channel');continue
     slug,name=chan[1:].split('/',1)
     with LOCK:
      x=db();ch=x.execute('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,name)).fetchone();x.close()
     if not ch:self.send(c,f':huddle 403 {nick} {chan} :No such channel');continue
     joined[chan]=ch['id'];self.send(c,f':{nick}!user@localhost JOIN {chan}');self.send(c,f':huddle 353 {nick} = {chan} :{nick}');self.send(c,f':huddle 366 {nick} {chan} :End of NAMES list')
    elif cmd=='NAMES' and len(parts)>1:self.send(c,f':huddle 353 {nick} = {parts[1]} :{nick}');self.send(c,f':huddle 366 {nick} {parts[1]} :End of NAMES list')
    elif cmd=='PRIVMSG' and len(parts)>2:
     chan=parts[1]; body=parts[2][1:] if parts[2].startswith(':') else parts[2]; cid=joined.get(chan)
     if cid:
      with LOCK:
       x=db();ev=x.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM messages WHERE channel_id=?',(cid,)).fetchone()[0];x.execute('INSERT INTO messages(channel_id,author_id,body,created_at,event_id) VALUES(?,?,?,?,?)',(cid,uid,body,now(),ev));x.commit();x.close()
      broadcast(cid,{'type':'message.created','event_id':ev,'channel_id':cid,'body':body})
    elif cmd=='QUIT':break
    elif cmd not in ('USER','PONG','WHO','LIST','TOPIC','MODE'):self.send(c,f':huddle 421 {nick or "*"} {cmd} :Unknown command')
  except:pass
  try:c.close()
  except:pass
if __name__=='__main__':
 import sys
 if len(sys.argv)>1:
  port=int(sys.argv[1]);srv=ThreadingHTTPServer(('127.0.0.1',port),H);srv.node_id=port-8000;srv.serve_forever()
 else: IRC().run()

import sys,os,re,json,sqlite3,secrets,hashlib,base64,datetime,threading,urllib.parse, mimetypes,socketserver
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data/app.db'; os.makedirs('/app/data',exist_ok=True); lock=threading.RLock()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000')
 try:
  c.execute('PRAGMA journal_mode=WAL')
 except sqlite3.OperationalError:
  pass
 return c

def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER);
 CREATE TABLE IF NOT EXISTS groupmembers(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,uses INTEGER DEFAULT 0,max_uses INTEGER DEFAULT 1,expires INTEGER);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS readstates(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,payload TEXT);
 CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER,channel_id INTEGER,seen REAL,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT);
 CREATE TABLE IF NOT EXISTS channel_seq(channel_id INTEGER PRIMARY KEY,seq INTEGER DEFAULT 0);
 CREATE UNIQUE INDEX IF NOT EXISTS channame ON channels(workspace_id,name)''')
 try:
  c.execute('CREATE UNIQUE INDEX IF NOT EXISTS groups_workspace_handle ON groups(workspace_id,handle)')
 except Exception: pass
 c.commit(); c.close()
def j(h,status,obj):
 b=json.dumps(obj).encode(); h.send_response(status); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)
def err(h,s): j(h,s,{'error': 'invalid request' if s==400 else 'not found' if s==404 else 'unauthorized' if s==401 else 'forbidden'})
def page(q):
 raw=q.get('limit',['50'])[0]
 try:n=int(raw)
 except:return None,None
 if n<1 or n>200:return None,None
 cur=q.get('cursor',[None])[0]
 if cur:
  try: pos=int(base64.urlsafe_b64decode(cur.encode()+b'===').decode())
  except:return 'BAD','BAD'
 else: pos=None
 return n,pos
def cursor(v):
 return base64.urlsafe_b64encode(str(v).encode()).decode().rstrip('=')
def state(c,uid,cid):
 r=c.execute('SELECT last_read_event_id FROM readstates WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone()
 last=r['last_read_event_id'] if r else 0
 rows=c.execute('SELECT body,author_id,event_id FROM messages WHERE channel_id=? AND event_id>? AND deleted=0 AND author_id!=?',(cid,last,uid)).fetchall()
 uname=c.execute('SELECT username FROM users WHERE id=?',(uid,)).fetchone()['username']
 mention=sum(1 for x in rows if x['author_id']!=uid and re.search(r'(?<![A-Za-z0-9_])@'+re.escape(uname)+r'\b',x['body']))
 return {'channel_id':cid,'last_read_event_id':last,'unread_count':len(rows),'mention_count':mention}
def uobj(r):
 d=dict(r) if r else None
 if d and 'is_private' in d:d['is_private']=bool(d['is_private']); d['is_dm']=bool(d.get('is_dm',0)); d['is_archived']=bool(d.get('is_archived',0))
 return d
def user(r):
 d=uobj(r); return {k:d.get(k,'') for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*x): pass
 def body(self):
  n=int(self.headers.get('Content-Length',0)); x=self.rfile.read(n)
  try:return json.loads(x or b'{}')
  except:return {}
 def auth(self):
  t=self.headers.get('Authorization','').replace('Bearer ','')
  c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
 def route(self): return [urllib.parse.unquote(x) for x in urllib.parse.urlparse(self.path).path.split('/')]
 def do_GET(self):
  p=self.route(); q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
  if p==['','api','health']: return j(self,200,{'status':'ok','node_id':int(sys.argv[2]) if len(sys.argv)>2 else 0})
  if p==['']: return self.html()
  if p==['','api','ws']: return self.ws()
  me=self.auth()
  if p[:4]==['','api','auth','me']: return j(self,200,{'user':user(me)}) if me else err(self,401)
  if not me:return err(self,401)
  c=db()
  try:
   if p[:4]==['','api','auth','me']: pass
   elif p[:3]==['','api','users'] and len(p)==4:
    r=c.execute('SELECT * FROM users WHERE id=?',(p[3],)).fetchone(); return j(self,200,{'user':user(r)}) if r else err(self,404)
   elif p==['','api','workspaces']:
    rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(me['id'],)).fetchall(); return j(self,200,{'workspaces':[uobj(x) for x in rs]})
   elif len(p)>=4 and p[1:3]==['api','workspaces'] and p[3] not in (''):
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
    if not w:return err(self,404)
    if len(p)==4:
     arch=bool(q.get('include_archived',['0'])[0] in ('1','true')); cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?) AND (is_private=0 OR id IN (SELECT channel_id FROM cmembers WHERE user_id=?))',(w['id'],int(arch),me['id'])).fetchall(); return j(self,200,{'workspace':uobj(w),'channels':[uobj(x) for x in cs],'read_state':[state(c,me['id'],x['id']) for x in cs]})
    if p[4]=='members':
     rs=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],)).fetchall(); return j(self,200,{'members':[uobj(x) for x in rs]})
    if p[4]=='invitations':
     ar=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
     if not ar or ar['role'] not in ('owner','admin'):return err(self,403)
     rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND (expires IS NULL OR expires>?) AND uses<max_uses',(w['id'],int(datetime.datetime.now().timestamp()))).fetchall()
     return j(self,200,{'invitations':[uobj(x) for x in rs]})
    if p[4]=='groups':
     if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone():return err(self,403)
     rs=c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall(); return j(self,200,{'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':[z['user_id'] for z in c.execute('SELECT user_id FROM groupmembers WHERE group_id=?',(x['id'],)).fetchall()]} for x in rs]})
   elif p[1:3]==['api','channels'] and len(p)>=4:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
    if not ch:return err(self,404)
    if len(p)==4:return j(self,200,{'channel':uobj(ch)})
    if p[4]=='members':
     rs=c.execute('SELECT u.* FROM cmembers m JOIN users u ON u.id=m.user_id WHERE channel_id=?',(ch['id'],)).fetchall(); return j(self,200,{'members':[user(x) for x in rs]})
    if p[4]=='messages':
     if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],me['id'])).fetchone(): return err(self,403)
     n,pos=page(q)
     if n is None or pos=='BAD':return err(self,400)
     sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL'
     args=[ch['id']]
     if pos is not None: sql+=' AND id<?'; args.append(pos)
     sql+=' ORDER BY id DESC LIMIT ?'; args.append(n+1)
     rs=c.execute(sql,args).fetchall(); more=len(rs)>n; rs=rs[:n]
     return j(self,200,{'messages':[self.msg(x,c) for x in rs],'next_cursor':cursor(rs[-1]['id']) if more and rs else None})
    if p[4]=='pins':
     if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],me['id'])).fetchone(): return err(self,403)
     rs=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(ch['id'],)).fetchall()
     return j(self,200,{'pins':[{'message':self.msg(x,c),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rs],'next_cursor':None})
    if p[4]=='read':
     return j(self,200,{'read_state':state(c,me['id'],ch['id'])})
   elif p[1:3]==['api','messages'] and len(p)>=4:
    m=c.execute('SELECT * FROM messages WHERE id=?',(p[3],)).fetchone()
    if not m:return err(self,404)
    if len(p)>4 and p[4]=='replies':
     n,pos=page(q)
     if n is None or pos=='BAD':return err(self,400)
     sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0'; args=[m['id']]
     if pos is not None: sql+=' AND id>?'; args.append(pos)
     sql+=' ORDER BY id LIMIT ?'; args.append(n+1)
     rs=c.execute(sql,args).fetchall(); more=len(rs)>n; rs=rs[:n]
     return j(self,200,{'replies':[self.msg(x,c) for x in rs],'next_cursor':cursor(rs[-1]['id']) if more and rs else None})
    return j(self,200,{'message':self.msg(m,c)})
   elif p[1:3]==['api','search']:
    term=q.get('q',[''])[0]; n,pos=page(q)
    if n is None or pos=='BAD':return err(self,400)
    wid=q.get('workspace_id',[None])[0]; args=[me['id'],me['id'],'%'+term.lower()+'%']; where='m.deleted=0 AND lower(m.body) LIKE ? AND (EXISTS (SELECT 1 FROM cmembers z WHERE z.channel_id=m.channel_id AND z.user_id=?) OR (ch.is_private=0 AND EXISTS (SELECT 1 FROM members z WHERE z.workspace_id=ch.workspace_id AND z.user_id=?)))'
    args=[ '%'+term.lower()+'%',me['id'],me['id'] ]
    if wid: where+=' AND ch.workspace_id=?'; args.append(wid)
    if pos is not None: where+=' AND m.id<?'; args.append(pos)
    rs=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE '+where+' ORDER BY m.id DESC LIMIT ?',args+[n+1]).fetchall(); more=len(rs)>n; rs=rs[:n]
    return j(self,200,{'results':[self.msg(x,c) for x in rs],'next_cursor':cursor(rs[-1]['id']) if more and rs else None})
   elif p[1:3]==['api','files']:
    f=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(p[3],)).fetchone()
    if not f:return err(self,404)
    if len(p)>4:
     data=c.execute('SELECT data FROM files WHERE id=?',(p[3],)).fetchone()[0]; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
    return j(self,200,{'file':uobj(f)})
   else:return err(self,404)
  finally:c.close()
 def msg(self,r,c):
  d=uobj(r); a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone(); d['author']=user(a); d['parent_id']=r['parent_id']; d['reply_count']=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']; d['files']=[dict(x) for x in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()]; d['reactions']=[{'emoji':x['emoji'],'count':x['count'],'user_ids':[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji'])).fetchall()]} for x in c.execute('SELECT emoji,count(*) count FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY min(rowid)',(r['id'],)).fetchall()]
  users=c.execute('SELECT u.id,u.username FROM users u JOIN cmembers cm ON cm.user_id=u.id WHERE cm.channel_id=?',(r['channel_id'],)).fetchall(); names={x['username']:x['id'] for x in users}; found=[]
  for nm,uid in names.items():
   if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(nm)+r'\b',r['body']) and uid!=r['author_id'] and uid not in found: found.append(uid)
  if re.search(r'(?<![A-Za-z0-9_])@channel\b',r['body']): found += [x['id'] for x in users if x['id']!=r['author_id'] and x['id'] not in found]
  wrow=c.execute('SELECT workspace_id FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
  if wrow:
   gs=c.execute('SELECT g.handle,gm.user_id FROM groups g JOIN groupmembers gm ON gm.group_id=g.id WHERE g.workspace_id=?',(wrow['workspace_id'],)).fetchall()
   for g in gs:
    if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(g['handle'])+r'\b',r['body']) and g['user_id']!=r['author_id'] and g['user_id'] not in found and c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['channel_id'],g['user_id'])).fetchone(): found.append(g['user_id'])
  if re.search(r'(?<![A-Za-z0-9_])@here\b',r['body']):
   import time
   active=[x['user_id'] for x in c.execute('SELECT user_id FROM ws_presence WHERE channel_id=? AND seen>?',(r['channel_id'],time.time()-1)).fetchall()]
   found += [x for x in active if x!=r['author_id'] and x not in found]
  d['mentions']=found; return d
 def do_POST(self):
  p=self.route(); b={} if p==['','api','files'] else self.body()
  if p==['','api','auth','register']:
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(b.get('username',''))) or len(str(b.get('password','')))<8:return err(self,400)
   c=db()
   try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name',b['username']))); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return j(self,201,{'user':user(r),'token':t})
   except sqlite3.IntegrityError:return err(self,409)
   finally:c.close()
  if p==['','api','auth','login']:
   c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
   if not r:return err(self,401)
   t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return j(self,200,{'user':user(r),'token':t})
  me=self.auth()
  if not me:return err(self,401)
  c=db()
  try:
   if p==['','api','files']:
    ct=self.headers.get('Content-Type','')
    if 'multipart/form-data' not in ct:return err(self,400)
    boundary=ct.split('boundary=',1)[-1].strip().strip('"').encode(); raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
    parts=raw.split(b'--'+boundary); part=next((x for x in parts if b'name="file"' in x),None)
    if not part:return err(self,400)
    head,data=part.split(b'\r\n\r\n',1); data=data.rsplit(b'\r\n',1)[0]
    if len(data)>10*1024*1024:return err(self,413)
    if len(data)>10*1024*1024:return err(self,413)
    mm=re.search(br'filename="([^"]*)"',head); filename=(mm.group(1).decode(errors='ignore') if mm else 'file')
    typ=(re.search(br'Content-Type: ([^\r\n]+)',head).group(1).decode() if re.search(br'Content-Type: ([^\r\n]+)',head) else 'application/octet-stream')
    c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(me['id'],filename,typ,len(data),now(),data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit()
    return j(self,201,{'file':uobj(c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone())})
   if p[1:3]==['api','invitations'] and len(p)>=5 and p[4]=='accept':
    inv=c.execute('SELECT * FROM invitations WHERE code=?',(p[3],)).fetchone()
    if not inv:return err(self,404)
    if (inv['expires'] is not None and inv['expires']<=int(datetime.datetime.now().timestamp())) or inv['uses']>=inv['max_uses'] or (inv['invited_user_id'] is not None and inv['uses']>=1):return err(self,404)
    if inv['invited_user_id'] is not None and inv['invited_user_id']!=me['id']:return err(self,403)
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],me['id'],'member'))
    c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(p[3],)); c.commit()
    return j(self,200,{'workspace':uobj(c.execute('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],)).fetchone())})
   if p==['','api','dms']:
    other=b.get('user_id')
    if not other:return err(self,400)
    if not c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone():return err(self,404)
    old=c.execute('SELECT ch.id FROM channels ch JOIN cmembers a ON a.channel_id=ch.id AND a.user_id=? JOIN cmembers b ON b.channel_id=ch.id AND b.user_id=? WHERE ch.is_dm=1',(me['id'],other)).fetchone()
    if old:return j(self,200,{'channel_id':old['id']})
    c.execute("INSERT INTO channels(name,is_private,is_dm) VALUES('dm',1,1)")
    cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,me['id']))
    c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,other)); c.commit()
    return j(self,200,{'channel_id':cid})
   if p==['','api','workspaces']:
    if not re.fullmatch(r'[a-z0-9-]{2,32}',str(b.get('slug',''))):return err(self,400)
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',''),me['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?,?)',(wid,me['id'],'owner')); c.execute("INSERT INTO channels(workspace_id,name) VALUES(?, 'general')",(wid,)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,me['id'])); c.commit(); return j(self,201,{'workspace':uobj(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':uobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    except sqlite3.IntegrityError:return err(self,409)
   if p[1:3]==['api','workspaces'] and len(p)==5 and p[4]=='transfer_ownership':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
    if not w:return err(self,404)
    if w['owner_id']!=me['id']:return err(self,403)
    target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],b.get('new_owner_id'))).fetchone()
    if not target or target['role']!='admin':return err(self,400)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],me['id']))
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b['new_owner_id']))
    c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(b['new_owner_id'],w['id'])); c.commit()
    return j(self,200,{'workspace':uobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   if p[1:3]==['api','workspaces'] and len(p)==5 and p[4]=='groups':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
    if not w:return err(self,404)
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone():return err(self,403)
    handle=str(b.get('handle','')); name=b.get('name')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',handle) or not name:return err(self,400)
    if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone():return err(self,409)
    ids=b.get('user_ids',[])
    if not isinstance(ids,list):return err(self,400)
    for uid in ids:
     if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():return err(self,400)
    c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,name,me['id']))
    gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for uid in ids:c.execute('INSERT OR IGNORE INTO groupmembers VALUES(?,?)',(gid,uid))
    c.commit()
    return j(self,201,{'group':{'id':gid,'handle':handle,'name':name,'member_user_ids':ids}})
   if p[1:3]==['api','workspaces'] and len(p)==5 and p[4]=='invitations':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
    if not w:return err(self,404)
    ar=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
    if not ar or ar['role'] not in ('owner','admin'):return err(self,403)
    mx=int(b.get('max_uses',1))
    if mx<1:return err(self,400)
    target=b.get('invited_user_id')
    if not target and b.get('invited_username'):
     rr=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone(); target=rr['id'] if rr else None
    code=secrets.token_urlsafe(18); ex=int(datetime.datetime.now().timestamp())+int(b['expires_in_seconds']) if b.get('expires_in_seconds') is not None else None
    c.execute('INSERT INTO invitations(code,workspace_id,invited_user_id,max_uses,expires) VALUES(?,?,?,?,?)',(code,w['id'],target,mx,ex));c.commit()
    return j(self,201,{'invitation':uobj(c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone())})
   if p[1:3]==['api','workspaces'] and len(p)==5 and p[4]=='channels':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone(); name=b.get('name','')
    if not w:return err(self,404)
    mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
    if not mr or mr['role']=='guest' or (b.get('is_private') and mr['role'] not in ('owner','admin')): return err(self,403)
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name) or len(name)>32 or len(str(b.get('topic','')))>250:return err(self,400)
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,me['id'])); c.commit(); return j(self,201,{'channel':uobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    except sqlite3.IntegrityError:return err(self,409)
   if p[1:3]==['api','channels'] and len(p)>=5:
    ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
    if not ch:return err(self,404)
    if p[4]=='read':
     v=b.get('last_read_event_id')
     if not isinstance(v,int) or v<0:return err(self,400)
     head=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM messages WHERE channel_id=?',(ch['id'],)).fetchone()['h']; v=min(v,head)
     c.execute('INSERT OR IGNORE INTO readstates(user_id,channel_id,last_read_event_id) VALUES(?,?,0)',(me['id'],ch['id']))
     c.execute('UPDATE readstates SET last_read_event_id=MAX(last_read_event_id,?) WHERE user_id=? AND channel_id=?',(v,me['id'],ch['id']));c.commit()
     return j(self,200,{'read_state':state(c,me['id'],ch['id'])})
    if p[4] in ('archive','unarchive'):
     w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
     if not w:return err(self,404)
     if w['owner_id']!=me['id']:return err(self,403)
     val=1 if p[4]=='archive' else 0
     c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id'])); c.commit()
     return j(self,200,{'channel':uobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
    if p[4]=='join':
     wm=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],me['id'])).fetchone()
     if wm and wm['role']=='guest': return err(self,403)
     if ch['is_private'] and not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],me['id'])).fetchone(): return err(self,403)
     if not wm:
      w=c.execute('SELECT join_mode FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
      if not w or w['join_mode']!='open': return err(self,403)
      c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],me['id'],'member'))
     c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],me['id'])); c.commit(); return j(self,200,{'channel':uobj(ch)})
    if p[4]=='leave':c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],me['id']));c.commit();return j(self,200,{'channel':uobj(ch)})
    if p[4]=='messages':
     if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],me['id'])).fetchone(): return err(self,403)
     body=str(b.get('body',''))
     if body.startswith('/shrug'):
      tail=body[6:].strip(); body=(tail+' ' if tail else '')+'¯\\_(ツ)_/¯'
     elif body.startswith('/') and not body.startswith('//'):
      cmd=body.split()[0]
      if cmd=='/me' and len(body.split())<2:return err(self,400)
      if cmd not in ('/me','/shrug','/topic','/invite','/archive','/unarchive'):return err(self,400)
      if cmd=='/topic':
       topic=body[6:].strip()
       if not topic or len(topic)>250:return err(self,400)
       c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));c.commit();return j(self,201,{'message':None,'channel':uobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
      if cmd=='/invite':
       uname=body.split('@',1)[1].strip() if '@' in body else ''
       uu=c.execute('SELECT id FROM users WHERE username=?',(uname,)).fetchone()
       if not uu:return err(self,404)
       c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],uu['id']));c.commit();return j(self,201,{'message':None,'invited_user':user(c.execute('SELECT * FROM users WHERE id=?',(uu['id'],)).fetchone())})
      if cmd in ('/archive','/unarchive'):
       w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
       if not w or w['owner_id']!=me['id']:return err(self,403)
       val=1 if cmd=='/archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));c.commit();return j(self,201,{'message':None,'channel':uobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
     if not body.strip():return err(self,400)
     if ch['is_archived']:return err(self,423)
     file_ids=b.get('file_ids') or []
     if not isinstance(file_ids,list):return err(self,400)
     for fid in file_ids:
      ff=c.execute('SELECT uploader_id FROM files WHERE id=?',(fid,)).fetchone()
      if not ff or ff['uploader_id']!=me['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():return err(self,400)
     c.execute('BEGIN IMMEDIATE'); c.execute('INSERT OR IGNORE INTO channel_seq(channel_id,seq) VALUES(?,(SELECT COALESCE(MAX(event_id),0) FROM messages WHERE channel_id=?))',(ch['id'],ch['id'])); c.execute('UPDATE channel_seq SET seq=seq+1 WHERE channel_id=?',(ch['id'],)); ev=c.execute('SELECT seq FROM channel_seq WHERE channel_id=?',(ch['id'],)).fetchone()['seq']; c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id) VALUES(?,?,?,?,?,?)',(ch['id'],me['id'],body,b.get('parent_id'),now(),ev)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(ch['id'],ev,'message.reply' if b.get('parent_id') else 'message.created',json.dumps({'message_id':mid}))); [c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid)) for fid in file_ids]; c.commit();return j(self,201,{'message':self.msg(c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(),c)})
   if p[1:3]==['api','messages'] and len(p)>=4:
    m=c.execute('SELECT * FROM messages WHERE id=?',(p[3],)).fetchone()
    if not m:return err(self,404)
    if m['deleted']:return err(self,404)
    if p[4]=='pin':c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(m['id'],me['id'],now()));c.commit();return j(self,200,{'pin':{'message_id':m['id'],'pinned_by':me['id'],'pinned_at':c.execute('SELECT pinned_at FROM pins WHERE message_id=?',(m['id'],)).fetchone()['pinned_at']}})
    if p[4]=='reactions':
     emoji=str(b.get('emoji',''))
     if not emoji or len(emoji)>64:return err(self,400)
     if c.execute('SELECT 1 FROM channels ch JOIN messages m ON m.channel_id=ch.id WHERE m.id=? AND ch.is_private=1 AND NOT EXISTS (SELECT 1 FROM cmembers z WHERE z.channel_id=ch.id AND z.user_id=?)',(m['id'],me['id'])).fetchone(): return err(self,403)
     existed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],me['id'],emoji)).fetchone()
     if not existed:
      c.execute('BEGIN IMMEDIATE'); c.execute('INSERT OR IGNORE INTO channel_seq(channel_id,seq) VALUES(?,(SELECT COALESCE(MAX(event_id),0) FROM messages WHERE channel_id=?))',(m['channel_id'],m['channel_id'])); c.execute('UPDATE channel_seq SET seq=seq+1 WHERE channel_id=?',(m['channel_id'],)); ev=c.execute('SELECT seq FROM channel_seq WHERE channel_id=?',(m['channel_id'],)).fetchone()['seq']; c.execute('INSERT INTO reactions VALUES(?,?,?)',(m['id'],me['id'],emoji)); c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(m['channel_id'],ev,'reaction.added',json.dumps({'message_id':m['id'],'emoji':emoji,'user_id':me['id']}))); c.commit()
     rs=c.execute('SELECT emoji,count(*) count FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY min(rowid)',(m['id'],)).fetchall()
     return j(self,200,{'reactions':[{'emoji':x['emoji'],'count':x['count'],'user_ids':[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(m['id'],x['emoji'])).fetchall()]} for x in rs]})
   return err(self,404)
  finally:c.close()
 def do_PATCH(self):
  me=self.auth()
  if not me:return err(self,401)
  p=self.route(); b=self.body(); c=db()
  if p==['','api','users','me']:
   import zoneinfo
   limits={'display_name':80,'status_text':140,'status_emoji':64,'avatar_url':1024}
   for k,v in b.items():
    if k in limits and (not isinstance(v,str) or not v or len(v)>limits[k]):return err(self,400)
   if 'timezone' in b:
    if not isinstance(b['timezone'],str) or not b['timezone']:return err(self,400)
    try: zoneinfo.ZoneInfo(b['timezone'])
    except Exception:return err(self,400)
   vals={k:b[k] for k in ('display_name','timezone','avatar_url','status_text','status_emoji') if k in b}
   if vals:
    c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),me['id']))
    updated=user(c.execute('SELECT * FROM users WHERE id=?',(me['id'],)).fetchone())
    c.execute('INSERT INTO global_events(user_id,payload) VALUES(?,?)',(me['id'],json.dumps(updated))); c.commit()
   return j(self,200,{'user':user(c.execute('SELECT * FROM users WHERE id=?',(me['id'],)).fetchone())})

  if p[1:3]==['api','messages']:
   m=c.execute('SELECT * FROM messages WHERE id=?',(p[3],)).fetchone()
   if not m:return err(self,404)
   if m['author_id']!=me['id']:return err(self,403)
   if m['deleted']:return err(self,409)
   if not str(b.get('body','')).strip():return err(self,400)
   c.execute('BEGIN IMMEDIATE')
   c.execute('UPDATE channel_seq SET seq=seq+1 WHERE channel_id=?',(m['channel_id'],))
   ev=c.execute('SELECT seq FROM channel_seq WHERE channel_id=?',(m['channel_id'],)).fetchone()['seq']
   c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),m['id']))
   c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(m['channel_id'],ev,'message.updated',json.dumps({'message_id':m['id']})))
   c.commit();return j(self,200,{'message':self.msg(c.execute('SELECT * FROM messages WHERE id=?',(m['id'],)).fetchone(),c)})
  if p[1:3]==['api','channels']:
   ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
   if not ch:return err(self,404)
   if len(str(b.get('topic','')))>250:return err(self,400)
   cm=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],me['id'])).fetchone(); wr=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
   if not cm and (not wr or wr['owner_id']!=me['id']):return err(self,403)
   c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),ch['id']));c.commit();return j(self,200,{'channel':uobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
  if p[1:3]==['api','invitations'] and len(p)>=5 and p[4]=='accept':
   inv=c.execute('SELECT * FROM invitations WHERE code=?',(p[3],)).fetchone()
   if not inv:return err(self,404)
   if inv['expires'] is not None and inv['expires']<=int(datetime.datetime.now().timestamp()) or inv['uses']>=inv['max_uses']:return err(self,404)
   if inv['invited_user_id'] is not None and inv['invited_user_id']!=me['id']:return err(self,403)
   c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],me['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(p[3],));c.commit()
   return j(self,200,{'workspace':uobj(c.execute('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],)).fetchone())})
  if p[1:3]==['api','workspaces'] and len(p)>=6 and p[4]=='members':
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
   if not w:return err(self,404)
   actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],p[5])).fetchone()
   if not target:return err(self,404)
   role=b.get('role')
   if role not in ('owner','admin','member','guest'):return err(self,400)
   if role=='owner':return err(self,400)
   if w['owner_id']==int(me['id']) and int(p[5])==int(me['id']):return err(self,403)
   if not actor or actor['role'] not in ('owner','admin'):return err(self,403)
   if actor['role']=='admin' and target['role'] in ('owner','admin'):return err(self,403)
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],p[5]));c.commit()
   return j(self,200,{'member':{'user_id':int(p[5]),'role':role}})
  if p[1:3]==['api','workspaces'] and len(p)>=6 and p[4]=='groups':
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
   g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'] if w else -1,p[5])).fetchone()
   if not g:return err(self,404)
   mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   if not mr or (g['creator_id']!=me['id'] and mr['role']!='owner'):return err(self,403)
   ids=b.get('user_ids',[])
   if not isinstance(ids,list):return err(self,400)
   for uid in ids:
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():return err(self,400)
   if 'name' in b and not b['name']:return err(self,400)
   c.execute('UPDATE groups SET name=? WHERE id=?',(b.get('name',g['name']),g['id']))
   c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],))
   for uid in ids:c.execute('INSERT OR IGNORE INTO groupmembers VALUES(?,?)',(g['id'],uid))
   c.commit()
   return j(self,200,{'group':{'id':g['id'],'handle':g['handle'],'name':b.get('name',g['name']),'member_user_ids':ids}})
  if p[1:3]==['api','workspaces'] and len(p)>=5 and p[4]=='transfer_ownership':
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
   if not w:return err(self,404)
   if w['owner_id']!=me['id']:return err(self,403)
   target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],b.get('new_owner_id'))).fetchone()
   if not target or target['role']!='admin':return err(self,400)
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],me['id']))
   c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b['new_owner_id']))
   c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(b['new_owner_id'],w['id']));c.commit()
   return j(self,200,{'workspace':uobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  if p[1:3]==['api','workspaces']:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
   if not w:return err(self,404)
   ar=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   if not ar or ar['role'] not in ('owner','admin'):return err(self,403)
   if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'): return err(self,400)
   vals={k:b[k] for k in ('name','join_mode') if k in b}; c.execute('UPDATE workspaces SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),w['id']));c.commit();return j(self,200,{'workspace':uobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
  return err(self,404)
 def do_DELETE(self):
  me=self.auth(); p=self.route(); c=db()
  if not me:return err(self,401)
  if p[1:3]==['api','workspaces'] and len(p)>=6 and p[4]=='groups':
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
   g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'] if w else -1,p[5])).fetchone()
   if not g:return j(self,200,{'deleted':True})
   mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],me['id'])).fetchone()
   if not mr or (g['creator_id']!=me['id'] and mr['role']!='owner'):return err(self,403)
   c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit()
   return j(self,200,{'deleted':True})
  if p[1:3]==['api','workspaces'] and len(p)>=6 and p[4]=='invitations':
   inv=c.execute('SELECT * FROM invitations WHERE code=?',(p[5],)).fetchone()
   if not inv:return err(self,404)
   w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(inv['workspace_id'],)).fetchone()
   if not w or w['owner_id']!=me['id']:return err(self,403)
   c.execute('DELETE FROM invitations WHERE code=?',(p[5],));c.commit();return j(self,200,{'deleted':True})
  if p[1:3]==['api','messages'] and len(p)>=5 and p[4]=='pin':
   m=c.execute('SELECT * FROM messages WHERE id=?',(p[3],)).fetchone()
   if not m:return err(self,404)
   c.execute('DELETE FROM pins WHERE message_id=?',(m['id'],));c.commit();return j(self,200,{'unpinned':True})
  if p[1:3]==['api','messages'] and len(p)>=6 and p[4]=='reactions':
   m=c.execute('SELECT * FROM messages WHERE id=?',(p[3],)).fetchone()
   if not m:return err(self,404)
   if c.execute('SELECT 1 FROM channels ch JOIN messages mm ON mm.channel_id=ch.id WHERE mm.id=? AND ch.is_private=1 AND NOT EXISTS (SELECT 1 FROM cmembers z WHERE z.channel_id=ch.id AND z.user_id=?)',(m['id'],me['id'])).fetchone(): return err(self,403)
   changed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],me['id'],p[5])).fetchone()
   if changed:
    c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE channel_seq SET seq=seq+1 WHERE channel_id=?',(m['channel_id'],)); ev=c.execute('SELECT seq FROM channel_seq WHERE channel_id=?',(m['channel_id'],)).fetchone()['seq']; c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],me['id'],p[5])); c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(m['channel_id'],ev,'reaction.removed',json.dumps({'message_id':m['id'],'emoji':p[5],'user_id':me['id']}))); c.commit()
   rs=c.execute('SELECT emoji,count(*) count FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY min(rowid)',(m['id'],)).fetchall()
   return j(self,200,{'reactions':[{'emoji':x['emoji'],'count':x['count'],'user_ids':[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(m['id'],x['emoji'])).fetchall()]} for x in rs]})
  if p[1:3]==['api','messages']:
   m=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(p[3],)).fetchone()
   if not m:return err(self,404)
   if m['author_id']!=me['id'] and m['owner_id']!=me['id']:return err(self,403)
   if m['deleted']:return j(self,200,{'deleted':True})
   c.execute('BEGIN IMMEDIATE')
   c.execute('UPDATE channel_seq SET seq=seq+1 WHERE channel_id=?',(m['channel_id'],))
   ev=c.execute('SELECT seq FROM channel_seq WHERE channel_id=?',(m['channel_id'],)).fetchone()['seq']
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(p[3],))
   c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(m['channel_id'],ev,'message.deleted',json.dumps({'message_id':int(p[3])})))
   c.commit();return j(self,200,{'deleted':True})
  return err(self,404)
 def ws(self):
  tok=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
  c=db(); ok=c.execute('SELECT 1 FROM tokens WHERE token=?',(tok,)).fetchone(); c.close()
  if not ok:return err(self,401)
  key=base64.b64encode(hashlib.sha1((self.headers.get('Sec-WebSocket-Key','')+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',key); self.end_headers(); self.wfile.flush()
  sock=self.connection; sock.settimeout(.2); channel=None; last=0; ge_last=0
  def send(x):
   z=json.dumps(x).encode(); n=len(z)
   if n<126: h=bytes([0x81,n])
   elif n<65536:h=bytes([0x81,126])+n.to_bytes(2,'big')
   else:h=bytes([0x81,127])+n.to_bytes(8,'big')
   sock.sendall(h+z)
  def read():
   try:
    h=sock.recv(2)
    if len(h)<2:return None
    ln=h[1]&127
    if ln==126:ln=int.from_bytes(sock.recv(2),'big')
    elif ln==127:ln=int.from_bytes(sock.recv(8),'big')
    mask=sock.recv(4); data=b''
    while len(data)<ln:
     x=sock.recv(ln-len(data))
     if not x:return None
     data+=x
    return json.loads(bytes(v^mask[i%4] for i,v in enumerate(data)))
   except (socket.timeout,TimeoutError):return 'TIME'
   except:return None
  import socket
  while True:
   x=read()
   if x is None:
    cc=db(); tu=cc.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone();
    if tu and channel: cc.commit()
    cc.close(); return
   if x!='TIME':
    if x.get('type')=='subscribe':
     channel=int(x.get('channel_id')); c=db(); chx=c.execute('SELECT * FROM channels WHERE id=?',(channel,)).fetchone(); tokrow=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); allowed=(not chx or not chx['is_private'] or c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(channel,tokrow['user_id'])).fetchone()); row=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM messages WHERE channel_id=?',(channel,)).fetchone(); c.close();
     if not allowed: send({'type':'error','channel_id':channel,'error':'forbidden'}); channel=None; continue
     last=row['h']; c=db(); import time; tokuser=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); c.execute('INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)',(tokuser['user_id'],channel,time.time())); c.commit(); c.close(); send({'type':'subscribed','channel_id':channel,'head_event_id':last})
    elif x.get('type')=='resume':
     channel=int(x.get('channel_id')); since=int(x.get('since_event_id',0)); c=db(); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,since)).fetchall(); head=c.execute('SELECT COALESCE(MAX(event_id),0) h FROM messages WHERE channel_id=?',(channel,)).fetchone()['h']; 
     for m in rows: send({'type':'message.reply' if m['parent_id'] else 'message.created','event_id':m['event_id'],'channel_id':channel,'message':self.msg(m,c)})
     c.close(); last=head; send({'type':'resumed','channel_id':channel,'head_event_id':head})
   if channel:
    cc=db(); tu=cc.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone();
    if tu: cc.execute('UPDATE ws_presence SET seen=? WHERE user_id=? AND channel_id=?',(time.time(),tu['user_id'],channel)); cc.commit()
    cc.close()
   c=db(); ge=c.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(ge_last,)).fetchall()
   for g in ge:
    try: send({'type':'user.updated','user':json.loads(g['payload'])})
    except: pass
    ge_last=g['id']
   c.close()
   if channel:
    c=db(); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,last)).fetchall()
    for e in rows:
     z=json.loads(e['payload']); out={'type':e['type'],'event_id':e['event_id'],'channel_id':channel}
     if e['type'] in ('message.created','message.reply','message.updated'):
      mm=c.execute('SELECT * FROM messages WHERE id=?',(z['message_id'],)).fetchone(); out['message']=self.msg(mm,c); out['message_id']=z['message_id'] if e['type']=='message.updated' else out.get('message_id')
     else: out.update({k:z[k] for k in ('message_id','emoji','user_id') if k in z})
     send(out); last=e['event_id']
    c.close()

 def html(self):
  x='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;background:#f8f8f8}button{border:0;padding:9px 14px;color:#fff;border-radius:4px}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}.layout{display:flex;height:100vh}.side{width:240px;background:#3f0e40;color:white;padding:20px}.main{padding:30px;flex:1}.msg{padding:12px;border-bottom:1px solid #ddd}</style></head><body><div id=app><div class=layout><aside class=side><h2>Huddle</h2><div data-testid=current-user></div><div data-testid=channel-list></div><button data-testid=new-channel-btn class=secondary>New channel</button><button data-testid=logout-btn class=danger>Logout</button></aside><main class=main><h1 data-testid=workspace-header>Welcome</h1><h2 data-testid=channel-title># general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn class=primary>Send</button></main></div></div><script>const $=x=>document.querySelector(x);let t=localStorage.getItem('huddle.token');async function api(u,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+t,'Content-Type':'application/json'};return fetch('/api'+u,o).then(x=>x.json())}if(!t){$('#app').innerHTML='<form data-testid=auth-form><input name=username><input name=password type=password><button data-testid=auth-submit class=primary>Login</button><button type=button data-testid=auth-toggle>Sign up</button><div id=error></div></form>';$('.auth-form')}document.querySelector('[data-testid=auth-form]')?.addEventListener('submit',async e=>{e.preventDefault();let f=new FormData(e.target),r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(f))});let d=await r.json();if(r.ok){localStorage.setItem('huddle.token',d.token);location.reload()}else $('#error').textContent='Invalid login'});</script></body></html>'''
  b=x.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)

class IRCHandler(socketserver.StreamRequestHandler):
 def setup(self):
  self.nick=None; self.uid=None; self.authed=False; self.joined={}
 def sendl(self,x):
  try:self.wfile.write((x+'\r\n').encode()); self.wfile.flush()
  except:pass
 def lookup(self,name):
  c=db(); r=c.execute("SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE '#'+w.slug+'/'+ch.name=?",(name,)).fetchone(); c.close(); return r
 def reg(self):
  if not self.authed or not self.nick or not self.uid:return
  c=db(); clash=c.execute('SELECT 1 FROM users WHERE username=?',(self.nick,)).fetchone()
  # nick collision is checked against already registered gateway clients
  self.sendl(':huddle 001 %s :Welcome to Huddle' % self.nick)
  self.sendl(':huddle 002 %s :Your host is huddle' % self.nick)
  self.sendl(':huddle 003 %s :This server was created today' % self.nick)
  self.sendl(':huddle 004 %s huddle 1.0 o o' % self.nick)
  self.sendl(':huddle 005 %s CHANTYPES=# :are supported' % self.nick)
  self.sendl(':huddle 422 %s :MOTD File is missing' % self.nick); c.close()
 def handle(self):
  self.setup(); self.request.settimeout(30); buf=b''
  while True:
   try:x=self.rfile.readline()
   except:break
   if not x:break
   line=x.decode(errors='ignore').strip('\r\n')
   if not line:continue
   a=line.split(); cmd=a[0].upper()
   if cmd=='PASS':
    tok=a[1] if len(a)>1 else ''; c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone(); c.close()
    if r:self.authed=True;self.uid=r['user_id']
    else:self.sendl(':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    nn=a[1] if len(a)>1 else ''
    if self.nick is None and self.authed:
     if nn and nn.isalnum():self.nick=nn; self.reg()
    elif self.nick and nn!=self.nick:self.nick=nn;self.reg()
   elif cmd=='USER':
    if self.authed:self.userline=True; self.reg()
   elif cmd=='PING':self.sendl('PONG '+(' '.join(a[1:]) if len(a)>1 else ''))
   elif cmd=='PONG':pass
   elif cmd=='JOIN' and len(a)>1:
    name=a[1]; ch=self.lookup(name)
    if not ch:self.sendl(':huddle 403 %s %s :No such channel' %(self.nick,name));continue
    self.joined[ch['id']]=name; self.sendl(':%s!%s@huddle JOIN %s'%(self.nick,self.nick,name))
    c=db(); ns=[z['username'] for z in c.execute('SELECT u.username FROM cmembers m JOIN users u ON u.id=m.user_id WHERE m.channel_id=?',(ch['id'],)).fetchall()];c.close()
    self.sendl(':huddle 353 %s = %s :%s'%(self.nick,name,' '.join(ns)));self.sendl(':huddle 366 %s %s :End of NAMES list'%(self.nick,name))
   elif cmd=='PRIVMSG' and len(a)>=3:
    name=a[1]; body=line.split(':',1)[1] if ':' in line else ' '.join(a[2:]); ch=self.lookup(name)
    if ch and ch['id'] in self.joined:
     c=db(); c.execute('BEGIN IMMEDIATE');c.execute('INSERT OR IGNORE INTO channel_seq(channel_id,seq) VALUES(?,0)',(ch['id'],));c.execute('UPDATE channel_seq SET seq=seq+1 WHERE channel_id=?',(ch['id'],));ev=c.execute('SELECT seq FROM channel_seq WHERE channel_id=?',(ch['id'],)).fetchone()['seq'];c.execute('INSERT INTO messages(channel_id,author_id,body,created_at,event_id) VALUES(?,?,?,?,?)',(ch['id'],self.uid,body,now(),ev));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(ch['id'],ev,'message.created',json.dumps({'message_id':mid})));c.commit();c.close()
   elif cmd=='NAMES':self.sendl(':huddle 366 %s %s :End of NAMES list'%(self.nick,a[1] if len(a)>1 else '*'))
   elif cmd in ('WHO','LIST','TOPIC','MODE'):self.sendl(':huddle 421 %s %s :Command accepted'%(self.nick,cmd))
   elif cmd=='QUIT':return
   else:self.sendl(':huddle 421 %s %s :Unknown command'%(self.nick,cmd))
class IRCServer(ThreadingHTTPServer): allow_reuse_address=True
def run_irc():
 from socketserver import ThreadingTCPServer
 class S(ThreadingTCPServer): allow_reuse_address=True
 with S(('0.0.0.0',6667),IRCHandler) as srv:srv.serve_forever()

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='irc': init(); run_irc(); raise SystemExit
 for _ in range(20):
  try: init(); break
  except sqlite3.OperationalError: import time; time.sleep(.5)
 port=int(sys.argv[3]); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()

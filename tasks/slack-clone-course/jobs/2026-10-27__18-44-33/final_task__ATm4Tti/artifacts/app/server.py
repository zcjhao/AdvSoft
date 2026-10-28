import asyncio, hashlib, json, os, re, secrets, sqlite3, sys, time
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

NODE=int(sys.argv[1]); PORT=int(sys.argv[2]); DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000'); return c
def init():
 c=db(); c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '',mention_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,data TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0,invited_user_id INTEGER,invited_username TEXT,revoked INTEGER DEFAULT 0);
'''); c.commit(); c.close()
init()
def out(o,status=200): return web.json_response(o,status=status)
def err(n): return web.json_response({'error':'invalid request'},status=n)
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(req):
 h=req.headers.get('Authorization','')
 token=h[7:] if h.startswith('Bearer ') else req.query.get('token','')
 if not token: return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token,)).fetchone(); c.close(); return r
def channelobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def wsobj(r): return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def role(c,w,u): 
 r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); return r['role'] if r else None
def accessible(c,ch,u):
 r=c.execute('SELECT is_private,workspace_id FROM channels WHERE id=?',(ch,)).fetchone()
 return bool(r and (not r['is_private'] or c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u)).fetchone()))
def event(c,ch,typ,data):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
 c.execute('INSERT INTO events(channel_id,event_id,type,data) VALUES(?,?,?,?)',(ch,n,typ,json.dumps(data))); return n
def msgobj(c,r=None):
 if r is None: r=c; c=db()
 d=dict(r); au=c.execute('SELECT * FROM users WHERE id=?',(d['author_id'],)).fetchone()
 if au is None: return {'id':d['id'],'channel_id':d['channel_id'],'author_id':d['author_id'],'author':{},'body':d.get('body',''),'parent_id':d.get('parent_id'),'reply_count':0,'created_at':d.get('created_at'), 'edited_at':d.get('edited_at'), 'files':[], 'reactions':[], 'mentions':[]}
 reps=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(d['id'],)).fetchall()
 return {'id':d['id'],'channel_id':d['channel_id'],'author_id':d['author_id'],'author':userobj(au),'body':d['body'],'parent_id':d['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(d['id'],)).fetchone()[0],'created_at':d['created_at'],'edited_at':d['edited_at'],'files':[],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in reps],'mentions':mentions_for(c,d['channel_id'],d['author_id'],d['body'])}
async def poll(app):
 last=0
 while True:
  await asyncio.sleep(.05); c=db()
  rows=c.execute('SELECT id,channel_id,event_id,type,data FROM events WHERE id>? ORDER BY id',(last,)).fetchall()
  for r in rows:
   last=r['id']; frame={'type':r['type'],'event_id':r['event_id'],'channel_id':r['channel_id'],**json.loads(r['data'])}
   for ws,chans in list(app['subs'].items()):
    if r['channel_id'] in chans and r['event_id'] > chans[r['channel_id']]:
     chans[r['channel_id']]=r['event_id']
     try: await ws.send_json(frame)
     except: pass
  c.close()
async def ws_handler(req):
 u=auth(req)
 if not u: return web.Response(status=401)
 ws=web.WebSocketResponse(); await ws.prepare(req); req.app['subs'][ws]={}
 c=db()
 async for m in ws:
  if m.type!=WSMsgType.TEXT: continue
  x=json.loads(m.data); ch=int(x.get('channel_id',0))
  if not accessible(c,ch,u['id']): await ws.send_json({'type':'error','error':'forbidden'}); continue
  if x.get('type')=='subscribe':
   await asyncio.sleep(0.08)
   head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
   req.app['subs'][ws][ch]=head
   await ws.send_json({'type':'subscribed','channel_id':ch,'head_event_id':head})
  elif x.get('type')=='resume':
   since=int(x.get('since_event_id',0)); rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
   for r in rows: await ws.send_json({'type':r['type'],'event_id':r['event_id'],'channel_id':ch,**json.loads(r['data'])})
   head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch,)).fetchone()[0]; req.app['subs'][ws][ch]=head
   await ws.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
 c.close(); req.app['subs'].pop(ws,None); return ws
async def handler(req):
 p=req.path; method=req.method
 if p=='/api/health': return out({'status':'ok','node_id':NODE})
 if p=='/api/ws': return await ws_handler(req)
 u=auth(req)
 if p=='/api/auth/register' and method=='POST':
  x=await req.json()
  if not re.fullmatch(r'[A-Za-z0-9_]+',x.get('username','')) or len(x.get('password',''))<8:return err(400)
  c=db()
  try:
   c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(x['username'],x['password'],x.get('display_name') or x['username'])); c.commit()
  except sqlite3.IntegrityError:c.close();return err(409)
  r=c.execute('SELECT * FROM users WHERE username=?',(x['username'],)).fetchone(); t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return out({'user':userobj(r),'token':t},201)
 if p=='/api/auth/login' and method=='POST':
  x=await req.json();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
  if not r:c.close();return err(401)
  t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return out({'user':userobj(r),'token':t})
 if not u:return err(401)
 uid=u['id']; c=db()
 try:
  if p=='/api/auth/me': return out({'user':userobj(u)})
  m=re.fullmatch(r'/api/users/(\d+)',p)
  if m and method=='GET':
   r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone(); return out({'user':userobj(r)}) if r else err(404)
  if p=='/api/workspaces' and method=='GET':
   rows=c.execute('SELECT DISTINCT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(uid,)).fetchall();return out({'workspaces':[wsobj(r) for r in rows]})
  if p=='/api/workspaces' and method=='POST':
   x=await req.json()
   if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return err(400)
   try:
    c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(x['slug'],x.get('name',''),uid)); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,uid,'owner')); cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(gid,uid));c.commit()
   except sqlite3.IntegrityError:c.rollback();return err(409)
   return out({'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(gid,)).fetchone())},201)
  m=re.fullmatch(r'/api/workspaces/([^/]+)(?:/(.*))?',p)
  if m:
   slug,tail=m.group(1),m.group(2) or ''; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return err(404)
   rr=role(c,w['id'],uid)
   if tail=='members' and method=='GET':
    rows=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall();return out({'members':[dict(x) for x in rows]})
   if tail=='' and method=='GET':
    inc=req.query.get('include_archived','') in ('1','true'); rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (? OR is_archived=0) AND (is_private=0 OR id IN (SELECT channel_id FROM channel_members WHERE user_id=?))',(w['id'],int(inc),uid)).fetchall(); rs=[readstate(c,x['id'],uid) for x in rows];return out({'workspace':wsobj(w),'channels':[channelobj(x) for x in rows],'read_state':rs})
   if tail=='' and method=='PATCH':
    if rr not in ('owner','admin'):return err(403)
    x=await req.json(); jm=x.get('join_mode',w['join_mode'])
    if jm not in ('open','invite_only'):return err(400)
    c.execute('UPDATE workspaces SET name=?,join_mode=? WHERE id=?',(x.get('name',w['name']),jm,w['id']));c.commit();return out({'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   mm=re.fullmatch(r'members/(\d+)',tail)
   if mm and method=='PATCH':
    target=int(mm.group(1)); x=await req.json(); new=x.get('role')
    if new not in ('owner','admin','member','guest'):return err(400)
    if new=='owner': return err(400)
    old=role(c,w['id'],target)
    if old is None: return err(404)
    if rr not in ('owner','admin') or target==w['owner_id'] or (rr=='admin' and old=='admin'):return err(403)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],target));c.commit();return out({'member':{'user_id':target,'role':new}})
   if tail=='transfer_ownership' and method=='POST':
    if rr!='owner':return err(403)
    x=await req.json(); target=int(x.get('new_owner_id',0))
    if role(c,w['id'],target)!='admin':return err(400)
    c.execute('UPDATE members SET role="admin" WHERE workspace_id=? AND user_id=?',(w['id'],uid));c.execute('UPDATE members SET role="owner" WHERE workspace_id=? AND user_id=?',(w['id'],target));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']));c.commit();return out({'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   if tail=='channels' and method=='POST':
    x=await req.json()
    if rr not in ('owner','admin','member') or (x.get('is_private',False) and rr not in ('owner','admin')):return err(403)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')):return err(400)
    if not isinstance(x.get('topic',''),str) or len(x.get('topic',''))>250:return err(400)
    try:
     cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],x['name'],int(bool(x.get('is_private'))),x.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,uid));c.commit()
    except sqlite3.IntegrityError:return err(409)
    return out({'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())},201)
  m=re.fullmatch(r'/api/channels/(\d+)(?:/(.*))?',p)
  if m:
   cid=int(m.group(1)); tail=m.group(2) or ''; ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
   if not ch:return err(404)
   w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); rr=role(c,w['id'],uid)
   if tail=='join' and method=='POST':
    if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone():return err(403)
    if rr is None:
     if w['join_mode']!='open' or rr=='guest':return err(403)
     c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],uid,'member'))
    if rr=='guest':return err(403)
    had=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,uid));
    if not had: event(c,cid,'member.joined',{'user_id':uid})
    c.commit();return out({'channel':channelobj(ch)})
   if tail=='' and method=='PATCH':
    x=await req.json(); topic=x.get('topic','')
    if len(topic)>250:return err(400)
    if rr!='owner' and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone():return err(403)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); event(c,cid,'channel.updated',{'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}); c.commit();return out({'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   if tail=='leave' and method=='POST':
    had=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid));
    if had: event(c,cid,'member.left',{'user_id':uid})
    c.commit();return out({'channel':channelobj(ch)})
   if tail=='members' and method=='GET':
    if not accessible(c,cid,uid):return err(403)
    rows=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall();return out({'members':[userobj(x) for x in rows]})
   if tail in ('archive','unarchive') and method=='POST':
    if rr!='owner':return err(403)
    val=1 if tail=='archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid)); event(c,cid,'channel.archived' if val else 'channel.unarchived',{'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}); c.commit();return out({'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   if tail=='pins' and method=='GET':
    if not accessible(c,cid,uid):return err(403)
    rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)).fetchall(); part,nxt,st=paging(req,rows)
    if st!=200:return err(st)
    return out({'pins':[{'message':msgobj(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in part],'next_cursor':nxt})
   if tail=='messages' and method=='GET': return await listmsgs(req,c,ch,uid)
   if tail=='messages' and method=='POST': return await postmsg(req,c,ch,uid)
   if tail=='read': 
    if method=='GET':return out({'read_state':readstate(c,cid,uid)})
    x=await req.json();v=x.get('last_read_event_id'); 
    if not isinstance(v,int) or v<0:return err(400)
    head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; old=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone();v=min(head,max(v,old[0] if old else 0));c.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?)',(uid,cid,v));c.commit();return out({'read_state':readstate(c,cid,uid)})
  m=re.fullmatch(r'/api/messages/(\d+)(?:/(.*))?',p)
  if m:
   mid=int(m.group(1));tail=m.group(2) or ''; msg=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
   if not msg:
    gone=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if gone and method=='PATCH': return err(409)
    if gone and method=='DELETE': return out({'deleted':True})
    return err(404)
   rr=role(c,c.execute('SELECT workspace_id FROM channels WHERE id=?',(msg['channel_id'],)).fetchone()['workspace_id'],uid)
   if tail=='replies' and method=='GET':
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall(); part,nxt,st=paging(req,rows); return err(st) if st!=200 else out({'replies':[msgobj(c,x) for x in part],'next_cursor':nxt})
   if tail=='pin' and method=='POST':c.execute('INSERT OR REPLACE INTO pins VALUES(?,?,?)',(mid,uid,now()));c.commit();return out({'pin':{'message_id':mid,'pinned_by':uid,'pinned_at':now()}})
   if tail=='pin' and method=='DELETE':c.execute('DELETE FROM pins WHERE message_id=?',(mid,));c.commit();return out({'unpinned':True})
   if tail=='reactions' and method=='POST':
    x=await req.json(); before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,x.get('emoji',''))).fetchone(); c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,uid,x.get('emoji',''))); 
    if not before: event(c,msg['channel_id'],'reaction.added',{'message_id':mid,'emoji':x.get('emoji',''),'user_id':uid})
    c.commit()
    rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall();return out({'reactions':[{'emoji':z['emoji'],'count':z['n'],'user_ids':[int(q) for q in z['ids'].split(',')]} for z in rs]})
   if tail.startswith('reactions/') and method=='DELETE':
    emoji=tail.split('/',1)[1]; before=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,emoji)).fetchone(); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,emoji));
    if before: event(c,msg['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':uid})
    c.commit()
    rs=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall();return out({'reactions':[{'emoji':z['emoji'],'count':z['n'],'user_ids':[int(q) for q in z['ids'].split(',')]} for z in rs]})
   if not tail and method=='PATCH':
    x=await req.json()
    if msg['author_id']!=uid:return err(403)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(x.get('body',''),now(),mid)); mm=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); event(c,msg['channel_id'],'message.updated',{'message':msgobj(c,mm)}); c.commit();return out({'message':msgobj(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())})
   if not tail and method=='DELETE':
    if msg['author_id']!=uid and rr!='owner':return err(403)
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); event(c,msg['channel_id'],'message.deleted',{'message_id':mid}); c.commit();return out({'deleted':True})
  if p=='/api/search' and method=='GET':
   q=req.query.get('q','').lower(); wid=req.query.get('workspace_id'); base='SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND lower(m.body) LIKE ?'; args=[f'%{q}%'];
   if wid: base+=' AND ch.workspace_id=?'; args.append(wid)
   base+=' ORDER BY m.id DESC'; rows=[x for x in c.execute(base,args).fetchall() if accessible(c,x['channel_id'],uid)]; part,nxt,st=paging(req,rows); return err(st) if st!=200 else out({'results':[msgobj(c,x) for x in part],'next_cursor':nxt})
  if p.startswith('/api/invitations/') and p.endswith('/accept') and method=='POST':
   code=p.split('/')[3]
   inv=c.execute('SELECT code,workspace_id,max_uses,uses,revoked,expires_at,invited_user_id,invited_username FROM invitations WHERE code=?',(code,)).fetchone()
   if inv is None:return err(404)
   if inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['invited_user_id'] is not None and inv['uses']>0) or (inv['invited_username'] is not None and inv['uses']>0) or (inv['expires_at'] is not None and float(inv['expires_at'])<=time.time()):return err(404)
   if inv['invited_user_id'] is not None and int(inv['invited_user_id'])!=uid:return err(403)
   if inv['invited_username'] is not None and inv['invited_username']!=u['username']:return err(403)
   c.execute('UPDATE invitations SET uses=uses+1 WHERE code=? AND uses<? AND revoked=0',(code,inv['max_uses']))
   c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],uid,'member')); c.commit()
   wr=c.execute('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],)).fetchone()
   return out({'workspace':wsobj(wr)})
  if method=='DELETE' and re.fullmatch(r'/api/workspaces/[^/]+/invitations/[^/]+',p):
   parts=p.split('/'); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[3],)).fetchone()
   if not w:return err(404)
   rr=role(c,w['id'],uid)
   if rr not in ('owner','admin'):return err(403)
   r=c.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(parts[5],w['id'])).fetchone()
   if not r:return err(404)
   c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(parts[5],));c.commit();return out({'deleted':True})
  if p.startswith('/api/workspaces/') and '/invitations' in p:
   return await invitation(req,c,u)
  return err(404)
 except Exception as e:
  c.rollback(); print('HANDLER ERROR:',repr(e),file=sys.stderr,flush=True); return web.json_response({'error':repr(e)},status=500)
 finally:c.close()
async def listmsgs(req,c,ch,uid):
 if not accessible(c,ch['id'],uid):return err(403)
 try: lim=int(req.query.get('limit','50'))
 except: return err(400)
 if lim<1 or lim>200:return err(400)
 cur=req.query.get('cursor'); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC',(ch['id'],)).fetchall()
 if cur:
  try: boundary=int(cur)
  except:return err(400)
  rows=[x for x in rows if x['id']<boundary]
 part=rows[:lim]; nxt=str(part[-1]['id']) if len(rows)>lim else None
 return out({'messages':[msgobj(c,x) for x in part],'next_cursor':nxt})
async def postmsg(req,c,ch,uid):
 if ch['is_archived']:return err(423)
 if not accessible(c,ch['id'],uid):return err(403)
 x=await req.json(); body=x.get('body','')
 if not isinstance(body,str) or not body.strip():return err(400)
 mentions=[]; 
 for name in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_]+)',body):
  if name=='channel': mentions += [r['user_id'] for r in c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(ch['id'],uid))]
  else:
   r=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
   if r and r['id']!=uid and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],r['id'])).fetchone(): mentions.append(r['id'])
 mentions=list(dict.fromkeys(mentions)); c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],uid,body,x.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); mo=msgobj(c,r); ev=event(c,ch['id'],'message.reply' if x.get('parent_id') else 'message.created',{'message':mo});c.execute('UPDATE messages SET event_id=? WHERE id=?',(ev,mid));c.commit();return out({'message':mo},201)
def mentions_for(c,cid,author,body):
 out=[]
 for name in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_]+)',body):
  if name=='channel': out += [x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(cid,author))]
  else:
   x=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
   if x and x['id']!=author and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,x['id'])).fetchone(): out.append(x['id'])
 return list(dict.fromkeys(out))
def paging(req, rows):
 try: lim=int(req.query.get('limit','50'))
 except: return None,None,400
 if lim<1 or lim>200:return None,None,400
 cur=req.query.get('cursor'); start=0
 if cur:
  try: start=int(cur)
  except:return None,None,400
  if start<0:return None,None,400
 part=rows[start:start+lim]; nxt=str(start+lim) if start+lim<len(rows) else None
 return part,nxt,200
def readstate(c,cid,uid):
 r=c.execute('SELECT last_read FROM reads WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone(); last=r['last_read'] if r else 0
 rows=c.execute('SELECT event_id,author_id,body FROM messages WHERE channel_id=? AND deleted=0 AND event_id>?',(cid,last)).fetchall(); un=sum(x['author_id']!=uid for x in rows); mc=sum(x['author_id']!=uid and ('@'+(c.execute('SELECT username FROM users WHERE id=?',(uid,)).fetchone()['username'])) in x['body'] for x in rows)
 return {'channel_id':cid,'last_read_event_id':last,'unread_count':un,'mention_count':mc}
async def invitation(req,c,u):
 p=req.path; parts=p.split('/'); slug=parts[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:return err(404)
 rr=role(c,w['id'],u['id'])
 if req.method=='POST':
  if rr not in ('owner','admin'):return err(403)
  x=await req.json(); uses=x.get('max_uses',1)
  if not isinstance(uses,int) or uses<1:return err(400)
  expires=(time.time()+int(x['expires_in_seconds'])) if x.get('expires_in_seconds') is not None else None
  code=secrets.token_urlsafe(12); c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],expires,uses,0,x.get('invited_user_id'),x.get('invited_username'),0));c.commit();return out({'invitation':{'code':code,'workspace_id':w['id'],'max_uses':uses}},201)
 if req.method=='GET':
  if rr not in ('owner','admin'):return err(403)
  rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses',(w['id'],)).fetchall();return out({'invitations':[dict(x) for x in rows]})
 if req.method=='DELETE' and len(parts)>=6:
  if rr not in ('owner','admin'):return err(403)
  code=parts[5]; r=c.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(code,w['id'])).fetchone()
  if not r:return err(404)
  c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,));c.commit();return out({'deleted':True})
 return err(404)
async def startup(a):
 a['poll_task']=asyncio.create_task(poll(a))
app=web.Application();app['subs']={};app.router.add_route('*','/api/{tail:.*}',handler);app.on_startup.append(startup)
web.run_app(app,host='127.0.0.1',port=PORT)

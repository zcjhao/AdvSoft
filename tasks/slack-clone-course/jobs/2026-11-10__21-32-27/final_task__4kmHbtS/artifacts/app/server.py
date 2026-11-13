import asyncio, json, os, re, secrets, sqlite3, sys, hashlib
from datetime import datetime, timezone
from aiohttp import web
DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
sockets=set(); subscriptions={}
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    os.makedirs('/app/data',exist_ok=True); c=sqlite3.connect(DB,timeout=5,isolation_level=None)
    c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS wmembers(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT "member",UNIQUE(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));''')
 c.close()
def U(r):
 return dict(r) if r else None
def userobj(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('Authorization','')
 if not h.startswith('Bearer '): raise web.HTTPUnauthorized()
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
 if not r: raise web.HTTPUnauthorized()
 return r
async def body(req):
 try:return await req.json()
 except: raise web.HTTPBadRequest()
def channel(c,i):
 r=c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone()
 if not r: raise web.HTTPNotFound()
 return r
def chanjson(r): return {k:r[k] for k in ['id','workspace_id','name','is_private','is_dm','topic','is_archived']}
def msgjson(c,r):
 if not r or r['deleted']: raise web.HTTPNotFound()
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rc=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
 reacts=[]
 for x in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  ids=[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]
  reacts.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':rc,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':reacts,'mentions':[]}
def event(c,cid,typ,**kw):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
 f={'type':typ,'event_id':n,'channel_id':cid}; f.update(kw); c.execute('INSERT INTO events VALUES(?,?,?)',(cid,n,json.dumps(f))); return f
def member(c,cid,uid):
 return c.execute('SELECT 1 FROM members WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone() is not None
def post(c,u,ch,body,parent=None):
 if not body or not body.strip(): raise web.HTTPBadRequest()
 if ch['is_archived']: raise web.HTTPRequestEntityTooLarge()
 if not member(c,ch['id'],u['id']): raise web.HTTPForbidden()
 t=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,parent,t)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); m=msgjson(c,r); event(c,ch['id'],'message.reply' if parent else 'message.created',message=m); return m
async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
 d=await body(req); un=d.get('username',''); pw=d.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise web.HTTPBadRequest()
 c=db()
 try:c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,d.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
 except sqlite3.IntegrityError:c.rollback(); raise web.HTTPConflict()
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return web.json_response({'user':userobj(r),'token':tok},status=201)
async def login(req):
 d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
 if not r:c.close(); raise web.HTTPUnauthorized()
 tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id'])); c.close(); return web.json_response({'user':userobj(r),'token':tok})
async def me(req): return web.json_response({'user':userobj(auth(req))})
async def workspaces(req):
 u=auth(req); c=db(); rows=c.execute('SELECT DISTINCT w.* FROM workspaces w JOIN wmembers m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
 return web.json_response({'workspaces':[U(x) for x in rows]})
async def createws(req):
 u=auth(req); d=await body(req); slug=d.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise web.HTTPBadRequest()
 c=db()
 try:c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO wmembers VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id'])); c.commit()
 except sqlite3.IntegrityError:c.rollback(); raise web.HTTPConflict()
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
 return web.json_response({'workspace':U(w),'general_channel':chanjson(ch)},status=201)
async def wsdetail(req):
 u=auth(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:raise web.HTTPNotFound()
 rows=c.execute('SELECT c.* FROM channels c JOIN members m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?',(w['id'],u['id'])).fetchall(); c.close()
 return web.json_response({'workspace':U(w),'channels':[chanjson(x) for x in rows],'read_state':[]})
async def createch(req):
 u=auth(req); d=await body(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:raise web.HTTPNotFound()
 if not d.get('name') or not re.fullmatch(r'[a-z0-9-]{1,32}',d['name']):raise web.HTTPBadRequest()
 try:c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id'])); c.commit()
 except sqlite3.IntegrityError:c.rollback();raise web.HTTPConflict()
 r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return web.json_response({'channel':chanjson(r)},status=201)
async def join(req):
 u=auth(req);c=db();ch=channel(c,int(req.match_info['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(ch['id'],u['id']));c.close();return web.json_response({'channel':chanjson(ch)})
async def patch_channel(req):
 u=auth(req); d=await body(req); c=db(); ch=channel(c,int(req.match_info['id']))
 if not member(c,ch['id'],u['id']):
  w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
  if not w or w['owner_id']!=u['id']: c.close(); raise web.HTTPForbidden()
 if 'topic' not in d or len(str(d['topic']))>250: c.close(); raise web.HTTPBadRequest()
 c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE channels SET topic=? WHERE id=?',(str(d['topic']),ch['id'])); r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone(); c.commit(); c.close()
 return web.json_response({'channel':chanjson(r)})
async def members(req):
 auth(req);c=db();ch=channel(c,int(req.match_info['id'])); rows=c.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close();return web.json_response({'members':[userobj(x) for x in rows]})
async def messages(req):
 u=auth(req);c=db();ch=channel(c,int(req.match_info['id'])); lim=min(int(req.query.get('limit',50)),200); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(ch['id'],lim)).fetchall();out=[msgjson(c,x) for x in rows];c.close();return web.json_response({'messages':out,'next_cursor':None})
async def replies(req):
 auth(req);c=db(); p=c.execute('SELECT * FROM messages WHERE id=?',(int(req.match_info['id']),)).fetchone()
 if not p:raise web.HTTPNotFound()
 rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(p['id'],)).fetchall();out=[msgjson(c,x) for x in rows];c.close();return web.json_response({'replies':out,'next_cursor':None})
async def postroute(req):
 u=auth(req);d=await body(req);c=db();ch=channel(c,int(req.match_info['id']))
 try:c.execute('BEGIN IMMEDIATE');m=post(c,u,ch,d.get('body',''),d.get('parent_id'));c.commit()
 except: c.rollback();raise
 c.close();return web.json_response({'message':m},status=201)
async def edit(req):
 u=auth(req);d=await body(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(int(req.match_info['id']),)).fetchone()
 if not r: c.close();raise web.HTTPNotFound()
 if r['deleted']:c.close();raise web.HTTPConflict()
 if r['author_id']!=u['id']:c.close();raise web.HTTPForbidden()
 if not d.get('body','').strip():c.close();raise web.HTTPBadRequest()
 c.execute('BEGIN IMMEDIATE');t=now();c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],t,r['id']));m=msgjson(c,c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone());event(c,r['channel_id'],'message.updated',message=m);c.commit();c.close();return web.json_response({'message':m})
async def delete(req):
 u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(int(req.match_info['id']),)).fetchone()
 if not r:c.close();raise web.HTTPNotFound()
 if r['deleted']:c.close();return web.json_response({'deleted':True})
 w=c.execute('SELECT w.owner_id FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(r['channel_id'],)).fetchone()
 if r['author_id']!=u['id'] and w['owner_id']!=u['id']:c.close();raise web.HTTPForbidden()
 c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));event(c,r['channel_id'],'message.deleted',message_id=r['id']);c.commit();c.close();return web.json_response({'deleted':True})
async def react(req):
 u=auth(req);d=await body(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(int(req.match_info['id']),)).fetchone()
 if not r:c.close();raise web.HTTPNotFound()
 c.execute('BEGIN IMMEDIATE');c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(r['id'],u['id'],d.get('emoji',''))); changed=c.total_changes
 if changed:event(c,r['channel_id'],'reaction.added',message_id=r['id'],emoji=d.get('emoji',''),user_id=u['id'])
 c.commit(); out=msgjson(c,r)['reactions'];c.close();return web.json_response({'reactions':out})
async def unreact(req):
 u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(int(req.match_info['id']),)).fetchone()
 if not r:raise web.HTTPNotFound()
 c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],req.match_info['emoji']))
 if c.total_changes:event(c,r['channel_id'],'reaction.removed',message_id=r['id'],emoji=req.match_info['emoji'],user_id=u['id'])
 c.commit();out=msgjson(c,r)['reactions'];c.close();return web.json_response({'reactions':out})
async def ws_handler(req):
 tok=req.query.get('token','');c=db();r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone();c.close()
 if not r:raise web.HTTPUnauthorized()
 w=web.WebSocketResponse();await w.prepare(req); sockets.add(w); subscriptions[w]=set()
 try:
  async for x in w:
   d=json.loads(x.data); cid=int(d.get('channel_id'));c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
   if d.get('type')=='subscribe':
    subscriptions[w].add(cid); pump_marks[w,cid]=head
    await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
   elif d.get('type')=='resume':
    for z in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(d.get('since_event_id',0)))):await w.send_json(json.loads(z['frame']))
    await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
   c.close()
 finally:sockets.discard(w); subscriptions.pop(w,None)
 return w
pump_marks={}
async def pump(app):
 last={}; 
 while True:
  try:
   c=db()
   for r in c.execute('SELECT channel_id,event_id,frame FROM events'):
    k=(r['channel_id'],r['event_id'])
    if r['event_id']>last.get(r['channel_id'],0):
     last[r['channel_id']]=r['event_id']; f=json.loads(r['frame'])
     for w in list(sockets):
      if r['channel_id'] not in subscriptions.get(w,set()): continue
      if r['event_id'] <= pump_marks.get((w,r['channel_id']),0): continue
      pump_marks[w,r['channel_id']]=r['event_id']
      try: await w.send_json(f)
      except: pass
   c.close()
  except Exception: pass
  await asyncio.sleep(.05)
async def onstart(app):app['pump']=asyncio.create_task(pump(app))
def app():
 init();a=web.Application(client_max_size=11*1024*1024);a.on_startup.append(onstart)
 a.router.add_get('/',lambda req:web.FileResponse('/app/static/index.html'))
 a.router.add_static('/static/','/app/static')
 a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me)
 a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',createws);a.router.add_get('/api/workspaces/{slug}',wsdetail);a.router.add_post('/api/workspaces/{slug}/channels',createch)
 a.router.add_post('/api/channels/{id}/join',join);a.router.add_patch('/api/channels/{id}',patch_channel);a.router.add_get('/api/channels/{id}/members',members);a.router.add_get('/api/channels/{id}/messages',messages);a.router.add_post('/api/channels/{id}/messages',postroute)
 a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_patch('/api/messages/{id}',edit);a.router.add_delete('/api/messages/{id}',delete);a.router.add_post('/api/messages/{id}/reactions',react);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);a.router.add_get('/api/ws',ws_handler)
 return a
if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='node': web.run_app(app(),host='127.0.0.1',port=PORT)

irc_clients={}; irc_joined={}; irc_sources={}
async def irc_send(w,line):
    try:
        w.write((line+'\r\n').encode()); await w.drain()
    except: pass
def irc_channel(name):
    if not name.startswith('#') or '/' not in name:return None
    slug,cn=name[1:].split('/',1); c=db(); r=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone(); c.close(); return r
async def irc_client(reader,writer):
    irc_joined[writer]=set()
    nick=None; user_id=None; token=None; username=None; real=None; registered=False; joined=set()
    peer=writer.get_extra_info('peername'); host='huddle'
    async def reply(code,*parts):
        await irc_send(writer,':huddle '+code+' '+(nick or '*')+' '+' '.join(parts))
    async def complete():
        nonlocal registered, user_id
        if registered or not token or not nick or real is None:return
        c=db(); ok=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token,)).fetchone();c.close()
        if not ok:
            await irc_send(writer,':huddle 464 * :Password incorrect'); writer.close(); return
        if nick in irc_clients and irc_clients[nick] is not writer:
            await irc_send(writer,':huddle 433 * '+nick+' :Nickname is already in use'); return
        registered=True; irc_clients[nick]=writer; user_id=ok['id']
        for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle'),('005','CHANTYPES=#')]:
            await irc_send(writer,':huddle '+code+' '+nick+' :'+text)
        await irc_send(writer,':huddle 422 '+nick+' :MOTD File is missing')
    try:
        while not reader.at_eof():
            raw=await reader.readline()
            if not raw:break
            line=raw.decode(errors='ignore').strip('\r\n')
            if not line:continue
            parts=line.split(' ',1); cmd=parts[0].upper(); arg=parts[1] if len(parts)>1 else ''
            if cmd=='PASS':
                token=arg.lstrip(':').strip(); await complete()
            elif cmd=='NICK':
                nn=arg.split()[0] if arg else ''
                if nn in irc_clients and irc_clients[nn] is not writer:
                    nick=nn; await irc_send(writer,':huddle 433 * '+nn+' :Nickname is already in use')
                else:
                    nick=nn; await complete()
            elif cmd=='USER':
                ps=arg.split(' ',3); username=ps[0] if ps else ''; real=(ps[3][1:] if len(ps)>3 and ps[3].startswith(':') else (ps[3] if len(ps)>3 else '')); await complete()
            elif cmd=='PING':
                x=arg.lstrip(':').split()[0] if arg else ''; await irc_send(writer,':huddle PONG huddle :'+x)
            elif cmd=='QUIT':
                break
            elif cmd=='PRIVMSG' and registered:
                target, sep, text = arg.partition(' :')
                if not sep:
                    bits=arg.split(' ',1); target=bits[0]; text=bits[1][1:] if len(bits)>1 and bits[1].startswith(':') else (bits[1] if len(bits)>1 else '')
                ch=irc_channel(target.strip())
                if ch and ch['id'] in irc_joined.get(writer,set()) and text:
                    c=db()
                    try:
                        c.execute('BEGIN IMMEDIATE')
                        m=post(c, {'id':user_id}, ch, text, None)
                        row=c.execute('SELECT u.username FROM users u WHERE u.id=?',(user_id,)).fetchone()
                        ev=c.execute('SELECT MAX(event_id) n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']
                        c.commit(); irc_sources[(ch['id'],ev)]=writer
                    except Exception:
                        c.rollback()
                    finally: c.close()
            elif not registered:
                continue
            elif cmd=='JOIN':
                name=arg.split(',')[0].strip(); ch=irc_channel(name)
                if not ch or ch['is_private']:
                    await irc_send(writer,':huddle 403 '+nick+' '+name+' :No such channel'); continue
                c=db(); row=c.execute('SELECT u.username FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token,)).fetchone()
                uid=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()['user_id']
                c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(ch['id'],uid))
                names=[(nick if x['username']==row['username'] else x['username']) for x in c.execute('SELECT u.username FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],))]
                c.close(); joined.add(ch['id']); irc_joined[writer].add(ch['id'])
                prefix=nick+'!'+(row['username'] if row else username)+'@huddle'
                await irc_send(writer,':'+prefix+' JOIN '+name)
                await irc_send(writer,':huddle 353 '+nick+' = '+name+' :'+(' '.join(names)))
                await irc_send(writer,':huddle 366 '+nick+' '+name+' :End of /NAMES list')
                if ch['topic']: await irc_send(writer,':huddle 332 '+nick+' '+name+' :'+ch['topic'])
                else: await irc_send(writer,':huddle 331 '+nick+' '+name+' :No topic is set')
            elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):
                if cmd=='NAMES' and arg:
                    await irc_send(writer,':huddle 366 '+nick+' '+arg.split()[0]+' :End of /NAMES list')
            else:
                await irc_send(writer,':huddle 421 '+nick+' '+cmd+' :Unknown command')
    except Exception: pass
    finally:
        if nick and irc_clients.get(nick) is writer: del irc_clients[nick]
        irc_joined.pop(writer,None)
        try: writer.close(); await writer.wait_closed()
        except: pass
async def irc_event_pump():
    seen={}
    while True:
        try:
            c=db()
            rows=c.execute('SELECT e.channel_id,e.event_id,e.frame,w.slug,ch.name FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id ORDER BY e.rowid').fetchall()
            for r in rows:
                if r['event_id']<=seen.get(r['channel_id'],0): continue
                seen[r['channel_id']]=r['event_id']
                f=json.loads(r['frame'])
                if f.get('type')!='message.created': continue
                m=f.get('message') or {}; au=m.get('author') or {}; target='#%s/%s'%(r['slug'],r['name'])
                src=irc_sources.pop((r['channel_id'],r['event_id']),None)
                for w, chans in list(irc_joined.items()):
                    if r['channel_id'] not in chans or w is src: continue
                    await irc_send(w,':%s!%s@huddle PRIVMSG %s :%s'%(au.get('username',''),au.get('username',''),target,m.get('body','')))
            c.close()
        except Exception: pass
        await asyncio.sleep(.05)

async def irc_main(port):
    asyncio.create_task(irc_event_pump())
    srv=await asyncio.start_server(irc_client,'0.0.0.0',port)
    async with srv: await srv.serve_forever()
if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='irc':
    asyncio.run(irc_main(int(sys.argv[2])))

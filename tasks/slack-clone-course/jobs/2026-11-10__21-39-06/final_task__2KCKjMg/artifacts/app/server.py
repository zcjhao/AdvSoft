import asyncio, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
clients={}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    os.makedirs('/app/data',exist_ok=True)
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS members(user_id INTEGER,channel_id INTEGER,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS ws_members(user_id INTEGER,workspace_id INTEGER,role TEXT DEFAULT 'member',PRIMARY KEY(user_id,workspace_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));
    '''); c.commit(); c.close()
def userobj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chanobj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def reaction_list(c,mid):
    rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY emoji,user_id',(mid,)).fetchall()
    d={}
    for r in rows: d.setdefault(r['emoji'],[]).append(r['user_id'])
    return [{'emoji':e,'count':len(ids),'user_ids':ids} for e,ids in d.items()]
def msgobj(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    n=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':n,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':reaction_list(c,r['id']),'mentions':[]}
def frame(c,cid,typ,**kw):
    x={'type':typ,'event_id':c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'],'channel_id':cid}; x.update(kw)
    c.execute('INSERT INTO events VALUES(?,?,?)',(cid,x['event_id'],json.dumps(x))); return x
def bearer(req):
    h=req.headers.get('Authorization','')
    tok = h[7:] if h.startswith('Bearer ') else req.query.get('token','')
    if tok.startswith('Bearer '): tok=tok[7:]
    if not tok: return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close(); return r
def auth(req):
    r=bearer(req)
    if not r: raise web.HTTPUnauthorized()
    return r
async def body(req):
    try:return await req.json()
    except:return {}
def j(x,status=200): return web.json_response(x,status=status)
async def health(req): return j({'status':'ok','node_id':NODE})
async def register(req):
    b=await body(req); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: return j({'error':'invalid'},400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
    except sqlite3.IntegrityError: c.rollback(); c.close(); return j({'error':'duplicate'},409)
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return j({'user':userobj(r),'token':tok},201)
async def login(req):
    b=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
    if not r: c.close(); return j({'error':'invalid'},401)
    tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id'])); c.commit(); c.close(); return j({'user':userobj(r),'token':tok})
async def me(req): return j({'user':userobj(auth(req))})
async def workspaces(req):
    u=auth(req); c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN ws_members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
    return j({'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rows]})
async def create_ws(req):
    u=auth(req); b=await body(req); slug=b.get('slug',''); name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name:return j({'error':'invalid'},400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,name,u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO ws_members VALUES(?,?,?)',(u['id'],wid,'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?)',(u['id'],cid)); c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return j({'error':'duplicate'},409)
    c.close(); return j({'workspace':{'id':wid,'slug':slug,'name':name,'owner_id':u['id'],'join_mode':'open'},'general_channel':{'id':cid,'workspace_id':wid,'name':'general','is_private':False,'is_dm':False,'topic':'','is_archived':False}},201)
async def ws_detail(req):
    u=auth(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return j({'error':'not found'},404)
    rows=c.execute('SELECT ch.* FROM channels ch JOIN members m ON m.channel_id=ch.id WHERE ch.workspace_id=? AND m.user_id=?',(w['id'],u['id'])).fetchall(); c.close()
    return j({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[chanobj(x) for x in rows],'read_state':[]})
async def create_channel(req):
    u=auth(req); b=await body(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return j({'error':'not found'},404)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')):c.close();return j({'error':'invalid'},400)
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?)',(u['id'],cid)); c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return j({'error':'duplicate'},409)
    r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return j({'channel':chanobj(r)},201)
async def channel(req):
    u=auth(req); c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return j({'error':'not found'},404)
    c.close();return j({'channel':chanobj(r)})
async def join(req):
    u=auth(req); c=db(); r=c.execute('SELECT ch.*,w.join_mode FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return j({'error':'not found'},404)
    c.execute('INSERT OR IGNORE INTO ws_members(user_id,workspace_id) VALUES(?,?)',(u['id'],r['workspace_id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(u['id'],r['id']));c.commit();c.close();return j({'channel':chanobj(r)})
async def members(req):
    auth(req);c=db(); rows=c.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(req.match_info['id'],)).fetchall();c.close();return j({'members':[userobj(x) for x in rows]})
async def post(req):
    u=auth(req); b=await body(req); bodyv=b.get('body','')
    if not isinstance(bodyv,str) or not bodyv.strip():return j({'error':'blank'},400)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return j({'error':'not found'},404)
    if ch['is_archived']:c.close();return j({'error':'archived'},423)
    if not c.execute('SELECT 1 FROM members WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone():c.close();return j({'error':'forbidden'},403)
    parent=b.get('parent_id'); c.execute('BEGIN IMMEDIATE'); ts=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],bodyv,parent,ts));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();m=msgobj(c,r); typ='message.reply' if parent else 'message.created';f=frame(c,ch['id'],typ,message=m);c.commit();c.close();return j({'message':m},201)
async def messages(req):
    u=auth(req);c=db(); cid=int(req.match_info['id'])
    if not c.execute('SELECT 1 FROM members WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone():c.close();return j({'error':'forbidden'},403)
    lim=int(req.query.get('limit','50')); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(cid,min(lim,200))).fetchall();out=[msgobj(c,r) for r in rows];c.close();return j({'messages':out,'next_cursor':None})
async def replies(req):
    u=auth(req);c=db(); p=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not p:c.close();return j({'error':'not found'},404)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(p['id'],)).fetchall();out=[msgobj(c,r) for r in rows];c.close();return j({'replies':out,'next_cursor':None})
async def edit(req):
    u=auth(req);b=await body(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return j({'error':'not found'},404)
    if r['deleted']:c.close();return j({'error':'deleted'},409)
    if r['author_id']!=u['id']:c.close();return j({'error':'forbidden'},403)
    if not str(b.get('body','')).strip():c.close();return j({'error':'blank'},400)
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),r['id']));r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();m=msgobj(c,r);frame(c,r['channel_id'],'message.updated',message=m);c.commit();c.close();return j({'message':m})
async def delete(req):
    u=auth(req);c=db();r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return j({'error':'not found'},404)
    if r['deleted']:c.close();return j({'deleted':True})
    if r['author_id']!=u['id'] and r['owner_id']!=u['id']:c.close();return j({'error':'forbidden'},403)
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));frame(c,r['channel_id'],'message.deleted',message_id=r['id']);c.commit();c.close();return j({'deleted':True})
async def react(req):
    u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r or r['deleted']:c.close();return j({'error':'not found'},404)
    b=await body(req);em=b.get('emoji','')
    if not em:c.close();return j({'error':'invalid'},400)
    c.execute('BEGIN IMMEDIATE'); old=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],em)).fetchone()
    if not old:c.execute('INSERT INTO reactions VALUES(?,?,?)',(r['id'],u['id'],em));frame(c,r['channel_id'],'reaction.added',message_id=r['id'],emoji=em,user_id=u['id'])
    c.commit();x=reaction_list(c,r['id']);c.close();return j({'reactions':x})
async def unreact(req):
    u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r or r['deleted']:c.close();return j({'error':'not found'},404)
    em=req.match_info['emoji'];c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],em)).fetchone()
    if old:c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],em));frame(c,r['channel_id'],'reaction.removed',message_id=r['id'],emoji=em,user_id=u['id'])
    c.commit();x=reaction_list(c,r['id']);c.close();return j({'reactions':x})
async def ws(req):
    if not bearer(req): raise web.HTTPUnauthorized()
    w=web.WebSocketResponse();await w.prepare(req); clients[w]={}; c=db(); last={}
    try:
      async for z in w:
        x=json.loads(z.data);cid=int(x.get('channel_id',0)); rows=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone(); head=rows['n']
        if x.get('type')=='subscribe': last[cid]=head; clients[w][cid]=head; await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
        elif x.get('type')=='resume':
          since=int(x.get('since_event_id',0))
          for r in c.execute('SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):await w.send_str(r['frame'])
          last[cid]=head; clients[w][cid]=head; await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
    finally:clients.pop(w,None);c.close()
    return w
async def pump():
    seen={}
    while True:
      try:
       c=db()
       for r in c.execute('SELECT channel_id,event_id,frame FROM events ORDER BY channel_id,event_id'):
        k=(r['channel_id'],r['event_id'])
        if k in seen:continue
        seen[k]=1
        for w, subs in list(clients.items()):
         if r['channel_id'] not in subs or r['event_id'] <= subs[r['channel_id']]:
          continue
         try:
          await w.send_str(r['frame']); subs[r['channel_id']]=r['event_id']
         except: clients.pop(w,None)
       c.close()
      except Exception: pass
      await asyncio.sleep(.05)
async def start(app): init();app['task']=asyncio.create_task(pump())
app=web.Application(client_max_size=11*1024*1024); app.on_startup.append(start)
app.router.add_get('/',lambda req:web.FileResponse('/app/static/index.html'))
app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me)
app.router.add_get('/api/workspaces',workspaces);app.router.add_post('/api/workspaces',create_ws);app.router.add_get('/api/workspaces/{slug}',ws_detail);app.router.add_post('/api/workspaces/{slug}/channels',create_channel)
async def patch_channel(req):
    u=auth(req); b=await body(req); c=db(); r=c.execute('SELECT ch.*,w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return j({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM members WHERE user_id=? AND channel_id=?',(u['id'],r['id'])).fetchone() and r['owner_id']!=u['id']:c.close();return j({'error':'forbidden'},403)
    if 'topic' not in b or len(str(b.get('topic','')))>250:c.close();return j({'error':'invalid'},400)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(str(b['topic']),r['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(r['id'],)).fetchone();c.close();return j({'channel':chanobj(r)})
app.router.add_get('/api/channels/{id}',channel);app.router.add_patch('/api/channels/{id}',patch_channel);app.router.add_post('/api/channels/{id}/join',join);app.router.add_get('/api/channels/{id}/members',members);app.router.add_post('/api/channels/{id}/messages',post);app.router.add_get('/api/channels/{id}/messages',messages)
app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_patch('/api/messages/{id}',edit);app.router.add_delete('/api/messages/{id}',delete);app.router.add_post('/api/messages/{id}/reactions',react);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);app.router.add_get('/api/ws',ws)

irc_clients={}
ircsessions={}
async def irc_send(w,line):
    w.write((line+"\r\n").encode()); await w.drain()
def irc_lookup(name):
    if not name.startswith('#') or '/' not in name: return None
    slug,chname=name[1:].split('/',1)
    c=db(); r=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,chname)).fetchone(); c.close(); return r
async def irc_client(reader,writer):
    nick=None; token=None; userrow=None; registered=False; joined=set()
    ircsessions[writer]=joined
    peer=writer.get_extra_info('peername'); host='huddle'
    async def reply(code,*parts):
        n=nick or '*'
        await irc_send(writer,':huddle %s %s %s'%(code,n,' '.join(str(x) for x in parts)))
    async def register():
        nonlocal registered,userrow
        if registered:return
        c=db(); userrow=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token or '',)).fetchone();c.close()
        if not userrow:
            await irc_send(writer,':huddle 464 * :Password incorrect'); writer.close(); return
        if not nick:return
        if nick in irc_clients and irc_clients[nick] is not writer:
            await irc_send(writer,':huddle 433 * %s :Nickname is already in use'%nick); return
        registered=True; irc_clients[nick]=writer
        for code,text in [('001','Welcome to huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 o o'),('005','CHANTYPES=#')]:
            await irc_send(writer,':huddle %s %s :%s'%(code,nick,text))
        await irc_send(writer,':huddle 422 %s :MOTD File is missing'%nick)
    try:
        while not reader.at_eof():
            raw=await reader.readline()
            if not raw:break
            line=raw.decode(errors='replace').strip('\r\n')
            if not line:continue
            parts=line.split(' ',1); cmd=parts[0].upper(); rest=parts[1] if len(parts)>1 else ''
            args=rest.split()
            if cmd=='PASS': token=rest.strip()
            elif cmd=='NICK':
                nn=args[0] if args else ''
                if nn and nn in irc_clients and irc_clients[nn] is not writer:
                    await irc_send(writer,':huddle 433 * %s :Nickname is already in use'%nn)
                else: nick=nn; await register()
            elif cmd=='USER': await register()
            elif cmd=='PING':
                val=rest.lstrip(':'); await irc_send(writer,':huddle PONG huddle :'+val)
            elif cmd=='QUIT':
                break
            elif not registered:
                continue
            elif cmd=='JOIN':
                name=args[0] if args else ''
                ch=irc_lookup(name)
                if not ch:
                    await reply('403',name,':No such channel'); continue
                c=db(); c.execute('INSERT OR IGNORE INTO ws_members(user_id,workspace_id) SELECT ?,workspace_id FROM channels WHERE id=?',(userrow['id'],ch['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(userrow['id'],ch['id']));c.commit();c.close();joined.add(ch['id'])
                prefix='%s!%s@huddle'%(nick,userrow['username'])
                await irc_send(writer,':%s JOIN %s'%(prefix,name))
                if ch['topic']: await reply('332',name,':'+ch['topic'])
                else: await reply('331',name,':No topic is set')
                await reply('353','=','%s :%s'%(name,nick))
                await reply('366',name,':End of /NAMES list')
            elif cmd=='PRIVMSG':
                if len(args)<2:continue
                name=args[0]; body=rest.split(None,1)[1].lstrip(':'); ch=irc_lookup(name)
                if not ch or ch['id'] not in joined: continue
                c=db(); c.execute('BEGIN IMMEDIATE'); ts=now();c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],userrow['id'],body,ts));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();m=msgobj(c,r);frame(c,ch['id'],'message.created',message=m);c.commit();c.close()
            elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):
                continue
            else:
                await reply('421',cmd,':Unknown command')
    finally:
        if nick and irc_clients.get(nick) is writer: del irc_clients[nick]
        ircsessions.pop(writer,None)
        writer.close()
        try: await writer.wait_closed()
        except: pass
async def irc_event_pump():
    seen={}
    while True:
        try:
            c=db()
            rows=c.execute('SELECT e.channel_id,e.event_id,e.frame,ch.name cname,w.slug slug,m.author_id,u.username FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id LEFT JOIN messages m ON e.frame LIKE "%%message.created%%" AND m.id=json_extract(e.frame,"$.message.id") LEFT JOIN users u ON u.id=m.author_id ORDER BY e.channel_id,e.event_id').fetchall()
            for r in rows:
                k=(r['channel_id'],r['event_id'])
                if k in seen: continue
                seen[k]=1
                f=json.loads(r['frame'])
                if f.get('type')!='message.created': continue
                body=(f.get('message') or {}).get('body','')
                author=r['username'] or ''
                name='#%s/%s'%(r['slug'],r['cname'])
                line=':%s!%s@huddle PRIVMSG %s :%s'%(author,author,name,body)
                for w,joined in list(ircsessions.items()):
                    if r['channel_id'] in joined:
                        try:
                            await irc_send(w,line)
                        except Exception:
                            ircsessions.pop(w,None)
            c.close()
        except Exception:
            pass
        await asyncio.sleep(.05)
async def irc_main(port):
    srv=await asyncio.start_server(irc_client,'0.0.0.0',port)
    pump=asyncio.create_task(irc_event_pump())
    try:
        async with srv: await srv.serve_forever()
    finally:
        pump.cancel()
if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='node': web.run_app(app,host='127.0.0.1',port=PORT)
    elif len(sys.argv)>1 and sys.argv[1]=='irc': asyncio.run(irc_main(int(sys.argv[2])))


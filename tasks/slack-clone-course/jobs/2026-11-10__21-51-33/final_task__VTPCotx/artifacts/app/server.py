import asyncio, json, os, re, secrets, sqlite3, sys, hashlib
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

DB='/app/data/app.db'
NODE=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='node' else 0
PORT=int(sys.argv[3]) if len(sys.argv)>3 and sys.argv[1]=='node' else 8000
SOCKETS=set()
SUBS={}

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB, timeout=5, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=db()
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS workspace_members(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT 'member',UNIQUE(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));''')
    c.commit(); c.close()
def ujson(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def cjson(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def mjson(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    rx=[]
    for x in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
        ids=[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]
        rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
    n=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':ujson(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':n,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':rx,'mentions':[]}
def err(msg,status): return web.json_response({'error':msg},status=status)
async def body(req):
    try:return await req.json()
    except:return {}
def auth(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): raise web.HTTPUnauthorized()
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
    if not r: raise web.HTTPUnauthorized()
    return r
def event(c,channel,typ,**kw):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(channel,)).fetchone()[0]
    frame={'type':typ,'event_id':n,'channel_id':channel,**kw}
    c.execute('INSERT INTO events VALUES(?,?,?)',(channel,n,json.dumps(frame))); return frame
def channel(c,i): return c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone()
def member(c,ch,uid): return c.execute('SELECT 1 FROM members WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone()
def owner(c,ch,uid):
    r=c.execute('SELECT w.owner_id FROM channels x JOIN workspaces w ON w.id=x.workspace_id WHERE x.id=?',(ch,)).fetchone()
    return r and r[0]==uid
async def health(req): return web.json_response({'status':'ok','node_id':NODE})
async def register(req):
    d=await body(req); un=d.get('username','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(d.get('password',''))<8:return err('invalid',400)
    c=db()
    try:
        for attempt in range(12):
            try:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,d['password'],d.get('display_name') or un))
                uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
                break
            except sqlite3.OperationalError as ex:
                c.rollback()
                if 'locked' not in str(ex).lower() or attempt == 11: raise
                import time; time.sleep(.08)
    except sqlite3.IntegrityError:c.close(); return err('duplicate',409)
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return web.json_response({'user':ujson(r),'token':tok},status=201)
async def login(req):
    d=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone()
    if not r:c.close(); return err('invalid',401)
    t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return web.json_response({'user':ujson(r),'token':t})
async def me(req): return web.json_response({'user':ujson(auth(req))})
async def workspaces(req):
    u=auth(req); c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
    return web.json_response({'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']} for r in rows]})
async def create_ws(req):
    u=auth(req); d=await body(req)
    if not re.fullmatch(r'[a-z0-9-]{2,32}',d.get('slug','')):return err('invalid',400)
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO workspace_members VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id'])); c.commit()
    except sqlite3.IntegrityError:c.rollback();c.close();return err('duplicate',409)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=channel(c,cid); c.close(); return web.json_response({'workspace':{'id':wid,'slug':w['slug'],'name':w['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':cjson(ch)},status=201)
async def ws_detail(req):
    u=auth(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    rows=c.execute('SELECT x.* FROM channels x JOIN members m ON m.channel_id=x.id WHERE x.workspace_id=? AND m.user_id=?',(w['id'],u['id'])).fetchall();c.close()
    return web.json_response({'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':w['join_mode']},'channels':[cjson(x) for x in rows],'read_state':[]})
async def create_ch(req):
    u=auth(req); d=await body(req); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return err('not found',404)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):c.close();return err('invalid',400)
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT OR IGNORE INTO workspace_members VALUES(?,?,?)',(w['id'],u['id'],'member'));c.execute('INSERT INTO members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:c.close();return err('duplicate',409)
    r=channel(c,cid);c.close();return web.json_response({'channel':cjson(r)},status=201)
async def patch_channel(req):
    u=auth(req); d=await body(req); c=db(); r=channel(c,int(req.match_info['id']))
    if not r: c.close(); return err('not found',404)
    if not member(c,r['id'],u['id']) and not owner(c,r['id'],u['id']): c.close(); return err('forbidden',403)
    topic=d.get('topic','')
    if not isinstance(topic,str) or len(topic)>250: c.close(); return err('invalid',400)
    try:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,r['id']))
        nr=channel(c,r['id'])
        try: event(c,r['id'],'channel.updated',channel=cjson(nr))
        except Exception: pass
        c.commit()
    except Exception:
        c.rollback(); c.close(); return err('update failed',500)
    c.close(); return web.json_response({'channel':cjson(nr)})

async def join(req):
    u=auth(req);c=db();r=channel(c,int(req.match_info['id']))
    if not r:c.close();return err('not found',404)
    c.execute('INSERT OR IGNORE INTO workspace_members(workspace_id,user_id) VALUES(?,?)',(r['workspace_id'],u['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(r['id'],u['id']));c.commit();c.close();return web.json_response({'channel':cjson(r)})
async def members(req):
    u=auth(req);c=db();r=channel(c,int(req.match_info['id']))
    if not r:c.close();return err('not found',404)
    rows=c.execute('SELECT u.* FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(r['id'],)).fetchall();c.close();return web.json_response({'members':[ujson(x) for x in rows]})
async def post(req):
    u=auth(req); d=await body(req); text=d.get('body','')
    if not text or not text.strip():return err('blank',400)
    c=db();r=channel(c,int(req.match_info['id']))
    if not r:c.close();return err('not found',404)
    if r['is_archived']:c.close();return err('archived',423)
    if not member(c,r['id'],u['id']):c.close();return err('forbidden',403)
    parent=d.get('parent_id')
    if parent and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(parent,r['id'])).fetchone():c.close();return err('parent',404)
    c.execute('BEGIN IMMEDIATE'); ts=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(r['id'],u['id'],text,parent,ts));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); frame=event(c,r['id'],'message.reply' if parent else 'message.created',message=mjson(c,mr));c.commit();c.close();return web.json_response({'message':frame['message']},status=201)
async def listing(req):
    u=auth(req);c=db(); ch=int(req.match_info['id']); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC',(ch,)).fetchall();out=[mjson(c,x) for x in rows];c.close();return web.json_response({'messages':out[:int(req.query.get('limit','50'))],'next_cursor':None})
async def replies(req):
    auth(req);c=db(); rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(req.match_info['id'],)).fetchall();out=[mjson(c,x) for x in rows];c.close();return web.json_response({'replies':out,'next_cursor':None})
async def edit(req):
    u=auth(req);d=await body(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    if r['deleted']:c.close();return err('deleted',409)
    if r['author_id']!=u['id']:c.close();return err('forbidden',403)
    if not d.get('body','').strip():c.close();return err('blank',400)
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],now(),r['id']));nr=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();f=event(c,r['channel_id'],'message.updated',message=mjson(c,nr));c.commit();c.close();return web.json_response({'message':f['message']})
async def delete(req):
    u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    if r['deleted']:c.close();return web.json_response({'deleted':True})
    if r['author_id']!=u['id'] and not owner(c,r['channel_id'],u['id']):c.close();return err('forbidden',403)
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));event(c,r['channel_id'],'message.deleted',message_id=r['id']);c.commit();c.close();return web.json_response({'deleted':True})
async def react(req):
    u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    d=await body(req);em=d.get('emoji','')
    c.execute('BEGIN IMMEDIATE'); existed=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],em)).fetchone()
    if not existed:c.execute('INSERT INTO reactions VALUES(?,?,?)',(r['id'],u['id'],em));event(c,r['channel_id'],'reaction.added',message_id=r['id'],emoji=em,user_id=u['id'])
    c.commit(); rx=mjson(c,r)['reactions'];c.close();return web.json_response({'reactions':rx})
async def unreact(req):
    u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return err('not found',404)
    em=req.match_info['emoji'];c.execute('BEGIN IMMEDIATE');n=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],em)).rowcount
    if n:event(c,r['channel_id'],'reaction.removed',message_id=r['id'],emoji=em,user_id=u['id'])
    c.commit();rx=mjson(c,r)['reactions'];c.close();return web.json_response({'reactions':rx})
async def ws(req):
    tok=req.query.get('token',''); c=db()
    r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close()
    if not r: return err('unauthorized',401)
    sock=web.WebSocketResponse(); await sock.prepare(req); SOCKETS.add(sock); SUBS[sock]={}
    try:
        async for msg in sock:
            if msg.type==WSMsgType.TEXT:
                d=json.loads(msg.data); ch=int(d.get('channel_id',0)); c=db()
                head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
                if d.get('type')=='subscribe':
                    SUBS[sock][ch]=head
                    await sock.send_json({'type':'subscribed','channel_id':ch,'head_event_id':head})
                elif d.get('type')=='resume':
                    since=int(d.get('since_event_id',0))
                    rows=c.execute('SELECT frame,event_id FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
                    for x in rows: await sock.send_str(x['frame'])
                    head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
                    SUBS[sock][ch]=head
                    await sock.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
                c.close()
    finally:
        SOCKETS.discard(sock); SUBS.pop(sock,None)
    return sock

async def event_pump():
    while True:
        try:
            for sock in list(SOCKETS):
                if sock.closed: continue
                for ch,last in list(SUBS.get(sock,{}).items()):
                    c=db()
                    rows=c.execute('SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last)).fetchall()
                    c.close()
                    for row in rows:
                        try:
                            await sock.send_str(row['frame'])
                            SUBS[sock][ch]=row['event_id']
                        except Exception:
                            break
        except Exception:
            pass
        await asyncio.sleep(.05)

async def start_pump(app):
    app['pump']=asyncio.create_task(event_pump())

async def cleanup_pump(app):
    app['pump'].cancel()

async def root(req): return web.FileResponse('/app/static/index.html')
def app():
 init();a=web.Application(client_max_size=11*1024*1024);a.router.add_get('/',root);a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me);a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',create_ws);a.router.add_get('/api/workspaces/{slug}',ws_detail);a.router.add_post('/api/workspaces/{slug}/channels',create_ch);a.router.add_post('/api/channels/{id}/join',join);a.router.add_patch('/api/channels/{id}',patch_channel);a.router.add_get('/api/channels/{id}/members',members);a.router.add_post('/api/channels/{id}/messages',post);a.router.add_get('/api/channels/{id}/messages',listing);a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_patch('/api/messages/{id}',edit);a.router.add_delete('/api/messages/{id}',delete);a.router.add_post('/api/messages/{id}/reactions',react);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);a.router.add_get('/api/ws',ws);a.on_startup.append(start_pump);a.on_cleanup.append(cleanup_pump);return a

IRC_NICKS={}
IRC_CLIENTS=set()
IRC_LAST={}
IRC_STATES={}
IRC_SENT=set()

def irc_channel(name):
    if not name.startswith('#') or '/' not in name[1:]:
        return None
    slug,chname=name[1:].split('/',1)
    c=db()
    r=c.execute('SELECT x.* FROM channels x JOIN workspaces w ON w.id=x.workspace_id WHERE w.slug=? AND x.name=?',(slug,chname)).fetchone()
    c.close()
    return r

async def irc_send(w, line):
    try:
        w.write((line+'\r\n').encode()); await w.drain()
    except Exception:
        pass

async def irc_client(reader,writer):
    state={'pass':None,'nick':None,'user':None,'registered':False,'joined':set()}
    IRC_CLIENTS.add(writer)
    IRC_STATES[writer]=state
    async def reply(code,*parts):
        nick=state['nick'] or '*'
        await irc_send(writer, ':huddle '+code+' '+nick+' '+' '.join(parts))
    async def register():
        if state['registered'] or not state['pass'] or not state['nick'] or not state['user']:
            return True
        c=db(); u=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(state['pass'],)).fetchone(); c.close()
        if not u:
            await irc_send(writer, ':huddle 464 * :Password incorrect')
            return False
        old=IRC_NICKS.get(state['nick'].lower())
        if old is not None and old is not writer:
            await irc_send(writer, ':huddle 433 * '+state['nick']+' :Nickname is already in use')
            state['nick']=None
            return True
        IRC_NICKS[state['nick'].lower()]=writer; state['registered']=True; state['uid']=u['id']; state['username']=u['username']
        await irc_send(writer, ':huddle 001 '+state['nick']+' :Welcome to Huddle')
        await irc_send(writer, ':huddle 002 '+state['nick']+' :Your host is huddle')
        await irc_send(writer, ':huddle 003 '+state['nick']+' :This server was created today')
        await irc_send(writer, ':huddle 004 '+state['nick']+' huddle 1.0 oi')
        await irc_send(writer, ':huddle 005 '+state['nick']+' CHANTYPES=# :are supported by this server')
        await irc_send(writer, ':huddle 422 '+state['nick']+' :MOTD File is missing')
        return True
    try:
        while not reader.at_eof():
            raw=await reader.readline()
            if not raw: break
            line=raw.decode(errors='ignore').strip('\r\n')
            if not line: continue
            bits=line.split(' ',1); cmd=bits[0].upper(); arg=bits[1] if len(bits)>1 else ''
            if cmd=='PASS':
                state['pass']=arg.strip().lstrip(':')
            elif cmd=='NICK':
                nick=arg.strip().split()[0] if arg.strip() else ''
                if nick and nick.lower() in IRC_NICKS and IRC_NICKS[nick.lower()] is not writer:
                    await irc_send(writer, ':huddle 433 * '+nick+' :Nickname is already in use')
                else:
                    if state['nick']: IRC_NICKS.pop(state['nick'].lower(),None)
                    state['nick']=nick
                    if not await register(): break
            elif cmd=='USER':
                state['user']=arg.split()[0] if arg else ''
                if not await register(): break
            elif cmd=='PING':
                token=arg.strip().lstrip(':')
                await irc_send(writer, ':huddle PONG huddle :'+token)
            elif cmd=='QUIT':
                break
            elif not state['registered']:
                if cmd not in ('PASS','NICK','USER','PING'):
                    await reply('421',cmd,':Unknown command')
            elif cmd=='PRIVMSG':
                if ' :' in arg:
                    target,text=arg.split(' :',1)
                else:
                    z=arg.split(' ',1); target=z[0]; text=z[1] if len(z)>1 else ''
                ch=irc_channel(target)
                if not ch or ch['is_private'] or ch['id'] not in state['joined']:
                    await reply('403',target,':No such channel')
                    continue
                c=db()
                try:
                    c.execute('BEGIN IMMEDIATE')
                    ts=now()
                    c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],state['uid'],text,ts))
                    mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                    mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                    frame=event(c,ch['id'],'message.created',message=mjson(c,mr))
                    c.commit()
                    IRC_SENT.add((ch['id'], frame['event_id']))
                    line=':'+state['username']+'!'+state['username']+'@huddle PRIVMSG '+target+' :'+text
                    for ow in list(IRC_CLIENTS):
                        ost=IRC_STATES.get(ow)
                        if ow is not writer and ost and ch['id'] in ost.get('joined',set()):
                            await irc_send(ow,line)
                except Exception:
                    c.rollback()
                finally: c.close()
            elif cmd=='JOIN':
                name=arg.split()[0] if arg else ''
                ch=irc_channel(name)
                if not ch or ch['is_private']:
                    c=db(); ok=bool(ch and c.execute('SELECT 1 FROM members WHERE channel_id=? AND user_id=?',(ch['id'],state['uid'])).fetchone()); c.close()
                    if not ok: await reply('403',name,':No such channel'); continue
                c=db(); c.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(ch['id'],state['uid']));c.commit()
                rows=c.execute('SELECT u.username FROM users u JOIN members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall();c.close()
                state['joined'].add(ch['id'])
                pref=state['nick']+'!'+state['username']+'@huddle'
                await irc_send(writer, ':'+pref+' JOIN '+name)
                if ch['topic']: await reply('332',name,':'+ch['topic'])
                else: await reply('331',name,':No topic is set')
                names=' '.join([state['nick']]+[x['username'] for x in rows if x['username']!=state['username']])
                await irc_send(writer, ':huddle 353 '+state['nick']+' = '+name+' :'+names)
                await irc_send(writer, ':huddle 366 '+state['nick']+' '+name+' :End of /NAMES list')
            elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):
                continue
            else:
                await reply('421',cmd,':Unknown command')
    except Exception:
        pass
    finally:
        if state['nick']: IRC_NICKS.pop(state['nick'].lower(),None)
        IRC_CLIENTS.discard(writer); IRC_STATES.pop(writer,None)
        try: writer.close(); await writer.wait_closed()
        except: pass

async def irc_event_pump():
    while True:
        try:
            c=db()
            chans=c.execute('SELECT channels.id AS id, workspaces.slug AS slug, channels.name AS name FROM channels JOIN workspaces ON workspaces.id=channels.workspace_id').fetchall()
            for ch in chans:
                last=IRC_LAST.get(ch['id'])
                if last is None:
                    last=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(ch['id'],)).fetchone()[0]
                    IRC_LAST[ch['id']]=last
                rows=c.execute("SELECT event_id,frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(ch['id'],last)).fetchall()
                if rows:
                    IRC_LAST[ch['id']]=rows[-1]['event_id']
                    for row in rows:
                        if (ch['id'], row['event_id']) in IRC_SENT:
                            IRC_SENT.discard((ch['id'], row['event_id']))
                            continue
                        f=json.loads(row['frame'])
                        if f.get('type')!='message.created': continue
                        msg=f.get('message',{})
                        author=msg.get('author',{}).get('username','user')
                        target='#'+ch['slug']+'/'+ch['name']
                        line=':'+author+'!'+author+'@huddle PRIVMSG '+target+' :'+msg.get('body','')
                        for w in list(IRC_CLIENTS):
                            st=IRC_STATES.get(w)
                            if st and ch['id'] in st.get('joined',set()):
                                await irc_send(w,line)
            c.close()
        except Exception:
            pass
        await asyncio.sleep(.05)

async def irc_main(port):
    server=await asyncio.start_server(irc_client,'0.0.0.0',port)
    task=asyncio.create_task(irc_event_pump())
    async with server:
        try: await server.serve_forever()
        finally: task.cancel()

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='node': web.run_app(app(),host='127.0.0.1',port=PORT)
 elif len(sys.argv)>1 and sys.argv[1]=='irc': asyncio.run(irc_main(int(sys.argv[2])))

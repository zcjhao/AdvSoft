import asyncio, sqlite3, json, os, sys, re, secrets, hashlib, datetime, html
from aiohttp import web

DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
def db():
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS wmembers(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT "member",UNIQUE(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,frame TEXT,UNIQUE(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    '''); c.commit(); c.close()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def userj(r):
    return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def chan(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def msgj(c,r):
    u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
    rs=[]
    for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
        ids=[z['user_id'] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]
        rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
    rc=c.execute('select count(*) n from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':rc,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':rs,'mentions':[]}
def auth(req):
    t=req.headers.get('Authorization','')
    if t.startswith('Bearer '): t=t[7:]
    else: t=req.query.get('token','')
    if not t: return None
    c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone(); c.close(); return r
def resp(data,status=200): return web.json_response(data,status=status)
def jbody(req): return req.json()
def append(c,cid,typ,**kw):
    n=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(cid,)).fetchone()['n']
    f={'type':typ,'event_id':n,'channel_id':cid}; f.update(kw); c.execute('insert into events values(?,?,?)',(cid,n,json.dumps(f))); return f
def hasmember(c,cid,uid): return c.execute('select 1 from members where channel_id=? and user_id=?',(cid,uid)).fetchone() is not None
async def health(req): return resp({'status':'ok','node_id':int(req.app['node'])})
async def register(req):
    try:
        b=await jbody(req); un=b.get('username',''); pw=b.get('password','')
        if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return resp({'error':'invalid registration'},400)
        c=db(); c.execute('begin immediate')
        try: c.execute('insert into users(username,password,display_name) values(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un))
        except sqlite3.IntegrityError: c.rollback(); c.close(); return resp({'error':'duplicate'},409)
        uid=c.execute('select last_insert_rowid() x').fetchone()['x']; tok=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(tok,uid)); c.commit(); u=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return resp({'user':userj(u),'token':tok},201)
    except Exception as e:return resp({'error':str(e)},400)
async def login(req):
    b=await jbody(req); c=db(); u=c.execute('select * from users where username=? and password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
    if not u:c.close(); return resp({'error':'invalid credentials'},401)
    t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,u['id'])); c.commit(); c.close(); return resp({'user':userj(u),'token':t})
async def me(req):
    u=auth(req)
    return resp({'user':userj(u)}) if u else resp({'error':'unauthorized'},401)
async def workspaces(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db(); rows=c.execute('select distinct w.* from workspaces w join wmembers m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall(); c.close()
    return resp({'workspaces':[dict(r) for r in rows]})
async def create_ws(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    b=await jbody(req); slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return resp({'error':'invalid slug'},400)
    c=db()
    try:
        c.execute('begin immediate'); c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,b.get('name',''),u['id'])); wid=c.execute('select last_insert_rowid() x').fetchone()['x']; c.execute('insert into wmembers values(?,?,?)',(wid,u['id'],'owner')); c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=c.execute('select last_insert_rowid() x').fetchone()['x']; c.execute('insert into members values(?,?)',(cid,u['id'])); c.commit(); w=c.execute('select * from workspaces where id=?',(wid,)).fetchone(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone(); c.close(); return resp({'workspace':dict(w),'general_channel':chan(ch)},201)
    except sqlite3.IntegrityError:c.rollback();c.close();return resp({'error':'duplicate'},409)
async def wsdetail(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db(); w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return resp({'error':'not found'},404)
    rows=c.execute('select c.* from channels c join members m on m.channel_id=c.id where c.workspace_id=? and m.user_id=?',(w['id'],u['id'])).fetchall();c.close()
    return resp({'workspace':dict(w),'channels':[chan(x) for x in rows],'read_state':[]})
async def channels(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db(); w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return resp({'error':'not found'},404)
    b=await jbody(req); name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name):c.close();return resp({'error':'invalid name'},400)
    if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],name)).fetchone():c.close();return resp({'error':'duplicate'},409)
    c.execute('begin immediate'); c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('select last_insert_rowid() x').fetchone()['x'];c.execute('insert into members values(?,?)',(cid,u['id']));c.commit();r=c.execute('select * from channels where id=?',(cid,)).fetchone();c.close();return resp({'channel':chan(r)},201)
async def patch_channel(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    b=await jbody(req); c=db(); r=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    if 'topic' not in b or len(str(b.get('topic','')))>250:c.close();return resp({'error':'invalid topic'},400)
    c.execute('begin immediate');c.execute('update channels set topic=? where id=?',(str(b['topic']),r['id']));r=c.execute('select * from channels where id=?',(r['id'],)).fetchone();append(c,r['id'],'channel.updated',channel=chan(r));c.commit();c.close();return resp({'channel':chan(r)})
async def join(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db();r=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    c.execute('insert or ignore into members values(?,?)',(r['id'],u['id']));c.commit();c.close();return resp({'channel':chan(r)})
async def members(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db();r=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    out=[userj(x) for x in c.execute('select u.* from users u join members m on m.user_id=u.id where m.channel_id=?',(r['id'],))];c.close();return resp({'members':out})
async def post(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    b=await jbody(req); body=b.get('body','')
    if not body.strip():return resp({'error':'empty'},400)
    c=db();r=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    if r['is_archived']:c.close();return resp({'error':'archived'},423)
    if not hasmember(c,r['id'],u['id']):c.execute('insert or ignore into members values(?,?)',(r['id'],u['id']))
    parent=b.get('parent_id'); c.execute('begin immediate'); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(r['id'],u['id'],body,parent,now())); mid=c.execute('select last_insert_rowid() x').fetchone()['x'];m=c.execute('select * from messages where id=?',(mid,)).fetchone(); f=append(c,r['id'],'message.reply' if parent else 'message.created',message=msgj(c,m));c.commit();c.close();return resp({'message':mjson(f['message'])},201)
def mjson(x): return x
async def listings(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db();r=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    lim=min(int(req.query.get('limit',50)),200); rows=c.execute('select * from messages where channel_id=? and parent_id is null and deleted=0 order by id desc limit ?',(r['id'],lim)).fetchall();out=[msgj(c,x) for x in rows];c.close();return resp({'messages':out,'next_cursor':None})
async def replies(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db(); rows=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(req.match_info['id'],)).fetchall();out=[msgj(c,x) for x in rows];c.close();return resp({'replies':out,'next_cursor':None})
async def edit(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    b=await jbody(req);c=db();r=c.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    if r['deleted']:c.close();return resp({'error':'deleted'},409)
    if r['author_id']!=u['id']:c.close();return resp({'error':'forbidden'},403)
    if not b.get('body','').strip():c.close();return resp({'error':'empty'},400)
    c.execute('begin immediate');t=now();c.execute('update messages set body=?,edited_at=? where id=?',(b['body'],t,r['id']));r=c.execute('select * from messages where id=?',(r['id'],)).fetchone();f=append(c,r['channel_id'],'message.updated',message=msgj(c,r));c.commit();c.close();return resp({'message':f['message']})
async def delete(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db();r=c.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    w=c.execute('select w.owner_id from workspaces w join channels ch on ch.workspace_id=w.id where ch.id=?',(r['channel_id'],)).fetchone()
    if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):c.close();return resp({'error':'forbidden'},403)
    if not r['deleted']:c.execute('begin immediate');c.execute('update messages set deleted=1 where id=?',(r['id'],));append(c,r['channel_id'],'message.deleted',message_id=r['id']);c.commit()
    c.close();return resp({'deleted':True})
async def reaction(req):
    u=auth(req)
    if not u:return resp({'error':'unauthorized'},401)
    c=db();r=c.execute('select * from messages where id=? and deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    b=await jbody(req); e=b.get('emoji',''); existed=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(r['id'],u['id'],e)).fetchone()
    if not existed:c.execute('begin immediate');c.execute('insert into reactions values(?,?,?)',(r['id'],u['id'],e));append(c,r['channel_id'],'reaction.added',message_id=r['id'],emoji=e,user_id=u['id']);c.commit()
    rs=msgj(c,r)['reactions'];c.close();return resp({'reactions':rs})
async def unreact(req):
    u=auth(req); 
    if not u:return resp({'error':'unauthorized'},401)
    c=db();r=c.execute('select * from messages where id=? and deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return resp({'error':'not found'},404)
    e=req.match_info['emoji']; c.execute('begin immediate');c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(r['id'],u['id'],e));append(c,r['channel_id'],'reaction.removed',message_id=r['id'],emoji=e,user_id=u['id']);c.commit();rs=msgj(c,r)['reactions'];c.close();return resp({'reactions':rs})
async def ws(req):
    u=auth(req)
    if not u:return web.Response(status=401)
    w=web.WebSocketResponse();await w.prepare(req); app=req.app; subs=set(); last={}
    async def pump():
        while not w.closed:
            try:
                c=db()
                for cid in list(subs):
                    n=last.get(cid,0); es=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,n)).fetchall()
                    for e in es: await w.send_str(e['frame']);last[cid]=e['event_id']
                c.close()
            except: pass
            await asyncio.sleep(.05)
    task=asyncio.create_task(pump())
    async for x in w:
        if x.type==web.WSMsgType.TEXT:
            try:
                b=json.loads(x.data);cid=int(b.get('channel_id'))
                if b.get('type')=='subscribe':subs.add(cid);c=db();h=c.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(cid,)).fetchone()['n'];c.close();last[cid]=h;await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
                elif b.get('type')=='resume':subs.add(cid);last[cid]=int(b.get('since_event_id',0));await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':last[cid]})
            except: pass
    task.cancel();return w
async def root(req):
    return web.FileResponse('/app/static/index.html')
def appmain(node,port):
    init();a=web.Application();a['node']=node
    a.router.add_get('/',root);a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me)
    a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',create_ws);a.router.add_get('/api/workspaces/{slug}',wsdetail);a.router.add_post('/api/workspaces/{slug}/channels',channels)
    a.router.add_post('/api/channels/{id}/join',join);a.router.add_patch('/api/channels/{id}',patch_channel);a.router.add_get('/api/channels/{id}/members',members);a.router.add_post('/api/channels/{id}/messages',post);a.router.add_get('/api/channels/{id}/messages',listings)
    a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_patch('/api/messages/{id}',edit);a.router.add_delete('/api/messages/{id}',delete);a.router.add_post('/api/messages/{id}/reactions',reaction);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);a.router.add_get('/api/ws',ws)
    web.run_app(a,host='127.0.0.1',port=port)

IRC_CLIENTS=[]
IRC_NICKS=set()
def irc_send(w,line):
    try: w.write((line+"\r\n").encode())
    except: pass
def irc_lookup(name):
    if not name.startswith('#') or '/' not in name: return None
    slug,ch=name[1:].split('/',1); c=db(); r=c.execute('select c.*,w.slug from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(slug,ch)).fetchone(); c.close(); return r
async def irc_client(reader,writer):
    state={'token':None,'user':None,'nick':None,'user_sent':False,'registered':False,'joined':set()}
    IRC_CLIENTS.append((reader,writer,state))
    async def close():
        
        try: IRC_CLIENTS.remove((reader,writer,state))
        except ValueError: pass
        if state['nick']: IRC_NICKS.discard(state['nick'])
        try: writer.close(); await writer.wait_closed()
        except: pass
    async def register():
        if state['registered'] or not state['nick'] or not state['user_sent']: return
        c=db();u=c.execute('select u.* from users u join tokens t on t.user_id=u.id where t.token=?',(state['token'] or '',)).fetchone();c.close()
        if not u: irc_send(writer,':huddle 464 * :Password incorrect'); await close(); return
        if state['nick'] in IRC_NICKS:
            irc_send(writer,':huddle 433 * %s :Nickname is already in use'%state['nick']); state['nick']=None; return
        IRC_NICKS.add(state['nick']);state['user']=u;state['registered']=True
        for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle'),('005','CHANTYPES=#')]:
            irc_send(writer,':huddle %s %s :%s'%(code,state['nick'],text))
        irc_send(writer,':huddle 422 %s :MOTD File is missing'%state['nick'])
    try:
        while True:
            line=await reader.readline()
            if not line: break
            line=line.decode(errors='ignore').strip('\r\n')
            if not line: continue
            parts=line.split(' ',1); cmd=parts[0].upper(); rest=parts[1] if len(parts)>1 else ''
            args=rest.split()
            if cmd=='PASS': state['token']=rest.strip()
            elif cmd=='NICK':
                n=args[0] if args else ''
                if n in IRC_NICKS: irc_send(writer,':huddle 433 * %s :Nickname is already in use'%n)
                else: state['nick']=n; await register()
            elif cmd=='USER': state['user_sent']=True; await register()
            elif cmd=='PING': irc_send(writer,':huddle PONG huddle :'+rest.lstrip(':'))
            elif cmd=='QUIT': break
            elif not state['registered']: continue
            elif cmd=='JOIN':
                name=args[0] if args else ''; ch=irc_lookup(name)
                if not ch: irc_send(writer,':huddle 403 %s %s :No such channel'%(state['nick'],name)); continue
                c=db();c.execute('insert or ignore into members values(?,?)',(ch['id'],state['user']['id']));c.commit();names=[x['username'] for x in c.execute('select u.username from users u join members m on m.user_id=u.id where m.channel_id=?',(ch['id'],))]; names.append(state['nick']) if state['nick'] not in names else None; c.close();state['joined'].add(ch['id'])
                pre='%s!%s@huddle'%(state['nick'],state['user']['username']);irc_send(writer,':%s JOIN %s'%(pre,name));irc_send(writer,':huddle 353 %s = %s :%s'%(state['nick'],name,' '.join(names)));irc_send(writer,':huddle 366 %s %s :End of /NAMES list'%(state['nick'],name))
            elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'): continue
            elif cmd=='PRIVMSG':
                if not args: continue
                name=args[0]; body=rest.split(' ',1)[1] if ' ' in rest else ''; body=body.lstrip(':'); ch=irc_lookup(name)
                if not ch or ch['id'] not in state['joined']: continue
                c=db(); c.execute('begin immediate'); c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(ch['id'],state['user']['id'],body,now())); mid=c.execute('select last_insert_rowid() x').fetchone()['x'];m=c.execute('select * from messages where id=?',(mid,)).fetchone(); f=append(c,ch['id'],'message.created',message=msgj(c,m));c.commit();c.close()
            else: irc_send(writer,':huddle 421 %s %s :Unknown command'%(state['nick'] or '*',cmd))
    finally: await close()
async def irc_event_pump():
    last={}
    while True:
        try:
            c=db()
            rows=c.execute('select e.* from events e join (select channel_id,max(event_id) mx from events group by channel_id) z on z.channel_id=e.channel_id where e.event_id<=z.mx order by e.channel_id,e.event_id').fetchall()
            for e in rows:
                cid=e['channel_id']; eid=e['event_id']
                if eid <= last.get(cid,0): continue
                last[cid]=eid
                f=json.loads(e['frame'])
                if f.get('type')!='message.created': continue
                ch=c.execute('select ch.name,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(cid,)).fetchone()
                if not ch: continue
                m=f.get('message') or {}; au=m.get('author') or {}
                target='#%s/%s'%(ch['slug'],ch['name'])
                for rr,ww,st in list(IRC_CLIENTS):
                    if st.get('registered') and cid in st.get('joined',set()):
                        nick=au.get('username','huddle')
                        irc_send(ww,':%s!%s@huddle PRIVMSG %s :%s'%(nick,nick,target,m.get('body','')))
            c.close()
        except Exception:
            pass
        await asyncio.sleep(.05)
async def irc_main(port):
    srv=await asyncio.start_server(irc_client,'0.0.0.0',port)
    asyncio.create_task(irc_event_pump())
    async with srv: await srv.serve_forever()

if __name__=='__main__':
    init()
    if len(sys.argv)>1 and sys.argv[1]=='irc': asyncio.run(irc_main(int(sys.argv[2])))
    else: appmain(int(sys.argv[2]),int(sys.argv[3]))

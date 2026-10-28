import asyncio, json, os, re, secrets, sqlite3, sys, base64
from datetime import datetime, timezone
from aiohttp import web, WSMsgType

NODE=int(sys.argv[1]); PORT=int(sys.argv[2]); DB='/app/data/app.db'
os.makedirs('/app/data',exist_ok=True)
db=sqlite3.connect(DB,check_same_thread=False); db.row_factory=sqlite3.Row
db.executescript('''PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
''')
db.commit()
sockets={}; seen=0

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def tx():
    if db.in_transaction:
        db.rollback()
    db.execute('BEGIN IMMEDIATE')
def userobj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def chobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def msgobj(r):
    u=db.execute('select * from users where id=?',(r['author_id'],)).fetchone()
    reacts=[]
    for x in db.execute('select emoji,group_concat(user_id) ids,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
        reacts.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':db.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':reacts,'mentions':mentions(r['body'],r['channel_id'],r['author_id'])}
def mentions(body,cid,author):
    c=db.execute('select workspace_id from channels where id=?',(cid,)).fetchone()
    if not c:return []
    mem={x['user_id'] for x in db.execute('select user_id from cmembers where channel_id=?',(cid,))}
    if '@channel' in body:return sorted(mem-{author})
    out=[]
    for m in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',body):
        u=db.execute('select id from users where username=?',(m,)).fetchone()
        if u and u['id'] in mem and u['id']!=author: out.append(u['id'])
    return list(dict.fromkeys(out))
def token_user(req):
    h=req.headers.get('Authorization','')
    if not h.startswith('Bearer '): return None
    x=db.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(h[7:],)).fetchone()
    return x
def j(data,status=200): return web.json_response(data,status=status)
def bad(s,status=400): return j({'error':s},status)
async def body(req):
    try:return await req.json()
    except:return {}
def pag(req,items,key='id',reverse=False):
    lim=req.query.get('limit','50')
    try: lim=int(lim)
    except: return None,None,'bad limit'
    if lim<=0 or lim>200:return None,None,'bad limit'
    cur=req.query.get('cursor')
    if cur:
        try:
            obj=json.loads(base64.urlsafe_b64decode(cur+'=='))
            boundary=obj['i']
            if not isinstance(boundary,int): raise ValueError()
            desc=len(items)>1 and items[0][key]>items[1][key]
            if desc: items=[x for x in items if x[key]<boundary]
            else: items=[x for x in items if x[key]>boundary]
        except:return None,None,'bad cursor'
    out=items[:lim]; nxt=None
    if len(items)>lim:
        nxt=base64.urlsafe_b64encode(json.dumps({'i':out[-1][key]}).encode()).decode().rstrip('=')
    return out,nxt,None
def event(cid,kind,payload):
    n=db.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(cid,)).fetchone()['n']
    db.execute('insert into events(channel_id,event_id,kind,payload) values(?,?,?,?)',(cid,n,kind,json.dumps(payload))); db.commit()
async def broadcast():
    global seen
    while True:
        rows=db.execute('select * from events where id>? order by id',(seen,)).fetchall()
        for r in rows:
            seen=r['id']
            frame=json.loads(r['payload']); frame.update(type=r['kind'],event_id=r['event_id'],channel_id=r['channel_id'])
            for ws,states in list(sockets.items()):
                if r['channel_id'] in states and r['event_id'] > states[r['channel_id']]: 
                    try: await ws.send_json(frame)
                    except: sockets.pop(ws,None)
        await asyncio.sleep(.05)
async def auth(req):
    u=token_user(req)
    return u
async def health(req): return j({'status':'ok','node_id':NODE})
async def register(req):
    b=await body(req); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return bad('invalid')
    try:
        tx(); cur=db.execute('insert into users(username,password,display_name) values(?,?,?)',(un,pw,b.get('display_name') or un)); uid=cur.lastrowid
        t=secrets.token_urlsafe(24); db.execute('insert into tokens values(?,?)',(t,uid));db.commit()
        return j({'user':userobj(db.execute('select * from users where id=?',(uid,)).fetchone()),'token':t},201)
    except sqlite3.IntegrityError: db.rollback();return bad('duplicate',409)
async def createws(req):
    u=await auth(req)
    if not u:return bad('auth',401)
    b=await body(req)
    if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return bad('invalid')
    try:
        tx(); w=db.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(b['slug'],b.get('name',''),u['id'])); wid=w.lastrowid
        db.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); c=db.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));cid=c.lastrowid;db.execute('insert into cmembers values(?,?)',(cid,u['id']));db.commit()
        return j({'workspace':dict(db.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':chobj(db.execute('select * from channels where id=?',(cid,)).fetchone())},201)
    except sqlite3.IntegrityError:db.rollback();return bad('duplicate',409)
async def login(req):
    b=await body(req);u=db.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
    if not u:return bad('bad auth',401)
    t=secrets.token_urlsafe(24);db.execute('insert into tokens values(?,?)',(t,u['id'],));db.commit();return j({'user':userobj(u),'token':t})
async def me(req):
    u=await auth(req)
    return j({'user':userobj(u)}) if u else bad('auth',401)
async def createch(req):
    u=await auth(req)
    if not u:return bad('auth',401)
    slug=req.match_info['slug'];w=db.execute('select * from workspaces where slug=?',(slug,)).fetchone()
    if not w:return bad('not found',404)
    role=db.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role['role']=='guest':return bad('forbidden',403)
    b=await body(req);name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(b.get('topic',''))>250:return bad('invalid')
    if b.get('is_private') and role['role'] not in ('owner','admin'):return bad('forbidden',403)
    if db.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],name)).fetchone():return bad('duplicate',409)
    tx();c=db.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')));cid=c.lastrowid;db.execute('insert into cmembers values(?,?)',(cid,u['id']));db.commit()
    return j({'channel':chobj(db.execute('select * from channels where id=?',(cid,)).fetchone())},201)

async def setrole(req):
    u=await auth(req)
    if not u:return bad('auth',401)
    w=db.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return bad('not found',404)
    me=db.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if not me or me['role'] not in ('owner','admin'):return bad('forbidden',403)
    b=await body(req); role=b.get('role')
    if role not in ('owner','admin','member','guest'):return bad('invalid')
    if role=='owner':return bad('invalid')
    target=db.execute('select user_id,role from members where workspace_id=? and user_id=?',(w['id'],req.match_info['user_id'])).fetchone()
    if not target:return bad('not found',404)
    db.execute('update members set role=? where workspace_id=? and user_id=?',(role,w['id'],target['user_id']));db.commit()
    return j({'member':{'user_id':target['user_id'],'role':role}})
async def wsdetail(req):
    u=await auth(req)
    if not u:return bad('auth',401)
    w=db.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
    if not w:return bad('not found',404)
    chans=[]
    for c in db.execute('select * from channels where workspace_id=? and (is_archived=0 or ?)',(w['id'], req.query.get('include_archived') in ('1','true'))):
        if not c['is_private'] or db.execute('select 1 from cmembers where channel_id=? and user_id=?',(c['id'],u['id'])).fetchone():chans.append(chobj(c))
    rs=[]
    for c in chans:
        x=db.execute('select last_read from reads where user_id=? and channel_id=?',(u['id'],c['id'])).fetchone()
        head=db.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(c['id'],)).fetchone()['n']
        rs.append({'channel_id':c['id'],'last_read_event_id':x['last_read'] if x else 0,'unread_count':unread_count(u['id'],c['id'],x['last_read'] if x else 0),'mention_count':mention_count(u['id'],c['id'],x['last_read'] if x else 0)})
    return j({'workspace':dict(w),'channels':chans,'read_state':rs})
async def join(req):
    u=await auth(req); c=db.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not u:return bad('auth',401)
    if not c:return bad('not found',404)
    w=db.execute('select * from workspaces where id=?',(c['workspace_id'],)).fetchone();m=db.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
    if c['is_private'] and not m:return bad('forbidden',403)
    tx()
    if not m:db.execute('insert into members values(?,?,?)',(w['id'],u['id'],'member'))
    db.execute('insert or ignore into cmembers values(?,?)',(c['id'],u['id']));db.commit()
    event(c['id'],'member.joined',{'user_id':u['id']})
    return j({'channel':chobj(c)})
async def channelop(req):
    u=await auth(req);c=db.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not u:return bad('auth',401)
    if not c:return bad('not found',404)
    role=db.execute('select role from members where workspace_id=? and user_id=?',(c['workspace_id'],u['id'])).fetchone()
    if req.method=='POST' and req.path.endswith('/leave'):
        db.execute('delete from cmembers where channel_id=? and user_id=?',(c['id'],u['id']));db.commit();event(c['id'],'member.left',{'user_id':u['id']});return j({'channel':chobj(c)})
    if req.path.endswith('/archive') or req.path.endswith('/unarchive'):
        if not role or role['role']!='owner':return bad('forbidden',403)
        val=not req.path.endswith('/unarchive');db.execute('update channels set is_archived=? where id=?',(val,c['id']));db.commit();event(c['id'],'channel.unarchived' if not val else 'channel.archived',{'channel':chobj(c)});c=db.execute('select * from channels where id=?',(c['id'],)).fetchone();return j({'channel':chobj(c)})
    if not role or (role['role']!='owner' and not db.execute('select 1 from cmembers where channel_id=? and user_id=?',(c['id'],u['id'])).fetchone()):return bad('forbidden',403)
    b=await body(req)
    if len(b.get('topic',''))>250:return bad('invalid')
    db.execute('update channels set topic=? where id=?',(b.get('topic',c['topic']),c['id']));db.commit();c=db.execute('select * from channels where id=?',(c['id'],)).fetchone();event(c['id'],'channel.updated',{'channel':chobj(c)});c=db.execute('select * from channels where id=?',(c['id'],)).fetchone();return j({'channel':chobj(c)})
async def members(req):
    u=await auth(req);c=db.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not u:return bad('auth',401)
    if not c:return bad('not found',404)
    if not db.execute('select 1 from cmembers where channel_id=? and user_id=?',(c['id'],u['id'])).fetchone() and not db.execute('select 1 from members where workspace_id=? and user_id=? and role=\'owner\'',(c['workspace_id'],u['id'])).fetchone():return bad('forbidden',403)
    return j({'members':[userobj(x) for x in db.execute('select u.* from users u join cmembers m on m.user_id=u.id where m.channel_id=?',(c['id'],))]})
async def messages(req):
    u=await auth(req);c=db.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
    if not u:return bad('auth',401)
    if not c:return bad('not found',404)
    if req.method=='POST' and c['is_archived']:return bad('archived',423)
    if not db.execute('select 1 from cmembers where channel_id=? and user_id=?',(c['id'],u['id'])).fetchone():return bad('forbidden',403)
    if req.method=='GET':
        rows=list(db.execute('select * from messages where channel_id=? and parent_id is null and deleted=0 order by id desc',(c['id'],))); out,n,e=pag(req,rows)
        if e:return bad(e)
        return j({'messages':[msgobj(x) for x in out],'next_cursor':n})
    if c['is_archived']:return bad('archived',423)
    b=await body(req);text=b.get('body')
    if not isinstance(text,str) or not text.strip():return bad('invalid')
    parent=b.get('parent_id')
    if parent and not db.execute('select 1 from messages where id=? and channel_id=? and deleted=0',(parent,c['id'])).fetchone():return bad('not found',404)
    tx();r=db.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(c['id'],u['id'],text,parent,now()));mid=r.lastrowid;db.commit()
    m=msgobj(db.execute('select * from messages where id=?',(mid,)).fetchone());event(c['id'],'message.reply' if parent else 'message.created',{'message':m});return j({'message':m},201)
async def messageop(req):
    u=await auth(req);r=db.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
    if not u:return bad('auth',401)
    if not r:return bad('not found',404)
    c=db.execute('select * from channels where id=?',(r['channel_id'],)).fetchone();role=db.execute('select role from members where workspace_id=? and user_id=?',(c['workspace_id'],u['id'])).fetchone()
    if req.method=='GET':
        if not db.execute('select 1 from cmembers where channel_id=? and user_id=?',(c['id'],u['id'])).fetchone():return bad('forbidden',403)
        rows=list(db.execute('select * from messages where parent_id=? and deleted=0 order by id',(r['id'],)));out,n,e=pag(req,rows)
        if e:return bad(e)
        return j({'replies':[msgobj(x) for x in out],'next_cursor':n})
    if req.method=='DELETE':
        if u['id']!=r['author_id'] and (not role or role['role']!='owner'):return bad('forbidden',403)
        db.execute('update messages set deleted=1 where id=?',(r['id'],));db.commit();event(c['id'],'message.deleted',{'message_id':r['id']});return j({'deleted':True})
    if r['deleted']:return bad('deleted',409)
    if u['id']!=r['author_id']:return bad('forbidden',403)
    b=await body(req)
    if not b.get('body','').strip():return bad('invalid')
    db.execute('update messages set body=?,edited_at=? where id=?',(b['body'],now(),r['id']));db.commit();m2=msgobj(db.execute('select * from messages where id=?',(r['id'],)).fetchone());event(c['id'],'message.updated',{'message':m2});return j({'message':m2})
async def reactions(req):
    u=await auth(req);r=db.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
    if not u:return bad('auth',401)
    if not r:return bad('not found',404)
    if req.method=='POST':
        b=await body(req);db.execute('insert or ignore into reactions values(?,?,?)',(r['id'],u['id'],b.get('emoji','')));db.commit();event(db.execute('select channel_id from messages where id=?',(r['id'],)).fetchone()['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':b.get('emoji','')})
    else:db.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(r['id'],u['id'],req.match_info['emoji']));db.commit();event(db.execute('select channel_id from messages where id=?',(r['id'],)).fetchone()['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':req.match_info['emoji']})
    return j({'reactions':msgobj(db.execute('select * from messages where id=?',(r['id'],)).fetchone())['reactions']})
async def search(req):
    u=await auth(req)
    if not u:return bad('auth',401)
    q=req.query.get('q','');wid=req.query.get('workspace_id'); sql="select m.* from messages m join cmembers cm on cm.channel_id=m.channel_id and cm.user_id=? join channels c on c.id=m.channel_id where m.deleted=0 and m.body like ?"; args=[u['id'],'%'+q+'%'];
    if wid: sql+=' and c.workspace_id=?'; args.append(wid)
    sql+=' order by m.id desc'; rows=list(db.execute(sql,args));out,n,e=pag(req,rows)
    if e:return bad(e)
    return j({'results':[msgobj(x) for x in out],'next_cursor':n})
async def pin(req):
    u=await auth(req);r=db.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
    if not u:return bad('auth',401)
    if not r:return bad('not found',404)
    if req.method=='POST':
        db.execute('insert or ignore into pins values(?,?,?)',(r['id'],u['id'],now()));db.commit();p=db.execute('select * from pins where message_id=?',(r['id'],)).fetchone();return j({'pin':{'message_id':r['id'],'pinned_by':p['pinned_by'],'pinned_at':p['pinned_at']}})
    db.execute('delete from pins where message_id=?',(r['id'],));db.commit();return j({'unpinned':True})
async def pinlist(req):
    u=await auth(req);cid=int(req.match_info['id'])
    if not u:return bad('auth',401)
    c=db.execute('select * from channels where id=?',(cid,)).fetchone()
    if not c:return bad('not found',404)
    member=db.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone()
    owner=db.execute('select 1 from members where workspace_id=? and user_id=? and role=\'owner\'',(c['workspace_id'],u['id'])).fetchone()
    if not member and not owner:return bad('forbidden',403)
    rows=list(db.execute('select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=? order by p.message_id desc',(cid,)));out,n,e=pag(req,rows)
    if e:return bad(e)
    return j({'pins':[{'message':msgobj(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in out],'next_cursor':n})
def unread_count(uid,cid,last):
    return db.execute("select count(*) from messages m join events e on e.channel_id=m.channel_id and e.event_id> ? and json_extract(e.payload,'$.message.id')=m.id where m.channel_id=? and m.author_id<>? and m.deleted=0",(last,cid,uid)).fetchone()[0]
def mention_count(uid,cid,last):
    n=0
    for r in db.execute('select m.*,e.event_id from messages m join events e on e.channel_id=m.channel_id and e.payload like ? where m.channel_id=? and e.event_id>? and m.id=cast(json_extract(e.payload,\'$.message.id\') as integer) and m.deleted=0 and m.author_id<>?',('%"id":%',cid,last,uid)):
        if uid in mentions(r['body'],cid,r['author_id']): n+=1
    return n
async def readstate(req):
    u=await auth(req);cid=int(req.match_info['id'])
    if not u:return bad('auth',401)
    if not db.execute('select 1 from channels where id=?',(cid,)).fetchone():return bad('not found',404)
    head=db.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(cid,)).fetchone()['n'];x=db.execute('select last_read from reads where user_id=? and channel_id=?',(u['id'],cid)).fetchone();last=x['last_read'] if x else 0
    if req.method=='POST':
        v=(await body(req)).get('last_read_event_id')
        if not isinstance(v,int) or isinstance(v,bool) or v<0:return bad('invalid')
        last=min(head,max(last,v));db.execute('insert or replace into reads values(?,?,?)',(u['id'],cid,last));db.commit()
    return j({'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread_count(u['id'],cid,last),'mention_count':mention_count(u['id'],cid,last)}})
async def ws(req):
    q=req.query.get('token')
    u=db.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(q,)).fetchone() if q else token_user(req)
    if not u:return web.Response(status=401)
    w=web.WebSocketResponse();await w.prepare(req);sockets[w]={}
    async for x in w:
        if x.type==WSMsgType.TEXT:
            b=json.loads(x.data);cid=int(b.get('channel_id',0))
            if not db.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone():await w.send_json({'type':'error'});continue
            head=db.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(cid,)).fetchone()['n'];sockets[w][cid]=head
            if b['type']=='resume':
                for e in db.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,b.get('since_event_id',0))):
                    f=json.loads(e['payload']);f.update(type=e['kind'],event_id=e['event_id'],channel_id=cid);await w.send_json(f)
                await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            else: await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    sockets.pop(w,None);return w

app=web.Application()
for method,path,fn in [
('GET','/api/health',health),('POST','/api/auth/register',register),('POST','/api/auth/login',login),('GET','/api/auth/me',me),('POST','/api/workspaces',createws),('PATCH','/api/workspaces/{slug}/members/{user_id}',setrole),('GET','/api/workspaces/{slug}',wsdetail),('POST','/api/workspaces/{slug}/channels',createch),('POST','/api/channels/{id}/join',join),('POST','/api/channels/{id}/leave',channelop),('PATCH','/api/channels/{id}',channelop),('POST','/api/channels/{id}/archive',channelop),('POST','/api/channels/{id}/unarchive',channelop),('GET','/api/channels/{id}/members',members),('GET','/api/channels/{id}/messages',messages),('POST','/api/channels/{id}/messages',messages),('GET','/api/messages/{id}/replies',messageop),('PATCH','/api/messages/{id}',messageop),('DELETE','/api/messages/{id}',messageop),('POST','/api/messages/{id}/reactions',reactions),('DELETE','/api/messages/{id}/reactions/{emoji}',reactions),('GET','/api/search',search),('POST','/api/messages/{id}/pin',pin),('DELETE','/api/messages/{id}/pin',pin),('GET','/api/channels/{id}/pins',pinlist),('GET','/api/channels/{id}/read',readstate),('POST','/api/channels/{id}/read',readstate),('GET','/api/ws',ws)]:
    app.router.add_route(method,path,fn)
async def startup(app):app['poll']=asyncio.create_task(broadcast())
async def cleanup(app):app['poll'].cancel()
app.on_startup.append(startup);app.on_cleanup.append(cleanup)
web.run_app(app,host='127.0.0.1',port=PORT)

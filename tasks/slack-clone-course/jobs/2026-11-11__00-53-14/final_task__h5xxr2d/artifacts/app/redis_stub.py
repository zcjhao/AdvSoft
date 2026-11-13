import socket, threading
def client(c):
    try:
        while True:
            b=c.recv(4096)
            if not b: break
            if b.startswith(b'*') or b.upper().startswith(b'PING'):
                c.sendall(b'+PONG\r\n')
    finally:c.close()
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',6379));s.listen(50)
while True:
    c,_=s.accept();threading.Thread(target=client,args=(c,),daemon=True).start()

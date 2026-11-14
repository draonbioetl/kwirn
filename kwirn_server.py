#!/usr/bin/env python3
"""Kwirn LAN server.

Serves kwirn.html and relays moves between two players on the same network.
Standard library only.

    python kwirn_server.py              # port 8765, opens your browser
    python kwirn_server.py --port 9000 --no-browser

The server never plays or checks moves itself: both browsers run the same
deterministic engine and replay the same move list, so they stay in sync.
"""
import argparse, json, os, queue, secrets, socket, sys, threading, time, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, 'kwirn.html')
CODE_CHARS = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
LOCK = threading.Lock()
ROOMS = {}
PORT = 8765


def lan_ips():
    """Best guess at this PC's addresses on the local network, most likely first."""
    ips = []
    try:  # a UDP "connect" sends nothing; it just asks the OS which interface it would route through
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    ips = [ip for ip in ips if not ip.startswith('127.') and not ip.startswith('169.254.')]
    private = lambda ip: ip.startswith('192.168.') or ip.startswith('10.') or (ip.startswith('172.') and 16 <= int(ip.split('.')[1]) <= 31)
    first = ips[:1]
    rest = sorted(ips[1:], key=lambda ip: (not ip.startswith('192.168.'), not private(ip)))
    return first + rest


class Room:
    def __init__(self, code, rules, host_seat, host_name):
        self.code = code
        self.rules = rules
        self.moves = []
        self.result = None          # {'resigned': seat} once someone resigns
        self.game = 1
        self.tokens = [None, None]  # seat -> token
        self.names = ['', '']
        self.subs = {}              # token -> set of queues (one per open tab)
        self.conns = {}             # connection id -> queue, so a closing tab can end its own stream
        self.touched = time.time()
        tok = secrets.token_hex(12)
        self.tokens[host_seat] = tok
        self.names[host_seat] = host_name
        self.host_token = tok

    def seat_of(self, token):
        if not token or not isinstance(token, str):
            return -1
        return self.tokens.index(token) if token in self.tokens else -1

    def online(self):
        return [bool(t and self.subs.get(t)) for t in self.tokens]

    def snapshot(self, token):
        return {'type': 'state', 'code': self.code, 'rules': self.rules, 'moves': self.moves,
                'result': self.result, 'game': self.game, 'seat': self.seat_of(token),
                'names': self.names, 'online': self.online(), 'ready': all(self.tokens)}

    def push(self, token, msg):
        for q in list(self.subs.get(token, ())):
            q.put(msg)

    def broadcast(self, msg, skip=None):
        for t in self.tokens:
            if t and t != skip:
                self.push(t, msg)

    def broadcast_state(self):
        for t in self.tokens:
            if t:
                self.push(t, self.snapshot(t))

    def presence(self):
        self.broadcast({'type': 'presence', 'online': self.online(), 'names': self.names})


def clean_name(s, fallback):
    s = ' '.join(str(s or '').split())[:16]
    return s or fallback


def sweep():
    now = time.time()
    for code in [c for c, r in ROOMS.items() if not any(r.subs.values()) and now - r.touched > 6 * 3600]:
        del ROOMS[code]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Kwirn'

    def log_message(self, fmt, *args):  # keep the console quiet apart from our own lines
        pass

    # ---------- helpers ----------
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, msg):
        self.send_json({'error': msg}, status)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 200_000:
            raise ValueError('too large')
        return json.loads(self.rfile.read(n) or b'{}')

    def room_for(self, data, need_seat=True):
        room = ROOMS.get(str(data.get('code', '')).upper())
        if not room:
            return None, -1
        seat = room.seat_of(data.get('token'))
        if need_seat and seat < 0:
            return None, -1
        room.touched = time.time()
        return room, seat

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ('/', '/index.html', '/kwirn.html'):
            try:
                with open(PAGE, 'rb') as f:
                    body = f.read()
            except OSError:
                return self.fail(500, 'kwirn.html not found next to kwirn_server.py')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == '/api/hello':
            self.send_json({'ok': True, 'lan': lan_ips(), 'port': PORT})
        elif url.path == '/api/room':
            with LOCK:
                room = ROOMS.get(qs.get('code', '').upper())
                if not room:
                    return self.fail(404, 'No game with that code. It may have ended when the host closed the server.')
                seat = room.seat_of(qs.get('token'))
                host = room.seat_of(room.host_token)
                self.send_json({'code': room.code, 'host': room.names[host], 'hostSeat': host, 'rules': room.rules,
                                'full': all(room.tokens) and seat < 0, 'mine': seat >= 0})
        elif url.path == '/api/state':
            with LOCK:
                room, seat = self.room_for(qs)
                if not room:
                    return self.fail(404, 'Game not found')
                self.send_json(room.snapshot(qs.get('token')))
        elif url.path == '/api/events':
            self.events(qs)
        elif url.path == '/favicon.ico':
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.fail(404, 'Not found')

    def events(self, qs):
        with LOCK:
            room, seat = self.room_for(qs)
            if not room:
                return self.fail(404, 'Game not found')
            token = qs['token']
            q = queue.Queue()
            room.subs.setdefault(token, set()).add(q)
            cid = qs.get('cid')
            if cid:
                room.conns[cid] = q
            q.put(room.snapshot(token))
            room.presence()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                try:
                    msg = q.get(timeout=4)
                    if msg is None:  # the tab said goodbye
                        break
                    chunk = ('data: ' + json.dumps(msg) + '\n\n').encode()
                except queue.Empty:
                    chunk = b': ping\n\n'
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            with LOCK:
                room.subs.get(token, set()).discard(q)
                if cid and room.conns.get(cid) is q:
                    del room.conns[cid]
                room.presence()

    # ---------- POST ----------
    def do_POST(self):
        url = urlparse(self.path)
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError):
            return self.fail(400, 'Bad request')
        with LOCK:
            sweep()
            if url.path == '/api/create':
                seat = 1 if data.get('seat') == 1 else 0
                code = ''.join(secrets.choice(CODE_CHARS) for _ in range(4))
                while code in ROOMS:
                    code = ''.join(secrets.choice(CODE_CHARS) for _ in range(4))
                rules = data.get('rules') if isinstance(data.get('rules'), dict) else {}
                room = Room(code, rules, seat, clean_name(data.get('name'), 'Host'))
                ROOMS[code] = room
                print(f'  game {code} created by {room.names[seat]}', flush=True)
                return self.send_json({'code': code, 'token': room.host_token, 'seat': seat})

            if url.path == '/api/join':
                room = ROOMS.get(str(data.get('code', '')).upper())
                if not room:
                    return self.fail(404, 'No game with that code. It may have ended when the host closed the server.')
                seat = room.seat_of(data.get('token'))
                if seat >= 0:  # coming back after a refresh
                    if data.get('name'):
                        room.names[seat] = clean_name(data.get('name'), room.names[seat])
                    return self.send_json({'code': room.code, 'token': data['token'], 'seat': seat})
                if all(room.tokens):
                    return self.fail(409, 'This game already has two players.')
                seat = room.tokens.index(None)
                tok = secrets.token_hex(12)
                room.tokens[seat] = tok
                room.names[seat] = clean_name(data.get('name'), 'Guest')
                room.touched = time.time()
                print(f'  {room.names[seat]} joined game {room.code}', flush=True)
                room.broadcast_state()
                return self.send_json({'code': room.code, 'token': tok, 'seat': seat})

            room, seat = self.room_for(data)
            if not room:
                return self.fail(404, 'Game not found')

            if url.path == '/api/move':
                mv = data.get('mv')
                if not all(room.tokens):
                    return self.fail(409, 'Waiting for the second player')
                if room.result:
                    return self.fail(409, 'Game is over')
                if data.get('ply') != len(room.moves) or not isinstance(mv, dict):
                    return self.fail(409, 'Out of sync')
                room.moves.append({'x': mv.get('x'), 'y': mv.get('y'), 'pair': mv.get('pair'), 'p': seat})
                room.broadcast({'type': 'move', 'ply': len(room.moves) - 1, 'mv': room.moves[-1], 'seat': seat}, skip=data.get('token'))
                return self.send_json({'ok': True})

            if url.path == '/api/resign':
                if not room.result:
                    room.result = {'resigned': seat}
                    room.broadcast({'type': 'resign', 'seat': seat, 'game': room.game}, skip=data.get('token'))
                return self.send_json({'ok': True})

            if url.path == '/api/rematch':
                if data.get('game') != room.game:  # the other player already started it
                    return self.send_json({'ok': True})
                room.tokens.reverse()
                room.names.reverse()
                room.moves = []
                room.result = None
                room.game += 1
                room.broadcast_state()
                return self.send_json({'ok': True})

            if url.path == '/api/bye':
                q = room.conns.pop(str(data.get('cid')), None)
                if q:
                    q.put(None)
                return self.send_json({'ok': True})

            if url.path == '/api/leave':
                if not all(room.tokens):  # nobody joined yet: drop the invite
                    ROOMS.pop(room.code, None)
                return self.send_json({'ok': True})

        self.fail(404, 'Not found')


def main():
    global PORT
    ap = argparse.ArgumentParser(description='Kwirn LAN server')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()
    if not os.path.exists(PAGE):
        sys.exit('kwirn.html must sit next to kwirn_server.py')
    srv = None
    if os.environ.get('PORT'):  # hosted (Render etc.): the platform assigns the port and handles HTTPS
        srv = ThreadingHTTPServer(('0.0.0.0', int(os.environ['PORT'])), Handler)
        srv.daemon_threads = True
        PORT = srv.server_address[1]
        print(f'Kwirn server listening on port {PORT}', flush=True)
        srv.serve_forever()
        return
    for port in range(args.port, args.port + 20):
        try:
            srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
            break
        except OSError:
            continue
    if not srv:
        sys.exit(f'No free port between {args.port} and {args.port + 19}')
    srv.daemon_threads = True
    PORT = srv.server_address[1]
    ips = lan_ips()
    print('\n  Kwirn LAN server is running.\n')
    print(f'  On this PC:        http://localhost:{PORT}')
    for ip in ips[:1]:
        print(f'  On your network:   http://{ip}:{PORT}')
    for ip in ips[1:]:
        print(f'                     http://{ip}:{PORT}  (other adapter)')
    print('\n  Pick "Friend on your network" in the game and send the invite link.')
    print('  If Windows asks about the firewall, allow access on private networks.')
    print('  Press Ctrl+C to stop.\n', flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')


if __name__ == '__main__':
    main()

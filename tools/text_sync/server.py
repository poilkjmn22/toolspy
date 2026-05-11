#!/usr/bin/env python3
import argparse
import os
import socket
import threading
import struct
import json
import base64
import hashlib

PORT_DEFAULT = 8000
CONTENT = ""
CONTENT_HASH = ""


def get_local_ip():
    import subprocess
    try:
        output = subprocess.check_output(['ipconfig', 'getifaddr', 'en0'], stderr=subprocess.DEVNULL).decode().strip()
        if output:
            return output
    except Exception:
        pass
    try:
        output = subprocess.check_output(['ipconfig', 'getifaddr', 'en1'], stderr=subprocess.DEVNULL).decode().strip()
        if output:
            return output
    except Exception:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.1)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Text Sync</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; height: 100vh; display: flex; flex-direction: column; }
  #header { padding: 12px 16px; background: #f5f5f5; border-bottom: 1px solid #ddd; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  #header .info { flex: 1; font-size: 13px; color: #666; }
  #header .info span { margin-right: 16px; }
  #syncBtn { padding: 6px 16px; background: #2563eb; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
  #syncBtn:hover { background: #1d4ed8; }
  #syncBtn.syncing { opacity: 0.6; pointer-events: none; }
  #status { padding: 6px 16px; background: #fef9c3; border: 1px solid #fef08a; border-radius: 6px; font-size: 13px; color: #854d0e; display: none; }
  #textarea { flex: 1; width: 100%; resize: none; border: none; outline: none; padding: 16px; font-size: 16px; line-height: 1.6; }
  #footer { padding: 8px 16px; background: #f5f5f5; border-top: 1px solid #ddd; font-size: 12px; color: #999; flex-shrink: 0; }
</style>
</head>
<body>
<div id="header">
  <div class="info">
    <span id="connCount">0</span> 在线设备 &nbsp;|&nbsp; Hash: <span id="contentHash">--</span>
  </div>
  <button id="syncBtn" onclick="syncContent()">同步到多端</button>
  <div id="status">已同步</div>
</div>
<textarea id="textarea" placeholder="在此输入文本内容..."></textarea>
<div id="footer">最后更新: <span id="lastUpdate">--</span></div>
<script>
let ws;
let localContent = '';
let lastHash = '';

function connect() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(protocol + '//' + location.host + '/ws');

  ws.onopen = () => { startPing(); };
  ws.onmessage = (e) => {
    console.log('WS message:', e.data);
    const msg = JSON.parse(e.data);
    if (msg.type === 'content') {
      if (msg.hash !== lastHash) {
        textarea.value = msg.content;
        localContent = msg.content;
        lastHash = msg.hash;
        document.getElementById('contentHash').textContent = msg.hash;
        document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
        console.log('Content updated from sync');
      }
    } else if (msg.type === 'count') {
      document.getElementById('connCount').textContent = msg.count;
    } else if (msg.type === 'init') {
      textarea.value = msg.content || '';
      localContent = textarea.value;
      lastHash = msg.hash || '';
      document.getElementById('contentHash').textContent = lastHash || '--';
      document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
    }
  };
  ws.onclose = () => { console.log('WS closed, reconnecting...'); setTimeout(connect, 2000); };
  ws.onerror = (e) => { console.log('WS error:', e); };
}

function startPing() {
  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ping' }));
    }
  }, 5000);
}

function syncContent() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const btn = document.getElementById('syncBtn');
  btn.classList.add('syncing');
  const content = textarea.value;
  const hash = calcHash(content);
  lastHash = hash;
  document.getElementById('contentHash').textContent = hash;
  ws.send(JSON.stringify({ type: 'sync', content: content, hash: hash }));
  document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
  const status = document.getElementById('status');
  status.style.display = 'block';
  setTimeout(() => { status.style.display = 'none'; btn.classList.remove('syncing'); }, 1500);
}

function calcHash(content) {
  let h = 0;
  for (let i = 0; i < content.length; i++) {
    h = ((h << 5) - h) + content.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h || 1).toString(16);
}

textarea.addEventListener('input', () => { localContent = textarea.value; });
console.log('Connecting to WS... v2');
connect();
</script>
</body>
</html>
"""

ws_clients = []
ws_clients_lock = threading.Lock()
content_lock = threading.Lock()


class WebSocketStream:
    def __init__(self, rfile, wfile):
        self.rfile = rfile
        self.wfile = wfile
        self.closed = False

    def read(self):
        try:
            first_byte = self.rfile.read(1)
            if not first_byte:
                return None
            b = first_byte[0]
            opcode = b & 0x0F
            if opcode == 0x08:
                return None
            if opcode != 0x01:
                return self.read()
            b2 = self.rfile.read(1)[0]
            mask = (b2 >> 7) & 1
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack('>H', self.rfile.read(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', self.rfile.read(8))[0]
            if mask:
                masking = self.rfile.read(4)
                payload = bytearray(self.rfile.read(length))
                for i in range(len(payload)):
                    payload[i] ^= masking[i % 4]
                return payload.decode('utf-8', errors='replace')
            else:
                return self.rfile.read(length).decode('utf-8', errors='replace')
        except Exception:
            return None

    def write(self, data):
        try:
            payload = data.encode('utf-8')
            length = len(payload)
            frame = bytearray()
            frame.append(0x81)
            if length < 126:
                frame.append(length)
            elif length < 65536:
                frame.append(126)
                frame.extend(struct.pack('>H', length))
            else:
                frame.append(127)
                frame.extend(struct.pack('>Q', length))
            frame.extend(payload)
            self.wfile.write(bytes(frame))
            self.wfile.flush()
        except Exception:
            self.closed = True


class SyncRequestHandler:
    pass


import http.server
import socketserver


class SyncRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ws':
            self.handle_websocket()
        elif self.path == '/' or self.path == '/index.html' or self.path == '':
            self.serve_html()
        else:
            super().do_GET()

    def serve_html(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode('utf-8'))

    def do_POST(self):
        super().do_POST()

    def handle_websocket(self):
        try:
            headers = self.headers
            if headers.get('Upgrade', '').lower() != 'websocket':
                self.send_error(400)
                return
            key = headers.get('Sec-WebSocket-Key', '')
            if not key:
                self.send_error(400)
                return
            resp_key = base64.b64encode(hashlib.sha1(base64.b64decode(key) + '258EAFA5-E914-47DA-95CA-C5AC0F8C8C8E'.encode()).digest()).decode()
            response = (
                b'HTTP/1.1 101 Switching Protocols\r\n'
                b'Upgrade: websocket\r\n'
                b'Connection: Upgrade\r\n'
                b'Sec-WebSocket-Accept: ' + resp_key.encode() + b'\r\n'
                b'\r\n'
            )
            self.wfile.write(response)
            self.wfile.flush()
        except Exception as e:
            print(f"WebSocket handshake error: {e}")
            return

        stream = WebSocketStream(self.rfile, self.wfile)
        with ws_clients_lock:
            ws_clients.append(stream)
        try:
            with content_lock:
                stream.write(json.dumps({
                    'type': 'init',
                    'content': CONTENT,
                    'hash': CONTENT_HASH
                }))
        except Exception:
            pass

        try:
            while not stream.closed:
                data = stream.read()
                if not data:
                    break
                try:
                    msg = json.loads(data)
                    t = msg.get('type')
                    if t == 'sync':
                        new_content = msg.get('content', '')
                        new_hash = msg.get('hash', '')
                        with content_lock:
                            CONTENT = new_content
                            CONTENT_HASH = new_hash
                        with ws_clients_lock:
                            for c in ws_clients:
                                if c is not stream and not c.closed:
                                    try:
                                        c.write(json.dumps({'type': 'content', 'content': new_content, 'hash': new_hash}))
                                    except Exception:
                                        c.closed = True
                    elif t == 'ping':
                        with ws_clients_lock:
                            count = len([c for c in ws_clients if not c.closed])
                        try:
                            stream.write(json.dumps({'type': 'count', 'count': count}))
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            with ws_clients_lock:
                if stream in ws_clients:
                    ws_clients.remove(stream)
                count = len([c for c in ws_clients if not c.closed])
                for c in ws_clients:
                    try:
                        c.write(json.dumps({'type': 'count', 'count': count}))
                    except Exception:
                        pass

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run(port, directory):
    os.chdir(directory or os.getcwd())
    server = ThreadedHTTPServer(('', port), SyncRequestHandler)
    ip = get_local_ip()
    print(f"Serving at http://{ip}:{port}")
    print(f"Or http://localhost:{port}")
    print(f"Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description='HTTP server with real-time text sync')
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT, help=f'Port (default {PORT_DEFAULT})')
    parser.add_argument('-d', '--directory', help='Directory to serve (default current directory)')
    args = parser.parse_args()
    run(args.listen, args.directory)


if __name__ == '__main__':
    main()
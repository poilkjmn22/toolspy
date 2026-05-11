#!/usr/bin/env python3
import socket
import threading
import argparse
import os
import json
import base64
import hashlib
import struct

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

  ws.onopen = () => { startPing(); console.log('WS connected'); };
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
  if (!ws || ws.readyState !== WebSocket.OPEN) { console.log('WS not ready'); return; }
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
  console.log('Sync sent:', content.substring(0, 50));
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
console.log('Connecting to WS... v9');
connect();
</script>
</body>
</html>
"""

ws_clients = []
ws_clients_lock = threading.Lock()
content_lock = threading.Lock()
SERVER_PORT = [8000]


def send_ws_frame(conn, data):
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
    conn.sendall(bytes(frame))


def recv_ws_frame(conn):
    try:
        first = conn.recv(1)
        if not first:
            return None
        b = first[0]
        opcode = b & 0x0F
        if opcode == 0x08:
            return None
        if opcode != 0x01:
            return recv_ws_frame(conn)
        b2 = conn.recv(1)[0]
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack('>H', conn.recv(2))[0]
        elif length == 127:
            length = struct.unpack('>Q', conn.recv(8))[0]
        payload = conn.recv(length)
        return payload.decode('utf-8', errors='replace')
    except Exception:
        return None


def parse_http_request(data):
    lines = data.decode('utf-8', errors='replace').split('\r\n')
    if not lines:
        return None, None
    request_line = lines[0].split(' ')
    if len(request_line) < 2:
        return None, None
    method, path = request_line[0], request_line[1]
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            key, val = line.split(':', 1)
            headers[key.strip().lower()] = val.strip()
    return method, path, headers


def handle_client(conn, addr):
    global CONTENT, CONTENT_HASH

    try:
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(1)
            if not chunk:
                conn.close()
                return
            data += chunk

        method, path, headers = parse_http_request(data)
        if path == '/ws' and headers.get('upgrade', '') == 'websocket':
            key = headers.get('sec-websocket-key', '')
            if key:
                resp_key = base64.b64encode(
                    hashlib.sha1(base64.b64decode(key) + b'258EAFA5-E914-47DA-95CA-C5AC0F8C8C8E').digest()
                ).decode()
                response = (
                    b'HTTP/1.1 101 Switching Protocols\r\n'
                    b'Upgrade: websocket\r\n'
                    b'Connection: Upgrade\r\n'
                    b'Sec-WebSocket-Accept: ' + resp_key.encode() + b'\r\n'
                    b'\r\n'
                )
                conn.sendall(response)

                with ws_clients_lock:
                    ws_clients.append(conn)

                try:
                    send_ws_frame(conn, json.dumps({'type': 'init', 'content': CONTENT, 'hash': CONTENT_HASH}))
                except Exception:
                    pass

                while True:
                    frame_data = recv_ws_frame(conn)
                    if not frame_data:
                        break
                    try:
                        msg = json.loads(frame_data)
                        t = msg.get('type')
                        if t == 'sync':
                            new_content = msg.get('content', '')
                            new_hash = msg.get('hash', '')
                            CONTENT = new_content
                            CONTENT_HASH = new_hash
                            with ws_clients_lock:
                                for c in ws_clients:
                                    if c is not conn:
                                        try:
                                            send_ws_frame(c, json.dumps({'type': 'content', 'content': new_content, 'hash': new_hash}))
                                        except Exception:
                                            pass
                        elif t == 'ping':
                            with ws_clients_lock:
                                count = len([c for c in ws_clients if c])
                            try:
                                send_ws_frame(conn, json.dumps({'type': 'count', 'count': count}))
                            except Exception:
                                pass
                    except Exception:
                        pass
        elif path in ('/', '/index.html', ''):
            response = (
                b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: text/html; charset=utf-8\r\n'
                b'Cache-Control: no-cache\r\n'
                b'Access-Control-Allow-Origin: *\r\n'
                b'\r\n'
            )
            response += HTML_PAGE.encode('utf-8')
            conn.sendall(response)

            while True:
                chunk = conn.recv(1)
                if not chunk:
                    break
    except Exception as e:
        print(f"Client error: {e}")
    finally:
        with ws_clients_lock:
            if conn in ws_clients:
                ws_clients.remove(conn)
        conn.close()


def main():
    global SERVER_PORT
    parser = argparse.ArgumentParser(description='HTTP server with real-time text sync')
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT, help=f'Port (default {PORT_DEFAULT})')
    parser.add_argument('-d', '--directory', help='Directory to serve (default current directory)')
    args = parser.parse_args()
    SERVER_PORT[0] = args.listen

    os.chdir(args.directory or os.getcwd())

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('', args.listen))
    server.listen(50)

    ip = get_local_ip()
    print(f"Serving at http://{ip}:{args.listen}")
    print(f"Or http://localhost:{args.listen}")
    print(f"Press Ctrl+C to stop")

    def accept_loop():
        while True:
            try:
                conn, addr = server.accept()
                t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
                t.start()
            except Exception:
                break

    accept_thread = threading.Thread(target=accept_loop, daemon=True)
    accept_thread.start()

    try:
        accept_thread.join()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.close()


if __name__ == '__main__':
    main()

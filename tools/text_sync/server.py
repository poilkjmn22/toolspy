#!/usr/bin/env python3
import asyncio
import socket
import argparse
import os
import json

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
  #header .actions { display: flex; gap: 8px; }
  #syncBtn, #copyBtn { padding: 6px 16px; background: #2563eb; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
  #syncBtn:hover, #copyBtn:hover { background: #1d4ed8; }
  #syncBtn.syncing { opacity: 0.6; pointer-events: none; }
  #copyBtn.copied { background: #16a34a; }
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
  <div class="actions">
    <button id="copyBtn" onclick="copyContent()">复制</button>
    <button id="syncBtn" onclick="syncContent()">同步到多端</button>
  </div>
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
    const msg = JSON.parse(e.data);
    if (msg.type === 'content') {
      if (msg.hash !== lastHash) {
        textarea.value = msg.content;
        localContent = msg.content;
        lastHash = msg.hash;
        document.getElementById('contentHash').textContent = msg.hash;
        document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
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
  ws.onclose = () => { setTimeout(connect, 2000); };
  ws.onerror = () => { };
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

async function copyContent() {
  const btn = document.getElementById('copyBtn');
  const text = textarea.value;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    btn.textContent = '已复制';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = '复制'; btn.classList.remove('copied'); }, 1500);
  } catch (e) {
    console.error('Copy failed:', e);
  }
}

connect();
</script>
</body>
</html>
"""

ws_clients = set()
ws_clients_lock = asyncio.Lock()


async def ws_handler(websocket):
    global CONTENT, CONTENT_HASH
    async with ws_clients_lock:
        ws_clients.add(websocket)

    try:
        await websocket.send_str(json.dumps({'type': 'init', 'content': CONTENT, 'hash': CONTENT_HASH}))
    except Exception:
        pass

    try:
        async for message in websocket:
            try:
                msg_data = message.data
                if isinstance(msg_data, tuple):
                    msg_data = msg_data[1]
                msg = json.loads(msg_data)
                t = msg.get('type')
                if t == 'sync':
                    new_content = msg.get('content', '')
                    new_hash = msg.get('hash', '')
                    async with ws_clients_lock:
                        CONTENT = new_content
                        CONTENT_HASH = new_hash
                        for c in ws_clients:
                            if c is not websocket:
                                try:
                                    await c.send_str(json.dumps({'type': 'content', 'content': new_content, 'hash': new_hash}))
                                except Exception:
                                    pass
                elif t == 'ping':
                    async with ws_clients_lock:
                        count = len(ws_clients)
                    try:
                        await websocket.send_str(json.dumps({'type': 'count', 'count': count}))
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass
    finally:
        async with ws_clients_lock:
            ws_clients.discard(websocket)


async def http_handler(request):
    global HTML_PAGE
    from aiohttp import web
    return web.Response(text=HTML_PAGE, content_type='text/html', status=200, headers={'Cache-Control': 'no-cache'})


async def websocket_handler(request):
    from aiohttp import web
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws_handler(ws)
    return ws


async def main(port, directory):
    global CONTENT, CONTENT_HASH
    os.chdir(directory or os.getcwd())

    from aiohttp import web

    app = web.Application()
    app.router.add_get('/', http_handler)
    app.router.add_get('/ws', websocket_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '', port)
    await site.start()

    ip = get_local_ip()
    print(f"Serving at http://{ip}:{port}")
    print(f"Or http://localhost:{port}")
    print(f"Press Ctrl+C to stop")

    async def cleanup_dead_connections():
        while True:
            await asyncio.sleep(30)
            async with ws_clients_lock:
                dead = []
                for c in ws_clients:
                    if c.closed:
                        dead.append(c)
                for c in dead:
                    ws_clients.discard(c)
                if dead:
                    print(f"Cleaned up {len(dead)} dead connections")

    cleanup_task = asyncio.create_task(cleanup_dead_connections())

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        cleanup_task.cancel()
        await runner.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='HTTP server with real-time text sync')
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT, help=f'Port (default {PORT_DEFAULT})')
    parser.add_argument('-d', '--directory', help='Directory to serve (default current directory)')
    args = parser.parse_args()

    try:
        asyncio.run(main(args.listen, args.directory))
    except KeyboardInterrupt:
        print("\nShutting down...")

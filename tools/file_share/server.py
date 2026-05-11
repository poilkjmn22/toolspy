#!/usr/bin/env python3
import asyncio
import argparse
import base64
import hashlib
import io
import json
import os
import socket
import time
from datetime import datetime
from aiohttp import web
import aiohttp

PORT_DEFAULT = 8001
MAX_FILES = 20
MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024

files_storage = {}
files_lock = asyncio.Lock()


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


def format_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def get_file_icon(filename):
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    icon_map = {
        'pdf': '📄', 'doc': '📝', 'docx': '📝', 'xls': '📊', 'xlsx': '📊',
        'ppt': '📊', 'pptx': '📊', 'txt': '📃', 'md': '📃',
        'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️', 'gif': '🖼️', 'bmp': '🖼️', 'webp': '🖼️',
        'mp4': '🎬', 'avi': '🎬', 'mov': '🎬', 'mkv': '🎬',
        'mp3': '🎵', 'wav': '🎵', 'flac': '🎵', 'aac': '🎵',
        'zip': '📦', 'rar': '📦', '7z': '📦', 'tar': '📦', 'gz': '📦',
        'py': '🐍', 'js': '📜', 'ts': '📜', 'html': '🌐', 'css': '🎨',
        'json': '📋', 'xml': '📋', 'csv': '📊',
        'exe': '⚙️', 'dmg': '💿', 'iso': '💿',
    }
    return icon_map.get(ext, '📁')


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Share</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; height: 100vh; display: flex; flex-direction: column; }
  #header { padding: 16px 20px; background: #1a1a2e; color: white; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
  #header h1 { font-size: 18px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  #header h1::before { content: '📁'; font-size: 24px; }
  #clearBtn { padding: 8px 16px; background: #dc3545; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
  #clearBtn:hover { background: #c82333; }
  #uploadArea { margin: 20px; padding: 40px; border: 2px dashed #ccc; border-radius: 12px; text-align: center; cursor: pointer; transition: all 0.2s; flex-shrink: 0; }
  #uploadArea:hover, #uploadArea.dragover { border-color: #4dabf7; background: #f0f7ff; }
  #uploadArea input { display: none; }
  #uploadArea p { color: #666; font-size: 14px; margin-top: 8px; }
  #fileList { flex: 1; overflow-y: auto; padding: 0 20px 20px; }
  .file-item { display: flex; align-items: center; padding: 12px 16px; background: white; border-radius: 8px; margin-bottom: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); cursor: pointer; transition: all 0.15s; }
  .file-item:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.15); transform: translateY(-1px); }
  .file-icon { font-size: 32px; margin-right: 12px; flex-shrink: 0; }
  .file-info { flex: 1; min-width: 0; }
  .file-name { font-size: 15px; font-weight: 500; color: #333; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .file-meta { font-size: 12px; color: #999; margin-top: 4px; }
  .file-size { color: #666; margin-right: 12px; font-size: 14px; flex-shrink: 0; }
  .file-preview { width: 80px; height: 60px; object-fit: cover; border-radius: 4px; margin-right: 12px; flex-shrink: 0; display: none; }
  .file-preview.visible { display: block; }
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.8); color: white; padding: 12px 24px; border-radius: 8px; font-size: 14px; opacity: 0; transition: opacity 0.3s; pointer-events: none; }
  #toast.show { opacity: 1; }
  .empty-state { text-align: center; padding: 60px 20px; color: #999; }
  .empty-state .icon { font-size: 48px; margin-bottom: 16px; }
  .empty-state p { font-size: 14px; }
  #uploadProgress { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); justify-content: center; align-items: center; flex-direction: column; }
  #uploadProgress.visible { display: flex; }
  #progressBar { width: 300px; height: 8px; background: #444; border-radius: 4px; overflow: hidden; margin-top: 16px; }
  #progressFill { height: 100%; background: #4dabf7; width: 0%; transition: width 0.2s; }
  #progressText { color: white; font-size: 14px; }
</style>
</head>
<body>
<div id="header">
  <h1>File Share</h1>
  <button id="clearBtn" onclick="clearAll()">清空全部</button>
</div>

<div id="uploadArea" onclick="document.getElementById('fileInput').click()">
  <div style="font-size: 48px; margin-bottom: 12px;">☁️</div>
  <p style="font-size: 16px; color: #333; font-weight: 500;">拖拽文件到此处上传</p>
  <p>或点击选择文件 (最大 5GB)</p>
  <input type="file" id="fileInput" multiple onchange="handleFileSelect(event)">
</div>

<div id="fileList"></div>

<div id="toast"></div>

<div id="uploadProgress">
  <p id="progressText">上传中... 0%</p>
  <div id="progressBar"><div id="progressFill"></div></div>
</div>

<script>
let ws;
let files = [];

function connect() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(protocol + '//' + location.host + '/ws');

  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'init') {
      files = data.files || [];
      renderFiles();
    } else if (data.type === 'update') {
      files = data.files || [];
      renderFiles();
      if (data.action === 'upload') {
        showToast('新文件: ' + data.filename);
      } else if (data.action === 'clear') {
        showToast('已清空所有文件');
      }
    }
  };

  ws.onclose = () => { setTimeout(connect, 2000); };
}

function renderFiles() {
  const list = document.getElementById('fileList');
  if (files.length === 0) {
    list.innerHTML = '<div class="empty-state"><div class="icon">📂</div><p>暂无文件</p></div>';
    return;
  }
  list.innerHTML = files.map(f => `
    <div class="file-item" onclick="downloadFile('${f.id}')">
      ${f.preview ? `<img class="file-preview visible" src="${f.preview}" alt="${f.name}">` : `<span class="file-icon">${f.icon}</span>`}
      <div class="file-info">
        <div class="file-name">${escapeHtml(f.name)}</div>
        <div class="file-meta">${f.time}</div>
      </div>
      <span class="file-size">${f.size}</span>
    </div>
  `).join('');
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function showToast(msg) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 3000);
}

async function uploadFile(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    const progressPanel = document.getElementById('uploadProgress');

    progressPanel.classList.add('visible');
    progressFill.style.width = '0%';
    progressText.textContent = '上传中... 0%';

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        progressFill.style.width = pct + '%';
        progressText.textContent = '上传中... ' + pct + '%';
      }
    };

    xhr.onload = () => {
      progressPanel.classList.remove('visible');
      if (xhr.status === 200) {
        resolve(xhr.responseText);
      } else {
        reject(new Error(xhr.responseText || 'Upload failed'));
      }
    };

    xhr.onerror = () => {
      progressPanel.classList.remove('visible');
      reject(new Error('Network error'));
    };

    const formData = new FormData();
    formData.append('file', file);

    xhr.open('POST', '/upload');
    xhr.send(formData);
  });
}

async function handleFileSelect(e) {
  const files = e.target.files;
  for (const file of files) {
    try {
      await uploadFile(file);
    } catch (err) {
      showToast('上传失败: ' + err.message);
    }
  }
  e.target.value = '';
}

async function handleDrop(e) {
  e.preventDefault();
  document.getElementById('uploadArea').classList.remove('dragover');
  const files = e.dataTransfer.files;
  for (const file of files) {
    try {
      await uploadFile(file);
    } catch (err) {
      showToast('上传失败: ' + err.message);
    }
  }
}

document.getElementById('uploadArea').addEventListener('dragover', (e) => {
  e.preventDefault();
  e.currentTarget.classList.add('dragover');
});

document.getElementById('uploadArea').addEventListener('dragleave', (e) => {
  e.currentTarget.classList.remove('dragover');
});

document.getElementById('uploadArea').addEventListener('drop', handleDrop);

function downloadFile(id) {
  window.location.href = '/download/' + id;
}

async function clearAll() {
  if (!confirm('确定要清空所有文件吗？')) return;
  try {
    const resp = await fetch('/clear', { method: 'POST' });
    if (!resp.ok) throw new Error('Failed');
    showToast('已清空');
  } catch (err) {
    showToast('清空失败');
  }
}

connect();
</script>
</body>
</html>
"""

ws_clients = set()
ws_lock = asyncio.Lock()


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async with ws_lock:
        ws_clients.add(ws)
    try:
        files_list = await get_files_list()
        await ws.send_str(json.dumps({'type': 'init', 'files': files_list}))
        async for msg in ws:
            pass
    except Exception:
        pass
    finally:
        async with ws_lock:
            ws_clients.discard(ws)
    return ws


async def get_files_list():
    result = []
    for fid, finfo in files_storage.items():
        result.append({
            'id': fid,
            'name': finfo['name'],
            'size': finfo['size_str'],
            'time': finfo['time_str'],
            'icon': finfo['icon'],
            'preview': finfo.get('preview'),
        })
    result.sort(key=lambda x: x['time'], reverse=True)
    return result


async def broadcast_update(action=None, filename=None):
    files_list = await get_files_list()
    msg = {'type': 'update', 'files': files_list}
    if action:
        msg['action'] = action
        msg['filename'] = filename
    msg_str = json.dumps(msg)
    async with ws_lock:
        for ws in list(ws_clients):
            try:
                await ws.send_str(msg_str)
            except Exception:
                ws_clients.discard(ws)


async def http_handler(request):
    return web.Response(text=HTML_PAGE, content_type='text/html', headers={'Cache-Control': 'no-cache'})


async def upload_handler(request):
    global files_storage
    reader = await request.multipart()

    field = await reader.next()
    if not field or field.name != 'file':
        return web.Response(status=400, text='No file field')

    filename = field.filename
    content_length = request.content_length or 0

    if content_length > MAX_FILE_SIZE:
        return web.Response(status=413, text='File too large. Max size is 5GB.')

    file_data = await field.read()

    if len(file_data) > MAX_FILE_SIZE:
        return web.Response(status=413, text='File too large. Max size is 5GB.')

    file_id = hashlib.sha1((filename + str(time.time())).encode()).hexdigest()[:16]

    preview = None
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    if ext in ('jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'):
        if len(file_data) < 500 * 1024:
            preview = 'data:image/jpeg;base64,' + base64.b64encode(file_data).decode()

    size_str = format_size(len(file_data))
    time_str = datetime.now().strftime('%H:%M')

    finfo = {
        'name': filename,
        'data': file_data,
        'size': len(file_data),
        'size_str': size_str,
        'time_str': time_str,
        'icon': get_file_icon(filename),
        'preview': preview,
        'content_type': get_content_type(ext),
    }

    async with files_lock:
        files_storage[file_id] = finfo
        file_ids = list(files_storage.keys())
        if len(file_ids) > MAX_FILES:
            oldest = file_ids[0]
            del files_storage[oldest]

    await broadcast_update(action='upload', filename=filename)

    return web.Response(text='OK')


def get_content_type(ext):
    types = {
        'pdf': 'application/pdf',
        'txt': 'text/plain',
        'html': 'text/html',
        'css': 'text/css',
        'js': 'application/javascript',
        'json': 'application/json',
        'xml': 'application/xml',
        'zip': 'application/zip',
        'tar': 'application/x-tar',
        'gz': 'application/gzip',
        'rar': 'application/vnd.rar',
        '7z': 'application/x-7z-compressed',
        'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xls': 'application/vnd.ms-excel',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'ppt': 'application/vnd.ms-powerpoint',
        'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'mp4': 'video/mp4',
        'avi': 'video/x-msvideo',
        'mov': 'video/quicktime',
        'mkv': 'video/x-matroska',
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'flac': 'audio/flac',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'bmp': 'image/bmp',
        'webp': 'image/webp',
    }
    return types.get(ext, 'application/octet-stream')


async def download_handler(request):
    file_id = request.match_info['file_id']
    async with files_lock:
        finfo = files_storage.get(file_id)

    if not finfo:
        return web.Response(status=404, text='File not found')

    headers = {
        'Content-Disposition': f'attachment; filename*=UTF-8\'\'{finfo["name"]}',
        'Content-Type': finfo.get('content_type', 'application/octet-stream'),
    }
    return web.Response(body=finfo['data'], headers=headers)


async def clear_handler(request):
    global files_storage
    async with files_lock:
        files_storage.clear()
    await broadcast_update(action='clear')
    return web.Response(text='OK')


async def main_async(port, directory):
    app = web.Application()
    app.router.add_get('/', http_handler)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_post('/upload', upload_handler)
    app.router.add_get('/download/{file_id}', download_handler)
    app.router.add_post('/clear', clear_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '', port)
    await site.start()

    ip = get_local_ip()
    print(f"Serving at http://{ip}:{port}")
    print(f"Or http://localhost:{port}")
    print(f"Press Ctrl+C to stop")

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description='LAN file sharing server')
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT, help=f'Port (default {PORT_DEFAULT})')
    parser.add_argument('-d', '--directory', help='Working directory (optional)')
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args.listen, args.directory))
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == '__main__':
    main()

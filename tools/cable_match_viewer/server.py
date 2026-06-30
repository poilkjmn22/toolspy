#!/usr/bin/env python3
"""cable_match_viewer.server — aiohttp HTTP server for cable.db.

Routes:
    GET  /                     — main HTML+JS shell (flyfish viewer frontend)
    GET  /api/summary          — global stats
    GET  /api/documents        — all documents in cable.db
    GET  /api/document/{hash}  — one document with entities + matches
    GET  /api/cables           — all matched cables
    GET  /api/cable/{cable}    — documents for one cable
    GET  /file?hash=<hash>     — stream PDF file from disk
    GET  /healthz              — liveness
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import socket
from pathlib import Path

from aiohttp import web
import aiohttp

from .viewer import CableDbViewer

PORT_DEFAULT = 8003

# ---------------------------------------------------------------------------
# Local IP discovery
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    for iface in ('en0', 'en1'):
        try:
            import subprocess
            out = subprocess.check_output(
                ['ipconfig', 'getifaddr', iface], stderr=subprocess.DEVNULL
            ).decode().strip()
            if out:
                return out
        except Exception:
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.2)
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Inline HTML+JS shell (flyfish viewer CDN)
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cable Match Viewer</title>
<script src="/flyfish/flyfish-file-viewer-web-full.iife.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; color: #222; background: #fafafa; }
  body { display: flex; flex-direction: column; overflow: hidden; }
  #header { padding: 10px 16px; background: #1a1a2e; color: white; display: flex; align-items: center; gap: 12px; flex-shrink: 0; flex-wrap: wrap; }
  #header h1 { font-size: 16px; font-weight: 600; }
  #header .stats { font-size: 12px; opacity: 0.85; flex: 1; }
  #main { flex: 1; display: flex; min-height: 0; }
  #left, #mid, #right { height: 100%; overflow-y: auto; }
  #left { width: 280px; background: white; border-right: 1px solid #e0e0e0; flex-shrink: 0; }
  #mid { width: 380px; background: white; border-right: 1px solid #e0e0e0; flex-shrink: 0; display: flex; flex-direction: column; }
  #right { flex: 1; display: flex; flex-direction: column; min-width: 0; background: #f5f5f5; }
  .tree-section { padding: 8px 12px 4px; font-size: 11px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: 0.5px; border-top: 1px solid #f0f0f0; }
  .tree-section:first-child { border-top: none; }
  .cable-item, .doc-item { padding: 4px 12px; cursor: pointer; display: flex; align-items: center; gap: 6px; user-select: none; }
  .cable-item:hover, .doc-item:hover { background: #f0f7ff; }
  .cable-item.selected, .doc-item.selected { background: #d0e8ff; font-weight: 600; }
  .cable-item .badge { background: #4dabf7; color: white; border-radius: 9px; padding: 0 6px; font-size: 10px; min-width: 18px; text-align: center; }
  .doc-item .type-badge { font-size: 10px; padding: 1px 5px; border-radius: 3px; color: white; }
  .type-badge.dwg { background: #845ef7; }
  .type-badge.pdf { background: #e64980; }
  .doc-item .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #mid h2 { padding: 10px 16px; font-size: 13px; background: #f7f7f7; border-bottom: 1px solid #e0e0e0; flex-shrink: 0; }
  #mid .tab-bar { display: flex; border-bottom: 1px solid #e0e0e0; flex-shrink: 0; background: #f7f7f7; }
  #mid .tab-btn { padding: 8px 16px; cursor: pointer; font-size: 12px; border: none; background: transparent; border-bottom: 2px solid transparent; }
  #mid .tab-btn.active { border-bottom-color: #4dabf7; font-weight: 600; background: white; }
  #mid .tab-content { flex: 1; overflow-y: auto; }
  .entity-row { padding: 4px 12px; border-bottom: 1px solid #f0f0f0; font-size: 12px; display: flex; gap: 8px; align-items: flex-start; }
  .entity-row .type { font-weight: 600; color: #4dabf7; min-width: 50px; }
  .entity-row .text { font-family: 'SF Mono', Menlo, monospace; font-size: 11px; white-space: pre-wrap; word-break: break-all; color: #333; flex: 1; }
  .entity-row .meta { color: #888; font-size: 10px; min-width: 40px; text-align: right; }
  .match-row { padding: 4px 12px; border-bottom: 1px solid #e8f5e9; background: #f1f8e9; font-size: 12px; }
  .match-row .cable { color: #2e7d32; font-weight: 600; }
  .match-row .tier { font-size: 10px; color: #888; }
  .empty { padding: 40px 16px; text-align: center; color: #999; font-size: 13px; }
  .loading { padding: 16px; text-align: center; color: #999; font-size: 12px; }
  /* Right pane: flyfish viewer */
  #right { padding: 0; }
  #right .placeholder { flex: 1; display: flex; align-items: center; justify-content: center; color: #999; font-size: 14px; }
  flyfish-file-viewer { width: 100%; height: 100%; display: block; }
  /* Search */
  #search { padding: 5px 10px; border: 1px solid #444; border-radius: 4px; background: rgba(255,255,255,0.1); color: white; width: 180px; }
  #search::placeholder { color: rgba(255,255,255,0.5); }
  #search:focus { outline: none; background: rgba(255,255,255,0.2); }
</style>
</head>
<body>
<div id="header">
  <h1>Cable Match Viewer</h1>
  <div class="stats" id="header-stats">加载中…</div>
  <input id="search" type="text" placeholder="搜索 cable / document">
</div>
<div id="main">
  <div id="left">
    <div class="tree-section">📄 文档</div>
    <div id="doc-list"></div>
    <div class="tree-section">🔌 匹配 Cable</div>
    <div id="cable-list"></div>
  </div>
  <div id="mid">
    <div id="mid-placeholder" class="placeholder" style="flex:1;display:flex;align-items:center;justify-content:center">
      ← 从左侧选择文档或 cable
    </div>
  </div>
  <div id="right">
    <div class="placeholder" id="right-placeholder">← 选文档后在右侧预览</div>
  </div>
</div>

<script>
const state = {
  documents: [],
  cables: [],
  selectedDoc: null,
  selectedCable: null,
  docDetail: null,
  filter: '',
};

const $ = (id) => document.getElementById(id);

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return await r.json();
}

async function loadSummary() {
  const s = await api('/api/summary');
  $('header-stats').innerHTML = [
    `${s.documents} documents`,
    `${s.entities} entities`,
    `${s.matches} matches`,
    `${s.cables_with_matches}/${s.total_cables} cables`,
    `engine: ${s.engine_used}`,
  ].join(' &nbsp;·&nbsp; ');
}

async function loadDocuments() {
  state.documents = await api('/api/documents');
  renderLeftPane();
}

async function loadCables() {
  state.cables = await api('/api/cables');
  renderLeftPane();
}

function renderLeftPane() {
  const filter = state.filter.toLowerCase();
  const left = $('left');

  // Documents
  const docs = state.documents.filter(d => !filter || (d.pdf_rel_path || '').toLowerCase().includes(filter));
  const docHtml = docs.map(d => {
    const rel = d.pdf_rel_path || d.content_hash || '';
    const style = d.document_type === 'dwg' ? 'dwg' : 'pdf';
    return `<div class="doc-item${state.selectedDoc === d.content_hash ? ' selected' : ''}" data-hash="${d.content_hash}">
      <span class="type-badge ${style}">${style}</span>
      <span class="name" title="${rel}">${rel.slice(-50)}</span>
    </div>`;
  }).join('') || '<div class="empty">无文档</div>';

  // Cables
  const cables = state.cables.filter(c => !filter || c.cable.toLowerCase().includes(filter));
  const cableHtml = cables.map(c => {
    return `<div class="cable-item${state.selectedCable === c.cable ? ' selected' : ''}" data-cable="${c.cable}">
      <span class="badge">${c.match_count}</span>
      <span>${c.cable}</span>
    </div>`;
  }).join('') || '<div class="empty">无匹配电缆</div>';

  left.innerHTML = `
    <div class="tree-section">📄 文档 (${docs.length})</div>
    ${docHtml}
    <div class="tree-section">🔌 匹配 Cable (${cables.length})</div>
    ${cableHtml}
  `;

  left.querySelectorAll('.doc-item').forEach(el => {
    el.onclick = () => selectDocument(el.dataset.hash);
  });
  left.querySelectorAll('.cable-item').forEach(el => {
    el.onclick = () => selectCable(el.dataset.cable);
  });
}

async function selectDocument(hash) {
  state.selectedDoc = hash;
  state.selectedCable = null;
  state.docDetail = null;
  renderLeftPane();

  try {
    const detail = await api(`/api/document/${hash}`);
    state.docDetail = detail;
    renderMidPane('document');
    renderRightPane(detail);
  } catch (e) {
    $('mid').innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
  }
}

async function selectCable(cable) {
  state.selectedCable = cable;
  state.selectedDoc = null;
  state.docDetail = null;
  renderLeftPane();
  try {
    const data = await api(`/api/cable/${encodeURIComponent(cable)}`);
    renderMidPane('cable', data);
    $('right').innerHTML = '<div class="placeholder">← 选文档看预览</div>';
  } catch (e) {
    $('mid').innerHTML = `<div class="empty">无此 cable: ${cable}</div>`;
  }
}

function renderMidPane(mode, data) {
  const mid = $('mid');
  if (mode === 'document' && state.docDetail) {
    const d = state.docDetail;
    const rel = d.pdf_rel_path || 'unknown';
    const docType = d.document_type || '?';
    const entities = d.entities || [];
    const matches = d.matches || [];
    const ocrPages = d.ocr_pages || [];

    const textEntities = entities.filter(e => e.entity_type === 'text' && e.text && e.text.trim());
    const lineEntities = entities.filter(e => e.entity_type === 'line');

    let html = `<h2>${rel.slice(-60)} <span class="type-badge ${docType}">${docType}</span></h2>`;
    html += `<div class="tab-bar">
      <button class="tab-btn active" data-tab="entities">实体 (${entities.length})</button>
      <button class="tab-btn" data-tab="matches">匹配 (${matches.length})</button>
      <button class="tab-btn" data-tab="ocr">OCR (${ocrPages.length})</button>
    </div>`;
    html += `<div class="tab-content" id="tab-entities">
      ${textEntities.slice(0, 200).map(e =>
        `<div class="entity-row">
          <span class="type">text</span>
          <span class="text">${escHtml((e.text || '').slice(0, 120))}</span>
          <span class="meta">${e.confidence || ''}</span>
        </div>`
      ).join('')}
      ${textEntities.length > 200 ? `<div class="empty">…还有 ${textEntities.length - 200} 个</div>` : ''}
    </div>`;

    let matchHtml = matches.map(m =>
      `<div class="match-row"><span class="cable">${escHtml(m.cable)}</span> <span class="tier">(${m.tier})</span></div>`
    ).join('') || '<div class="empty">无匹配</div>';
    html += `<div class="tab-content" id="tab-matches" style="display:none">${matchHtml}</div>`;

    let ocrHtml = ocrPages.map(p =>
      `<div class="entity-row">
        <span class="type">page ${p.page}</span>
        <span class="text">${escHtml((p.text || '').slice(0, 500))}</span>
      </div>`
    ).join('') || '<div class="empty">无 OCR 文本</div>';
    html += `<div class="tab-content" id="tab-ocr" style="display:none">${ocrHtml}</div>`;

    mid.innerHTML = html;

    // Tab switching
    mid.querySelectorAll('.tab-btn').forEach(btn => {
      btn.onclick = () => {
        mid.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        mid.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
        btn.classList.add('active');
        const tab = $('tab-' + btn.dataset.tab);
        if (tab) tab.style.display = '';
      };
    });
  } else if (mode === 'cable' && data) {
    const docs = data.documents || [];
    let html = `<h2>${escHtml(data.cable)} <span class="count">(${docs.length} documents)</span></h2>`;
    docs.forEach(d => {
      const rel = d.pdf_rel_path || d.content_hash || '';
      const style = d.document_type === 'dwg' ? 'dwg' : 'pdf';
      html += `<div class="doc-item" data-hash="${d.content_hash}">
        <span class="type-badge ${style}">${style}</span>
        <span class="name">${rel.slice(-50)}</span>
      </div>`;
    });
    html += '</div>';
    mid.innerHTML = html;
    mid.querySelectorAll('.doc-item').forEach(el => {
      el.onclick = () => selectDocument(el.dataset.hash);
    });
  }
}

function renderRightPane(detail) {
  if (!detail) {
    $('right').innerHTML = '<div class="placeholder">无文档数据</div>';
    return;
  }
  const rel = detail.pdf_rel_path || '';
  const hash = detail.content_hash;
  const fileUrl = `/file?hash=${hash}`;
  const sourceFile = detail.source_file || rel;
  const ext = sourceFile.split('.').pop().toLowerCase();
  const docType = detail.document_type || '';

  $('right').innerHTML = `
    <div id="preview-container" style="width:100%;height:100%;display:flex;flex-direction:column;">
      <flyfish-file-viewer
        id="preview"
        src="${fileUrl}"
        filename="${sourceFile}"
        locale="zh-CN"
        theme="light"
        toolbar-position="bottom-right"
        style="width:100%;height:100%"
      ></flyfish-file-viewer>
    </div>
  `;

  // Error fallback: if flyfish fails, show entities
  const viewer = document.getElementById('preview');
  viewer.addEventListener('viewer-error', (e) => {
    console.warn('Flyfish preview error, showing entity fallback', e.detail);
    showEntityFallback(detail);
  });

  // Timeout fallback: if no ready event within 30s
  let ready = false;
  const timer = setTimeout(() => {
    if (!ready) {
      console.warn('Flyfish preview timeout, showing entity fallback');
      showEntityFallback(detail);
    }
  }, 30000);
  viewer.addEventListener('viewer-ready', () => { ready = true; clearTimeout(timer); });
}

function showEntityFallback(detail) {
  const entities = detail.entities || [];
  const textEntities = entities.filter(e => e.entity_type === 'text' && e.text && e.text.trim());
  const matches = detail.matches || [];
  const ocrPages = detail.ocr_pages || [];
  let html = `<div class="placeholder" style="padding:16px;text-align:left;font-size:13px;color:#555;">
    <p style="color:#e74c3c;margin-bottom:8px;">⚠ 文档预览不可用，显示提取的实体数据</p>`;
  if (matches.length) {
    html += `<p><b>匹配:</b> ${matches.map(m => m.cable).join(', ')}</p>`;
  }
  html += `<p><b>文本实体:</b> ${textEntities.length} 个</p>`;
  html += `<div style="max-height:300px;overflow-y:auto;margin-top:8px;font-family:monospace;font-size:11px;background:#f9f9f9;padding:8px;border-radius:4px;">`;
  textEntities.slice(0, 100).forEach(e => {
    html += `<div>${escHtml((e.text || '').slice(0, 120))}</div>`;
  });
  if (textEntities.length > 100) html += `<div>…还有 ${textEntities.length - 100} 个</div>`;
  html += `</div></div>`;
  $('right').innerHTML = html;
}

function escHtml(s) {
  if (!s) return '';
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Search
$('search').addEventListener('input', (e) => {
  state.filter = e.target.value.trim();
  renderLeftPane();
});

// Init
(async function init() {
  try {
    await loadSummary();
    await Promise.all([loadDocuments(), loadCables()]);
  } catch (e) {
    $('header-stats').textContent = '加载失败: ' + e.message;
  }
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------
async def index_handler(request: web.Request) -> web.Response:
    return web.Response(
        text=HTML_PAGE,
        content_type='text/html',
        charset='utf-8',
        headers={'Cache-Control': 'no-cache'},
    )

async def healthz_handler(request: web.Request) -> web.Response:
    return web.Response(text='OK', content_type='text/plain')

def _viewer(request: web.Request) -> CableDbViewer:
    return request.app['_viewer']

async def summary_handler(request: web.Request) -> web.Response:
    return web.json_response(_viewer(request).stats)

async def documents_handler(request: web.Request) -> web.Response:
    return web.json_response(_viewer(request).get_documents())

async def document_handler(request: web.Request) -> web.Response:
    h = request.match_info['hash']
    data = _viewer(request).get_document(h)
    if data is None:
        return web.json_response({'error': f'unknown hash: {h!r}'}, status=404)
    return web.json_response(data)

async def cables_handler(request: web.Request) -> web.Response:
    return web.json_response(_viewer(request).get_cables())

async def cable_handler(request: web.Request) -> web.Response:
    cable = request.match_info['cable']
    data = _viewer(request).get_cable(cable)
    if data is None:
        return web.json_response({'error': f'unknown cable: {cable!r}'}, status=404)
    return web.json_response(data)

async def file_handler(request: web.Request) -> web.Response:
    h = request.query.get('hash', '')
    if not h:
        return web.json_response({'error': 'missing ?hash='}, status=400)
    viewer = _viewer(request)
    abs_path = viewer.resolve_document_path(h)
    if abs_path is None:
        return web.json_response({'error': f'file not found: {h!r}'}, status=404)
    mime, _ = mimetypes.guess_type(str(abs_path))
    mime = mime or 'application/octet-stream'
    return web.FileResponse(abs_path, headers={
        'Content-Type': mime,
        'Content-Disposition': f'inline; filename="{abs_path.name}"',
        'Cache-Control': 'no-cache',
    })


_FLYFISH_CDN = 'https://cdn.jsdelivr.net/npm/@file-viewer/web-full@latest/dist'

async def flyfish_handler(request: web.Request) -> web.Response:
    path = request.match_info['path']
    cdn_url = f'{_FLYFISH_CDN}/{path}'
    async with aiohttp.ClientSession() as session:
        async with session.get(cdn_url) as resp:
            if resp.status != 200:
                return web.json_response({'error': 'flyfish asset not found'}, status=404)
            body = await resp.read()
            ct = resp.headers.get('Content-Type', 'application/octet-stream').split(';')[0].strip()
            if path.endswith('.wasm') and 'wasm' not in ct:
                ct = 'application/wasm'
            return web.Response(
                body=body,
                content_type=ct,
                headers={
                    'Cache-Control': 'public, max-age=86400',
                    'Access-Control-Allow-Origin': '*',
                    'Cross-Origin-Opener-Policy': 'same-origin',
                    'Cross-Origin-Embedder-Policy': 'require-corp',
                },
            )

# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------
async def main_async(port: int, host: str, db_path: str,
                     input_root: str | None = None) -> None:
    print(f'Loading viewer...', flush=True)
    viewer = CableDbViewer(db_path=db_path, input_root=input_root)
    stats = viewer.stats
    print(f'  DB:    {stats["db_path"]}', flush=True)
    print(f'  input: {stats["input_root"]}', flush=True)
    print(f'  docs:  {stats["documents"]}, entities: {stats["entities"]}', flush=True)
    print(f'  cables: {stats["total_cables"]} ({stats["cables_with_matches"]} matched)', flush=True)

    app = web.Application()
    app['_viewer'] = viewer

    app.router.add_get('/', index_handler)
    app.router.add_get('/healthz', healthz_handler)
    app.router.add_get('/api/summary', summary_handler)
    app.router.add_get('/api/documents', documents_handler)
    app.router.add_get('/api/document/{hash}', document_handler)
    app.router.add_get('/api/cables', cables_handler)
    app.router.add_get('/api/cable/{cable}', cable_handler)
    app.router.add_get('/file', file_handler)
    app.router.add_get('/flyfish/{path:.*}', flyfish_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    ip = get_local_ip()
    print(flush=True)
    print(f'Serving at http://{ip}:{port}', flush=True)
    print(f'Or http://localhost:{port}', flush=True)
    print(f'Press Ctrl+C to stop', flush=True)

    try:
        while True:
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await runner.cleanup()
        viewer.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Cable Match Viewer — browse cable_engine cable.db in a web UI',
    )
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT,
                        help=f'Port (default {PORT_DEFAULT})')
    parser.add_argument('-b', '--bind', default='0.0.0.0',
                        help='Bind address (default 0.0.0.0 for LAN access)')
    parser.add_argument('--db', required=True,
                        help='Path to cable.db')
    parser.add_argument('--input-root',
                        help='Source file root dir on disk')
    args = parser.parse_args()

    try:
        asyncio.run(main_async(
            args.listen, args.bind, args.db, args.input_root,
        ))
    except KeyboardInterrupt:
        print('\nShutting down...')


if __name__ == '__main__':
    main()

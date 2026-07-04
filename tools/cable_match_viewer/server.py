"""cable_viewer.server — V5 minimal viewer.

Layout:
  - Left column: cable list (scrollable, click to select)
  - Right column: cable detail (terminals, loops, source docs)
  - Bottom drawer: file preview iframe (PDF via built-in viewer,
    DWG served as application/octet-stream with a "download" hint)

The viewer is intentionally minimal — no graph visualization, no
complex topology renderer. Per the V5 spec, it answers two
questions:
  1. "What cables are in the project?"  (left column)
  2. "Where does cable X connect?"       (right column, on-demand)
  3. "Show me the source document."      (bottom drawer)
"""

from __future__ import annotations

import argparse
import mimetypes
import sqlite3
from functools import partial
from pathlib import Path

from aiohttp import web

from cable_engine.storage import CableStore, open_db, ensure_schema

from .store import CableViewer


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Cable Viewer</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system, "SF Pro", "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 13px; color: #222; background: #fafafa; }
  #app { display: flex; height: 100vh; }
  #left { width: 280px; border-right: 1px solid #e0e0e0; background: #fff; display: flex; flex-direction: column; }
  #right { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #header { padding: 10px 14px; border-bottom: 1px solid #e0e0e0; font-weight: 600; font-size: 14px; }
  #stats { padding: 6px 14px; border-bottom: 1px solid #e0e0e0; color: #888; font-size: 11px; }
  #search { padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }
  #search input { width: 100%; padding: 5px 8px; border: 1px solid #ccc; border-radius: 3px; font-size: 12px; }
  #cable-list { flex: 1; overflow-y: auto; }
  .cable-row { padding: 5px 14px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; font-family: "SF Mono", Menlo, monospace; font-size: 12px; }
  .cable-row:hover { background: #f0f7ff; }
  .cable-row.selected { background: #d0e8ff; font-weight: 600; }
  .cable-row .cnt { color: #888; font-size: 11px; }
  .empty { padding: 14px; text-align: center; color: #999; font-size: 12px; }
  #detail { flex: 1; overflow-y: auto; padding: 14px 18px; }
  #detail .cable-id { font-family: "SF Mono", Menlo, monospace; font-size: 18px; font-weight: 600; margin-bottom: 10px; }
  #detail .section { margin: 18px 0; }
  #detail .section h3 { margin: 0 0 6px 0; font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #eee; padding-bottom: 4px; }
  #detail .terminal, #detail .loop, #detail .doc { padding: 4px 0; font-family: "SF Mono", Menlo, monospace; font-size: 12px; }
  #detail .terminal .conf, #detail .loop .conf, #detail .doc .conf { color: #888; font-size: 10px; margin-left: 8px; }
  #detail .doc { cursor: pointer; color: #06c; }
  #detail .doc:hover { text-decoration: underline; }
  #detail .empty-state { color: #999; font-style: italic; padding: 4px 0; }
  #preview { height: 360px; border-top: 1px solid #e0e0e0; background: #fff; display: flex; flex-direction: column; }
  #preview-header { padding: 6px 14px; border-bottom: 1px solid #e0e0e0; font-size: 11px; color: #666; display: flex; justify-content: space-between; align-items: center; }
  #preview-header .close { cursor: pointer; color: #888; }
  #preview-header .close:hover { color: #c00; }
  #preview-frame { flex: 1; width: 100%; border: none; }
  #preview-hint { padding: 14px; color: #888; font-size: 12px; }
</style>
</head>
<body>
<div id="app">
  <div id="left">
    <div id="header">电缆列表</div>
    <div id="stats">加载中…</div>
    <div id="search"><input id="search-input" placeholder="过滤电缆…"></div>
    <div id="cable-list"><div class="empty">加载中…</div></div>
  </div>
  <div id="right">
    <div id="detail">
      <div class="empty-state">← 选择一条电缆查看详情</div>
    </div>
    <div id="preview" style="display:none">
      <div id="preview-header">
        <span id="preview-title"></span>
        <span id="preview-close" class="close">关闭 ✕</span>
      </div>
      <iframe id="preview-frame"></iframe>
    </div>
  </div>
</div>
<script>
let allCables = [];
let selectedCable = null;

const $ = (id) => document.getElementById(id);
const cableList = $('cable-list');
const detail = $('detail');
const search = $('search-input');
const preview = $('preview');
const previewTitle = $('preview-title');
const previewFrame = $('preview-frame');
const previewClose = $('preview-close');

async function loadCables() {
  const r = await fetch('/api/cables');
  allCables = await r.json();
  renderCables();
  $('stats').textContent = `共 ${allCables.length} 条电缆`;
}

function renderCables() {
  const q = search.value.trim().toLowerCase();
  const filtered = q ? allCables.filter(c => c.cable_id.toLowerCase().includes(q)) : allCables;
  if (!filtered.length) {
    cableList.innerHTML = '<div class="empty">没有匹配的电缆</div>';
    return;
  }
  cableList.innerHTML = filtered.map(c =>
    `<div class="cable-row ${selectedCable === c.cable_id ? 'selected' : ''}" data-cable="${c.cable_id}">
       <span>${c.cable_id}</span>
       <span class="cnt">×${c.occurrence_count}</span>
     </div>`
  ).join('');
  cableList.querySelectorAll('.cable-row').forEach(el => {
    el.onclick = () => selectCable(el.dataset.cable);
  });
}

async function selectCable(cableId) {
  selectedCable = cableId;
  renderCables();
  const r = await fetch('/api/cable/' + encodeURIComponent(cableId));
  if (!r.ok) {
    detail.innerHTML = '<div class="empty-state">未找到此电缆</div>';
    return;
  }
  const data = await r.json();
  renderDetail(data);
}

function renderDetail(d) {
  let html = `<div class="cable-id">${d.cable_id}</div>`;

  // Source type badge
  const sourceType = d.conductors.length ? d.conductors[0].source_type : '';
  const badge = sourceType === 'terminal_strip' ? '端子排图' : sourceType === 'circuit_loop' ? '回路图' : '未知';
  html += `<span style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;background:${sourceType === 'terminal_strip' ? '#e3f2fd' : '#fff3e0'};color:${sourceType === 'terminal_strip' ? '#1565c0' : '#e65100'};margin-bottom:10px;">${badge}</span>`;

  // Cabinet info
  const cabinetLocal = d.conductors.length ? (d.conductors[0].cabinet_name || '--') : '--';
  const cabinetRemote = d.conductors.length ? (d.conductors[0].cabinet_name_remote || '--') : '--';
  html += `<div style="font-size:12px;color:#666;margin-bottom:10px;">本端柜体: ${cabinetLocal} &nbsp;|&nbsp; 对端柜体: ${cabinetRemote}</div>`;

  html += `<div class="section"><h3>线芯 (${d.conductor_count})</h3>`;
  if (!d.conductors.length) html += '<div class="empty-state">无关联线芯</div>';
  else {
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="background:#f5f5f5;font-weight:600;"><th style="padding:4px 6px;text-align:left;border-bottom:1px solid #ddd;">线芯</th><th style="padding:4px 6px;text-align:left;border-bottom:1px solid #ddd;">端子</th><th style="padding:4px 6px;text-align:left;border-bottom:1px solid #ddd;">对端端子</th><th style="padding:4px 6px;text-align:left;border-bottom:1px solid #ddd;">回路描述</th><th style="padding:4px 6px;text-align:left;border-bottom:1px solid #ddd;">回路编号</th></tr>';
    d.conductors.forEach((c, i) => {
      const no = c.conductor_no || (i + 1);
      const strip = c.strip_name || '--';
      const tn = c.terminal_no != null ? c.terminal_no : '--';
      const remote = c.terminal_no_remote || '--';
      const desc = c.circuit_desc || '--';
      const loop = c.loop_id || '--';
      html += `<tr style="border-bottom:1px solid #eee;"><td style="padding:4px 6px;font-family:monospace;">${no}</td><td style="padding:4px 6px;font-family:monospace;">${strip}:${tn}</td><td style="padding:4px 6px;font-family:monospace;">${remote}</td><td style="padding:4px 6px;">${desc}</td><td style="padding:4px 6px;font-family:monospace;">${loop}</td></tr>`;
    });
    html += '</table>';
  }
  html += `</div>`;
  html += `<div class="section"><h3>来源图纸 (${d.documents.length})</h3>`;
  html += d.documents.map(doc =>
    `<div class="doc" data-hash="${doc.document.content_hash}">${doc.document.rel_path || doc.document.content_hash}<span class="conf">${doc.document.document_type}</span></div>`
  ).join('');
  html += `</div>`;
  detail.innerHTML = html;
  detail.querySelectorAll('.doc').forEach(el => {
    el.onclick = () => previewDoc(el.dataset.hash);
  });
}

async function previewDoc(hash) {
  const r = await fetch('/api/document/' + encodeURIComponent(hash));
  if (!r.ok) return;
  const d = await r.json();
  previewTitle.textContent = d.rel_path || hash;
  previewFrame.src = '/api/document/' + encodeURIComponent(hash) + '/file';
  preview.style.display = 'flex';
}

previewClose.onclick = () => {
  preview.style.display = 'none';
  previewFrame.src = '';
};

search.oninput = renderCables;

loadCables().catch(e => {
  $('stats').textContent = '加载失败: ' + e;
  cableList.innerHTML = '<div class="empty">加载失败</div>';
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def index_handler(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type='text/html')


async def cables_handler(request: web.Request) -> web.Response:
    return web.json_response(_viewer(request).list_cables())


async def cable_handler(request: web.Request) -> web.Response:
    cable_id = request.match_info['cable']
    data = _viewer(request).get_cable(cable_id)
    if data is None:
        return web.json_response(
            {'error': f'unknown cable: {cable_id!r}'},
            status=404,
        )
    return web.json_response(data)


async def document_handler(request: web.Request) -> web.Response:
    h = request.match_info['hash']
    data = _viewer(request).get_document(h)
    if data is None:
        return web.json_response({'error': f'unknown document: {h!r}'}, status=404)
    return web.json_response(data)


async def document_file_handler(request: web.Request) -> web.Response:
    h = request.match_info['hash']
    abs_path = _viewer(request).resolve_document_path(h)
    if abs_path is None:
        return web.json_response(
            {'error': f'no path for document: {h!r}'},
            status=404,
        )
    if not abs_path.exists():
        return web.json_response(
            {'error': f'file not found on disk: {abs_path}'},
            status=404,
        )
    mime, _ = mimetypes.guess_type(str(abs_path))
    mime = mime or 'application/octet-stream'
    return web.FileResponse(abs_path, headers={
        'Content-Type': mime,
        'Content-Disposition': f'inline; filename="{abs_path.name}"',
        'Cache-Control': 'no-cache',
    })


async def stats_handler(request: web.Request) -> web.Response:
    return web.json_response(_viewer(request).stats())


async def healthz_handler(request: web.Request) -> web.Response:
    return web.Response(text='OK', content_type='text/plain')


def _viewer(request: web.Request) -> CableViewer:
    return request.app['_viewer']


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def make_app(db_path: Path) -> web.Application:
    conn = open_db(db_path)
    ensure_schema(conn)
    store = CableStore(conn)
    viewer = CableViewer(store)
    app = web.Application()
    app['_viewer'] = viewer
    app['_conn'] = conn
    app.router.add_get('/', index_handler)
    app.router.add_get('/api/cables', cables_handler)
    app.router.add_get('/api/cable/{cable}', cable_handler)
    app.router.add_get('/api/document/{hash}', document_handler)
    app.router.add_get('/api/document/{hash}/file', document_file_handler)
    app.router.add_get('/api/stats', stats_handler)
    app.router.add_get('/healthz', healthz_handler)
    return app


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description='Cable viewer (V5 minimal)')
    p.add_argument('--db', default='cable.db', help='Path to cable.db')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8003)
    args = p.parse_args()

    app = make_app(Path(args.db))
    web.run_app(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
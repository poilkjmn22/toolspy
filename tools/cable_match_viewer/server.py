#!/usr/bin/env python3
"""cable_match_viewer.server — aiohttp HTTP server with inline HTML/JS frontend.

Mirrors the layout of tools/file_share/server.py and tools/text_sync/server.py:
single-file server, inline HTML_PAGE constant, all routes registered in
main_async(), main() is the argparse CLI entry.

Routes:
    GET  /                              — main HTML+JS shell
    GET  /api/summary                    — global stats (cables, pdfs, ocr_rows)
    GET  /api/cables                     — list of all cables (natural-sorted)
    GET  /api/cable/{cable}              — PDFs matched by cable
    GET  /api/pdf?path=<rel>             — one PDF detail + OCR text
    GET  /api/ocr/{hash_prefix}          — all OCR rows for a content_hash
    GET  /file?path=<rel>                — stream PDF bytes from disk (whitelist)
    GET  /healthz                        — liveness probe (200 OK)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import socket

from aiohttp import web

from .viewer import CableMatchViewer

PORT_DEFAULT = 8003  # text-sync=8000, file-share=8001, llm-chat=8002 already taken

# ---------------------------------------------------------------------------
# Local IP discovery (same pattern as file_share / text_sync)
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    # macOS
    for iface in ('en0', 'en1'):
        try:
            import subprocess
            out = subprocess.check_output(['ipconfig', 'getifaddr', iface],
                                         stderr=subprocess.DEVNULL).decode().strip()
            if out:
                return out
        except Exception:
            pass
    # Cross-platform fallback
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
# Inline HTML+JS shell
# ---------------------------------------------------------------------------
# Layout: 3-pane flexbox
#   left  — cable tree (matched first, then unmatched, natural-sort)
#   mid   — PDF list under selected cable, OR detail panel when PDF selected
#   right — PDF preview (PDF.js) + OCR text below
# Search box filters the left pane by cable name OR pdf path.
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cable Match Viewer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; color: #222; background: #fafafa; }
  body { display: flex; flex-direction: column; overflow: hidden; }
  /* Header */
  #header { padding: 10px 16px; background: #1a1a2e; color: white; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  #header h1 { font-size: 16px; font-weight: 600; }
  #header .stats { font-size: 12px; opacity: 0.85; flex: 1; }
  #header input { padding: 5px 10px; border: 1px solid #444; border-radius: 4px; background: rgba(255,255,255,0.1); color: white; width: 240px; }
  #header input::placeholder { color: rgba(255,255,255,0.5); }
  #header input:focus { outline: none; background: rgba(255,255,255,0.2); }
  /* 3-pane layout */
  #main { flex: 1; display: flex; min-height: 0; }
  #left, #mid, #right { height: 100%; overflow-y: auto; }
  #left { width: 280px; background: white; border-right: 1px solid #e0e0e0; flex-shrink: 0; }
  #mid { width: 360px; background: white; border-right: 1px solid #e0e0e0; flex-shrink: 0; }
  #right { flex: 1; display: flex; flex-direction: column; min-width: 0; background: #f5f5f5; }
  /* Tree */
  .tree-section { padding: 8px 12px 4px; font-size: 11px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: 0.5px; border-top: 1px solid #f0f0f0; }
  .tree-section:first-child { border-top: none; }
  .cable { padding: 4px 12px; cursor: pointer; display: flex; align-items: center; gap: 6px; user-select: none; }
  .cable:hover { background: #f0f7ff; }
  .cable.selected { background: #d0e8ff; font-weight: 600; }
  .cable .badge { background: #4dabf7; color: white; border-radius: 9px; padding: 0 6px; font-size: 10px; min-width: 18px; text-align: center; }
  .cable.zero { color: #aaa; }
  .cable.zero .badge { background: #ddd; }
  /* PDF list (middle pane) */
  #mid h2 { padding: 12px 16px; font-size: 13px; background: #f7f7f7; border-bottom: 1px solid #e0e0e0; }
  #mid h2 .count { color: #888; font-weight: normal; font-size: 12px; margin-left: 6px; }
  .pdf-item { padding: 10px 16px; border-bottom: 1px solid #f0f0f0; cursor: pointer; display: flex; align-items: center; gap: 8px; }
  .pdf-item:hover { background: #f0f7ff; }
  .pdf-item.selected { background: #d0e8ff; }
  .pdf-item .body { flex: 1; min-width: 0; }
  .pdf-item .name { font-weight: 500; word-break: break-all; }
  .pdf-item .cables { margin-top: 4px; font-size: 11px; color: #4dabf7; }
  .pdf-item .cables span { display: inline-block; background: #e7f3ff; padding: 1px 6px; border-radius: 3px; margin-right: 4px; }
  .pdf-item .meta { margin-top: 3px; font-size: 11px; color: #888; }
  .pdf-item .preview-btn { flex-shrink: 0; padding: 5px 9px; background: #f0f7ff; border: 1px solid #4dabf7; border-radius: 4px; color: #1971c2; cursor: pointer; font-size: 14px; line-height: 1; }
  .pdf-item .preview-btn:hover { background: #4dabf7; color: white; }
  .empty { padding: 40px 16px; text-align: center; color: #999; font-size: 13px; }
  /* Right pane: now OCR-only (PDF preview is in the fullscreen modal below) */
  #ocr-pane { flex: 1; background: white; display: flex; flex-direction: column; min-height: 0; }
  #ocr-tabs { display: flex; background: #f7f7f7; border-bottom: 1px solid #e0e0e0; flex-shrink: 0; }
  #ocr-tab { padding: 8px 16px; cursor: pointer; border-right: 1px solid #e0e0e0; font-size: 12px; }
  #ocr-tab.active { background: white; font-weight: 600; }
  #ocr-tab .badge { background: #ddd; color: #666; border-radius: 9px; padding: 0 5px; font-size: 10px; margin-left: 4px; }
  #ocr-text { flex: 1; overflow: auto; padding: 12px 16px; font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
  mark.cable-exact { background: #c8e6c9; padding: 0 2px; border-radius: 2px; }
  mark.cable-conf { background: #fff59d; padding: 0 2px; border-radius: 2px; }
  #ocr-meta { padding: 6px 16px; font-size: 11px; color: #666; background: #fafafa; border-bottom: 1px solid #eee; flex-shrink: 0; }
  /* Empty right pane */
  #right .placeholder { flex: 1; display: flex; align-items: center; justify-content: center; color: #999; font-size: 14px; }
  /* Loading */
  .loading { padding: 16px; text-align: center; color: #999; font-size: 12px; }
  /* Fullscreen PDF preview modal */
  #fs-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.92); display: none; flex-direction: column; z-index: 1000; }
  #fs-modal.show { display: flex; }
  #fs-toolbar { padding: 8px 14px; background: #1a1a2e; color: white; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  #fs-toolbar .filename { flex: 1; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #fs-toolbar a { color: #4dabf7; text-decoration: none; font-size: 12px; }
  #fs-toolbar a:hover { text-decoration: underline; }
  #fs-toolbar button { background: #444; color: white; border: none; padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 14px; }
  #fs-toolbar button:hover { background: #666; }
  #fs-canvas-wrap { flex: 1; overflow: auto; text-align: center; padding: 16px; background: #2a2a2a; min-height: 0; }
  #fs-canvas { box-shadow: 0 2px 12px rgba(0,0,0,0.5); margin: 0 auto; display: block; max-width: 100%; }
  #fs-fallback { width: 100%; height: 100%; border: none; background: white; display: none; }
  #fs-page-nav { display: flex; align-items: center; gap: 8px; }
  #fs-page-info { font-size: 12px; min-width: 60px; text-align: center; }
</style>
</head>
<body>
<div id="header">
  <h1>📋 Cable Match Viewer</h1>
  <div class="stats" id="header-stats">加载中…</div>
  <input id="search" type="text" placeholder="🔍 搜索 cable / PDF 路径" />
</div>
<div id="main">
  <div id="left">
    <div class="loading" id="tree-status">加载中…</div>
  </div>
  <div id="mid">
    <div class="placeholder">← 从左侧选 cable 或 PDF</div>
  </div>
  <div id="right">
    <div class="placeholder">← 选 PDF 看 OCR 文本</div>
  </div>
</div>

<!-- Fullscreen PDF preview modal (only opened by the ⛶ button on each PDF row) -->
<div id="fs-modal" role="dialog" aria-modal="true">
  <div id="fs-toolbar">
    <span class="filename" id="fs-filename">…</span>
    <div id="fs-page-nav">
      <button id="fs-prev" type="button">←</button>
      <span id="fs-page-info">1 / ?</span>
      <button id="fs-next" type="button">→</button>
    </div>
    <a id="fs-open" href="#" target="_blank">↗ 新窗口</a>
    <a id="fs-download" href="#" download>⬇ 下载</a>
    <button id="fs-close" type="button">✕ 关闭</button>
  </div>
  <div id="fs-canvas-wrap">
    <canvas id="fs-canvas"></canvas>
    <iframe id="fs-fallback" src="about:blank"></iframe>
  </div>
</div>

<script>
const state = {
  cables: [],            // [{cable, pdf_count, ocr_bytes}, ...]
  pdfsByCable: {},       // cable → [{pdf_rel_path, content_hash, pdf_size, cables, ocr_text_preview}]
  pdfDetail: null,       // currently selected PDF detail (full OCR text)
  selectedCable: null,
  selectedPath: null,
  filter: '',            // search text
  pdfjsReady: false,
};

const $ = (id) => document.getElementById(id);

// ====== helpers ======
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return await r.json();
}

function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  return (n/1024/1024).toFixed(2) + ' MB';
}

// ====== header ======
async function loadSummary() {
  const s = await api('/api/summary');
  const parts = [
    `<b>${s.cables_with_matches}</b>/${s.total_cables} cables`,
    `<b>${s.total_pdfs}</b> PDFs`,
    `<b>${s.ocr_rows}</b> OCR rows`,
    `<b>${s.failed}</b> failed`,
    `engine: ${s.engine_used}`,
  ];
  $('header-stats').innerHTML = parts.join(' &nbsp;·&nbsp; ');
}

// ====== left pane: cable tree ======
async function loadCables() {
  state.cables = await api('/api/cables');
  renderTree();
}

function renderTree() {
  const filter = state.filter.toLowerCase();
  const matched = state.cables.filter(c => c.pdf_count > 0);
  const zero = state.cables.filter(c => c.pdf_count === 0);
  const filt = (c) => !filter || c.cable.toLowerCase().includes(filter);
  const left = $('left');
  const mhtml = matched.filter(filt).map(c =>
    `<div class="cable${c.pdf_count === 0 ? ' zero' : ''}${state.selectedCable === c.cable ? ' selected' : ''}" data-cable="${c.cable}">
       <span class="badge">${c.pdf_count}</span>
       <span>${c.cable}</span>
     </div>`
  ).join('');
  const zhtml = zero.filter(filt).slice(0, 500).map(c =>
    `<div class="cable zero${state.selectedCable === c.cable ? ' selected' : ''}" data-cable="${c.cable}">
       <span class="badge">0</span>
       <span>${c.cable}</span>
     </div>`
  ).join('');
  const more = zero.length > 500 ? `<div class="empty" style="padding:8px 12px;">… 还有 ${zero.length - 500} 个未匹配 cable</div>` : '';
  let html = '';
  if (mhtml) html += `<div class="tree-section">✅ 已匹配 (${matched.filter(filt).length})</div>${mhtml}`;
  if (zhtml) html += `<div class="tree-section">⬜ 未匹配 (${zero.filter(filt).length}${filter ? '' : ' 显示前 500'})</div>${zhtml}${more}`;
  if (!html) html = '<div class="empty">无匹配项</div>';
  left.innerHTML = html;
  // Delegate click
  left.querySelectorAll('.cable').forEach(el => {
    el.onclick = () => selectCable(el.dataset.cable);
  });
}

async function selectCable(cable) {
  state.selectedCable = cable;
  state.selectedPath = null;
  state.pdfDetail = null;
  renderTree(); // refresh highlight
  renderMidPane();
  // Clear right pane
  $('right').innerHTML = '<div class="placeholder">← 选 PDF 看预览 + OCR 文本</div>';
  try {
    const data = await api(`/api/cable/${encodeURIComponent(cable)}`);
    state.pdfsByCable[cable] = data.pdfs;
    renderMidPane();
  } catch (e) {
    $('mid').innerHTML = `<div class="empty">无此 cable: ${cable}</div>`;
  }
}

// ====== middle pane: PDFs under selected cable ======
function renderMidPane() {
  const cable = state.selectedCable;
  if (!cable) {
    $('mid').innerHTML = '<div class="placeholder">← 从左侧选 cable 或 PDF</div>';
    return;
  }
  const pdfs = state.pdfsByCable[cable] || [];
  const filter = state.filter.toLowerCase();
  const visible = pdfs.filter(p => !filter || p.pdf_rel_path.toLowerCase().includes(filter));
  let html = `<h2>${cable} <span class="count">(${pdfs.length} PDFs${filter ? ` · 过滤后 ${visible.length}` : ''})</span></h2>`;
  if (!visible.length) {
    html += '<div class="empty">无 PDF</div>';
  } else {
    html += visible.map(p => {
      const cables = (p.cables || []).filter(c => c !== cable);
      const otherCables = cables.length ? `<div class="cables">${cables.map(c => `<span>${c}</span>`).join('')}</div>` : '';
      const encPath = encodeURIComponent(p.pdf_rel_path);
      return `<div class="pdf-item${state.selectedPath === p.pdf_rel_path ? ' selected' : ''}" data-path="${encPath}">
        <div class="body">
          <div class="name">${p.pdf_rel_path}</div>
          ${otherCables}
          <div class="meta">${fmtSize(p.pdf_size)} · ${p.content_hash.slice(0, 12)}</div>
        </div>
        <button class="preview-btn" type="button" data-fs-path="${encPath}" title="全屏预览 PDF">⛶</button>
      </div>`;
    }).join('');
  }
  $('mid').innerHTML = html;
  // Whole-row click → load OCR; fullscreen button → open modal preview
  $('mid').querySelectorAll('.pdf-item').forEach(el => {
    el.onclick = (ev) => {
      // Ignore clicks that originated on the preview button (button handler
      // stops propagation; this is a defensive double-check).
      if (ev.target.closest('.preview-btn')) return;
      selectPdf(decodeURIComponent(el.dataset.path));
    };
  });
  $('mid').querySelectorAll('.preview-btn').forEach(btn => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      openFullscreenPreview(decodeURIComponent(btn.dataset.fsPath));
    };
  });
}

// ====== right pane: OCR-only (PDF preview moved to fullscreen modal) ======
async function selectPdf(pdfRelPath) {
  state.selectedPath = pdfRelPath;
  renderMidPane(); // refresh highlight
  await renderRightPane();
}

async function renderRightPane() {
  const path = state.selectedPath;
  if (!path) {
    $('right').innerHTML = '<div class="placeholder">← 选 PDF 看 OCR 文本</div>';
    return;
  }
  $('right').innerHTML = '<div class="loading">加载 OCR…</div>';
  let detail;
  try {
    detail = await api(`/api/pdf?path=${encodeURIComponent(path)}`);
  } catch (e) {
    $('right').innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
    return;
  }
  state.pdfDetail = detail;
  const ocrText = detail.ocr_text || '';
  const engine = (detail.ocr_rows && detail.ocr_rows[0] && detail.ocr_rows[0].ocr_engine) || 'tesseract';
  const lang = (detail.ocr_rows && detail.ocr_rows[0] && detail.ocr_rows[0].ocr_lang) || '';
  const dpi = (detail.ocr_rows && detail.ocr_rows[0] && detail.ocr_rows[0].ocr_dpi) || '';
  const allCables = detail.cables || [];
  const highlighted = highlightCables(ocrText, allCables);
  $('right').innerHTML = `
    <div id="ocr-pane">
      <div id="ocr-meta">
        <b>${path}</b> · ${fmtSize(detail.pdf_size)} · hash ${detail.content_hash.slice(0, 12)} ·
        OCR via ${engine} ${lang ? `(${lang})` : ''} ${dpi ? `@ ${dpi} dpi` : ''} ·
        cables: ${allCables.map(c => `<mark class="cable-exact">${c}</mark>`).join(' ')}
        <button type="button" class="preview-btn" data-fs-path="${encodeURIComponent(path)}" style="float:right;margin-left:8px;" title="全屏预览 PDF">⛶ PDF</button>
      </div>
      <div id="ocr-text">${highlighted}</div>
    </div>
  `;
  // Hook the inline OCR-pane preview button (same handler as the row button).
  const btn = $('right').querySelector('.preview-btn');
  if (btn) btn.onclick = () => openFullscreenPreview(decodeURIComponent(btn.dataset.fsPath));
}

function highlightCables(text, cables) {
  if (!text) return '<i style="color:#999">(无 OCR 文本)</i>';
  // Escape HTML first
  let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // For each cable, wrap occurrences with <mark>. Use case-insensitive + word-ish
  // boundary (we don't require strict word boundaries since cable IDs like 3B-507
  // contain digits; instead we just substitute substring → wrapped).
  const sorted = [...cables].sort((a, b) => b.length - a.length); // longest first
  for (const c of sorted) {
    if (!c) continue;
    const esc = c.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // Match the escaped form (since text is already escaped). Use word-ish
    // boundary to avoid eating adjacent characters.
    const re = new RegExp(escapeRegex(esc), 'gi');
    html = html.replace(re, `<mark class="cable-exact">${esc}</mark>`);
  }
  return html;
}

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ====== Fullscreen PDF preview modal ======
// Opened by the ⛶ button on each PDF row (or the inline button in the
// OCR-pane header). Closes on Esc / ✕ button / backdrop click.
function openFullscreenPreview(pdfRelPath) {
  const url = `/file?path=${encodeURIComponent(pdfRelPath)}`;
  $('fs-filename').textContent = pdfRelPath;
  $('fs-open').href = url;
  $('fs-download').href = url;
  $('fs-page-info').textContent = '加载…';
  $('fs-canvas').style.display = 'block';
  $('fs-fallback').style.display = 'none';
  $('fs-fallback').src = 'about:blank';
  $('fs-modal').classList.add('show');
  state._fsCurrentPath = pdfRelPath;
  renderPdfInModal(url);
}

function closeFullscreenPreview() {
  $('fs-modal').classList.remove('show');
  // Drop the page 1 canvas so the next open starts clean.
  const canvas = $('fs-canvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  state._pdfjsDoc = null;
  state._fsCurrentPath = null;
}

// Close on Esc / backdrop click
$('fs-modal').addEventListener('click', (ev) => {
  if (ev.target.id === 'fs-modal') closeFullscreenPreview();
});
$('fs-close').addEventListener('click', closeFullscreenPreview);
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && $('fs-modal').classList.contains('show')) {
    closeFullscreenPreview();
  }
});

// ====== PDF.js render (in modal canvas) ======
async function renderPdfInModal(url) {
  const canvas = $('fs-canvas');
  const iframe = $('fs-fallback');
  // Try PDF.js from CDN
  if (!window.pdfjsLib) {
    try {
      const pdfjs = await import('https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs');
      pdfjs.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs';
      window.pdfjsLib = pdfjs;
    } catch (e) {
      // Fallback to <iframe>
      canvas.style.display = 'none';
      iframe.style.display = 'block';
      iframe.src = url + '#toolbar=0';
      $('fs-page-info').textContent = 'iframe';
      $('fs-prev').disabled = $('fs-next').disabled = true;
      return;
    }
  }
  try {
    const pdfjs = window.pdfjsLib;
    const loadingTask = pdfjs.getDocument(url);
    const pdf = await loadingTask.promise;
    state._pdfjsDoc = pdf;
    $('fs-prev').disabled = $('fs-next').disabled = (pdf.numPages <= 1);
    await renderFsPage(1);
  } catch (e) {
    canvas.style.display = 'none';
    iframe.style.display = 'block';
    iframe.src = url + '#toolbar=0';
    $('fs-page-info').textContent = 'iframe (PDF.js 失败)';
    $('fs-prev').disabled = $('fs-next').disabled = true;
  }
}

async function renderFsPage(n) {
  const pdf = state._pdfjsDoc;
  if (!pdf) return;
  n = Math.max(1, Math.min(n, pdf.numPages));
  state._currentPage = n;
  const page = await pdf.getPage(n);
  const wrap = $('fs-canvas-wrap');
  const wrapWidth = wrap.clientWidth - 32;        // padding
  const baseViewport = page.getViewport({ scale: 1 });
  // Cap scale at 2x so big PDFs don't render > 2x screen width
  const scale = Math.min(2.0, Math.max(0.5, wrapWidth / baseViewport.width));
  const viewport = page.getViewport({ scale });
  const canvas = $('fs-canvas');
  const ctx = canvas.getContext('2d');
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  await page.render({ canvasContext: ctx, viewport }).promise;
  $('fs-page-info').textContent = `${n} / ${pdf.numPages}`;
}

$('fs-prev').addEventListener('click', () => renderFsPage(state._currentPage - 1));
$('fs-next').addEventListener('click', () => renderFsPage(state._currentPage + 1));

// ====== search filter ======
$('search').addEventListener('input', (e) => {
  state.filter = e.target.value.trim();
  renderTree();
  if (state.selectedCable) renderMidPane();
});

// ====== init ======
(async function init() {
  try {
    await loadSummary();
    await loadCables();
    $('tree-status').remove();
  } catch (e) {
    $('tree-status').textContent = '加载失败: ' + e.message;
  }
})();

// ====== search filter ======
$('search').addEventListener('input', (e) => {
  state.filter = e.target.value.trim();
  renderTree();
  if (state.selectedCable) renderMidPane();
});

// ====== init ======
(async function init() {
  try {
    await loadSummary();
    await loadCables();
    $('tree-status').remove();
  } catch (e) {
    $('tree-status').textContent = '加载失败: ' + e.message;
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


def _viewer(request: web.Request) -> CableMatchViewer:
    """Return the singleton viewer attached to the app."""
    return request.app['_viewer']


async def summary_handler(request: web.Request) -> web.Response:
    return web.json_response(_viewer(request).stats)


async def cables_handler(request: web.Request) -> web.Response:
    return web.json_response(_viewer(request).get_cables())


async def cable_handler(request: web.Request) -> web.Response:
    cable = request.match_info['cable']
    data = _viewer(request).get_cable(cable)
    if data is None:
        return web.json_response({'error': f'unknown cable: {cable!r}'}, status=404)
    return web.json_response(data)


async def pdf_handler(request: web.Request) -> web.Response:
    path = request.query.get('path', '')
    if not path:
        return web.json_response({'error': 'missing ?path=<rel>'}, status=400)
    data = _viewer(request).get_pdf(path)
    if data is None:
        return web.json_response(
            {'error': f'unknown pdf path: {path!r}', 'hint': 'path must be in state.json processed list'},
            status=404,
        )
    return web.json_response(data)


async def ocr_handler(request: web.Request) -> web.Response:
    """Return all OCR rows for a content_hash (matched by prefix)."""
    hash_prefix = request.match_info['hash']
    rows = _viewer(request).get_ocr_text_for_hash(hash_prefix)
    if rows is None:
        return web.json_response({'error': f'no OCR rows for hash: {hash_prefix!r}'}, status=404)
    return web.json_response(rows)


async def file_handler(request: web.Request) -> web.Response:
    """Stream the original PDF from disk.

    Traversal defense: viewer.resolve_pdf_path() requires the relative
    path to be in state.json's processed list AND the resolved absolute
    path to live under input_root. Anything else returns 404.
    """
    rel = request.query.get('path', '')
    if not rel:
        return web.json_response({'error': 'missing ?path=<rel>'}, status=400)
    viewer = _viewer(request)
    abs_path = viewer.resolve_pdf_path(rel)
    if abs_path is None:
        return web.json_response(
            {'error': f'pdf path not found or not in whitelist: {rel!r}'},
            status=404,
        )
    # Guess mime
    mime, _ = mimetypes.guess_type(str(abs_path))
    mime = mime or 'application/octet-stream'
    # Use FileResponse so aiohttp streams the file in chunks without
    # loading the whole thing into memory.
    return web.FileResponse(abs_path, headers={
        'Content-Type': mime,
        'Content-Disposition': f'inline; filename="{abs_path.name}"',
        'Cache-Control': 'no-cache',
    })


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------
async def main_async(port: int, host: str, state_path: str, cache_path: str, input_root: str | None) -> None:
    print(f'Loading viewer...', flush=True)
    viewer = CableMatchViewer(state_path=state_path, cache_path=cache_path, input_root=input_root)
    stats = viewer.stats
    print(f'  state:  {stats["state_path"]}', flush=True)
    print(f'  cache:  {stats["cache_path"]}', flush=True)
    print(f'  input:  {stats["input_root"]}', flush=True)
    print(f'  cables: {stats["total_cables"]} ({stats["cables_with_matches"]} matched)', flush=True)
    print(f'  PDFs:   {stats["total_pdfs"]} ({stats["failed"]} failed, {stats["no_match"]} no-match, {stats["no_text"]} no-text)', flush=True)
    print(f'  OCR rows: {stats["ocr_rows"]}', flush=True)

    app = web.Application()
    app['_viewer'] = viewer

    app.router.add_get('/', index_handler)
    app.router.add_get('/healthz', healthz_handler)
    app.router.add_get('/api/summary', summary_handler)
    app.router.add_get('/api/cables', cables_handler)
    app.router.add_get('/api/cable/{cable}', cable_handler)
    app.router.add_get('/api/pdf', pdf_handler)
    app.router.add_get('/api/ocr/{hash}', ocr_handler)
    app.router.add_get('/file', file_handler)

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
        description='Cable Match Viewer — browse cable_match state + cache.db in a web UI',
    )
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT,
                        help=f'Port (default {PORT_DEFAULT})')
    parser.add_argument('-b', '--bind', default='127.0.0.1',
                        help='Bind address (default 127.0.0.1; use 0.0.0.0 for LAN)')
    parser.add_argument('--state', required=True,
                        help='Path to .cable_match_state.json')
    parser.add_argument('--cache', required=True,
                        help='Path to .cable_match_cache.db (same stage dir as state)')
    parser.add_argument('--input-root',
                        help='PDF root dir on disk (defaults to state.json["input"])')
    args = parser.parse_args()

    try:
        asyncio.run(main_async(
            args.listen, args.bind, args.state, args.cache, args.input_root,
        ))
    except KeyboardInterrupt:
        print('\nShutting down...')


if __name__ == '__main__':
    main()
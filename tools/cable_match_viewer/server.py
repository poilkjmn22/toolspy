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
<script src="/static/vendor/flyfish-file-viewer-web-full.iife.js"></script>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system, "SF Pro", "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 13px; color: #222; background: #fafafa; }
  #app { display: flex; height: 100vh; }
  #left { width: 280px; border-right: 1px solid #e0e0e0; background: #fff; display: flex; flex-direction: column; }
  #right { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #tabs { display: flex; border-bottom: 1px solid #e0e0e0; }
  #tabs .tab { flex: 1; padding: 8px 14px; cursor: pointer; text-align: center; font-size: 12px; font-weight: 600; color: #888; border-bottom: 2px solid transparent; }
  #tabs .tab:hover { color: #555; }
  #tabs .tab.active { color: #1565c0; border-bottom-color: #1565c0; }
  #header { padding: 10px 14px; border-bottom: 1px solid #e0e0e0; font-weight: 600; font-size: 14px; }
  #stats { padding: 6px 14px; border-bottom: 1px solid #e0e0e0; color: #888; font-size: 11px; }
  #search { padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }
  #search input { width: 100%; padding: 5px 8px; border: 1px solid #ccc; border-radius: 3px; font-size: 12px; }
  #cable-list { flex: 1; overflow-y: auto; }
  .cable-row { padding: 5px 14px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; font-family: "SF Mono", Menlo, monospace; font-size: 12px; }
  .cable-row:hover { background: #f0f7ff; }
  .cable-row.selected { background: #d0e8ff; font-weight: 600; }
  .cable-row .cnt { color: #888; font-size: 11px; }
  .cab-row { padding: 6px 14px; border-bottom: 1px solid #f0f0f0; cursor: pointer; }
  .cab-row:hover { background: #f0f7ff; }
  .cab-row .cab-name { font-size: 12px; font-weight: 600; }
  .cab-row .cab-meta { font-size: 11px; color: #888; margin-top: 2px; }
  .cab-row .cab-doc { font-size: 10px; color: #999; margin-top: 1px; }
  .empty { padding: 14px; text-align: center; color: #999; font-size: 12px; }
  #detail { flex: 1; overflow-y: auto; padding: 14px 18px; }
  #detail .cable-id { font-family: "SF Mono", Menlo, monospace; font-size: 18px; font-weight: 600; margin-bottom: 10px; }
  #detail .section { margin: 18px 0; }
  #detail .section h3 { margin: 0 0 6px 0; font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #eee; padding-bottom: 4px; }
  #detail .doc-row { padding: 4px 0; display: flex; align-items: center; gap: 8px; font-size: 12px; }
  #detail .doc-row .doc-link { cursor: pointer; color: #06c; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #detail .doc-row .doc-link:hover { text-decoration: underline; }
  #detail .doc-row .doc-type { font-size: 10px; color: #888; }
  #detail .doc-row .preview-btn { padding: 2px 10px; border: 1px solid #4dabf7; background: #e3f2fd; color: #1565c0; border-radius: 3px; cursor: pointer; font-size: 11px; white-space: nowrap; }
  #detail .doc-row .preview-btn:hover { background: #bbdefb; }
  #detail .empty-state { color: #999; font-style: italic; padding: 4px 0; }

  /* Full-screen flyfish modal */
  #flyfish-modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #000; z-index: 9999; flex-direction: column; }
  #flyfish-modal.open { display: flex; }
  #flyfish-modal-header { padding: 8px 16px; background: #1a1a2e; color: white; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
  #flyfish-modal-header .title { font-size: 13px; }
  #flyfish-modal-header .close-btn { cursor: pointer; color: #ff6b6b; font-size: 18px; line-height: 1; padding: 0 4px; }
  #flyfish-modal-body { flex: 1; min-height: 0; }
  flyfish-file-viewer { width: 100%; height: 100%; display: block; }
</style>
</head>
<body>
<div id="app">
  <div id="left">
    <div id="tabs">
      <div class="tab active" data-tab="cables">电缆</div>
      <div class="tab" data-tab="cabinets">柜体</div>
      <div class="tab" data-tab="unclassified">未分类</div>
      <div class="tab" data-tab="stats">统计</div>
    </div>
    <div id="stats">加载中…</div>
    <div id="search"><input id="search-input" placeholder="过滤电缆…"></div>
    <div id="cable-list"><div class="empty">加载中…</div></div>
  </div>
  <div id="right">
    <div id="detail">
      <div class="empty-state">← 选择一条电缆查看详情</div>
    </div>
  </div>
</div>

<!-- Full-screen flyfish modal -->
<div id="flyfish-modal">
  <div id="flyfish-modal-header">
    <span class="title" id="flyfish-title"></span>
    <span class="close-btn" id="flyfish-close">✕</span>
  </div>
  <div id="flyfish-modal-body">
    <flyfish-file-viewer
      id="flyfish-viewer"
      locale="zh-CN"
      theme="light"
      toolbar-position="bottom-right"
    ></flyfish-file-viewer>
  </div>
</div>
<script>
let allCables = [];
let selectedCable = null;
let activeTab = 'cables';

const $ = (id) => document.getElementById(id);
const cableList = $('cable-list');
const detail = $('detail');
const search = $('search-input');
const flyfishModal = $('flyfish-modal');
const flyfishViewer = $('flyfish-viewer');
const flyfishTitle = $('flyfish-title');
const flyfishClose = $('flyfish-close');

// Tab switching
document.querySelectorAll('.tab').forEach(el => {
  el.onclick = () => {
    activeTab = el.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === activeTab));
    search.value = '';
    search.placeholder = activeTab === 'cables' ? '过滤电缆…'
      : activeTab === 'stats' ? ''
      : activeTab === 'unclassified' ? '过滤文件路径…'
      : '搜索柜体(区域-名称)…';
    search.style.display = (activeTab === 'stats' || activeTab === 'unclassified') ? 'none' : '';
    if (activeTab === 'cables') {
      renderCables();
      $('stats').textContent = `共 ${allCables.length} 条电缆`;
    } else if (activeTab === 'stats') {
      renderStats();
    } else if (activeTab === 'unclassified') {
      renderUnclassified();
    } else {
      cableList.innerHTML = '<div class="empty">输入关键字搜索柜体</div>';
      $('stats').textContent = '柜体搜索';
    }
  };
});

async function loadCables() {
  const [cablesR, statsR] = await Promise.all([
    fetch('/api/cables'),
    fetch('/api/stats'),
  ]);
  allCables = await cablesR.json();
  const statsData = await statsR.json();
  renderCables();
  $('stats').textContent = `${statsData.documents} 份图纸, ${statsData.distinct_cables} 条电缆, ${statsData.conductors} 线芯`;
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

async function searchCabinets() {
  const q = search.value.trim();
  if (!q) {
    cableList.innerHTML = '<div class="empty">输入关键字搜索柜体</div>';
    $('stats').textContent = '柜体搜索';
    return;
  }
  $('stats').textContent = '搜索中…';
  const r = await fetch('/api/search-cabinets?q=' + encodeURIComponent(q));
  const data = await r.json();
  $('stats').textContent = `找到 ${data.length} 个匹配项`;
  if (!data.length) {
    cableList.innerHTML = '<div class="empty">没有匹配的柜体</div>';
    return;
  }
  cableList.innerHTML = data.map(item => {
    const name = item.cabinet_name || item.cabinet_name_remote || '--';
    const remote = item.cabinet_name_remote ? ' → ' + escHtml(item.cabinet_name_remote) : '';
    const docName = item.document ? escHtml(item.document.rel_path || item.document.content_hash) : '--';
    const cables = item.cable_ids ? item.cable_ids.join(', ') : '';
    return `<div class="cab-row" data-cabinet="${escHtml(name)}">
      <div class="cab-name">${escHtml(name)}${remote}</div>
      <div class="cab-meta">${item.conductor_count} 线芯 | ${escHtml(cables)}</div>
      <div class="cab-doc">${docName}</div>
    </div>`;
  }).join('');
  cableList.querySelectorAll('.cab-row').forEach(el => {
    el.onclick = () => {
      const name = el.dataset.cabinet;
      // Switch to cable tab and search for related cables
      const cables = data.find(d => (d.cabinet_name || '') === name || (d.cabinet_name_remote || '') === name);
      if (cables && cables.cable_ids) {
        activeTab = 'cables';
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === 'cables'));
        search.placeholder = '过滤电缆…';
        search.value = cables.cable_ids[0] || '';
        renderCables();
        if (cables.cable_ids[0]) selectCable(cables.cable_ids[0]);
      }
    };
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
  let html = `<div class="cable-id">${escHtml(d.cable_id)}</div>`;

  // Source type badge
  const sourceType = d.conductors.length ? d.conductors[0].source_type : '';
  const badge = sourceType === 'terminal_strip' ? '端子排图' : sourceType === 'circuit_loop' ? '回路图' : '未知';
  html += `<span style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600;background:${sourceType === 'terminal_strip' ? '#e3f2fd' : '#fff3e0'};color:${sourceType === 'terminal_strip' ? '#1565c0' : '#e65100'};margin-bottom:10px;">${badge}</span>`;

  // Cabinet info
  const cabinetLocal = d.conductors.length ? (d.conductors[0].cabinet_name || '--') : '--';
  const cabinetRemote = d.conductors.length ? (d.conductors[0].cabinet_name_remote || '--') : '--';
  html += `<div style="font-size:12px;color:#666;margin-bottom:10px;">本端柜体: ${escHtml(cabinetLocal)} &nbsp;|&nbsp; 对端柜体: ${escHtml(cabinetRemote)}</div>`;

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
      html += `<tr style="border-bottom:1px solid #eee;"><td style="padding:4px 6px;font-family:monospace;">${no}</td><td style="padding:4px 6px;font-family:monospace;">${escHtml(strip)}:${tn}</td><td style="padding:4px 6px;font-family:monospace;">${escHtml(remote)}</td><td style="padding:4px 6px;">${escHtml(desc)}</td><td style="padding:4px 6px;font-family:monospace;">${escHtml(loop)}</td></tr>`;
    });
    html += '</table>';
  }
  html += `</div>`;
  html += `<div class="section"><h3>来源图纸 (${d.documents.length})</h3>`;
  html += d.documents.map(doc => {
    const rel = doc.document.rel_path || doc.document.content_hash;
    return `<div class="doc-row">
      <span class="doc-link" data-hash="${doc.document.content_hash}">${escHtml(rel)}</span>
      <span class="doc-type">${doc.document.document_type}</span>
      <span class="preview-btn" data-hash="${doc.document.content_hash}" data-name="${escHtml(rel)}">预览</span>
    </div>`;
  }).join('');
  html += `</div>`;
  detail.innerHTML = html;

  // Doc link click → open flyfish
  detail.querySelectorAll('.doc-link').forEach(el => {
    el.onclick = () => openFlyfish(el.dataset.hash, el.textContent.trim());
  });
  detail.querySelectorAll('.preview-btn').forEach(el => {
    el.onclick = () => openFlyfish(el.dataset.hash, el.dataset.name);
  });
}

function openFlyfish(hash, name) {
  flyfishTitle.textContent = name || hash;
  flyfishViewer.setAttribute('src', '/api/document/' + encodeURIComponent(hash) + '/file');
  flyfishViewer.setAttribute('filename', name || 'preview.dwg');
  flyfishModal.classList.add('open');
}

flyfishClose.onclick = () => {
  flyfishModal.classList.remove('open');
  flyfishViewer.removeAttribute('src');
  flyfishViewer.removeAttribute('filename');
};

search.oninput = () => {
  if (activeTab === 'cables') renderCables();
  else if (activeTab === 'cabinets') searchCabinets();
  else if (activeTab === 'unclassified') renderUnclassified();
};

function escHtml(s) {
  if (!s) return s;
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function renderUnclassified() {
  const r = await fetch('/api/unclassified?limit=500');
  const items = await r.json();
  let html = '<div style="padding:10px 14px;">';

  // Group by classification_primary for visual clarity
  const groups = {};
  for (const it of items) {
    const cls = it.classification_primary || 'unclassified';
    if (!groups[cls]) groups[cls] = [];
    groups[cls].push(it);
  }

  const clsLabels = {
    'protection_diagram': '保护 / 测控信号回路图',
    'panel_layout': '屏位 / 屏柜布置图',
    'monitoring_system': '状态监测 / 通风控制 / SF6',
    'unknown': '目录 / 封面 / 总说明',
    'unclassified': '未分类 (legacy)',
    '': '未分类',
  };

  if (Object.keys(groups).length === 0) {
    html += '<div class="empty" style="padding:20px;">所有图档均已分类 ✓</div>';
  } else {
    for (const cls of Object.keys(groups)) {
      const list = groups[cls];
      const label = clsLabels[cls] || cls;
      html += `<div class="section"><h3>${escHtml(label)} (${list.length})</h3>`;
      html += '<table style="width:100%;border-collapse:collapse;font-size:11px;">';
      html += '<tr style="background:#f5f5f5;font-weight:600;"><th style="padding:3px 6px;text-align:left;border-bottom:1px solid #ddd;">路径</th><th style="padding:3px 6px;text-align:right;border-bottom:1px solid #ddd;">置信度</th></tr>';
      for (const it of list.slice(0, 50)) {
        const path = it.rel_path.split(/[/\\]/).slice(-2).join('/');
        const conf = it.classification_confidence ? it.classification_confidence.toFixed(2) : '-';
        const dot = it.has_topology ? '<span style="color:#2e7d32;">●</span>' : '<span style="color:#bbb;">○</span>';
        html += `<tr style="border-bottom:1px solid #eee;"><td style="padding:3px 6px;font-family:monospace;">${dot} ${escHtml(path)}</td><td style="padding:3px 6px;text-align:right;font-family:monospace;">${conf}</td></tr>`;
      }
      if (list.length > 50) {
        html += `<tr><td colspan="2" style="padding:4px 6px;color:#999;text-align:center;">… 还有 ${list.length - 50} 份</td></tr>`;
      }
      html += '</table></div>';
    }
    html += `<div style="font-size:11px;color:#888;margin-top:8px;">● 已有 cable_topology &nbsp; ○ 无业务数据</div>`;
  }
  html += '</div>';
  cableList.innerHTML = html;
  $('stats').textContent = `${items.length} 份未处理`;
}

async function renderStats() {
  const r = await fetch('/api/stats');
  const s = await r.json();
  let html = '<div style="padding:10px 14px;">';

  // Scan metadata
  if (s.scan_input && s.scan_input !== '--') {
    html += `<div style="font-size:11px;color:#888;margin-bottom:8px;">扫描目录: ${escHtml(s.scan_input)}</div>`;
  }
  if (s.started_at && s.started_at !== '--') {
    html += `<div style="font-size:11px;color:#888;margin-bottom:12px;">开始扫描: ${escHtml(s.started_at)}</div>`;
  }

  // Summary cards
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:14px;">';
  const cards = [
    {label:'图纸总数', value:s.documents, color:'#1565c0'},
    {label:'关联电缆', value:s.distinct_cables, color:'#2e7d32'},
    {label:'线芯总数', value:s.conductors, color:'#e65100'},
    {label:'端子条', value:s.terminal_strips, color:'#6a1b9a'},
  ];
  cards.forEach(c => {
    html += `<div style="background:#f5f5f5;border-radius:4px;padding:8px;text-align:center;">
      <div style="font-size:20px;font-weight:700;color:${c.color};">${c.value}</div>
      <div style="font-size:11px;color:#888;margin-top:2px;">${c.label}</div>
    </div>`;
  });
  html += '</div>';

  // V6.5: classification breakdown
  const clsLabels = {
    'circuit_loop': '回路图',
    'terminal_strip': '端子排图',
    'cable_schedule': '电缆清册',
    'protection_diagram': '保护原理图',
    'panel_layout': '屏位布置图',
    'monitoring_system': '状态监测/通风',
    'unknown': '目录/封面',
    'unclassified': '(未分类)',
  };
  if (s.documents_by_classification && Object.keys(s.documents_by_classification).length) {
    html += '<div class="section"><h3>按业务分类 (V6.5)</h3>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="background:#f5f5f5;font-weight:600;"><th style="padding:3px 6px;text-align:left;border-bottom:1px solid #ddd;">分类</th><th style="padding:3px 6px;text-align:right;border-bottom:1px solid #ddd;">图档</th><th style="padding:3px 6px;text-align:right;border-bottom:1px solid #ddd;">占比</th></tr>';
    const total = s.documents || 1;
    for (const [cls, n] of Object.entries(s.documents_by_classification)) {
      const label = clsLabels[cls] || cls;
      const pct = ((n / total) * 100).toFixed(1);
      html += `<tr style="border-bottom:1px solid #eee;"><td style="padding:3px 6px;">${escHtml(label)}</td><td style="padding:3px 6px;text-align:right;font-family:monospace;">${n}</td><td style="padding:3px 6px;text-align:right;font-family:monospace;color:#888;">${pct}%</td></tr>`;
    }
    html += '</table></div>';
  }

  // V6.5: unmatched alert
  if (s.unmatched_documents > 0) {
    html += `<div class="section"><h3 style="color:#d32f2f;">无 analyzer 的图档 <span style="font-weight:400;color:#888;font-size:11px;">(等待新增 analyzer)</span></h3>`;
    html += `<div style="font-size:12px;color:#d32f2f;font-weight:600;">${s.unmatched_documents} 份</div>`;
    html += '<div style="font-size:11px;color:#888;margin-top:2px;">查看 <b>未分类</b> 标签页了解详细分布</div>';
    html += '</div>';
  }

  // Document type breakdown
  if (s.documents_by_type && Object.keys(s.documents_by_type).length) {
    html += '<div class="section"><h3>图纸类型分布</h3>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="background:#f5f5f5;font-weight:600;"><th style="padding:3px 6px;text-align:left;border-bottom:1px solid #ddd;">类型</th><th style="padding:3px 6px;text-align:right;border-bottom:1px solid #ddd;">数量</th></tr>';
    for (const [t, n] of Object.entries(s.documents_by_type)) {
      const label = t === 'dwg' ? 'DWG 图纸' : t === 'pdf' ? 'PDF 文档' : t;
      html += `<tr style="border-bottom:1px solid #eee;"><td style="padding:3px 6px;">${escHtml(label)}</td><td style="padding:3px 6px;text-align:right;font-family:monospace;">${n}</td></tr>`;
    }
    html += '</table></div>';
  }

  // Unprocessed documents (legacy V6 stat — kept for backward compat;
// the V6.5 "unmatched_documents" above replaces it with finer detail.)
  if (s.unprocessed_documents && s.unprocessed_documents > 0
      && (!s.unmatched_documents || s.unmatched_documents === 0)) {
    html += `<div class="section"><h3 style="color:#d32f2f;">未处理图纸 <span style="font-weight:400;color:#888;font-size:11px;">(无业务分类命中)</span></h3>`;
    html += `<div style="font-size:12px;color:#d32f2f;font-weight:600;">${s.unprocessed_documents} 份</div>`;
    html += '<div style="font-size:11px;color:#888;margin-top:2px;">这些图档未匹配到任何业务分类</div>';
    html += '</div>';
  }

  // Business type breakdown
  if (s.topology_by_source_type && Object.keys(s.topology_by_source_type).length) {
    html += '<div class="section"><h3>业务分类 (拓扑记录)</h3>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">';
    html += '<tr style="background:#f5f5f5;font-weight:600;"><th style="padding:3px 6px;text-align:left;border-bottom:1px solid #ddd;">分类</th><th style="padding:3px 6px;text-align:right;border-bottom:1px solid #ddd;">记录数</th><th style="padding:3px 6px;text-align:right;border-bottom:1px solid #ddd;">关联电缆</th></tr>';
    for (const [t, n] of Object.entries(s.topology_by_source_type)) {
      const cables = s.cables_by_source_type && s.cables_by_source_type[t] ? s.cables_by_source_type[t] : '--';
      const label = t === 'terminal_strip' ? '端子排图' : t === 'circuit_loop' ? '回路图' : t;
      html += `<tr style="border-bottom:1px solid #eee;"><td style="padding:3px 6px;">${escHtml(label)}</td><td style="padding:3px 6px;text-align:right;font-family:monospace;">${n}</td><td style="padding:3px 6px;text-align:right;font-family:monospace;">${cables}</td></tr>`;
    }
    html += '</table></div>';
  }

  // Cables with/without terminals
  html += '<div class="section"><h3>电缆端子状态</h3>';
  html += `<div style="font-size:12px;">有端子: <strong>${s.cables_with_terminals}</strong> &nbsp; 无端子: <strong>${s.cables_without_terminals}</strong></div>`;
  html += '</div>';

  // Distinct cabinets
  html += '<div class="section"><h3>其他</h3>';
  html += `<div style="font-size:12px;">不同柜体: <strong>${s.distinct_cabinets}</strong></div>`;
  html += '</div>';

  html += '</div>';
  cableList.innerHTML = html;
  $('stats').textContent = `${s.documents} 份图纸, ${s.distinct_cables} 条电缆`;
}

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


async def search_cabinets_handler(request: web.Request) -> web.Response:
    q = request.query.get('q', '').strip()
    if not q:
        return web.json_response([])
    return web.json_response(_viewer(request).search_cabinets(q))


async def unclassified_handler(request: web.Request) -> web.Response:
    """V6.5: documents whose classification has no analyzer yet."""
    limit = int(request.query.get('limit', '500'))
    return web.json_response(_viewer(request).list_unclassified_documents(limit=limit))


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
    static_path = Path(__file__).resolve().parent / 'static'
    app = web.Application()
    app['_viewer'] = viewer
    app['_conn'] = conn
    app.router.add_static('/static', static_path)
    app.router.add_get('/', index_handler)
    app.router.add_get('/api/cables', cables_handler)
    app.router.add_get('/api/cable/{cable}', cable_handler)
    app.router.add_get('/api/document/{hash}', document_handler)
    app.router.add_get('/api/document/{hash}/file', document_file_handler)
    app.router.add_get('/api/search-cabinets', search_cabinets_handler)
    app.router.add_get('/api/stats', stats_handler)
    app.router.add_get('/api/unclassified', unclassified_handler)
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
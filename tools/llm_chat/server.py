#!/usr/bin/env python3
import argparse
import asyncio
import aiohttp
import json
import socket


PORT_DEFAULT = 8002
LLAMA_SERVER = "http://localhost:8081"


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
<title>LLM Chat</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; height: 100vh; display: flex; flex-direction: column; }
  #header { padding: 12px 20px; background: #1a1a2e; color: white; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
  #header h1 { font-size: 16px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  #header h1::before { content: '🤖'; font-size: 20px; }
  #modelLabel { padding: 6px 12px; background: #2d2d4a; color: #a0d8f1; border: 1px solid #444; border-radius: 6px; font-size: 13px; font-family: 'SF Mono', Monaco, monospace; }
  #clearBtn { padding: 6px 16px; background: #dc3545; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; margin-left: 12px; }
  #clearBtn:hover { background: #c82333; }
  #chat { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
  .message { max-width: 85%; padding: 12px 16px; border-radius: 16px; line-height: 1.6; font-size: 15px; position: relative; }
  .user { align-self: flex-end; background: #4dabf7; color: white; border-bottom-right-radius: 4px; }
  .assistant { align-self: flex-start; background: #f1f3f5; color: #333; border-bottom-left-radius: 4px; padding-bottom: 32px; }
  .typing { display: flex; gap: 4px; padding: 12px 16px; background: #f1f3f5; border-radius: 16px; align-self: flex-start; }
  .typing span { width: 8px; height: 8px; background: #666; border-radius: 50%; animation: bounce 1.4s infinite ease-in-out; }
  .typing span:nth-child(1) { animation-delay: 0s; }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1); } }
  .msg-actions { position: absolute; top: 8px; right: 8px; display: none; gap: 4px; }
  .message:hover .msg-actions { display: flex; }
  .msg-actions button { padding: 4px 8px; background: rgba(0,0,0,0.1); border: none; border-radius: 4px; cursor: pointer; font-size: 12px; color: #666; }
  .msg-actions button:hover { background: rgba(0,0,0,0.2); color: #333; }
  .msg-content { white-space: pre-wrap; word-break: break-word; }
  .msg-content code { background: #e9ecef; padding: 2px 6px; border-radius: 4px; font-family: 'SF Mono', Monaco, monospace; font-size: 13px; }
  .msg-content pre { background: #2d2d2d; color: #f8f8f2; padding: 12px 16px; border-radius: 8px; overflow-x: auto; margin: 8px 0; }
  .msg-content pre code { background: none; padding: 0; color: inherit; }
  .msg-content strong { font-weight: 600; }
  #inputArea { padding: 16px 20px; background: #f8f9fa; border-top: 1px solid #dee2e6; display: flex; gap: 12px; flex-shrink: 0; }
  #input { flex: 1; padding: 12px 16px; border: 1px solid #ddd; border-radius: 24px; font-size: 15px; outline: none; resize: none; max-height: 120px; font-family: inherit; }
  #input:focus { border-color: #4dabf7; }
  #sendBtn { padding: 12px 24px; background: #4dabf7; color: white; border: none; border-radius: 24px; cursor: pointer; font-size: 15px; font-weight: 500; }
  #sendBtn:hover { background: #339af0; }
  #sendBtn:disabled { background: #adb5bd; cursor: not-allowed; }
  .empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #999; }
  .empty .icon { font-size: 48px; margin-bottom: 16px; }
  .error { background: #fff5f5; color: #c53030; border: 1px solid #feb2b2; padding: 12px 16px; border-radius: 8px; align-self: stretch; }
  .info { background: #ebf8ff; color: #2b6cb0; border: 1px solid #90cdf4; padding: 12px 16px; border-radius: 8px; align-self: stretch; font-size: 14px; }
</style>
</head>
<body>
<div id="header">
  <h1>LLM Chat</h1>
  <div style="display: flex; align-items: center;">
    <span id="modelLabel">加载中...</span>
    <button id="clearBtn" onclick="clearChat()">新对话</button>
  </div>
</div>

<div id="chat">
  <div class="empty">
    <div class="icon">💬</div>
    <p>开始聊天吧</p>
  </div>
</div>

<div id="inputArea">
  <textarea id="input" placeholder="输入消息..." rows="1"></textarea>
  <button id="sendBtn" onclick="sendMessage()">发送</button>
</div>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const modelLabel = document.getElementById('modelLabel');

let messages = [];
let isGenerating = false;

async function loadModel() {
  try {
    const resp = await fetch('/api/model');
    if (resp.ok) {
      const data = await resp.json();
      modelLabel.textContent = data.name || data.id || 'Unknown';
    } else {
      modelLabel.textContent = '模型加载失败';
    }
  } catch (e) {
    modelLabel.textContent = '获取模型失败';
  }
}

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

function escapeHtml(text) {
  if (!text) return '';
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatContent(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  html = html.replace(/```(\\w*)\\n?([\\s\\S]*?)```/g, '<pre><code>$2</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
  html = html.replace(/\\n/g, '<br>');
  return html;
}

function clearChat() {
  messages = [];
  chat.innerHTML = '<div class="empty"><div class="icon">💬</div><p>开始聊天吧</p></div>';
}

function createMsgActions(msgId, content) {
  return '<div class="msg-actions">' +
    '<button onclick="copyMsg(' + msgId + ')" title="复制">📋复制</button>' +
    '<button onclick="retryMsg(' + msgId + ')" title="重试">🔄重试</button>' +
    '<button onclick="editMsg(' + msgId + ')" title="修改">✏️修改</button>' +
    '<button onclick="deleteMsg(' + msgId + ')" title="删除">🗑️删除</button>' +
    '</div>';
}

let msgIdCounter = 0;

function addMessage(role, content, streamContent = '') {
  const empty = chat.querySelector('.empty');
  if (empty) empty.remove();

  const msgId = msgIdCounter++;
  const div = document.createElement('div');
  div.className = 'message ' + role;
  div.id = 'msg-' + msgId;

  const contentEl = document.createElement('div');
  contentEl.className = 'msg-content';
  contentEl.textContent = content;
  div.appendChild(contentEl);

  if (role === 'assistant') {
    div.innerHTML += createMsgActions(msgId, content);
    div.querySelector('.msg-actions').style.display = 'none';
    div.addEventListener('mouseenter', () => {
      div.querySelector('.msg-actions').style.display = 'flex';
    });
    div.addEventListener('mouseleave', () => {
      div.querySelector('.msg-actions').style.display = 'none';
    });
  }

  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return { msgId, contentEl };
}

function updateMessage(msgId, content) {
  const el = document.getElementById('msg-' + msgId);
  if (el) {
    el.querySelector('.msg-content').textContent = content;
    chat.scrollTop = chat.scrollHeight;
  }
}

function copyMsg(msgId) {
  const el = document.getElementById('msg-' + msgId);
  if (el) {
    navigator.clipboard.writeText(el.querySelector('.msg-content').textContent);
  }
}

function retryMsg(msgId) {
  const idx = messages.findIndex(m => m.id === msgId);
  if (idx >= 0) {
    const userMsg = messages[idx];
    messages = messages.slice(0, idx);
    const msgEl = document.getElementById('msg-' + msgId);
    if (msgEl) msgEl.remove();
    sendMessage(userMsg.content);
  }
}

function editMsg(msgId) {
  const idx = messages.findIndex(m => m.id === msgId);
  if (idx >= 0) {
    const msg = messages[idx];
    if (msg.role === 'user') {
      const newContent = prompt('修改问题:', msg.content);
      if (newContent !== null && newContent.trim()) {
        messages = messages.slice(0, idx);
        const msgEl = document.getElementById('msg-' + msgId);
        if (msgEl) msgEl.remove();
        sendMessage(newContent.trim());
      }
    }
  }
}

function deleteMsg(msgId) {
  const idx = messages.findIndex(m => m.id === msgId);
  if (idx >= 0) {
    messages.splice(idx, 1);
    const el = document.getElementById('msg-' + msgId);
    if (el) el.remove();
  }
}

async function sendMessage(overrideText) {
  if (isGenerating) return;
  const text = overrideText || input.value.trim();
  if (!text) return;

  isGenerating = true;
  sendBtn.disabled = true;
  if (!overrideText) {
    input.value = '';
    input.style.height = 'auto';
  }

  const userMsg = addMessage('user', text);
  messages.push({ id: userMsg.msgId, role: 'user', content: text });

  const typingEl = document.createElement('div');
  typingEl.className = 'message assistant';
  typingEl.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  chat.appendChild(typingEl);
  chat.scrollTop = chat.scrollHeight;

  try {
    const model = 'qwen2.5-0.5b-instruct-q4_k_m.gguf';
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, messages: messages.filter(m => m.role === 'user').map(m => ({ role: m.role, content: m.content })) })
    });

    if (!response.ok) {
      throw new Error('API Error: ' + response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let full = '';
    const assistantMsg = addMessage('assistant', '');
    messages.push({ id: assistantMsg.msgId, role: 'assistant', content: '' });
    typingEl.remove();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      const lines = chunk.split('\\n');
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const json = JSON.parse(line.slice(6));
            if (json.choices && json.choices[0]?.delta?.content) {
              full += json.choices[0].delta.content;
              updateMessage(assistantMsg.msgId, full);
            }
          } catch (e) {}
        }
      }
    }

    messages[messages.length - 1].content = full;
    chat.scrollTop = chat.scrollHeight;
  } catch (err) {
    typingEl.remove();
    const errEl = document.createElement('div');
    errEl.className = 'error';
    errEl.textContent = '错误: ' + err.message;
    chat.appendChild(errEl);
  }

  isGenerating = false;
  sendBtn.disabled = false;
  if (!overrideText) input.focus();
}

loadModel();
</script>
</body>
</html>
"""


def main():
    global LLAMA_SERVER

    parser = argparse.ArgumentParser(description='Local LLM Chat Server')
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT, help='Port')
    parser.add_argument('-s', '--server', default=None, help='llama.cpp server URL')
    args = parser.parse_args()

    if args.server:
        LLAMA_SERVER = args.server

    from aiohttp import web

    app = web.Application()

    async def index_handler(request):
        return web.Response(text=HTML_PAGE, content_type='text/html')

    async def chat_api(request):
        data = await request.json()
        model = data.get('model', 'qwen2.5-0.5b-instruct-q4_k_m.gguf')
        msgs = data.get('messages', [])

        url = f"{LLAMA_SERVER}/v1/chat/completions"
        payload = {"model": model, "messages": msgs, "stream": True}

        response = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream', 'X-Accel-Buffering': 'no'})
        await response.prepare(request)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    if resp.status != 200:
                        await response.write(f'Error: API returned {resp.status}'.encode())
                    else:
                        async for chunk in resp.content.iter_chunked(512):
                            if chunk:
                                await response.write(chunk)
                                await response.drain()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await response.write(f'Error: {str(e)}'.encode())
        finally:
            await response.write_eof()
        return response

    async def model_api(request):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{LLAMA_SERVER}/v1/models") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        model_info = data.get('data', [{}])[0]
                        model_name = model_info.get('id', 'unknown')
                        return web.json_response({'id': model_name, 'name': model_name})
                    return web.json_response({'id': 'unknown', 'name': '获取模型失败'})
        except Exception as e:
            return web.json_response({'id': 'error', 'name': str(e)}, status=500)

    app.router.add_get('/', index_handler)
    app.router.add_post('/api/chat', chat_api)
    app.router.add_get('/api/model', model_api)

    runner = web.AppRunner(app)

    async def run():
        await runner.setup()
        site = web.TCPSite(runner, '', args.listen)
        await site.start()
        ip = get_local_ip()
        print(f"LLM Chat at http://{ip}:{args.listen}")
        print(f"LLM Server: {LLAMA_SERVER}")
        print(f"Press Ctrl+C to stop")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == '__main__':
    main()

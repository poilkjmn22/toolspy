#!/usr/bin/env python3
import argparse
import asyncio
import aiohttp
import json
import socket


PORT_DEFAULT = 8002
LLAMA_SERVER = "http://localhost:8080"


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
  #modelSelect { padding: 6px 12px; background: #2d2d4a; color: white; border: 1px solid #444; border-radius: 6px; font-size: 13px; cursor: pointer; }
  #modelSelect option { background: #1a1a2e; }
  #clearBtn { padding: 6px 16px; background: #dc3545; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; margin-left: 12px; }
  #clearBtn:hover { background: #c82333; }
  #chat { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
  .message { max-width: 80%; padding: 12px 16px; border-radius: 16px; line-height: 1.6; font-size: 15px; white-space: pre-wrap; word-break: break-word; }
  .user { align-self: flex-end; background: #4dabf7; color: white; border-bottom-right-radius: 4px; }
  .assistant { align-self: flex-start; background: #f1f3f5; color: #333; border-bottom-left-radius: 4px; }
  .typing { display: flex; gap: 4px; padding: 12px 16px; background: #f1f3f5; border-radius: 16px; align-self: flex-start; }
  .typing span { width: 8px; height: 8px; background: #666; border-radius: 50%; animation: bounce 1.4s infinite ease-in-out; }
  .typing span:nth-child(1) { animation-delay: 0s; }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1); } }
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
    <select id="modelSelect">
      <option value="qwen2.5-3b-instruct-q4_K_M">qwen2.5:3b</option>
      <option value="llama3.2:3b">llama3.2:3b</option>
      <option value="phi3.5-mini-instruct">phi3.5:3.8b</option>
    </select>
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

let messages = [];
let isGenerating = false;

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

function clearChat() {
  messages = [];
  chat.innerHTML = '<div class="empty"><div class="icon">💬</div><p>开始聊天吧</p></div>';
}

function addMessage(role, content) {
  const empty = chat.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = 'message ' + role;
  div.textContent = content;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

async function sendMessage() {
  if (isGenerating) return;
  const text = input.value.trim();
  if (!text) return;

  isGenerating = true;
  sendBtn.disabled = true;
  input.value = '';
  input.style.height = 'auto';

  messages.push({ role: 'user', content: text });
  addMessage('user', text);

  const model = document.getElementById('modelSelect').value;
  const typingEl = document.createElement('div');
  typingEl.className = 'message assistant';
  typingEl.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  chat.appendChild(typingEl);

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, messages })
    });

    if (!response.ok) {
      throw new Error('API Error: ' + response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let full = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      full += chunk;
      typingEl.className = 'message assistant';
      typingEl.textContent = full;
      chat.scrollTop = chat.scrollHeight;
    }

    messages.push({ role: 'assistant', content: full });
  } catch (err) {
    typingEl.remove();
    const errEl = document.createElement('div');
    errEl.className = 'error';
    errEl.textContent = '错误: ' + err.message;
    chat.appendChild(errEl);
  }

  isGenerating = false;
  sendBtn.disabled = false;
  input.focus();
}
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
        model = data.get('model', 'qwen2.5-3b-instruct-q4_K_M')
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

    app.router.add_get('/', index_handler)
    app.router.add_post('/api/chat', chat_api)

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

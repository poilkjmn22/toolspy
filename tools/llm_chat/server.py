#!/usr/bin/env python3
import argparse
import asyncio
import aiohttp
import json
import socket


PORT_DEFAULT = 8002
DEFAULT_MODEL = "qwen2.5:3b-instruct-q4_K_M"
OLLAMA_HOST = "http://localhost:11434"


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
  .message { max-width: 80%; padding: 12px 16px; border-radius: 16px; line-height: 1.5; font-size: 15px; white-space: pre-wrap; word-break: break-word; }
  .user { align-self: flex-end; background: #4dabf7; color: white; border-bottom-right-radius: 4px; }
  .assistant { align-self: flex-start; background: #f1f3f5; color: #333; border-bottom-left-radius: 4px; }
  .assistant.streaming .cursor { display: inline; animation: blink 0.8s infinite; }
  .cursor { display: none; }
  @keyframes blink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }
  .typing-indicator { display: flex; gap: 4px; padding: 12px 16px; background: #f1f3f5; border-radius: 16px; align-self: flex-start; }
  .typing-indicator span { width: 8px; height: 8px; background: #666; border-radius: 50%; animation: bounce 1.4s infinite ease-in-out; }
  .typing-indicator span:nth-child(1) { animation-delay: 0s; }
  .typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
  .typing-indicator span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1); } }
  #inputArea { padding: 16px 20px; background: #f8f9fa; border-top: 1px solid #dee2e6; display: flex; gap: 12px; flex-shrink: 0; }
  #input { flex: 1; padding: 12px 16px; border: 1px solid #ddd; border-radius: 24px; font-size: 15px; outline: none; resize: none; max-height: 120px; font-family: inherit; }
  #input:focus { border-color: #4dabf7; }
  #sendBtn { padding: 12px 24px; background: #4dabf7; color: white; border: none; border-radius: 24px; cursor: pointer; font-size: 15px; font-weight: 500; }
  #sendBtn:hover { background: #339af0; }
  #sendBtn:disabled { background: #adb5bd; cursor: not-allowed; }
  .empty-state { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #999; }
  .empty-state .icon { font-size: 48px; margin-bottom: 16px; }
  .error { background: #fff5f5; color: #c53030; border: 1px solid #feb2b2; padding: 12px 16px; border-radius: 8px; align-self: stretch; }
</style>
</head>
<body>
<div id="header">
  <h1>LLM Chat</h1>
  <div style="display: flex; align-items: center;">
    <select id="modelSelect">
      <option value="qwen2.5:3b-instruct-q4_K_M">qwen2.5:3b</option>
      <option value="gemma3:4b">gemma3:4b</option>
      <option value="phi3.5:3.8b-mini-instruct-4k">phi3.5:3.8b</option>
    </select>
    <button id="clearBtn" onclick="clearChat()">新对话</button>
  </div>
</div>

<div id="chat">
  <div class="empty-state">
    <div class="icon">💬</div>
    <p>开始聊天吧！</p>
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
const modelSelect = document.getElementById('modelSelect');

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
  chat.innerHTML = '<div class="empty-state"><div class="icon">💬</div><p>开始聊天吧！</p></div>';
}

function addMessage(role, content) {
  const empty = chat.querySelector('.empty-state');
  if (empty) empty.remove();

  const div = document.createElement('div');
  div.className = 'message ' + role;
  div.textContent = content;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function appendChunk(el, chunk) {
  el.textContent += chunk;
  chat.scrollTop = chat.scrollHeight;
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

  const model = modelSelect.value;
  const assistantEl = addMessage('assistant', '');
  const typingEl = document.createElement('div');
  typingEl.className = 'typing-indicator';
  typingEl.innerHTML = '<span></span><span></span><span></span>';
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
      typingEl.remove();
      appendChunk(assistantEl, full);
      assistantEl.classList.add('assistant');
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


async def chat_handler(request):
    data = await request.json()
    model = data.get('model', DEFAULT_MODEL)
    messages = data.get('messages', [])

    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True
    }

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream'
    }

    response = aiohttp.ClientSession()
    try:
        async with response.post(url, json=payload, headers=headers) as resp:
            return web.StreamResponse(
                status=200,
                reason='OK',
                headers={
                    'Content-Type': 'text/event-stream',
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                }
            )
    except Exception as e:
        return web.Response(status=500, text=f'Failed to connect to Ollama: {e}')


async def main_async(port, model):
    app = web.Application()

    async def sse_chat_handler(request):
        data = await request.json()
        model_name = data.get('model', model)
        msgs = data.get('messages', [])

        async def stream_response():
            url = f"{OLLAMA_HOST}/api/chat"
            payload = {"model": model_name, "messages": msgs, "stream": True}

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                        if resp.status != 200:
                            yield b'event: error\ndata: API error\n\n'
                            return

                        async for chunk in resp.content.iter_chunked(1024):
                            if chunk:
                                yield b'data: ' + chunk + b'\n\n'
            except asyncio.CancelledError:
                raise
            except Exception as e:
                yield f'event: error\ndata: {str(e)}\n\n'.encode()

        return web.StreamResponse(
            stream_response(),
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            }
        )

    async def chat_api(request):
        data = await request.json()
        model_name = data.get('model', model)
        msgs = data.get('messages', [])

        payload = {"model": model_name, "messages": msgs, "stream": True}

        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            }
        )
        await response.prepare(request)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    async for chunk in resp.content.iter_chunked(512):
                        if chunk:
                            await response.write(b'data: ' + chunk + b'\n\n')
                            await response.drain()
        except Exception as e:
            await response.write(f'event: error\ndata: {str(e)}\n\n'.encode())
        finally:
            await response.write(b'data: [DONE]\n\n')
            await response.write_eof()
        return response

    async def index_handler(request):
        return web.Response(text=HTML_PAGE, content_type='text/html', headers={'Cache-Control': 'no-cache'})

    app.router.add_get('/', index_handler)
    app.router.add_post('/api/chat', chat_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '', port)
    await site.start()

    ip = get_local_ip()
    print(f"LLM Chat at http://{ip}:{port}")
    print(f"Ollama API: {OLLAMA_HOST}")
    print(f"Default model: {model}")
    print(f"Press Ctrl+C to stop")

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description='Local LLM Chat Server')
    parser.add_argument('-l', '--listen', type=int, default=PORT_DEFAULT, help=f'Port (default {PORT_DEFAULT})')
    parser.add_argument('-m', '--model', default=DEFAULT_MODEL, help='Default Ollama model')
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args.listen, args.model))
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == '__main__':
    main()

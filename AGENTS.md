# ToolsPy

A collection of small, independent Python tools with a unified CLI entry point.

## Run tools

```bash
# Activate virtual environment first
source myenv/bin/activate

# List all tools
python -m tools --list

# Run a specific tool
python -m tools docx-merger <args>
python -m tools text-sync -l 8000
python -m tools file-share -l 8001
python -m tools llm-chat -l 8002

# Or run tools directly
python tools/docx_merger/main.py <args>
python tools/text_sync/server.py -l 8000
python tools/file_share/server.py -l 8001
python tools/llm_chat/server.py -l 8002
```

## Architecture

- `tools/` - All tool packages live here
- `tools/cli.py` - Unified CLI dispatcher (maps tool name → module)
- `tools/<tool_name>/` - One directory per tool, contains `main()` function
- `TOOLS.md` - Full architecture and contribution guide
- Virtual env is at `myenv/` (not `venv/`)

## Existing tools

- **docx-merger** - Merges `.tsx` files into a DOCX document
- **text-sync** - HTTP server with real-time text sync across devices via WebSocket
- **file-share** - LAN file sharing with drag-and-drop upload, image preview, max 5GB per file, 20 files max
- **llm-chat** - Local LLM chat via Ollama API with streaming output

## Key facts

- `setup.sh` has a typo: installs `python-docs` instead of `python-docx`; use `requirements.txt` directly.
- Dependencies: `lxml`, `pillow`, `python-docx`, `typing_extensions`, `aiohttp`.
- No tests, no lint/typecheck configured.
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

# Or run tools directly (without going through CLI)
python tools/docx_merger/main.py <args>
python tools/text_sync/server.py -l 8000
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

## Key facts

- `setup.sh` has a typo: installs `python-docs` instead of `python-docx`; use `requirements.txt` directly.
- Dependencies: `lxml`, `pillow`, `python-docx`, `typing_extensions`.
- No tests, no lint/typecheck configured.
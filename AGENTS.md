# ToolsPy

Simple Python scripts: one merges `.tsx` files to DOCX, the other serves a real-time text sync page.

## Scripts

```bash
# Text -> DOCX merger (uses python-docx)
python src/main.py <source_folder> [-o output.docx] [--toc] [--header-footer]

# Real-time text sync HTTP server
python http_server.py [-l PORT] [-d DIRECTORY]
```

## Key facts

- DOCX merger searches for `.tsx` files (not `.txt`) in `find_txt_files()` at `src/main.py:88`.
- Virtual env is at `myenv/`, not `venv/`.
- `setup.sh` has a typo: installs `python-docs` instead of `python-docx`; use `requirements.txt` directly.
- Dependencies: `lxml`, `pillow`, `python-docx`, `typing_extensions` (from requirements.txt).
- No tests, no lint/typecheck configured.

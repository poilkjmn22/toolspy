# cable-match-viewer

A small aiohttp web UI for browsing a single `cable_match` stage's
`cable_match_state.json` + `cable_match_cache.db` outputs.

## What it shows

- **Left pane (cable tree)** — All cable IDs from the target CSV,
  grouped into "matched" and "unmatched" sections. Matched cables show
  how many PDFs they matched; unmatched cables (no PDF found in this
  stage) are still listed so you can spot coverage gaps.
- **Middle pane (PDFs under the selected cable)** — Every PDF the
  selected cable matched, plus a sub-list of *other* cables that
  same PDF matched (so you can see a PDF belongs to multiple cable
  groups).
- **Right pane (PDF + OCR)** — PDF.js in-browser preview of the
  selected PDF, plus the full OCR text with the matched cable IDs
  highlighted. OCR text comes from `cache.db` (raw output from
  `text-extractor` / `cable_match` pipeline); the metadata line
  `# Extracted from ... by text-extractor (OCR via <engine>, ...)`
  is preserved at the top so you can see which engine + recipe
  produced it.

## Run it

```bash
myenv/bin/python -m tools cable-match-viewer \
    --state /path/to/.stage_chieng_tess/.cable_match_state.json \
    --cache /path/to/.stage_chieng_tess/.cable_match_cache.db \
    -l 8003
```

Then open <http://localhost:8003> in your browser.

## Files

| File | Lines | What it does |
|------|-------|--------------|
| `__init__.py` | ~15  | re-exports `main()` and `PORT_DEFAULT` |
| `viewer.py`  | ~400 | loads state.json + cache.db, builds in-memory indices, query API |
| `server.py`  | ~600 | aiohttp HTTP server + inline HTML+JS (PDF.js via CDN) |

## Security

`/file?path=<rel>` serves the original PDF from disk. Defense-in-depth:

1. The path must be in `state.json['processed']` (whitelist built at viewer
   startup from the state file).
2. After resolving to an absolute path, it must lie under `input_root`
   (rejects `../` traversal).
3. Anything else → 404.

## Limitations

- **Single stage only.** Multi-stage union (`merge_5stage_matches.py` output)
  is not directly viewable; pick one stage's state.json + cache.db.
- **No live re-matching.** OCR text is the raw output from `cache.db`; the
  viewer doesn't re-run `find_matches()` on it. (Phase 1 — was discussed
  and user opted for raw cache.db OCR text.)
- **PDF.js via CDN.** If the browser can't reach `cdn.jsdelivr.net`,
  falls back to `<iframe>` + browser-native PDF viewer.
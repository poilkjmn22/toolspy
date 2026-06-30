#!/usr/bin/env python3
import sys
import argparse
import importlib

TOOLS = {
    'docx-merger': {
        'module': 'tools.docx_merger',
        'help': 'Merge .tsx files into a DOCX document',
    },
    'text-sync': {
        'module': 'tools.text_sync.server',
        'help': 'HTTP server with real-time text sync',
    },
    'file-share': {
        'module': 'tools.file_share.server',
        'help': 'LAN file sharing server with drag-and-drop upload',
    },
    'llm-chat': {
        'module': 'tools.llm_chat.server',
        'help': 'Local LLM chat server via Ollama API',
    },
    'text-extractor': {
        'module': 'tools.text_extractor',
        'help': 'Extract text from PDF/XLS/XLSX files to .txt (originals untouched)',
    },
    'pdf-organize': {
        'module': 'tools.pdf_organize',
        'help': 'Find PDFs containing a target string; copy/move matches to a new folder',
    },
    'process-xlsx-row': {
        'module': 'tools.process_xlsx_row',
        'help': 'Highlight xlsx rows that match a boolean expression of cell rules',
    },
        'cable-match-viewer': {
            'module': 'tools.cable_match_viewer',
            'help': 'Web UI to browse cable_engine cable.db: docs + entities + flyfish preview',
        },
}


def list_tools():
    print("Available tools:")
    for name, info in TOOLS.items():
        print(f"  {name:15s} - {info['help']}")


def main():
    parser = argparse.ArgumentParser(
        description='ToolsPy - A collection of handy command-line tools',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-l', '--list', action='store_true', help='List all available tools')
    parser.add_argument('tool', nargs='?', help='Tool to run')
    parser.add_argument('args', nargs=argparse.REMAINDER, help='Arguments for the tool')

    parsed = parser.parse_args()

    if parsed.list or not parsed.tool:
        list_tools()
        return

    if parsed.tool not in TOOLS:
        print(f"Error: unknown tool '{parsed.tool}'")
        print("Run with --list to see available tools")
        sys.exit(1)

    mod_path = TOOLS[parsed.tool]['module']
    try:
        mod = importlib.import_module(mod_path)
        sys.argv = [parsed.tool] + parsed.args
        if hasattr(mod, 'main'):
            mod.main()
        else:
            print(f"Error: {parsed.tool} has no main() function")
            sys.exit(1)
    except ModuleNotFoundError as e:
        print(f"Error: could not load tool '{parsed.tool}': {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error running {parsed.tool}: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
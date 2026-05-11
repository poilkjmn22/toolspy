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
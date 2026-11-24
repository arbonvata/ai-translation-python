"""Parse the existing command syntax and report errors at the CLI boundary."""

import argparse
import sys
from xml.etree import ElementTree

from .models import TranslationError
from .workflows import run_extraction, run_project_translation, run_translation, run_write_back


USAGE = """python3 translate.py <folder_path> [output.json]
       python3 translate.py --translate [input.json] [output.json] [--override]
       python3 translate.py --write-back [input.json] [--dry-run]
       python3 translate.py --project <values_dir> [--dry-run] [--override]"""


def parse_arguments(argv):
    """Retain the flag-based modes, filenames, and defaults used by existing users."""
    parser = argparse.ArgumentParser(
        prog="translate.py", usage=USAGE,
        description="Extract, translate, and write Android string resources.",
        epilog="Set DEEPL_BASE_URL to use the Go proxy; --override refreshes its cached translations.",
    )
    if argv and argv[0] == "--project":
        parser.add_argument("--project", metavar="values_dir", required=True,
                            help="Translate the app's default Android values directory in place.")
        parser.add_argument("--dry-run", action="store_true", help="List pending changes without calling DeepL.")
        parser.add_argument("--override", action="store_true",
                            help="Refresh requested cached translations (requires DEEPL_BASE_URL).")
        return "project", parser.parse_args(argv)
    if argv and argv[0] == "--translate":
        parser.add_argument("--translate", action="store_true", required=True)
        parser.add_argument("input", nargs="?", default="missing_translations.json")
        parser.add_argument("output", nargs="?", default="translations.json")
        parser.add_argument("--override", action="store_true",
                            help="Refresh requested cached translations (requires DEEPL_BASE_URL).")
        return "translate", parser.parse_args(argv)
    if argv and argv[0] == "--write-back":
        parser.add_argument("--write-back", action="store_true", required=True)
        parser.add_argument("input", nargs="?", default="translations.json")
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files.")
        return "write-back", parser.parse_args(argv)
    parser.add_argument("folder", help="Android resource folder to extract")
    parser.add_argument("output", nargs="?", default="missing_translations.json")
    if not argv:
        parser.print_help(sys.stderr)
        return None, None
    return "extract", parser.parse_args(argv)


def main(argv=None):
    """Return a process status; library modules raise errors instead of exiting."""
    mode, args = parse_arguments(sys.argv[1:] if argv is None else argv)
    if mode is None:
        return 1
    try:
        if mode == "project":
            run_project_translation(args.project, dry_run=args.dry_run, override=args.override)
        elif mode == "translate":
            run_translation(args.input, args.output, override=args.override)
        elif mode == "write-back":
            run_write_back(args.input, dry_run=args.dry_run)
        else:
            run_extraction(args.folder, args.output)
    except (TranslationError, OSError, ValueError, ElementTree.ParseError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0

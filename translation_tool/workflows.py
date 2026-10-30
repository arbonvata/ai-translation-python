"""Coordinate extraction, translation reports, and locale-file write-back."""

import json
import os
from pathlib import Path
import sys

from .android_xml import (
    clean_source_markers,
    extract_missing_translations,
    get_string_xml_files,
    write_language_file,
)
from .client import TranslationClient
from .models import StringResource, TARGET_LANGUAGES, placeholder_warnings


def read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=4, ensure_ascii=False)


def run_extraction(folder, output_file="missing_translations.json"):
    files = get_string_xml_files(folder)
    if not files:
        print("No matching string XML files found.", file=sys.stderr)
        return
    report = []
    for path in files:
        resources = extract_missing_translations(path)
        if resources:
            report.append({"file": str(path), "missing_translations": [r.to_dict() for r in resources]})
    write_json(output_file, report)
    print(f"Successfully saved {len(report)} files to '{output_file}'")


def resource_warnings(resources, translated, language):
    return [
        f"[{language}] {source.name}: '{problem}' missing in '{item.value}'"
        for source, item in zip(resources, translated)
        for problem in placeholder_warnings(source.value, item.value)
    ]


def translate_report(report, client):
    """Translate report entries without writing any output files."""
    output, warnings = [], []
    for entry in report:
        resources = [StringResource(**item) for item in entry["missing_translations"]]
        file_name = Path(entry["file"]).name
        for language in TARGET_LANGUAGES:
            print(f"Translating {len(resources)} strings from '{file_name}' to {language}...")
            translated = client.translate(resources, language)
            warnings.extend(resource_warnings(resources, translated, language))
            output.append({
                "file": entry["file"], "language": language,
                "translations": [item.to_dict() for item in translated],
            })
    return output, warnings


def save_translation_outputs(output_file, entries):
    """Save the combined report and one adjacent file per target language."""
    write_json(output_file, entries)
    output_path = Path(output_file)
    language_files = []
    for language in TARGET_LANGUAGES:
        path = output_path.with_name(f"{output_path.stem}_{language}{output_path.suffix}")
        write_json(path, [entry for entry in entries if entry["language"] == language])
        language_files.append(path)
    return language_files


def print_translation_summary(input_count, output_file, language_files, warnings):
    print(f"\nTranslated {input_count} files into {len(TARGET_LANGUAGES)} languages -> '{output_file}'")
    print("Per-language files:")
    for path in language_files:
        print(f"  {path}")
    if warnings:
        print(f"\n{len(warnings)} placeholder warnings — review these translations:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)


def run_translation(input_file, output_file, override=False):
    """Load environment configuration and run the report translation pipeline."""
    client = TranslationClient(
        api_key=os.environ.get("DEEPL_API_KEY", ""),
        base_url=os.environ.get("DEEPL_BASE_URL"), override=override,
    )
    report = read_json(input_file)
    output, warnings = translate_report(report, client)
    language_files = save_translation_outputs(output_file, output)
    print_translation_summary(len(report), output_file, language_files, warnings)


def write_source_translations(source_file, entries, dry_run):
    """Track successful writes before removing fully translated source markers."""
    written = {}
    for entry in entries:
        if not write_language_file(source_file, entry["language"], entry["translations"], dry_run):
            continue
        for translation in entry["translations"]:
            key = (translation.get("type"), translation["name"])
            written.setdefault(key, set()).add(entry["language"])
    completed = {key for key, languages in written.items() if languages >= set(TARGET_LANGUAGES)}
    if completed:
        clean_source_markers(source_file, completed, dry_run)


def run_write_back(input_file, dry_run=False):
    """Write locale files, then clean markers only for completed resources."""
    entries_by_file = {}
    for entry in read_json(input_file):
        entries_by_file.setdefault(entry["file"], []).append(entry)
    for source_file, entries in entries_by_file.items():
        write_source_translations(source_file, entries, dry_run)

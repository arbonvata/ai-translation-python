"""Coordinate extraction, translation reports, and locale-file write-back."""

import json
import os
from pathlib import Path
import sys

from .android_xml import (
    RESOURCE_TAGS,
    android_destination,
    clean_source_markers,
    extract_missing_translations,
    get_string_xml_files,
    localized_resources,
    missing_items,
    read_resource_xml,
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
    print_placeholder_warnings(warnings)


def print_placeholder_warnings(warnings):
    if warnings:
        print(f"\n{len(warnings)} placeholder warnings — review these translations:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)


def configured_client(override):
    return TranslationClient(
        api_key=os.environ.get("DEEPL_API_KEY", ""),
        base_url=os.environ.get("DEEPL_BASE_URL"), override=override,
    )


def run_translation(input_file, output_file, override=False):
    """Load environment configuration and run the report translation pipeline."""
    client = configured_client(override)
    report = read_json(input_file)
    output, warnings = translate_report(report, client)
    language_files = save_translation_outputs(output_file, output)
    print_translation_summary(len(report), output_file, language_files, warnings)


def plan_project_translations(values):
    locale_resources = {
        language: localized_resources(values.parent / f"values-{language}")
        for language in TARGET_LANGUAGES
    }
    pending = {}
    for source in sorted(path for path in values.glob("string*.xml") if path.is_file()):
        root = read_resource_xml(source).resources
        if root is None:
            raise ValueError(f"Android XML must contain a <resources> element: {source}")
        for tag in root.find_all(RESOURCE_TAGS, recursive=False):
            name = tag.get("name")
            if not name or tag.get("translatable") == "false":
                continue
            for language, resources in locale_resources.items():
                destination, localized = resources.get((tag.name, name), (None, None))
                items = missing_items(tag, localized)
                if items:
                    destination = destination or android_destination(source, language)
                    pending.setdefault((source, language, destination), []).extend(items)
    return pending


def run_project_translation(folder, *, dry_run=False, override=False):
    values = Path(folder)
    if values.name != "values" or not values.is_dir():
        raise ValueError(f"Expected the app's default 'values' directory: {values}")
    pending = plan_project_translations(values)
    if not pending:
        print(f"No pending translations in {values}")
        return
    if dry_run:
        for (_, _, destination), resources in pending.items():
            print(f"Would translate {len(resources)} values for {destination}")
        return

    client = configured_client(override)
    output, warnings = {}, []
    for (source, language, destination), resources in pending.items():
        print(f"Translating {len(resources)} values from '{source.name}' to {language}...")
        translated = client.translate(resources, language)
        warnings.extend(resource_warnings(resources, translated, language))
        output.setdefault(source, []).append((language, [item.to_dict() for item in translated], destination))
    print_placeholder_warnings(warnings)
    for source, entries in output.items():
        write_source_translations(source, entries, dry_run=False)


def write_source_translations(source_file, entries, dry_run):
    """Track successful writes before removing fully translated source markers."""
    written = {}
    for language, translations, destination in entries:
        if not write_language_file(source_file, language, translations, dry_run, destination=destination):
            continue
        for translation in translations:
            key = (translation.get("type"), translation["name"])
            written.setdefault(key, set()).add(language)
    completed = {key for key, languages in written.items() if languages >= set(TARGET_LANGUAGES)}
    if completed:
        clean_source_markers(source_file, completed, dry_run)


def run_write_back(input_file, dry_run=False):
    """Write locale files, then clean markers only for completed resources."""
    entries_by_file = {}
    for entry in read_json(input_file):
        entries_by_file.setdefault(entry["file"], []).append((entry["language"], entry["translations"], None))
    for source_file, entries in entries_by_file.items():
        write_source_translations(source_file, entries, dry_run)

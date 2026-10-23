import sys
import json
import os
import re
import requests
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List
from bs4 import BeautifulSoup, Comment, Tag

# Languages to translate the extracted strings into
TARGET_LANGUAGES = ["da", "de", "es", "fr", "sv"]

# DeepL endpoints: free-tier auth keys end with ":fx"
DEEPL_FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO_ENDPOINT = "https://api.deepl.com/v2/translate"
MAX_TEXTS_PER_REQUEST = 50  # DeepL accepts at most 50 texts per request

# Android format placeholders like %1$s / %2$d that must survive translation
PLACEHOLDER_PATTERN = re.compile(r"%\d+\$[sd]")

# 1. Define the Object structure
@dataclass
class StringResource:
    name: str
    value: str
    context: str

def get_string_xml_files(folder_path):
    """Finds all .xml files containing 'string' in the name."""
    target_dir = Path(folder_path)
    if not target_dir.is_dir():
        print(f"Error: '{target_dir}' is not a valid directory.", file=sys.stderr)
        sys.exit(1)
        
    matching_files = []
    for file_path in target_dir.rglob('*.xml'):
        if file_path.is_file() and 'string' in file_path.name.lower():
            matching_files.append(file_path)
    return matching_files

def extract_missing_translations(file_path: str) -> List[StringResource]:
    """Parses a single XML file and returns a list of StringResource objects."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'xml')
    except FileNotFoundError:
        print(f"Error: File not found at '{file_path}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error parsing XML: {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    for string_tag in soup.find_all('string'):
        ignore_attr = string_tag.get('tools:ignore', '')
        if 'MissingTranslation' in ignore_attr:
            name = string_tag.get('name', 'unknown')
            value = string_tag.get_text(strip=True)
            
            context = "No context found"
            for sibling in string_tag.previous_siblings:
                if isinstance(sibling, Comment):
                    # Checked first because Comment subclasses NavigableString
                    context = sibling.strip()
                    break
                if isinstance(sibling, Tag) or str(sibling).strip():
                    # Another element or real text sits above -> no comment directly above
                    break
                # Whitespace-only text node between comment and tag -> keep walking
                
            results.append(StringResource(
                name=name,
                value=value,
                context=context
            ))
    return results

def deepl_translate_texts(texts: List[str], target_language: str, api_key: str,
                          source_language: str = "EN") -> List[str]:
    """Translate a list of strings with the DeepL API, batching up to 50 texts per request."""
    endpoint = DEEPL_FREE_ENDPOINT if api_key.endswith(":fx") else DEEPL_PRO_ENDPOINT
    translations = []
    for start in range(0, len(texts), MAX_TEXTS_PER_REQUEST):
        chunk = texts[start:start + MAX_TEXTS_PER_REQUEST]
        response = requests.post(
            endpoint,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            json={
                "text": chunk,
                "source_lang": source_language,
                "target_lang": target_language.upper(),
                "preserve_formatting": True,
            },
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # 403 = invalid key, 429 = too many requests, 456 = quota exceeded
            print(f"DeepL API error for '{target_language}': {e} {response.text}", file=sys.stderr)
            sys.exit(1)
        translations.extend(t["text"] for t in response.json()["translations"])
    return translations

def translate_resources(resources: List[StringResource], target_language: str,
                        api_key: str) -> List[StringResource]:
    """Translate the value of every StringResource; name and context are kept unchanged."""
    translated_values = deepl_translate_texts([r.value for r in resources], target_language, api_key)
    return [
        StringResource(name=resource.name, value=value, context=resource.context)
        for resource, value in zip(resources, translated_values)
    ]

def placeholder_warnings(source_value: str, translated_value: str) -> List[str]:
    """Android placeholders (%1$s, %2$d, ...) and escaped apostrophes must survive translation."""
    problems = [
        placeholder for placeholder in PLACEHOLDER_PATTERN.findall(source_value)
        if placeholder not in translated_value
    ]
    if "\\'" in source_value and "\\'" not in translated_value:
        problems.append("escaped apostrophe \\'")
    return problems

def run_translation(input_file: str, output_file: str) -> None:
    """Reads an extraction report, translates every string into TARGET_LANGUAGES via DeepL."""
    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        print("Error: DEEPL_API_KEY environment variable is not set.", file=sys.stderr)
        print("Get a free key at https://www.deepl.com/pro-api and run:", file=sys.stderr)
        print("  export DEEPL_API_KEY=\"<your key>\"", file=sys.stderr)
        sys.exit(1)

    with open(input_file, 'r', encoding='utf-8') as f:
        report = json.load(f)

    output = []
    warnings = []
    for file_entry in report:
        resources = [StringResource(**item) for item in file_entry["missing_translations"]]
        file_name = Path(file_entry["file"]).name
        for language in TARGET_LANGUAGES:
            print(f"Translating {len(resources)} strings from '{file_name}' to {language}...")
            translated = translate_resources(resources, language, api_key)

            warnings.extend(
                f"[{language}] {source.name}: '{problem}' missing in '{item.value}'"
                for source, item in zip(resources, translated)
                for problem in placeholder_warnings(source.value, item.value)
            )
            output.append({
                "file": file_entry["file"],
                "language": language,
                "translations": [asdict(item) for item in translated],
            })

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=4, ensure_ascii=False)

    # Also dump one file per language next to the combined output
    output_path = Path(output_file)
    language_files = []
    for language in TARGET_LANGUAGES:
        language_entries = [entry for entry in output if entry["language"] == language]
        language_file = output_path.with_name(f"{output_path.stem}_{language}{output_path.suffix}")
        with open(language_file, 'w', encoding='utf-8') as f:
            json.dump(language_entries, f, indent=4, ensure_ascii=False)
        language_files.append(language_file)

    print(f"\nTranslated {len(report)} files into {len(TARGET_LANGUAGES)} languages -> '{output_file}'")
    print("Per-language files:")
    for language_file in language_files:
        print(f"  {language_file}")
    if warnings:
        print(f"\n{len(warnings)} placeholder warnings — review these translations:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--translate":
        # Translate a previous extraction report with the DeepL API.
        # Usage: python3 translate.py --translate [input.json] [output.json]
        input_file = sys.argv[2] if len(sys.argv) > 2 else "missing_translations.json"
        output_file = sys.argv[3] if len(sys.argv) > 3 else "translations.json"
        run_translation(input_file, output_file)
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Usage: python3 translate.py <folder_path>                 extract strings to JSON", file=sys.stderr)
        print("       python3 translate.py --translate [input] [output]  translate via DeepL API", file=sys.stderr)
        print("Example: python3 translate.py ./app/src/main/res", file=sys.stderr)
        sys.exit(1)

    folder = Path(sys.argv[1])

    # 1. Get full paths of all files containing "string" and ending with ".xml"
    # Full paths are required: several folders (values/, values-de/, ...)
    # each contain a file named strings.xml
    string_files = get_string_xml_files(folder)

    if not string_files:
        print("No matching string XML files found.", file=sys.stderr)
        sys.exit(0)

    report = []

    # 2. Run the extraction for every matching file
    for file_path in string_files:
        # This is a true List of Objects
        resources = extract_missing_translations(str(file_path))

        # Skip files without matches (e.g. locale files hold translations,
        # not the tools:ignore="MissingTranslation" markers)
        if not resources:
            continue

        # 3. Keep the file path together with its extracted objects
        report.append({
            "file": str(file_path),
            "missing_translations": [asdict(item) for item in resources]
        })
    # 4. Dump the JSON directly to a file
    # You can pass an optional 2nd argument for the output file name, or it defaults to 'missing_translations.json'
    output_file = sys.argv[2] if len(sys.argv) > 2 else "missing_translations.json"
    
    with open(output_file, 'w', encoding='utf-8') as f:
        # Note: we use json.dump() (no 's') to write directly to the file object
        json.dump(report, f, indent=4, ensure_ascii=False)
        
    print(f"Successfully saved {len(report)} files to '{output_file}'")
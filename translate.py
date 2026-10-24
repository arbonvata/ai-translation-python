import sys
import json
import os
import re
import requests
from pathlib import Path
from dataclasses import dataclass, asdict, replace
from typing import List
from bs4 import BeautifulSoup, Comment, Tag, NavigableString

# Languages to translate the extracted strings into
TARGET_LANGUAGES = ["da", "de", "es", "fr", "sv"]

# DeepL endpoints: free-tier auth keys end with ":fx"
DEEPL_FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO_ENDPOINT = "https://api.deepl.com/v2/translate"
MAX_TEXTS_PER_REQUEST = 50  # DeepL accepts at most 50 texts per request

# Android format placeholders like %1$s / %2$d / %s / %d that must survive translation
PLACEHOLDER_PATTERN = re.compile(r"%\d+\$[sd]|%[sd]")

# 1. Define the Object structure
@dataclass
class StringResource:
    name: str
    value: str
    context: str
    # Only set for <string-array> and <plurals> items; absent for plain strings
    type: str = None        # "string-array" or "plurals"
    quantity: str = None    # plurals only: "one", "other", ...
    index: int = None       # string-array only: item position

    def to_dict(self) -> dict:
        """asdict() without the None fields, so plain strings keep the same JSON shape."""
        return {key: value for key, value in asdict(self).items() if value is not None}

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

def comment_above(tag) -> str:
    """Returns the comment directly above a tag, or 'No context found'."""
    for sibling in tag.previous_siblings:
        if isinstance(sibling, Comment):
            # Checked first because Comment subclasses NavigableString
            return sibling.strip()
        if isinstance(sibling, Tag) or str(sibling).strip():
            # Another element or real text sits above -> no comment directly above
            break
        # Whitespace-only text node between comment and tag -> keep walking
    return "No context found"

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
    for tag in soup.find_all(['string', 'string-array', 'plurals']):
        if 'MissingTranslation' not in tag.get('tools:ignore', ''):
            continue
        name = tag.get('name', 'unknown')
        context = comment_above(tag)
        if tag.name == 'string':
            results.append(StringResource(
                name=name,
                value=tag.get_text(strip=True),
                context=context
            ))
        elif tag.name == 'string-array':
            for index, item in enumerate(tag.find_all('item')):
                results.append(StringResource(
                    name=name,
                    value=item.get_text(strip=True),
                    context=context,
                    type='string-array',
                    index=index
                ))
        elif tag.name == 'plurals':
            for item in tag.find_all('item'):
                results.append(StringResource(
                    name=name,
                    value=item.get_text(strip=True),
                    context=context,
                    type='plurals',
                    quantity=item.get('quantity')
                ))
    return results

def deepl_translate_texts(texts: List[str], target_language: str, api_key: str,
                          source_language: str = "EN", context: str = None) -> List[str]:
    """Translate a list of strings with the DeepL API, batching up to 50 texts per request."""
    endpoint = DEEPL_FREE_ENDPOINT if api_key.endswith(":fx") else DEEPL_PRO_ENDPOINT
    translations = []
    for start in range(0, len(texts), MAX_TEXTS_PER_REQUEST):
        chunk = texts[start:start + MAX_TEXTS_PER_REQUEST]
        payload = {
            "text": chunk,
            "source_lang": source_language,
            "target_lang": target_language.upper(),
            "preserve_formatting": True,
        }
        if context:
            payload["context"] = context
        response = requests.post(
            endpoint,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            json=payload,
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
    translated_values = {}

    # Plain strings and plurals: translate in shared batches
    plain_indices = [i for i, r in enumerate(resources) if r.type != 'string-array']
    plain_values = deepl_translate_texts(
        [resources[i].value for i in plain_indices], target_language, api_key)
    for i, value in zip(plain_indices, plain_values):
        translated_values[i] = value

    # string-array items: one request per array, passing the comment as DeepL
    # context — short items like "Mon" are ambiguous on their own
    array_indices = {}
    for i, resource in enumerate(resources):
        if resource.type == 'string-array':
            array_indices.setdefault(resource.name, []).append(i)
    for indices in array_indices.values():
        array_context = resources[indices[0]].context
        if array_context == "No context found":
            array_context = None
        values = deepl_translate_texts(
            [resources[i].value for i in indices], target_language, api_key,
            context=array_context)
        for i, value in zip(indices, values):
            translated_values[i] = value

    return [
        replace(resource, value=translated_values[i])
        for i, resource in enumerate(resources)
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
                "translations": [item.to_dict() for item in translated],
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

def escape_android_value(value: str) -> str:
    """Escape bare ASCII apostrophes — Android requires ' as \\' inside string resources."""
    return value.replace("\\'", "'").replace("'", "\\'")

def android_destination(source_file: str, language: str):
    """Maps a source file to its locale counterpart: values/strings.xml -> values-da/strings.xml."""
    source = Path(source_file)
    if source.parent.name != "values":
        return None
    return source.parent.parent / f"values-{language}" / source.name

def append_indented(parent, tag, indent: str) -> None:
    """Appends tag as the last element of parent, keeping the surrounding whitespace tidy."""
    contents = parent.contents
    last = contents[-1] if contents else None
    if isinstance(last, NavigableString) and not isinstance(last, Comment) and not last.strip():
        # Insert before the trailing whitespace (the newline before the closing tag)
        parent.insert(len(contents) - 1, NavigableString("\n" + indent))
        parent.insert(len(contents) - 1, tag)
    else:
        parent.append(NavigableString("\n" + indent))
        parent.append(tag)

def reindent(element, child_indent: str, closing_indent: str) -> None:
    """Restores indentation the xml parser collapses to bare newlines."""
    children = list(element.children)
    for position, child in enumerate(children):
        if not (isinstance(child, NavigableString) and not isinstance(child, Comment)
                and not child.strip()):
            continue
        indent = closing_indent if position == len(children) - 1 else child_indent
        child.replace_with(NavigableString("\n" + indent))

def drop_missing_translation_marker(tag) -> bool:
    """Removes MissingTranslation from tools:ignore — that marker belongs on source strings only."""
    ignore = tag.get("tools:ignore")
    if not ignore:
        return False
    remaining = [v.strip() for v in ignore.split(",")
                 if v.strip() and v.strip() != "MissingTranslation"]
    if remaining:
        tag["tools:ignore"] = ", ".join(remaining)
    else:
        del tag["tools:ignore"]
    return True

def set_comment(soup, element, context, indent: str = "    ") -> bool:
    """Makes the comment above element match context. Returns True when something changed."""
    if not context or context == "No context found":
        return False
    # Refresh a stale comment sitting directly above the element
    stale = None
    for sibling in element.previous_siblings:
        if isinstance(sibling, NavigableString) and not isinstance(sibling, Comment) \
                and not sibling.strip():
            continue
        if isinstance(sibling, Comment):
            if sibling.strip() == context:
                return False  # already correct
            stale = sibling
        break
    if stale is not None:
        whitespace_above = stale.previous_sibling
        stale.extract()
        if isinstance(whitespace_above, NavigableString) and not isinstance(whitespace_above, Comment) \
                and not whitespace_above.strip():
            whitespace_above.extract()
    previous = element.previous_sibling
    if isinstance(previous, NavigableString) and not isinstance(previous, Comment) \
            and not previous.strip():
        previous.replace_with(NavigableString("\n" + indent))
    else:
        element.insert_before(NavigableString("\n" + indent))
    element.insert_before(Comment(f" {context} "))
    element.insert_before(NavigableString("\n" + indent))
    return True

def upsert_string(soup, root, entry: dict) -> str:
    """Adds or updates a plain <string> element. Returns 'added', 'updated' or 'unchanged'."""
    value = escape_android_value(entry["value"])
    existing = root.find("string", attrs={"name": entry["name"]})
    if existing is not None:
        changed = drop_missing_translation_marker(existing)
        changed = set_comment(soup, existing, entry.get("context")) or changed
        if existing.get_text() == value and not changed:
            return "unchanged"
        if existing.get_text() != value:
            existing.string = value
        return "updated"
    tag = soup.new_tag("string")
    tag["name"] = entry["name"]
    tag.string = value
    append_indented(root, tag, "    ")
    set_comment(soup, tag, entry.get("context"))
    return "added"

def upsert_plurals(soup, root, group: List[dict]) -> str:
    """Adds or updates a <plurals> element from its quantity entries."""
    name = group[0]["name"]
    element = root.find("plurals", attrs={"name": name})
    created = element is None
    if created:
        element = soup.new_tag("plurals")
        element["name"] = name
        append_indented(root, element, "    ")
        changed = created
    else:
        changed = drop_missing_translation_marker(element)
        changed = set_comment(soup, element, group[0].get("context")) or changed
    for entry in sorted(group, key=lambda e: e.get("quantity", "")):
        value = escape_android_value(entry["value"])
        item = element.find("item", attrs={"quantity": entry.get("quantity")})
        if item is None:
            item = soup.new_tag("item")
            item["quantity"] = entry.get("quantity")
            item.string = value
            append_indented(element, item, "        ")
            changed = True
        elif item.get_text() != value:
            item.string = value
            changed = True
    if created:
        element.append(NavigableString("\n    "))
        set_comment(soup, element, group[0].get("context"))
    if not changed:
        return "unchanged"
    return "added" if created else "updated"

def upsert_string_array(soup, root, group: List[dict]) -> str:
    """Adds or updates a <string-array> element from its indexed entries."""
    name = group[0]["name"]
    element = root.find("string-array", attrs={"name": name})
    created = element is None
    if created:
        element = soup.new_tag("string-array")
        element["name"] = name
        append_indented(root, element, "    ")
        changed = created
    else:
        changed = drop_missing_translation_marker(element)
        changed = set_comment(soup, element, group[0].get("context")) or changed
    items = element.find_all("item")
    for entry in sorted(group, key=lambda e: e["index"]):
        index = entry["index"]
        value = escape_android_value(entry["value"])
        if index < len(items):
            if items[index].get_text() != value:
                items[index].string = value
                changed = True
        else:
            item = soup.new_tag("item")
            item.string = value
            append_indented(element, item, "        ")
            changed = True
    if created:
        element.append(NavigableString("\n    "))
        set_comment(soup, element, group[0].get("context"))
    if not changed:
        return "unchanged"
    return "added" if created else "updated"

def write_language_file(source_file: str, language: str, translations: List[dict],
                        dry_run: bool = False) -> None:
    """Writes one translated source file into its values-<language> counterpart."""
    destination = android_destination(source_file, language)
    if destination is None:
        print(f"Skipping '{source_file}': not inside a 'values' folder", file=sys.stderr)
        return

    if destination.is_file():
        with open(destination, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'xml')
        created = False
    else:
        soup = BeautifulSoup("<resources></resources>", "xml")
        created = True
    root = soup.find("resources")
    if not created:
        reindent(root, "    ", "")
        for element in root.find_all(["plurals", "string-array"]):
            reindent(element, "        ", "    ")

    # Group plurals / string-array entries so each element is written once
    groups, seen = [], set()
    for entry in translations:
        if entry.get("type") in ("plurals", "string-array"):
            key = (entry["type"], entry["name"])
            if key in seen:
                continue
            seen.add(key)
            groups.append([t for t in translations
                           if t.get("type") == entry["type"] and t["name"] == entry["name"]])
        else:
            groups.append([entry])

    counts = {"added": 0, "updated": 0, "unchanged": 0}
    for group in groups:
        if group[0].get("type") == "plurals":
            status = upsert_plurals(soup, root, group)
        elif group[0].get("type") == "string-array":
            status = upsert_string_array(soup, root, group)
        else:
            status = upsert_string(soup, root, group[0])
        counts[status] += 1

    if dry_run:
        verb = "would create" if created else "would update"
        print(f"{verb} {destination} "
              f"(add {counts['added']}, update {counts['updated']}, unchanged {counts['unchanged']})")
        return
    if counts["added"] == counts["updated"] == 0:
        print(f"unchanged, skipped {destination}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    body = str(soup)
    if body.startswith("<?xml"):
        body = body.split("?>", 1)[1].lstrip("\n")
    with open(destination, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n")
    verb = "created" if created else "updated"
    print(f"{verb} {destination} "
          f"(add {counts['added']}, update {counts['updated']}, unchanged {counts['unchanged']})")

def run_write_back(input_file: str, dry_run: bool = False) -> None:
    """Writes every translation from the combined output into the Android values-<lang> folders."""
    with open(input_file, 'r', encoding='utf-8') as f:
        entries = json.load(f)
    for entry in entries:
        write_language_file(entry["file"], entry["language"], entry["translations"], dry_run)

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--translate":
        # Translate a previous extraction report with the DeepL API.
        # Usage: python3 translate.py --translate [input.json] [output.json]
        input_file = sys.argv[2] if len(sys.argv) > 2 else "missing_translations.json"
        output_file = sys.argv[3] if len(sys.argv) > 3 else "translations.json"
        run_translation(input_file, output_file)
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "--write-back":
        # Write translations into the Android values-<lang> folders.
        # Usage: python3 translate.py --write-back [translations.json] [--dry-run]
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        input_file = args[0] if args else "translations.json"
        run_write_back(input_file, dry_run="--dry-run" in sys.argv)
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Usage: python3 translate.py <folder_path>                     extract strings to JSON", file=sys.stderr)
        print("       python3 translate.py --translate [input] [output]      translate via DeepL API", file=sys.stderr)
        print("       python3 translate.py --write-back [input] [--dry-run]  write values-<lang> XML files", file=sys.stderr)
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
            "missing_translations": [item.to_dict() for item in resources]
        })
    # 4. Dump the JSON directly to a file
    # You can pass an optional 2nd argument for the output file name, or it defaults to 'missing_translations.json'
    output_file = sys.argv[2] if len(sys.argv) > 2 else "missing_translations.json"
    
    with open(output_file, 'w', encoding='utf-8') as f:
        # Note: we use json.dump() (no 's') to write directly to the file object
        json.dump(report, f, indent=4, ensure_ascii=False)
        
    print(f"Successfully saved {len(report)} files to '{output_file}'")
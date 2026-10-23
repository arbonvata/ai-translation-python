import sys
import json
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List
from bs4 import BeautifulSoup, Comment, Tag

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

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 extract_strings.py <folder_path>", file=sys.stderr)
        print("Example: python3 extract_strings.py ./app/src/main/res", file=sys.stderr)
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
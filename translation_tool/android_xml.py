"""Read and update Android resource XML while preserving inline markup."""

from copy import deepcopy
from dataclasses import replace
import html
from pathlib import Path
import sys
from xml.etree import ElementTree

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .models import StringResource


def get_string_xml_files(folder_path):
    """Finds all .xml files containing 'string' in the name."""
    target_dir = Path(folder_path)
    if not target_dir.is_dir():
        raise ValueError(f"'{target_dir}' is not a valid directory.")
        
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


def resource_value(tag):
    """Keep whitespace and inline XML; plain values remain ordinary JSON strings."""
    if tag.find() is None:
        return {"value": tag.get_text()}
    # Namespace declarations on <resources> must travel with extracted fragments.
    fragment = deepcopy(tag)
    for child in fragment.find_all():
        if child.prefix and child.namespace:
            child.attrs.setdefault(f"xmlns:{child.prefix}", child.namespace)
    return {"value": fragment.decode_contents(), "markup": True}


def translation_value(resource, use_xml=True):
    """Send well-formed XML, escaping plain text when sharing a markup batch."""
    if not use_xml:
        return resource.value
    value = resource.value if resource.markup else html.escape(resource.value, quote=False)
    wrapped = f"<resource>{value}</resource>"
    ElementTree.fromstring(wrapped)
    return wrapped


def translated_resource(resource, value, use_xml=True):
    """Validate translated XML and remove the transport-only wrapper."""
    if not use_xml:
        return replace(resource, value=value)
    root = ElementTree.fromstring(value)
    if root.tag != "resource":
        raise ValueError("Translated XML is missing its resource wrapper")
    if resource.markup:
        fragment = BeautifulSoup(value, "xml", preserve_whitespace_tags={"resource"})
        return replace(resource, value=fragment.resource.decode_contents())
    return replace(resource, value="".join(root.itertext()))


def set_resource_value(tag, entry):
    """Write translated inline nodes as XML, escaping apostrophes in text only."""
    value = entry["value"]
    if not entry.get("markup"):
        tag.string = escape_android_value(value)
        return
    ElementTree.fromstring(f"<resource>{value}</resource>")
    fragment = BeautifulSoup(f"<resource>{value}</resource>", "xml",
                             preserve_whitespace_tags={"resource"}).resource
    for node in list(fragment.descendants):
        if isinstance(node, NavigableString) and not isinstance(node, Comment):
            node.replace_with(NavigableString(escape_android_value(str(node))))
    tag.clear()
    for node in list(fragment.contents):
        tag.append(node.extract())


def extract_tag_resources(tag):
    """Share name/context extraction while keeping collection-specific metadata."""
    common = {"name": tag.get("name", "unknown"), "context": comment_above(tag)}
    if tag.name == "string":
        yield StringResource(**common, **resource_value(tag))
        return
    for index, item in enumerate(tag.find_all("item")):
        metadata = {"index": index} if tag.name == "string-array" else {"quantity": item.get("quantity")}
        yield StringResource(**common, **resource_value(item), type=tag.name, **metadata)


def extract_missing_translations(file_path: str) -> list[StringResource]:
    """Read marked strings, arrays, and plurals from one Android resource file."""
    soup = read_resource_xml(file_path)
    return [
        resource
        for tag in soup.find_all(["string", "string-array", "plurals"])
        if "MissingTranslation" in tag.get("tools:ignore", "")
        for resource in extract_tag_resources(tag)
    ]


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
    if not ignore or "MissingTranslation" not in [value.strip() for value in ignore.split(",")]:
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


def read_resource_xml(path):
    with open(path, encoding="utf-8") as stream:
        return BeautifulSoup(stream, "xml", preserve_whitespace_tags={"string", "item", "resource"})


def normalize_resource_xml(soup):
    root = soup.find("resources")
    if root is None:
        raise ValueError("Android XML must contain a <resources> element")
    reindent(root, "    ", "")
    for element in root.find_all(["plurals", "string-array"]):
        reindent(element, "        ", "    ")
    return root


def save_resource_xml(path, soup):
    body = str(soup)
    if body.startswith("<?xml"):
        body = body.split("?>", 1)[1].lstrip("\n")
    Path(path).write_text('<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n", encoding="utf-8")


def prepare_resource(soup, root, tag_name, entry):
    """Create or find a named element and synchronize its shared metadata."""
    element = root.find(tag_name, attrs={"name": entry["name"]})
    created = element is None
    if created:
        element = soup.new_tag(tag_name)
        element["name"] = entry["name"]
        append_indented(root, element, "    ")
    changed = created or drop_missing_translation_marker(element)
    changed = set_comment(soup, element, entry.get("context")) or changed
    return element, created, changed


def update_value(element, entry):
    before = element.decode_contents()
    set_resource_value(element, entry)
    return element.decode_contents() != before


def append_item(soup, element, entry, **attrs):
    item = soup.new_tag("item", attrs=attrs)
    set_resource_value(item, entry)
    append_indented(element, item, "        ")


def collection_status(element, created, changed):
    if created:
        element.append(NavigableString("\n    "))
    return "added" if created else "updated" if changed else "unchanged"


def upsert_string(soup, root, entry):
    """Add or update a named string, including comments and inline markup."""
    element, created, changed = prepare_resource(soup, root, "string", entry)
    changed = update_value(element, entry) or changed
    return "added" if created else "updated" if changed else "unchanged"


def upsert_plurals(soup, root, group):
    """Update plural items by quantity while retaining unmentioned quantities."""
    element, created, changed = prepare_resource(soup, root, "plurals", group[0])
    for entry in sorted(group, key=lambda entry: entry.get("quantity", "")):
        item = element.find("item", attrs={"quantity": entry.get("quantity")})
        if item is None:
            append_item(soup, element, entry, quantity=entry.get("quantity"))
            changed = True
        else:
            changed = update_value(item, entry) or changed
    return collection_status(element, created, changed)


def upsert_string_array(soup, root, group):
    """Update array items by position while retaining unmentioned items."""
    element, created, changed = prepare_resource(soup, root, "string-array", group[0])
    items = element.find_all("item")
    for entry in sorted(group, key=lambda entry: entry["index"]):
        if entry["index"] < len(items):
            changed = update_value(items[entry["index"]], entry) or changed
        else:
            append_item(soup, element, entry)
            changed = True
    return collection_status(element, created, changed)


def translation_groups(translations):
    """Group collection entries in one pass, preserving first-seen order."""
    groups = []
    collections = {}
    for entry in translations:
        if entry.get("type") in ("plurals", "string-array"):
            key = (entry["type"], entry["name"])
            if key not in collections:
                collections[key] = []
                groups.append(collections[key])
            collections[key].append(entry)
        else:
            groups.append([entry])
    return groups


def apply_translations(soup, root, translations):
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    for group in translation_groups(translations):
        kind = group[0].get("type")
        if kind == "plurals":
            status = upsert_plurals(soup, root, group)
        elif kind == "string-array":
            status = upsert_string_array(soup, root, group)
        else:
            status = upsert_string(soup, root, group[0])
        counts[status] += 1
    return counts


def write_language_file(source_file, language, translations, dry_run=False):
    """Upsert one locale file; return False only when its source path is skipped."""
    destination = android_destination(source_file, language)
    if destination is None:
        print(f"Skipping '{source_file}': not inside a 'values' folder", file=sys.stderr)
        return False
    created = not destination.is_file()
    soup = BeautifulSoup("<resources></resources>", "xml") if created else read_resource_xml(destination)
    root = soup.resources if created else normalize_resource_xml(soup)
    counts = apply_translations(soup, root, translations)
    summary = f"(add {counts['added']}, update {counts['updated']}, unchanged {counts['unchanged']})"
    if dry_run:
        verb = "would create" if created else "would update"
        print(f"{verb} {destination} {summary}")
        return True
    if counts["added"] == counts["updated"] == 0:
        print(f"unchanged, skipped {destination}")
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    if created:
        root.append(NavigableString("\n"))
    save_resource_xml(destination, soup)
    verb = "created" if created else "updated"
    print(f"{verb} {destination} {summary}")
    return True


def clean_source_markers(source_file, completed, dry_run=False):
    """Remove source markers only for entries translated into every language."""
    soup = read_resource_xml(source_file)
    root = normalize_resource_xml(soup)
    removed = 0
    for tag in root.find_all(["string", "string-array", "plurals"]):
        kind = None if tag.name == "string" else tag.name
        if (kind, tag.get("name")) in completed and "MissingTranslation" in tag.get("tools:ignore", ""):
            removed += drop_missing_translation_marker(tag)
    if dry_run:
        print(f"would remove MissingTranslation from {removed} entries in {source_file}")
    elif removed:
        save_resource_xml(source_file, soup)
        print(f"removed MissingTranslation from {removed} entries in {source_file}")

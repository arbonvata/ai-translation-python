"""Resource data and validation shared by the pipeline."""

from collections import Counter
from dataclasses import asdict, dataclass
import re
from typing import List

TARGET_LANGUAGES = ["da", "de", "es", "fr", "sv"]
PLACEHOLDER_PATTERN = re.compile(r"%\d+\$[sd]|%[sd]")


class TranslationError(Exception):
    """An actionable translation or configuration error for the CLI to report."""


@dataclass
class StringResource:
    name: str
    value: str
    context: str
    # Only set for <string-array> and <plurals> items; absent for plain strings
    type: str = None        # "string-array" or "plurals"
    quantity: str = None    # plurals only: "one", "other", ...
    index: int = None       # string-array only: item position
    markup: bool = None     # True when value contains serialized inline XML

    def to_dict(self) -> dict:
        """asdict() without the None fields, so plain strings keep the same JSON shape."""
        return {key: value for key, value in asdict(self).items() if value is not None}


def resource_label(resource: StringResource) -> str:
    """Human-readable id for a resource, e.g. tab_month, week_days[0], items (one)."""
    if resource.type == 'string-array':
        return f"{resource.name}[{resource.index}]"
    if resource.type == 'plurals':
        return f"{resource.name} ({resource.quantity})"
    return resource.name


def placeholder_warnings(source_value: str, translated_value: str) -> List[str]:
    """Android placeholders (%1$s, %2$d, ...) and escaped apostrophes must survive translation."""
    # Compare counts: losing one of two identical placeholders is still a bug.
    missing = (Counter(PLACEHOLDER_PATTERN.findall(source_value))
               - Counter(PLACEHOLDER_PATTERN.findall(translated_value)))
    problems = list(missing.elements())
    if "\\'" in source_value and "\\'" not in translated_value:
        problems.append("escaped apostrophe \\'")
    return problems

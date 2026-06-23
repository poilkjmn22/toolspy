"""Built-in match functions for process-xlsx-row rules.

Each function takes a cell value and a target, returns bool.
Stringification is done in the caller so matchers stay simple.
"""
import re
from typing import Any, Callable, Dict


def equals(cell_value: Any, target: Any) -> bool:
    return str(cell_value) == str(target)


def contains(cell_value: Any, target: Any) -> bool:
    return str(target) in str(cell_value)


def startswith(cell_value: Any, target: Any) -> bool:
    return str(cell_value).startswith(str(target))


def endswith(cell_value: Any, target: Any) -> bool:
    return str(cell_value).endswith(str(target))


def regex_match(cell_value: Any, pattern: str) -> bool:
    return re.search(pattern, str(cell_value)) is not None


BUILTIN_MATCHERS: Dict[str, Callable[[Any, Any], bool]] = {
    'equals': equals,
    'contains': contains,
    'startswith': startswith,
    'endswith': endswith,
    'regex': regex_match,
}

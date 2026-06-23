"""Rule definitions, registry, JSON/Python loaders, and safe expression evaluator.

A Rule is a triple (rule_id, columns, matcher). `columns` is a tuple of
1-based column indices. A rule with multiple columns has OR semantics: it
matches if the matcher returns true for ANY of the listed columns.

The matcher is a unary callable `cell_value -> bool`. It is built from a
MatchSpec that can be:
  * a shorthand string  - "equals:Active", "regex:^foo", ...
  * a dict              - {"type": "equals", "value": "Active"}
                         {"type": "py", "function": "my_check"}
  * a callable          - a Python function (script context only)

The `column` value in JSON / Python rules is itself flexible:
  * single int / letter         - 3, "A", "AA"
  * list of ints / letters      - [1, 3, 6], ["A", "C", "F"], [1, "C", 6]
  * comma-separated string      - "1,3,6", "A,C,F", "1,C,F"
All forms are normalized to a tuple of 1-based ints.

The expression in --rules is a boolean expression over rule IDs. It is parsed
with `ast` and only allows: rule names, & (And), | (Or), ! (Not), parentheses,
True/False constants. No other names, no attribute access, no calls.
"""
import ast
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, Union

from openpyxl.utils import column_index_from_string

from tools.process_xlsx_row.matchers import BUILTIN_MATCHERS


MatchSpec = Union[str, Dict[str, Any], Callable[[Any], bool]]


ColumnSpec = Union[int, str, List[Any], Tuple[Any, ...]]


def _normalize_expression(expression: str) -> str:
    """Map user-facing operators to Python AST-friendly equivalents.

    `!` (unary NOT) is not a Python operator; rewrite it to `~` (Python's
    bitwise NOT, which the evaluator treats as boolean NOT). Skip `!=`.
    `|` and `&` are valid Python bitwise operators and are handled by the
    evaluator directly.
    """
    return re.sub(r'!(?!=)', '~', expression)


def _column_token_to_index(token: str) -> int:
    """Convert one column token to a 1-based int.

    Token is digits ("3") or 1-3 letters ("A", "AA", "AB"). Case-insensitive.
    """
    token = token.strip()
    if not token:
        raise ValueError("empty column token")
    if token.isdigit():
        n = int(token)
        if n < 1:
            raise ValueError(f"column index must be >= 1, got {token!r}")
        return n
    try:
        return column_index_from_string(token)
    except ValueError as e:
        raise ValueError(f"invalid column token {token!r}: {e}") from e


def parse_column_spec(spec: ColumnSpec) -> Tuple[int, ...]:
    """Normalize a column spec into a tuple of 1-based column ints.

    Accepted shapes (any of):
      int / str (single)
        3                -> (3,)
        "A"              -> (1,)
        "AA"             -> (27,)
      string with comma separators
        "1,3,6"          -> (1, 3, 6)
        "A,C,F"          -> (1, 3, 6)
        "1,C,F"          -> (1, 3, 6)        # mixed numbers & letters
        " 1 , C "        -> (1, 3)           # whitespace tolerant
      list / tuple of items
        [1, 3, 6]        -> (1, 3, 6)
        ("A", "C", "F")  -> (1, 3, 6)
        [1, "C", 6]      -> (1, 3, 6)

    Empty input, empty tokens, and invalid tokens raise ValueError.
    Duplicate columns are removed (order preserved).
    """
    if isinstance(spec, bool):
        # bool is a subclass of int — reject explicitly to avoid `True` parsing
        raise ValueError(f"invalid column spec: {spec!r}")

    if isinstance(spec, int):
        if spec < 1:
            raise ValueError(f"column index must be >= 1, got {spec}")
        return (spec,)

    if isinstance(spec, str):
        parts = [p for p in (s.strip() for s in spec.split(',')) if p]
        if not parts:
            raise ValueError(f"column spec {spec!r} is empty")
        tokens = parts
    elif isinstance(spec, (list, tuple)):
        if not spec:
            raise ValueError("column spec list cannot be empty")
        tokens = []
        for item in spec:
            if isinstance(item, bool):
                raise ValueError(
                    f"invalid column list item {item!r} (must be int or letter)"
                )
            if isinstance(item, int):
                if item < 1:
                    raise ValueError(
                        f"column index must be >= 1, got {item}"
                    )
                tokens.append(str(item))
            elif isinstance(item, str):
                item = item.strip()
                if not item:
                    raise ValueError(
                        "column spec contains empty token"
                    )
                tokens.append(item)
            else:
                raise ValueError(
                    f"invalid column list item {item!r} (must be int or letter)"
                )
    else:
        raise ValueError(
            f"invalid column spec: {spec!r} (expected int, str, list, or tuple)"
        )

    cols = [_column_token_to_index(t) for t in tokens]
    seen: set = set()
    unique: List[int] = []
    for c in cols:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return tuple(unique)


@dataclass
class Rule:
    rule_id: str
    columns: Tuple[int, ...]
    matcher: Callable[[Any], bool]
    description: str = ""

    @property
    def column(self) -> int:
        """Back-compat: first column (most rules have exactly one)."""
        return self.columns[0]


class RuleRegistry:
    def __init__(self) -> None:
        self.rules: Dict[str, Rule] = {}

    def add(self, rule: Rule) -> None:
        if not rule.rule_id.isidentifier():
            raise ValueError(
                f"Rule ID {rule.rule_id!r} must be a valid Python identifier "
                f"(it appears in the --rules expression)."
            )
        if rule.rule_id in self.rules:
            raise ValueError(
                f"Duplicate rule ID: {rule.rule_id!r} (already registered)"
            )
        self.rules[rule.rule_id] = rule

    def __contains__(self, item: str) -> bool:
        return item in self.rules

    def __iter__(self):
        return iter(self.rules)


def _build_builtin_matcher(mtype: str, value: Any) -> Callable[[Any], bool]:
    if mtype not in BUILTIN_MATCHERS:
        raise ValueError(
            f"Unknown matcher type: {mtype!r}. "
            f"Available built-ins: {sorted(BUILTIN_MATCHERS.keys())}"
        )
    if mtype == 'regex':
        try:
            pattern = re.compile(str(value))
        except re.error as e:
            raise ValueError(f"Invalid regex pattern {value!r}: {e}")
        def matcher(cell_value: Any) -> bool:
            return pattern.search(str(cell_value)) is not None
        return matcher
    target_str = '' if value is None else str(value)
    builtin = BUILTIN_MATCHERS[mtype]
    def matcher(cell_value: Any) -> bool:
        return builtin(str(cell_value), target_str)
    return matcher


def build_matcher(
    spec: MatchSpec,
    custom_functions: Dict[str, Callable],
) -> Callable[[Any], bool]:
    if callable(spec) and not isinstance(spec, str):
        return spec
    if isinstance(spec, str):
        if ':' in spec:
            mtype, _, value = spec.partition(':')
        else:
            raise ValueError(
                f"Invalid match spec string {spec!r}. "
                f"Expected 'type:value' format (e.g. 'equals:Active', 'regex:^foo')."
            )
        return _build_builtin_matcher(mtype, value)
    if isinstance(spec, dict):
        if 'type' not in spec:
            raise ValueError(
                f"Match spec dict must have 'type' field: {spec!r}"
            )
        mtype = spec['type']
        if mtype == 'py':
            func_name = spec.get('function')
            if not func_name:
                raise ValueError("'py' matcher requires 'function' field")
            if func_name not in custom_functions:
                raise ValueError(
                    f"Custom function {func_name!r} not found in rule script. "
                    f"Available: {sorted(custom_functions.keys())}"
                )
            return custom_functions[func_name]
        return _build_builtin_matcher(mtype, spec.get('value'))
    raise ValueError(f"Invalid match spec: {spec!r}")


def _register_from_spec(
    rule_id: str,
    spec: Any,
    registry: RuleRegistry,
    custom_functions: Dict[str, Callable],
) -> None:
    if not isinstance(spec, dict):
        raise ValueError(
            f"Rule {rule_id!r} spec must be a dict, got {type(spec).__name__}"
        )
    if 'columns' in spec:
        raise ValueError(
            f"Rule {rule_id!r}: use 'column' (singular), not 'columns'. "
            f"Pass a list or comma-separated string under 'column' to target "
            f"multiple columns."
        )
    col_spec = spec.get('column')
    if col_spec is None:
        raise ValueError(f"Rule {rule_id!r}: 'column' is required")
    try:
        columns = parse_column_spec(col_spec)
    except ValueError as e:
        raise ValueError(f"Rule {rule_id!r}: invalid 'column': {e}") from e
    match = spec.get('match')
    if match is None:
        raise ValueError(f"Rule {rule_id!r}: 'match' is required")
    matcher = build_matcher(match, custom_functions)
    registry.add(Rule(
        rule_id=rule_id,
        columns=columns,
        matcher=matcher,
        description=str(spec.get('description', '')),
    ))


def load_rules_from_json(path: Path, registry: RuleRegistry) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Rules file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}")
    if not isinstance(data, dict):
        raise ValueError(
            f"Rules JSON must be an object mapping rule_id -> spec, "
            f"got {type(data).__name__}"
        )
    for rule_id, spec in data.items():
        _register_from_spec(rule_id, spec, registry, custom_functions={})


def load_rules_from_script(
    path: Path,
    registry: RuleRegistry,
) -> Dict[str, Callable]:
    if not path.exists():
        raise FileNotFoundError(f"Rule script not found: {path}")
    spec = importlib.util.spec_from_file_location(
        "user_process_xlsx_row_rules", str(path)
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load Python script: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["user_process_xlsx_row_rules"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise RuntimeError(f"Error executing rule script {path}: {e}")

    custom_functions: Dict[str, Callable] = {}
    for name in dir(module):
        if name.startswith('_') or name == 'RULES':
            continue
        attr = getattr(module, name)
        if callable(attr):
            custom_functions[name] = attr

    if not hasattr(module, 'RULES'):
        raise ValueError(
            f"Rule script {path} must define a top-level 'RULES' variable "
            f"(a dict or a list of rule specs)."
        )

    rules_def = module.RULES
    if isinstance(rules_def, dict):
        for rule_id, rule_spec in rules_def.items():
            _register_from_spec(rule_id, rule_spec, registry, custom_functions)
    elif isinstance(rules_def, list):
        for rule_spec in rules_def:
            if not isinstance(rule_spec, dict):
                raise ValueError(
                    f"Each RULES list entry must be a dict, "
                    f"got {type(rule_spec).__name__}"
                )
            rule_id = rule_spec.get('id')
            if not isinstance(rule_id, str) or not rule_id:
                raise ValueError(
                    f"Each RULES list entry must have a string 'id' field: "
                    f"{rule_spec!r}"
                )
            _register_from_spec(rule_id, rule_spec, registry, custom_functions)
    else:
        raise ValueError(
            f"RULES must be a dict or list, got {type(rules_def).__name__}"
        )

    return custom_functions


class SafeExpressionEvaluator:
    """Safely evaluate a boolean expression over rule IDs.

    Allowed grammar (user-facing, any of these are accepted):
        r1 | r2                bitwise OR  -> boolean OR
        r1 & r2                bitwise AND -> boolean AND
        !r1                    bitwise NOT -> boolean NOT
        r1 or r2               boolean OR
        r1 and r2              boolean AND
        not r1                 boolean NOT
        ( expr )
        True / False

    Names must be known rule IDs. No function calls, no attribute access,
    no arithmetic, no comparisons.
    """

    def __init__(self, expression: str, registry: RuleRegistry) -> None:
        self.expression = expression
        self.registry = registry
        normalized = _normalize_expression(expression)
        try:
            self.tree = ast.parse(normalized, mode='eval')
        except SyntaxError as e:
            raise ValueError(f"Invalid expression {expression!r}: {e}")
        self._validate(self.tree.body)

    def _validate(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if child.id not in self.registry.rules:
                    raise ValueError(
                        f"Unknown rule ID in expression: {child.id!r}. "
                        f"Available: {sorted(self.registry.rules.keys())}"
                    )
            elif isinstance(child, ast.Call):
                raise ValueError(
                    f"Function calls are not allowed in expressions: "
                    f"{ast.dump(child)}"
                )
            elif isinstance(child, ast.Attribute):
                raise ValueError(
                    f"Attribute access is not allowed in expressions: "
                    f"{ast.dump(child)}"
                )
            elif isinstance(child, ast.BinOp) and not isinstance(
                child.op, (ast.BitOr, ast.BitAnd)
            ):
                raise ValueError(
                    f"Only '|' and '&' are allowed as binary operators; "
                    f"got {type(child.op).__name__}: {ast.dump(child)}"
                )
            elif isinstance(child, ast.UnaryOp) and not isinstance(
                child.op, (ast.Not, ast.Invert)
            ):
                raise ValueError(
                    f"Only 'not' and '!' are allowed as unary operators; "
                    f"got {type(child.op).__name__}: {ast.dump(child)}"
                )
            elif isinstance(child, ast.Compare):
                raise ValueError(
                    f"Comparisons are not allowed in expressions: "
                    f"{ast.dump(child)}"
                )

    def evaluate(
        self, row_values: List[Any]
    ) -> Tuple[bool, Dict[str, bool]]:
        rule_results: Dict[str, bool] = {}
        for rule_id, rule in self.registry.rules.items():
            hit = False
            for col in rule.columns:
                cell_value = (
                    row_values[col - 1]
                    if col <= len(row_values)
                    else None
                )
                try:
                    if bool(rule.matcher(cell_value)):
                        hit = True
                        break
                except Exception as e:
                    raise RuntimeError(
                        f"Rule {rule_id!r} raised {type(e).__name__}: {e}"
                    ) from e
            rule_results[rule_id] = hit
        return self._eval(self.tree.body, rule_results), rule_results

    @staticmethod
    def _eval(node: ast.AST, rule_results: Dict[str, bool]) -> bool:
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(
                    SafeExpressionEvaluator._eval(v, rule_results)
                    for v in node.values
                )
            if isinstance(node.op, ast.Or):
                return any(
                    SafeExpressionEvaluator._eval(v, rule_results)
                    for v in node.values
                )
            raise ValueError(
                f"Unsupported boolean operator: {type(node.op).__name__}"
            )
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.BitOr):
                return (
                    SafeExpressionEvaluator._eval(node.left, rule_results)
                    or SafeExpressionEvaluator._eval(node.right, rule_results)
                )
            if isinstance(node.op, ast.BitAnd):
                return (
                    SafeExpressionEvaluator._eval(node.left, rule_results)
                    and SafeExpressionEvaluator._eval(node.right, rule_results)
                )
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, (ast.Not, ast.Invert)):
                return not SafeExpressionEvaluator._eval(
                    node.operand, rule_results
                )
        if isinstance(node, ast.Name):
            return rule_results[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        raise ValueError(f"Unsupported expression node: {ast.dump(node)}")

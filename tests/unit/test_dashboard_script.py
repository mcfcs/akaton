"""Static checks on the dashboard's inline script.

None of the other tests execute this JavaScript — they assert markup is present and that
the endpoints behave — so a call to a helper that does not exist passes every one of them
and then throws in the browser on the first poll. That is exactly what happened: a
`rowButton` helper called `el(...)`, which was never defined, so `load()` aborted partway
and the page sat at "Loading monitor state…" with every counter blank.

These checks are deliberately crude. They cannot prove the script works, only that it does
not call something that is not there and does not reach for an element that does not exist.
"""

from __future__ import annotations

import re

from akaton.dashboard.web import DASHBOARD_HTML

SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, re.DOTALL))

# Anything called as `name(` where `name` is not preceded by a dot.
CALLS = re.compile(r"(?<![\w.$'\"])([A-Za-z_$][\w$]*)\s*\(")

KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "function",
    "typeof",
    "await",
    "new",
    "else",
    "do",
    "throw",
    "of",
    "in",
    "delete",
    "void",
    "case",
    "async",
}
BROWSER_GLOBALS = {
    "fetch",
    "setTimeout",
    "setInterval",
    "clearTimeout",
    "clearInterval",
    "document",
    "window",
    "console",
    "localStorage",
    "JSON",
    "Object",
    "Array",
    "Number",
    "String",
    "Boolean",
    "Date",
    "Math",
    "Promise",
    "Error",
    "encodeURIComponent",
    "decodeURIComponent",
    "parseInt",
    "parseFloat",
    "isNaN",
    "Set",
    "Map",
    "alert",
    "confirm",
    "requestAnimationFrame",
    "structuredClone",
}


def _declared() -> set[str]:
    names: set[str] = set(KEYWORDS | BROWSER_GLOBALS)
    names |= set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", SCRIPT))
    names |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)", SCRIPT))
    # Destructured bindings: const [a, b] = ... and const {a, b} = ...
    for group in re.findall(r"(?:const|let|var)\s*[\[{]([^\]}]*)[\]}]", SCRIPT):
        names |= {part.strip().split(":")[-1].strip() for part in group.split(",") if part.strip()}
    # Parameters of every function and arrow, which may themselves be called.
    for params in re.findall(r"function\s*[\w$]*\s*\(([^)]*)\)", SCRIPT):
        names |= {p.strip().split("=")[0].strip() for p in params.split(",") if p.strip()}
    for params in re.findall(r"\(([^)]*)\)\s*=>", SCRIPT):
        names |= {p.strip().split("=")[0].strip() for p in params.split(",") if p.strip()}
    names |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=>", SCRIPT))
    return {name for name in names if name}


def test_every_function_the_script_calls_is_defined():
    """`el(...)` was called by three helpers and defined by none of them."""
    declared = _declared()
    missing = sorted({name for name in CALLS.findall(SCRIPT) if name not in declared})
    assert not missing, f"the dashboard script calls undefined function(s): {missing}"


def test_every_element_the_script_reaches_for_exists():
    ids = set(re.findall(r'id="([\w-]+)"', DASHBOARD_HTML))
    wanted = set(re.findall(r"\$\('([\w-]+)'\)", SCRIPT))
    assert not (wanted - ids), f"script reaches for missing element(s): {sorted(wanted - ids)}"


def test_the_hidden_attribute_is_forced_to_win():
    """`hidden` is only `display:none` in the user-agent sheet, so any Tailwind display
    utility beats it. The modal is `hidden class="... flex ..."` and sat open over the
    whole page from load until this rule existed."""
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", DASHBOARD_HTML)


def test_elements_hidden_by_attribute_carry_no_conflicting_display_class():
    """Belt and braces: even with the rule above, this is a trap worth not re-laying."""
    display_utilities = {"flex", "grid", "block", "inline-block", "inline-flex", "table"}
    for tag in re.findall(r"<[^>]*\bhidden\b[^>]*>", DASHBOARD_HTML):
        classes = set(re.findall(r'class="([^"]*)"', tag)[0].split()) if 'class="' in tag else set()
        clash = classes & display_utilities
        assert not clash or re.search(r"\[hidden\]", DASHBOARD_HTML), tag[:80]


def test_empty_state_rows_span_their_whole_table():
    """Adding a column to a table and forgetting its empty-state colSpan is easy and silent."""
    tables = re.findall(r"<thead>(.*?)</thead>\s*<tbody id=\"([\w-]+)\"", DASHBOARD_HTML, re.DOTALL)
    assert tables, "no tables found; this check has stopped matching the markup"
    for head, body_id in tables:
        columns = len(re.findall(r"<th\b", head))
        # The empty-state row is written just after the render function grabs its tbody.
        block = re.search(rf"\$\('{re.escape(body_id)}'\).*?colSpan = (\d+)", SCRIPT, re.DOTALL)
        if not block:
            continue
        assert int(block.group(1)) == columns, (
            f"#{body_id} has {columns} columns but its empty row spans {block.group(1)}"
        )


def test_the_script_has_no_python_invalid_escape_sequences():
    """The template is a plain Python string, so a JS regex in it can warn at import time.

    `\\/` was the one that had bitten us, but `\\s`, `\\d` and `\\w` are far likelier in a
    regex and warned exactly the same way — `split(/\\s+/)` slipped past a check that only
    looked for `\\/`. Python 3.12 warns on these and a future version makes them an error,
    so the whole class is checked rather than the one instance we happened to hit.
    """
    import re as _re
    import warnings

    # Whatever Python itself considers invalid, asked directly rather than reimplemented.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SyntaxWarning)
        compile(f"_ = '''{DASHBOARD_HTML}'''", "<dashboard>", "exec")
    offenders = [str(item.message) for item in caught if item.category is SyntaxWarning]
    assert not offenders, f"invalid escape(s) in the dashboard template: {offenders}"
    # Belt and braces for the original case, which is silent in some Python builds.
    assert not _re.search(r"(?<!\\)\\/", DASHBOARD_HTML)

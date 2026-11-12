"""Repository boundaries and documentation must survive a fresh clone."""
import ast
import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_production_layers_do_not_depend_on_tests_or_concrete_adapters():
    for path in (ROOT / "app").rglob("*.py"):
        imports = set(imported_modules(path))
        assert not any(m == "tests" or m.startswith("tests.") for m in imports), path
        if path.parent.name == "domain":
            assert not any(m.startswith(("httpx", "supabase", "postgrest", "app.api", "app.services", "app.repositories", "app.integrations")) for m in imports), path
        if path.parent.name == "ports":
            assert not any(m.startswith(("supabase", "postgrest", "httpx", "app.services", "app.integrations")) for m in imports), path
        if path.parent.name == "services" and path.name != "container.py":
            assert not any(m.startswith("app.repositories.supabase") for m in imports), path


def test_shared_test_support_does_not_import_test_modules():
    for path in (ROOT / "tests").rglob("*.py"):
        assert not any(m.startswith(("tests.unit", "tests.contract", "tests.integration")) for m in imported_modules(path)), path


def test_tests_do_not_require_ignored_agent_notes_or_parent_docs():
    for path in (ROOT / "tests").rglob("test_*.py"):
        # Inspect actual filesystem expressions, not explanatory prose.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and isinstance(parents.get(node), (ast.BinOp, ast.Call)):
                assert ".agents/" not in node.value and ".agents\\" not in node.value, path


def test_documentation_relative_links_resolve_inside_repository():
    repository = ROOT.parent
    documents = [
        repository / "README.md",
        *(repository / "docs").rglob("*.md"),
        ROOT / "README.md",
        *(ROOT / "docs").rglob("*.md"),
        repository / "frontend/README.md",
        repository / "mobile/watch/README.md",
        repository / "deploy/README.md",
    ]
    for path in documents:
        text = path.read_text(encoding="utf-8")
        targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
        targets += re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', text)
        for target in targets:
            target = target.strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            resolved = (path.parent / unquote(target)).resolve()
            assert resolved.is_relative_to(repository), (path, target)
            assert resolved.exists(), (path, target)

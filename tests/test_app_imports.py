import ast
from pathlib import Path


def test_managed_service_does_not_import_legacy_runtime_or_jupiter():
    managed_files = Path("tradebot").rglob("*.py")
    imported = set()
    for path in managed_files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert "main" not in imported
    assert "jupiter_client" not in imported
    assert "server" not in imported

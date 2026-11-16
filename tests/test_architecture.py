# tests/test_architecture.py
"""Предотвращает смешивание domain/application с ORM, HTTP и Excel-адаптерами."""

import ast
from pathlib import Path


def test_dependency_direction():
    """Внутренние слои импортируют только свои контракты и независимые модели."""
    for folder in ("domain", "application"):
        for path in (Path("app") / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [name.name for name in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                assert not any(
                    module.startswith(
                        ("sqlalchemy", "fastapi", "httpx", "app.infrastructure", "xlsxwriter")
                    )
                    for module in modules
                ), path

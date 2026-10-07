"""Test de fumée : vérifie que le package est installé et importable."""

import text_to_sql_mcp


def test_package_importable() -> None:
    """Le package s'importe depuis l'environnement du projet (layout src/)."""
    assert text_to_sql_mcp.__name__ == "text_to_sql_mcp"

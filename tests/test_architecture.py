from __future__ import annotations

import ast
import unittest
from pathlib import Path


PACKAGE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "local_agent_record_janitor"
)


def imported_modules(name: str) -> set[str]:
    tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
        elif isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
    return result


class DependencyDirectionTests(unittest.TestCase):
    def test_human_and_agent_facades_share_service_without_importing_each_other(
        self,
    ) -> None:
        human = imported_modules("cli.py")
        agent = imported_modules("agent_cli.py")

        self.assertIn("cleanup_service", human)
        self.assertIn("cleanup_service", agent)
        self.assertNotIn("cli", agent)
        # ``cli.main`` is the package-level command router and may dispatch
        # once after argparse.  The agent implementation itself must never
        # import or reuse the human command flow.

    def test_core_layers_do_not_depend_on_cli_facades(self) -> None:
        for name in (
            "core_types.py",
            "planning.py",
            "cleanup_service.py",
            "execution.py",
            "session_cleanup.py",
            "relation_cleanup.py",
            "frontend_reference_cleanup.py",
        ):
            with self.subTest(module=name):
                imports = imported_modules(name)
                self.assertNotIn("cli", imports)
                self.assertNotIn("agent_cli", imports)


if __name__ == "__main__":
    unittest.main()

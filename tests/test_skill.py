from __future__ import annotations

from pathlib import Path
import unittest


SKILL = Path(__file__).resolve().parents[1] / "skills" / "agent-preview" / "SKILL.md"


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = SKILL.read_text(encoding="utf-8")

    def test_occupied_interaction_has_exact_two_labels(self) -> None:
        self.assertEqual(self.content.count("1. **Kill process and replace it**"), 1)
        self.assertEqual(self.content.count("2. **Keep existing process**"), 1)
        self.assertIn("Present only those two choices", self.content)
        self.assertIn("Do not add, remove, rename, abbreviate, recommend, or reorder choices", self.content)

    def test_collision_check_precedes_project_modification(self) -> None:
        occupied_guard = self.content.index("If either result shows an occupied port")
        project_inspection = self.content.index("Inspect the project's documentation")
        self.assertLess(occupied_guard, project_inspection)
        self.assertIn("do not place new content beneath the working directory", self.content)


if __name__ == "__main__":
    unittest.main()

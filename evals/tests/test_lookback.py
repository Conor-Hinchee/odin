"""The lookback recall scorer: ground truth comes from the runner and the protocol checker, not the state file."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lookback as L  # noqa: E402


class LookbackTest(unittest.TestCase):
    def test_expected_failures_from_runner_rows(self):
        row = {"opened_pr": False, "nudges": [{"gate": "huginn-plan"}, {"gate": "human-validation"}],
               "protocol": {"violations": ["state_file_first"]}}
        self.assertEqual(L.expected(row), {"missing-pr", "gate-nudged", "state-file-late"})
        clean = {"opened_pr": True, "nudges": [{"gate": "human-validation"}], "protocol": {"violations": []}}
        self.assertEqual(L.expected(clean), set(), "a sign-off nudge comes after the retrospective")

    def test_opened_pr_falls_back_to_gh_log(self):
        with tempfile.TemporaryDirectory() as d:
            trial = Path(d)
            (trial / "gh.log").write_text("gh pr create --help\ngh pr view\n")
            self.assertFalse(L.opened_pr({"opened_pr": None}, trial))
            (trial / "gh.log").write_text("gh pr create --title x --body y\n")
            self.assertTrue(L.opened_pr({"opened_pr": None}, trial))

    def test_table_counts_recall_and_false_positives(self):
        res = {"trials": [
            {"trial": "a", "expected": ["missing-pr"], "written": [], "surfaced": ["missing-pr"]},
            {"trial": "b", "expected": ["gate-nudged"], "written": ["gate-nudged"], "surfaced": []},
            {"trial": "c", "expected": [], "written": [], "surfaced": ["state-file-late"]},
        ], "skipped": ["d"]}
        t = L.table(res)
        self.assertIn("| `missing-pr` | 1 | 0/1 | 1/1 | 0 |", t)
        self.assertIn("| `gate-nudged` | 1 | 1/1 | 0/1 | 0 |", t)
        self.assertIn("| `state-file-late` | 0 | 0/0 | 0/0 | 1 |", t)
        self.assertIn("| **all** | 2 | 1/2 | 1/2 | 1 |", t)
        self.assertIn("never started): d", t)


if __name__ == "__main__":
    unittest.main()

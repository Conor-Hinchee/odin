"""Self-tests for the eval cases and grader (no model calls, no cost).

For every case:
  - null:   the untouched fixture fails the hidden tests (the case tests something);
  - oracle: the reference fix passes hidden + visible tests, every repo check, and stays in scope.
Cases with repo checks also get a known-bad run that must fail them.

    python3 -m unittest discover -s evals/tests
"""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

EVALS = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("odin_eval_run", EVALS / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


def apply_oracle(case_id: str, repo: Path) -> list:
    oracle = run.CASES_DIR / case_id / "oracle"
    touched = []
    for src in oracle.rglob("*"):
        rel = src.relative_to(oracle)
        if src.is_dir() or rel.name == "DELETE":
            continue
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        touched.append(str(rel))
    delete = oracle / "DELETE"
    if delete.exists():
        for rel in delete.read_text().split():
            (repo / rel).unlink()
            touched.append(rel)
    return touched


class CaseSelfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def sandbox(self, case_id: str, name: str):
        out = Path(self.tmp.name) / case_id / name
        case = run.load_case(case_id)
        return case, run.make_sandbox(case, out), out

    def test_every_case(self):
        ids = run.all_case_ids()
        self.assertGreaterEqual(len(ids), 6)
        for cid in ids:
            with self.subTest(case=cid, check="null"):
                case, repo, out = self.sandbox(cid, "null")
                g = run.grade(case, repo, out)
                self.assertFalse(g["hidden_pass"], f"{cid}: hidden tests pass on the untouched fixture")
                self.assertTrue(g["visible_pass"], f"{cid}: visible tests should pass before the change")

            with self.subTest(case=cid, check="oracle"):
                case, repo, out = self.sandbox(cid, "oracle")
                touched = apply_oracle(cid, repo)
                run.git(repo, "checkout", "-q", "-b", "feat/oracle")
                run.git(repo, "add", "--", *touched)
                run.git(repo, "commit", "-q", "-m", "oracle")
                g = run.grade(case, repo, out)
                self.assertTrue(g["hidden_pass"], f"{cid}: oracle fails hidden tests: {g['hidden_tail']}")
                self.assertTrue(g["visible_pass"], f"{cid}: oracle breaks visible tests")
                self.assertTrue(g["repo_checks_pass"], f"{cid}: oracle fails repo checks: {g['repo_checks']}")
                self.assertEqual(g["out_of_scope"], [], f"{cid}: oracle flagged out of scope")

            if run.load_case(cid).get("repo_checks"):
                with self.subTest(case=cid, check="known-bad"):
                    case, repo, out = self.sandbox(cid, "bad")
                    apply_oracle(cid, repo)
                    run.git(repo, "add", "-A")  # sweeps in the other session's work
                    run.git(repo, "commit", "-q", "-m", "bad")
                    g = run.grade(case, repo, out)
                    self.assertFalse(g["repo_checks_pass"], f"{cid}: repo checks missed a known-bad run")

    def test_pr_head_is_graded(self):
        case, repo, out = self.sandbox("bugfix-single-file", "pr")
        touched = apply_oracle("bugfix-single-file", repo)
        run.git(repo, "checkout", "-q", "-b", "feat/fix")
        run.git(repo, "add", "--", *touched)
        run.git(repo, "commit", "-q", "-m", "fix")
        run.git(repo, "checkout", "-q", "main")  # working tree is back to the bug
        (out / "gh.log").write_text("2026-11-10T00:00:00Z gh pr create --base main --head feat/fix --title x\n")
        g = run.grade(case, repo, out)
        self.assertEqual(g["graded_ref"], "feat/fix")
        self.assertTrue(g["hidden_pass"])
        self.assertTrue(g["pr_opened"])

    def test_pr_without_head_uses_branch_gh_ran_on(self):
        case, repo, out = self.sandbox("bugfix-single-file", "pr-fill")
        touched = apply_oracle("bugfix-single-file", repo)
        run.git(repo, "checkout", "-q", "-b", "feat/fill")
        run.git(repo, "add", "--", *touched)
        run.git(repo, "commit", "-q", "-m", "fix")
        run.git(repo, "checkout", "-q", "main")
        (out / "gh.log").write_text("2026-11-10T00:00:00Z [branch=feat/fill] gh pr create --fill\n")
        g = run.grade(case, repo, out)
        self.assertEqual(g["graded_ref"], "feat/fill")
        self.assertTrue(g["pr_opened"])
        self.assertTrue(g["hidden_pass"])


if __name__ == "__main__":
    unittest.main()

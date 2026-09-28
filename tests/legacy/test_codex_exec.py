import argparse
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid


SCRIPT = Path(os.environ.get(
    "CODEX_EXEC_UNDER_TEST",
    Path(__file__).resolve().parents[2] / "src" / "agent_exec" / "_legacy.py",
)).resolve()
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_TABLES = PACKAGE_ROOT / "legacy"
FAKE_CODEX_BIN = Path(__file__).with_name("oracle") / "bin"


def load_runner(worker_home):
    os.environ["CODEX_WORKER_HOME"] = str(worker_home)
    os.environ.pop("CODEX_EXEC_JOB", None)
    name = f"codex_exec_under_test_{uuid.uuid4().hex}"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_file_location(name, SCRIPT, loader=loader)
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
    return module


class CodexExecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.worker_home = self.base / "worker-home"
        self.worker_home.mkdir()
        self.table_home = self.base / "config"
        self.table_home.mkdir()
        for name in ("roles.yaml", "backends.yaml"):
            shutil.copy2(PACKAGE_TABLES / name, self.table_home / name)
        self.old_env = {name: os.environ.get(name) for name in (
            "HOME", "CODEX_WORKER_HOME", "CODEX_EXEC_JOB", "AGENT_EXEC_CONFIG",
            "AGENT_EXEC_LEDGER", "PATH",
        )}
        sandbox_home = self.base / "home"
        sandbox_home.mkdir()
        os.environ["HOME"] = str(sandbox_home)
        os.environ["CODEX_WORKER_HOME"] = str(self.worker_home)
        os.environ.pop("CODEX_EXEC_JOB", None)
        os.environ["AGENT_EXEC_CONFIG"] = str(self.table_home)
        os.environ["AGENT_EXEC_LEDGER"] = str(self.base / "ledger.tsv")
        os.environ["PATH"] = (str(FAKE_CODEX_BIN) + os.pathsep
                              + (self.old_env["PATH"] or os.defpath))
        self.runner = load_runner(self.worker_home)

    def tearDown(self):
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def test_packaged_runner_is_canonical_byte_copy_and_shim_execs_it(self):
        self.assertEqual(
            hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
            "e5708c0b463e7306dc6d93ac2b2838c2ee1d40d583c3c0426739388e6d067887",
        )
        from agent_exec import legacy
        with mock.patch.object(legacy.os, "execv") as execv:
            legacy.main(["roles"])
        execv.assert_called_once_with(
            sys.executable,
            [sys.executable, str(Path(legacy.__file__).with_name("_legacy.py")), "roles"],
        )

    def git(self, repo, *args, check=True):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], text=True, capture_output=True,
        )
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result

    def make_repo(self, name="origin"):
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "config", "user.name", "Test User")
        self.git(repo, "config", "user.email", "test@example.invalid")
        (repo / "tracked.txt").write_text("base\n")
        (repo / ".gitignore").write_text("ignored.dat\n")
        self.git(repo, "add", "tracked.txt", ".gitignore")
        self.git(repo, "commit", "-q", "-m", "initial")
        return repo

    def make_job_worktree(self, repo, job="job-one", config=None):
        values = self.runner.make_worktree(repo, job, config or self.runner.DEFAULT_CONFIG)
        wt, origin, branch = values[:3]
        jobdir = self.runner.JOBS / job
        jobdir.mkdir(parents=True, exist_ok=True)
        meta = {
            "job_id": job, "worktree": wt, "origin": origin, "branch": branch,
            "cwd": wt, "engine": "codex", "resume_of": None,
        }
        self.runner.write_json(jobdir / "meta.json", meta)
        self.runner.write_json(jobdir / "status.json", {"state": "done"})
        return jobdir, meta, values

    def start_args(self, repo, task, **overrides):
        values = dict(
            profile=None, engine="codex", default_engine="codex", worktree=True,
            cwd=str(repo), sandbox=None, yolo=False, model=None, effort=None,
            label="test", resume=None, max_seconds=10, no_schema=False,
            task_file=str(task),
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def wait_for_raw_state(self, jobdir, state="running", timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.runner.read_json(jobdir / "status.json", {})
            if status.get("state") == state:
                return status
            time.sleep(0.02)
        self.fail(f"job did not reach {state}: "
                  f"{self.runner.read_json(jobdir / 'status.json', {})}")

    def launch_test_supervisor(self, jobdir, job):
        env = {**os.environ, "CODEX_WORKER_HOME": str(self.worker_home)}
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "_run", job], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        (jobdir / "supervisor.pid").write_text(str(proc.pid))
        self.wait_for_raw_state(jobdir)
        return proc

    def test_c1_c2_dirty_snapshot_preserves_origin_and_excludes_large(self):
        repo = self.make_repo()
        gitdir = repo / ".git"
        index_before = (gitdir / "index").read_bytes()
        head_file_before = (gitdir / "HEAD").read_bytes()
        heads_before = self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
                                "refs/heads").stdout
        original_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "tracked.txt").write_text("dirty tracked\n")
        (repo / "small.txt").write_text("small\n")
        (repo / " leading-space.txt").write_text("spaced\n")
        (repo / "nested").mkdir()
        (repo / "nested" / "one.txt").write_text("one\n")
        (repo / "nested" / "two.txt").write_text("two\n")
        (repo / "ignored.dat").write_text("ignored\n")
        (repo / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
        config = {**self.runner.DEFAULT_CONFIG, "snapshot_max_file_mb": 1}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            values = self.runner.make_worktree(repo, "dirty", config)
        wt, _, branch, snapshot, dirty_count, base = values

        self.assertEqual((gitdir / "index").read_bytes(), index_before)
        self.assertEqual((gitdir / "HEAD").read_bytes(), head_file_before)
        heads_after = self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
                               "refs/heads").stdout
        self.assertEqual(set(heads_after.splitlines()) - set(heads_before.splitlines()),
                         {f"refs/heads/{branch} {snapshot}"})
        self.assertEqual((Path(wt) / "tracked.txt").read_text(), "dirty tracked\n")
        self.assertEqual((Path(wt) / "small.txt").read_text(), "small\n")
        self.assertEqual((Path(wt) / " leading-space.txt").read_text(), "spaced\n")
        self.assertEqual((Path(wt) / "nested" / "one.txt").read_text(), "one\n")
        self.assertEqual((Path(wt) / "nested" / "two.txt").read_text(), "two\n")
        self.assertFalse((Path(wt) / "ignored.dat").exists())
        self.assertFalse((Path(wt) / "large.bin").exists())
        self.assertIn("large.bin", stderr.getvalue())
        # Match the line count of plain `git status --porcelain`: nested files
        # are represented by one untracked-directory line.
        self.assertEqual(dirty_count, 5)
        self.assertEqual(base, snapshot)
        self.assertEqual(self.git(repo, "rev-parse", f"{snapshot}^").stdout.strip(),
                         original_head)
        self.assertEqual(
            self.git(repo, "rev-parse", f"refs/codex/base/{branch}").stdout.strip(),
            snapshot,
        )

    def test_c1_clean_origin_uses_head_without_snapshot_commit(self):
        repo = self.make_repo()
        head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
        values = self.runner.make_worktree(repo, "clean", self.runner.DEFAULT_CONFIG)
        _, _, branch, snapshot, dirty_count, base = values
        self.assertIsNone(snapshot)
        self.assertEqual(dirty_count, 0)
        self.assertEqual(base, head)
        self.assertEqual(self.git(repo, "rev-parse", f"refs/codex/base/{branch}").stdout.strip(),
                         head)

    def test_c1_c8_start_metadata_and_worktree_sandbox(self):
        repo = self.make_repo()
        (repo / "tracked.txt").write_text("dirty\n")
        task = self.base / "brief.md"
        task.write_text("do a harmless test\n")
        self.assertEqual(self.runner.DEFAULT_CONFIG["worktree_sandbox"], "yolo")
        self.assertEqual(self.runner.DEFAULT_CONFIG["snapshot_max_file_mb"], 50)
        default_args = self.start_args(repo, task, profile="build")
        default_out = io.StringIO()
        with mock.patch.object(self.runner, "launch_supervisor", return_value=12344):
            with contextlib.redirect_stdout(default_out):
                self.runner.cmd_start(default_args)
        default_meta = self.runner.read_json(
            self.runner.JOBS / default_out.getvalue().strip() / "meta.json", {})
        self.assertEqual(default_meta["sandbox"], "yolo")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", default_meta["cmd"])

        self.runner.write_json(self.runner.CONFIG, {"worktree_sandbox": "workspace-write"})
        args = self.start_args(repo, task)
        out = io.StringIO()
        with mock.patch.object(self.runner, "launch_supervisor", return_value=12345):
            with contextlib.redirect_stdout(out):
                self.runner.cmd_start(args)
        job = out.getvalue().strip()
        meta = self.runner.read_json(self.runner.JOBS / job / "meta.json", {})
        self.assertEqual(meta["sandbox"], "workspace-write")
        self.assertIn("--sandbox", meta["cmd"])
        self.assertIn("workspace-write", meta["cmd"])
        self.assertTrue(meta["snapshot_commit"])
        self.assertEqual(meta["origin_dirty_at_start"], 1)

        # --yolo is explicit and must beat a read-only profile.
        args = self.start_args(repo, task, profile="scout", yolo=True)
        out = io.StringIO()
        with mock.patch.object(self.runner, "launch_supervisor", return_value=12346):
            with contextlib.redirect_stdout(out):
                self.runner.cmd_start(args)
        explicit = self.runner.read_json(
            self.runner.JOBS / out.getvalue().strip() / "meta.json", {})
        self.assertEqual(explicit["sandbox"], "yolo")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", explicit["cmd"])

    def test_c3_result_commit_for_done_failed_and_timeout(self):
        cases = [
            ("done", ["sh", "-c", "printf done > result.txt"], 3, "done"),
            ("failed", ["sh", "-c", "printf failed > result.txt; exit 7"], 3, "failed"),
            ("timeout", ["sh", "-c", "printf timeout > result.txt; sleep 5"], 0.1,
             "timeout"),
        ]
        for index, (name, command, timeout, expected_state) in enumerate(cases):
            with self.subTest(name=name):
                repo = self.make_repo(f"origin-{index}")
                jobdir, meta, _ = self.make_job_worktree(repo, f"job-{name}")
                meta.update({"cmd": command, "stdin_brief": False, "max_seconds": timeout})
                self.runner.write_json(jobdir / "meta.json", meta)
                self.runner.cmd_run(argparse.Namespace(job=f"job-{name}"))
                status = self.runner.read_json(jobdir / "status.json", {})
                self.assertEqual(status["state"], expected_state)
                self.assertTrue(status["result_commit"])
                self.assertNotIn("result_commit_error", status)
                self.assertEqual(self.git(meta["worktree"], "status", "--porcelain").stdout, "")
                self.assertTrue((Path(meta["worktree"]) / "result.txt").exists())
                author = self.git(meta["worktree"], "show", "-s", "--format=%an <%ae>").stdout.strip()
                self.assertEqual(author, "codex-exec <codex-exec@local>")

    def test_c3_no_changes_records_null_result_commit(self):
        repo = self.make_repo()
        jobdir, meta, _ = self.make_job_worktree(repo, "no-changes")
        meta.update({"cmd": ["sh", "-c", "true"], "stdin_brief": False, "max_seconds": 2})
        self.runner.write_json(jobdir / "meta.json", meta)
        self.runner.cmd_run(argparse.Namespace(job="no-changes"))
        status = self.runner.read_json(jobdir / "status.json", {})
        self.assertEqual(status["state"], "done")
        self.assertIsNone(status["result_commit"])
        self.assertNotIn("result_commit_error", status)

    def test_c3_commit_error_is_recorded_without_changing_state(self):
        repo = self.make_repo()
        jobdir, meta, _ = self.make_job_worktree(repo, "commit-error")
        meta.update({"cmd": ["sh", "-c", "true"], "stdin_brief": False, "max_seconds": 2})
        self.runner.write_json(jobdir / "meta.json", meta)
        with mock.patch.object(self.runner, "commit_worktree_result",
                               return_value=(None, "simulated commit failure")):
            self.runner.cmd_run(argparse.Namespace(job="commit-error"))
        status = self.runner.read_json(jobdir / "status.json", {})
        self.assertEqual(status["state"], "done")
        self.assertIsNone(status["result_commit"])
        self.assertEqual(status["result_commit_error"], "simulated commit failure")

    def test_c4_patch_is_cumulative_and_legacy_falls_back_to_head(self):
        repo = self.make_repo()
        jobdir, meta, _ = self.make_job_worktree(repo, "cumulative")
        wt = Path(meta["worktree"])
        (wt / "first.txt").write_text("first\n")
        commit, error = self.runner.commit_worktree_result(meta)
        self.assertIsNone(error)
        patch, _ = self.runner.make_patch(jobdir, meta)
        self.assertIn("first.txt", patch.read_text())

        self.git(repo, "update-ref", f"refs/codex/adopted/{meta['branch']}", commit)
        (wt / "second.txt").write_text("second\n")
        patch, _ = self.runner.make_patch(jobdir, meta)
        self.assertIn("second.txt", patch.read_text())
        self.assertNotIn("first.txt", patch.read_text())

        self.runner.commit_worktree_result(meta)
        self.git(repo, "update-ref", "-d", f"refs/codex/adopted/{meta['branch']}")
        self.git(repo, "update-ref", "-d", f"refs/codex/base/{meta['branch']}")
        (wt / "legacy.txt").write_text("legacy\n")
        patch, _ = self.runner.make_patch(jobdir, meta)
        self.assertIn("legacy.txt", patch.read_text())
        self.assertNotIn("second.txt", patch.read_text())

    def test_c1_c4_c5_dirty_snapshot_adopts_only_worker_delta(self):
        repo = self.make_repo()
        index_before = (repo / ".git" / "index").read_bytes()
        (repo / "tracked.txt").write_text("origin dirty\n")
        (repo / "origin-untracked.txt").write_text("origin untracked\n")
        jobdir, meta, _ = self.make_job_worktree(repo, "dirty-adopt")
        wt = Path(meta["worktree"])
        (wt / "tracked.txt").write_text("origin dirty\nworker delta\n")
        (wt / "origin-untracked.txt").write_text("origin untracked\nworker delta\n")
        self.runner.commit_worktree_result(meta)
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_diff(argparse.Namespace(job="dirty-adopt"))
            self.runner.cmd_adopt(argparse.Namespace(job="dirty-adopt", force=False))
        self.assertEqual((repo / "tracked.txt").read_text(),
                         "origin dirty\nworker delta\n")
        self.assertEqual((repo / "origin-untracked.txt").read_text(),
                         "origin untracked\nworker delta\n")
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        self.assertIn("worker delta", (jobdir / "changes.patch").read_text())

    def test_c5_diff_gate_staleness_success_and_adopt_receipt(self):
        repo = self.make_repo()
        jobdir, meta, values = self.make_job_worktree(repo, "adopt")
        wt = Path(meta["worktree"])
        (wt / "one.txt").write_text("one\n")
        self.runner.commit_worktree_result(meta)
        with self.assertRaises(SystemExit) as caught:
            self.runner.cmd_adopt(argparse.Namespace(job="adopt", force=False))
        self.assertIn("codex-exec diff adopt", str(caught.exception))

        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            self.runner.cmd_diff(argparse.Namespace(job="adopt"))
        patch = (jobdir / "changes.patch").read_bytes()
        self.assertEqual((jobdir / "diff-viewed").read_text(),
                         hashlib.sha256(patch).hexdigest())
        self.assertEqual(shown.getvalue().encode(), patch)

        (wt / "two.txt").write_text("two\n")
        self.runner.commit_worktree_result(meta)
        with self.assertRaises(SystemExit):
            self.runner.cmd_adopt(argparse.Namespace(job="adopt", force=False))
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_diff(argparse.Namespace(job="adopt"))
            self.runner.cmd_adopt(argparse.Namespace(job="adopt", force=False))
        self.assertEqual((repo / "one.txt").read_text(), "one\n")
        self.assertEqual((repo / "two.txt").read_text(), "two\n")
        receipt = self.runner.read_json(jobdir / "adopt.json", {})
        head = self.git(wt, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(receipt["result"], "applied")
        self.assertEqual(receipt["range"], f"{values[5]}..{head}")
        self.assertTrue(isinstance(receipt["at"], float))
        self.assertTrue((jobdir / "adopted").exists())
        self.assertEqual(
            self.git(repo, "rev-parse", f"refs/codex/adopted/{meta['branch']}").stdout.strip(),
            head,
        )

    def test_c5_force_bypass_and_conflict_receipt(self):
        repo = self.make_repo()
        jobdir, meta, _ = self.make_job_worktree(repo, "force")
        (Path(meta["worktree"]) / "forced.txt").write_text("forced\n")
        self.runner.commit_worktree_result(meta)
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_adopt(argparse.Namespace(job="force", force=True))
        self.assertTrue((repo / "forced.txt").exists())

        conflict_dir, conflict_meta, _ = self.make_job_worktree(repo, "conflict")
        conflict_wt = Path(conflict_meta["worktree"])
        (conflict_wt / "tracked.txt").write_text("worker\n")
        self.runner.commit_worktree_result(conflict_meta)
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_diff(argparse.Namespace(job="conflict"))
        (repo / "tracked.txt").write_text("origin diverged\n")
        with self.assertRaises(SystemExit):
            self.runner.cmd_adopt(argparse.Namespace(job="conflict", force=False))
        receipt = self.runner.read_json(conflict_dir / "adopt.json", {})
        self.assertEqual(receipt["result"], "conflict")
        self.assertTrue(receipt["stderr"])

    def test_c5_adopt_requires_terminal_job_and_rolls_back_ref_failure(self):
        repo = self.make_repo()
        jobdir, meta, _ = self.make_job_worktree(repo, "ref-failure")
        (Path(meta["worktree"]) / "rollback.txt").write_text("rollback\n")
        self.runner.commit_worktree_result(meta)
        self.runner.write_json(jobdir / "status.json", {"state": "running"})
        (jobdir / "supervisor.pid").write_text(str(os.getpid()))
        with self.assertRaises(SystemExit) as caught:
            self.runner.cmd_adopt(argparse.Namespace(job="ref-failure", force=True))
        self.assertIn("has not finished", str(caught.exception))

        self.runner.write_json(jobdir / "status.json", {"state": "done"})
        real_git = self.runner._git
        adopted_ref = f"refs/codex/adopted/{meta['branch']}"

        def fail_adopted_ref(root, *args, env=None):
            if args[:2] == ("update-ref", adopted_ref):
                return subprocess.CompletedProcess(
                    ["git"], 1, stdout="", stderr="simulated ref failure")
            return real_git(root, *args, env=env)

        with mock.patch.object(self.runner, "_git", side_effect=fail_adopted_ref):
            with self.assertRaises(SystemExit) as caught:
                self.runner.cmd_adopt(argparse.Namespace(job="ref-failure", force=True))
        self.assertIn("applied patch was reversed", str(caught.exception))
        self.assertFalse((repo / "rollback.txt").exists())
        receipt = self.runner.read_json(jobdir / "adopt.json", {})
        self.assertEqual(receipt["result"], "conflict")
        self.assertIn("simulated ref failure", receipt["stderr"])

    def test_c6_drop_removes_worktree_branch_and_refs(self):
        repo = self.make_repo()
        jobdir, meta, _ = self.make_job_worktree(repo, "drop")
        head = self.git(meta["worktree"], "rev-parse", "HEAD").stdout.strip()
        self.git(repo, "update-ref", f"refs/codex/adopted/{meta['branch']}", head)
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_drop(argparse.Namespace(job="drop", force=True))
        self.assertFalse(Path(meta["worktree"]).exists())
        for ref in (f"refs/heads/{meta['branch']}", f"refs/codex/base/{meta['branch']}",
                    f"refs/codex/adopted/{meta['branch']}"):
            self.assertNotEqual(self.git(repo, "rev-parse", "--verify", "--quiet", ref,
                                         check=False).returncode, 0)
        self.assertTrue((jobdir / "changes.patch").exists())

    def test_c7_directive_parsing_and_misplacement_detection(self):
        directives, body = self.runner.parse_directives(
            "   cwd: /tmp/example\n\n Tier: model medium\n  WORKTREE : yes\n\n# Body\ntext")
        self.assertEqual(directives, {
            "CWD": "/tmp/example", "TIER": "model medium", "WORKTREE": "yes",
        })
        self.assertEqual(body, "# Body\ntext")

        for brief in ("\nCWD: /lost\nbody", "# title\nworktree: yes\nbody"):
            _, misplaced_body = self.runner.parse_directives(brief)
            with self.assertRaises(SystemExit) as caught:
                self.runner.validate_body_directives(misplaced_body)
            self.assertIn("directives must be at the very top", str(caught.exception))
            self.assertIn("CWD" if "CWD" in brief else "worktree", str(caught.exception))

        misplaced_task = self.base / "misplaced.md"
        misplaced_task.write_text("# title\nCWD: /lost\nbody\n")
        with self.assertRaises(SystemExit) as caught:
            self.runner.cmd_dispatch(argparse.Namespace(
                task_file=str(misplaced_task), profile="build",
                default_engine="codex", timeout=0))
        self.assertIn("CWD: /lost", str(caught.exception))

        late = "LABEL: ok\n" + "\n".join(["body"] * 31 + ["cwd: /late"])
        task = self.base / "late.md"
        task.write_text(late)
        captured = {}

        def fake_start(namespace):
            captured["namespace"] = namespace
            print("fake-job")

        stderr = io.StringIO()
        with mock.patch.object(self.runner, "cmd_start", side_effect=fake_start), \
                mock.patch.object(self.runner, "_digest_or_running", return_value=0), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            result = self.runner.cmd_dispatch(argparse.Namespace(
                task_file=str(task), profile="build", default_engine="codex", timeout=0))
        self.assertEqual(result, 0)
        self.assertIn("cwd: /late", stderr.getvalue())
        self.assertEqual(captured["namespace"].label, "ok")

    def test_c9_adopt_lock_is_scoped_to_worker_root(self):
        repo = self.make_repo()
        _, meta, _ = self.make_job_worktree(repo, "locked")
        (Path(meta["worktree"]) / "locked.txt").write_text("locked\n")
        self.runner.commit_worktree_result(meta)
        (self.runner.ROOT / "ORCHESTRATOR_ADOPT_LOCK").write_text("secret")
        os.environ.pop("CODEX_ADOPT_TOKEN", None)
        with self.assertRaises(SystemExit) as caught:
            self.runner.cmd_adopt(argparse.Namespace(job="locked", force=True))
        self.assertIn("only the supervising orchestrator", str(caught.exception))

    def test_b1_start_records_version_config_and_brief_hashes(self):
        self.assertTrue(hasattr(self.runner, "engine_version"))
        self.runner.ENGINE_VERSION_CACHE.clear()
        fake_version = subprocess.CompletedProcess(
            ["codex", "--version"], 0, stdout="codex-cli cached\nsecond line\n",
            stderr="")
        with mock.patch.object(self.runner.subprocess, "run",
                               return_value=fake_version) as version_run:
            self.assertEqual(self.runner.engine_version("codex"), "codex-cli cached")
            self.assertEqual(self.runner.engine_version("codex"), "codex-cli cached")
        self.assertEqual(version_run.call_count, 1)
        repo = self.make_repo()
        task = self.base / "brief.md"
        brief = "preserve these exact bytes\n"
        task.write_text(brief)
        args = self.start_args(repo, task, worktree=False)
        out = io.StringIO()
        with mock.patch.object(self.runner, "engine_version",
                               return_value="codex-cli test-version", create=True), \
                mock.patch.object(self.runner, "launch_supervisor",
                                  return_value=os.getpid()), \
                contextlib.redirect_stdout(out):
            self.runner.cmd_start(args)
        meta = self.runner.read_json(
            self.runner.JOBS / out.getvalue().strip() / "meta.json", {})
        self.assertEqual(meta.get("runner_sha"),
                         hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:12])
        self.assertEqual(meta.get("engine_version"), "codex-cli test-version")
        self.assertEqual(meta.get("brief_sha"),
                         hashlib.sha256(brief.encode()).hexdigest()[:12])
        self.assertEqual(meta.get("config_snapshot"), {
            "default_sandbox": "yolo", "worktree_sandbox": "yolo",
            "snapshot_max_file_mb": 50,
        })

    def test_b2_sigkill_reconciliation_and_sigterm_handler_commit_results(self):
        processes = []
        try:
            repo = self.make_repo("lost-origin")
            jobdir, meta, _ = self.make_job_worktree(repo, "lost-sigkill")
            meta.update({
                "cmd": ["sh", "-c", "printf recovered > killed.txt; sleep 30"],
                "stdin_brief": False, "max_seconds": 60,
            })
            self.runner.write_json(jobdir / "meta.json", meta)
            self.runner.write_json(jobdir / "status.json", {
                "state": "starting", "started_at": time.time(),
            })
            supervisor = self.launch_test_supervisor(jobdir, "lost-sigkill")
            processes.append((supervisor, jobdir))
            deadline = time.time() + 5
            while not (Path(meta["worktree"]) / "killed.txt").exists() \
                    and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue((Path(meta["worktree"]) / "killed.txt").exists())
            supervisor.kill()
            supervisor.wait(timeout=5)
            status = self.runner.status_of(jobdir)
            self.assertEqual(status["state"], "lost")
            self.assertIn(f"supervisor pid {supervisor.pid} not alive",
                          status["lost_reason"])
            self.assertTrue(status["result_commit"])
            self.assertEqual(self.git(meta["worktree"], "status", "--porcelain").stdout, "")

            repo = self.make_repo("term-origin")
            termdir, termmeta, _ = self.make_job_worktree(repo, "lost-sigterm")
            termmeta.update({
                "cmd": ["sh", "-c", "printf recovered > term.txt; sleep 30"],
                "stdin_brief": False, "max_seconds": 60,
            })
            self.runner.write_json(termdir / "meta.json", termmeta)
            self.runner.write_json(termdir / "status.json", {
                "state": "starting", "started_at": time.time(),
            })
            supervisor = self.launch_test_supervisor(termdir, "lost-sigterm")
            processes.append((supervisor, termdir))
            deadline = time.time() + 5
            while not (Path(termmeta["worktree"]) / "term.txt").exists() \
                    and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue((Path(termmeta["worktree"]) / "term.txt").exists())
            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=8)
            status = self.runner.read_json(termdir / "status.json", {})
            self.assertEqual(status["state"], "lost")
            self.assertEqual(status["lost_reason"], f"signal {signal.SIGTERM}")
            self.assertTrue(status["result_commit"])
        finally:
            for supervisor, jobdir in processes:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait(timeout=5)
                pid = self.runner.read_json(jobdir / "status.json", {}).get("pid")
                if pid:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except OSError:
                        pass

    def test_b3_adopt_drop_and_gc_cleanup_legacy_refs(self):
        repo = self.make_repo()
        jobdir, meta, _ = self.make_job_worktree(repo, "adopt-drop")
        (Path(meta["worktree"]) / "adopted.txt").write_text("applied\n")
        self.runner.commit_worktree_result(meta)
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_adopt(argparse.Namespace(
                job="adopt-drop", force=True, drop=True))
        self.assertFalse(Path(meta["worktree"]).exists())
        self.assertTrue((jobdir / "changes.patch").exists())

        olddir, oldmeta, _ = self.make_job_worktree(repo, "gc-legacy")
        (Path(oldmeta["worktree"]) / "legacy.txt").write_text("legacy\n")
        self.runner.commit_worktree_result(oldmeta)
        for kind in ("base", "adopted"):
            self.git(repo, "update-ref", "-d", f"refs/codex/{kind}/{oldmeta['branch']}")
        (olddir / "adopted").write_text(str(time.time() - 100000))
        self.runner.write_json(olddir / "status.json", {
            "state": "done", "ended_at": time.time() - 100000,
        })
        dry = io.StringIO()
        with contextlib.redirect_stdout(dry):
            self.runner.cmd_gc(argparse.Namespace(
                older_than=24, dry_run=True, force=False))
        self.assertIn("would drop gc-legacy", dry.getvalue())
        self.assertTrue(Path(oldmeta["worktree"]).exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_gc(argparse.Namespace(
                older_than=24, dry_run=False, force=False))
        self.assertFalse(Path(oldmeta["worktree"]).exists())
        self.assertTrue((olddir / "changes.patch").exists())

        unadopted, unmet, _ = self.make_job_worktree(repo, "gc-unadopted")
        (Path(unmet["worktree"]) / "discard.txt").write_text("discard\n")
        self.runner.write_json(unadopted / "status.json", {
            "state": "done", "ended_at": time.time() - 100000,
        })
        skipped = io.StringIO()
        with contextlib.redirect_stdout(skipped):
            self.runner.cmd_gc(argparse.Namespace(
                older_than=24, dry_run=False, force=False))
        self.assertIn("skip gc-unadopted: unadopted", skipped.getvalue())
        self.assertTrue(Path(unmet["worktree"]).exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_gc(argparse.Namespace(
                older_than=24, dry_run=False, force=True))
        self.assertFalse(Path(unmet["worktree"]).exists())

    def test_b4_verdict_and_read_only_grouped_report(self):
        self.assertTrue(hasattr(self.runner, "cmd_verdict"))
        self.assertTrue(hasattr(self.runner, "cmd_report"))
        now = time.time()
        done = self.runner.JOBS / "report-done"
        done.mkdir(parents=True)
        self.runner.write_json(done / "meta.json", {
            "profile": "build", "runner_sha": "abc", "engine_version": "v1",
            "sandbox": "yolo", "worktree": "/tmp/worktree", "snapshot_commit": "sha",
            "resume_of": None,
        })
        self.runner.write_json(done / "status.json", {
            "state": "done", "started_at": now, "ended_at": now + 10,
            "duration_sec": 10,
        })
        (done / "events.jsonl").write_text("\n".join([
            json.dumps({"type": "item.completed",
                        "item": {"type": "command_execution"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "command_execution"}}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 40}}),
        ]))
        self.runner.write_json(done / "adopt.json", {
            "result": "applied", "at": now + 15,
        })
        (done / "diff-viewed").write_text("hash")
        with contextlib.redirect_stdout(io.StringIO()):
            self.runner.cmd_verdict(argparse.Namespace(
                job="report-done", verdict="PASS", fail_class=None, rework=1,
                review_min=2.0, solo_estimate_min=15.0, notes="effective"))

        stale = self.runner.JOBS / "report-stale"
        stale.mkdir()
        self.runner.write_json(stale / "meta.json", {
            "profile": "build", "runner_sha": "abc", "engine_version": "v1",
            "sandbox": "read-only", "worktree": None, "snapshot_commit": None,
            "resume_of": "session",
        })
        self.runner.write_json(stale / "status.json", {
            "state": "running", "started_at": now,
        })
        original_status = (stale / "status.json").read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.runner.cmd_report(argparse.Namespace(
                since=str(time.strftime("%Y-%m-%d", time.localtime(now))), until=None,
                by="profile", json=True, tsv=False))
        rows = json.loads(out.getvalue())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["group"], row["n"], row["done"], row["lost"]),
                         ("build", 2, 1, 1))
        self.assertEqual(row["median_command_execution"], 1)
        self.assertEqual(row["median_input_tokens"], 50)
        self.assertEqual(row["snapshot_pct"], 50.0)
        self.assertEqual(row["diff_before_adopt_pct"], 100.0)
        self.assertEqual((row["PASS"], row["none"], row["rework_sum"]), (1, 1, 1))
        self.assertEqual((stale / "status.json").read_bytes(), original_status)
        verdict = self.runner.read_json(done / "verdict.json", {})
        self.assertEqual(verdict["solo_estimate_min"], 15.0)
        with self.assertRaises(SystemExit):
            self.runner.cmd_verdict(argparse.Namespace(
                job="missing", verdict="FAIL", fail_class=None, rework=0,
                review_min=None, solo_estimate_min=None, notes=None))

    def test_b5_duplicate_dispatch_warning_and_suppression(self):
        repo = self.make_repo()
        task = self.base / "duplicate.md"
        task.write_text("same brief\n")

        def start(allow):
            out, err = io.StringIO(), io.StringIO()
            args = self.start_args(repo, task, worktree=False,
                                   allow_duplicate=allow)
            with mock.patch.object(self.runner, "engine_version", return_value="v",
                                   create=True), \
                    mock.patch.object(self.runner, "launch_supervisor",
                                      return_value=os.getpid()), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.runner.cmd_start(args)
            return out.getvalue().strip(), err.getvalue()

        first, _ = start(False)
        second, warning = start(False)
        self.assertIn(
            f"codex-exec: WARNING duplicate dispatch: job {first} is already running "
            f"this brief in {repo}", warning)
        _, suppressed = start(True)
        self.assertNotIn("duplicate dispatch", suppressed)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()

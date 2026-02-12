"""Integration tests for the multi-agent orchestrator workflow.

All external I/O (Claude CLI, cargo, git) is mocked — no real subprocesses.
Each test uses an isolated temp DB to avoid side effects.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import db
from config import Config
from orchestrator import Orchestrator, format_dashboard
from schemas import (
    AgentResult,
    AgentStatus,
    AgentTask,
    ExecutionPlan,
    TaskStatus,
    TestBaseline,
    TestLevel,
    TestResult,
)


def _make_config(**overrides) -> Config:
    defaults = dict(
        telegram_token="fake-token",
        allowed_chat_id=123,
        build_budget_usd=15.0,
        build_max_retries=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _ok_agent_result(**kw) -> AgentResult:
    """Successful agent result with token counts."""
    defaults = dict(
        status=AgentStatus.SUCCESS,
        files_modified=["src/main.rs"],
        tests_added=1,
        raw_output="## RESULT\nSTATUS: success",
        input_tokens=1000,
        output_tokens=500,
        duration_seconds=10.0,
    )
    defaults.update(kw)
    return AgentResult(**defaults)


def _fail_agent_result(**kw) -> AgentResult:
    """Failed agent result."""
    defaults = dict(
        status=AgentStatus.FAILED,
        errors=["compilation error"],
        input_tokens=800,
        output_tokens=300,
        duration_seconds=5.0,
    )
    defaults.update(kw)
    return AgentResult(**defaults)


def _ok_test_result(level=TestLevel.FAST) -> TestResult:
    return TestResult(
        level=level, passed=True, total_tests=10, passed_tests=10,
        duration_seconds=2.0,
    )


def _fail_test_result(level=TestLevel.FAST) -> TestResult:
    return TestResult(
        level=level, passed=False, total_tests=10, passed_tests=8,
        failed_tests=["test_foo", "test_bar"], duration_seconds=3.0,
    )


class _IsolatedDBMixin:
    """Mixin that redirects db.DB_PATH to a temp file for test isolation."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_patcher = patch.object(db, "DB_PATH", self.tmp_db.name)
        self.db_patcher.start()
        db._db_initialized.discard(self.tmp_db.name)

    def tearDown(self):
        self.db_patcher.stop()
        db._db_initialized.discard(self.tmp_db.name)
        os.unlink(self.tmp_db.name)


def _patch_externals(
    planner_output: str = "",
    agent_results: list[AgentResult] | None = None,
    test_results: list[TestResult] | None = None,
    baseline: TestBaseline | None = None,
    merge_conflicts: list[str] | None = None,
):
    """Return a dict of patchers for all external dependencies.

    This mocks:
      - AgentRunner.run (planner + agents)
      - capture_baseline
      - run_test_level
      - WorktreeManager (create, commit, merge, cleanup, remove)
    """
    if baseline is None:
        baseline = TestBaseline(total_tests=10, passing_tests=10, snapshot_hash="abc123")
    if agent_results is None:
        agent_results = [_ok_agent_result()]
    if test_results is None:
        test_results = [_ok_test_result(TestLevel.FAST), _ok_test_result(TestLevel.NORMAL)]
    if merge_conflicts is None:
        merge_conflicts = []

    # Build planner JSON output
    if not planner_output:
        plan_dict = {
            "agents": [{"role": "rust-backend", "description": "Implement the feature"}],
        }
        planner_output = json.dumps(plan_dict)

    planner_result = _ok_agent_result(raw_output=planner_output)

    # AgentRunner.run: first call = planner, subsequent calls = agents
    run_side_effects = [planner_result] + list(agent_results)
    mock_runner_run = AsyncMock(side_effect=run_side_effects)

    # Test gate
    test_iter = iter(test_results)
    async def _mock_run_test_level(level, path, **kw):
        return next(test_iter)
    mock_run_test = AsyncMock(side_effect=_mock_run_test_level)

    # Worktree mocks
    mock_wt_create = AsyncMock(return_value="/tmp/fake-worktree")
    mock_wt_commit = AsyncMock(return_value="abc1234")
    mock_wt_merge = AsyncMock(return_value=merge_conflicts)
    mock_wt_cleanup = AsyncMock()
    mock_wt_remove = AsyncMock()

    return {
        "runner_run": patch("orchestrator.AgentRunner.run", mock_runner_run),
        "capture_baseline": patch("orchestrator.capture_baseline", AsyncMock(return_value=baseline)),
        "run_test_level": patch("orchestrator.run_test_level", mock_run_test),
        "wt_create": patch("orchestrator.WorktreeManager.create", mock_wt_create),
        "wt_commit": patch("orchestrator.WorktreeManager.commit_agent_work", mock_wt_commit),
        "wt_merge": patch("orchestrator.WorktreeManager.merge_to_integration", mock_wt_merge),
        "wt_cleanup": patch("orchestrator.WorktreeManager.cleanup", mock_wt_cleanup),
        "wt_remove": patch("orchestrator.WorktreeManager.remove", mock_wt_remove),
        # Expose mocks for assertions
        "_mocks": {
            "runner_run": mock_runner_run,
            "run_test_level": mock_run_test,
            "wt_create": mock_wt_create,
            "wt_merge": mock_wt_merge,
            "wt_cleanup": mock_wt_cleanup,
            "wt_remove": mock_wt_remove,
        },
    }


class TestOrchestratorHappyPath(_IsolatedDBMixin, unittest.TestCase):
    """Test 1: Full workflow PENDING → DONE."""

    def test_happy_path(self):
        config = _make_config()
        patches = _patch_externals()

        with (
            patches["runner_run"],
            patches["capture_baseline"],
            patches["run_test_level"],
            patches["wt_create"],
            patches["wt_commit"],
            patches["wt_merge"],
            patches["wt_cleanup"],
            patches["wt_remove"],
        ):
            orch = Orchestrator(config, "/fake/project", "Add login endpoint")
            dashboard = asyncio.run(orch.execute())

        self.assertEqual(dashboard.status, TaskStatus.DONE)
        self.assertEqual(len(dashboard.agents), 1)
        self.assertEqual(dashboard.agents[0].role, "rust-backend")
        self.assertEqual(dashboard.agents[0].status, "done")
        self.assertGreater(dashboard.total_cost_usd, 0)

        # Verify DB task was created and completed
        task = db.get_task(orch.task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "done")

        # Worktree cleanup was called
        patches["_mocks"]["wt_cleanup"].assert_awaited_once()


class TestOrchestratorRetrySuccess(_IsolatedDBMixin, unittest.TestCase):
    """Test 2: Agent fails tests on first attempt, succeeds on second."""

    def test_retry_then_success(self):
        config = _make_config(build_max_retries=3)

        # First agent attempt: success but tests fail
        # Second agent attempt: success and tests pass
        agent_results = [_ok_agent_result(), _ok_agent_result()]
        test_results = [
            _fail_test_result(TestLevel.FAST),  # attempt 1 — test gate fails
            _ok_test_result(TestLevel.FAST),     # attempt 2 — test gate passes
            _ok_test_result(TestLevel.NORMAL),   # level 2 integration test
        ]

        patches = _patch_externals(
            agent_results=agent_results,
            test_results=test_results,
        )

        with (
            patches["runner_run"],
            patches["capture_baseline"],
            patches["run_test_level"],
            patches["wt_create"],
            patches["wt_commit"],
            patches["wt_merge"],
            patches["wt_cleanup"],
            patches["wt_remove"],
        ):
            orch = Orchestrator(config, "/fake/project", "Fix auth bug")
            dashboard = asyncio.run(orch.execute())

        self.assertEqual(dashboard.status, TaskStatus.DONE)
        # AgentRunner.run called: 1 planner + 2 agent attempts = 3
        self.assertEqual(patches["_mocks"]["runner_run"].await_count, 3)


class TestOrchestratorBudgetExceeded(_IsolatedDBMixin, unittest.TestCase):
    """Test 3: Budget exceeded stops execution early."""

    def test_budget_exceeded(self):
        # Very low budget so the planner's cost already exceeds it
        config = _make_config(build_budget_usd=0.001)

        plan_dict = {
            "agents": [
                {"role": "rust-backend", "description": "Task 1"},
                {"role": "rust-frontend", "description": "Task 2"},
            ],
        }
        planner_output = json.dumps(plan_dict)

        # Planner result with high token count to blow the budget
        planner_result = _ok_agent_result(
            input_tokens=100_000, output_tokens=50_000,
            raw_output=planner_output,
        )

        agent_results = [_ok_agent_result(), _ok_agent_result()]
        test_results = [
            _ok_test_result(TestLevel.FAST),
            _ok_test_result(TestLevel.NORMAL),
        ]

        # Override planner in the run side effects
        run_side_effects = [planner_result] + agent_results
        mock_runner_run = AsyncMock(side_effect=run_side_effects)

        patches = _patch_externals(
            planner_output=planner_output,
            agent_results=agent_results,
            test_results=test_results,
        )

        with (
            patch("orchestrator.AgentRunner.run", mock_runner_run),
            patches["capture_baseline"],
            patches["run_test_level"],
            patches["wt_create"],
            patches["wt_commit"],
            patches["wt_merge"],
            patches["wt_cleanup"],
            patches["wt_remove"],
        ):
            orch = Orchestrator(config, "/fake/project", "Add feature")
            dashboard = asyncio.run(orch.execute())

        # Planner ran, but agents should have been skipped due to budget
        # The planner always runs (1 call), agents skipped
        self.assertEqual(mock_runner_run.await_count, 1)
        # Status should still be DONE (no agents to fail) or at least not ERROR
        # since budget exceeded just skips the loop, doesn't raise
        self.assertIn(dashboard.status, [TaskStatus.DONE, TaskStatus.ERROR])


class TestOrchestratorMaxRetriesExhausted(_IsolatedDBMixin, unittest.TestCase):
    """Test 4: Agent fails all retries → ERROR."""

    def test_max_retries_error(self):
        config = _make_config(build_max_retries=2)

        # Agent always fails
        agent_results = [_ok_agent_result(), _ok_agent_result()]
        # Tests always fail
        test_results = [
            _fail_test_result(TestLevel.FAST),
            _fail_test_result(TestLevel.FAST),
        ]

        patches = _patch_externals(
            agent_results=agent_results,
            test_results=test_results,
        )

        with (
            patches["runner_run"],
            patches["capture_baseline"],
            patches["run_test_level"],
            patches["wt_create"],
            patches["wt_commit"],
            patches["wt_merge"],
            patches["wt_cleanup"],
            patches["wt_remove"],
        ):
            orch = Orchestrator(config, "/fake/project", "Broken feature")
            dashboard = asyncio.run(orch.execute())

        self.assertEqual(dashboard.status, TaskStatus.ERROR)

        # Verify DB task has error status
        task = db.get_task(orch.task_id)
        self.assertEqual(task["status"], "error")
        self.assertIn("failed after", task["error"])

        # Worktree remove should have been called (cleanup on crash)
        patches["_mocks"]["wt_remove"].assert_awaited()


class TestOrchestratorMergeConflicts(_IsolatedDBMixin, unittest.TestCase):
    """Test 5: Merge conflicts → ERROR."""

    def test_merge_conflicts_error(self):
        config = _make_config()

        patches = _patch_externals(
            merge_conflicts=["rust-backend: CONFLICT in src/main.rs"],
        )

        with (
            patches["runner_run"],
            patches["capture_baseline"],
            patches["run_test_level"],
            patches["wt_create"],
            patches["wt_commit"],
            patches["wt_merge"],
            patches["wt_cleanup"],
            patches["wt_remove"],
        ):
            orch = Orchestrator(config, "/fake/project", "Conflicting changes")
            dashboard = asyncio.run(orch.execute())

        self.assertEqual(dashboard.status, TaskStatus.ERROR)

        task = db.get_task(orch.task_id)
        self.assertIn("Merge conflicts", task["error"])


class TestOrchestratorPlannerFallback(_IsolatedDBMixin, unittest.TestCase):
    """Test 6: Invalid planner JSON → fallback to default agent."""

    def test_planner_invalid_json_fallback(self):
        config = _make_config()

        # Planner returns garbage instead of JSON
        planner_result = _ok_agent_result(raw_output="I don't know what to do, sorry!")
        agent_result = _ok_agent_result()
        run_side_effects = [planner_result, agent_result]
        mock_runner_run = AsyncMock(side_effect=run_side_effects)

        test_results = [
            _ok_test_result(TestLevel.FAST),
            _ok_test_result(TestLevel.NORMAL),
        ]

        patches = _patch_externals(test_results=test_results)

        with (
            patch("orchestrator.AgentRunner.run", mock_runner_run),
            patches["capture_baseline"],
            patches["run_test_level"],
            patches["wt_create"],
            patches["wt_commit"],
            patches["wt_merge"],
            patches["wt_cleanup"],
            patches["wt_remove"],
        ):
            orch = Orchestrator(config, "/fake/project", "Do something")
            dashboard = asyncio.run(orch.execute())

        self.assertEqual(dashboard.status, TaskStatus.DONE)
        # Fallback creates a single rust-backend agent
        self.assertEqual(len(dashboard.agents), 1)
        self.assertEqual(dashboard.agents[0].role, "rust-backend")


class TestFormatDashboard(unittest.TestCase):
    """Test 7: Dashboard formatting."""

    def test_format_dashboard_output(self):
        from schemas import AgentDashboardEntry, TaskDashboard

        dashboard = TaskDashboard(
            task_id="test-123",
            description="Add authentication to the API",
            status=TaskStatus.EXECUTING,
            agents=[
                AgentDashboardEntry(
                    role="rust-backend", status="done",
                    cost_usd=1.50, duration_seconds=45, tokens=15000,
                ),
                AgentDashboardEntry(role="rust-frontend", status="running"),
                AgentDashboardEntry(role="tester-cargo", status="waiting"),
            ],
            total_cost_usd=1.50,
            budget_usd=15.0,
            baseline_tests=42,
            regressions=0,
        )

        output = format_dashboard(dashboard)

        # Verify structure
        self.assertIn("Add authentication", output)
        self.assertIn("$1.50/$15.00", output)
        self.assertIn("rust-backend", output)
        self.assertIn("rust-frontend", output)
        self.assertIn("tester-cargo", output)
        self.assertIn("42 baseline", output)
        self.assertIn("0 regressions", output)
        # Done agent has cost details
        self.assertIn("$1.50", output)
        self.assertIn("45s", output)
        self.assertIn("15k tok", output)


if __name__ == "__main__":
    unittest.main()

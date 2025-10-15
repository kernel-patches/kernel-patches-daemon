# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

from kernel_patches_daemon.config import KPDConfig
from kernel_patches_daemon.daemon import KernelPatchesWorker
from kernel_patches_daemon.stats import Stats

TEST_CONFIG: Dict[str, Any] = {
    "version": 3,
    "patchwork": {
        "project": "test",
        "server": "pw",
        "search_patterns": ["pw-search-pattern"],
        "lookback": 7,
    },
    "branches": {
        "test-branch": {
            "repo": "repo",
            "github_oauth_token": "test-oauth-token",
            "upstream": "https://127.0.0.2:0/upstream_org/upstream_repo",
            "ci_repo": "ci-repo",
            "ci_branch": "test_ci_branch",
        },
    },
    "tag_to_branch_mapping": {
        "tag": ["test-branch"],
        "__DEFAULT__": ["test-branch"],
    },
    "base_directory": "/tmp",
}


class TestException(Exception):
    pass


LOGGED_METRICS: List[Dict[str, Any]] = []


def metrics_logger_mock(project: str, stats: Stats) -> None:
    LOGGED_METRICS.append({project: stats})


class TestKernelPatchesWorker(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        LOGGED_METRICS.clear()
        self.github_patcher = patch("kernel_patches_daemon.github_connector.Github")
        self.github_mock = self.github_patcher.start()
        self.addCleanup(self.github_patcher.stop)

        user_mock = self.github_mock.return_value.get_user.return_value
        user_mock.login = "test-user"

        self.git_patcher = patch("kernel_patches_daemon.branch_worker.git.Repo")
        self.git_patcher.start()
        self.addCleanup(self.git_patcher.stop)

        kpd_config = KPDConfig.from_json(TEST_CONFIG)
        self.worker = KernelPatchesWorker(
            kpd_config, {}, metrics_logger=metrics_logger_mock
        )

        self.worker.github_sync_worker.sync_patches = AsyncMock()
        self.worker.reset_github_sync = MagicMock(return_value=True)

    async def test_run_ok(self) -> None:
        with (
            patch(
                "asyncio.sleep",
                AsyncMock(side_effect=TestException("Test complete")),
            ),
            self.assertRaises(TestException),
        ):
            await self.worker.run()

        gh_sync = self.worker.github_sync_worker
        # pyrefly: ignore  # missing-attribute
        gh_sync.sync_patches.assert_called_once()
        # pyrefly: ignore  # missing-attribute
        self.worker.reset_github_sync.assert_called_once()
        self.assertEqual(len(LOGGED_METRICS), 1)
        stats = LOGGED_METRICS[0][self.worker.project]
        self.assertEqual(stats["runs_successful"], 1)

    async def test_run_exception(self) -> None:
        """Test that stats are correctly collected when an exception occurs."""
        gh_sync = self.worker.github_sync_worker
        gh_sync.sync_patches = AsyncMock(side_effect=ValueError("Test exception"))

        with (
            patch(
                "asyncio.sleep", AsyncMock(side_effect=TestException("Test complete"))
            ),
            self.assertRaises(TestException),
        ):
            await self.worker.run()

        self.assertEqual(len(LOGGED_METRICS), 1)
        stats = LOGGED_METRICS[0][self.worker.project]
        self.assertEqual(stats["runs_failed"], 1)
        self.assertEqual(stats["unhandled_ValueError"], 1)

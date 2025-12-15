# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import copy
import os
import unittest
from dataclasses import dataclass
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses import aioresponses

from kernel_patches_daemon.branch_worker import (
    MERGE_CONFLICT_LABEL,
    NewPRWithNoChangeException,
)
from kernel_patches_daemon.config import KPDConfig, SERIES_TARGET_SEPARATOR
from kernel_patches_daemon.github_sync import GithubSync
from tests.common.patchwork_mock import init_pw_responses, PatchworkMock
from tests.common.utils import load_test_data

TEST_BRANCH = "test-branch"
TEST_BPF_NEXT_BRANCH = "test-bpf-next"
TEST_CONFIG: Dict[str, Any] = {
    "version": 3,
    "patchwork": {
        "project": "test",
        "server": "pw",
        "search_patterns": ["pw-search-pattern"],
        "lookback": 5,
    },
    "branches": {
        TEST_BRANCH: {
            "repo": "repo",
            "github_oauth_token": "test-oauth-token",
            "upstream": "https://127.0.0.2:0/upstream_org/upstream_repo",
            "ci_repo": "ci-repo",
            "ci_branch": "test_ci_branch",
        },
        TEST_BPF_NEXT_BRANCH: {
            "repo": "bpf-next-repo",
            "github_oauth_token": "bpf-next-oauth-token",
            "upstream": "https://127.0.0.3:0/kernel-patches/bpf-next",
            "ci_repo": "bpf-next-ci-repo",
            "ci_branch": "bpf-next-ci-branch",
        },
    },
    "tag_to_branch_mapping": {
        "bpf-next": [TEST_BPF_NEXT_BRANCH],
        "multibranch-tag": [TEST_BRANCH, TEST_BPF_NEXT_BRANCH],
        "__DEFAULT__": [TEST_BRANCH],
    },
    "base_directory": "/tmp",
}


class GithubSyncMock(GithubSync):
    def __init__(
        self, kpd_config: Optional[KPDConfig] = None, *args: Any, **kwargs: Any
    ) -> None:
        if kpd_config is None:
            kpd_config = KPDConfig.from_json(TEST_CONFIG)

        presets = {
            "kpd_config": kpd_config,
            "labels_cfg": {},
            **kwargs,
        }

        super().__init__(*args, **presets)


class TestGithubSync(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch("kernel_patches_daemon.github_connector.Github")
        self._gh_mock = patcher.start()
        self.addCleanup(patcher.stop)
        # avoid local git commands
        patcher = patch("kernel_patches_daemon.branch_worker.git.Repo")
        self._git_repo_mock = patcher.start()
        self.addCleanup(patcher.stop)

        self._gh = GithubSyncMock()
        for worker in self._gh.workers.values():
            rate_limit = MagicMock()
            rate_limit.core.remaining = 5000
            worker.git.get_rate_limit = MagicMock(return_value=rate_limit)

    def test_init_with_base_directory(self) -> None:
        @dataclass
        class TestCase:
            name: str
            prefix: str
            base_dir: Optional[str] = None

        test_cases = [
            TestCase(
                name="base_directory not set in config",
                base_dir=None,
                prefix="/tmp/",
            ),
            TestCase(
                name="base_directory set in config",
                base_dir="/foo/bar/code",
                prefix="/foo/bar/code/",
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                config = copy.copy(TEST_CONFIG)
                if case.base_dir is not None:
                    config["base_directory"] = case.base_dir
                kpd_config = KPDConfig.from_json(config)
                gh = GithubSyncMock(kpd_config=kpd_config)
                self.assertEqual(
                    True,
                    gh.workers[TEST_BRANCH].repo_dir.startswith(case.prefix),
                )
                self.assertEqual(
                    True,
                    gh.workers[TEST_BRANCH].ci_repo_dir.startswith(case.prefix),
                )

    def test_close_existing_prs_for_series(self) -> None:
        matching_pr_mock = MagicMock()
        matching_pr_mock.title = "matching"
        matching_pr_mock.head.ref = "test_branch=>remote_branch"

        irrelevant_pr_mock = MagicMock()
        irrelevant_pr_mock.title = "irrelevant"
        irrelevant_pr_mock.head.ref = "other_branch=>other_remote"

        branch_worker_mock = MagicMock()
        branch_worker_mock.prs = {
            matching_pr_mock.title: matching_pr_mock,
            irrelevant_pr_mock.title: irrelevant_pr_mock,
        }

        input_pr_mock = MagicMock()
        input_pr_mock.head.ref = "test_branch=>other_remote_branch"

        workers = [copy.copy(branch_worker_mock) for _ in range(2)]
        self._gh.close_existing_prs_for_series(workers, input_pr_mock)
        for worker in workers:
            self.assertEqual(len(worker.prs), 1)
            self.assertTrue("irrelevant" in worker.prs)
            self.assertTrue("matching" not in worker.prs)

        matching_pr_mock.edit.assert_called_with(state="close")

    async def test_checkout_and_patch_safe(self) -> None:
        pr_branch_name = "fake_pr_branch"
        series = MagicMock()
        pr = MagicMock()
        branch_worker_mock = MagicMock()
        branch_worker_mock.checkout_and_patch = AsyncMock()

        # PR generated
        branch_worker_mock.checkout_and_patch.return_value = pr
        self.assertEqual(
            await self._gh.checkout_and_patch_safe(
                branch_worker_mock, pr_branch_name, series
            ),
            pr,
        )

        # One patch in series failed to apply
        branch_worker_mock.checkout_and_patch.return_value = None
        self.assertIsNone(
            await self._gh.checkout_and_patch_safe(
                branch_worker_mock, pr_branch_name, series
            )
        )

        # Series generates no changes vs target branch, likely already merged
        branch_worker_mock.checkout_and_patch.side_effect = NewPRWithNoChangeException(
            pr_branch_name, "target"
        )
        self.assertIsNone(
            await self._gh.checkout_and_patch_safe(
                branch_worker_mock, pr_branch_name, series
            )
        )

    def _setup_sync_relevant_subject_mocks(self):
        """Helper method to set up common mocks for sync_relevant_subject tests."""
        series_mock = MagicMock()
        series_mock.id = 12345
        series_mock.all_tags = AsyncMock(return_value=["tag"])
        subject_mock = MagicMock()
        subject_mock.subject = "Test subject"
        subject_mock.latest_series = AsyncMock(return_value=series_mock)

        return subject_mock, series_mock

    async def test_sync_relevant_subject_no_mapped_branches(self) -> None:
        subject_mock, series_mock = self._setup_sync_relevant_subject_mocks()
        self._gh.get_mapped_branches = AsyncMock(return_value=[])
        self._gh.checkout_and_patch_safe = AsyncMock()

        await self._gh.sync_relevant_subject(subject_mock)

        self._gh.get_mapped_branches.assert_called_once_with(series_mock)
        self._gh.checkout_and_patch_safe.assert_not_called()

    async def test_sync_relevant_subject_success_first_branch(self) -> None:
        series_prefix = "series/987654"
        series_branch_name = f"{series_prefix}{SERIES_TARGET_SEPARATOR}{TEST_BRANCH}"

        subject_mock, series_mock = self._setup_sync_relevant_subject_mocks()
        series_mock.all_tags = AsyncMock(return_value=["multibranch-tag"])
        subject_mock.branch = AsyncMock(return_value=series_prefix)

        pr_mock = MagicMock()
        pr_mock.head.ref = series_branch_name

        self._gh.checkout_and_patch_safe = AsyncMock(return_value=pr_mock)
        self._gh.close_existing_prs_for_series = MagicMock()

        worker_mock = self._gh.workers[TEST_BRANCH]
        worker_mock.sync_checks = AsyncMock()
        worker_mock.try_apply_mailbox_series = AsyncMock(
            return_value=(True, None, None)
        )

        await self._gh.sync_relevant_subject(subject_mock)

        worker_mock.try_apply_mailbox_series.assert_called_once_with(
            series_branch_name, series_mock
        )
        self._gh.checkout_and_patch_safe.assert_called_once_with(
            worker_mock, series_branch_name, series_mock
        )
        worker_mock.sync_checks.assert_called_once_with(pr_mock, series_mock)
        self._gh.close_existing_prs_for_series.assert_called_once_with(
            list(self._gh.workers.values()), pr_mock
        )

    async def test_sync_relevant_subject_success_second_branch(self) -> None:
        """Test sync_relevant_subject when series fails on first branch but succeeds on second."""
        series_prefix = "series/333333"
        bad_branch_name = f"{series_prefix}{SERIES_TARGET_SEPARATOR}{TEST_BRANCH}"
        good_branch_name = (
            f"{series_prefix}{SERIES_TARGET_SEPARATOR}{TEST_BPF_NEXT_BRANCH}"
        )

        subject_mock, series_mock = self._setup_sync_relevant_subject_mocks()
        series_mock.all_tags = AsyncMock(return_value=["multibranch-tag"])

        pr_mock = MagicMock()
        pr_mock.head.ref = good_branch_name

        self._gh.checkout_and_patch_safe = AsyncMock(return_value=pr_mock)
        self._gh.close_existing_prs_for_series = MagicMock()

        bad_worker_mock = self._gh.workers[TEST_BRANCH]
        bad_worker_mock.sync_checks = AsyncMock()
        bad_worker_mock.subject_to_branch = AsyncMock(return_value=bad_branch_name)
        bad_worker_mock.try_apply_mailbox_series = AsyncMock(
            return_value=(False, None, None)
        )

        good_worker_mock = self._gh.workers[TEST_BPF_NEXT_BRANCH]
        good_worker_mock.sync_checks = AsyncMock()
        good_worker_mock.subject_to_branch = AsyncMock(return_value=good_branch_name)
        good_worker_mock.try_apply_mailbox_series = AsyncMock(
            return_value=(True, None, None)
        )

        await self._gh.sync_relevant_subject(subject_mock)

        bad_worker_mock.try_apply_mailbox_series.assert_called_once_with(
            bad_branch_name, series_mock
        )
        good_worker_mock.try_apply_mailbox_series.assert_called_once_with(
            good_branch_name, series_mock
        )
        self._gh.checkout_and_patch_safe.assert_called_once_with(
            good_worker_mock, good_branch_name, series_mock
        )
        good_worker_mock.sync_checks.assert_called_once_with(pr_mock, series_mock)
        self._gh.close_existing_prs_for_series.assert_called_once_with(
            list(self._gh.workers.values()), pr_mock
        )

    def _setup_test_select_target_branches_for_subject(self):
        series_prefix = "series/123123"
        subject_mock = MagicMock()
        subject_mock.subject = "Test subject"
        subject_mock.branch = AsyncMock(return_value=series_prefix)

        pr_mock = MagicMock()
        pr_mock.head.ref = (
            f"{series_prefix}{SERIES_TARGET_SEPARATOR}{TEST_BPF_NEXT_BRANCH}"
        )

        worker_mock = self._gh.workers[TEST_BPF_NEXT_BRANCH]
        worker_mock.prs = {subject_mock.subject: pr_mock}

        return subject_mock, pr_mock

    async def test_select_target_branches_for_subject(self) -> None:
        subject_mock, _ = self._setup_test_select_target_branches_for_subject()
        mapped_branches = [TEST_BRANCH, TEST_BPF_NEXT_BRANCH]

        selected_branches = await self._gh.select_target_branches_for_subject(
            subject_mock, mapped_branches
        )

        self.assertEqual(selected_branches, [TEST_BPF_NEXT_BRANCH])

    async def test_select_target_branches_for_subject_merge_conflict(self) -> None:
        subject_mock, pr_mock = self._setup_test_select_target_branches_for_subject()
        label = MagicMock()
        label.name = MERGE_CONFLICT_LABEL
        pr_mock.get_labels = MagicMock(return_value=[label])
        mapped_branches = [TEST_BRANCH, TEST_BPF_NEXT_BRANCH]

        selected_branches = await self._gh.select_target_branches_for_subject(
            subject_mock, mapped_branches
        )

        self.assertEqual(selected_branches, mapped_branches)

    @aioresponses()
    async def test_sync_patches_pr_summary_success(self, m: aioresponses) -> None:
        """
        This is a kind of an integration test, attempting to cover most of the
        github_sync.sync_patches() happy-path code.
        For patchwork mocking, it uses real response examples stored at tests/data/
        """

        data_path = os.path.join(
            os.path.dirname(__file__),
            "data/test_sync_patches_pr_summary_success",
        )
        test_data = load_test_data(data_path)
        init_pw_responses(m, test_data)

        # Set up mocks and test data
        patchwork = PatchworkMock(
            server="patchwork.test",
            api_version="1.1",
            search_patterns=[{"archived": False, "project": 399, "delegate": 121173}],
            auth_token="mocktoken",
        )
        patchwork.since = "2025-06-11T00:00:00"
        self._gh.pw = patchwork

        worker = self._gh.workers[TEST_BRANCH]
        # pyrefly: ignore  # missing-attribute
        worker.repo.get_branches.return_value = [MagicMock(name=TEST_BRANCH)]

        worker = self._gh.workers[TEST_BPF_NEXT_BRANCH]
        # pyrefly: ignore  # missing-attribute
        worker.repo.get_branches.return_value = [MagicMock(name=TEST_BPF_NEXT_BRANCH)]
        # pyrefly: ignore  # missing-attribute
        worker.repo.create_pull.return_value = MagicMock(
            html_url="https://github.com/org/repo/pull/98765"
        )

        m.post("https://patchwork.test/api/1.1/patches/14114605/checks/")

        # Execute the function under test
        await self._gh.sync_patches()

        # Verify expected patches and series fetched from patchwork
        # pyrefly: ignore  # missing-attribute
        get_requests = [key for key in m.requests.keys() if key[0] == "GET"]
        touched_urls = set()
        for req in get_requests:
            touched_urls.add(str(req[1]))
        self.assertIn("https://patchwork.test/api/1.1/series/970926/", touched_urls)
        self.assertIn("https://patchwork.test/api/1.1/series/970968/", touched_urls)
        self.assertIn("https://patchwork.test/api/1.1/series/970970/", touched_urls)
        self.assertIn("https://patchwork.test/api/1.1/patches/14114605/", touched_urls)
        self.assertIn("https://patchwork.test/api/1.1/patches/14114773/", touched_urls)
        self.assertIn("https://patchwork.test/api/1.1/patches/14114774/", touched_urls)
        self.assertIn("https://patchwork.test/api/1.1/patches/14114775/", touched_urls)
        self.assertIn("https://patchwork.test/api/1.1/patches/14114777/", touched_urls)

        # Verify that a single POST request was made to patchwork
        # Updating state of a patch 14114605
        # pyrefly: ignore  # missing-attribute
        post_requests = [key for key in m.requests.keys() if key[0] == "POST"]
        self.assertEqual(1, len(post_requests))
        # pyrefly: ignore  # unsupported-operation
        post_calls = m.requests[post_requests[0]]
        url = str(post_requests[0][1])
        self.assertEqual("https://patchwork.test/api/1.1/patches/14114605/checks/", url)
        self.assertEqual(1, len(post_calls))
        post_data = post_calls[0].kwargs["data"]
        expected_post_data = {
            "target_url": "https://github.com/org/repo/pull/98765",
            "context": "test-bpf-next-PR",
            "description": "PR summary",
            "state": "success",
        }
        self.assertDictEqual(expected_post_data, post_data)

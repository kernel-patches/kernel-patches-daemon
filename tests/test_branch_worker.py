# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import os
import random
import re
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import git

from aioresponses import aioresponses

from freezegun import freeze_time
from git.exc import GitCommandError
from kernel_patches_daemon.branch_worker import (
    _is_branch_changed,
    _series_already_applied,
    ALREADY_MERGED_LOOKBACK,
    BRANCH_TTL,
    BranchWorker,
    build_email,
    create_color_labels,
    email_in_submitter_allowlist,
    EmailBodyContext,
    furnish_ci_email_body,
    parse_pr_ref,
    same_series_different_target,
    temporary_patch_file,
    UPSTREAM_REMOTE_NAME,
)
from kernel_patches_daemon.config import (
    EmailConfig,
    SERIES_ID_SEPARATOR,
    SERIES_TARGET_SEPARATOR,
)
from kernel_patches_daemon.github_logs import DefaultGithubLogExtractor
from kernel_patches_daemon.patchwork import Series, Subject
from kernel_patches_daemon.status import Status
from munch import Munch, munchify

from tests.common.patchwork_mock import (
    DEFAULT_TEST_RESPONSES,
    get_default_pw_client,
    init_pw_responses,
)

from . import read_fixture


TEST_REPO = "repo"
TEST_REPO_URL = f"https://user:pass@127.0.0.1/org/{TEST_REPO}"
TEST_REPO_BRANCH = "test_branch"
TEST_REPO_PR_BASE_BRANCH = "test_branch_base"
TEST_UPSTREAM_REPO_URL = "https://127.0.0.2:0/upstream_org/upstream_repo"
TEST_UPSTREAM_BRANCH = "test_upstream_branch"
TEST_CI_REPO = "ci-repo"
TEST_CI_REPO_URL = f"https://user:pass@127.0.0.1/ci-org/{TEST_CI_REPO}"
TEST_CI_BRANCH = "test_ci_branch"
TEST_BASE_DIRECTORY = "/repos"
TEST_BRANCH = "test-branch"
TEST_CONFIG: Dict[str, Any] = {
    "version": 2,
    "project": "test",
    "pw_url": "pw",
    "pw_search_patterns": "pw-search-pattern",
    "pw_lookback": 5,
    "branches": {
        TEST_BRANCH: {
            "repo": TEST_REPO,
            "github_oauth_token": "test-oauth-token",
            "upstream": TEST_UPSTREAM_REPO_URL,
            "ci_repo": TEST_CI_REPO,
            "ci_branch": TEST_CI_BRANCH,
        }
    },
    "tag_to_branch_mapping": [],
}
TEST_LABELS_CFG = {
    "changes-requested": "2a76af",
    "merge-conflict": "e85506",
    "RFC": "f2e318",
    "new": "c2e0c6",
}
SERIES_DATA: Dict[str, Any] = {
    "id": 0,
    "name": "foo",
    "date": "2010-07-20T01:00:00",
    "patches": [
        {
            "msgid": "patch1-msgid@localhost",
        }
    ],
    "cover_letter": None,
    "version": 0,
    "url": "https://example.com",
    "web_url": "https://example.com",
    "submitter": {"email": "a-user@example.com"},
    "mbox": "https://example.com",
}


class BranchWorkerMock(BranchWorker):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        presets = {
            "patchwork": MagicMock(),
            "labels_cfg": TEST_LABELS_CFG,
            "repo_branch": TEST_REPO_BRANCH,
            "repo_url": TEST_REPO_URL,
            "github_oauth_token": "random_gh_oauth_token",
            "upstream_url": TEST_UPSTREAM_REPO_URL,
            "upstream_branch": TEST_UPSTREAM_BRANCH,
            "ci_repo_url": TEST_CI_REPO_URL,
            "ci_branch": TEST_CI_BRANCH,
            "log_extractor": DefaultGithubLogExtractor(),
            "base_directory": TEST_BASE_DIRECTORY,
        }
        presets.update(kwargs)

        super().__init__(*args, **presets)


def get_default_bw_client() -> BranchWorkerMock:
    return BranchWorkerMock()


class TestBranchWorker(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch("kernel_patches_daemon.github_connector.Github")
        self._gh_mock = patcher.start()
        self.addCleanup(patcher.stop)
        # avoid local git commands
        patcher = patch("kernel_patches_daemon.branch_worker.git.Repo")
        self._git_repo_mock = patcher.start()
        self.addCleanup(patcher.stop)

        self._bw = BranchWorkerMock()

    async def asyncSetUp(self) -> None:
        # Patchwork client
        self._pw = get_default_pw_client()

    def test_fetch_repo_branch(self) -> None:
        """
        Fetch master fetches upstream repo, the CI repo, and check out the right branch.
        """
        with patch.object(BranchWorker, "fetch_repo") as fr:
            git_mock = unittest.mock.Mock()
            fr.return_value = git_mock
            self._bw.fetch_repo_branch()
            self.assertEqual(fr.call_count, 2)
            # We pull the repo with the right branch.
            self.assertEqual(fr.mock_calls[0].args[1], TEST_REPO_URL)
            self.assertEqual(fr.mock_calls[0].args[2], TEST_REPO_BRANCH)
            # Same for the CI repo.
            self.assertEqual(fr.mock_calls[1].args[1], TEST_CI_REPO_URL)
            self.assertEqual(fr.mock_calls[1].args[2], TEST_CI_BRANCH)
            # We check out the right branch.
            self.assertEqual(git_mock.mock_calls[0].args[0], f"origin/{TEST_CI_BRANCH}")

    def test_get_pulls(self) -> None:
        """
        When getting PR from GH some bookkeeping is done like checking relevancy of PR and state.
        """

        @dataclass
        class PR:
            state: str = "open"
            relevant: bool = True
            # Generate a random title... abuse Mock for this
            title: MagicMock = field(default_factory=MagicMock)

        @dataclass
        class TestCase:
            name: str
            prs: List[PR]
            added_pr_delta: int = 0
            relevant_prs_delta: int = 0

        test_cases = [
            TestCase(
                name="All open PRs, all relevant PRs",
                prs=[PR(), PR(), PR()],
                added_pr_delta=0,
                relevant_prs_delta=0,
            ),
            # Not supposed to happen, but we currently filter out non-opened PR regardless of the query to GH already doing that.
            TestCase(
                name="One non-opened, all relevant PRs",
                prs=[PR(), PR(state="invalid"), PR()],
                added_pr_delta=1,
                relevant_prs_delta=0,
            ),
            TestCase(
                name="All open PRs, one non-relevant PR",
                prs=[PR(), PR(relevant=False), PR()],
                added_pr_delta=0,
                relevant_prs_delta=1,
            ),
            # Not supposed to happen, but we currently filter out non-opened PR regardless of the query to GH already doing that.
            TestCase(
                name="One non-opened and non-relevant PR",
                prs=[PR(), PR(relevant=False, state="invalid"), PR()],
                added_pr_delta=1,
                relevant_prs_delta=1,
            ),
            # Not supposed to happen, but we currently filter out non-opened PR regardless of the query to GH already doing that.
            TestCase(
                name="One non-opened PR, one non-relevant PR",
                prs=[PR(), PR(relevant=False), PR(state="invalid"), PR()],
                added_pr_delta=1,
                relevant_prs_delta=1,
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                self._bw.repo.get_pulls.reset_mock()
                self._bw.repo.get_pulls.return_value = case.prs
                with (
                    patch.object(BranchWorker, "add_pr") as ap,
                    patch.object(BranchWorker, "_is_relevant_pr") as rp,
                ):
                    # set is_relevant return values
                    rp.side_effect = [pr.relevant for pr in case.prs]

                    self._bw.get_pulls()
                    # We get pull requests from upstream repo through Github API, only looking for opened PR against our branch of interest.
                    self._bw.repo.get_pulls.assert_called_once_with(
                        state="open", base=TEST_REPO_PR_BASE_BRANCH
                    )
                    # For each PR, we check that they are relevant
                    self.assertEqual(len(case.prs), rp.call_count)
                    # and only store the relevant ones
                    self.assertEqual(
                        len(case.prs) - len(self._bw.prs), case.relevant_prs_delta
                    )
                    # We only call add_pr for open PRs
                    self.assertEqual(len(case.prs) - ap.call_count, case.added_pr_delta)

    def test_do_sync_create_remote(self) -> None:
        """
        When syncing, if the remote does not exist in repo_local, create it.
        """

        # A fake remote
        @dataclass
        class R:
            name: str

        with patch.object(self._bw, "repo_local") as lr:
            lr.remotes = [
                R("a"),
                R("b"),
                R("c"),
            ]
            self._bw.do_sync()
            lr.create_remote.assert_called_once_with(
                UPSTREAM_REMOTE_NAME, TEST_UPSTREAM_REPO_URL
            )

            lr.delete_remote.assert_not_called()
            # After having created the remote, we access it.
            lr.remote.assert_called_once_with(UPSTREAM_REMOTE_NAME)

    def test_do_sync_recreate_remote_when_changed(self) -> None:
        """
        When syncing, if the remote exist in repo_local but is not tracking the
        right URL, delete and recreate it.
        """

        # A fake remote
        @dataclass
        class R:
            name: str

        with patch.object(self._bw, "repo_local") as lr:
            lr.remote.return_value = MagicMock(urls=["1", "2", "3"])
            lr.remotes = [
                R("a"),
                R("b"),
                R(UPSTREAM_REMOTE_NAME),
                R("c"),
            ]

            self._bw.do_sync()
            lr.create_remote.assert_called_once_with(
                UPSTREAM_REMOTE_NAME, TEST_UPSTREAM_REPO_URL
            )

            lr.delete_remote.assert_called_once_with(UPSTREAM_REMOTE_NAME)
            # We access the remote twice.
            self.assertEqual(2, lr.remote.call_count)
            lr.remote.assert_called_with(UPSTREAM_REMOTE_NAME)

    def test_do_sync_no_remote_modification(self) -> None:
        """
        If the remote is set correctly, we don't modify it.
        """

        # A fake remote
        @dataclass
        class R:
            name: str

        with patch.object(self._bw, "repo_local") as lr:
            lr.remote.return_value = MagicMock(urls=[self._bw.upstream_url])
            lr.remotes = [
                R("a"),
                R(UPSTREAM_REMOTE_NAME),
                R("b"),
            ]

            self._bw.do_sync()
            lr.create_remote.assert_not_called()
            lr.delete_remote.assert_not_called()
            # We access the remote twice.
            self.assertEqual(2, lr.remote.call_count)
            lr.remote.assert_called_with(UPSTREAM_REMOTE_NAME)

    def test_do_sync_reset_repo(self) -> None:
        with (
            patch.object(self._bw, "repo_local") as lr,
            patch("kernel_patches_daemon.branch_worker._reset_repo") as rr,
        ):
            # Create a mock suitable to mock a git.RemoteReference
            remote_ref = "a/b/c/d"
            m = MagicMock()
            m.__str__.return_value = remote_ref
            lr.remote.return_value.refs = MagicMock(**{TEST_UPSTREAM_BRANCH: m})
            self._bw.do_sync()

            # the repo is reset
            rr.assert_called_once()
            # and pushed from upstream_url to downstream
            lr.git.push.assert_called_once_with(
                "--force", "origin", f"{remote_ref}:refs/heads/{TEST_REPO_BRANCH}"
            )

    def test_relevant_pr(self) -> None:
        """
        Test to validate the combination of what make a PR relevant/irrelevant.
        """

        user_login = "foo"

        # Set our user login name
        self._bw.user_or_org = user_login
        self._bw.user_login = user_login

        def make_munch(
            user: str = user_login,
            head_user: str = user_login,
            base_user: str = user_login,
            base_ref: str = TEST_REPO_PR_BASE_BRANCH,
            state: str = "open",
        ) -> Munch:
            """Helper to make a Munch that can be consumed as a PR (e.g accessing nested attributes)"""
            return munchify(
                {
                    "user": {"login": user},
                    "head": {"user": {"login": head_user}},
                    "base": {"user": {"login": base_user}, "ref": base_ref},
                    "state": state,
                },
            )

        @dataclass
        class TestCase:
            name: str
            pr: Munch
            relevant: bool

        test_cases = [
            TestCase(
                name="Relevant PR",
                pr=make_munch(),
                relevant=True,
            ),
            TestCase(
                name="Wrong user",
                pr=make_munch(user="bar"),
                relevant=False,
            ),
            TestCase(
                name="Wrong head user",
                pr=make_munch(head_user="bar"),
                relevant=False,
            ),
            TestCase(
                name="Wrong base user",
                pr=make_munch(base_user="bar"),
                relevant=False,
            ),
            TestCase(
                name="Wrong head and base user",
                pr=make_munch(head_user="bar", base_user="bar"),
                relevant=False,
            ),
            TestCase(
                name="Wrong base ref",
                pr=make_munch(base_ref="some other base ref"),
                relevant=False,
            ),
            TestCase(
                name="Wrong state",
                pr=make_munch(state="some other state"),
                relevant=False,
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                # pyre-fixme: Incompatible parameter type [6]: In call `BranchWorker._is_relevant_pr`,
                # for 1st positional argument, expected `PullRequest` but got `Munch`.
                self.assertEqual(self._bw._is_relevant_pr(case.pr), case.relevant)

    def test_fetch_repo_path_doesnt_exist_full_sync(self) -> None:
        """When the repo does not exist yet, a full sync is performed."""
        fetch_params = ["somepath", "giturl", "branch"]
        with (
            patch.object(self._bw, "full_sync") as fr,
            patch("kernel_patches_daemon.branch_worker.os.path.exists") as exists,
        ):
            # path does not exists
            exists.return_value = False
            self._bw.fetch_repo(*fetch_params)
            fr.assert_called_once_with(*fetch_params)

    def test_fetch_repo_path_exists_no_full_sync(self) -> None:
        """If the repo already exist, we don't perform a full sync."""
        fetch_params = ["somepath", "giturl", "branch"]
        with (
            patch.object(self._bw, "full_sync") as fr,
            patch("kernel_patches_daemon.branch_worker.os.path.exists") as exists,
        ):
            # path does exists
            exists.return_value = True
            self._bw.fetch_repo(*fetch_params)
            fr.assert_not_called()

    def test_fetch_repo_path_exists_git_exception(self) -> None:
        """When the repo exists but we hit a git command exception, we fallback on full sync."""
        fetch_params = ["somepath", "giturl", "branch"]
        with (
            patch.object(self._bw, "full_sync") as fr,
            patch("kernel_patches_daemon.branch_worker.os.path.exists") as exists,
        ):
            # path does exists
            exists.return_value = True
            self._git_repo_mock.init.return_value.git.fetch.side_effect = (
                GitCommandError(command="foo bar")
            )
            self._bw.fetch_repo(*fetch_params)
            fr.assert_called_once_with(*fetch_params)

    def test_expire_branches(self) -> None:
        """Only the branch that matches pattern and is expired should be deleted"""
        not_expired_time = datetime.fromtimestamp(3 * BRANCH_TTL)
        expired_time = datetime.fromtimestamp(BRANCH_TTL)

        @dataclass
        class PR:
            state: str = "closed"
            updated_at: datetime = not_expired_time

        @dataclass
        class TestCase:
            name: str
            branches: list
            all_prs: list = field(default_factory=list)
            # args and return PR for self.filter_closed_pr(branch)
            fcp_called_branches: list = field(default_factory=list)
            fcp_return_prs: list = field(default_factory=list)
            # args for self.delete_branch(branch)
            deleted_branches: list = field(default_factory=list)

        test_cases = [
            TestCase(
                name="A branch in all PR, even if supposedly expired, is not deleted.",
                branches=["test1", "test2"],
                all_prs=["test1", "test2"],
                # no filter_closed_pr call, no delete_branch call
            ),
            TestCase(
                name="A branch fetch pattern: expired should be deleted, not expired should not be deleted.",
                branches=[
                    f"series{SERIES_ID_SEPARATOR}111111{SERIES_TARGET_SEPARATOR}{self._bw.repo_branch}",
                    f"series{SERIES_ID_SEPARATOR}222222{SERIES_TARGET_SEPARATOR}{self._bw.repo_branch}",
                ],
                fcp_called_branches=[
                    f"series{SERIES_ID_SEPARATOR}111111{SERIES_TARGET_SEPARATOR}{self._bw.repo_branch}",
                    f"series{SERIES_ID_SEPARATOR}222222{SERIES_TARGET_SEPARATOR}{self._bw.repo_branch}",
                ],
                fcp_return_prs=[PR(updated_at=expired_time), PR()],
                deleted_branches=[
                    f"series{SERIES_ID_SEPARATOR}111111{SERIES_TARGET_SEPARATOR}{self._bw.repo_branch}"
                ],
                # filter_closed_prs for both "test1=>repo_branch", "test2=>repo_branch"
                # only delete "test1=>repo_branch"
            ),
            TestCase(
                name="A branch that does not match the expected branch pattern, even if supposedly expired, is not deleted",
                branches=["test1", "test2"],
                # no filter_closed_pr call, no delete_branch call
            ),
            TestCase(
                name="A branch that belongs to self.repo_branch, even if supposedly expired, is not deleted",
                branches=[self._bw.repo_branch],
                # no filter_closed_pr call, no delete_branch call
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                self._bw.branches = case.branches
                self._bw.all_prs = {p: {} for p in case.all_prs}
                with (
                    patch.object(self._bw, "filter_closed_pr") as fcp,
                    patch.object(self._bw, "delete_branch") as db,
                    freeze_time(not_expired_time),
                ):
                    fcp.side_effect = case.fcp_return_prs
                    self._bw.expire_branches()
                    # check fcp and db are called with proper counts
                    self.assertEqual(len(case.fcp_called_branches), fcp.call_count)
                    self.assertEqual(len(case.deleted_branches), db.call_count)
                    # check args for each fcp called
                    self.assertEqual(
                        [x.args[0] for x in fcp.mock_calls], case.fcp_called_branches
                    )
                    # check args for each db called
                    self.assertEqual(
                        [x.args[0] for x in db.mock_calls], case.deleted_branches
                    )

    def test_filter_closed_pr(self) -> None:
        """Filter the most recent one closed PR per head from all closed PRs"""
        base_time = 10000
        base_datetime = datetime.fromtimestamp(base_time)

        def make_munch(
            head_ref: str = "test",
            state: str = "closed",
            updated_at: datetime = base_datetime,
            title: str = "title",
        ) -> Munch:
            """Helper to make a Munch that can be consumed as a PR (e.g accessing nested attributes)"""
            return munchify(
                {
                    "head": {"ref": head_ref},
                    "state": state,
                    "updated_at": updated_at,
                    "title": title,
                }
            )

        @dataclass
        class TestCase:
            name: str
            closed_prs: List[Munch]
            branch: str
            return_pr: Munch

        test_cases = [
            TestCase(
                name="No PR should be returned",
                closed_prs=[
                    make_munch(head_ref="branch1"),
                    make_munch(head_ref="branch2"),
                ],
                branch="branch3",
                return_pr=make_munch(head_ref="None"),
            ),
            TestCase(
                name="PR with correct head should be returned",
                closed_prs=[
                    make_munch(head_ref="branch1", title="branch1"),
                    make_munch(head_ref="branch2", title="branch2"),
                ],
                branch="branch1",
                return_pr=make_munch(head_ref="branch1", title="branch1"),
            ),
            TestCase(
                name="The most recent one should be returned",
                closed_prs=[
                    make_munch(head_ref="branch1", title="branch1_old_pr"),
                    make_munch(
                        head_ref="branch1",
                        updated_at=datetime.fromtimestamp(base_time + 100),
                        title="branch1_recent_pr",
                    ),
                    make_munch(
                        head_ref="branch1",
                        updated_at=datetime.fromtimestamp(base_time + 50),
                        title="branch1_intermediary_pr",
                    ),
                ],
                branch="branch1",
                return_pr=make_munch(
                    head_ref="branch1",
                    updated_at=datetime.fromtimestamp(base_time + 100),
                    title="branch1_recent_pr",
                ),
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                with patch.object(self._bw, "closed_prs") as cp:
                    cp.return_value = case.closed_prs
                    return_pr = self._bw.filter_closed_pr(case.branch)
                    if not return_pr:
                        self.assertEqual("None", case.return_pr.head.ref)
                    else:
                        self.assertEqual(return_pr.title, case.return_pr.title)

    def test_delete_branches(self) -> None:
        """Delete a branch with correct calls and args"""
        branch_deleted = "branch"
        with patch.object(self._bw.repo, "get_git_ref") as ggr:
            self._bw.delete_branch(branch_deleted)
            ggr.assert_called_once_with(f"heads/{branch_deleted}")
            ggr.return_value.delete.assert_called_once()

    @aioresponses()
    async def test_guess_pr_return_from_active_pr_cache(self, m: aioresponses) -> None:
        # Whatever is in our self.prs's cache dictionary will be returned.
        series = Series(self._pw, SERIES_DATA)
        sentinel = random.random()
        # pyre-fixme[6]: For 2nd argument expected `PullRequest` but got `float`.
        self._bw.prs["foo"] = sentinel
        pr = await self._bw._guess_pr(series)
        self.assertEqual(sentinel, pr)

    async def test_guess_pr_return_from_secondary_cache_with_specified_branch(
        self,
    ) -> None:
        # After self.prs, we will look into self.all_prs
        # When calling _guess_pr with a branch name, we will look for it in
        # self.all_prs without trying to resolve the actual branch name based
        # on the series.
        mybranch = "mybranch"
        series = Series(self._pw, SERIES_DATA)
        sentinel = random.random()
        self._bw.all_prs[mybranch] = {}
        self._bw.all_prs[mybranch][TEST_REPO_BRANCH] = [sentinel]
        pr = await self._bw._guess_pr(series, mybranch)
        self.assertEqual(sentinel, pr)

    @aioresponses()
    async def test_guess_pr_return_from_secondary_cache_without_specified_branch(
        self, m: aioresponses
    ) -> None:
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        # After self.prs, we will look into self.all_prs
        # When calling _guess_pr without a branch name, we will resolve it and
        # then look in self.all_prs.
        series = Series(self._pw, SERIES_DATA)
        mybranch = await self._bw.subject_to_branch(Subject(series.subject, self._pw))

        sentinel = random.random()
        self._bw.all_prs[mybranch] = {}
        self._bw.all_prs[mybranch][TEST_REPO_BRANCH] = [sentinel]
        pr = await self._bw._guess_pr(series, mybranch)
        self.assertEqual(sentinel, pr)

    @aioresponses()
    async def test_guess_pr_not_in_cache_no_specified_branch_no_remote_branch(
        self, m: aioresponses
    ) -> None:
        """
        Handling of series which is not in our PR cache (self.prs, self.all_prs empty)
        and for which we do not have an active remote branch (self.branches)
        Repro for T147351415
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        # Replace our BranchWorker PW instance by the mocked one.
        self._bw.patchwork = self._pw
        # Replace our BranchWorker repo instance by our gh_mock
        self._bw.repo = self._gh_mock

        series = Series(self._pw, {**SERIES_DATA, "name": "foo"})
        mybranch = await self._bw.subject_to_branch(Subject(series.subject, self._pw))
        pr = await self._bw._guess_pr(series, mybranch)

        # branch is not an active remote branch, we look up for existing PRs
        # and do not find any.
        self.assertTrue(self._gh_mock.method_calls)
        self.assertIsNone(pr)

    @aioresponses()
    async def test_guess_pr_not_in_cache_no_specified_branch_has_remote_branch_v1(
        self, m: aioresponses
    ) -> None:
        """
        Handling of series which is not in our PR cache (self.prs, self.all_prs empty)
        and for which we do not have an active remote branch (self.branches).
        V1 series.
        Repro for T147351415
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        # Replace our BranchWorker PW instance by the mocked one.
        self._bw.patchwork = self._pw
        # Replace our BranchWorker repo instance by our gh_mock
        self._bw.repo = self._gh_mock

        series = Series(self._pw, {**SERIES_DATA, "version": 1})
        mybranch = await self._bw.subject_to_branch(Subject(series.subject, self._pw))
        self._bw.branches = ["aaa"]
        pr = await self._bw._guess_pr(series, mybranch)

        # branch is an active remote branch, our series version is v1. We look up closed
        # PRs regardless but don't find any.
        self.assertTrue(self._gh_mock.method_calls)
        self.assertIsNone(pr)

    @aioresponses()
    async def test_guess_pr_not_in_cache_no_specified_branch_has_remote_branch_v2_first_series(
        self, m: aioresponses
    ) -> None:
        """
        Handling of series which is not in our PR cache (self.prs, self.all_prs empty)
        and for which we do not have an active remote branch (self.branches)
        V2 series.
        Repro for T147351415
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)

        # Replace our BranchWorker PW instance by the mocked one.
        self._bw.patchwork = self._pw
        # Replace our BranchWorker repo instance by our gh_mock
        self._bw.repo = self._gh_mock

        series = Series(self._pw, {**SERIES_DATA, "name": "code", "version": 2})
        mybranch = await self._bw.subject_to_branch(Subject(series.subject, self._pw))
        self._bw.branches = [mybranch]
        pr = await self._bw._guess_pr(series, mybranch)

        # branch is an active remote branch, we are on version 2, but we find only
        # one relevant series. We search for closed PRs regardless.
        self.assertTrue(self._gh_mock.method_calls)
        self.assertIsNone(pr)

    @aioresponses()
    async def test_guess_pr_not_in_cache_no_specified_branch_is_remote_branch_v2_multiple_series_noclosed_pr(
        self, m: aioresponses
    ) -> None:
        """
        Handling of series which is not in our PR cache (self.prs, self.all_prs empty)
        and for which we do not have an active remote branch (self.branches).
        We look for a closed PR but don't find any.
        Repro for T147351415
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)

        # Replace our BranchWorker PW instance by the mocked one.
        self._bw.patchwork = self._pw
        # Replace our BranchWorker repo instance by our gh_mock
        self._bw.repo = self._gh_mock
        series = Series(self._pw, {**SERIES_DATA, "name": "[v2] barv2", "version": 2})

        # DEFAULT_TEST_RESPONSES will return series 6 and 9, 6 being the first one
        mybranch = f"series/6=>{TEST_REPO_BRANCH}"
        self._bw.branches = [mybranch]

        # Calling without specifying `branch` so we force looking up series in
        # pw_tests.DEFAULT_TEST_RESPONSES
        pr = await self._bw._guess_pr(series)

        # branch is an active remote branch, our series is on v2, we have
        # multiple relevant series so we lookup for closed PR but don't find any.
        self.assertTrue(self._gh_mock.method_calls)
        self.assertIsNone(pr)

    @aioresponses()
    async def test_guess_pr_not_in_cache_no_specified_branch_is_remote_branch_v2_multiple_series_with_closed_pr(
        self, m: aioresponses
    ) -> None:
        """
        Handling of series which is not in our PR cache (self.prs, self.all_prs empty)
        and for which we do not have an active remote branch (self.branches)
        We look for a closed PR and find one.
        Repro for T147351415
        """

        init_pw_responses(m, DEFAULT_TEST_RESPONSES)

        # Replace our BranchWorker PW instance by the mocked one.
        self._bw.patchwork = self._pw
        # Replace our BranchWorker repo instance by our gh_mock
        self._bw.repo = self._gh_mock
        # DEFAULT_TEST_RESPONSES will return series 6 and 9, 9 being the latest one
        mybranch = f"series/9=>{TEST_REPO_BRANCH}"
        mymunch = munchify(
            {
                "head": {"ref": mybranch},
                "state": "closed",
                "updated_at": "2010-07-19T00:00:00",
                "title": "title",
            }
        )
        self._gh_mock.get_pulls.return_value = [
            mymunch,
        ]

        series = Series(self._pw, {**SERIES_DATA, "name": "[v2] barv2", "version": 2})

        self._bw.branches = [mybranch]

        # Calling without specifying `branch` so we force looking up series in
        # pw_tests.DEFAULT_TEST_RESPONSES
        pr = await self._bw._guess_pr(series)

        # branch is an active remote branch, our series is on v2, we have
        # multiple relevant series so we lookup for closed PR and find one.
        self.assertTrue(self._gh_mock.method_calls)
        self.assertIsNotNone(pr)
        self.assertEqual(pr, mymunch)


class TestSupportFunctions(unittest.TestCase):
    def test_temporary_patch_file(self) -> None:
        content = b"test content"
        with temporary_patch_file(content) as tmp_file:
            self.assertEqual(content, tmp_file.read())

    def test_create_color_labels(self) -> None:
        with self.subTest("new"):
            repo_mock = MagicMock()
            create_color_labels({"label": "00000"}, repo_mock)

            repo_mock.get_labels.return_value = []
            repo_mock.create_label.assert_called_once_with(name="label", color="00000")

        with self.subTest("existing_update"):
            repo_label_mock = MagicMock()
            repo_label_mock.name = "label"
            repo_label_mock.color = "00001"

            repo_mock = MagicMock()
            repo_mock.get_labels.return_value = [repo_label_mock]

            create_color_labels({"label": "00000"}, repo_mock)

            repo_mock.create_label.assert_not_called()
            repo_label_mock.edit.assert_called_once_with(name="label", color="00000")

        with self.subTest("existing_skip"):
            repo_label_mock = MagicMock()
            repo_label_mock.name = "label"
            repo_label_mock.color = "00000"

            repo_mock = MagicMock()
            repo_mock.get_labels.return_value = [repo_label_mock]

            create_color_labels({"label": "00000"}, repo_mock)

            repo_mock.create_label.assert_not_called()
            repo_label_mock.edit.assert_not_called()

        with self.subTest("existing_case_mismatch"):
            repo_label_mock = MagicMock()
            repo_label_mock.name = "LabeL"
            repo_label_mock.color = "00001"

            repo_mock = MagicMock()
            repo_mock.get_labels.return_value = [repo_label_mock]

            create_color_labels({"laBel": "00000"}, repo_mock)

            repo_mock.create_label.assert_not_called()
            repo_label_mock.edit.assert_called_once_with(name="label", color="00000")

    def test_same_series_different_target(self) -> None:
        with self.subTest("same_series_different_target"):
            self.assertTrue(
                same_series_different_target(
                    "series/1=>target", "series/1=>other_target"
                )
            )

        with self.subTest("different_series_same_target"):
            self.assertFalse(
                same_series_different_target("series/1=>target", "series/2=>target")
            )

        with self.subTest("different_series_different_target"):
            self.assertFalse(
                same_series_different_target(
                    "series/1=>target", "series/2=>other_target"
                )
            )


class TestGitSeriesAlreadyApplied(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.repo = git.Repo.init(self.tmp_dir)

        # Create a file in the repository
        with open(self.tmp_dir + "/file.txt", "w") as f:
            f.write("Hello, world!")

        # So that test cases know they have at least 100 values to pick from
        self.assertGreaterEqual(ALREADY_MERGED_LOOKBACK, 100)

        # Add some dummy commits.
        # Note despite file.txt not changing, this still creates commits.
        for i in range(1, 2 * ALREADY_MERGED_LOOKBACK + 1):
            self.repo.index.add(["file.txt"])
            self.repo.index.commit(f"Commit {i}\n\nThis commit body should never match")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    async def asyncSetUp(self):
        # Patchwork client
        self._pw = get_default_pw_client()

    async def _get_series(self, m: aioresponses, summaries: List[str]) -> Series:
        """
        Given a list of commit summaries, return a `Series` that
        contains such commits.
        """
        data = {
            "https://127.0.0.1/api/1.1/series/42/": {
                "id": 42,
                "name": "[a/b] this series is *NOT* closed!",
                "date": "2010-07-20T01:00:00",
                "patches": [{"id": i} for i in range(len(summaries))],
                "cover_letter": {"name": "[tag] cover letter name"},
                "version": 4,
                "url": "https://example.com",
                "web_url": "https://example.com",
                "submitter": {"email": "a-user@example.com"},
                "mbox": "https://example.com",
            },
            **{
                f"https://127.0.0.1/api/1.1/patches/{i}/": {
                    "id": i,
                    "project": {"id": "1234"},
                    "delegate": {"id": "12345"},
                    "archived": False,
                    "state": "new",
                    "name": summaries[i],
                }
                for i in range(len(summaries))
            },
        }

        init_pw_responses(m, data)
        series = await self._pw.get_series_by_id(42)

        return series

    @aioresponses()
    async def test_applied_all(self, m: aioresponses):
        in_1 = ALREADY_MERGED_LOOKBACK + 33
        in_2 = ALREADY_MERGED_LOOKBACK + 34
        series = await self._get_series(m, [f"Commit {in_1}", f"[tag] Commit {in_2}"])
        self.assertTrue(await _series_already_applied(self.repo, series))

    @aioresponses()
    async def test_applied_none_newer(self, m: aioresponses):
        out_1 = ALREADY_MERGED_LOOKBACK * 2 + 2
        out_2 = ALREADY_MERGED_LOOKBACK * 2 + 3
        series = await self._get_series(
            m, [f"[some tags]Commit {out_1}", f"[tag] Commit {out_2}"]
        )
        self.assertFalse(await _series_already_applied(self.repo, series))

    @aioresponses()
    async def test_applied_none_older(self, m: aioresponses):
        series = await self._get_series(m, ["[some tags]Commit 33", "[tag] Commit 34"])
        self.assertFalse(await _series_already_applied(self.repo, series))

    @aioresponses()
    async def test_applied_some(self, m: aioresponses):
        inside = ALREADY_MERGED_LOOKBACK + 55
        out = ALREADY_MERGED_LOOKBACK * 3
        series = await self._get_series(m, [f"Commit {inside}", f"Commit {out}"])
        self.assertTrue(await _series_already_applied(self.repo, series))

    @aioresponses()
    async def test_applied_all_case_insensitive(self, m: aioresponses):
        in_1 = ALREADY_MERGED_LOOKBACK + 33
        in_2 = ALREADY_MERGED_LOOKBACK + 34
        series = await self._get_series(m, [f"commit {in_1}", f"[tag] COMMIT {in_2}"])
        self.assertTrue(await _series_already_applied(self.repo, series))


class TestBranchChanged(unittest.TestCase):
    SINGLE_COMMIT_CHANGE_MESSAGE = "single commit change\n"

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.repo = git.Repo.init(self.tmp_dir)

        # So git-commit --amend doesn't error about missing configs in CI
        self.repo.git.config("user.name", "test")
        self.repo.git.config("user.email", "test@test.com")

        # Create a file in the repository
        with open(self.tmp_dir + "/file.txt", "w") as f:
            f.write("Hello, world!\n")

        # Create initial commit on master
        self.repo.index.add(["file.txt"])
        self.repo.index.commit("Initial commit\n")

        # Create new branch for single commit change
        self.repo.create_head("single_commit_change", commit="master").checkout()
        with open(f"{self.tmp_dir}/file.txt", "a") as f:
            f.write("line 1\n")
            f.write("line 2\n")
        self.repo.index.add(["file.txt"])
        self.repo.index.commit(self.SINGLE_COMMIT_CHANGE_MESSAGE)

        # Create branch for a different single commit change
        self.repo.create_head(
            "different_single_commit_change", commit="master"
        ).checkout()
        with open(f"{self.tmp_dir}/file.txt", "a") as f:
            f.write("built different\n")
        self.repo.index.add(["file.txt"])
        self.repo.index.commit("different single commit change\n")

        # Create duplicate branch for single commit change but with different SHA
        self.repo.create_head(
            "single_commit_change_clone", commit="single_commit_change"
        ).checkout()
        self.repo.git.commit("--amend", "--no-edit")

        # Create branch to do same change but in two commits
        self.repo.create_head("two_commit_change", commit="master").checkout()
        for i in range(2):
            with open(f"{self.tmp_dir}/file.txt", "a") as f:
                f.write(f"line {i+1}\n")
            self.repo.index.add(["file.txt"])
            self.repo.index.commit("split change, part {i+1}\n")

        # Create branch to do same change but in two commits with same duplicate
        self.repo.create_head(
            "two_commit_change_with_same_msg", commit="master"
        ).checkout()
        for i in range(2):
            with open(f"{self.tmp_dir}/file.txt", "a") as f:
                f.write(f"line {i+1}\n")
            self.repo.index.add(["file.txt"])
            self.repo.index.commit(self.SINGLE_COMMIT_CHANGE_MESSAGE)

        self.repo.heads.master.checkout()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_different_change(self):
        self.assertTrue(
            _is_branch_changed(
                self.repo,
                "master",
                "single_commit_change",
                "different_single_commit_change",
            )
        )

    def test_duplicate_change(self):
        self.assertFalse(
            _is_branch_changed(
                self.repo,
                "master",
                "single_commit_change",
                "single_commit_change",
            )
        )
        self.assertFalse(
            _is_branch_changed(
                self.repo,
                "master",
                "two_commit_change",
                "two_commit_change",
            )
        )
        self.assertFalse(
            _is_branch_changed(
                self.repo,
                "master",
                "two_commit_change_with_same_msg",
                "two_commit_change_with_same_msg",
            )
        )
        self.assertFalse(
            _is_branch_changed(
                self.repo,
                "master",
                "single_commit_change_clone",
                "single_commit_change_clone",
            )
        )
        self.assertFalse(
            _is_branch_changed(
                self.repo,
                "master",
                "single_commit_change",
                "single_commit_change_clone",
            )
        )

    def test_split_change(self):
        # Double check that there are no source changes
        self.repo.heads.single_commit_change.checkout()
        self.assertFalse(self.repo.git.diff("two_commit_change"))

        # But the varying number of commits triggers a change detection
        self.assertTrue(
            _is_branch_changed(
                self.repo,
                "master",
                "single_commit_change",
                "two_commit_change",
            )
        )

    def test_split_duplicate_message_change(self):
        # Double check that there are no source changes
        self.repo.heads.single_commit_change.checkout()
        self.assertFalse(self.repo.git.diff("two_commit_change_with_same_msg"))

        self.assertTrue(
            _is_branch_changed(
                self.repo,
                "master",
                "single_commit_change",
                "two_commit_change_with_same_msg",
            )
        )


class TestEmailNotificationBody(unittest.TestCase):
    # Always show full diff on string match failures
    maxDiff = None

    def test_email_body_success(self):
        expected = read_fixture("test_email_body_success.golden")

        ctx = EmailBodyContext(
            status=Status.SUCCESS,
            submission_name="[bpf] Successfull patchset",
            patchwork_url="https://patchwork.com/success",
            github_url="https://github.com/success",
            inline_logs="",
        )
        body = furnish_ci_email_body(ctx)

        self.assertEqual(expected, body)

    def test_email_body_failure(self):
        inline_logs = read_fixture("test_inline_email_text_multiple.golden")
        expected = read_fixture("test_email_body_failure.golden")

        ctx = EmailBodyContext(
            status=Status.FAILURE,
            submission_name="[bpf] Failing patchset",
            patchwork_url="https://patchwork.com/failure",
            github_url="https://github.com/failure",
            inline_logs=inline_logs,
        )
        body = furnish_ci_email_body(ctx)

        self.assertEqual(expected, body)

    def test_email_body_conflict(self):
        expected = read_fixture("test_email_body_conflict.golden")

        ctx = EmailBodyContext(
            status=Status.CONFLICT,
            submission_name="[bpf-next] Conflicting patchset",
            patchwork_url="https://patchwork.com/conflict",
            github_url="https://github.com/conflict",
            inline_logs="",
        )
        body = furnish_ci_email_body(ctx)

        self.assertEqual(expected, body)


class TestEmailNotification(unittest.TestCase):
    # Fully expand all diffs
    maxDiff = None

    BOUNDARY = "================asdf"

    def test_allowlist_non_regex(self):
        allowlist = [
            re.compile("asdf@gmail.com"),
            re.compile("some.email@domain.xyz"),
        ]

        cases = [
            ("asdf@gmail.com", True),
            ("zzz@gmail.com", False),
            # No partial matches allows
            ("asdf@gmail.com.xyz", False),
            ("leading-asdf@gmail.com", False),
            ("some.email@domain.xyz", True),
            # False positives are allowed - this is a rollout mechanism
            ("somezemail@domain.xyz", True),
        ]

        for email, expected in cases:
            with self.subTest(msg=email):
                result = email_in_submitter_allowlist(email, allowlist)
                self.assertEqual(result, expected)

    def test_allowlist_regex_match(self):
        allowlist = [
            re.compile(r"^[a-gA-G].*"),
            re.compile(r"some.email@domain.xyz"),
        ]

        cases = [
            ("asdf@gmail.com", True),
            ("Asdf@gmail.com", True),
            ("gsdf@gmail.com", True),
            ("Gsdf@gmail.com", True),
            ("zzz@gmail.com", False),
            ("Zzz@gmail.com", False),
            ("some.email@domain.xyz", True),
        ]

        for email, expected in cases:
            with self.subTest(msg=email):
                result = email_in_submitter_allowlist(email, allowlist)
                self.assertEqual(result, expected)

    def test_email_submitter_in_allowlist(self):
        pw = get_default_pw_client()
        series = Series(pw, SERIES_DATA)
        config = EmailConfig(
            smtp_host="mail.example.com",
            smtp_port=465,
            smtp_user="bot-bpf-ci",
            smtp_from="bot+bpf-ci@example.com",
            smtp_to=["email1-to@example.com", "email2-to@example.com"],
            smtp_cc=["email1-cc@example.com", "email2-cc@example.com"],
            smtp_pass="super-secret-is-king",
            smtp_http_proxy="http://example.com:8080",
            submitter_allowlist=[
                re.compile("a-user@example.com"),
            ],
            ignore_allowlist=False,
        )
        expected_cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--ssl-reqd",
            "smtps://mail.example.com",
            "--mail-from",
            "bot+bpf-ci@example.com",
            "--user",
            f"bot-bpf-ci:super-secret-is-king",
            "--crlf",
            "--upload-file",
            "-",
            "--mail-rcpt",
            "email1-to@example.com",
            "--mail-rcpt",
            "email2-to@example.com",
            "--mail-rcpt",
            "a-user@example.com",
            "--mail-rcpt",
            "email1-cc@example.com",
            "--mail-rcpt",
            "email2-cc@example.com",
            "--proxy",
            "http://example.com:8080",
        ]
        expected_email = read_fixture("test_email_submitter_in_allowlist.golden")

        cmd, email = build_email(
            config,
            series,
            "[PATCH bpf] my subject",
            "my-id",
            "body body body",
            boundary=self.BOUNDARY,
        )
        self.assertEqual(expected_cmd, cmd)
        self.assertEqual(expected_email, email)

    def test_email_submitter_not_in_allowlist(self):
        pw = get_default_pw_client()
        series = Series(pw, SERIES_DATA)
        config = EmailConfig(
            smtp_host="mail.example.com",
            smtp_port=465,
            smtp_user="bot-bpf-ci",
            smtp_from="bot+bpf-ci@example.com",
            smtp_to=["email1-to@example.com", "email2-to@example.com"],
            smtp_cc=["email1-cc@example.com", "email2-cc@example.com"],
            smtp_pass="super-secret-is-king",
            smtp_http_proxy="http://example.com:8080",
            submitter_allowlist=[
                re.compile("email1-allow@example.com"),
                re.compile("email2-allow@example.com"),
            ],
            ignore_allowlist=False,
        )
        expected_cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--ssl-reqd",
            "smtps://mail.example.com",
            "--mail-from",
            "bot+bpf-ci@example.com",
            "--user",
            f"bot-bpf-ci:super-secret-is-king",
            "--crlf",
            "--upload-file",
            "-",
            "--mail-rcpt",
            "email1-to@example.com",
            "--mail-rcpt",
            "email2-to@example.com",
            "--mail-rcpt",
            "email1-cc@example.com",
            "--mail-rcpt",
            "email2-cc@example.com",
            "--proxy",
            "http://example.com:8080",
        ]
        expected_email = read_fixture("test_email_submitter_not_in_allowlist.golden")

        cmd, email = build_email(
            config, series, "my subject", "my-id", "body body", boundary=self.BOUNDARY
        )
        self.assertEqual(expected_cmd, cmd)
        self.assertEqual(expected_email, email)

    def test_email_submitter_not_in_allowlist_and_allowlist_disabled(self):
        pw = get_default_pw_client()
        series = Series(pw, SERIES_DATA)
        config = EmailConfig(
            smtp_host="mail.example.com",
            smtp_port=465,
            smtp_user="bot-bpf-ci",
            smtp_from="bot+bpf-ci@example.com",
            smtp_to=["email1-to@example.com", "email2-to@example.com"],
            smtp_cc=["email1-cc@example.com", "email2-cc@example.com"],
            smtp_pass="super-secret-is-king",
            smtp_http_proxy="http://example.com:8080",
            submitter_allowlist=[
                re.compile("email1-allow@example.com"),
                re.compile("email2-allow@example.com"),
            ],
            ignore_allowlist=True,
        )
        expected_cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--ssl-reqd",
            "smtps://mail.example.com",
            "--mail-from",
            "bot+bpf-ci@example.com",
            "--user",
            f"bot-bpf-ci:super-secret-is-king",
            "--crlf",
            "--upload-file",
            "-",
            "--mail-rcpt",
            "email1-to@example.com",
            "--mail-rcpt",
            "email2-to@example.com",
            "--mail-rcpt",
            "a-user@example.com",
            "--mail-rcpt",
            "email1-cc@example.com",
            "--mail-rcpt",
            "email2-cc@example.com",
            "--proxy",
            "http://example.com:8080",
        ]
        expected_email = read_fixture(
            "test_email_submitter_not_in_allowlist_and_allowlist_disabled.golden"
        )

        cmd, email = build_email(
            config,
            series,
            "[PATCH bpf-next] some-subject",
            "my-id",
            "zzzzz\nzz",
            boundary=self.BOUNDARY,
        )
        self.assertEqual(expected_cmd, cmd)
        self.assertEqual(expected_email, email)


class TestParsePrRef(unittest.TestCase):
    def test_parse_pr_ref_comprehensive(self):
        test_cases = [
            (
                "series/123456=>main",
                {"series": "series/123456", "series_id": 123456, "target": "main"},
            ),
            (
                "patch/789=>bpf-next",
                {"series": "patch/789", "series_id": 789, "target": "bpf-next"},
            ),
            ("series/42", {"series": "series/42", "series_id": 42}),
            ("series/abc=>target", {"series": "series/abc", "target": "target"}),
            (
                "series/999=>feature/branch-name",
                {
                    "series": "series/999",
                    "series_id": 999,
                    "target": "feature/branch-name",
                },
            ),
            ("", {"series": ""}),
            ("=>", {"series": "", "target": ""}),
            ("series", {"series": "series"}),
            ("=>target", {"series": "", "target": "target"}),
            (
                "series/123=>target=>extra",
                {"series": "series/123", "series_id": 123, "target": "target=>extra"},
            ),
            (
                "path/to/series/456=>target",
                {"series": "path/to/series/456", "target": "target"},
            ),
            ("series/abc123=>target", {"series": "series/abc123", "target": "target"}),
            ("series/123abc=>target", {"series": "series/123abc", "target": "target"}),
            (
                " series/123=>target ",
                {"series": " series/123", "series_id": 123, "target": "target "},
            ),
            (
                "series/1=>target",
                {"series": "series/1", "series_id": 1, "target": "target"},
            ),
            (
                "series/007=>target",
                {"series": "series/007", "series_id": 7, "target": "target"},
            ),
            (
                "series/000=>target",
                {"series": "series/000", "series_id": 0, "target": "target"},
            ),
            ("series/12a34=>target", {"series": "series/12a34", "target": "target"}),
            ("series/a1234=>target", {"series": "series/a1234", "target": "target"}),
            ("series/abc=>target", {"series": "series/abc", "target": "target"}),
            ("series/=>target", {"series": "series/", "target": "target"}),
            (
                "path/123/series/456=>target",
                {"series": "path/123/series/456", "target": "target"},
            ),
            ("series/123!@#=>target", {"series": "series/123!@#", "target": "target"}),
            (
                "série/123=>tärget",
                {"series": "série/123", "series_id": 123, "target": "tärget"},
            ),
            (
                "series/999999999999999999999=>target",
                {
                    "series": "series/999999999999999999999",
                    "series_id": 999999999999999999999,
                    "target": "target",
                },
            ),
            ("series/-123=>target", {"series": "series/-123", "target": "target"}),
            (
                "series/0=>target",
                {"series": "series/0", "series_id": 0, "target": "target"},
            ),
            ("series/123.45=>target", {"series": "series/123.45", "target": "target"}),
            ("series/1e5=>target", {"series": "series/1e5", "target": "target"}),
            ("\n\r\t", {"series": "\n\r\t"}),
            ("/" * 100, {"series": "/" * 100}),
        ]

        def validate_parsed_pr_ref(ref):
            self.assertIsInstance(ref, dict)
            self.assertIn("series", ref)
            self.assertIsInstance(ref["series"], str)
            if "target" in ref:
                self.assertIsInstance(ref["target"], str)
            if "series_id" in ref:
                self.assertIsInstance(ref["series_id"], int)
            valid_keys = {"series", "target", "series_id"}
            self.assertTrue(set(result.keys()).issubset(valid_keys))

        for test_input, expected in test_cases:
            with self.subTest(input=test_input):
                try:
                    result = parse_pr_ref(test_input)
                    validate_parsed_pr_ref(result)
                    self.assertEqual(result, expected)
                except Exception as e:
                    self.fail(
                        f"parse_pr_ref raised {type(e).__name__} for input '{test_input}': {e}"
                    )

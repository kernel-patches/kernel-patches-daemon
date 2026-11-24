# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import unittest
from unittest.mock import patch

import aiohttp
from aioresponses import aioresponses
from github.WorkflowJob import WorkflowJob
from kernel_patches_daemon.github_logs import (
    BpfGithubLogExtractor,
    DefaultGithubLogExtractor,
    extract_failed_steps,
    GithubFailedJobLog,
)
from tests.common.utils import read_fixture


class MockWorkflowJob(WorkflowJob):
    """Pretty hacky mock object where we only override the fields the code uses"""

    def __init__(self, name: str, conclusion: str, logs_url: str, html_url: str):
        self.__name: str = name
        self.__conclusion: str = conclusion
        self.__logs_url: str = logs_url
        self.__html_url: str = html_url

    @property
    def name(self) -> str:
        return self.__name

    @property
    def conclusion(self) -> str:
        return self.__conclusion

    def logs_url(self) -> str:
        return self.__logs_url

    @property
    def html_url(self) -> str:
        return self.__html_url


class TestBpfGithubLogs(unittest.IsolatedAsyncioTestCase):
    # Always show full diff on string match failures
    maxDiff = None

    @aioresponses()
    async def test_extract_some_failures(self, mocked: aioresponses):
        mocked.get("job1.com", status=200, body="job1")
        mocked.get("job2.com", status=200, body="job2")
        mocked.get("job3.com", status=200, body="job3")

        jobs = [
            MockWorkflowJob(
                "x86_64-gcc / test / suite1 on x86_64 with gcc",
                "failure",
                "job1.com",
                "https://job1.com",
            ),
            MockWorkflowJob(
                "aarch64-gcc / test / suite2 on aarch64 with gcc",
                "success",
                "job2.com",
                "https://job2.com",
            ),
            MockWorkflowJob(
                "s390x-llvm-17 / test / suite3 on s390x with llvm-17",
                "failure",
                "job3.com",
                "https://job3.com",
            ),
        ]

        extractor = BpfGithubLogExtractor()
        sessions = []
        new_session = aiohttp.ClientSession

        def track_session(*args, **kwargs):
            sessions.append(new_session(*args, **kwargs))
            return sessions[-1]

        with patch("aiohttp.ClientSession", side_effect=track_session):
            logs = await extractor.extract_failed_logs(jobs)

        self.assertTrue(sessions and all(session.closed for session in sessions))

        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0].suite, "suite1")
        self.assertEqual(logs[0].arch, "x86_64")
        self.assertEqual(logs[0].compiler, "gcc")
        self.assertEqual(logs[0].log, "job1")
        self.assertEqual(logs[1].suite, "suite3")
        self.assertEqual(logs[1].arch, "s390x")
        self.assertEqual(logs[1].compiler, "llvm-17")
        self.assertEqual(logs[1].log, "job3")

    @aioresponses()
    async def test_extract_none(self, mocked: aioresponses):
        mocked.get("job1.com", status=200, body="job1")
        mocked.get("job2.com", status=200, body="job2")

        jobs = [
            MockWorkflowJob(
                "x86_64-gcc / test / suite1 on x86_64 with gcc",
                "pending",
                "job1.com",
                "https://job2.com",
            ),
            MockWorkflowJob(
                "aarch64-gcc / build / suite2 on aarch64 with gcc",
                "success",
                "job2.com",
                "https://job2.com",
            ),
        ]

        extractor = BpfGithubLogExtractor()
        logs = await extractor.extract_failed_logs(jobs)
        self.assertEqual(len(logs), 0)

    @aioresponses()
    async def test_extract_partial_invalid_names(self, mocked: aioresponses):
        mocked.get("job1.com", status=200, body="job1")
        mocked.get("job2.com", status=200, body="job2")

        jobs = [
            MockWorkflowJob(
                "valid / valid / suite1 zzz x86_64 with gcc",
                "failure",
                "job1.com",
                "https://job1.com",
            ),
            MockWorkflowJob(
                "valid / valid / build for aarch64 with gcc",
                "failure",
                "job2.com",
                "https://job2.com",
            ),
        ]

        extractor = BpfGithubLogExtractor()
        logs = await extractor.extract_failed_logs(jobs)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].suite, "build")
        self.assertEqual(logs[0].arch, "aarch64")
        self.assertEqual(logs[0].compiler, "gcc")

    @aioresponses()
    async def test_extract_invalid_names_no_error(self, mocked: aioresponses):
        mocked.get("job1.com", status=200, body="job1")
        mocked.get("job2.com", status=200, body="job2")

        jobs = [
            MockWorkflowJob(
                "valid / valid / zzzzzzzzz", "pending", "job1.com", "https://job1.com"
            ),
            MockWorkflowJob(
                "valid / valid / this is-not a valid job",
                "success",
                "job2.com",
                "https://job2.com",
            ),
        ]

        extractor = BpfGithubLogExtractor()
        logs = await extractor.extract_failed_logs(jobs)

        # None of the jobs should have parsed. We expect no errors regardless.
        self.assertEqual(len(logs), 0)

    @aioresponses()
    async def test_extract_names_no_matrix(self, mocked: aioresponses):
        """
        This tests the case where somehow the matrix parameters are removed.
        In case github does something funny or the overall matrix configuration is changed.
        """
        mocked.get("job1.com", status=200, body="job1")
        mocked.get("job2.com", status=200, body="job2")
        mocked.get("job3.com", status=200, body="job3")

        jobs = [
            MockWorkflowJob(
                "suite1 on x86_64 with gcc", "failure", "job1.com", "https://job1.com"
            ),
            MockWorkflowJob(
                "suite3 on s390x with llvm-17",
                "failure",
                "job3.com",
                "https://job3.com",
            ),
        ]

        extractor = BpfGithubLogExtractor()
        logs = await extractor.extract_failed_logs(jobs)

        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0].suite, "suite1")
        self.assertEqual(logs[0].arch, "x86_64")
        self.assertEqual(logs[0].compiler, "gcc")
        self.assertEqual(logs[0].log, "job1")
        self.assertEqual(logs[1].suite, "suite3")
        self.assertEqual(logs[1].arch, "s390x")
        self.assertEqual(logs[1].compiler, "llvm-17")
        self.assertEqual(logs[1].log, "job3")

    def test_inline_email_text_none(self):
        input = read_fixture("job_log_no_failures")
        expected = read_fixture("test_inline_email_text_none.golden")

        extractor = BpfGithubLogExtractor()
        output = extractor.generate_inline_email_text(
            [
                GithubFailedJobLog(
                    suite="test_progs",
                    arch="x86_64",
                    compiler="llvm-17",
                    log=input,
                    url="https://job1.com",
                )
            ]
        )

        self.assertEqual(expected, output)

    def test_inline_email_text_single(self):
        input = read_fixture("job_log_two_failures")
        expected = read_fixture("test_inline_email_text_single.golden")

        extractor = BpfGithubLogExtractor()
        output = extractor.generate_inline_email_text(
            [
                GithubFailedJobLog(
                    suite="test_progs",
                    arch="x86_64",
                    compiler="gcc",
                    log=input,
                    url="https://job1.com",
                )
            ]
        )

        self.assertEqual(expected, output)

    def test_inline_email_text_multiple(self):
        input1 = read_fixture("job_log_two_failures")
        input2 = read_fixture("job_log_one_failure")
        expected = read_fixture("test_inline_email_text_multiple.golden")

        extractor = BpfGithubLogExtractor()
        output = extractor.generate_inline_email_text(
            [
                GithubFailedJobLog(
                    suite="test_progs",
                    arch="x86_64",
                    compiler="gcc",
                    log=input1,
                    url="https://job1.com",
                ),
                GithubFailedJobLog(
                    suite="test_progs_no_alu32",
                    arch="x86_64",
                    compiler="gcc",
                    log=input2,
                    url="https://job2.com",
                ),
            ]
        )

        self.assertEqual(expected, output)


class TestDefaultGithubLogs(unittest.IsolatedAsyncioTestCase):
    # Always show full diff on string match failures
    maxDiff = None

    def test_extract_failed_steps(self):
        log = read_fixture("job_log_failed_steps")
        expected = read_fixture("test_extract_failed_steps.golden")

        self.assertEqual(expected, extract_failed_steps(log) + "\n")

    def test_extract_failed_steps_none(self):
        log = read_fixture("job_log_no_failed_steps")

        self.assertEqual("", extract_failed_steps(log))

    def test_extract_failed_steps_empty_log(self):
        self.assertEqual("", extract_failed_steps(""))

    def test_extract_failed_steps_error_without_step(self):
        """An `##[error]` outside of any step has no output to report."""
        log = "2026-01-01T00:00:00.0Z ##[error]Process completed with exit code 1."

        self.assertEqual("", extract_failed_steps(log))

    def test_extract_failed_steps_reports_each_step_once(self):
        """Several `##[error]` lines in one step must not duplicate it."""
        log = "\n".join(
            [
                "2026-01-01T00:00:00.0Z ##[group]Run make",
                "2026-01-01T00:00:01.0Z ##[endgroup]",
                "2026-01-01T00:00:02.0Z ##[error]first problem",
                "2026-01-01T00:00:03.0Z ##[error]Process completed with exit code 1.",
            ]
        )

        self.assertEqual(
            "Step 'make' failed:\nfirst problem",
            extract_failed_steps(log),
        )

    @aioresponses()
    async def test_extract_failed_logs(self, mocked: aioresponses):
        log = read_fixture("job_log_failed_steps")
        mocked.get("job1.com", status=200, body=log)
        mocked.get("job2.com", status=200, body=log)

        jobs = [
            MockWorkflowJob(
                "Host tests (x86_64)", "failure", "job1.com", "https://job1.com"
            ),
            MockWorkflowJob("VM tests", "success", "job2.com", "https://job2.com"),
        ]

        extractor = DefaultGithubLogExtractor()
        logs = await extractor.extract_failed_logs(jobs)

        self.assertEqual(len(logs), 1)
        # Job names need no parsing, unlike the bpf ones.
        self.assertEqual(logs[0].name, "Host tests (x86_64)")
        self.assertEqual(logs[0].url, "https://job1.com")
        self.assertIn("✗ build of VMA tests failed", logs[0].log)
        self.assertNotIn("Cleaning up orphan processes", logs[0].log)

    @aioresponses()
    async def test_extract_failed_logs_download_failure(self, mocked: aioresponses):
        """A job whose log cannot be fetched is still reported, without logs."""
        mocked.get("job1.com", status=404)

        jobs = [
            MockWorkflowJob("VM tests", "failure", "job1.com", "https://job1.com"),
        ]

        extractor = DefaultGithubLogExtractor()
        logs = await extractor.extract_failed_logs(jobs)

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].name, "VM tests")
        self.assertEqual(logs[0].log, "")

    def test_inline_email_text(self):
        log = read_fixture("job_log_failed_steps")
        expected = read_fixture("test_default_inline_email_text.golden")

        extractor = DefaultGithubLogExtractor()
        output = extractor.generate_inline_email_text(
            [
                GithubFailedJobLog(
                    name="Host tests (x86_64)",
                    log=extract_failed_steps(log),
                    url="https://job1.com",
                ),
                GithubFailedJobLog(
                    name="VM tests",
                    log="",
                    url="https://job2.com",
                ),
            ]
        )

        self.assertEqual(expected, output)

    def test_inline_email_text_none(self):
        extractor = DefaultGithubLogExtractor()

        self.assertEqual("", extractor.generate_inline_email_text([]))

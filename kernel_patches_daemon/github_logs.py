# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import asyncio
import io
import logging
import re
from abc import ABC, abstractmethod
from typing import Final, List, Optional, Sequence, Tuple

import aiohttp
from github.WorkflowJob import WorkflowJob
from kernel_patches_daemon.status import gh_conclusion_to_status, Status

logger: logging.Logger = logging.getLogger(__name__)


# Prefix that the GitHub Actions runner puts in front of every log line.
LOG_TIMESTAMP: Final[re.Pattern] = re.compile(r"^\S+Z ")
# The runner brackets the output of every `run:` step with these markers and
# reports a failing step with `##[error]`. None of this is workflow specific.
STEP_START: Final[re.Pattern] = re.compile(r"^##\[group\]Run (?P<command>.*)$")
STEP_PREAMBLE_END: Final[str] = "##[endgroup]"
STEP_ERROR: Final[re.Pattern] = re.compile(r"^##\[error\]")
# The error the runner appends to every failing step. It carries no
# information that the step output does not already convey.
STEP_EXIT_ERROR: Final[re.Pattern] = re.compile(
    r"^##\[error\]Process completed with exit code \d+\.?$"
)


def strip_log_timestamps(log: str) -> List[str]:
    """Split a raw job log into lines, dropping the runner timestamps."""
    return [LOG_TIMESTAMP.sub("", line.rstrip()) for line in log.splitlines()]


def _failed_step_bounds(lines: Sequence[str]) -> List[Tuple[int, int]]:
    """Locate the failing steps as (start, end) indices into `lines`."""
    starts = [i for i, line in enumerate(lines) if STEP_START.match(line)]

    bounds: List[Tuple[int, int]] = []
    for end, line in enumerate(lines):
        if not STEP_ERROR.match(line):
            continue
        # `##[error]` may also be emitted in the middle of a step, so attribute
        # it to the step it appeared in and report every step just once.
        preceding = [start for start in starts if start < end]
        if not preceding:
            continue
        start = preceding[-1]
        if not bounds or bounds[-1][0] != start:
            bounds.append((start, end))

    return bounds


def extract_failed_steps(log: str) -> str:
    """Extract the output of the steps that failed from a raw job log.

    A failing step spans from the `##[group]Run ...` line that introduces it to
    the `##[error]` line that concludes it. Everything else is checkout and
    runner setup noise, which is by far the bulk of a job log.
    """
    lines = strip_log_timestamps(log)

    steps = []
    for start, end in _failed_step_bounds(lines):
        # pyrefly: ignore  # missing-attribute
        command = STEP_START.match(lines[start]).group("command")
        # Skip the preamble in which the runner echoes the command it is about
        # to run along with the environment it uses.
        body_start = start + 1
        for i in range(start, end):
            if lines[i] == STEP_PREAMBLE_END:
                body_start = i + 1
                break

        body = [
            STEP_ERROR.sub("", line)
            for line in lines[body_start : end + 1]
            if not STEP_EXIT_ERROR.match(line)
        ]
        steps.append(f"Step '{command}' failed:\n" + "\n".join(body).strip())

    return "\n\n".join(steps).strip()


class GithubFailedJobLog:
    def __init__(
        self,
        log: str,
        url: str,
        name: Optional[str] = None,
        suite: str = "",
        arch: str = "",
        compiler: str = "",
    ):
        self._suite: str = suite
        self._arch: str = arch
        self._compiler: str = compiler
        self._log: str = log
        self._url: str = url
        self._name: Optional[str] = name

    @property
    def suite(self) -> str:
        return self._suite

    @property
    def arch(self) -> str:
        return self._arch

    @property
    def compiler(self) -> str:
        return self._compiler

    @property
    def log(self) -> str:
        return self._log

    @property
    def url(self) -> str:
        return self._url

    @property
    def name(self) -> str:
        if self._name is not None:
            return self._name
        return f"{self._suite}-{self._arch}-{self._compiler}"


class GithubLogExtractor(ABC):
    async def _download_job_log(
        self, session: aiohttp.ClientSession, job: WorkflowJob
    ) -> str:
        url = job.logs_url()
        async with session.get(url) as resp:
            logger.info(f"Getting logs for {job.name} at {url}")
            if resp.ok:
                return await resp.text()

            logger.warning(f"Failed to GET logs for {job.name}: HTTP {resp.status}")
            return ""

    @abstractmethod
    async def _extract_job_log(
        self, session: aiohttp.ClientSession, job: WorkflowJob
    ) -> Optional[GithubFailedJobLog]:
        """
        Extract the log of `job` if it failed, or return None otherwise.
        """
        raise NotImplementedError

    async def extract_failed_logs(
        self, jobs: Sequence[WorkflowJob]
    ) -> List[GithubFailedJobLog]:
        """
        Given a list of workflow jobs, `jobs`, filter out all the successful
        jobs. For the remaining failed jobs, pull out output logs. The logs
        will be minimally filtered. For maximal filtering, see
        generate_inline_email_text().
        """
        # Read the proxy from the environment.
        async with aiohttp.ClientSession(trust_env=True) as session:
            tasks = [
                asyncio.create_task(self._extract_job_log(session, job)) for job in jobs
            ]
            results = await asyncio.gather(*tasks)
        return [result for result in results if result is not None]

    @abstractmethod
    def generate_inline_email_text(self, logs: Sequence[GithubFailedJobLog]) -> str:
        """
        Given a list of failed job logs, return a (possibly multi-line) string
        suitable to be embedded in the body of a notification email. The text
        will try to be conservative -- high signal to email length is important.
        """
        raise NotImplementedError


class DefaultGithubLogExtractor(GithubLogExtractor):
    """Extractor relying solely on the structure of GitHub Actions job logs.

    The runner brackets each step and flags the failing ones, so the output of
    a failed step can be recovered without knowing anything about the workflow
    that produced it.
    """

    async def _extract_job_log(
        self, session: aiohttp.ClientSession, job: WorkflowJob
    ) -> Optional[GithubFailedJobLog]:
        if gh_conclusion_to_status(job.conclusion) != Status.FAILURE:
            return None

        log = await self._download_job_log(session, job)
        return GithubFailedJobLog(
            name=job.name,
            log=extract_failed_steps(log),
            url=job.html_url,
        )

    def generate_inline_email_text(self, logs: Sequence[GithubFailedJobLog]) -> str:
        if not logs:
            return ""

        text = "Failed jobs:\n"
        for log in logs:
            text += f"{log.name}: {log.url}\n"

        for log in logs:
            if not log.log:
                continue
            text += f"\nFailure log for {log.name}:\n{log.log}\n"

        return text


class BpfGithubLogExtractor(GithubLogExtractor):
    TEST_PROGS_PREFIX: Final[str] = "test_progs"
    JOB_LOG_ERROR_START: Final[re.Pattern] = re.compile(".*##\\[group\\].*Error:.*")
    JOB_LOG_ERROR_END: Final[str] = "##[endgroup]"
    JOB_LOG_ERROR_MARKER: Final[str] = "##[error]"

    async def _extract_job_log(
        self, session: aiohttp.ClientSession, job: WorkflowJob
    ) -> Optional[GithubFailedJobLog]:
        status = gh_conclusion_to_status(job.conclusion)
        if status != Status.FAILURE:
            return None

        # NB: the job name is load bearing.
        #
        # Example names:
        #   x86_64-gcc / test (test_progs_no_alu32, false, 360) / test_progs_no_alu32 on x86_64 with gcc
        #   x86_64-llvm-17 / build / build for x86_64 with llvm-17
        job_name = [s.strip() for s in job.name.split("/")][-1]
        parts = job_name.split()
        if len(parts) != 5 or parts[1] not in ["on", "for"] or parts[3] != "with":
            logger.error(f"Invalid job name: '{job_name}', did workflow change?")
            return None

        suite = parts[0]
        arch = parts[2]
        compiler = parts[4]

        log = await self._download_job_log(session, job)

        return GithubFailedJobLog(
            suite=suite,
            arch=arch,
            compiler=compiler,
            log=log,
            url=job.html_url,
        )

    def _parse_out_test_progs_failure(self, log: str) -> str:
        # Avoid keeping a duplicate copy of a possibly large file in-memory
        log_file = io.StringIO(log)

        # Simple state machine track if we're looking at an error message
        in_error = False
        error_log = []

        # Example lines:
        # 2024-05-21T19:13:46.4638076Z ##[group][1;31mError:[0m #53 cgrp_local_storage
        # 2024-05-21T19:08:07.9400261Z ##[error]#53 cgrp_local_storage
        # 2024-05-21T19:08:07.9400806Z cgrp2_local_storage:PASS:join_cgroup /cgrp_local_storage 0 nsec
        # 2024-05-21T19:08:07.9401619Z ##[endgroup]
        for line in log_file:
            line = line.strip()

            if self.JOB_LOG_ERROR_START.match(line):
                in_error = True
                continue

            if self.JOB_LOG_ERROR_END in line:
                in_error = False
                continue

            if in_error:
                # Remove timestamp
                line = line[line.index(" ") + 1 :]

                # Remove ##[error] prefix on first line
                if line.startswith(self.JOB_LOG_ERROR_MARKER):
                    line = line[len(self.JOB_LOG_ERROR_MARKER) :]
                    if not line:
                        continue

                # pyrefly: ignore  # bad-argument-type
                error_log.append(line)

        return "\n".join(error_log)

    def generate_inline_email_text(self, logs: Sequence[GithubFailedJobLog]) -> str:
        """
        Given a list of failed job logs, return a (possibly multi-line) string
        suitable to be embedded in the body of a notification email. The text
        will try to be conservative -- high signal to email length is important.
        """
        if not logs:
            return ""

        # Render header with links to failed jobs
        text = "Failed jobs:\n"
        for log in logs:
            text += f"{log.name}: {log.url}\n"

        # Render first test_progs failure
        for log in logs:
            if not log.suite.startswith(self.TEST_PROGS_PREFIX):
                continue

            error = self._parse_out_test_progs_failure(log.log)
            if not error:
                continue

            text += f"\nFirst test_progs failure ({log.name}):\n"
            text += f"{error}\n"
            break

        return text

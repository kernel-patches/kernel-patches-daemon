# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import asyncio
import logging
import signal
import threading
import time
from typing import Callable, Dict, Final, Optional

from github import GithubException
from kernel_patches_daemon.config import KPDConfig
from kernel_patches_daemon.github_sync import GithubSync
from kernel_patches_daemon.patchwork import Patchwork, PatchworkUnavailable
from pyre_extensions import none_throws

logger: logging.Logger = logging.getLogger(__name__)

DEFAULT_LOOP_DELAY: Final[int] = 120
MAX_LOOP_DELAY: Final[int] = 3600
_HEARTBEAT_INTERVAL_SECS: Final[int] = 60


class KernelPatchesWorker:
    def __init__(
        self,
        kpd_config: KPDConfig,
        labels_cfg: Dict[str, str],
        metrics_logger: Optional[Callable] = None,
        loop_delay: int = DEFAULT_LOOP_DELAY,
    ) -> None:
        self.project: str = kpd_config.patchwork.project
        self.kpd_config = kpd_config
        self.labels_cfg = labels_cfg
        self.loop_delay: Final[int] = loop_delay
        self.metrics_logger = metrics_logger
        if self.metrics_logger is None:
            logger.info(
                "Metrics logging is disabled; run metrics will not be submitted"
            )
        # Unlike GithubSync, the Patchwork client lives as long as the daemon.
        self.pw = Patchwork(
            server=kpd_config.patchwork.base_url,
            search_patterns=kpd_config.patchwork.search_patterns,
            lookback_in_days=kpd_config.patchwork.lookback,
            auth_token=kpd_config.patchwork.token,
            max_concurrent_requests=kpd_config.patchwork.max_concurrent_requests,
            max_requests_per_sec=kpd_config.patchwork.max_requests_per_sec,
        )
        self.github_sync_worker: Optional[GithubSync] = None

    def reset_github_sync(self) -> None:
        # Clear previous worker and attempt to assign a new one
        self.github_sync_worker = None
        self.github_sync_worker = GithubSync(
            kpd_config=self.kpd_config, labels_cfg=self.labels_cfg, pw=self.pw
        )

    async def submit_metrics(self, stats: dict[str, int | float]) -> None:
        if self.metrics_logger is None:
            # Metrics are intentionally disabled (see __init__); nothing to do.
            return
        try:
            self.metrics_logger(self.project, stats)
            logger.info("Submitted run metrics into metrics logger")
        except Exception:
            logger.exception(
                "Failed to submit run metrics into metrics logger", exc_info=True
            )

    async def run(self) -> None:
        # Cycles in a row that failed because Patchwork was unavailable.
        patchwork_failures = 0
        while True:
            started = time.monotonic()
            requests_sent = self.pw.requests_sent
            stats = {
                "runs_attempted": 1,
                "runs_successful": 0,
                "runs_failed": 0,
                "patchwork_failed": 0,
                "github_failed": 0,
                "github_http_status": 0,
                "other_failed": 0,
            }
            try:
                self.reset_github_sync()
                await none_throws(self.github_sync_worker).sync_patches()
                stats["runs_successful"] = 1
                patchwork_failures = 0
            except Exception as e:
                stats["runs_failed"] = 1
                exception_name = type(e).__name__
                stats[f"unhandled_{exception_name}"] = 1
                logger.exception(
                    "Unhandled exception in KernelPatchesWorker.run()", exc_info=True
                )
                if isinstance(e, PatchworkUnavailable):
                    stats["patchwork_failed"] = 1
                    patchwork_failures += 1
                elif isinstance(e, GithubException):
                    stats["github_failed"] = 1
                    stats["github_http_status"] = e.status or 0
                    patchwork_failures = 0
                else:
                    stats["other_failed"] = 1
                    patchwork_failures = 0
            stats = {
                **(self.github_sync_worker.stats if self.github_sync_worker else {}),
                **stats,
            }
            await self.submit_metrics(stats)
            # Wait longer while Patchwork is overloaded or refuses KPD.
            delay = min(self.loop_delay * 2**patchwork_failures, MAX_LOOP_DELAY)
            logger.info(
                f"Cycle took {time.monotonic() - started:.0f} s and sent "
                f"{self.pw.requests_sent - requests_sent} Patchwork requests; "
                f"waiting {delay} s before the next one"
            )
            await asyncio.sleep(delay)


class KernelPatchesDaemon:
    def __init__(
        self,
        kpd_config: KPDConfig,
        labels_cfg: Dict[str, str],
        metrics_logger: Optional[Callable] = None,
    ) -> None:
        self._stopping_lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self.worker = KernelPatchesWorker(
            kpd_config=kpd_config,
            labels_cfg=labels_cfg,
            metrics_logger=metrics_logger,
        )

    def stop(self) -> None:
        if not self._task:
            logger.info("Kernel Patches Daemon was never started")

        with self._stopping_lock:
            logger.info("Stopping Kernel Patches Daemon...")
            none_throws(self._task).cancel()
            logger.info("Kernel Patches Daemon stopped")

    async def _publish_heartbeat(self) -> None:
        while True:
            # Keep liveness visible during sync work and retry backoff. These
            # rows are excluded from completed-attempt and PR-count queries.
            await self.worker.submit_metrics(
                {"heartbeat": 1, "runs_attempted": 0, "runs_successful": 0}
            )
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECS)

    async def start_async(self) -> None:
        logger.info("Starting Kernel Patches Daemon...")

        loop = asyncio.get_running_loop()

        # pyrefly: ignore  # bad-argument-type
        loop.add_signal_handler(signal.SIGTERM, self.stop)
        # pyrefly: ignore  # bad-argument-type
        loop.add_signal_handler(signal.SIGINT, self.stop)

        self._task = asyncio.create_task(self.worker.run())
        heartbeat = asyncio.create_task(self._publish_heartbeat())
        try:
            await asyncio.gather(self._task, return_exceptions=True)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.worker.pw.close()

    def start(self) -> None:
        asyncio.run(self.start_async())

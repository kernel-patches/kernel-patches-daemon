# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import asyncio
import logging
import signal
import threading
from typing import Callable, Dict, Final, Optional

from kernel_patches_daemon.config import KPDConfig
from kernel_patches_daemon.github_sync import GithubSync
from pyre_extensions import none_throws

logger: logging.Logger = logging.getLogger(__name__)

DEFAULT_LOOP_DELAY: Final[int] = 120


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
        self.github_sync_worker: GithubSync = GithubSync(
            kpd_config=self.kpd_config, labels_cfg=self.labels_cfg
        )

    def reset_github_sync(self) -> bool:
        try:
            self.github_sync_worker = GithubSync(
                kpd_config=self.kpd_config, labels_cfg=self.labels_cfg
            )
            return True
        except Exception:
            logger.exception(
                "Unhandled exception while creating GithubSync object",
                exc_info=True,
            )
            return False

    async def submit_metrics(self) -> None:
        if self.metrics_logger is None:
            # pyrefly: ignore  # deprecated
            logger.warn(
                "Not submitting run metrics because metrics logger is not configured"
            )
            return
        try:
            self.metrics_logger(self.project, self.github_sync_worker.stats)
            logger.info("Submitted run metrics into metrics logger")
        except Exception:
            logger.exception(
                "Failed to submit run metrics into metrics logger", exc_info=True
            )

    async def run(self) -> None:
        while True:
            ok = self.reset_github_sync()
            if not ok:
                logger.error(
                    "Most likely something went wrong connecting to GitHub or Patchwork. Skipping this iteration without submitting metrics."
                )
                continue
            try:
                await self.github_sync_worker.sync_patches()
                self.github_sync_worker.increment_counter("runs_successful")
            except Exception as e:
                self.github_sync_worker.increment_counter("runs_failed")
                exception_name = type(e).__name__
                self.github_sync_worker.increment_counter(
                    f"unhandled_{exception_name}", create=True
                )
                logger.exception(
                    "Unhandled exception in KernelPatchesWorker.run()", exc_info=True
                )
            await self.submit_metrics()
            logger.info(f"Waiting for {self.loop_delay} seconds before next run...")
            await asyncio.sleep(self.loop_delay)


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

    async def start_async(self) -> None:
        logger.info("Starting Kernel Patches Daemon...")

        loop = asyncio.get_event_loop()

        # pyrefly: ignore  # bad-argument-type
        loop.add_signal_handler(signal.SIGTERM, self.stop)
        # pyrefly: ignore  # bad-argument-type
        loop.add_signal_handler(signal.SIGINT, self.stop)

        self._task = asyncio.create_task(self.worker.run())
        await asyncio.gather(self._task, return_exceptions=True)

    def start(self) -> None:
        asyncio.run(self.start_async())

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
        self.github_sync_worker = GithubSync(
            kpd_config=kpd_config, labels_cfg=labels_cfg
        )
        self.loop_delay: Final[int] = loop_delay
        self.metrics_logger = metrics_logger

    async def run_once(self) -> None:
        await self.github_sync_worker.sync_patches()
        logger.info("Submitting run metrics into metrics logger")
        if self.metrics_logger:
            self.metrics_logger(self.project, self.github_sync_worker.stats)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
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

        loop.add_signal_handler(signal.SIGTERM, self.stop)
        loop.add_signal_handler(signal.SIGINT, self.stop)

        self._task = asyncio.create_task(self.worker.run())
        await asyncio.gather(self._task, return_exceptions=True)

    def start(self) -> None:
        asyncio.run(self.start_async())

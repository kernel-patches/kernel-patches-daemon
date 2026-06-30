import json
import os
import runpy
import sys
import tempfile
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

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


class TestNoMetricsOption(unittest.TestCase):
    """Exercise the kernel_patches_daemon.__main__ entry point as-is.

    The daemon is mocked out so start() does not block; we then inspect the
    real `metrics_logger` global the entry point computed from the CLI args.
    """

    def setUp(self) -> None:
        self._tmpfiles: List[str] = []

    def tearDown(self) -> None:
        for path in self._tmpfiles:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _write_tmp_json(self, data: Any) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        self._tmpfiles.append(path)
        return path

    def _run_main(self, extra_argv: List[str]) -> Dict[str, Any]:
        cfg_path = self._write_tmp_json(TEST_CONFIG)
        labels_path = self._write_tmp_json({})
        argv = [
            "kpd",
            "--config",
            cfg_path,
            "--label-colors",
            labels_path,
        ] + extra_argv
        with (
            patch.object(sys, "argv", argv),
            patch("kernel_patches_daemon.daemon.KernelPatchesDaemon"),
        ):
            return runpy.run_module(
                "kernel_patches_daemon.__main__", run_name="__main__"
            )

    def test_no_metrics_disables_metrics_logger(self) -> None:
        """When --no-metrics is passed, metrics_logger is None."""
        ns = self._run_main(["--no-metrics"])
        self.assertIsNone(ns["metrics_logger"])

    def test_metrics_logger_enabled_by_default(self) -> None:
        """Without --no-metrics, a metrics_logger callable is configured."""
        with (
            patch("opentelemetry.sdk.metrics.MeterProvider"),
            patch("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader"),
            patch("opentelemetry.sdk.metrics.export.ConsoleMetricExporter"),
            patch("opentelemetry.metrics.set_meter_provider"),
        ):
            ns = self._run_main([])
        self.assertIsNotNone(ns["metrics_logger"])
        self.assertTrue(callable(ns["metrics_logger"]))


if __name__ == "__main__":
    unittest.main()

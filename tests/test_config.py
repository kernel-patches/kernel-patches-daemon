# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import json
import re
import unittest
from dataclasses import dataclass
from typing import Dict, List, Union
from unittest.mock import mock_open, patch

from kernel_patches_daemon.config import (
    BranchConfig,
    DEFAULT_EMAIL_CONTACT_EMAIL,
    DEFAULT_EMAIL_CONTACT_NAME,
    EmailConfig,
    GithubAppAuthConfig,
    InvalidConfig,
    KPDConfig,
    PatchworksConfig,
    PRCommentsForwardingConfig,
)
from kernel_patches_daemon.status import Status
from tests.common.utils import read_fixture


def load_kpd_config(filename: str) -> Dict[str, Union[str, int, bool, Dict]]:
    raw_json = read_fixture(filename)
    return json.loads(raw_json)


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()

    def test_invalid_tag_to_branch_mapping(self) -> None:
        """
        Tests that if a tag is mapped to a branch that doesn't exist, an exception is raised.
        """
        # Load a valid config
        kpd_config_json = load_kpd_config("kpd_config.json")

        @dataclass
        class TestCase:
            name: str
            tag_to_branch_mapping: Dict[str, List[str]]

        test_cases = [
            TestCase(
                name="tag mapped to a non-existent branch",
                tag_to_branch_mapping={"tag": ["non_existent_branch"]},
            ),
            TestCase(
                name="tag mapped to some branches that exists and some that don't",
                tag_to_branch_mapping={
                    "tag": ["app_auth_key", "non_existent_branch", "oauth"]
                },
            ),
            TestCase(
                name="__DEFAULT__ mapped to a non-existent branch",
                tag_to_branch_mapping={"__DEFAULT__": ["non_existent_branch"]},
            ),
            TestCase(
                name="__DEFAULT__ mapped to some branches that exists and some that don't",
                tag_to_branch_mapping={
                    "__DEFAULT__": ["app_auth_key", "non_existent_branch", "oauth"]
                },
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                conf = kpd_config_json.copy()
                conf["tag_to_branch_mapping"] = case.tag_to_branch_mapping
                with self.assertRaises(InvalidConfig):
                    KPDConfig.from_json(conf)

    def test_valid_tag_to_branch_mapping(self) -> None:
        """
        Tests combinaisons of valid tag_to_branch_mapping setup.
        """
        # Load a valid config
        kpd_config_json = load_kpd_config("kpd_config.json")

        @dataclass
        class TestCase:
            name: str
            tag_to_branch_mapping: Dict[str, List[str]]

        test_cases = [
            TestCase(
                name="single tag mapped to a valid branch",
                tag_to_branch_mapping={"tag": ["oauth"]},
            ),
            TestCase(
                name="multiple tags mapped to valid branches",
                tag_to_branch_mapping={
                    "tag1": ["app_auth_key"],
                    "tag2": ["oauth"],
                },
            ),
            TestCase(
                name="multiple tags mapped to same branch",
                tag_to_branch_mapping={
                    "tag1": ["oauth"],
                    "tag2": ["oauth"],
                },
            ),
            TestCase(
                name="tags mapped to multiple valid branch",
                tag_to_branch_mapping={
                    "tag1": ["oauth", "app_auth_key"],
                    "tag2": ["oauth"],
                },
            ),
            TestCase(
                name="__DEFAULT__ mapped to a valid branch",
                tag_to_branch_mapping={"__DEFAULT__": ["app_auth_key"]},
            ),
            TestCase(
                name="__DEFAULT__ mapped to multiple valid branches",
                tag_to_branch_mapping={"__DEFAULT__": ["app_auth_key", "oauth"]},
            ),
            TestCase(
                name="tags and __DEFAULT__ mapped to multiple valid branches",
                tag_to_branch_mapping={
                    "tag1": ["app_auth_key"],
                    "tag2": ["app_auth_key", "oauth"],
                    "__DEFAULT__": ["app_auth_key"],
                },
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                conf = kpd_config_json.copy()
                conf["tag_to_branch_mapping"] = case.tag_to_branch_mapping
                KPDConfig.from_json(conf)

    def test_valid(self) -> None:
        kpd_config_json = load_kpd_config("kpd_config.json")

        with patch(
            "builtins.open", mock_open(read_data="TEST_KEY_FILE_CONTENT")
        ) as mock_file:
            config = KPDConfig.from_json(kpd_config_json)
            mock_file.assert_called_with("/key.pem")

        expected_config = KPDConfig(
            version=3,
            patchwork=PatchworksConfig(
                base_url="patchwork.kernel.org",
                project="unittest",
                user="unittest_user",
                token="unittest_token",
                search_patterns=[{"key": "value"}],
                lookback=1,
            ),
            email=EmailConfig(
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
                pr_comments_forwarding=PRCommentsForwardingConfig(
                    enabled=True,
                    always_cc=["bpf-ci-test@example.com"],
                    always_reply_to_author=False,
                    commenter_allowlist=["kpd-bot[bot]"],
                    recipient_denylist=[re.compile(".*@vger.kernel.org")],
                    recipient_allowlist=[],
                    body_preprocessor_func=None,
                ),
                email_ignore_workflows=[],
            ),
            tag_to_branch_mapping={"tag": ["app_auth_key_path"]},
            branches={
                "app_auth_key": BranchConfig(
                    github_app_auth=GithubAppAuthConfig(
                        app_id=123, installation_id=456, private_key="TEST_KEY_CONTENT"
                    ),
                    repo="https://repo.git",
                    upstream_repo="https://upstream.git",
                    upstream_branch="upstream_branch",
                    ci_repo="https://cirepo.git",
                    ci_branch="ci_branch",
                    github_oauth_token=None,
                ),
                "app_auth_key_path": BranchConfig(
                    repo="https://repo.git",
                    upstream_repo="https://upstream.git",
                    upstream_branch="upstream_branch",
                    ci_repo="https://cirepo.git",
                    ci_branch="ci_branch",
                    github_app_auth=GithubAppAuthConfig(
                        app_id=123,
                        installation_id=456,
                        private_key="TEST_KEY_FILE_CONTENT",
                    ),
                    github_oauth_token=None,
                ),
                "oauth": BranchConfig(
                    repo="https://repo.git",
                    upstream_repo="https://upstream.git",
                    upstream_branch="upstream_branch",
                    ci_repo="https://cirepo.git",
                    ci_branch="ci_branch",
                    github_app_auth=None,
                    github_oauth_token="TEST_OAUTH_TOKEN",
                ),
            },
            base_directory="/repos",
        )
        self.assertEqual(config, expected_config)


class TestEmailConfig(unittest.TestCase):
    """Tests for EmailConfig parsing."""

    def test_contact_defaults(self):
        """Contact details retain the Meta defaults when not configured."""
        cfg = EmailConfig.from_json(
            {"host": "smtp.example.com", "user": "u", "from": "f@x.com", "pass": "p"}
        )
        self.assertEqual(cfg.contact_name, DEFAULT_EMAIL_CONTACT_NAME)
        self.assertEqual(cfg.contact_email, DEFAULT_EMAIL_CONTACT_EMAIL)

    def test_contact_overrides(self):
        """Configured contact details override the Meta defaults."""
        cfg = EmailConfig.from_json(
            {
                "host": "smtp.example.com",
                "user": "u",
                "from": "f@x.com",
                "pass": "p",
                "contact_name": "Example Kernel CI team",
                "contact_email": "kernel-ci@example.com",
            }
        )
        self.assertEqual(cfg.contact_name, "Example Kernel CI team")
        self.assertEqual(cfg.contact_email, "kernel-ci@example.com")

    def test_email_ignore_workflows_default(self):
        """email_ignore_workflows defaults to empty list when not in config."""
        cfg = EmailConfig.from_json(
            {"host": "smtp.example.com", "user": "u", "from": "f@x.com", "pass": "p"}
        )
        self.assertEqual(cfg.email_ignore_workflows, [])

    def test_email_ignore_workflows_parsed(self):
        """email_ignore_workflows is correctly parsed as compiled regex patterns."""
        cfg = EmailConfig.from_json(
            {
                "host": "smtp.example.com",
                "user": "u",
                "from": "f@x.com",
                "pass": "p",
                "email_ignore_workflows": ["AI Code Review", "Lint"],
            }
        )
        self.assertEqual(len(cfg.email_ignore_workflows), 2)
        for pat in cfg.email_ignore_workflows:
            self.assertIsInstance(pat, re.Pattern)
        self.assertIsNotNone(cfg.email_ignore_workflows[0].search("AI Code Review"))
        self.assertIsNotNone(cfg.email_ignore_workflows[1].search("Lint"))

    def test_email_ignore_workflows_regex(self):
        """email_ignore_workflows supports regex patterns."""
        cfg = EmailConfig.from_json(
            {
                "host": "smtp.example.com",
                "user": "u",
                "from": "f@x.com",
                "pass": "p",
                "email_ignore_workflows": ["AI.*Review"],
            }
        )
        self.assertEqual(len(cfg.email_ignore_workflows), 1)
        self.assertIsNotNone(cfg.email_ignore_workflows[0].search("AI Code Review"))
        self.assertIsNone(cfg.email_ignore_workflows[0].search("Build and Test"))

    def test_email_ignore_workflows_empty_list(self):
        """email_ignore_workflows accepts an explicit empty list."""
        cfg = EmailConfig.from_json(
            {
                "host": "smtp.example.com",
                "user": "u",
                "from": "f@x.com",
                "pass": "p",
                "email_ignore_workflows": [],
            }
        )
        self.assertEqual(cfg.email_ignore_workflows, [])

    # --- notify_on ---------------------------------------------------------

    BASE_EMAIL_JSON = {
        "host": "smtp.example.com",
        "user": "u",
        "from": "f@x.com",
        "pass": "p",
    }

    def test_notify_on_default_is_all_emailable(self):
        """notify_on defaults to all emailable statuses when not configured."""
        cfg = EmailConfig.from_json(dict(self.BASE_EMAIL_JSON))
        self.assertEqual(
            cfg.notify_on,
            {Status.SUCCESS, Status.FAILURE, Status.CONFLICT},
        )

    def test_notify_on_subset(self):
        """notify_on can select a subset of statuses."""
        cfg = EmailConfig.from_json(
            {**self.BASE_EMAIL_JSON, "notify_on": ["failure", "conflict"]}
        )
        self.assertEqual(cfg.notify_on, {Status.FAILURE, Status.CONFLICT})

    def test_notify_on_empty_list_disables_all(self):
        """An explicit empty notify_on list disables all email notifications."""
        cfg = EmailConfig.from_json({**self.BASE_EMAIL_JSON, "notify_on": []})
        self.assertEqual(cfg.notify_on, set())

    def test_notify_on_unknown_status_rejected(self):
        """An unknown notify_on status is rejected."""
        with self.assertRaises(InvalidConfig):
            EmailConfig.from_json({**self.BASE_EMAIL_JSON, "notify_on": ["bogus"]})

    def test_notify_on_non_emailable_status_rejected(self):
        """A valid but non-emailable status (pending/skipped) is rejected."""
        with self.assertRaises(InvalidConfig):
            EmailConfig.from_json({**self.BASE_EMAIL_JSON, "notify_on": ["pending"]})

    def test_notify_on_non_list_rejected(self):
        """notify_on must be a list rather than another iterable."""
        with self.assertRaises(InvalidConfig):
            EmailConfig.from_json(
                {**self.BASE_EMAIL_JSON, "notify_on": {"success": False}}
            )

    def test_notify_on_non_string_entry_rejected(self):
        """notify_on entries must be status names."""
        with self.assertRaises(InvalidConfig):
            EmailConfig.from_json({**self.BASE_EMAIL_JSON, "notify_on": [1]})

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import json

import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set


logger = logging.getLogger(__name__)

SERIES_TARGET_SEPARATOR = "=>"
SERIES_ID_SEPARATOR = "/"


class UnsupportedConfigVersion(ValueError):
    def __init__(self, version: int) -> None:
        super().__init__(f"Unsupported config version {version}")


class InvalidConfig(ValueError):
    pass


@dataclass
class GithubAppAuthConfig:
    app_id: int
    installation_id: int
    private_key: str

    @classmethod
    def _read_private_key(cls, private_key_path: os.PathLike) -> str:
        try:
            with open(private_key_path) as f:
                return f.read()
        except OSError as e:
            raise InvalidConfig(
                f"Failed to read Github AppAuth private key {private_key_path}"
            ) from e

    @classmethod
    def from_json(cls, json: Dict) -> "GithubAppAuthConfig":
        private_key_config = json.keys() & {
            "private_key",
            "private_key_path",
        }
        if len(private_key_config) != 1:
            raise InvalidConfig(
                "Github AppAuth config expect to have private_key OR private_key_path"
            )

        private_key = json.get("private_key")
        if private_key_path := json.get("private_key_path"):
            private_key = cls._read_private_key(private_key_path)

        if not private_key:
            raise InvalidConfig("Failed to load Github AppAuth private key")

        try:
            return cls(
                app_id=json["app_id"],
                installation_id=json["installation_id"],
                private_key=private_key,
            )
        except KeyError as e:
            raise InvalidConfig(e)


@dataclass
class BranchConfig:
    repo: str
    upstream_repo: str
    upstream_branch: str
    ci_repo: str
    ci_branch: str
    github_oauth_token: Optional[str]
    github_app_auth: Optional[GithubAppAuthConfig]

    @classmethod
    def from_json(cls, json: Dict) -> "BranchConfig":
        github_app_auth_config: Optional[GithubAppAuthConfig] = None
        if app_auth_json := json.get("github_app_auth"):
            try:
                github_app_auth_config = GithubAppAuthConfig.from_json(app_auth_json)
            except InvalidConfig as e:
                logger.warning(f"Failed to parse Github AppAuth config: {e}")

        return cls(
            repo=json["repo"],
            upstream_repo=json["upstream"],
            upstream_branch=json.get("upstream_branch", "master"),
            ci_repo=json["ci_repo"],
            ci_branch=json["ci_branch"],
            github_oauth_token=json.get("github_oauth_token", None),
            github_app_auth=github_app_auth_config,
        )


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_from: str
    smtp_pass: str
    smtp_to: List[str]
    smtp_cc: List[str]
    smtp_http_proxy: Optional[str]
    # List of email addresses of patch submitters to whom we send email
    # notifications only for *their* very submission. This attribute is meant to
    # be temporary while the email notification feature is being rolled out.
    # Once we send email notifications to all patch submitters it can be
    # removed.
    submitter_allowlist: List[re.Pattern]
    # Ignore the `submitter_allowlist` entries and send emails to all patch
    # submitters, unconditionally.
    ignore_allowlist: bool

    @classmethod
    def from_json(cls, json: Dict) -> "EmailConfig":
        return cls(
            smtp_host=json["host"],
            smtp_port=json.get("port", 465),
            smtp_user=json["user"],
            smtp_from=json["from"],
            smtp_pass=json["pass"],
            smtp_to=json.get("to", []),
            smtp_cc=json.get("cc", []),
            smtp_http_proxy=json.get("http_proxy", None),
            submitter_allowlist=[
                re.compile(pattern) for pattern in json.get("submitter_allowlist", [])
            ],
            ignore_allowlist=json.get("ignore_allowlist", False),
        )


@dataclass
class PatchworksConfig:
    base_url: str
    project: str
    search_patterns: List[Dict]
    lookback: int
    user: Optional[str]
    token: Optional[str]

    @classmethod
    def from_json(cls, json: Dict) -> "PatchworksConfig":
        return cls(
            base_url=json["server"],
            project=json["project"],
            search_patterns=json["search_patterns"],
            lookback=json["lookback"],
            user=json.get("api_username", None),
            token=json.get("api_token", None),
        )


@dataclass
class KPDConfig:
    version: int
    patchwork: PatchworksConfig
    email: Optional[EmailConfig]
    branches: Dict[str, BranchConfig]
    tag_to_branch_mapping: Dict[str, List[str]]
    base_directory: str

    @classmethod
    def from_json(cls, json: Dict) -> "KPDConfig":
        try:
            version = int(json["version"])
        except (KeyError, IndexError) as e:
            raise InvalidConfig("Invalid KPD config") from e

        # Currently we only support config v3
        if version != 3:
            raise UnsupportedConfigVersion(version)

        tag_to_branch_mapping = json["tag_to_branch_mapping"]
        branch_names = json["branches"].keys()

        for branches in tag_to_branch_mapping.values():
            for branch in branches:
                if branch not in branch_names:
                    raise InvalidConfig(
                        f"Branch *{branch}* in `tag_to_branch_mapping` is not defined in `branches`"
                    )

        return cls(
            version=3,
            tag_to_branch_mapping=tag_to_branch_mapping,
            patchwork=PatchworksConfig.from_json(json["patchwork"]),
            email=EmailConfig.from_json(json["email"]) if "email" in json else None,
            branches={
                name: BranchConfig.from_json(json_config)
                for name, json_config in json["branches"].items()
            },
            base_directory=json["base_directory"],
        )

    @classmethod
    def from_file(cls, path: os.PathLike) -> "KPDConfig":
        with open(path) as f:
            json_config = json.load(f)
            return cls.from_json(json_config)

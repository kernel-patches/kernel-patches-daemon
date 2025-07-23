# KPD Configuration Guide

## Overview

This document provides detailed configuration options for Kernel Patches Daemon (KPD).

## Configuration File Structure

KPD uses a JSON configuration file with the following main sections:

- `version`: Configuration version (currently 3)
- `patchwork`: Patchwork server settings
- `branches`: Branch-specific settings
- `tag_to_branch_mapping`: Mapping of tags to branches
- `base_directory`: Base directory for repositories
- `email`: Optional email configuration

## Branch Configuration

Each branch in the `branches` section supports the following options:

- `repo`: Target GitHub repository URL
- `upstream`: Upstream repository URL
- `upstream_branch`: Upstream branch name (default: "master")
- `ci_repo`: CI repository URL
- `ci_branch`: CI branch name
- `github_oauth_token`: GitHub authentication token
- `github_app_auth`: GitHub App authentication (preferred over token)

## Github personal tokens

You may use [Github personal token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)
to authenticate KPD to github, but using [Github App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app) is preferrable.

When using a GH app, it needs to have the following read and write access:
- Content (write to repo)
- Pull request (create PRs)
- Workflow

## Example Configuration

See the `configs/` directory for complete configuration examples.

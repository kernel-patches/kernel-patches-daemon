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
- `mirror_dir`: Optional mirror directory for bandwidth optimization
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
- `mirror_fallback_repo`: Optional fallback repository for mirror setup

## Mirror Setup

To optimize network bandwidth usage, KPD supports using local git mirrors:

### Basic Mirror Configuration

Set the `mirror_dir` configuration option to specify where git mirrors are located:

```json
{
  "mirror_dir": "/path/to/mirrors/"
}
```

### Mirror Fallback Repository

For each branch, you can specify a `mirror_fallback_repo` as an alternative path within the mirror directory:

```json
{
  "branches": {
    "xfs-branch": {
      "repo": "https://github.com/example/xfs.git",
      "upstream": "https://git.example.com/xfs-linux.git",
      "mirror_fallback_repo": "linux.git",
      // ... other options
    }
  }
}
```

### How Mirror Works

1. KPD looks for a mirror directory based on the upstream repository basename (e.g., `$mirror_dir/xfs-linux.git`)
2. If the primary mirror doesn't exist, it checks for the fallback path (e.g., `$mirror_dir/linux.git`)
3. When cloning repositories, KPD uses `--reference-if-able` to reference the available mirror
4. This significantly reduces bandwidth usage and speeds up clones by reusing existing git objects

### Benefits

- Saves considerable network bandwidth
- Faster repository operations
- Ideal for corporate environments with NFS-mounted git repositories
- Reduces storage requirements for multiple repository clones

## Github personal tokens

You may use [Github personal token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)
to authenticate KPD to github, but using [Github App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app) is preferrable.

When using a GH app, it needs to have the following read and write access:
- Content (write to repo)
- Pull request (create PRs)
- Workflow

## Example Configuration

See the `configs/` directory for complete configuration examples.

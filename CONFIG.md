# KPD Configuration Guide

## Overview

This document provides detailed configuration options for Kernel Patches Daemon (KPD). For comprehensive deployment guidance including GitHub organization setup and security considerations, see the [kdevops KPD integration documentation](https://github.com/linux-kdevops/kdevops/blob/main/docs/kernel-ci/kernel-ci-kpd.md).

## Configuration File Structure

KPD uses a JSON configuration file with the following main sections:

- `version`: Configuration version (currently 3)
- `patchwork`: Patchwork server settings
- `branches`: Branch-specific settings
- `tag_to_branch_mapping`: Mapping of tags to branches
- `base_directory`: Base directory for repositories
- `mirror_dir`: Optional mirror directory for bandwidth optimization
- `email`: Optional email configuration

### Patchwork Configuration

The `patchwork` section includes:
- `server`: Patchwork server hostname (e.g., "patchwork.kernel.org")
- `project`: Project name or ID (can be string or integer)
- `lookback`: Number of days to look back for patches (default: 7, recommended: 14-21 for kernel projects)
- `search_patterns`: List of search criteria for filtering patches
- `api_username`: Username for Patchwork API authentication
- `api_token`: API token for write operations

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

## Authentication

### GitHub Apps (Recommended)

KPD supports GitHub Apps for authentication, which provides better security and more granular permissions compared to personal access tokens. A GitHub App requires the following permissions:

**Repository Permissions:**
- **Content**: Read and write access to repository content (for creating branches and updating files)
- **Pull requests**: Read and write access to manage pull requests
- **Workflows**: Read access to workflow runs and write access to trigger workflows

**Organization Permissions (if applicable):**
- **Members**: Read access to organization members (for team-based access control)

### GitHub Personal Tokens (Legacy)

While you may use [GitHub personal tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token) to authenticate KPD, GitHub Apps are strongly recommended for production deployments.

### Security Best Practices

- **Disable webhooks** on the GitHub App for enhanced security
- **Limit repository access** to only the repositories that need KPD integration
- **Use environment variables** or secure secret management for tokens and keys
- **Regularly rotate** authentication credentials
- **Monitor access logs** and audit permissions periodically

For detailed GitHub App setup instructions, including security considerations and organizational policies, refer to the [kdevops documentation](https://github.com/linux-kdevops/kdevops/blob/main/docs/kernel-ci/kernel-ci-kpd.md).

## Configuration Validation

KPD validates configuration on startup and will report specific errors for:
- Missing required fields
- Invalid JSON syntax
- Malformed URLs or paths
- Conflicting branch configurations
- Authentication credential issues

## Troubleshooting Common Configuration Issues

**Authentication Failures:**
- Verify GitHub App installation and permissions
- Check token expiration dates
- Ensure repository access is properly configured

**Mirror Setup Issues:**
- Verify mirror directory paths exist and are readable
- Check git repository validity in mirror directories
- Ensure proper file permissions on mirror directories

**Branch Configuration Problems:**
- Validate upstream and CI repository URLs
- Verify branch names exist in respective repositories
- Check for conflicting tag-to-branch mappings

**Patch Detection Issues:**
- Check lookback period is appropriate (7 days may be too short for kernel projects)
- Confirm search patterns match the patches you expect to process
- Use `--dry-run-list-candidates-only` to debug patch detection

## Example Configuration

See the `configs/` directory for complete configuration examples demonstrating various deployment scenarios.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kernel Patches Daemon (KPD) is a Python service that connects Patchwork with GitHub repositories for automated CI testing. It watches Patchwork for new patch series, creates GitHub pull requests, and syncs CI results back to Patchwork.

## Common Development Commands

### Setup and Installation
```bash
# Install poetry (if not installed)
pip install --user poetry

# Setup virtual environment and install dependencies
python -m venv .venv
poetry install
```

### Testing
```bash
# Run all tests
poetry run python -m unittest

# Run specific test file
poetry run python -m unittest tests.test_<filename>
```

### Code Quality
```bash
# Format code with black
poetry run black .

# Run linting (flake8)
poetry run flake8
```

### Running the Daemon
```bash
# Run with configuration
poetry run python -m kernel_patches_daemon --config <config_path> --label-color configs/labels.json

# Dry run mode: List candidate patches only (useful for debugging)
poetry run python -m kernel_patches_daemon --config <config_path> --label-color configs/labels.json --dry-run-list-candidates-only

# Purge all PRs and branches (destructive)
poetry run python -m kernel_patches_daemon --config <config_path> --action purge
```

### Docker
```bash
# Build Docker image
docker-compose build

# Start KPD service
docker-compose up

# Use pre-built image
docker pull ghcr.io/kernel-patches/kernel-patches-daemon:latest
```

## Architecture Overview

### Core Components

- **`daemon.py`**: Main daemon orchestration and worker management
- **`github_sync.py`**: Core sync logic between Patchwork and GitHub
- **`patchwork.py`**: Patchwork API interactions and patch management
- **`github_connector.py`**: GitHub API operations (PRs, branches, etc.)
- **`branch_worker.py`**: Per-branch processing logic
- **`config.py`**: Configuration management and validation
- **`status.py`**: CI status reporting back to Patchwork
- **`utils.py`**: Common utilities and helpers

### Key Workflows

1. **Patch Sync**: Monitor Patchwork → Create GitHub PRs → Update with CI results
2. **Mirror Support**: Use local git mirrors to optimize bandwidth via `--reference-if-able`
3. **Branch Management**: Handle multiple branches with different upstream/CI repositories
4. **Email Notifications**: Optional email alerts for patch authors

### Configuration

- Main config: JSON file with version 3 format
- Key sections: `patchwork`, `branches`, `tag_to_branch_mapping`, `mirror_dir`
- Authentication: GitHub Apps preferred over personal tokens
- Mirror setup: Uses `mirror_dir` with fallback repositories for bandwidth optimization
- Lookback period: Recommended 14-21 days for kernel projects (default 7 may be too short)

### Testing Structure

- Unit tests in `tests/` directory
- Mock data in `tests/data/` and `tests/fixtures/`
- Common test utilities in `tests/common/`
- Golden files for email template testing

## Development Notes

### Debugging and Troubleshooting
- Use `--dry-run-list-candidates-only` for debugging patch detection issues
- Check project ID type consistency (should be strings in config, auto-converted to int)
- Verify lookback period is appropriate for the project's patch lifecycle

### Code Style
- Uses `black` formatter (enforced)
- Python 3.10+ required
- Type hints encouraged (uses `pyre` type checker)
- Async/await patterns for network operations

### Dependencies
- `aiohttp`: Async HTTP client
- `PyGithub`: GitHub API interactions
- `GitPython`: Git operations
- `opentelemetry`: Metrics and observability
- `poetry`: Dependency management

### Contribution Requirements
- All commits must use the Signed-off-by tag (DCO)
- Tests must pass
- Code must be formatted with black
- PRs should target `main` branch

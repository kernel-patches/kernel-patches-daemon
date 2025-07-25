# Kernel Patches Daemon (KPD)

Kernel Patches Daemon (KPD) is a service connecting [Patchwork](https://github.com/getpatchwork/patchwork) with a GitHub repository, primarily for the purpose of running automated continuous integration (CI) testing via [GitHub Actions](https://github.com/features/actions).

## What KPD Does

KPD automates the kernel patch development workflow by:
- Monitoring Patchwork for new patch series
- Creating GitHub pull requests for each patch series
- Running automated CI testing via GitHub Actions
- Reporting test results back to Patchwork as checks
- Sending optional email notifications to patch authors
- Managing branch lifecycle and cleanup

This enables kernel subsystem maintainers to provide automated feedback to
patch contributors without manual intervention, significantly improving the
development workflow and code quality.

## Origins and Use Cases

KPD was originally developed at Meta to facilitate automated testing of the [BPF subsystem](https://docs.cilium.io/en/latest/reference-guides/bpf/index.html) of the [Linux Kernel](https://kernel.org/) (see [BPF CI](https://github.com/kernel-patches/bpf/actions/workflows/test.yml)).

The tool is designed for:
- **Kernel subsystem maintainers** who want to automate patch testing
- **Organizations** implementing structured kernel development workflows
- **Teams** requiring granular access control and security for kernel contributions
- **Projects** needing integration between traditional mailing list workflows and modern CI/CD systems

## Talks about KPD

There have been a number of talks at various Linux-related conferences about KPD, see the slide decks for an introduction and overview:
- ["KPD: Connect LKML to GitHub"](https://github.com/user-attachments/files/21110162/KPD_.Connect.LKML.to.GitHub.pdf) (Automated Testing Summit 2025)
- ["Get Started with KPD"](https://github.com/user-attachments/files/21110192/Get.Started.with.KPD.pdf) (2024)
- ["How BPF CI works?"](http://oldvger.kernel.org/bpfconf2022_material/lsfmmbpf2022-bpf-ci.pdf) (LSF/MM/BPF 2022)

## Integration and Deployment

For comprehensive deployment guidance, including GitHub organization setup, security considerations, and access control best practices, see the [kdevops KPD integration documentation](https://github.com/linux-kdevops/kdevops/blob/main/docs/kernel-ci/kernel-ci-kpd.md). The kdevops project provides detailed instructions for:

- Setting up GitHub Apps with proper permissions
- Configuring organizational security policies
- Managing authentication tokens and keys securely
- Implementing access control for kernel development teams

## Security Considerations

KPD requires careful security configuration:
- Use a **private, secure system** to host the daemon
- Configure GitHub organization with **strict access controls**
- Set repository base permissions to **"No access"** by default
- Create dedicated teams for kernel development access
- Regularly audit GitHub App permissions and access

## Configuration

See [CONFIG.md](CONFIG.md) for more details.

## Building
```
# Install poetry
pip install --user poetry

# Setup virtualenv
python -m venv .venv
poetry install

# Test
poetry run python -m unittest
```

## Running

### Normal Operation
```
poetry run python -m kernel_patches_daemon --config <config_path> --label-color configs/labels.json
```

### Debugging and Testing
```bash
# Dry run mode: List candidate patches without processing them
poetry run python -m kernel_patches_daemon --config <config_path> --label-color configs/labels.json --dry-run-list-candidates-only

# Purge all existing PRs and branches (destructive operation)
poetry run python -m kernel_patches_daemon --config <config_path> --action purge
```

The dry-run mode is particularly useful for:
- Debugging patch detection issues
- Verifying configuration changes
- Understanding what patches KPD would process
- Testing with new patchwork instances

## Docker

Kernel Patches Daemon is available as pre-build image:

```
$ docker pull ghcr.io/kernel-patches/kernel-patches-daemon:latest
```

To build Kernel Patches Daemon with [Docker](https://docs.docker.com/engine/install):

1. Install [Docker](https://docs.docker.com/engine/install) and [docker-compose](https://docs.docker.com/compose/install/)
2. Build image
```
$ docker-compose build
```
3. Start KPD service
```
$ docker-compose up
```

## AI-Assisted Development

KPD embraces AI-assisted development to accelerate feature development and debugging. The project provides comprehensive guidance for using generative AI tools effectively:

- **[CLAUDE.md](CLAUDE.md)**: Guidelines for using Claude Code with this repository
- **[PROMPTS.md](PROMPTS.md)**: Example prompts and case studies showing successful AI-assisted development patterns

### Getting Started with Claude Code

Claude Code can help with debugging complex issues, adding new features, and understanding the codebase. Here are some effective prompting strategies:

**For debugging:**
```
I'm having trouble with KPD not detecting patches from [patchwork instance].
Here's my configuration: [paste config]. When I run the dry-run command,
I see [describe output]. Can you help debug this issue?
```

**For feature development:**
```
I want to add [feature description] to KPD. The feature should [describe requirements].
Looking at the existing codebase, what's the best approach to implement this?
```

**For code understanding:**
```
Can you explain how KPD's patch synchronization works? I'm particularly interested
in [specific aspect] and how it interacts with [related component].
```

See [PROMPTS.md](PROMPTS.md) for detailed examples and successful development patterns.

## CONTRIBUTING
See the [CONTRIBUTING](CONTRIBUTING.md) file for how to help out.

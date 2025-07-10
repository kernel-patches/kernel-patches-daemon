# Kernel Patches Daemon (KPD)

Kernel Patches Daemon (KPD) is a service connecting [Patchwork](https://github.com/getpatchwork/patchwork) with a GitHub repository, primarily for the puprose of running automated continuous integration (CI) testing via [GitHub Actions](https://github.com/features/actions).

KPD watches Patchwork for new patch series and keeps them in sync with pull requests for a specified repository.
It also updates series checks at Patchwork with CI workflow results, and can send email notifications to the authors of a patch.

KPD was originally developed at Meta in order to facilitate automated testing  of [BPF subsystem](https://docs.cilium.io/en/latest/reference-guides/bpf/index.html) of the [Linux Kernel](https://kernel.org/) (see [BPF CI](https://github.com/kernel-patches/bpf/actions/workflows/test.yml)).

There have been a number of talks at various Linux-related conferences about KPD, see the slide decks for an introduction and overview: 
- ["KPD: Connect LKML to GitHub"](https://github.com/user-attachments/files/21110162/KPD_.Connect.LKML.to.GitHub.pdf) (Automated Testing Summit 2025)
- ["Get Started with KPD"](https://github.com/user-attachments/files/21110192/Get.Started.with.KPD.pdf) (2024)
- ["How BPF CI works?"](http://oldvger.kernel.org/bpfconf2022_material/lsfmmbpf2022-bpf-ci.pdf) (LSF/MM/BPF 2022)

Also [kdevops](https://github.com/linux-kdevops/kdevops) project has great documentation with [a page on KPD integration](https://github.com/linux-kdevops/kdevops/blob/main/docs/kernel-ci/kernel-ci-kpd.md).

## Configuration

See an example of the configuration files in `configs/` directory.

You may use [Github personal token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)
to authenticate KPD to github, but using [Github App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/about-authentication-with-a-github-app) is preferrable.

When using a GH app, it needs to have the following read and write access:
- Content (write to repo)
- Pull request (create PRs)
- Workflow

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

### Mirror setup

To make more efficient use of network bandwidth consider having a mirror of your target git tree
under /mirror/ or something like that and set the configuration attribute "mirror_dir" variable to the
path where to find possible git trees.

If your git tree is a linux clone set the "linux_clone" to true. In that case, in case your
target exact basename repo is not in in the mirror path, for example {{ mirror_dir }}/linux-subsystem.git
then the extra fallback path of {{ mirror_dir }}/linux.git will be used as a reference target.

A reference target mirror path is only used if it exists. The mirror takes effect by leveraging
the git clone --reference option when cloning. Using this can save considerable bandwidth and
space, allowing kpd to run on thing guests on a corporate environment with for example an NFS
mount for local git trees on a network.

## Running
```
poetry run python -m kernel_patches_daemon --config <config_path> --label-color configs/labels.json
```

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

## CONTRIBUTING
See the [CONTRIBUTING](CONTRIBUTING.md) file for how to help out.

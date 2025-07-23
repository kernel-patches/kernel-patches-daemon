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

#!/usr/bin/env bash
# Package setup for disposable GitHub-hosted Ubuntu runners.
set -euo pipefail
if (($# == 0)); then
  echo 'usage: install-ubuntu-packages.sh <package> ...' >&2
  exit 2
fi

# The Azure mirror has repeatedly stalled on GCC and media package downloads.
# Keep the runner's suites and Ubuntu signature verification unchanged.
sudo sed -Ei 's|https?://azure.archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' /etc/apt/apt-mirrors.txt
apt_options=(-o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
timeout --kill-after=15s 2m sudo apt-get "${apt_options[@]}" update
timeout --kill-after=15s 5m sudo apt-get "${apt_options[@]}" install -y --no-install-recommends -- "$@"

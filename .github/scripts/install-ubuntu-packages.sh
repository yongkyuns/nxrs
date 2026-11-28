#!/usr/bin/env bash
# Package setup for disposable GitHub-hosted Ubuntu runners.
set -euo pipefail
if (($# == 0)); then
  echo 'usage: install-ubuntu-packages.sh <package> ...' >&2
  exit 2
fi

# Hosted Azure and regional EC2 mirrors have stalled on package downloads.
# Keep the runner's suites and Ubuntu signature verification unchanged.
sudo sed -Ei 's@^https?://(azure|[a-z0-9-]+\.ec2)\.archive\.ubuntu\.com/ubuntu([[:space:]/]|$)@https://archive.ubuntu.com/ubuntu\2@' /etc/apt/apt-mirrors.txt
# These jobs install Ubuntu packages only. Unrelated preinstalled vendor feeds
# can fail (for example, Microsoft's 403) before any requested package is read.
# Keep the runner's Ubuntu suites, mirrors and Signed-By settings intact.
sudo test -s /etc/apt/sources.list.d/ubuntu.sources
apt_empty_parts=$(mktemp -d)
trap 'rmdir -- "$apt_empty_parts"' EXIT
apt_options=(-o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30
             -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources
             -o "Dir::Etc::sourceparts=$apt_empty_parts")
timeout --kill-after=15s 2m sudo apt-get "${apt_options[@]}" update
timeout --kill-after=15s 5m sudo apt-get "${apt_options[@]}" install -y --no-install-recommends -- "$@"

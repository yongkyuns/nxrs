#!/usr/bin/env python3
"""Install qualification packages only on disposable GitHub-hosted Ubuntu 24.04.

Use an invocation-local, signed official HTTPS source list rather than waiting on
an unreachable image-default mirror. No persistent apt source file is rewritten.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SOURCE = '''Types: deb
URIs: https://archive.ubuntu.com/ubuntu
Suites: noble noble-updates noble-backports noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
'''


def hosted():
    if (sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        raise ValueError('disposable GitHub-hosted Linux required')
    release = dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID','').strip('"') != 'ubuntu' or release.get('VERSION_ID','').strip('"') != '24.04':
        raise ValueError('Ubuntu 24.04 required')
    if not Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg').is_file():
        raise ValueError('Ubuntu signed archive keyring required')


def install(packages):
    # Refuse a personal host before creating files or invoking any command.
    hosted()
    if not 1<=len(packages)<=64 or any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,127}',p) for p in packages):
        raise ValueError('invalid package selection')
    with tempfile.TemporaryDirectory(prefix='nxrs-ci-apt-') as directory:
        sources = Path(directory)/'ubuntu.sources'
        sources.write_text(SOURCE)
        options = ['-o','Dir::Etc::sourcelist='+str(sources),'-o','Dir::Etc::sourceparts=-',
                   '-o','Acquire::Retries=1','-o','Acquire::http::Timeout=30','-o','Acquire::https::Timeout=30',
                   '-o','APT::Update::Error-Mode=any']
        subprocess.run(['sudo','-n','apt-get',*options,'update'],check=True,timeout=240)
        subprocess.run(['sudo','-n','apt-get',*options,'install','-y','--no-install-recommends',*packages],
                       check=True,timeout=360)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packages',nargs='+')
    args=parser.parse_args()
    try:
        install(args.packages)
    except (ValueError,OSError,subprocess.SubprocessError):
        parser.exit(1,'Qualification package setup failed; no compilation or execution pass is implied.\n')


if __name__=='__main__': main()

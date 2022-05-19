"""
Utility functions for hook scripts.
"""

import sys
import os
import subprocess

from subprocess import CalledProcessError
from typing import Tuple, Optional

def complain(message):
    sys.stderr.flush()
    sys.stderr.write(message)
    sys.stderr.write('\n')
    sys.stderr.flush()

def announce(message):
    sys.stderr.flush()
    sys.stderr.write(message)
    sys.stderr.write('\n')
    sys.stderr.flush()

def get_current_commit() -> str:
    """
    Get the currently checked-out commit.
    """
    return subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode('utf-8').strip()

# We have a cache for mypy results so we can compute them in advance.
CACHE_DIR = '.mypy_result_cache'

def write_cache(commit: str, result: bool, log: str) -> None:
    """
    Save the given status and log to the cache for the given commit.
    """

    os.makedirs(CACHE_DIR, exist_ok=True)
    basename = os.path.join(CACHE_DIR, commit)
    if os.path.exists(basename + '.fail.txt'):
        os.unlink(basename + '.fail.txt')
    if os.path.exists(basename + '.success.txt'):
        os.unlink(basename + '.success.txt')
    fullname = basename + ('.success.txt' if result else '.fail.txt')
    with open(fullname, 'w') as f:
        f.write(log)

def read_cache(commit: str) -> Tuple[Optional[bool], Optional[str]]:
    """
    Read the status and log from the cache for the given commit.
    """

    status = None
    log = None

    basename = os.path.join(CACHE_DIR, commit)
    fullname = None
    if os.path.exists(basename + '.fail.txt'):
        # We have a cached failure.
        fullname = basename + '.fail.txt'
        status = False
    elif os.path.exists(basename + '.success.txt'):
        # We have a cached success
        fullname = basename + '.success.txt'
        status = True
    if fullname:
        log = open(fullname).read()
    return status, log

def check_to_cache(local_object) -> Tuple[bool, str]:
    """
    Type-check current commit and save result to cache. Return status and log.
    """

    try:
        # As a hook we know we're in the project root when running.
        mypy_output = subprocess.check_output(['make', 'mypy'], stderr=subprocess.STDOUT)
        log = mypy_output.decode('utf-8')
        # If we get here it passed
        write_cache(local_object, True, log)
        return True, log
    except CalledProcessError as e:
        # It did not work.
        log = e.output.decode('utf-8')
        # Save this in a cache
        write_cache(local_object, False, log)
        return False, log

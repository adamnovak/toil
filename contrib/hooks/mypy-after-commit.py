#!/usr/bin/env python3

"""
mypy-after-commit.py: Git post-commit script to type-check commits in the background.

Install with:

ln -rs ./contrib/hooks/mypy-after-commit.py .git/hooks/post-commit
"""

import sys
import subprocess
import os

from lib import announce, in_acceptable_environment, check_to_cache, get_current_commit

def main(argc, argv):
    # No input; we want to run in the background
    if in_acceptable_environment():
        announce('Type-checking commit')
        commit = get_current_commit()
        result, log = check_to_cache(commit, timeout=0.1)
        if result is None:
            # Type checking timed out
            announce('This is taking a while. Type-checking in the background.')
            if os.fork() == 0:
                check_to_cache(commit)
        elif result:
            announce('Commit OK')
        else:
            complain('Commit did not type-check!')
    return 0

if __name__ == "__main__":
    sys.exit(main(len(sys.argv), sys.argv))

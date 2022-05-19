#!/usr/bin/env python3

"""
mypy-after-commit.py: Git post-commit script to type-check commits in the background.

Install with:

ln -rs ./contrib/hooks/mypy-after-commit.py .git/hooks/post-commit
"""

import sys
import subprocess
import os

from lib import check_to_cache, get_current_commit

try:
    from toil import inVirtualEnv
except:
    complain('Warning: Toil cannot be imported! Whatever you are pushing might not work!')
    sys.exit(0)
    
def main(argc, argv):
    # No input; we want to run in the background
    check_to_cache(get_current_commit())
    return 0

if __name__ == "__main__":
    sys.exit(main(len(sys.argv), sys.argv))

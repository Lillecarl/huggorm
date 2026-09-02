"""Write the front door, then let setuptools build the package.

The third `setup.py` in this repo that runs a generator at IMPORT, and
for the reason the other two give: setuptools resolves its package
list while it builds metadata, which is before any command runs. A
file that does not exist then is a file it will not ship.

Only `__init__.py` is written. Everything else in `huggorm/` is
hand-written - the client, the server, the codec - and that is the
whole difference between this package and the two beside it. What
the front door DOES is re-export the two packages behind it, which is
a mapping of Python names, and a mapping is derived here (tasks/064).
"""

import os

from setuptools import setup

from huggorm_gen.pygen.frontdoor import emit
from huggorm_gen.pygen.generate import build_manifest

HERE = os.path.dirname(os.path.abspath(__file__))

print("front door ->", emit(os.path.join(HERE, "huggorm"), build_manifest()))

setup()

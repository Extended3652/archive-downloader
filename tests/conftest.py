"""Pytest bootstrap.

Sets IA_MEDIA_ROOT before any test module imports ia_minotaur, so the
module-level constants resolve to a temp location instead of the user's
real ~/Media.
"""
import os
import sys

os.environ.setdefault("IA_MEDIA_ROOT", "/tmp/ia-test-root")

# Make the project root importable without installing it.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

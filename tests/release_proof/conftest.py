"""Puts tests/ and the repo root on sys.path before any release-proof module is imported (the rest of the suite does the
same with a sys.path line at the top of each file)."""
import release_proof.support  # noqa: F401

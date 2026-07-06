"""Job Scout internals, extracted from the top-level job_scout.py entrypoint.

job_scout.py remains the CLI + orchestration hub and re-imports the public names
defined here, so existing ``mock.patch("job_scout.<name>")`` targets keep working
for anything the orchestration layer calls directly. Helpers that call each other
within a submodule are patched at their defining module (e.g.
``jobscout.jsearch.jsearch_request``).
"""

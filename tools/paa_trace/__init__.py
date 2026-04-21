"""PAA VFS trace + batch-export tools.

Two scripts:

  * ``trace_vfs.py`` — walks every .paa file in a package group,
    parses it with :mod:`core.animation_parser`, and reports the
    format-variant distribution + per-variant parse success rate.
  * ``batch_export.py`` — walks every .paa in a group (or a filter
    matching a name glob), finds the matching .pab skeleton via
    the link-target or a global fallback, runs the export pipeline,
    and writes one .fbx + one .pipeline.json per source file.

Both scripts run standalone (``python -m tools.paa_trace.trace_vfs
--help``) and emit plain-text progress to stdout so they're easy
to pipe into a log file.
"""

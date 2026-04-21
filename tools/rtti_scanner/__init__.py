"""Pure-Python RTTI scanner for PE binaries.

Used to re-locate vtable and ClassObjectLocator RVAs inside
CrimsonDesert.exe every time the game ships an update. The runtime
patches in tools/character_unlock/ need those RVAs to hook the right
vtable slots; without a scanner, every update forces manual x64dbg
work to find the new addresses.
"""

from .scanner import (  # noqa: F401
    ClassMatch,
    ScanResult,
    mangle_class_name,
    scan_pe_for_classes,
)

"""Havok HKX (TAG0) parser for Crimson Desert.

Parses .hkx files using the TAG0 binary tagfile format (Havok SDK 2024.2).
Extracts bone names, skeleton hierarchy, physics shapes, and ragdoll data
from the binary stream without requiring the full Havok type reflection system.

TAG0 structure:
  [0-3]   uint32 BE: total file size
  [4-7]   'TAG0' magic
  [8-11]  'SDKV' marker
  [12-19] SDK version string (e.g., '20240200')
  [20+]   Sections: DATA, TYPE, TSTR, FSTR, etc.

Each section: [4B magic] [4B BE size] [data...]
"""

from __future__ import annotations

import os
import struct
import re
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.havok_parser")

TAG0_MAGIC = b"TAG0"


@dataclass
class HavokBone:
    """A bone extracted from Havok skeleton data."""
    index: int = 0
    name: str = ""
    parent_index: int = -1


@dataclass
class HavokSection:
    """A section within the TAG0 file."""
    magic: str = ""
    offset: int = 0
    size: int = 0
    data: bytes = b""


@dataclass
class ParsedHavok:
    """Parsed Havok HKX file."""
    path: str = ""
    sdk_version: str = ""
    total_size: int = 0
    sections: list[HavokSection] = field(default_factory=list)
    bones: list[HavokBone] = field(default_factory=list)
    class_names: list[str] = field(default_factory=list)
    has_skeleton: bool = False
    has_animation: bool = False
    has_physics: bool = False
    has_ragdoll: bool = False
    # April-2026 additions — the information we actually need to warn
    # modders about when they're about to edit a mesh that has paired
    # cloth or ragdoll physics.
    has_cloth: bool = False                       # hkClothData / hkaClothSetupData
    has_softbody: bool = False                    # hkpSoftBody / hkaiNavMesh
    has_mesh_shape: bool = False                  # hkpMeshShape — references mesh topology directly
    rigid_body_count: int = 0                     # distinct hkpRigidBody instances
    shape_types: list[str] = field(default_factory=list)  # unique shape class names
    cloth_class_hits: list[str] = field(default_factory=list)  # raw class names that triggered has_cloth
    # Summary flag: when True, the HKX carries physics that directly
    # references the paired mesh's topology. Editing that mesh without
    # regenerating the HKX will almost always break physics (stretching
    # cloth, collapsed ragdolls, floating collision hulls).
    binds_to_mesh_topology: bool = False


def parse_hkx(data: bytes, filename: str = "") -> ParsedHavok:
    """Parse a .hkx Havok TAG0 binary tagfile.

    Extracts sections, class names, and bone hierarchy from the binary.
    This is a best-effort parser — full Havok type reflection is not
    implemented, but bone names and basic structure are extracted.
    """
    result = ParsedHavok(path=filename, total_size=len(data))

    if len(data) < 16:
        return result

    # Verify TAG0 format
    magic = data[4:8]
    if magic != TAG0_MAGIC:
        # Not TAG0, might be older packfile format
        return result

    file_size = struct.unpack_from(">I", data, 0)[0]

    # SDK version
    sdkv_pos = data.find(b"SDKV")
    if sdkv_pos >= 0:
        ver_start = sdkv_pos + 4
        ver_end = data.find(b"\x00", ver_start, ver_start + 16)
        if ver_end < 0:
            ver_end = ver_start + 8
        result.sdk_version = data[ver_start:ver_end].decode("ascii", "replace").strip()

    # Find all sections (DATA, TYPE, TSTR, FSTR, etc.)
    pos = 0
    while pos < len(data) - 8:
        # Sections start with a 4-char ASCII tag followed by size or content
        chunk = data[pos:pos + 4]
        if chunk in (b"DATA", b"TYPE", b"TSTR", b"FSTR", b"TBDY", b"THSH", b"TPAD", b"INDX"):
            sec_magic = chunk.decode("ascii")
            # Size might be at pos-4 (before tag) or at pos+4 (after tag)
            # TAG0 uses: [tag 4B] [? padding] [content...]
            # The actual content follows the tag
            sec = HavokSection(magic=sec_magic, offset=pos)

            # Find next section to determine this section's size
            next_pos = len(data)
            for tag in [b"DATA", b"TYPE", b"TSTR", b"FSTR", b"TBDY", b"THSH", b"TPAD", b"INDX"]:
                np = data.find(tag, pos + 4)
                if 0 < np < next_pos:
                    next_pos = np

            sec.size = next_pos - pos
            sec.data = data[pos:next_pos]
            result.sections.append(sec)
            pos = next_pos
        else:
            pos += 1

    # Extract class/type names from TSTR section
    for sec in result.sections:
        if sec.magic == "TSTR":
            off = 4  # skip "TSTR"
            while off < len(sec.data):
                nul = sec.data.find(b"\x00", off)
                if nul < 0:
                    break
                s = sec.data[off:nul].decode("ascii", "replace")
                if len(s) > 1:
                    result.class_names.append(s)
                off = nul + 1

    # Fallback: real Crimson Desert HKX files (SDK 20240200) sometimes
    # store class names outside the TSTR section boundary our simple
    # scanner uses, which leaves class_names empty. Catch everything
    # that matches the Havok ``hk[pa]Identifier`` naming convention by
    # scanning the raw bytes. This is strictly additive — we only
    # append names that TSTR didn't already pick up — and keeps the
    # risk-assessor working against shipping files without needing a
    # full Havok type-reflection implementation.
    _HK_CLASS_RE = re.compile(rb"\b(hk[pacuix][A-Za-z][A-Za-z0-9]{2,60})\b")
    existing = set(result.class_names)
    for match in _HK_CLASS_RE.finditer(data):
        name = match.group(1).decode("ascii", "replace")
        if name not in existing:
            existing.add(name)
            result.class_names.append(name)

    # Extract bone names and parent hierarchy from DATA section
    _extract_bones(data, result)
    _extract_parent_indices(data, result)

    # Detect content types
    shape_names: list[str] = []
    cloth_hits: list[str] = []
    rigid_body_count = 0

    for cls in result.class_names:
        cls_lower = cls.lower()
        if "skeleton" in cls_lower:
            result.has_skeleton = True
        if "animation" in cls_lower or "anim" in cls_lower:
            result.has_animation = True
        if "rigidbody" in cls_lower or "shape" in cls_lower or "physics" in cls_lower:
            result.has_physics = True
        if "ragdoll" in cls_lower:
            result.has_ragdoll = True

        # Cloth / softbody detection — these are the classes that drive
        # fuse00_'s "beard stretches to ground" failure mode. When any
        # of them is present, editing the paired mesh without
        # regenerating the HKX will break the simulation because the
        # cloth constraint graph references per-vertex indices.
        if (
            "cloth" in cls_lower
            or "hkacloth" in cls_lower
            or "hkpcloth" in cls_lower
        ):
            result.has_cloth = True
            cloth_hits.append(cls)
        if "softbody" in cls_lower:
            result.has_softbody = True

        # Shape taxonomy. Any class with "meshshape" in it references
        # vertex / triangle arrays directly — topology change breaks it
        # regardless of whether cloth is present. Pearl Abyss ships
        # hknpLegacyCompressedMeshShape alongside the SDK 20240200
        # hkpMeshShape, so we match both via substring.
        if "meshshape" in cls_lower:
            result.has_mesh_shape = True
        if (cls_lower.startswith("hkp") or cls_lower.startswith("hknp")) and "shape" in cls_lower:
            if cls not in shape_names:
                shape_names.append(cls)

        # Rigid-body counting. We can't tell distinct instances from
        # the TSTR list alone (that only has type names), but we do
        # want to flag files that carry at least one rigid body. Both
        # hkp and hknp namespaces appear in shipping files.
        if "rigidbody" in cls_lower:
            rigid_body_count = max(rigid_body_count, 1)

    result.shape_types = shape_names
    result.cloth_class_hits = cloth_hits
    result.rigid_body_count = rigid_body_count
    # Topology-binding classes — any of these means editing the paired
    # mesh is going to desync this HKX.
    result.binds_to_mesh_topology = (
        result.has_cloth or result.has_softbody or result.has_mesh_shape
    )

    if result.bones:
        result.has_skeleton = True

    logger.info("Parsed HKX %s: SDK %s, %d sections, %d bones, %d classes, "
                "skel=%s anim=%s phys=%s ragdoll=%s",
                filename, result.sdk_version, len(result.sections),
                len(result.bones), len(result.class_names),
                result.has_skeleton, result.has_animation,
                result.has_physics, result.has_ragdoll)
    return result


def _extract_bones(data: bytes, result: ParsedHavok):
    """Extract bone names from the binary data.

    Scans for common bone name patterns (Bip01, B_, Bone, etc.)
    and builds a bone list. Parent indices are inferred from the
    naming convention when not explicitly stored.
    """
    bone_names = []
    seen = set()
    pos = 0

    while pos < len(data) - 5:
        found = False
        for prefix in (b"Bip01", b"B_", b"Bone", b"Root", b"Dummy"):
            if data[pos:pos + len(prefix)] == prefix:
                nul = data.find(b"\x00", pos, pos + 128)
                if nul > pos:
                    raw = data[pos:nul]
                    # Validate: all printable ASCII
                    if all(32 <= b < 127 for b in raw) and len(raw) >= 3:
                        name = raw.decode("ascii")
                        if name not in seen:
                            seen.add(name)
                            bone_names.append(name)
                        pos = nul + 1
                        found = True
                        break
        if not found:
            pos += 1

    # Build bone hierarchy from names
    for i, name in enumerate(bone_names):
        bone = HavokBone(index=i, name=name, parent_index=-1)

        # Infer parent from naming convention
        # "Bip01 Spine" → parent is "Bip01"
        # "Bip01 R Calf" → parent is "Bip01 R Thigh" (or nearest ancestor)
        if " " in name:
            parent_name = name.rsplit(" ", 1)[0]
            for j, pn in enumerate(bone_names):
                if pn == parent_name:
                    bone.parent_index = j
                    break

        result.bones.append(bone)


def _extract_parent_indices(data: bytes, result: ParsedHavok):
    """Find and apply the parent index array stored as int16 values.

    Havok stores bone parent indices as a contiguous int16 array where
    index 0 = -1 (root) and each subsequent value is the parent bone index.
    """
    if not result.bones:
        return

    bone_count = len(result.bones)
    if bone_count < 2:
        return

    # Scan for an int16 array that matches valid parent hierarchy:
    # [0] = -1, all others in range [-1, bone_count), and i > parent[i] (DAG)
    best_off = -1
    best_score = 0

    for off in range(0, len(data) - bone_count * 2, 2):
        first = struct.unpack_from("<h", data, off)[0]
        if first != -1:
            continue

        vals = [struct.unpack_from("<h", data, off + i * 2)[0]
                for i in range(bone_count)]

        # Validate: each parent must be -1 or a valid earlier index
        valid = True
        score = 0
        for i, v in enumerate(vals):
            if v < -1 or v >= bone_count:
                valid = False
                break
            if i > 0 and v == -1:
                score += 1  # multiple roots is less common but valid
            if 0 <= v < i:
                score += 2  # proper parent ordering
        if not valid:
            continue
        if score > best_score:
            best_score = score
            best_off = off

    if best_off >= 0:
        for i in range(bone_count):
            parent = struct.unpack_from("<h", data, best_off + i * 2)[0]
            result.bones[i].parent_index = parent


def get_hkx_summary(data: bytes) -> str:
    """Get a human-readable summary of an HKX file."""
    try:
        hkx = parse_hkx(data)
        lines = [
            f"Havok TAG0 File (SDK {hkx.sdk_version})",
            f"Size: {hkx.total_size:,} bytes",
            f"Sections: {len(hkx.sections)}",
        ]

        if hkx.bones:
            lines.append(f"Bones: {len(hkx.bones)}")
            for b in hkx.bones[:10]:
                parent = hkx.bones[b.parent_index].name if 0 <= b.parent_index < len(hkx.bones) else "ROOT"
                lines.append(f"  [{b.index}] {b.name} → {parent}")
            if len(hkx.bones) > 10:
                lines.append(f"  ... and {len(hkx.bones) - 10} more")

        content = []
        if hkx.has_skeleton:
            content.append("Skeleton")
        if hkx.has_animation:
            content.append("Animation")
        if hkx.has_physics:
            content.append("Physics")
        if hkx.has_ragdoll:
            content.append("Ragdoll")
        if content:
            lines.append(f"Content: {', '.join(content)}")

        if hkx.class_names:
            lines.append(f"Classes: {', '.join(hkx.class_names[:8])}")

        return "\n".join(lines)
    except Exception as e:
        return f"HKX parse error: {e}"


def is_havok_file(path: str) -> bool:
    """Check if a file is a Havok file."""
    return os.path.splitext(path.lower())[1] == ".hkx"


@dataclass
class HavokEditRisk:
    """Pre-edit diagnostic for a mesh's paired HKX.

    Populated by ``assess_mesh_edit_risk`` and surfaced in the mod-
    packaging UI so users get a clear, loud warning before the repack
    step for cases where editing the mesh will definitely break
    physics in-game (the flagship symptom being ``fuse00_``'s beard
    that stretches to the ground after an OBJ round-trip).
    """
    severity: str                         # "none" | "warn" | "block"
    reasons: list[str] = field(default_factory=list)
    driving_systems: list[str] = field(default_factory=list)  # human labels: "Cloth", "Ragdoll", ...

    @property
    def is_blocking(self) -> bool:
        return self.severity == "block"

    @property
    def is_warning(self) -> bool:
        return self.severity in ("warn", "block")

    def format_message(self, mesh_path: str = "", hkx_path: str = "") -> str:
        """Human-readable block suitable for a confirmation dialog."""
        lines: list[str] = []
        if self.severity == "none":
            return ""
        heading = "Physics desync risk" if self.severity == "warn" else "PHYSICS WILL DESYNC"
        lines.append(heading)
        if mesh_path:
            lines.append(f"  mesh: {mesh_path}")
        if hkx_path:
            lines.append(f"  hkx:  {hkx_path}")
        if self.driving_systems:
            lines.append(f"  paired HKX drives: {', '.join(self.driving_systems)}")
        for reason in self.reasons:
            lines.append(f"  - {reason}")
        lines.append("")
        lines.append(
            "Any vertex add/remove or UV reshuffle in the mesh will break "
            "the simulation because HKX references mesh topology by index. "
            "Re-export the HKX from the original rig alongside your mesh, "
            "or ship the unedited HKX together with the edit."
        )
        return "\n".join(lines)


def assess_mesh_edit_risk(hkx_data: bytes) -> HavokEditRisk:
    """Pre-edit check: does this HKX bind to the paired mesh's topology?

    Used by the mesh-sidecar service to loudly warn before the user
    commits to an edit that would break physics.

    Severity ladder:

      block  — hkpMeshShape or cloth simulation with vertex references;
               editing mesh topology will almost certainly break the
               file. Requires HKX regeneration from a DCC tool with
               the Havok plug-in.
      warn   — ragdoll or generic rigid-body physics present. Bone
               hierarchy matters; if the mesh edit preserves bone
               weights (donor-record path) things usually survive.
      none   — skeleton or animation only; mesh edits are safe.
    """
    risk = HavokEditRisk(severity="none")

    try:
        hkx = parse_hkx(hkx_data)
    except Exception as exc:
        risk.severity = "warn"
        risk.reasons.append(f"HKX parse failed: {exc}")
        return risk

    systems: list[str] = []
    if hkx.has_cloth:
        systems.append("Cloth")
    if hkx.has_softbody:
        systems.append("Softbody")
    if hkx.has_mesh_shape:
        systems.append("Mesh collision")
    if hkx.has_ragdoll:
        systems.append("Ragdoll")
    if hkx.rigid_body_count:
        systems.append(f"Rigid bodies ({hkx.rigid_body_count}+)")
    risk.driving_systems = systems

    if hkx.binds_to_mesh_topology:
        risk.severity = "block"
        if hkx.has_cloth:
            risk.reasons.append(
                f"cloth simulation detected ({len(hkx.cloth_class_hits)} cloth class ref(s))"
            )
        if hkx.has_softbody:
            risk.reasons.append("softbody simulation detected")
        if hkx.has_mesh_shape:
            risk.reasons.append("hkpMeshShape references mesh vertex/triangle buffers directly")
        return risk

    if hkx.has_ragdoll or hkx.rigid_body_count:
        risk.severity = "warn"
        if hkx.has_ragdoll:
            risk.reasons.append("ragdoll present — preserve bone hierarchy")
        if hkx.rigid_body_count:
            risk.reasons.append(
                f"rigid bodies present — edits that move bones will displace collision hulls"
            )
        return risk

    return risk

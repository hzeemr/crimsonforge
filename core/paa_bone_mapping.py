"""PAA-track to PAB-bone mapping.

Reverse-engineering context (Apr 2026)
--------------------------------------

PAA animation files declare N "tracks" (one per animated bone) but
they do NOT carry per-track bone identifiers that match the paired
PAB skeleton. We verified this empirically:

  * PAB bone HASHES (4-byte prefix of each bone record in the .pab)
    do not appear in the PAA bytes — zero occurrences across every
    sample and every common string-hash algorithm (Jenkins OAT, FNV,
    djb2, sdbm, CRC32)
  * PAB bone NAME strings do not appear as ASCII in the PAA bytes
  * The global header (0x00..first_separator) is just 80-90 bytes —
    metadata + Korean tags + 5 marker bytes. No hash table
  * The PAA track COUNT doesn't match the PAB bone count
    (71 tracks vs 56 bones on phm_01 + idle) — so it's not even a
    strict 1:1 ordered mapping

The likely explanation is that PAA is authored against a
CANONICAL RIG (larger than any single character's PAB) and the
canonical-index -> per-rig-bone mapping is hardcoded in the game's
C++ code (probably a ``phm_01.skel_map.cpp`` or similar).

What this module provides
-------------------------

Rather than trying to perfectly infer that mapping from bytes
(which isn't possible without the game binary), we ship:

  1. A BEST-EFFORT auto-correlation from bind-pose angular distance
     plus a per-index alignment nudge.

  2. A persistent override file so a user can manually remap tracks
     once per rig + save it to disk. Subsequent animations for that
     rig automatically use the saved mapping.

  3. A well-defined data model (``BoneMap``) and JSON on-disk format
     the UI editor uses.

Mapping semantics
-----------------

``track_to_pab[i] == j`` means PAA track ``i`` should drive PAB
bone ``j`` on export. Entries equal to ``-1`` mean "drop this
track" (e.g. canonical bones that don't exist in this character's
PAB — weapon attach points etc.). A dictionary is used internally
so missing keys also mean "drop".
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger("core.paa_bone_mapping")


# ── Data model ─────────────────────────────────────────────────────────

@dataclass
class BoneMap:
    """PAA track index -> PAB bone index (or -1 = drop).

    ``rig_key`` identifies the skeleton this mapping was authored
    against — typically the PAB basename (``phm_01``). Persisted
    mappings are keyed on this so any animation that targets the
    same rig picks up the saved overrides automatically.

    ``confidence`` is 0.0-1.0; auto-correlate emits its own score so
    the UI can flag low-confidence entries for user review.
    """
    rig_key: str = ""
    track_count: int = 0
    mapping: dict[int, int] = field(default_factory=dict)  # track_idx -> pab_idx
    confidence: dict[int, float] = field(default_factory=dict)
    source: str = "auto"   # "auto" | "user" | "mixed"

    def for_track(self, track_idx: int) -> int:
        """Return the PAB bone index this track maps to, or -1 if dropped."""
        return self.mapping.get(track_idx, -1)

    def set(self, track_idx: int, pab_idx: int, confidence: float = 1.0) -> None:
        if pab_idx < 0:
            # Explicit drop
            self.mapping[track_idx] = -1
        else:
            self.mapping[track_idx] = pab_idx
        self.confidence[track_idx] = confidence

    def to_json(self) -> dict:
        return {
            "rig_key": self.rig_key,
            "track_count": self.track_count,
            "mapping": {str(k): v for k, v in self.mapping.items()},
            "confidence": {str(k): v for k, v in self.confidence.items()},
            "source": self.source,
        }

    @classmethod
    def from_json(cls, data: dict) -> "BoneMap":
        return cls(
            rig_key=data.get("rig_key", ""),
            track_count=int(data.get("track_count", 0)),
            mapping={int(k): int(v) for k, v in data.get("mapping", {}).items()},
            confidence={int(k): float(v) for k, v in data.get("confidence", {}).items()},
            source=data.get("source", "auto"),
        )


# ── Auto-correlation ──────────────────────────────────────────────────

def _quat_angle_deg(q1: tuple, q2: tuple) -> float:
    """Return angular distance between two unit quaternions (degrees).

    Uses the absolute-dot-product form so we ignore the double-cover
    sign ambiguity.
    """
    dot = q1[0] * q2[0] + q1[1] * q2[1] + q1[2] * q2[2] + q1[3] * q2[3]
    dot = abs(dot)
    if dot > 1.0:
        dot = 1.0
    if dot < -1.0:
        dot = -1.0
    return math.degrees(2.0 * math.acos(dot))


def auto_correlate(
    paa_tracks: list,   # list[BoneTrack] from animation_parser_v2
    pab_bones: list,    # list[Bone] from skeleton_parser
    *,
    rig_key: str = "",
) -> BoneMap:
    """Pure byte-inspection mapping. Does its best with what's
    available; NOT authoritative.

    Strategy:
      * Start by assuming ORDERED 1:1 (track[i] -> pab[i]) for the
        first ``min(len)`` tracks
      * For each ordered-position pair, check angular distance
        between bind quats; score it
      * When ordered-position distance is high, fall back to the
        GREEDY-BEST-MATCH pab bone by bind angle and mark
        low confidence

    This is not perfect but it gives the user a sensible starting
    point to correct in the mapping dialog. Real enterprise fix
    lives on top of a per-rig persistence file (see ``load_bone_map``).
    """
    if rig_key == "" and pab_bones:
        # Derive from PAB filename stem when caller didn't supply
        rig_key = getattr(pab_bones[0], "_source", "unknown_rig")

    n_tracks = len(paa_tracks)
    n_bones = len(pab_bones)
    bmap = BoneMap(rig_key=rig_key, track_count=n_tracks, source="auto")

    # Pass 1: ordered 1:1 guess for the overlap region
    for i in range(min(n_tracks, n_bones)):
        track_bind = paa_tracks[i].bind_quat
        pab_bind = pab_bones[i].rotation
        d = _quat_angle_deg(track_bind, pab_bind)
        if d < 10.0:
            bmap.set(i, i, confidence=1.0 - d / 90.0)
        else:
            # Fall back to greedy best-match
            best_pab = -1
            best_dist = 999.0
            for j, pb in enumerate(pab_bones):
                dj = _quat_angle_deg(track_bind, pb.rotation)
                if dj < best_dist:
                    best_dist = dj
                    best_pab = j
            # Only accept when a clearly-better match exists
            if best_dist < 20.0:
                bmap.set(i, best_pab, confidence=max(0.1, 1.0 - best_dist / 90.0))
            else:
                bmap.set(i, -1, confidence=0.0)

    # Tracks beyond n_bones have no obvious mapping; drop them
    for i in range(n_bones, n_tracks):
        bmap.set(i, -1, confidence=0.0)

    logger.info(
        "Auto-correlated bone map for %s: %d/%d tracks mapped "
        "(%d dropped, avg confidence %.2f)",
        rig_key, sum(1 for v in bmap.mapping.values() if v >= 0), n_tracks,
        sum(1 for v in bmap.mapping.values() if v < 0),
        (sum(bmap.confidence.values()) / max(1, len(bmap.confidence))),
    )
    return bmap


# ── Persistence ───────────────────────────────────────────────────────

def bone_map_dir() -> str:
    """Return the user-data directory where bone-map overrides live."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "CrimsonForge", "bone_maps")
    os.makedirs(d, exist_ok=True)
    return d


def _path_for_rig(rig_key: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in rig_key)
    return os.path.join(bone_map_dir(), f"{safe}.bonemap.json")


def save_bone_map(bone_map: BoneMap) -> str:
    """Write the bone map to user-data. Returns the file path."""
    if not bone_map.rig_key:
        raise ValueError("BoneMap.rig_key is empty — cannot persist")
    path = _path_for_rig(bone_map.rig_key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bone_map.to_json(), f, indent=2)
    logger.info("Saved bone map for rig %s -> %s", bone_map.rig_key, path)
    return path


def load_bone_map(rig_key: str) -> Optional[BoneMap]:
    """Load a previously-saved bone map for the given rig, if any."""
    path = _path_for_rig(rig_key)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        bmap = BoneMap.from_json(data)
        logger.info("Loaded bone map for rig %s from %s", rig_key, path)
        return bmap
    except Exception as e:
        logger.warning("Failed to load bone map %s: %s", path, e)
        return None


def apply_bone_map(
    paa_tracks: list,
    pab_bones: list,
    bone_map: BoneMap,
) -> list[tuple[int, int]]:
    """Return a list of ``(track_idx, pab_idx)`` pairs suitable for the
    FBX exporter's ``bone_mapping`` parameter. Dropped tracks are
    excluded from the output.
    """
    out: list[tuple[int, int]] = []
    for track_idx in range(len(paa_tracks)):
        pab_idx = bone_map.for_track(track_idx)
        if 0 <= pab_idx < len(pab_bones):
            out.append((track_idx, pab_idx))
    return out

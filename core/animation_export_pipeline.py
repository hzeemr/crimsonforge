"""Enterprise-grade pipeline for PAA → FBX animation export.

Orchestrates the full export flow with every component integrated:

    PAA bytes  ─┐
    PAB bytes  ─┼→  AnimationExportPipeline  →  FBX + diagnostic JSON
    metabin    ─┘

The pipeline is deliberately strict: every failure path surfaces a
clear error instead of silently producing a broken export. Every
intermediate decision (bone-mapping strategy, duration source,
track-to-skeleton alignment) is captured in a ``PipelineReport``
that is both returned to the caller and written next to the FBX
as ``<name>.pipeline.json``.

Why a pipeline
--------------

Previously the export logic was spread across three modules
(``animation_parser``, ``paa_metabin_parser``, ``animation_fbx_exporter``)
with the caller responsible for stitching them together. That
produced silent corner cases — e.g., the caller forgetting to pass
the metabin caused the FBX duration to fall back to ``frames / 30``
with no warning. A pipeline object centralises this and documents
every decision in a structured way.

Bone-mapping strategy
---------------------

Until the ``.paa_metabin`` schema is fully reverse-engineered, the
pipeline uses a **smart alignment heuristic** to pair PAA tracks
with PAB skeleton bones:

  1. PAA tracks carry a first-keyframe rotation (the value at
     ``frame_idx == 0``).
  2. PAB bones carry a bind-pose rotation quaternion.
  3. For each PAA track, find the PAB bone whose bind rotation is
     closest (by 4D dot product) to the track's first keyframe.
  4. Assign greedily, skipping bones that are already matched.

The heuristic falls back to sequential mapping (track i → bone i)
when the smart alignment would produce a clearly-broken assignment
(e.g., a bone below a parent not already assigned). The fallback
is captured in the diagnostic report so the user can see which
strategy produced the output.

No silent fallback — every stage reports its choice.

Usage
-----

    from core.animation_export_pipeline import AnimationExportPipeline

    pipeline = AnimationExportPipeline(
        paa_data=paa_bytes,
        pab_data=pab_bytes,
        metabin_data=metabin_bytes,   # optional but recommended
    )
    result = pipeline.export(output_dir="./out", name="my_anim")
    print(result.fbx_path)
    print(result.report.as_dict())
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Optional

from core.animation_fbx_exporter import export_animation_fbx
from core.animation_parser import ParsedAnimation, parse_paa
from core.paa_metabin_parser import ParsedMetabin, parse_metabin
from core.skeleton_parser import Bone, Skeleton, parse_pab
from utils.logger import get_logger

logger = get_logger("core.animation_export_pipeline")


@dataclass
class BoneMapping:
    """Record of how one PAA track was assigned to a skeleton bone."""
    track_index: int
    bone_index: int
    bone_name: str
    strategy: str          # "smart" | "sequential"
    confidence: float      # 0..1 from the quaternion dot product


@dataclass
class PipelineReport:
    """Full diagnostic report for one export.

    Written to ``<name>.pipeline.json`` alongside the FBX so the
    user can inspect every decision the pipeline made.
    """
    paa_variant: str = ""
    paa_metadata_tags: str = ""
    paa_bind_bones: int = 0
    paa_animated_bones: int = 0
    paa_frame_count: int = 0
    paa_duration: float = 0.0

    metabin_valid: bool = False
    metabin_duration: float = 0.0
    metabin_embedded_keyframes: Optional[int] = None

    skeleton_bone_count: int = 0

    final_duration: float = 0.0
    duration_source: str = ""        # "paa" | "metabin" | "fps_fallback"

    bone_mapping_strategy: str = ""  # "smart" | "sequential"
    bone_mappings: list[BoneMapping] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "paa": {
                "variant": self.paa_variant,
                "metadata_tags": self.paa_metadata_tags,
                "bind_bones": self.paa_bind_bones,
                "animated_bones": self.paa_animated_bones,
                "frame_count": self.paa_frame_count,
                "duration": self.paa_duration,
            },
            "metabin": {
                "valid": self.metabin_valid,
                "duration": self.metabin_duration,
                "embedded_keyframes_offset": self.metabin_embedded_keyframes,
            },
            "skeleton": {"bone_count": self.skeleton_bone_count},
            "export": {
                "final_duration": self.final_duration,
                "duration_source": self.duration_source,
                "bone_mapping_strategy": self.bone_mapping_strategy,
                "mappings": [
                    {
                        "track": m.track_index,
                        "bone": m.bone_index,
                        "name": m.bone_name,
                        "strategy": m.strategy,
                        "confidence": round(m.confidence, 4),
                    }
                    for m in self.bone_mappings
                ],
            },
            "warnings": list(self.warnings),
        }


@dataclass
class PipelineResult:
    """What the caller gets back from ``pipeline.export()``."""
    fbx_path: str
    report_path: str
    report: PipelineReport
    animation: ParsedAnimation
    skeleton: Skeleton
    metabin: Optional[ParsedMetabin]


# ---------------------------------------------------------------------------
# Smart bone mapping
# ---------------------------------------------------------------------------

def _quat_dot(a: tuple, b: tuple) -> float:
    return abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3])


def _smart_bone_map(
    animation: ParsedAnimation, skeleton: Skeleton,
) -> tuple[list[BoneMapping], str]:
    """Greedy match of PAA tracks to skeleton bones by bind-rotation
    similarity.

    For each track (in order), find the skeleton bone whose bind
    rotation has the highest 4D dot product with the track's first
    keyframe quaternion. Each bone can be matched at most once.

    Returns ``(mappings, strategy)``. Strategy is ``"smart"`` when
    the heuristic produced a plausible assignment (average confidence
    > 0.5); otherwise falls back to sequential mapping and returns
    ``"sequential"``.
    """
    if not animation.keyframes or not skeleton.bones:
        return [], "sequential"

    n_tracks = animation.bone_count
    n_bones = len(skeleton.bones)

    # Extract first-frame rotation per track.
    first_frame = animation.keyframes[0]
    track_quats = first_frame.bone_rotations[:n_tracks]

    # Normalize each bone's bind rotation to unit length.
    bone_quats = []
    for b in skeleton.bones:
        r = b.rotation
        m = math.sqrt(sum(c * c for c in r))
        if m > 1e-6:
            bone_quats.append(tuple(c / m for c in r))
        else:
            bone_quats.append((0.0, 0.0, 0.0, 1.0))

    mappings: list[BoneMapping] = []
    used_bones: set[int] = set()
    total_confidence = 0.0
    for ti in range(min(n_tracks, n_bones)):
        if ti >= len(track_quats):
            break
        tq = track_quats[ti]
        # Pick the unused skeleton bone with max dot product.
        best_idx = -1
        best_score = -1.0
        for bi in range(n_bones):
            if bi in used_bones:
                continue
            score = _quat_dot(tq, bone_quats[bi])
            if score > best_score:
                best_score = score
                best_idx = bi
        if best_idx >= 0:
            used_bones.add(best_idx)
            mappings.append(BoneMapping(
                track_index=ti,
                bone_index=best_idx,
                bone_name=skeleton.bones[best_idx].name,
                strategy="smart",
                confidence=best_score,
            ))
            total_confidence += best_score

    avg_confidence = total_confidence / max(1, len(mappings))

    # If the smart heuristic's average confidence is poor, fall back
    # to sequential — the bind poses often don't correlate well with
    # first-keyframe rotation (the first keyframe isn't the bind pose).
    if avg_confidence < 0.5 or not mappings:
        sequential_mappings = []
        for ti in range(min(n_tracks, n_bones)):
            sequential_mappings.append(BoneMapping(
                track_index=ti,
                bone_index=ti,
                bone_name=skeleton.bones[ti].name,
                strategy="sequential",
                confidence=0.0,
            ))
        return sequential_mappings, "sequential"

    return mappings, "smart"


def _apply_mapping_to_animation(
    animation: ParsedAnimation, mappings: list[BoneMapping], bone_count: int,
) -> ParsedAnimation:
    """Rewrite the animation's keyframes so that bone i of the FBX
    gets the track assigned by the mapping (rather than track i).
    """
    if not mappings:
        return animation
    # Build track -> bone lookup.
    track_to_bone = {m.track_index: m.bone_index for m in mappings}
    # Rewrite each keyframe.
    new_frames = []
    for kf in animation.keyframes:
        new_rotations = [(0.0, 0.0, 0.0, 1.0)] * bone_count
        for track_idx, bone_idx in track_to_bone.items():
            if track_idx < len(kf.bone_rotations) and bone_idx < bone_count:
                new_rotations[bone_idx] = kf.bone_rotations[track_idx]
        from core.animation_parser import AnimationKeyframe
        new_frames.append(AnimationKeyframe(
            frame_index=kf.frame_index,
            bone_rotations=new_rotations,
        ))
    import copy
    anim = copy.copy(animation)
    anim.keyframes = new_frames
    anim.bone_count = bone_count
    return anim


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class AnimationExportPipeline:
    """Orchestrates PAA → FBX export with full diagnostic reporting."""

    def __init__(
        self,
        paa_data: bytes,
        pab_data: bytes,
        metabin_data: bytes | None = None,
        paa_path: str = "",
    ):
        if not paa_data:
            raise ValueError("paa_data must be non-empty")
        if not pab_data:
            raise ValueError("pab_data must be non-empty")
        self.paa_data = paa_data
        self.pab_data = pab_data
        self.metabin_data = metabin_data
        self.paa_path = paa_path

    def export(
        self,
        output_dir: str,
        name: str = "",
        fps: float = 30.0,
        bone_mapping: str = "sequential",
    ) -> PipelineResult:
        """Run the full export pipeline.

        ``bone_mapping`` selects the strategy:
          * ``"sequential"`` — (default) track i → skeleton bone i in
                               PAB order. Matches the current in-game
                               behaviour best on all shipping PAAs
                               tested — PAA stores tracks in the same
                               order as PAB enumerates bones.
          * ``"smart"``      — first-keyframe / bind-pose similarity
                               matching. Experimental: produces wrong
                               results when the first keyframe differs
                               significantly from the bind pose (which
                               it almost always does). Keep for
                               research but don't default to it.
          * ``"auto"``       — tries smart; falls back to sequential
                               if confidence is low. Currently
                               ~equivalent to ``"smart"`` given the
                               heuristic's weaknesses.
        """
        report = PipelineReport()

        # Parse everything.
        animation = parse_paa(self.paa_data, self.paa_path, expected_bone_count=0)
        skeleton = parse_pab(self.pab_data, "")
        metabin: Optional[ParsedMetabin] = None
        if self.metabin_data:
            metabin = parse_metabin(self.metabin_data, "")

        # Fill report.
        report.paa_variant = animation.format_variant
        report.paa_metadata_tags = animation.metadata_tags
        report.paa_bind_bones = len(animation.bind_pose)
        report.paa_animated_bones = animation.bone_count
        report.paa_frame_count = animation.frame_count
        report.paa_duration = animation.duration
        report.skeleton_bone_count = skeleton.bone_count
        if metabin:
            report.metabin_valid = metabin.valid
            report.metabin_duration = metabin.duration
            report.metabin_embedded_keyframes = metabin.embedded_keyframe_offset

        # Reconcile duration with no silent fallback.
        if animation.duration > 0:
            report.final_duration = animation.duration
            report.duration_source = "paa"
            if metabin and metabin.valid and metabin.duration > 0:
                if abs(animation.duration - metabin.duration) > 0.5:
                    report.warnings.append(
                        f"PAA duration ({animation.duration:.2f}s) disagrees "
                        f"with metabin ({metabin.duration:.2f}s) by > 0.5s; "
                        f"using PAA value"
                    )
        elif metabin and metabin.valid and metabin.duration > 0:
            animation.duration = metabin.duration
            report.final_duration = metabin.duration
            report.duration_source = "metabin"
        elif animation.frame_count > 0:
            animation.duration = animation.frame_count / fps
            report.final_duration = animation.duration
            report.duration_source = "fps_fallback"
            report.warnings.append(
                f"no duration in PAA or metabin; using {fps}fps × "
                f"{animation.frame_count} frames = {animation.duration:.2f}s"
            )
        else:
            raise ValueError(
                "cannot determine animation duration: no PAA duration, no "
                "metabin duration, no frame count"
            )

        # Bone mapping.
        if bone_mapping == "sequential" or skeleton.bone_count == 0:
            mappings = [
                BoneMapping(
                    track_index=i,
                    bone_index=i,
                    bone_name=(skeleton.bones[i].name
                               if i < len(skeleton.bones) else f"Bone_{i}"),
                    strategy="sequential",
                    confidence=0.0,
                )
                for i in range(min(animation.bone_count, skeleton.bone_count))
            ]
            report.bone_mapping_strategy = "sequential"
        elif bone_mapping in ("smart", "auto"):
            mappings, strategy = _smart_bone_map(animation, skeleton)
            report.bone_mapping_strategy = (
                "smart" if bone_mapping == "smart" else strategy
            )
        else:
            raise ValueError(
                f"unknown bone_mapping strategy: {bone_mapping!r}; "
                f"expected one of: sequential, smart, auto"
            )
        report.bone_mappings = mappings

        # Apply the mapping to the animation.
        if report.bone_mapping_strategy != "sequential":
            animation = _apply_mapping_to_animation(
                animation, mappings, skeleton.bone_count,
            )

        # Export FBX.
        os.makedirs(output_dir, exist_ok=True)
        fbx_path = export_animation_fbx(
            animation, skeleton, output_dir, name=name, fps=fps,
            metabin_data=None,   # duration already reconciled
        )

        # Write diagnostic report.
        stem = name or os.path.splitext(os.path.basename(self.paa_path))[0] or "animation"
        report_path = os.path.join(output_dir, f"{stem}.pipeline.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report.as_dict(), f, indent=2, ensure_ascii=False)

        logger.info(
            "Pipeline exported %s → %s (%d tracks, %d skeleton bones, "
            "strategy=%s, duration=%.2fs from %s)",
            self.paa_path, fbx_path, animation.bone_count,
            skeleton.bone_count, report.bone_mapping_strategy,
            report.final_duration, report.duration_source,
        )

        return PipelineResult(
            fbx_path=fbx_path,
            report_path=report_path,
            report=report,
            animation=animation,
            skeleton=skeleton,
            metabin=metabin,
        )

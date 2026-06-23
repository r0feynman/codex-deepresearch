"""Deterministic modality routing for research planner angles."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Sequence


ROUTES = ("text_only", "visual_required", "visual_optional")
TEXT_ONLY_MAX_IMAGES = 0
VISUAL_REQUIRED_MAX_IMAGES = 12
VISUAL_OPTIONAL_MAX_IMAGES = 6


@dataclass(frozen=True)
class ModalityDecision:
    """Route decision for one planner angle."""

    angle: str
    modality: str
    reason: str
    visual_tasks: list[str]
    max_images: int

    def to_dict(self) -> dict:
        return asdict(self)


_VISUAL_REQUIRED_PATTERNS = (
    r"\bscreenshot(s)?\b",
    r"\bscreen(s)?\b",
    r"\bui\b",
    r"\binterface\b",
    r"\blayout\b",
    r"\bphoto(s|graphy)?\b",
    r"\bvideo frame(s)?\b",
    r"\bocr\b",
    r"\bdiagram(s)?\b",
    r"\bchart(s)?\b",
    r"\bgraph value(s)?\b",
    r"\bmap(s)?\b",
    r"\bbefore[- ]and[- ]after\b",
    r"\bappearance\b",
)

_GENERIC_VISUAL_PATTERNS = (
    r"\bimage(s)?\b",
    r"\bvisual(s|ly)?\b",
)

_VISUAL_OPTIONAL_PATTERNS = (
    r"\bmarket report\b",
    r"\bmarket research\b",
    r"\bmarket share\b",
    r"\bcompetitive analysis\b",
    r"\bcompetitor(s)?\b",
    r"\bproduct comparison\b",
    r"\bcompare product(s)?\b",
    r"\bpricing\b",
    r"\bbenchmark(s|ing)?\b",
    r"\bnews\b",
    r"\btrend(s)?\b",
    r"\badoption\b",
)

_TEXT_ONLY_PATTERNS = (
    r"\bapi\b",
    r"\bapi doc(s|umentation)?\b",
    r"\bdoc(s|umentation)?\b",
    r"\breference\b",
    r"\bspec(s|ification)?\b",
    r"\brelease note(s)?\b",
    r"\bchangelog\b",
    r"\bpolicy doc(s|ument)?\b",
    r"\bpaper(s)?\b",
    r"\brfc\b",
    r"\bschema\b",
)


def route_angle(
    *,
    question: str,
    angle: str,
    max_images: int,
    route_override: str | None = None,
) -> ModalityDecision:
    """Classify one planner angle as text-only, visual-required, or visual-optional."""

    normalized_angle = _normalize_angle(angle)
    if route_override is not None:
        return _decision_for_route(
            normalized_angle,
            route_override,
            f"Explicit route override selected {route_override}.",
            max_images=max_images,
        )

    text = _classification_text(question, normalized_angle)
    if _matches(text, _VISUAL_REQUIRED_PATTERNS):
        return _decision_for_route(
            normalized_angle,
            "visual_required",
            "The question or angle asks for UI, layout, image, chart, map, or other visual evidence.",
            max_images=max_images,
        )
    if _matches(text, _VISUAL_OPTIONAL_PATTERNS):
        return _decision_for_route(
            normalized_angle,
            "visual_optional",
            "Text sources can answer first, while images may improve confidence or rebut the finding.",
            max_images=max_images,
        )
    if _matches(text, _TEXT_ONLY_PATTERNS):
        return _decision_for_route(
            normalized_angle,
            "text_only",
            "The angle targets text evidence such as official docs, specs, release notes, policies, or papers.",
            max_images=max_images,
        )
    if _matches(text, _GENERIC_VISUAL_PATTERNS):
        return _decision_for_route(
            normalized_angle,
            "visual_required",
            "The question or angle asks for image or visual evidence.",
            max_images=max_images,
        )
    return _decision_for_route(
        normalized_angle,
        "text_only",
        "No visual evidence trigger was detected, so the conservative route avoids visual budget.",
        max_images=max_images,
    )


def route_angles(
    *,
    question: str,
    angles: Sequence[str],
    max_images: int,
    route_override: str | None = None,
) -> list[ModalityDecision]:
    """Classify every supplied planner angle."""

    normalized_angles = [_normalize_angle(angle) for angle in angles if angle.strip()]
    if not normalized_angles:
        normalized_angles = ["primary source discovery"]
    return [
        route_angle(
            question=question,
            angle=angle,
            max_images=max_images,
            route_override=route_override,
        )
        for angle in normalized_angles
    ]


def _decision_for_route(
    angle: str,
    route: str,
    reason: str,
    *,
    max_images: int,
) -> ModalityDecision:
    if route not in ROUTES:
        raise ValueError("route must be one of: " + ", ".join(ROUTES))
    if max_images < 0:
        raise ValueError("max_images must be non-negative")
    if route == "text_only":
        return ModalityDecision(
            angle=angle,
            modality=route,
            reason=reason,
            visual_tasks=[],
            max_images=TEXT_ONLY_MAX_IMAGES,
        )
    if route == "visual_required":
        return ModalityDecision(
            angle=angle,
            modality=route,
            reason=reason,
            visual_tasks=["image_claim_alignment", "ocr", "layout_review"],
            max_images=min(max_images, VISUAL_REQUIRED_MAX_IMAGES),
        )
    return ModalityDecision(
        angle=angle,
        modality=route,
        reason=reason,
        visual_tasks=["image_claim_alignment"],
        max_images=min(max_images, VISUAL_OPTIONAL_MAX_IMAGES),
    )


def _normalize_angle(angle: str) -> str:
    normalized = " ".join(angle.strip().split())
    if not normalized:
        raise ValueError("angle cannot be empty")
    return normalized


def _classification_text(question: str, angle: str) -> str:
    if angle == "primary source discovery":
        return f"{question} {angle}".lower()
    return angle.lower()


def _matches(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)

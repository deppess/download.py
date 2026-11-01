"""Versioned, conservative genre taxonomy for review-first classification."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

TAXONOMY_VERSION = 1

# Broad folders only. A term is deliberately assigned to just one destination;
# unknown, conflicting, or weak evidence is always left for review.
GENRE_TERMS: dict[str, frozenset[str]] = {
    "Electronic": frozenset(
        {
            "electronic",
            "house",
            "techno",
            "trance",
            "drum and bass",
            "dnb",
            "jungle",
            "dubstep",
            "breakbeat",
            "garage",
            "ambient",
            "idm",
            "disco",
            "synthpop",
            "eurobeat",
            "dance",
            "edm",
        }
    ),
    "Hip Hop": frozenset(
        {"hip-hop", "hip hop", "rap", "trap", "drill", "grime", "phonk", "boom bap"}
    ),
    "Rock & Metal": frozenset(
        {"rock", "metal", "punk", "grunge", "shoegaze", "emo", "hardcore", "goth", "industrial"}
    ),
    "Soul": frozenset(
        {
            "soul",
            "r&b",
            "rnb",
            "funk",
            "jazz",
            "blues",
            "reggae",
            "dub",
            "afrobeat",
            "latin",
            "world",
            "country",
            "classical",
        }
    ),
    "Pop": frozenset({"pop", "indie pop", "k-pop", "j-pop"}),
}

ENDPOINT_WEIGHTS = {"track": 1.0, "album": 0.75, "artist": 0.45}


@dataclass(frozen=True)
class Classification:
    genre: str
    confidence: float
    status: str
    reason: str
    taxonomy_version: int = TAXONOMY_VERSION


def classify_tag_evidence(
    evidence: Iterable[tuple[str, str, Iterable[Mapping[str, object]]]],
) -> Classification:
    """Classify trusted tags, returning ``review`` unless evidence is decisive.

    Each item is ``(endpoint, provider_status, tags)``. Tags must have a name
    and optional numeric count. Provider failures are retained in the reason,
    but never turned into an automatic genre guess.
    """
    scores: dict[str, float] = defaultdict(float)
    sources: set[str] = set()
    failures: list[str] = []
    for endpoint, provider_status, tags in evidence:
        if provider_status != "ok":
            failures.append(f"{endpoint}:{provider_status}")
            continue
        weight = ENDPOINT_WEIGHTS.get(endpoint)
        if weight is None:
            failures.append(f"{endpoint}:unsupported")
            continue
        for tag in tags:
            name = str(tag.get("name", "")).casefold().strip()
            count = float(tag.get("count", 0) or 0)
            for genre, terms in GENRE_TERMS.items():
                if name in terms:
                    scores[genre] += weight * max(1.0, min(count, 100.0) / 20.0)
                    sources.add(endpoint)
    if not scores:
        suffix = f"; {', '.join(failures)}" if failures else ""
        return Classification("Review", 0.0, "review", "no supported genre tags" + suffix)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    genre, lead = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = lead / (lead + runner_up) if lead + runner_up else 0.0
    if confidence < 0.75 or lead < 1.0 or sources == {"artist"}:
        return Classification(
            "Review",
            confidence,
            "review",
            f"ambiguous/weak evidence: {genre}={lead:.2f}, runner-up={runner_up:.2f}",
        )
    return Classification(
        genre,
        confidence,
        "ready",
        f"taxonomy v{TAXONOMY_VERSION}; Last.fm evidence: {', '.join(sorted(sources))}; score={lead:.2f}",
    )

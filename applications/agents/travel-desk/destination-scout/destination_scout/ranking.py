"""Destination ranking: embedding similarity to the trip request, with a keyword-order fallback."""
import logging
import math

from pydantic_ai import Embedder
from travel_common.contracts import DestinationRequest

log = logging.getLogger(__name__)


def destination_text(d: dict) -> str:
    return ", ".join(filter(None, [
        d.get("name"), d.get("country"), d.get("blurb"),
        "vibes: " + "/".join(d.get("vibes", [])),
        "activities: " + "/".join(d.get("activities", [])),
    ]))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a, norm_b = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


async def rank_by_similarity(candidates: list[dict], request: DestinationRequest, embedder: Embedder) -> list[dict]:
    """Re-rank keyword-filtered candidates by embedding similarity to the trip request."""
    if len(candidates) <= 1:
        return candidates
    query = (f"A {request.budget_level or 'any'}-budget trip with vibes: {', '.join(request.vibes) or 'any'}. "
             f"Preferred activities: {', '.join(request.activities) or 'any'}.")
    try:
        query_vector = (await embedder.embed_query(query)).embeddings[0]
        doc_vectors = (await embedder.embed_documents([destination_text(d) for d in candidates])).embeddings
    except Exception:
        log.warning("embedding-based ranking unavailable, keeping keyword-filter order", exc_info=True)
        return candidates
    ranked = sorted(zip(candidates, doc_vectors), key=lambda pair: cosine(query_vector, pair[1]), reverse=True)
    return [candidate for candidate, _ in ranked]


def parse_pool(raw) -> list[dict]:
    """The destinations list from an MCP tool result (structured dict, or a dict wrapped as `result`)."""
    if isinstance(raw, dict):
        raw = raw.get("result", raw) if "destinations" not in raw else raw
        return list(raw.get("destinations", [])) if isinstance(raw, dict) else []
    return []

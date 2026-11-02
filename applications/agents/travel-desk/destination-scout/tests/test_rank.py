import types

from destination_scout.ranking import cosine, parse_pool, rank_by_similarity
from travel_common.contracts import DestinationRequest

REQ = DestinationRequest(vibes=["culture"], budget_level="mid", activities=["museums"], origin="AMS", limit=2)
POOL = [{"name": "A"}, {"name": "B"}, {"name": "C"}]


class FakeEmbedder:
    """Embeds texts as 1-D vectors: query -> [1, 0]; documents -> chosen unit vectors by name."""
    vectors = {"A": [0.0, 1.0], "B": [1.0, 0.0], "C": [0.7, 0.7]}

    async def embed_query(self, q):
        return types.SimpleNamespace(embeddings=[[1.0, 0.0]])

    async def embed_documents(self, docs):
        return types.SimpleNamespace(embeddings=[self.vectors[d.split(",")[0]] for d in docs])


class BrokenEmbedder:
    async def embed_query(self, q):
        raise ConnectionError("litellm down")


async def test_ranks_by_cosine_similarity():
    ranked = await rank_by_similarity(POOL, REQ, FakeEmbedder())
    assert [d["name"] for d in ranked] == ["B", "C", "A"]


async def test_embedding_failure_keeps_keyword_order():
    assert await rank_by_similarity(POOL, REQ, BrokenEmbedder()) == POOL


async def test_single_candidate_skips_embedding():
    assert await rank_by_similarity(POOL[:1], REQ, BrokenEmbedder()) == POOL[:1]


def test_cosine_handles_zero_vectors():
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_parse_pool_accepts_plain_and_wrapped_results():
    assert parse_pool({"destinations": [{"name": "A"}]}) == [{"name": "A"}]
    assert parse_pool({"result": {"destinations": [{"name": "A"}]}}) == [{"name": "A"}]
    assert parse_pool("garbage") == []

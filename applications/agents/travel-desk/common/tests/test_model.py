from travel_common.mcp import agentgateway_url
from travel_common.model import build_model


def test_model_defaults_to_the_gateways_openai_route(monkeypatch):
    for var in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    model = build_model()
    assert model.model_name == "gpt-4o-mini"
    assert str(model.client.base_url).startswith("http://agentgateway.lab/llm/openai/v1")
    assert model.client.api_key == "sk-ai-lab-gateway"


def test_the_route_in_the_base_url_picks_the_model(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://agentgateway.lab/llm/gemini/v1")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-pro")
    model = build_model()
    assert model.model_name == "gemini-2.5-pro"
    assert str(model.client.base_url).startswith("http://agentgateway.lab/llm/gemini/v1")


def test_embeddings_stay_on_the_openai_route_when_chat_moves(monkeypatch):
    from travel_common.model import _provider

    monkeypatch.setenv("LLM_BASE_URL", "http://agentgateway.lab/llm/local/v1")   # chat moved to the local model
    monkeypatch.delenv("LLM_EMBEDDINGS_BASE_URL", raising=False)
    assert str(_provider("LLM_EMBEDDINGS_BASE_URL").base_url).startswith("http://agentgateway.lab/llm/openai/v1")
    monkeypatch.setenv("LLM_EMBEDDINGS_BASE_URL", "http://example.test/v1")
    assert str(_provider("LLM_EMBEDDINGS_BASE_URL").base_url).startswith("http://example.test/v1")


def test_the_client_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k-from-secret")
    assert build_model().client.api_key == "k-from-secret"


def test_agentgateway_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_URL", "http://agentgateway.lab/")
    assert agentgateway_url() == "http://agentgateway.lab"


# ---- TripRequirements keeps to the vocabulary the services accept -------------------------------------------

from travel_common.contracts import TripRequirements  # noqa: E402


def req(**kw):
    return TripRequirements(traveller="A", origin="ams", date="2026-12-18", **kw)


def test_values_go_to_the_right_bucket_and_unknowns_are_dropped():
    r = req(vibes=["Design", "architecture", "vibey", "CITY"], activities=["museums", "food", "karaoke"])
    assert r.vibes == ["design", "city", "food"]
    assert r.activities == ["architecture", "museums"]


def test_bad_budget_becomes_mid_and_origin_is_normalised():
    assert req(budget_level="cheap").budget_level == "mid"
    assert req(budget_level=" HIGH ").budget_level == "high"
    assert req().origin == "AMS"


def test_duplicates_collapse():
    assert req(vibes=["food", "Food", "food"]).vibes == ["food"]

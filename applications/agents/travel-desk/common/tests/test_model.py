from travel_common.mcp import agentgateway_url
from travel_common.model import build_model


def test_model_defaults_to_litellm_alias(monkeypatch):
    for var in ("LITELLM_MODEL", "LITELLM_BASE_URL", "LITELLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    model = build_model()
    assert model.model_name == "openai-gpt"
    assert "litellm" in str(model.client.base_url)


def test_model_env_overrides(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.lab/v1")
    assert str(build_model("gemini-pro").client.base_url).startswith("http://litellm.lab/v1")


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

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

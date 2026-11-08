from pathlib import Path

from projektakte.config import DEFAULT_PROJECT_NUMBER_PATTERN, load_settings


def test_defaults_point_to_local_network_services():
    s = load_settings({})
    assert s.data_dir == Path("./data")
    assert s.llm_url == "http://192.168.178.62:8000/v1"
    assert s.embed_url == "http://192.168.178.62:8001"
    assert s.rerank_url == "http://192.168.178.62:4343/rerank"
    assert s.llm_model == "qwen3.8-flash-next"
    assert s.embed_model == "qwen3-embedding-4b"
    assert s.project_number_pattern == DEFAULT_PROJECT_NUMBER_PATTERN
    assert s.demo_mode is False and s.ai_mode == "real" and s.llm_vision is False
    assert s.projects_dir == Path("data/projekte")


def test_everything_comes_from_environment(tmp_path):
    s = load_settings({
        "DATA_DIR": str(tmp_path), "PROJECTS_DIR": "/srv/projekte", "MAIL_DIR": "/srv/mails",
        "PROJECT_NUMBER_PATTERN": r"P\d{5}", "LLM_URL": "http://llm:1/v1/", "LLM_MODEL": "m",
        "LLM_VISION": "1", "EMBED_URL": "http://e:2", "EMBED_MODEL": "em", "RERANK_URL": "http://r/rr",
        "DEMO_MODE": "1", "AI_MODE": "FAKE", "LLM_TIMEOUT": "5", "EMBED_TIMEOUT": "6",
        "RERANK_TIMEOUT": "7", "LLM_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": false}}',
    })
    assert s.projects_dir == Path("/srv/projekte") and s.mail_dir == Path("/srv/mails")
    assert s.project_number_pattern == r"P\d{5}"
    assert s.llm_url == "http://llm:1/v1" and s.llm_model == "m" and s.llm_vision
    assert (s.embed_url, s.embed_model, s.rerank_url) == ("http://e:2", "em", "http://r/rr")
    assert s.demo_mode and s.ai_mode == "fake"
    assert (s.llm_timeout, s.embed_timeout, s.rerank_timeout) == (5, 6, 7)
    assert s.llm_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_demo_mode_defaults_to_demo_folders(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "DEMO_MODE": "1"})
    assert s.projects_dir == tmp_path / "demo" / "projekte"
    assert s.mail_dir == tmp_path / "demo" / "mails"


def test_secrets_not_in_repr():
    s = load_settings({"LLM_API_KEY": "geheim123"})
    assert "geheim123" not in repr(s)

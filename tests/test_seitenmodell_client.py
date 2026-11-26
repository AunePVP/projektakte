"""The answer of the page model (TYP line, raw text, the ``kmw`` object) → a PageReading; and the settings for
it. Pure functions – the service itself is only reached by tests/test_seitenmodell_real.py."""
import pytest

from projektakte.ai.http import AIServiceError
from projektakte.ai.seitenmodell import TYPES, reading_from
from projektakte.ai.services import AIServices
from projektakte.config import load_settings

@pytest.fixture(autouse=True)
def _vision_dpi_restored(monkeypatch):
    """AIServices.from_settings sets VISION_DPI for the page model: put back after each test."""
    monkeypatch.setenv("VISION_DPI", "0")
    monkeypatch.delenv("VISION_DPI")


RAW = ("<x_0.0><y_0.0>Musterbüro GmbH · Beispielstadt<x_0.999><y_0.0164><class_Page-header>\n\n"
       "<x_0.0918><y_0.2516>Die Löschwassermenge beträgt 96 m³/h.<x_0.8906><y_0.2844><class_Text>")


def answer(content: str, kmw: dict | None = None) -> dict:
    data = {"model": "nemotron-kmw", "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                  "finish_reason": "stop"}]}
    if kmw is not None:
        data["kmw"] = kmw
    return data


def kmw(typ: str, **more) -> dict:
    return {"typ": typ, "kopf": typ, "sicherheit": 1.0, "tokens": 12, "finish": "stop", "ms": 2159,
            "bild": [1275, 1650], "schleife": False, "schleifen": []} | more


def test_text_page():
    r = reading_from(answer("TYP: Text\n" + RAW, kmw("Text")))
    assert r.typ == "Text" and r.kopf == "Text" and r.sicherheit == 1.0
    assert r.raw == RAW
    assert r.schleife is False and r.finish == "stop" and r.ms == 2159


def test_plan_has_no_raw_text():
    r = reading_from(answer("TYP: Plan", kmw("Plan")))
    assert (r.typ, r.raw) == ("Plan", "")


def test_type_from_kmw_when_line_is_missing():
    assert reading_from(answer("", kmw("Foto"))).typ == "Foto"


def test_unsicher_keeps_the_guess_of_the_head():
    r = reading_from(answer("TYP: Unsicher", kmw("Unsicher", kopf="Plan", sicherheit=0.41)))
    assert (r.typ, r.kopf, r.sicherheit) == ("Unsicher", "Plan", 0.41)


def test_answer_without_a_type_is_an_error():
    with pytest.raises(AIServiceError):
        reading_from(answer("OK"))


def test_unknown_type_is_an_error():
    with pytest.raises(AIServiceError):
        reading_from(answer("TYP: Tabelle\nirgendwas", kmw("Tabelle")))


def test_unexpected_shape_is_an_error():
    with pytest.raises(AIServiceError):
        reading_from({"choices": []})
    with pytest.raises(AIServiceError):
        reading_from(["kein", "Objekt"])


def test_loop_flag_and_cut_off():
    r = reading_from(answer("TYP: Text\n" + RAW, kmw("Text", schleife=True, finish="length")))
    assert r.schleife is True and r.finish == "length"


def test_type_line_tolerates_spacing_and_case():
    assert reading_from(answer("typ:  leer \n", kmw("Leer"))).typ == "Leer"


def test_types_are_the_five_of_the_service():
    assert TYPES == ("Text", "Plan", "Foto", "Leer", "Unsicher")


# -- settings ------------------------------------------------------------------------------------------------
def test_without_vision_url_nothing_changes():
    s = load_settings({})
    assert s.vision_url == ""
    ai = AIServices.from_settings(s)
    assert ai.vision is None
    assert ai.llm.page_model == ""


def test_vision_url_and_model():
    s = load_settings({"VISION_URL": "http://spark.example:8767/v1/"})
    assert s.vision_url == "http://spark.example:8767/v1"
    assert s.vision_model == "nemotron-kmw"
    assert load_settings({"VISION_URL": "http://x.example/v1", "VISION_MODEL": "anderes"}).vision_model == "anderes"


def test_fake_mode_ignores_vision_url():
    s = load_settings({"VISION_URL": "http://spark.example:8767/v1", "AI_MODE": "fake"})
    assert s.vision_url == ""
    assert any("VISION_URL" in w for w in s.config_warnings)


def test_warning_for_another_resolution():
    s = load_settings({"VISION_URL": "http://spark.example:8767/v1", "VISION_DPI": "150"})
    assert any("175" in w and "VISION_DPI" in w for w in s.config_warnings)
    quiet = load_settings({"VISION_URL": "http://spark.example:8767/v1", "VISION_DPI": "175"})
    assert not any("VISION_DPI" in w for w in quiet.config_warnings)


def test_vision_key_not_in_repr():
    s = load_settings({"VISION_URL": "http://spark.example:8767/v1", "VISION_API_KEY": "geheim-spark"})
    assert s.vision_api_key == "geheim-spark"
    assert "geheim-spark" not in repr(s)


def test_big_model_gets_no_images_with_page_model():
    s = load_settings({"VISION_URL": "http://spark.example:8767/v1", "LLM_VISION": "1", "VISION_TIMEOUT": "300"})
    ai = AIServices.from_settings(s)
    assert ai.llm.vision is False
    assert ai.llm.page_model == "nemotron-kmw"
    assert ai.vision is not None and ai.vision.model == "nemotron-kmw" and ai.vision.timeout == 300
    assert ai.llm.config()["page_model"] == "nemotron-kmw"
    assert type(ai.llm).from_config(ai.llm.config()).page_model == "nemotron-kmw"


def test_page_model_sets_175_dpi_unless_set(monkeypatch):
    from projektakte.extract import vision_dpi

    AIServices.from_settings(load_settings({}))
    assert vision_dpi() == 150
    AIServices.from_settings(load_settings({"VISION_URL": "http://spark.example:8767/v1"}))
    assert vision_dpi() == 175
    monkeypatch.setenv("VISION_DPI", "200")
    AIServices.from_settings(load_settings({"VISION_URL": "http://spark.example:8767/v1"}))
    assert vision_dpi() == 200


def test_health_check_has_three_services_without_page_model():
    s = load_settings({"LLM_URL": "http://127.0.0.1:9/v1", "EMBED_URL": "http://127.0.0.1:9",
                       "RERANK_URL": "http://127.0.0.1:9/rerank", "HEALTH_TIMEOUT": "0.2"})
    assert set(AIServices.from_settings(s).check()["services"]) == {"llm", "embeddings", "reranker"}


def test_health_check_names_the_page_model():
    s = load_settings({"LLM_URL": "http://127.0.0.1:9/v1", "EMBED_URL": "http://127.0.0.1:9",
                       "RERANK_URL": "http://127.0.0.1:9/rerank", "HEALTH_TIMEOUT": "0.2",
                       "VISION_URL": "http://127.0.0.1:9/v1"})
    result = AIServices.from_settings(s).check()
    svc = result["services"]["bildauswertung"]
    assert svc["ok"] is False and svc["model"] == "nemotron-kmw" and svc["error"]
    assert "fallback" in svc
    assert result["status"] == "degraded"

"""AI clients against the in-process fakes: both embedding formats, both reranker formats,
<think> stripping, timeouts and outages."""
import pytest

from projektakte.ai import AIServiceError
from projektakte.ai.embeddings import EmbeddingClient
from projektakte.ai.llm import LLMClient, strip_think
from projektakte.ai.rerank import RerankClient, parse_rerank_response


@pytest.mark.parametrize("raw,expected", [
    ("<think>hmm</think>\nAntwort", "Antwort"),
    ("<THINK>a\nb</THINK>Antwort <think>x</think>Ende", "Antwort Ende"),
    ("Vorüberlegung</think>Antwort", "Antwort"),
    ("Antwort<think>abgeschnitten", "Antwort"),
    ("<think>nur denken ohne Ende", ""),
    ("", ""),
])
def test_strip_think(raw, expected):
    assert strip_think(raw) == expected


def test_llm_chat_strips_think_and_reports_usage(fake_ai):
    c = LLMClient(f"{fake_ai.base_url}/v1", "qwen3.8-flash-next")
    res = c.chat([{"role": "user", "content": "Sag OK."}])
    assert res.text == "OK"
    assert "<think>" in res.raw_text
    assert res.usage["total_tokens"] > 0


def test_llm_vision_only_when_enabled(fake_ai):
    c = LLMClient(f"{fake_ai.base_url}/v1", "m", vision=False)
    with pytest.raises(AIServiceError):
        c.describe_image(b"\x89PNG", "image/png", "Lies den Plan")
    c = LLMClient(f"{fake_ai.base_url}/v1", "m", vision=True)
    assert "Fluchtwegplan" in c.describe_image(b"\x89PNG", "image/png", "Lies den Plan").text


@pytest.mark.parametrize("formats,expected", [(["openai"], "openai"), (["tei"], "tei"),
                                              (["openai", "tei"], "openai")])
def test_embedding_format_detection(fresh_fake_ai, formats, expected):
    fresh_fake_ai.options["embed_formats"] = formats
    c = EmbeddingClient(fresh_fake_ai.base_url, "qwen3-embedding-4b", batch_size=2)
    vecs = c.embed(["Rauchableitung", "Treppenraum", "Brandmeldeanlage"])
    assert c.format == expected and len(vecs) == 3 and c.dim == 256
    assert abs(sum(x * x for x in vecs[0]) - 1) < 1e-6


def test_embedding_accepts_v1_suffix_and_redetects_after_swap(fresh_fake_ai):
    c = EmbeddingClient(fresh_fake_ai.base_url + "/v1", "m")
    c.embed(["a b"])
    assert c.format == "openai"
    fresh_fake_ai.options["embed_formats"] = ["tei"]  # server swapped to TEI
    c.embed(["a b"])
    assert c.format == "tei"


def test_similar_texts_are_close(fake_ai):
    c = EmbeddingClient(fake_ai.base_url, "m")
    a, b, d = c.embed(["Rauchableitung im Treppenraum des Schulgebäudes",
                       "Treppenraum: Öffnung zur Rauchableitung", "Rechnung Büromaterial Toner"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))  # noqa: E731
    assert dot(a, b) > dot(a, d) + 0.2


def test_query_instruction_is_sent(fake_ai):
    c = EmbeddingClient(fake_ai.base_url, "m", query_instruction="Instruct: X\nQuery: ")
    q = c.embed_query("Rauchableitung")
    assert q == c.embed(["Rauchableitung"])[0]  # fake ignores the instruction part


@pytest.mark.parametrize("formats,expected", [(["tei"], "tei"), (["cohere"], "cohere")])
def test_rerank_both_formats(fresh_fake_ai, formats, expected):
    fresh_fake_ai.options["rerank_formats"] = formats
    c = RerankClient(fresh_fake_ai.base_url + "/rerank")
    scores = c.rerank("Rauchableitung Treppenraum",
                      ["Speiseplan der Kantine", "Rauchableitung im Treppenraum", "Treppenraum"])
    assert c.format == expected and c.response_format == expected
    assert scores[1] > scores[2] > scores[0]


def test_parse_rerank_response_formats():
    assert parse_rerank_response([{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}], 2) == (
        [0.1, 0.9], "tei")
    assert parse_rerank_response({"results": [{"index": 0, "relevance_score": 0.5}]}, 1) == (
        [0.5], "cohere")
    with pytest.raises(AIServiceError):
        parse_rerank_response({"foo": 1}, 1)


def test_unreachable_services_raise_clean_errors():
    dead = "http://127.0.0.1:9"  # discard port – nothing listens
    with pytest.raises(AIServiceError):
        LLMClient(dead + "/v1", "m", timeout=1).chat([{"role": "user", "content": "x"}])
    with pytest.raises(AIServiceError):
        EmbeddingClient(dead, "m", timeout=1).embed(["x"])
    rr = RerankClient(dead + "/rerank", timeout=1)
    with pytest.raises(AIServiceError):
        rr.rerank("q", ["a"])
    assert rr.last_error


def test_timeout(fresh_fake_ai):
    fresh_fake_ai.options["delay"] = 1.0
    with pytest.raises(AIServiceError, match="Zeitüberschreitung"):
        LLMClient(f"{fresh_fake_ai.base_url}/v1", "m", timeout=0.2).chat(
            [{"role": "user", "content": "x"}])


def test_server_error_is_reported(fresh_fake_ai):
    fresh_fake_ai.options["fail"] = ["rerank"]
    with pytest.raises(AIServiceError, match="500"):
        RerankClient(fresh_fake_ai.base_url + "/rerank").rerank("q", ["a"])


def test_llm_reports_finish_reason_and_reasoning(fresh_fake_ai):
    c = LLMClient(f"{fresh_fake_ai.base_url}/v1", "m")
    res = c.chat([{"role": "user", "content": "Sag OK."}])
    assert res.finish_reason == "stop" and not res.truncated
    fresh_fake_ai.options["chat_quirk"] = "reasoning_only"  # vLLM reasoning parser, budget used up
    res = c.chat([{"role": "user", "content": "Sag OK."}])
    assert res.text == "" and res.finish_reason == "length" and res.reasoning and res.truncated
    fresh_fake_ai.options["chat_quirk"] = "unterminated_think"
    res = c.chat([{"role": "user", "content": "Sag OK."}])
    assert res.text == "" and res.truncated and "<think>" in res.raw_text


def test_chat_result_stays_constructible_positionally():
    from projektakte.ai.llm import ChatResult
    r = ChatResult("Antwort [1]", "Antwort [1]", {}, "m", 1.0)
    assert r.finish_reason is None and r.reasoning == "" and not r.truncated


def test_llm_probe_without_model_list_is_ok(fresh_fake_ai):
    """ops-08: llama.cpp/proxies without GET /models – the mini chat decides, the list is a hint."""
    fresh_fake_ai.options["models_404"] = True
    info = LLMClient(f"{fresh_fake_ai.base_url}/v1", "qwen3.8-flash-next").probe(2.0)
    assert info["ok"] is True and info["format"] == "openai-chat" and info["sample"] == "OK"
    assert "404" in info["models_error"] and "Modellliste" in info["hint"]
    fresh_fake_ai.options["models_404"] = False
    info = LLMClient(f"{fresh_fake_ai.base_url}/v1", "anderes-modell").probe(2.0)
    assert info["ok"] is True and info["model_listed"] is False and "anderes-modell" in info["hint"]
    fresh_fake_ai.options["fail"] = ["chat"]
    info = LLMClient(f"{fresh_fake_ai.base_url}/v1", "qwen3.8-flash-next").probe(2.0)
    assert info["ok"] is False and "500" in info["error"]

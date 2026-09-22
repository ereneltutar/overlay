import datetime

import llm_fact_check as lfc

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


# --- _extract_text -----------------------------------------------------

def test_extract_text_concatenates_text_blocks_only():
    blocks = [
        {"type": "server_tool_use", "id": "t1"},
        {"type": "text", "text": "Part one."},
        {"type": "web_search_tool_result", "content": []},
        {"type": "text", "text": "Part two."},
    ]
    assert lfc._extract_text(blocks) == "Part one.\nPart two."


def test_extract_text_empty_or_none_returns_empty_string():
    assert lfc._extract_text([]) == ""
    assert lfc._extract_text(None) == ""


# --- parse_verdict -------------------------------------------------------

def test_parse_verdict_safe():
    text = "Some search reasoning...\nVERDICT: SAFE\nREASON: No contradicting evidence found."
    result = lfc.parse_verdict(text)
    assert result == {"verdict": "SAFE", "reason": "No contradicting evidence found."}


def test_parse_verdict_veto_case_insensitive():
    text = "verdict: veto\nreason: NVIDIA is already the largest company by market cap."
    result = lfc.parse_verdict(text)
    assert result["verdict"] == "VETO"


def test_parse_verdict_missing_format_falls_back_to_uncertain():
    result = lfc.parse_verdict("The model rambled without following instructions.")
    assert result["verdict"] == "UNCERTAIN"


def test_parse_verdict_missing_reason_line_still_parses_verdict():
    result = lfc.parse_verdict("VERDICT: SAFE")
    assert result["verdict"] == "SAFE"
    assert result["reason"] == "(no reason given)"


def test_parse_verdict_none_input():
    result = lfc.parse_verdict(None)
    assert result["verdict"] == "UNCERTAIN"


# --- ask_llm_fact_check (network mocked / missing key) ---------------------

def test_ask_llm_fact_check_missing_api_key_returns_error(monkeypatch):
    monkeypatch.delenv(lfc.ANTHROPIC_API_KEY_ENV, raising=False)
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "ERROR"
    assert "not set" in result["reason"]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_exc=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._payload


def test_ask_llm_fact_check_success_parses_verdict(monkeypatch):
    monkeypatch.setenv(lfc.ANTHROPIC_API_KEY_ENV, "test-key")
    payload = {"content": [
        {"type": "text", "text": "Researched via web search.\nVERDICT: VETO\nREASON: Already resolved."},
    ]}
    monkeypatch.setattr(lfc.requests, "post", lambda *a, **k: FakeResponse(payload=payload))
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "VETO"
    assert result["reason"] == "Already resolved."
    assert result["checked_at"] == NOW.isoformat()


def test_ask_llm_fact_check_request_exception_returns_error(monkeypatch):
    monkeypatch.setenv(lfc.ANTHROPIC_API_KEY_ENV, "test-key")

    def raise_request_exc(*a, **k):
        raise lfc.requests.RequestException("boom")

    monkeypatch.setattr(lfc.requests, "post", raise_request_exc)
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "ERROR"
    assert "boom" in result["reason"]


def test_ask_llm_fact_check_empty_reply_returns_error(monkeypatch):
    monkeypatch.setenv(lfc.ANTHROPIC_API_KEY_ENV, "test-key")
    monkeypatch.setattr(lfc.requests, "post", lambda *a, **k: FakeResponse(payload={"content": []}))
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "ERROR"


# --- passes_fact_check ---------------------------------------------------

def stub_llm(verdict):
    def _ask(question, recommended_side, deadline_str, now):
        return {"verdict": verdict, "reason": "stub", "checked_at": now.isoformat()}
    return _ask


def test_passes_fact_check_true_on_safe():
    passed, result = lfc.passes_fact_check("Q?", "YES", "2026-02-01T00:00:00Z", NOW, ask_llm=stub_llm("SAFE"))
    assert passed is True
    assert result["verdict"] == "SAFE"


def test_passes_fact_check_false_on_veto():
    passed, result = lfc.passes_fact_check("Q?", "YES", "2026-02-01T00:00:00Z", NOW, ask_llm=stub_llm("VETO"))
    assert passed is False


def test_passes_fact_check_fail_open_true_for_error_and_uncertain(monkeypatch):
    monkeypatch.setattr(lfc, "FAIL_OPEN", True)
    passed_error, _ = lfc.passes_fact_check("Q?", "YES", "d", NOW, ask_llm=stub_llm("ERROR"))
    passed_uncertain, _ = lfc.passes_fact_check("Q?", "YES", "d", NOW, ask_llm=stub_llm("UNCERTAIN"))
    assert passed_error is True
    assert passed_uncertain is True


def test_passes_fact_check_fail_closed_when_disabled(monkeypatch):
    monkeypatch.setattr(lfc, "FAIL_OPEN", False)
    passed_error, _ = lfc.passes_fact_check("Q?", "YES", "d", NOW, ask_llm=stub_llm("ERROR"))
    passed_uncertain, _ = lfc.passes_fact_check("Q?", "YES", "d", NOW, ask_llm=stub_llm("UNCERTAIN"))
    assert passed_error is False
    assert passed_uncertain is False


def test_passes_fact_check_veto_always_blocks_regardless_of_fail_open(monkeypatch):
    monkeypatch.setattr(lfc, "FAIL_OPEN", True)
    passed, _ = lfc.passes_fact_check("Q?", "YES", "d", NOW, ask_llm=stub_llm("VETO"))
    assert passed is False

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


# --- estimate_cost_usd / extract_usage --------------------------------

def test_estimate_cost_usd_token_only():
    # 1,000,000 input tokens @ $2/M + 1,000,000 output tokens @ $10/M, no searches
    assert lfc.estimate_cost_usd(1_000_000, 1_000_000, 0) == 12.0


def test_estimate_cost_usd_includes_web_search_flat_fee():
    assert lfc.estimate_cost_usd(0, 0, 3) == 0.03


def test_estimate_cost_usd_zero_usage_is_zero_cost():
    assert lfc.estimate_cost_usd(0, 0, 0) == 0.0


def test_extract_usage_reads_usage_object():
    body = {"usage": {"input_tokens": 2000, "output_tokens": 150,
                       "server_tool_use": {"web_search_requests": 2}}}
    usage = lfc.extract_usage(body)
    assert usage["input_tokens"] == 2000
    assert usage["output_tokens"] == 150
    assert usage["web_searches"] == 2
    assert usage["cost_usd"] == lfc.estimate_cost_usd(2000, 150, 2)


def test_extract_usage_missing_usage_object_returns_zero():
    assert lfc.extract_usage({}) == lfc.ZERO_USAGE


def test_extract_usage_missing_server_tool_use_defaults_zero_searches():
    body = {"usage": {"input_tokens": 500, "output_tokens": 50}}
    usage = lfc.extract_usage(body)
    assert usage["web_searches"] == 0


# --- ask_llm_fact_check (network mocked / missing key) ---------------------

def test_ask_llm_fact_check_missing_api_key_returns_error(monkeypatch):
    monkeypatch.delenv(lfc.ANTHROPIC_API_KEY_ENV, raising=False)
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "ERROR"
    assert "not set" in result["reason"]
    assert result["cost_usd"] == 0.0
    assert result["input_tokens"] == 0


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_exc=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            # Mirrors real requests.Response.raise_for_status(), which sets
            # .response on the HTTPError it raises -- _extract_error_detail
            # relies on that to read the real error body back out.
            self._raise_exc.response = self
            raise self._raise_exc

    def json(self):
        return self._payload


# --- _extract_error_detail -----------------------------------------------

class FakeErrorResponse:
    def __init__(self, status_code=400, payload=None, json_raises=False):
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


def test_extract_error_detail_pulls_api_message():
    exc = Exception("generic")
    exc.response = FakeErrorResponse(payload={"error": {"message": "Your credit balance is too low."}})
    assert lfc._extract_error_detail(exc) == "400: Your credit balance is too low."


def test_extract_error_detail_no_response_falls_back_to_str():
    assert lfc._extract_error_detail(Exception("connection refused")) == "connection refused"


def test_extract_error_detail_unparseable_body_falls_back_to_str():
    exc = Exception("bad gateway")
    exc.response = FakeErrorResponse(json_raises=True)
    assert lfc._extract_error_detail(exc) == "bad gateway"


def test_extract_error_detail_missing_message_falls_back_to_str():
    exc = Exception("boom")
    exc.response = FakeErrorResponse(payload={"error": {}})
    assert lfc._extract_error_detail(exc) == "boom"


def test_ask_llm_fact_check_success_parses_verdict_and_usage(monkeypatch):
    monkeypatch.setenv(lfc.ANTHROPIC_API_KEY_ENV, "test-key")
    payload = {
        "content": [
            {"type": "text", "text": "Researched via web search.\nVERDICT: VETO\nREASON: Already resolved."},
        ],
        "usage": {"input_tokens": 3000, "output_tokens": 120, "server_tool_use": {"web_search_requests": 2}},
    }
    monkeypatch.setattr(lfc.requests, "post", lambda *a, **k: FakeResponse(payload=payload))
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "VETO"
    assert result["reason"] == "Already resolved."
    assert result["checked_at"] == NOW.isoformat()
    assert result["input_tokens"] == 3000
    assert result["output_tokens"] == 120
    assert result["web_searches"] == 2
    assert result["cost_usd"] == lfc.estimate_cost_usd(3000, 120, 2)


def test_ask_llm_fact_check_request_exception_returns_error_with_zero_cost(monkeypatch):
    monkeypatch.setenv(lfc.ANTHROPIC_API_KEY_ENV, "test-key")

    def raise_request_exc(*a, **k):
        raise lfc.requests.RequestException("boom")

    monkeypatch.setattr(lfc.requests, "post", raise_request_exc)
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "ERROR"
    assert "boom" in result["reason"]
    assert result["cost_usd"] == 0.0


def test_ask_llm_fact_check_surfaces_real_api_error_message(monkeypatch):
    # Regression for the 2026-09-22 production incident: every fact-check
    # that day failed with the generic "400 Client Error: Bad Request for
    # url: ..." requests.HTTPError text, which gave no clue the real cause
    # was an empty Anthropic API credit balance -- diagnosing it took a
    # manual curl. The reason string must surface the API's own message.
    monkeypatch.setenv(lfc.ANTHROPIC_API_KEY_ENV, "test-key")
    error_body = {"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.",
    }}
    http_error = lfc.requests.exceptions.HTTPError("400 Client Error: Bad Request for url: ...")
    resp = FakeResponse(status_code=400, payload=error_body, raise_exc=http_error)
    monkeypatch.setattr(lfc.requests, "post", lambda *a, **k: resp)

    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "ERROR"
    assert "credit balance is too low" in result["reason"]
    assert result["cost_usd"] == 0.0


def test_ask_llm_fact_check_empty_reply_keeps_real_usage(monkeypatch):
    # The call was made (and may have been billed) even though the reply
    # text was unusable -- cost shouldn't be zeroed out in this case.
    monkeypatch.setenv(lfc.ANTHROPIC_API_KEY_ENV, "test-key")
    payload = {"content": [], "usage": {"input_tokens": 500, "output_tokens": 0}}
    monkeypatch.setattr(lfc.requests, "post", lambda *a, **k: FakeResponse(payload=payload))
    result = lfc.ask_llm_fact_check("Will X happen?", "YES", "2026-02-01T00:00:00Z", NOW)
    assert result["verdict"] == "ERROR"
    assert result["input_tokens"] == 500


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

"""
LLM-based fact-check gate
--------------------------
crypto_price_gate.py closed one specific blind spot (crypto threshold
markets vs. a live spot price) with a cheap, deterministic, no-API-key
check. Most markets don't have a numeric target and a free structured data
feed to check it against, though -- a 2026-09-22 review of open bets found
the exact same underlying problem (calibration_scan.py's bucket table only
knows a market's PRICE, never what it's actually about) producing bets that
were confidently wrong for reasons a five-second search would catch:
NVIDIA already being the world's most valuable company, a by-election
riding already engineered for the recommended-against candidate to win, an
incumbent regional chair the market was betting would be replaced, etc.
There's no cheap structured API for "is NVIDIA still #1" or "who's ahead in
this poll" the way there is for a crypto price -- that class of fact needs
an actual research step, not a formula.

This module is that step: it asks Claude, with live web search enabled (its
training data has a real cutoff and these are current-events questions), to
check whether the recommended side is already contradicted by verifiable,
current reality, and returns a VETO/SAFE/UNCERTAIN verdict plus a one-line
reason. Like crypto_price_gate.py, it's a veto on CONFIRMED problems, not a
probability model or a second opinion on genuinely uncertain bets (a
close election, an upcoming game) -- the prompt explicitly tells the model
not to veto on a hunch.

FAIL-OPEN BY DESIGN: an API error, a timeout, or the model itself answering
UNCERTAIN all fall through to "allow the bet" (see passes_fact_check). This
was a deliberate choice, not an oversight -- an Anthropic API outage
silently halting every new calibration/mispricing bet until it clears would
be a worse failure mode than occasionally missing a veto, and it matches
crypto_price_gate.py's existing philosophy (veto on confirmed evidence
only; never block on an inability to check). If that trade-off ever needs
revisiting, FAIL_OPEN below is the one switch to flip.

Every check (including vetoed ones, which otherwise leave no trace since a
vetoed candidate never becomes a bet) is meant to be logged by the caller
to docs/fact_check_log.jsonl for the same reason edge_pct_at_placement gets
stored on every bet: so a bad call is debuggable after the fact instead of
disappearing.
"""

import datetime
import os
import re

import requests

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_VERSION = "2023-06-01"
# Sonnet, not Haiku: low daily call volume (a handful of genuinely new
# calibration/mispricing candidates per run) makes the cost difference
# negligible, and a wrong VETO/SAFE call here risks real (paper) capital,
# same reasoning as picking a real per-market probability model over a
# cheaper heuristic elsewhere in this repo.
MODEL = "claude-sonnet-5"
# 1024 (the original value) was too tight for this prompt: with 2-5 web
# searches in play, the model regularly ran out of budget before reaching
# the closing VERDICT/REASON lines, surfacing as "Empty reply from the API"
# or "no parseable VERDICT line" -- 10 of 20 calls on 2026-09-23. Every one
# of those had already used more than 1024 output tokens when generation
# was cut off. 3000 gives room to finish the 4-5 search cases; the
# per-run call count (MAX_NEW_FACT_CHECKS_PER_RUN in track_bets.py) is the
# actual cost governor, not this.
MAX_TOKENS = 3000
REQUEST_TIMEOUT = 45  # web search adds real latency beyond a plain completion

FAIL_OPEN = True  # see module docstring; ERROR and UNCERTAIN both pass when True

# Pricing as of 2026-09-22 (https://platform.claude.com/docs/en/about-claude/pricing):
# Sonnet 5 is $2/$10 per million input/output tokens; the web search tool is
# a flat $0.01 per search on top of the token cost of whatever results it
# returns (those land in input_tokens like any other input). Hardcoded
# estimates, not fetched live -- same caveat as every other tunable constant
# in this repo that isn't sourced from a live API: update these by hand if
# Anthropic changes pricing. See fact_check_budget.py for how this rolls up
# into a daily/monthly spend report.
INPUT_TOKEN_COST_PER_MILLION = 2.00
OUTPUT_TOKEN_COST_PER_MILLION = 10.00
WEB_SEARCH_COST_PER_SEARCH = 0.01

ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}


def estimate_cost_usd(input_tokens: int, output_tokens: int, web_searches: int) -> float:
    """Estimated USD cost of one API call from its token/search counts (see
    the pricing constants above). Pure function, no network call."""
    token_cost = (input_tokens / 1_000_000) * INPUT_TOKEN_COST_PER_MILLION \
        + (output_tokens / 1_000_000) * OUTPUT_TOKEN_COST_PER_MILLION
    search_cost = web_searches * WEB_SEARCH_COST_PER_SEARCH
    return round(token_cost + search_cost, 6)


def extract_usage(response_body: dict) -> dict:
    """Pulls token/search counts out of a Messages API response's `usage`
    object and estimates the cost. Returns ZERO_USAGE (not an error) if
    `usage` is missing or malformed, since a response that made it this far
    already parsed -- this is about cost accounting, not correctness. Pure
    function of the parsed response body."""
    usage = response_body.get("usage") or {}
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        web_searches = int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)
    except (TypeError, ValueError):
        return dict(ZERO_USAGE)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "web_searches": web_searches,
        "cost_usd": estimate_cost_usd(input_tokens, output_tokens, web_searches),
    }

VERDICT_RE = re.compile(r"VERDICT:\s*(SAFE|VETO|UNCERTAIN)", re.IGNORECASE)
REASON_RE = re.compile(r"REASON:\s*(.+)", re.IGNORECASE)

PROMPT_TEMPLATE = """You are a fact-check gate for a prediction-market betting bot. The \
bot's statistical models sometimes recommend a bet that's already contradicted by \
easily-verifiable, CURRENT, real-world facts -- the models only see a market's price \
history, never what the market is actually about, so they can't catch this themselves.

Market question: {question}
Bot's recommended side: {side} (the bot is betting this side WILL be the correct \
resolution)
Market resolves by: {deadline}
Today's date: {today}

Use web search to check the CURRENT, objective state of whatever this question \
depends on -- current prices, standings, poll numbers, incumbency, official results, \
rosters, whatever is actually relevant. Only VETO if you find strong, current, \
verifiable evidence that the recommended side is likely WRONG. If you're not \
confident, or the question depends on genuine future uncertainty (a game that \
hasn't been played, a close election, a coin-flip event) rather than an \
already-settled or near-certain fact, answer SAFE or UNCERTAIN -- do not veto on a \
hunch or because something merely "seems risky."

End your reply with EXACTLY these two lines and nothing after them:
VERDICT: SAFE, VETO, or UNCERTAIN
REASON: one sentence explaining why
"""


def _extract_text(content_blocks) -> str:
    """Concatenates every text block in a Messages API response's `content`
    list, skipping tool-use/tool-result blocks (web search results show up
    as separate block types interleaved with the model's own text). Pure
    function of the parsed response body."""
    parts = []
    for block in content_blocks or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def parse_verdict(reply_text: str) -> dict:
    """Parses the VERDICT/REASON lines out of the model's free-text reply.
    Returns {"verdict": "SAFE"|"VETO"|"UNCERTAIN", "reason": str}; falls
    back to UNCERTAIN with a fixed reason if the model didn't follow the
    format (fail-open still applies to UNCERTAIN, so a malformed reply
    doesn't behave like a bug, just a missed check). Pure function, no
    network call."""
    verdict_match = VERDICT_RE.search(reply_text or "")
    reason_match = REASON_RE.search(reply_text or "")
    if not verdict_match:
        return {"verdict": "UNCERTAIN", "reason": "Model reply didn't include a parseable VERDICT line."}
    return {
        "verdict": verdict_match.group(1).upper(),
        "reason": reason_match.group(1).strip() if reason_match else "(no reason given)",
    }


def _extract_error_detail(exc: Exception) -> str:
    """Pulls the Anthropic API's own error message out of a failed
    request's response body (e.g. {"error": {"message": "Your credit
    balance is too low..."}}) instead of settling for the generic '400
    Client Error: Bad Request for url: ...' requests.HTTPError gives by
    default. That generic text alone can't tell "out of API credit" apart
    from "wrong model name" apart from a dozen other causes that all raise
    the same exception type -- the real diagnosis for this repo's first
    production failure (2026-09-22, all 40 fact-checks that day) took a
    manual curl to find, purely because this function was throwing away
    the response body. Falls back to str(exc) if there's no response
    object (a network-level failure never got a response at all) or its
    body isn't parseable JSON. Pure function, no network call."""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        body = response.json()
        message = (body.get("error") or {}).get("message")
    except (ValueError, AttributeError):
        return str(exc)
    if not message:
        return str(exc)
    return f"{response.status_code}: {message}"


def ask_llm_fact_check(question: str, recommended_side, deadline_str: str,
                        now: datetime.datetime) -> dict:
    """Calls the Claude API (web search enabled) to fact-check one market
    candidate. Returns {"verdict", "reason", "checked_at", "input_tokens",
    "output_tokens", "web_searches", "cost_usd"}; verdict is "ERROR" (not
    SAFE/VETO/UNCERTAIN) if the API key is missing, the request fails, or
    the response can't be parsed -- see FAIL_OPEN in passes_fact_check for
    how that's handled. Usage/cost fields are zeroed (ZERO_USAGE) on every
    ERROR path: a request that never completed was never billed for tokens
    it didn't use (Anthropic's own docs note a failed web search isn't
    billed either), so zero is the accurate cost, not just a placeholder.
    This is the one function in this module that hits the network;
    passes_fact_check takes it as an injectable parameter so it's never
    called for real in tests."""
    checked_at = now.isoformat()
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not api_key:
        return {"verdict": "ERROR", "reason": f"{ANTHROPIC_API_KEY_ENV} not set.",
                "checked_at": checked_at, **ZERO_USAGE}

    prompt = PROMPT_TEMPLATE.format(
        question=question,
        side=recommended_side or "YES",
        deadline=deadline_str or "unknown",
        today=now.date().isoformat(),
    )

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        return {"verdict": "ERROR", "reason": f"API request failed: {_extract_error_detail(exc)}",
                "checked_at": checked_at, **ZERO_USAGE}
    except ValueError as exc:
        return {"verdict": "ERROR", "reason": f"API request failed: invalid JSON response ({exc})",
                "checked_at": checked_at, **ZERO_USAGE}

    usage = extract_usage(body)

    reply_text = _extract_text(body.get("content"))
    if not reply_text:
        # The call was made (and may have been billed) even though the reply
        # was unusable, so this keeps the real usage/cost instead of zeroing it.
        return {"verdict": "ERROR", "reason": "Empty reply from the API.",
                "checked_at": checked_at, **usage}

    result = parse_verdict(reply_text)
    result["checked_at"] = checked_at
    result.update(usage)
    return result


def verdict_passes(result: dict) -> bool:
    """True unless `result` is a confirmed VETO. ERROR and UNCERTAIN both
    pass when FAIL_OPEN is True (the default -- see module docstring):
    this is a veto on CONFIRMED problems only, not a requirement to prove
    every bet safe, and an unreachable/ambiguous check should never be
    able to silently halt all new betting on its own.

    Factored out of passes_fact_check so a CACHED result (see
    track_bets.load_recent_fact_checks -- reusing a recent check instead
    of re-billing the API for the same still-open candidate every run)
    applies the exact same pass/fail rule a fresh call would, from one
    place, rather than a second copy of this logic living in track_bets.py."""
    if result["verdict"] == "VETO":
        return False
    if result["verdict"] in ("ERROR", "UNCERTAIN") and not FAIL_OPEN:
        return False
    return True


def passes_fact_check(question: str, recommended_side, deadline_str: str,
                       now: datetime.datetime, ask_llm=ask_llm_fact_check) -> tuple:
    """Calls `ask_llm` and applies verdict_passes to the result. Returns
    (passed: bool, result: dict) -- the caller logs `result` (verdict/
    reason/checked_at) to docs/fact_check_log.jsonl regardless of the
    outcome, since a VETO is exactly the case that would otherwise leave
    no trace (a vetoed candidate never becomes a bet)."""
    result = ask_llm(question, recommended_side, deadline_str, now)
    return verdict_passes(result), result

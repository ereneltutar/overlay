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
MAX_TOKENS = 1024
REQUEST_TIMEOUT = 45  # web search adds real latency beyond a plain completion

FAIL_OPEN = True  # see module docstring; ERROR and UNCERTAIN both pass when True

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


def ask_llm_fact_check(question: str, recommended_side, deadline_str: str,
                        now: datetime.datetime) -> dict:
    """Calls the Claude API (web search enabled) to fact-check one market
    candidate. Returns {"verdict", "reason", "checked_at"}; verdict is
    "ERROR" (not SAFE/VETO/UNCERTAIN) if the API key is missing, the
    request fails, or the response can't be parsed -- see FAIL_OPEN in
    passes_fact_check for how that's handled. This is the one function in
    this module that hits the network; passes_fact_check takes it as an
    injectable parameter so it's never called for real in tests."""
    checked_at = now.isoformat()
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not api_key:
        return {"verdict": "ERROR", "reason": f"{ANTHROPIC_API_KEY_ENV} not set.", "checked_at": checked_at}

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
    except (requests.RequestException, ValueError) as exc:
        return {"verdict": "ERROR", "reason": f"API request failed: {exc}", "checked_at": checked_at}

    reply_text = _extract_text(body.get("content"))
    if not reply_text:
        return {"verdict": "ERROR", "reason": "Empty reply from the API.", "checked_at": checked_at}

    result = parse_verdict(reply_text)
    result["checked_at"] = checked_at
    return result


def passes_fact_check(question: str, recommended_side, deadline_str: str,
                       now: datetime.datetime, ask_llm=ask_llm_fact_check) -> tuple:
    """True unless the fact-check comes back a confirmed VETO. ERROR and
    UNCERTAIN both pass when FAIL_OPEN is True (the default -- see module
    docstring): this is a veto on CONFIRMED problems only, not a
    requirement to prove every bet safe, and an unreachable/ambiguous check
    should never be able to silently halt all new betting on its own.

    Returns (passed: bool, result: dict) -- the caller logs `result`
    (verdict/reason/checked_at) to docs/fact_check_log.jsonl regardless of
    the outcome, since a VETO is exactly the case that would otherwise
    leave no trace (a vetoed candidate never becomes a bet)."""
    result = ask_llm(question, recommended_side, deadline_str, now)
    if result["verdict"] == "VETO":
        return False, result
    if result["verdict"] in ("ERROR", "UNCERTAIN") and not FAIL_OPEN:
        return False, result
    return True, result

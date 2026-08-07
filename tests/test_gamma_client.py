from unittest.mock import MagicMock, patch

import gamma_client as gc


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.raise_for_status = MagicMock()
    return r


def test_fetch_market_state_not_closed():
    with patch.object(gc.requests, "get", return_value=_resp(json_data={"closed": False})):
        assert gc.fetch_market_state("m1") == (False, None)


def test_fetch_market_state_closed_yes():
    data = {"closed": True, "outcomePrices": '["0.99", "0.01"]'}
    with patch.object(gc.requests, "get", return_value=_resp(json_data=data)):
        assert gc.fetch_market_state("m1") == (True, True)


def test_fetch_market_state_closed_no():
    data = {"closed": True, "outcomePrices": '["0.01", "0.99"]'}
    with patch.object(gc.requests, "get", return_value=_resp(json_data=data)):
        assert gc.fetch_market_state("m1") == (True, False)


def test_fetch_market_state_closed_ambiguous():
    data = {"closed": True, "outcomePrices": '["0.5", "0.5"]'}
    with patch.object(gc.requests, "get", return_value=_resp(json_data=data)):
        assert gc.fetch_market_state("m1") == (True, None)


def test_fetch_market_state_closed_missing_outcome_prices():
    data = {"closed": True}
    with patch.object(gc.requests, "get", return_value=_resp(json_data=data)):
        assert gc.fetch_market_state("m1") == (True, None)


def test_fetch_market_state_network_error():
    with patch.object(gc.requests, "get", side_effect=gc.requests.RequestException("boom")):
        assert gc.fetch_market_state("m1") == (False, None)


def test_fetch_market_state_retries_on_429_then_succeeds():
    responses = [_resp(status_code=429), _resp(json_data={"closed": False})]
    with patch.object(gc.requests, "get", side_effect=responses):
        with patch.object(gc.time, "sleep"):
            assert gc.fetch_market_state("m1") == (False, None)


def test_fetch_market_state_gives_up_after_max_429_retries():
    responses = [_resp(status_code=429)] * (gc.MAX_RETRIES_ON_429 + 1)
    with patch.object(gc.requests, "get", side_effect=responses):
        with patch.object(gc.time, "sleep"):
            assert gc.fetch_market_state("m1") == (False, None)


def test_fetch_market_state_outcome_prices_as_list_not_string():
    data = {"closed": True, "outcomePrices": ["0.99", "0.01"]}
    with patch.object(gc.requests, "get", return_value=_resp(json_data=data)):
        assert gc.fetch_market_state("m1") == (True, True)

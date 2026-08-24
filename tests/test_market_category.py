import market_category as mc


def test_is_crypto_market_bitcoin():
    assert mc.is_crypto_market("Will Bitcoin reach $65,000 on August 18?") is True


def test_is_crypto_market_ethereum():
    assert mc.is_crypto_market("Will the price of Ethereum be above $2,000 on August 25?") is True


def test_is_crypto_market_xrp():
    assert mc.is_crypto_market("Will XRP reach $1.60 in August?") is True


def test_is_crypto_market_case_insensitive():
    assert mc.is_crypto_market("will BITCOIN dip to $60,000 in August?") is True


def test_is_crypto_market_non_crypto_returns_false():
    assert mc.is_crypto_market("Will Fulham FC win on 2026-08-24?") is False


def test_is_crypto_market_does_not_match_substring_false_positives():
    # "eth" and "btc" must not match inside unrelated words
    assert mc.is_crypto_market("Will Elizabeth win the election?") is False
    assert mc.is_crypto_market("Will the Bethlehem team advance?") is False


def test_is_crypto_market_none_or_empty_returns_false():
    assert mc.is_crypto_market(None) is False
    assert mc.is_crypto_market("") is False

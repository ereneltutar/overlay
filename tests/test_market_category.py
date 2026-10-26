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


def test_is_sports_market_game_lines():
    for q in ["Ravens vs. Falcons", "Spread: Texans (-6.5)", "Notre Dame vs. BYU: O/U 45.5",
              "LoL: Natus Vincere vs FlyQuest - Game 2 Winner",
              "Exact Score: Arsenal FC 1 - 0 Coventry City FC?", "Club Puebla leading at halftime?",
              "CA Osasuna to win the second half?", "Will Fulham FC win on 2026-08-24?",
              "Will Portland Timbers vs. San Diego FC end in a draw?"]:
        assert mc.is_sports_market(q) is True, q


def test_is_sports_market_leaves_futures_and_politics_general():
    for q in ["Will Argentina win the 2026 FIFA World Cup?",
              "Will the Democratic Party win the CA-14 House seat?",
              "Will Elon Musk post 340-359 tweets from October 13 to October 20, 2026?",
              "Iran-Oman Hormuz Agreement by November 30?", None, ""]:
        assert mc.is_sports_market(q) is False, q


def test_category_crypto_wins_over_sports():
    assert mc.category("Bitcoin vs. Ethereum: which performs better?") == "crypto"
    assert mc.category("Ravens vs. Falcons") == "sports"
    assert mc.category("Will the Fed cut rates?") == "general"

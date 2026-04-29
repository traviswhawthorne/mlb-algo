"""
EV Calculator
=============
Expected Value and Kelly Criterion bet sizing.

EV = (win_prob * net_profit) - (loss_prob * 1)
Kelly fraction f = (b*p - q) / b
  where b = net profit per $1, p = win prob, q = loss prob
"""


def american_to_decimal(american_odds):
    """Convert American odds to decimal odds."""
    if american_odds is None or american_odds == 0:
        return None
    if american_odds > 0:
        return (american_odds / 100.0) + 1.0
    else:
        return (100.0 / abs(american_odds)) + 1.0


def prob_to_american_odds(prob):
    """Convert a win probability to American odds (rounded to nearest 5)."""
    if prob <= 0 or prob >= 1:
        return 0
    decimal = 1.0 / prob
    if decimal >= 2.0:
        raw = (decimal - 1) * 100
    else:
        raw = -100.0 / (decimal - 1)
    # Round to nearest 5 to look like real odds
    return int(round(raw / 5.0) * 5)


def calculate_ev(win_prob, american_odds):
    """
    Expected value of a bet as a fraction (0.05 = 5% edge).
    Positive = profitable bet, negative = losing bet.
    """
    decimal = american_to_decimal(american_odds)
    if decimal is None:
        return None
    net_profit = decimal - 1.0
    ev = (win_prob * net_profit) - ((1.0 - win_prob) * 1.0)
    return round(ev, 5)


def kelly_fraction(win_prob, american_odds, kelly_scale=0.25):
    """
    Kelly Criterion bet size as a fraction of bankroll.
    kelly_scale: multiply full Kelly by this (0.25 = quarter Kelly).
    Returns 0 if the bet has no edge.
    """
    decimal = american_to_decimal(american_odds)
    if decimal is None:
        return 0
    b = decimal - 1.0
    p = win_prob
    q = 1.0 - win_prob
    full_kelly = (b * p - q) / b
    if full_kelly <= 0:
        return 0
    return round(full_kelly * kelly_scale, 5)


def analyze_bet(model_prob, american_odds, bankroll,
                kelly_scale=0.25, min_ev=0.02):
    """
    Full analysis of one bet opportunity.

    Returns a dict with all metrics if the bet clears the EV threshold,
    otherwise returns None.
    """
    if american_odds is None or model_prob is None:
        return None
    if abs(american_odds) > 300:   # filter data errors / extreme lines
        return None

    ev = calculate_ev(model_prob, american_odds)
    if ev is None or ev < min_ev:
        return None

    frac = kelly_fraction(model_prob, american_odds, kelly_scale)
    # Normalize Kelly into [$10, $55]: treat 20% Kelly as the ceiling
    MAX_KELLY = 0.20
    normalized = min(frac / MAX_KELLY, 1.0)
    raw_dollars = 10 + normalized * (55 - 10)
    bet_dollars = (int(raw_dollars) // 5) * 5
    bet_dollars = max(10, min(55, bet_dollars))

    return {
        "ev":            round(ev, 4),
        "ev_pct":        f"+{ev * 100:.1f}%",
        "kelly_frac":    frac,
        "bet_amount":    bet_dollars,
        "model_prob":    round(model_prob, 4),
        "model_odds":    prob_to_american_odds(model_prob),
        "book_odds":     american_odds,
        "book_imp_prob": round(1.0 / american_to_decimal(american_odds), 4)
    }

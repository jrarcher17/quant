"""Market symbol helpers."""


def to_twelve_data_symbol(symbol: str) -> str:
    """Convert compact symbols like EURUSD or XAUUSD to Twelve Data format."""
    normalized = symbol.strip().upper()
    if "/" in normalized:
        return normalized
    if len(normalized) == 6:
        return f"{normalized[:3]}/{normalized[3:]}"
    return normalized

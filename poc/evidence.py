"""What the LLM is allowed to see about a column, in one place, so both prompts
use the same evidence and ablations flip a single switch."""


def column_summary(prof, use_names=True, use_values=True):
    s = {
        "declared_type": prof["declared_type"],
        "inferred_type": prof["family"],
        "role_guess": prof["role"],
        "null_rate": round(prof["null_rate"], 3),
        "distinct": prof["distinct"],
        "uniqueness": round(prof["uniqueness"], 3),
    }
    if use_names:
        s = {"name": prof["name"], **s}
    if use_values:
        s["top_values"] = prof["top_values"][:5]
        s["sample_values"] = prof["sample_values"][:5]
        if prof.get("numeric"):
            s["numeric"] = {k: _round(v) for k, v in prof["numeric"].items()}
        if prof.get("date_range"):
            s["date_range"] = prof["date_range"]
    return s


def sample_rows(table_profile, n, use_values=True):
    return table_profile["sample_rows"][:n] if use_values else []


def _round(v):
    return round(v, 3) if isinstance(v, float) else v

"""Human-readable durations shared by settings and applicant notifications."""

from utils.i18n import t


def format_duration(language, seconds):
    seconds = max(int(seconds), 0)
    parts = []
    for unit, size in (
        ("days", 86400),
        ("hours", 3600),
        ("minutes", 60),
        ("seconds", 1),
    ):
        value, seconds = divmod(seconds, size)
        if value:
            parts.append(t(language, f"setting_vote_duration_{unit}", **{unit: value}))
    return " ".join(parts) or t(language, "setting_vote_duration_seconds", seconds=0)

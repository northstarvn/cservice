from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_LOCALES = {
    "en": {
        "default": "English",
        "fallback": "en",
    },
    "es": {
        "default": "Espa\u00f1ol",
        "fallback": "en",
    },
}


@dataclass(frozen=True)
class LocaleResolution:
    requested: str
    resolved: str
    fallback_used: bool


def resolve_locale(requested: str | None) -> LocaleResolution:
    normalized = (requested or "en").strip().lower().replace("_", "-")
    primary = normalized.split("-")[0] if normalized else "en"
    if primary in SUPPORTED_LOCALES:
        return LocaleResolution(requested=normalized, resolved=primary, fallback_used=primary != normalized)
    return LocaleResolution(requested=normalized, resolved="en", fallback_used=True)


def locale_payload(requested: str | None = None) -> dict[str, object]:
    resolution = resolve_locale(requested)
    config = SUPPORTED_LOCALES[resolution.resolved]
    return {
        "requested": resolution.requested,
        "resolved": resolution.resolved,
        "fallback_used": resolution.fallback_used,
        "supported_locales": sorted(SUPPORTED_LOCALES),
        "fallback_locale": config["fallback"],
        "display_name": config["default"],
    }

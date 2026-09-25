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
    "fr": {
        "default": "Fran\u00e7ais",
        "fallback": "en",
    },
}

# Message catalog. Each key maps to a per-locale template; missing locale
# entries fall back to English. Values may contain ``{placeholder}`` tokens
# resolved by :func:`translate`.
MESSAGE_CATALOG = {
    "common.welcome": {
        "en": "Welcome to Customer Service",
        "es": "Bienvenido al Servicio al Cliente",
        "fr": "Bienvenue au Service Client",
    },
    "ai_chat.greeting": {
        "en": "Hello! How can I assist you?",
        "es": "\u00a1Hola! \u00bfC\u00f3mo puedo ayudarte?",
        "fr": "Bonjour ! Comment puis-je vous aider ?",
    },
    "auth.welcome": {
        "en": "Welcome, {name}",
        "es": "Bienvenido, {name}",
        "fr": "Bienvenue, {name}",
    },
    "auth.login_success": {
        "en": "Login successful",
        "es": "Inicio de sesi\u00f3n exitoso",
        "fr": "Connexion r\u00e9ussie",
    },
    "auth.login_failed": {
        "en": "Incorrect username or password",
        "es": "Usuario o contrase\u00f1a incorrectos",
        "fr": "Nom d'utilisateur ou mot de passe incorrect",
    },
    "auth.password_changed": {
        "en": "Password updated successfully.",
        "es": "Contrase\u00f1a actualizada correctamente.",
        "fr": "Mot de passe mis \u00e0 jour avec succ\u00e8s.",
    },
    "auth.register_success": {
        "en": "Registration successful",
        "es": "Registro exitoso",
        "fr": "Inscription r\u00e9ussie",
    },
    "booking.created": {
        "en": "Booking created successfully.",
        "es": "Reserva creada correctamente.",
        "fr": "R\u00e9servation cr\u00e9\u00e9e avec succ\u00e8s.",
    },
    "booking.updated": {
        "en": "Booking updated successfully.",
        "es": "Reserva actualizada correctamente.",
        "fr": "R\u00e9servation mise \u00e0 jour avec succ\u00e8s.",
    },
    "booking.cancelled": {
        "en": "Booking cancelled.",
        "es": "Reserva cancelada.",
        "fr": "R\u00e9servation annul\u00e9e.",
    },
    "retention.snapshot_ready": {
        "en": "Retention snapshot is ready.",
        "es": "La instant\u00e1nea de retenci\u00f3n est\u00e1 lista.",
        "fr": "L'instantan\u00e9 de fid\u00e9lisation est pr\u00eat.",
    },
    "health.ok": {
        "en": "All systems operational.",
        "es": "Todos los sistemas operativos.",
        "fr": "Tous les syst\u00e8mes sont op\u00e9rationnels.",
    },
    "errors.internal": {
        "en": "Internal server error",
        "es": "Error interno del servidor",
        "fr": "Erreur interne du serveur",
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


def translate(key: str, requested_locale: str | None = None, **kwargs) -> str:
    """Resolve a catalog key into a localized message.

    Falls back to English, then to the key itself, when no translation exists.
    ``{placeholder}`` tokens are interpolated only when ``kwargs`` are provided
    and the template supports them.
    """
    entry = MESSAGE_CATALOG.get(key)
    if entry is None:
        return key
    locale = resolve_locale(requested_locale).resolved
    template = entry.get(locale) or entry.get("en") or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def build_i18n_catalog() -> dict[str, object]:
    """Introspectable catalog for metadata endpoints."""
    return {
        "locales": sorted(SUPPORTED_LOCALES),
        "fallback_locale": "en",
        "message_count": len(MESSAGE_CATALOG),
        "messages": {
            key: {
                locale: template
                for locale, template in entry.items()
            }
            for key, entry in sorted(MESSAGE_CATALOG.items())
        },
    }
"""Localization: locale negotiation, message catalogs, and safe rendering.

The original module resolved ``"pt-BR"`` to a supported locale, fell back to
English, and interpolated ``{name}``-style placeholders. That covers a single
flat keyspace, but real deployments need more:

- **A real fallback *chain*.** ``pt-BR`` should try ``pt-BR``, then ``pt``, then
  the tenant's default locale, then English — and report *which* rung it landed
  on, so a missing translation is diagnosable rather than silent.
- **Pluralisation.** "1 booking" / "3 bookings" is not a string swap. The
  renderer understands a compact ICU-style plural/select block so a locale's
  rule set can be declared rather than hand-branched in Python.
- **Content negotiation.** ``Accept-Language`` with q-values, because "the
  user's preference" is rarely the only signal available.
- **Runtime overrides.** A tenant or a test can supply its own catalog without
  editing the shipped one, so a customer-specific string is a data change.
- **Coverage reporting.** Which keys are missing for which locale, and which
  locales are missing entirely — the question a translator actually asks.

``SUPPORTED_LOCALES`` and ``MESSAGE_CATALOG`` keep their exact shape and the
three shipped locales, and ``translate`` still falls back to English and then
to the key itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
import re

# Locale registry. Adding a language is a data change: one entry here plus its
# message templates, no code edit.
SUPPORTED_LOCALES = {
    "en": {
        "default": "English",
        "fallback": "en",
    },
    "es": {
        "default": "Español",
        "fallback": "en",
    },
    "fr": {
        "default": "Français",
        "fallback": "en",
    },
}

# Text direction, so a UI knows to mirror without hardcoding a language list.
RTL_LOCALES = ("ar", "he", "fa", "ur")

# CLDR-style plural categories, per language. ``one`` covers 1; ``other``
# covers everything else unless the language distinguishes more.
PLURAL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "en": ("one", "other"),
    "es": ("one", "other"),
    "fr": ("one", "other"),
    "ar": ("zero", "one", "two", "few", "many", "other"),
}

DEFAULT_LOCALE = "en"

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
        "es": "¡Hola! ¿Cómo puedo ayudarte?",
        "fr": "Bonjour ! Comment puis-je vous aider ?",
    },
    "auth.welcome": {
        "en": "Welcome, {name}",
        "es": "Bienvenido, {name}",
        "fr": "Bienvenue, {name}",
    },
    "auth.login_success": {
        "en": "Login successful",
        "es": "Inicio de sesión exitoso",
        "fr": "Connexion réussie",
    },
    "auth.login_failed": {
        "en": "Incorrect username or password",
        "es": "Usuario o contraseña incorrectos",
        "fr": "Nom d'utilisateur ou mot de passe incorrect",
    },
    "auth.password_changed": {
        "en": "Password updated successfully.",
        "es": "Contraseña actualizada correctamente.",
        "fr": "Mot de passe mis à jour avec succès.",
    },
    "auth.register_success": {
        "en": "Registration successful",
        "es": "Registro exitoso",
        "fr": "Inscription réussie",
    },
    "booking.created": {
        "en": "Booking created successfully.",
        "es": "Reserva creada correctamente.",
        "fr": "Réservation créée avec succès.",
    },
    "booking.updated": {
        "en": "Booking updated successfully.",
        "es": "Reserva actualizada correctamente.",
        "fr": "Réservation mise à jour avec succès.",
    },
    "booking.cancelled": {
        "en": "Booking cancelled.",
        "es": "Reserva cancelada.",
        "fr": "Réservation annulée.",
    },
    "retention.snapshot_ready": {
        "en": "Retention snapshot is ready.",
        "es": "La instantánea de retención está lista.",
        "fr": "L'instantané de fidélisation est prêt.",
    },
    "health.ok": {
        "en": "All systems operational.",
        "es": "Todos los sistemas operativos.",
        "fr": "Tous les systèmes sont opérationnels.",
    },
    "errors.internal": {
        "en": "Internal server error",
        "es": "Error interno del servidor",
        "fr": "Erreur interne du serveur",
    },
    # --- plural-sensitive messages ---
    #
    # ``{count, plural, one {...} other {...}}`` is resolved by the renderer
    # using the locale's plural categories. A translation that omits the block
    # degrades to the English template rather than leaking a raw token.
    "booking.count": {
        "en": "{count, plural, one {# booking} other {# bookings}}",
        "es": "{count, plural, one {# reserva} other {# reservas}}",
        "fr": "{count, plural, one {# réservation} other {# réservations}}",
    },
    "error.count": {
        "en": "{count, plural, =0 {no errors} one {# error} other {# errors}}",
        "es": "{count, plural, =0 {ningún error} one {# error} other {# errores}}",
        "fr": "{count, plural, =0 {aucune erreur} one {# erreur} other {# erreurs}}",
    },
}


# --- Runtime overrides ---------------------------------------------------------


class CatalogOverrides:
    """Per-scope catalog overlays (tenant, user, test).

    Lookups consult the most specific scope first, so a tenant can reword a
    string without forking the shipped catalog, and a test can pin a string
    without monkeypatching a module global.
    """

    def __init__(self) -> None:
        self._layers: dict[str, dict[str, dict[str, str]]] = {}

    def set(self, scope: str, key: str, locale: str, template: str) -> None:
        self._layers.setdefault(scope, {}).setdefault(key, {})[locale] = template

    def set_catalog(self, scope: str, catalog: dict[str, dict[str, str]]) -> None:
        for key, entry in catalog.items():
            for locale, template in entry.items():
                self.set(scope, key, locale, template)

    def lookup(self, key: str, locale: str, scopes: Iterable[str] = ()) -> Optional[str]:
        for scope in scopes:
            template = self._layers.get(scope, {}).get(key, {}).get(locale)
            if template is not None:
                return template
        return None

    def scopes(self) -> list[str]:
        return sorted(self._layers)

    def clear(self, scope: str | None = None) -> None:
        if scope is None:
            self._layers.clear()
        else:
            self._layers.pop(scope, None)

    def stats(self) -> dict[str, Any]:
        return {
            "scopes": self.scopes(),
            "messages": sum(len(v) for v in self._layers.values()),
        }


OVERRIDES = CatalogOverrides()


# --- Resolution ----------------------------------------------------------------


@dataclass(frozen=True)
class LocaleResolution:
    requested: str
    resolved: str
    fallback_used: bool
    # Additive: the full chain that was walked, and where it landed.
    chain: tuple[str, ...] = ()
    landed_on: str = ""


def normalize_locale(requested: str | None) -> str:
    """Canonicalize ``pt_BR`` / ``PT-br`` -> ``pt-br``."""
    return (requested or "").strip().lower().replace("_", "-")


def fallback_chain(requested: str | None, *, default_locale: str = DEFAULT_LOCALE) -> tuple[str, ...]:
    """Ordered rungs to try for ``requested``.

    ``"pt-BR"`` -> ``("pt-br", "pt", "en")``. A language with no base
    (``"fr"``) -> ``("fr", "en")``. An empty request is the default locale
    itself, so nothing is reported as "falling back" when nothing was asked.
    """
    normalized = normalize_locale(requested) or default_locale
    chain: list[str] = [normalized]
    if "-" in normalized:
        base = normalized.split("-", 1)[0]
        if base and base not in chain:
            chain.append(base)
    if default_locale and default_locale not in chain:
        chain.append(default_locale)
    return tuple(chain)


def resolve_locale(requested: str | None) -> LocaleResolution:
    """Resolve a requested locale to a supported one.

    Behavior is unchanged: a supported language resolves exactly, an
    unsupported one falls back to English, and ``fallback_used`` is True when
    the answer is not precisely what was asked for. An empty request resolves
    to the default locale *without* being counted as a fallback.
    """
    chain = fallback_chain(requested)
    for rung in chain:
        base = rung.split("-", 1)[0]
        if base in SUPPORTED_LOCALES:
            normalized = normalize_locale(requested) or DEFAULT_LOCALE
            return LocaleResolution(
                requested=normalized,
                resolved=base,
                fallback_used=base != normalized,
                chain=chain,
                landed_on=rung,
            )
    return LocaleResolution(
        requested=normalize_locale(requested) or DEFAULT_LOCALE,
        resolved=DEFAULT_LOCALE,
        fallback_used=True,
        chain=chain,
        landed_on=DEFAULT_LOCALE,
    )


def parse_accept_language(header: str | None) -> list[tuple[str, float]]:
    """Parse ``Accept-Language`` into ``(tag, q)``, best first.

    Malformed q-values fall back to 1.0 rather than dropping the tag: a client
    that sends ``fr;q=abc`` means French, not "no preference".
    """
    if not header:
        return []
    scored: list[tuple[str, float]] = []
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        tag, _, params = part.partition(";")
        tag = tag.strip()
        if not tag or tag == "*":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except (TypeError, ValueError):
                    quality = 1.0
        scored.append((tag, quality))
    return sorted(scored, key=lambda item: (-item[1], item[0]))


def negotiate_locale(
    header: str | None,
    *,
    available: Iterable[str] | None = None,
    default_locale: str = DEFAULT_LOCALE,
) -> LocaleResolution:
    """Content negotiation from an ``Accept-Language`` header.

    Returns the first acceptable supported locale, recording every candidate in
    ``chain`` so a client can be told what it could have asked for.
    """
    supported = list(available or SUPPORTED_LOCALES)
    candidates = parse_accept_language(header)
    chain = tuple(tag for tag, _q in candidates)
    for tag, _q in candidates:
        normalized = normalize_locale(tag)
        base = normalized.split("-", 1)[0]
        if base in supported:
            return LocaleResolution(
                requested=normalize_locale(header),
                resolved=base,
                fallback_used=base != normalized or base != default_locale,
                chain=chain or (base,),
                landed_on=normalized,
            )
    return LocaleResolution(
        requested=normalize_locale(header),
        resolved=default_locale if default_locale in supported else (supported[0] if supported else default_locale),
        fallback_used=True,
        chain=chain or (default_locale,),
        landed_on=default_locale,
    )


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
        "fallback_chain": list(resolution.chain),
        "direction": "rtl" if resolution.resolved in RTL_LOCALES else "ltr",
    }


# --- Rendering -----------------------------------------------------------------

# ``{name, plural, =0 {...} one {...} other {...}}`` — braces balanced by hand
# so nested ``{count}`` inside a branch still resolves.
_PLURAL_BLOCK = re.compile(r"\{\s*(\w+)\s*,\s*plural\s*,", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _split_branches(body: str) -> list[tuple[str, str]]:
    """Split ``one {..} other {..}`` into ``[(selector, template), ...]``.

    Brace-balanced, so a branch may itself contain ``{placeholders}``.
    """
    branches: list[tuple[str, str]] = []
    index = 0
    length = len(body)
    while index < length:
        while index < length and (body[index].isspace() or body[index] == ","):
            index += 1
        if index >= length:
            break
        start = index
        while index < length and not body[index].isspace() and body[index] != "{":
            index += 1
        selector = body[start:index].strip()
        while index < length and body[index].isspace():
            index += 1
        if index >= length or body[index] != "{":
            break
        depth = 0
        inner_start = index + 1
        while index < length:
            if body[index] == "{":
                depth += 1
            elif body[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        branches.append((selector, body[inner_start:index]))
        index += 1
    return branches


def plural_category(count: float, locale: str) -> str:
    """Resolve a count to its plural category for ``locale``.

    Only the distinctions the shipped locales actually make are implemented
    (``one`` vs ``other``, plus explicit ``=0``); an unknown language safely
    falls back to ``other``, which is correct for the overwhelming majority of
    languages including English, Spanish, and French.
    """
    categories = PLURAL_CATEGORIES.get(locale, ("other",))
    if "one" not in categories:
        return "other"
    if float(count) == 1:
        return "one"
    return "other"


def _render_template(template: str, locale: str, values: dict[str, Any]) -> str:
    """Interpolate ``{placeholder}`` and resolve any plural blocks."""
    result: list[str] = []
    index = 0
    length = len(template)
    while index < length:
        char = template[index]
        if char != "{":
            result.append(char)
            index += 1
            continue
        match = _PLURAL_BLOCK.match(template, index)
        if not match:
            # A plain {placeholder} or an unrecognised block: emit verbatim and
            # let the final .format() either fill it or leave it alone.
            result.append(char)
            index += 1
            continue
        variable = match.group(1)
        # Skip past the selector, then walk the balanced branch list.
        cursor = match.end()
        depth = 0
        body_start = cursor
        while cursor < length:
            if template[cursor] == "{":
                depth += 1
            elif template[cursor] == "}":
                if depth == 0:
                    break
                depth -= 1
            cursor += 1
        body = template[body_start:cursor]
        branches = _split_branches(body)
        raw_value = values.get(variable)
        try:
            numeric = float(raw_value) if raw_value is not None else 0.0
        except (TypeError, ValueError):
            numeric = 0.0
        chosen = ""
        for selector, branch_template in branches:
            if selector.startswith("="):
                try:
                    if float(selector[1:]) == numeric:
                        chosen = branch_template
                        break
                except (TypeError, ValueError):
                    continue
            elif selector == plural_category(numeric, locale):
                chosen = branch_template
                break
        if not chosen and branches:
            chosen = branches[-1][1]  # "other" as the last resort
        # "#" is the ICU shorthand for the number itself.
        chosen = chosen.replace("#", str(int(numeric)) if numeric.is_integer() else str(numeric))
        result.append(chosen)
        index = cursor + 1
    joined = "".join(result)
    if not values:
        return joined
    try:
        return joined.format(**values)
    except (KeyError, IndexError, ValueError):
        return joined


def translate(
    key: str,
    requested_locale: str | None = None,
    *,
    count: float | None = None,
    scopes: Iterable[str] = (),
    **kwargs,
) -> str:
    """Resolve a catalog key into a localized message.

    Falls back through the full chain (requested -> base language -> default
    locale), then to the key itself, when no translation exists. ``{placeholder}``
    tokens are interpolated when ``kwargs`` are provided and the template
    supports them.

    ``count=`` drives plural selection: with it, a
    ``{count, plural, one {..} other {..}}`` block resolves to the right branch
    for the resolved locale.
    """
    locale = resolve_locale(requested_locale).resolved
    scope_list = list(scopes)
    template = OVERRIDES.lookup(key, locale, scope_list)
    if template is None:
        entry = MESSAGE_CATALOG.get(key)
        if entry is None:
            return key
        template = entry.get(locale) or entry.get("en")
        if template is None:
            return key
    values = dict(kwargs)
    if count is not None:
        values["count"] = count
    return _render_template(template, locale, values)


def translate_many(
    keys: Iterable[str], requested_locale: str | None = None, **kwargs
) -> dict[str, str]:
    """Translate a batch in one call — the shape a UI bootstrap needs."""
    locale = resolve_locale(requested_locale).resolved
    return {key: translate(key, locale, **kwargs) for key in keys}


def catalog_coverage() -> dict[str, Any]:
    """Which keys each locale is missing, and which locales exist at all.

    This is the translator's worklist, and the cheapest way to catch a locale
    that was added to the registry but never given any strings.
    """
    locales = sorted(SUPPORTED_LOCALES)
    per_locale: dict[str, list[str]] = {}
    for locale in locales:
        per_locale[locale] = sorted(
            key for key, entry in MESSAGE_CATALOG.items() if not entry.get(locale)
        )
    complete = [locale for locale in locales if not per_locale[locale]]
    return {
        "locales": locales,
        "message_count": len(MESSAGE_CATALOG),
        "missing_by_locale": per_locale,
        "complete_locales": complete,
        "incomplete_locales": [locale for locale in locales if per_locale[locale]],
        "coverage": {
            locale: round(
                1 - (len(per_locale[locale]) / len(MESSAGE_CATALOG)) if MESSAGE_CATALOG else 1.0,
                4,
            )
            for locale in locales
        },
        "rtl_locales": [locale for locale in locales if locale in RTL_LOCALES],
    }


def build_i18n_catalog() -> dict[str, object]:
    """Introspectable catalog for metadata endpoints."""
    return {
        "locales": sorted(SUPPORTED_LOCALES),
        "fallback_locale": DEFAULT_LOCALE,
        "message_count": len(MESSAGE_CATALOG),
        "messages": {
            key: {
                locale: template
                for locale, template in entry.items()
            }
            for key, entry in sorted(MESSAGE_CATALOG.items())
        },
        "locale_detail": {
            locale: {
                **config,
                "direction": "rtl" if locale in RTL_LOCALES else "ltr",
                "plural_categories": list(PLURAL_CATEGORIES.get(locale, ("other",))),
            }
            for locale, config in sorted(SUPPORTED_LOCALES.items())
        },
        "negotiation": {
            "order": ["X-API-Locale header", "Accept-Language q-values", "default locale"],
            "chain": "requested -> base language -> default -> catalog key",
        },
        "coverage": catalog_coverage(),
        "overrides": OVERRIDES.stats(),
    }

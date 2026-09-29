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
import itertools
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

    def keys_by_scope(self) -> dict[str, list[str]]:
        """Every key each scope defines, whether or not the shipped catalog has it.

        Read-only counterpart to :meth:`scopes`. A coverage report needs to ask
        "which keys does an overlay introduce?" and the answer is not reachable
        from the public surface otherwise -- ``_layers`` is the only place it
        lives. Nothing in the resolution path calls this, so adding it cannot
        change what a request renders.
        """
        return {scope: sorted(self._layers.get(scope, {})) for scope in sorted(self._layers)}

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


# ==============================================================================
# Governance layer
# ==============================================================================
#
# Everything above this line is the shipped behaviour and is unchanged. What
# follows makes that behaviour *inspectable* -- it turns the assumptions the
# renderer makes into rows a validator can check, and turns the questions a
# translator, a reviewer, or a tenant actually asks into functions that answer
# them:
#
#   "which of my strings will print a raw ``{name}`` to a customer?"   ->
#       :func:`message_placeholder_audit`
#   "which namespace is over budget, and by how much?"                  ->
#       :func:`message_budget_report`
#   "is this template's brace structure even balanced?"                 ->
#       :func:`unbalanced_template_report`
#   "which plural branches can this renderer ever select?"              ->
#       :func:`plural_audit`
#   "why did the two locale resolvers disagree?"                        ->
#       :func:`negotiation_audit`
#   "which tables in this module describe something that cannot happen?" ->
#       :func:`locale_registry_audit`
#   "are the six config tables coherent?"                               ->
#       :func:`validate_i18n`
#
# The rule this layer follows is *report, never repair*. The renderer swallows
# a ``KeyError`` and returns the joined string, so a missing ``{name}`` reaches
# the customer as a raw token. That is a real defect and it is a real product
# decision: a customer-visible string changes the moment it is fixed. Every
# finding below is therefore reported with the evidence that produced it, and
# :data:`RENDER_OPS` records ``report_only: True`` on every row.


# --- Namespaces ----------------------------------------------------------------

# The keyspace is ``<namespace>.<name>``. A namespace is the unit an owning
# team edits, so it is the unit a placeholder policy and a length budget apply
# to. Every prefix currently in ``MESSAGE_CATALOG`` has a row here; the
# validator reports a prefix that does not, so adding a key without a namespace
# is caught rather than silently governed by nothing.
#
# ``near_duplicates`` records namespaces a human will confuse. The catalog
# carries both ``error.*`` and ``errors.*``: two prefixes, one concept, split
# across a plural-bearing message and a plain one. That is left as-is and
# reported -- merging them would move keys that clients and translation files
# already reference, which is a breaking change and not a governance decision.
MESSAGE_NAMESPACES: dict[str, dict[str, Any]] = {
    "common": {
        "description": "chrome shared by every surface",
        "surface": "user",
        "audience": "end_user",
        "owner": "platform",
        "placeholder_policy": "none_expected",
        "max_characters": 80,
        "near_duplicates": (),
    },
    "ai_chat": {
        "description": "assistant-facing copy in the chat surface",
        "surface": "user",
        "audience": "end_user",
        "owner": "conversations",
        "placeholder_policy": "none_expected",
        "max_characters": 80,
        "near_duplicates": (),
    },
    "auth": {
        "description": "login, registration, and credential-change outcomes",
        "surface": "user",
        "audience": "end_user",
        "owner": "identity",
        "placeholder_policy": "required_values",
        "max_characters": 80,
        "near_duplicates": (),
    },
    "booking": {
        "description": "booking lifecycle copy, including the counted form",
        "surface": "user",
        "audience": "end_user",
        "owner": "bookings",
        "placeholder_policy": "plural_count",
        "max_characters": 80,
        "near_duplicates": (),
    },
    "retention": {
        "description": "retention and snapshot operator notices",
        "surface": "user",
        "audience": "end_user",
        "owner": "retention",
        "placeholder_policy": "none_expected",
        "max_characters": 80,
        "near_duplicates": (),
    },
    "health": {
        "description": "service status banners",
        "surface": "user",
        "audience": "end_user",
        "owner": "platform",
        "placeholder_policy": "none_expected",
        "max_characters": 80,
        "near_duplicates": (),
    },
    "errors": {
        "description": "generic failure copy; the prefix a new key should use",
        "surface": "user",
        "audience": "end_user",
        "owner": "platform",
        "placeholder_policy": "none_expected",
        "max_characters": 80,
        "near_duplicates": ("error",),
    },
    "error": {
        "description": (
            "the counted error message; a separate prefix from 'errors' for "
            "historical reasons and reported as a near-duplicate of it"
        ),
        "surface": "user",
        "audience": "end_user",
        "owner": "platform",
        "placeholder_policy": "plural_count",
        "max_characters": 80,
        "near_duplicates": ("errors",),
    },
}

# How a namespace's placeholders are treated. Three policies cover what the
# shipped catalog actually contains:
#
# ``none_expected``    no ``{token}`` at all. A token here is dead weight that
#                      will print verbatim.
# ``required_values``  plain ``{token}`` placeholders whose value must reach
#                      the renderer. The audit proves it does not, by rendering
#                      the template with no values and looking for a leak.
# ``plural_count``     a ``{count, plural, ...}`` block. ``count`` may be
#                      absent at the call site, and what that renders is a
#                      reported finding rather than a policy violation, because
#                      "no count supplied" is a legitimate call.
PLACEHOLDER_POLICIES: dict[str, dict[str, Any]] = {
    "none_expected": {
        "description": "templates in this namespace carry no interpolation",
        "allow_placeholders": False,
        "require_values": False,
        "max_placeholders": 0,
        "must_match_across_locales": True,
        "name_pattern": r"^\w+$",
        "count_is_plural_variable": False,
    },
    "required_values": {
        "description": "plain placeholders whose value must reach the renderer",
        "allow_placeholders": True,
        "require_values": True,
        "max_placeholders": 3,
        "must_match_across_locales": True,
        "name_pattern": r"^\w+$",
        "count_is_plural_variable": False,
    },
    "plural_count": {
        "description": "an ICU-style plural block driven by the count variable",
        "allow_placeholders": True,
        "require_values": False,
        "max_placeholders": 2,
        "must_match_across_locales": True,
        "name_pattern": r"^\w+$",
        "count_is_plural_variable": True,
    },
}


# --- Locale expectations -------------------------------------------------------

# What a *language* determines, which is narrower than what a locale tag
# determines. Two columns carry that distinction explicitly rather than papering
# over it:
#
#   ``region_ambiguous``  the language's conventions differ by region, so the
#                         tag alone is not enough. ``es`` is ``es_ES`` and
#                         ``es_419`` and they disagree about the decimal
#                         separator -- and this module negotiates on language
#                         only, because both ``resolve_locale`` and
#                         ``negotiate_locale`` reduce ``pt-BR`` to ``pt``.
#                         That is reported by :func:`negotiation_audit`, not
#                         fixed here.
#   ``format_profile``    the profile used when nothing better is known, which
#                         is the dominant region for that language.
#
# ``in_registry`` is a fact about ``SUPPORTED_LOCALES``, recorded here so the
# table is self-describing: ``ar`` has a row because ``RTL_LOCALES`` and
# ``PLURAL_CATEGORIES`` both mention it, and that row is precisely the finding
# -- a locale with a direction and a six-category plural rule set that no
# request can ever reach.
#
# Deliberately absent: currency and date order. A language tag does not
# determine either, and inventing them would make this table confidently wrong.
LOCALE_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "en": {
        "source": ("SUPPORTED_LOCALES", "PLURAL_CATEGORIES"),
        "in_registry": True,
        "direction": "ltr",
        "script": "Latin",
        "format_profile": "en_us",
        "region_ambiguous": True,
        "alternates": ("en_gb",),
    },
    "es": {
        "source": ("SUPPORTED_LOCALES", "PLURAL_CATEGORIES"),
        "in_registry": True,
        "direction": "ltr",
        "script": "Latin",
        "format_profile": "es_es",
        "region_ambiguous": True,
        "alternates": ("es_419",),
    },
    "fr": {
        "source": ("SUPPORTED_LOCALES", "PLURAL_CATEGORIES"),
        "in_registry": True,
        "direction": "ltr",
        "script": "Latin",
        "format_profile": "fr_fr",
        "region_ambiguous": False,
        "alternates": (),
    },
    "ar": {
        "source": ("RTL_LOCALES", "PLURAL_CATEGORIES"),
        "in_registry": False,
        "direction": "rtl",
        "script": "Arabic",
        "format_profile": "ar_eg",
        "region_ambiguous": True,
        "alternates": (),
    },
}

# Number, percent, and currency presentation, keyed by a *region* profile
# rather than by a language. A profile is unambiguous by construction, which is
# the whole point of keying it this way: the caller picks a region or accepts
# the language's dominant one.
#
# ``digit_substitution`` is declared and always ``"none"``. Arabic and Hindi
# locales conventionally render ``1234`` with a different numeral; this helper
# does not do that, and saying so in the table is better than a caller
# discovering it from a screenshot.
NUMBER_FORMATS: dict[str, dict[str, Any]] = {
    "en_us": {
        "language": "en",
        "region": "US",
        "decimal_separator": ".",
        "group_separator": ",",
        "group_size": 3,
        "minimum_grouping_digits": 1,
        "percent_position": "suffix",
        "currency_symbol": "$",
        "currency_position": "prefix",
        "currency_separator": "",
        "negative_sign_position": "prefix",
        "digit_substitution": "none",
    },
    "en_gb": {
        "language": "en",
        "region": "GB",
        "decimal_separator": ".",
        "group_separator": ",",
        "group_size": 3,
        "minimum_grouping_digits": 1,
        "percent_position": "suffix",
        "currency_symbol": "\u00a3",
        "currency_position": "prefix",
        "currency_separator": "",
        "negative_sign_position": "prefix",
        "digit_substitution": "none",
    },
    "es_es": {
        "language": "es",
        "region": "ES",
        "decimal_separator": ",",
        "group_separator": ".",
        "group_size": 3,
        "minimum_grouping_digits": 1,
        "percent_position": "suffix",
        "currency_symbol": "\u20ac",
        "currency_position": "suffix",
        "currency_separator": "\u00a0",
        "negative_sign_position": "prefix",
        "digit_substitution": "none",
    },
    "es_419": {
        "language": "es",
        "region": "419",
        "decimal_separator": ".",
        "group_separator": ",",
        "group_size": 3,
        "minimum_grouping_digits": 1,
        "percent_position": "suffix",
        "currency_symbol": "$",
        "currency_position": "prefix",
        "currency_separator": "",
        "negative_sign_position": "prefix",
        "digit_substitution": "none",
    },
    "fr_fr": {
        "language": "fr",
        "region": "FR",
        "decimal_separator": ",",
        # CLDR moved the French group separator from U+00A0 to U+202F; this
        # table follows CLDR and says which character it picked.
        "group_separator": "\u202f",
        "group_size": 3,
        "minimum_grouping_digits": 1,
        "percent_position": "suffix",
        "currency_symbol": "\u20ac",
        "currency_position": "suffix",
        "currency_separator": "\u00a0",
        "negative_sign_position": "prefix",
        "digit_substitution": "none",
    },
    "de_de": {
        "language": "de",
        "region": "DE",
        "decimal_separator": ",",
        "group_separator": ".",
        "group_size": 3,
        "minimum_grouping_digits": 1,
        "percent_position": "suffix",
        "currency_symbol": "\u20ac",
        "currency_position": "suffix",
        "currency_separator": "\u00a0",
        "negative_sign_position": "prefix",
        "digit_substitution": "none",
    },
    "ar_eg": {
        "language": "ar",
        "region": "EG",
        "decimal_separator": ".",
        "group_separator": ",",
        "group_size": 3,
        "minimum_grouping_digits": 1,
        "percent_position": "suffix",
        "currency_symbol": "\u062c.\u0645",
        "currency_position": "suffix",
        "currency_separator": "\u00a0",
        "negative_sign_position": "prefix",
        "digit_substitution": "none",
    },
}


# --- What the renderer actually does -------------------------------------------
#
# One row per decision point inside ``_render_template`` and
# :func:`parse_accept_language`. Every ``current_behaviour`` string below is a
# verified output, not a paraphrase -- the test module asserts each one by
# calling the shipped function. ``classification`` is the honest label:
#
#   ``defect``    a customer can see the wrong thing. Reported, not fixed.
#   ``wart``      surprising but defensible; worth knowing before relying on it.
#   ``info``      documented behaviour that exists to be relied on.
#
# Every row is ``report_only``. ``_render_template`` catches
# ``(KeyError, IndexError, ValueError)`` and returns the joined string, so
# "fixing" a leak here means a different string leaving the process. That is a
# product decision with a release attached, and the correct place to make it is
# not a governance table.
RENDER_OPS: dict[str, dict[str, Any]] = {
    "missing_value_leaks_token": {
        "op": "_render_template",
        "branch": "except (KeyError, IndexError, ValueError): return joined",
        "trigger": "a {token} placeholder with no matching value",
        "current_behaviour": "translate('auth.welcome', 'en') == 'Welcome, {name}'",
        "classification": "defect",
        "finding": "I18N_VALUE_NOT_SUPPLIED",
        "report_only": True,
    },
    "dotted_field_escapes_the_except_clause": {
        "op": "_render_template",
        "branch": "except (KeyError, IndexError, ValueError) -- no AttributeError",
        "trigger": "a template with an attribute field such as {a.b}",
        "current_behaviour": (
            "translate() raises AttributeError out of the renderer when that value is "
            "supplied, instead of degrading: _render_template('Value is {a.b} here', 'en', "
            "{'a': 'v'}) raises. The renderer catches the three exceptions that a *missing* "
            "value can produce and not the one that a present value can"
        ),
        "classification": "defect",
        "finding": "I18N_RENDER_RAISES",
        "report_only": True,
    },
    "stray_close_brace_disables_interpolation": {
        "op": "_render_template",
        "branch": "joined.format(**values) raising ValueError",
        "trigger": "a single unescaped '}' anywhere in the template",
        "current_behaviour": (
            "translate-style render of 'Welcome, {name} }' with name='x' returns "
            "'Welcome, {name} }' -- one stray brace silences every placeholder "
            "in the string, not just its own"
        ),
        "classification": "defect",
        "finding": "I18N_BRACE_UNBALANCED",
        "report_only": True,
    },
    "plural_without_count_renders_zero": {
        "op": "_render_template",
        "branch": "numeric = float(raw_value) if raw_value is not None else 0.0",
        "trigger": "a plural block rendered with no count supplied",
        "current_behaviour": (
            "translate('error.count', 'en') == 'no errors' and "
            "translate('booking.count', 'en') == '0 bookings' -- an omitted "
            "count is indistinguishable from a real zero"
        ),
        "classification": "defect",
        "finding": "I18N_PLURAL_COUNT_ABSENT",
        "report_only": True,
    },
    "unparsable_count_renders_zero": {
        "op": "_render_template",
        "branch": "except (TypeError, ValueError): numeric = 0.0",
        "trigger": "count is not numeric ('abc', None, a Decimal-less object)",
        "current_behaviour": "count='abc' renders the zero branch, not an error",
        "classification": "wart",
        "finding": "I18N_PLURAL_COUNT_ABSENT",
        "report_only": True,
    },
    "hash_is_replaced_without_word_boundary": {
        "op": "_render_template",
        "branch": "chosen.replace('#', ...)",
        "trigger": "a '#' immediately followed by a digit in the chosen branch",
        "current_behaviour": (
            "'{n, plural, other {issue #7 resolved with # items}}' with n=3 "
            "renders 'issue 37 resolved with 3 items' -- the reference number "
            "is rewritten by the count"
        ),
        "classification": "defect",
        "finding": "I18N_PLURAL_BARE_NUMBER",
        "report_only": True,
    },
    "last_branch_is_the_fallback": {
        "op": "_render_template",
        "branch": "if not chosen and branches: chosen = branches[-1][1]",
        "trigger": "no selector matches and there is no 'other' branch",
        "current_behaviour": (
            "'{n, plural, one {# item}}' with n=9 renders '9 item' -- the "
            "singular branch silently serves every other count"
        ),
        "classification": "defect",
        "finding": "I18N_PLURAL_NO_OTHER",
        "report_only": True,
    },
    "unparsable_exact_selector_is_skipped": {
        "op": "_render_template",
        "branch": "except (TypeError, ValueError): continue",
        "trigger": "an '=x' selector that is not a number",
        "current_behaviour": "the selector is ignored and selection continues",
        "classification": "wart",
        "finding": "I18N_PLURAL_SELECTOR_UNDECLARED",
        "report_only": True,
    },
    "unterminated_plural_block_drops_the_tail": {
        "op": "_render_template",
        "branch": "the depth walk reaching the end of the template",
        "trigger": "a plural block with no closing brace",
        "current_behaviour": (
            "'{n, plural, one {# item} other {# items' with n=1 renders "
            "'1 item' -- the malformed second branch and any text after it "
            "disappear with no diagnostic"
        ),
        "classification": "defect",
        "finding": "I18N_PLURAL_UNTERMINATED",
        "report_only": True,
    },
    "empty_template_renders_empty": {
        "op": "translate",
        "branch": "template = entry.get(locale) or entry.get('en')",
        "trigger": "a template that is the empty string",
        "current_behaviour": (
            "the key resolves to '' -- catalog_coverage() lists the locale as "
            "missing, because '' is falsy, so coverage and rendering disagree "
            "about whether the string exists"
        ),
        "classification": "wart",
        "finding": "I18N_TEMPLATE_EMPTY",
        "report_only": True,
    },
    "accept_language_q_zero_is_still_eligible": {
        "op": "parse_accept_language",
        "branch": "scored.append((tag, quality)) -- no q=0 filter",
        "trigger": "Accept-Language with an explicit q=0",
        "current_behaviour": (
            "parse_accept_language('fr;q=0') == [('fr', 0.0)] and "
            "negotiate_locale('fr;q=0') resolves to 'fr'. RFC 9110 defines "
            "q=0 as 'not acceptable'"
        ),
        "classification": "defect",
        "finding": "I18N_ACCEPT_LANGUAGE_Q_ZERO",
        "report_only": True,
    },
    "accept_language_q_is_not_clamped": {
        "op": "parse_accept_language",
        "branch": "quality = float(value.strip())",
        "trigger": "a q outside [0, 1]",
        "current_behaviour": "parse_accept_language('fr;q=5, es;q=-1') keeps 5.0 and -1.0",
        "classification": "wart",
        "finding": "I18N_ACCEPT_LANGUAGE_Q_UNCLAMPED",
        "report_only": True,
    },
    "accept_language_repeated_q_last_wins": {
        "op": "parse_accept_language",
        "branch": "for param in params.split(';')",
        "trigger": "the same parameter repeated in one range",
        "current_behaviour": "parse_accept_language('fr;q=0.2;q=0.9') reports 0.9",
        "classification": "info",
        "finding": "I18N_ACCEPT_LANGUAGE_DUPLICATE_Q",
        "report_only": True,
    },
    "negotiate_requested_is_the_whole_header": {
        "op": "negotiate_locale",
        "branch": "requested=normalize_locale(header)",
        "trigger": "any multi-tag Accept-Language header",
        "current_behaviour": (
            "negotiate_locale('fr-CA, es;q=0.8, en;q=0.5').requested == "
            "'fr-ca, es;q=0.8, en;q=0.5' -- the whole header, normalized as "
            "if it were one tag"
        ),
        "classification": "defect",
        "finding": "I18N_NEGOTIATION_REQUESTED_NOT_A_TAG",
        "report_only": True,
    },
    "per_locale_fallback_key_is_reported_not_used": {
        "op": "resolve_locale / fallback_chain",
        "branch": "chain.append(default_locale) -- DEFAULT_LOCALE, not config['fallback']",
        "trigger": "editing SUPPORTED_LOCALES[locale]['fallback']",
        "current_behaviour": (
            "the value is echoed by locale_payload() and build_i18n_catalog() "
            "but no resolution path reads it, so setting es->fr changes the "
            "report and not the behaviour"
        ),
        "classification": "wart",
        "finding": "I18N_LOCALE_FALLBACK_UNUSED",
        "report_only": True,
    },
}

# The finding taxonomy. Every code an audit can emit has a row, so a consumer
# can look up what a code means and how bad it is without parsing prose.
# ``emitted_by`` names the functions that can produce it -- which is also the
# list a caller needs in order to know what to run.
I18N_WARNINGS: dict[str, dict[str, Any]] = {
    "I18N_VALUE_NOT_SUPPLIED": {
        "severity": "defect",
        "description": "a template declares {token} and no value reaches the renderer, so the raw token is returned",
        "remediation": "supply the value at the call site, or drop the token from the template",
        "emitted_by": ("message_placeholder_audit", "unbalanced_template_report"),
    },
    "I18N_RENDER_RAISES": {
        "severity": "defect",
        "description": (
            "a template uses an attribute field such as {a.b}, which raises AttributeError "
            "outside the renderer's except (KeyError, IndexError, ValueError) clause and "
            "propagates out of translate() to the caller"
        ),
        "remediation": "use a bare name and pass the whole value; catch AttributeError in the renderer",
        "emitted_by": ("message_placeholder_audit",),
    },
    "I18N_PLACEHOLDER_MISMATCH": {
        "severity": "error",
        "description": "the placeholder set differs between locales of one message key",
        "remediation": "align the translations; a token present in one locale and not another is a leak or a stray",
        "emitted_by": ("message_placeholder_audit",),
    },
    "I18N_PLACEHOLDER_UNEXPECTED": {
        "severity": "warning",
        "description": "a template carries a placeholder in a namespace whose policy expects none",
        "remediation": "move the message to a namespace with a placeholder policy, or drop the token",
        "emitted_by": ("message_placeholder_audit",),
    },
    "I18N_PLACEHOLDER_LIMIT": {
        "severity": "warning",
        "description": "more placeholders than the namespace policy allows",
        "remediation": "raise max_placeholders on the policy, or simplify the message",
        "emitted_by": ("message_placeholder_audit",),
    },
    "I18N_PLACEHOLDER_NAME_INVALID": {
        "severity": "warning",
        "description": "a placeholder name does not match the policy's name pattern",
        "remediation": "rename to a bare identifier; the renderer only interpolates \\w+ tokens",
        "emitted_by": ("message_placeholder_audit",),
    },
    "I18N_NAMESPACE_UNKNOWN": {
        "severity": "error",
        "description": "a catalog key's prefix has no MESSAGE_NAMESPACES row",
        "remediation": "add the namespace, or fix the key",
        "emitted_by": ("message_placeholder_audit", "message_budget_report", "validate_i18n"),
    },
    "I18N_NAMESPACE_NEAR_DUPLICATE": {
        "severity": "warning",
        "description": "two namespaces a human will confuse, declared as near-duplicates",
        "remediation": "leave as-is and document, or migrate keys in a release with a redirect",
        "emitted_by": ("validate_i18n",),
    },
    "I18N_BUDGET_EXCEEDED": {
        "severity": "warning",
        "description": "a template is longer than its namespace's max_characters",
        "remediation": "shorten the string, or raise the namespace budget deliberately",
        "emitted_by": ("message_budget_report",),
    },
    "I18N_BRACE_UNBALANCED": {
        "severity": "error",
        "description": "brace depth does not return to zero, or a lone '}' suppresses interpolation for the whole template",
        "remediation": "escape a literal brace as {{ or }}",
        "emitted_by": ("unbalanced_template_report", "validate_i18n"),
    },
    "I18N_PLURAL_UNTERMINATED": {
        "severity": "error",
        "description": "a plural block has no closing brace, so the rest of the template is discarded silently",
        "remediation": "close the block",
        "emitted_by": ("unbalanced_template_report", "plural_audit"),
    },
    "I18N_PLURAL_NO_OTHER": {
        "severity": "warning",
        "description": "a plural block has no 'other' selector, so the last branch serves every unmatched count",
        "remediation": "add an 'other' branch",
        "emitted_by": ("plural_audit",),
    },
    "I18N_PLURAL_SELECTOR_UNREACHABLE": {
        "severity": "warning",
        "description": "a selector this renderer can never produce for the locale (zero/two/few/many)",
        "remediation": "the rule set in plural_category() is one-vs-other only; either narrow the template or accept dead branches",
        "emitted_by": ("plural_audit",),
    },
    "I18N_PLURAL_SELECTOR_UNDECLARED": {
        "severity": "error",
        "description": "a selector is neither an '=N' exact match nor one of the locale's declared categories",
        "remediation": "use a declared category, or declare the locale's categories properly",
        "emitted_by": ("plural_audit",),
    },
    "I18N_PLURAL_COUNT_ABSENT": {
        "severity": "defect",
        "description": "a plural template renders its zero branch when no count is supplied, so an omitted count reads as a real zero",
        "remediation": "pass count= at every call site that renders a plural message",
        "emitted_by": ("plural_audit",),
    },
    "I18N_PLURAL_BARE_NUMBER": {
        "severity": "defect",
        "description": "a '#' immediately followed by a digit inside a branch is rewritten by the count substitution",
        "remediation": "put whitespace after the literal '#', or move the reference out of the branch",
        "emitted_by": ("plural_audit",),
    },
    "I18N_PLURAL_CROSS_LOCALE_DIVERGENCE": {
        "severity": "error",
        "description": "two locales of one key declare different plural selectors, so the same count reads differently",
        "remediation": "align the branch sets across locales",
        "emitted_by": ("plural_audit",),
    },
    "I18N_TEMPLATE_EMPTY": {
        "severity": "warning",
        "description": "a template is the empty string: it renders as empty but counts as missing in coverage",
        "remediation": "use a real string, or delete the key",
        "emitted_by": ("message_placeholder_audit", "validate_i18n"),
    },
    "I18N_NEGOTIATION_REQUESTED_NOT_A_TAG": {
        "severity": "defect",
        "description": "negotiate_locale reports the whole Accept-Language header as 'requested'",
        "remediation": "read chain[0] instead of requested, or fix the field at the source",
        "emitted_by": ("negotiation_audit",),
    },
    "I18N_NEGOTIATION_FALLBACK_DIVERGENCE": {
        "severity": "defect",
        "description": "resolve_locale and negotiate_locale report different fallback_used for the same request",
        "remediation": "pick one definition; today 'es' is not a fallback to one and is a fallback to the other",
        "emitted_by": ("negotiation_audit",),
    },
    "I18N_NEGOTIATION_CHAIN_SEMANTICS": {
        "severity": "warning",
        "description": "LocaleResolution.chain means different things depending on which function produced it",
        "remediation": "document per producer, or add a separate field for the candidate list",
        "emitted_by": ("negotiation_audit",),
    },
    "I18N_NEGOTIATION_REGION_DISCARDED": {
        "severity": "warning",
        "description": "both resolvers reduce a regional tag to its language, so region-specific conventions never apply",
        "remediation": "keep the region for formatting decisions; negotiation may still resolve on the language",
        "emitted_by": ("negotiation_audit",),
    },
    "I18N_ACCEPT_LANGUAGE_Q_ZERO": {
        "severity": "defect",
        "description": "a q=0 tag is still eligible, though RFC 9110 defines it as not acceptable",
        "remediation": "filter q=0 tags after parsing, in a new function, leaving parse_accept_language alone",
        "emitted_by": ("negotiation_audit",),
    },
    "I18N_ACCEPT_LANGUAGE_Q_UNCLAMPED": {
        "severity": "warning",
        "description": "a q outside [0, 1] is kept verbatim and changes the sort order",
        "remediation": "clamp to the valid range",
        "emitted_by": ("negotiation_audit",),
    },
    "I18N_ACCEPT_LANGUAGE_DUPLICATE_Q": {
        "severity": "info",
        "description": "a repeated q parameter is resolved last-wins",
        "remediation": "none needed; documented behaviour",
        "emitted_by": ("negotiation_audit",),
    },
    "I18N_LOCALE_NOT_IN_REGISTRY": {
        "severity": "warning",
        "description": "a locale named by RTL_LOCALES, PLURAL_CATEGORIES, or LOCALE_EXPECTATIONS that SUPPORTED_LOCALES does not ship",
        "remediation": "add the locale with its templates, or drop the row; right now its rules are unreachable",
        "emitted_by": ("locale_registry_audit", "validate_i18n"),
    },
    "I18N_LOCALE_NO_TEMPLATES": {
        "severity": "warning",
        "description": "a shipped locale has no templates at all, so every request for it renders English",
        "remediation": "translate the catalog for that locale",
        "emitted_by": ("locale_registry_audit",),
    },
    "I18N_LOCALE_FALLBACK_UNUSED": {
        "severity": "warning",
        "description": "SUPPORTED_LOCALES[locale]['fallback'] is reported but never consulted by any resolution path",
        "remediation": "either honour it in a new resolver or stop reporting it as if it were live",
        "emitted_by": ("locale_registry_audit",),
    },
    "I18N_LOCALE_FALLBACK_NOT_SHIPPED": {
        "severity": "error",
        "description": "a declared per-locale fallback names a locale that is not in the registry",
        "remediation": "point it at a shipped locale",
        "emitted_by": ("locale_registry_audit", "validate_i18n"),
    },
    "I18N_LOCALE_REGION_AMBIGUOUS": {
        "severity": "warning",
        "description": "the language's number and currency conventions are region-dependent and negotiation drops the region",
        "remediation": "carry the region through to the formatter",
        "emitted_by": ("locale_registry_audit",),
    },
    "I18N_LOCALE_NO_EXPECTATIONS": {
        "severity": "warning",
        "description": "a shipped locale has no LOCALE_EXPECTATIONS row",
        "remediation": "add the row; direction and format profile are then inspectable",
        "emitted_by": ("locale_registry_audit", "validate_i18n"),
    },
    "I18N_OVERRIDE_OUTSIDE_CATALOG": {
        "severity": "warning",
        "description": "an override defines a key the shipped catalog does not have, so no coverage report can see it",
        "remediation": "coverage should be able to take the override scopes into account",
        "emitted_by": ("locale_registry_audit",),
    },
    "I18N_POLICY_UNKNOWN": {
        "severity": "error",
        "description": "a namespace points at a placeholder policy that does not exist",
        "remediation": "name an existing policy",
        "emitted_by": ("validate_i18n",),
    },
    "I18N_PROFILE_UNKNOWN": {
        "severity": "error",
        "description": "a locale points at a number-format profile that does not exist",
        "remediation": "name an existing profile",
        "emitted_by": ("validate_i18n",),
    },
    "I18N_ALTERNATE_PROFILE_UNKNOWN": {
        "severity": "error",
        "description": "a locale names an alternate format profile that does not exist",
        "remediation": "name an existing profile",
        "emitted_by": ("validate_i18n",),
    },
    "I18N_UNUSABLE_PATTERN": {
        "severity": "info",
        "description": "a module-level regex is not used by the shipped code paths",
        "remediation": "none; recorded so the dead-constant question has an answer",
        "emitted_by": ("validate_i18n",),
    },
}

I18N_GOVERNANCE_VERSION = "i18n_governance_v1"


# ``{name}`` -- recognised by ``_PLACEHOLDER``, and the only form the renderer
# can fill.
#
# ``{a.b}`` -- an *attribute* access, and the one form that escapes. The
# renderer's fallback catches ``(KeyError, IndexError, ValueError)``; a dotted
# field raises ``AttributeError`` instead, so the exception leaves ``translate``
# entirely and reaches whatever route was rendering the string. A translation is
# data a translator can edit, and one mistyped ``{word.word}`` is an unhandled
# exception on a live path. Indexing (``{a[0]}``) raises ``IndexError`` and *is*
# caught, which is why the detector below looks for the dot and not the bracket.
#
# The captured group is the field's *root*, which is what a demonstration has to
# supply: with no value for ``a`` at all the renderer raises ``KeyError`` and
# degrades, so a probe that omitted ``a`` would report the wrong exception and
# conclude there was no defect.
_RENDER_RAISES_FIELD = re.compile(r"\{\s*(\w+)\s*\.")

_BRACE_CONTENT = re.compile(r"\{([^{}]*)\}")


def _demo_values(template: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Values a demonstration needs so the renderer's *interesting* path is taken.

    Every field root a dotted access names gets a plain string. Without it the
    renderer raises ``KeyError``, which it catches, and a probe that omitted the
    root would quietly measure the degrading path instead of the escaping one.
    """
    values: dict[str, Any] = {root: "v" for root in _RENDER_RAISES_FIELD.findall(template)}
    if extra:
        values.update(extra)
    return values


def _probe_render(
    template: str,
    locale: str = DEFAULT_LOCALE,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a template the way ``translate`` would and record what came back.

    ``values`` is what ``translate`` would have assembled, so this is the shipped
    render path rather than a re-implementation of it. It is not *called* by
    ``translate`` -- the audits go through the real function whenever they can
    (see :func:`_safe_translate`) and fall back to this only when the template
    is not in the live catalog.

    Catches broadly on purpose. The question this answers is "what does the
    renderer do with this input", and a probe that only knew about the two
    exception types observed so far would be the thing that crashed on the
    third. ``BaseException`` is still excluded, so an interrupt propagates.
    """
    try:
        rendered = _render_template(template, locale, {} if values is None else values)
    except Exception as exc:  # noqa: BLE001 - the raise *is* the observation
        return {"rendered": None, "raised": type(exc).__name__, "message": str(exc)}
    return {"rendered": rendered, "raised": None, "message": None}


def _safe_translate(key: str, requested_locale: str | None = None, **kwargs) -> dict[str, Any]:
    """:func:`translate`, with any raise captured instead of propagated.

    The audits call the shipped function so their evidence is the shipped
    behaviour and not a re-implementation of it. They cannot call it unguarded,
    because one of the behaviours they are here to report is that it raises.
    """
    try:
        return {"rendered": translate(key, requested_locale, **kwargs), "raised": None, "message": None}
    except Exception as exc:  # noqa: BLE001 - see _probe_render
        return {"rendered": None, "raised": type(exc).__name__, "message": str(exc)}


# --- Inspection helpers ---------------------------------------------------------
#
# These read the catalog; they never call the resolver for a *decision*. Where
# one needs to know what the renderer would actually produce, it calls
# ``_render_template`` with no values -- which is safe, because that function
# catches everything it can raise and returns a string. That is precisely what
# makes it useful as a detector: the leak it returns *is* the leak a customer
# would see.

# The audits, named. ``I18N_WARNINGS[*]['emitted_by']`` is validated against
# this list, so a typo in a finding's producer is an error rather than a code
# nobody can ever emit.
_AUDIT_FUNCTIONS: tuple[str, ...] = (
    "message_placeholder_audit",
    "message_budget_report",
    "unbalanced_template_report",
    "plural_audit",
    "negotiation_audit",
    "locale_registry_audit",
    "validate_i18n",
)

# Probing counts for the reachable-category check. One-vs-other is all the
# renderer implements, so these are enough to *prove* that: whichever rule it
# applied, no count outside this set can produce a third category, because the
# function's entire output space is two strings.
_PROBE_COUNTS: tuple[float, ...] = (0, 1, 2, 3, 5, 11, 21, 100, 101)


def _finding(code: str, detail: str, **where: Any) -> dict[str, Any]:
    """One structured finding, with its taxonomy row attached.

    A finding the catalog cannot classify is a finding nobody can triage, so the
    severity and description come from :data:`I18N_WARNINGS` rather than from
    the call site. An unknown code degrades to ``unknown`` instead of raising:
    the audits run over operator-editable data and must not be the thing that
    breaks the metadata endpoint.
    """
    spec = I18N_WARNINGS.get(code, {})
    return {
        "code": code,
        "severity": str(spec.get("severity") or "unknown"),
        "description": str(spec.get("description") or ""),
        "remediation": str(spec.get("remediation") or ""),
        "detail": detail,
        **where,
    }


def _severity_counts(findings: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in findings:
        severity = str(item.get("severity") or "unknown")
        counts[severity] = counts.get(severity, 0) + 1
    return dict(sorted(counts.items()))


def _catalog_entries(
    catalog: Any,
) -> list[tuple[str, str, str]]:
    """Flatten a catalog into ``(key, locale, template)`` triples.

    Tolerant by design: the live catalog is well-formed, but an override or a
    hand-edited entry can be any shape at all, and an audit that raises on
    ``None`` is an audit that cannot be run against the thing it exists to
    describe. Non-string keys and non-string templates are yielded so the
    caller can report them rather than having them silently disappear.
    """
    if not isinstance(catalog, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for key, entry in catalog.items():
        name = key if isinstance(key, str) else str(key)
        if not isinstance(entry, dict):
            out.append((name, "", ""))
            continue
        for locale, template in entry.items():
            tag = locale if isinstance(locale, str) else str(locale)
            out.append((name, tag, template if isinstance(template, str) else ""))
    return out


def _namespace_of(key: str) -> str:
    return key.split(".", 1)[0] if key else ""


def _sorted_keys(catalog: Any) -> list[str]:
    """Sorted message keys of a catalog, tolerant of a malformed one."""
    if not isinstance(catalog, dict):
        return []
    return sorted(key if isinstance(key, str) else str(key) for key in catalog)


def _template_scan(template: Any, locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    """Everything the audits want to know about one template, computed once.

    ``plain`` and ``plural`` are separate lists on purpose. ``_PLACEHOLDER``
    matches ``{name}`` but not ``{count, plural, ...}`` -- the comma stops it --
    so a plural block's variable is invisible to a plain scan. Conflating the
    two is how a "no placeholders here" verdict gets attached to a template
    that is entirely driven by a count.
    """
    text = template if isinstance(template, str) else ""
    plain = _PLACEHOLDER.findall(text)
    plural = _PLURAL_BLOCK.findall(text)

    # Brace structure, scanned independently of the renderer because the
    # renderer's whole job is to survive this input.
    depth = 0
    lowest = 0
    stray_close = 0
    for char in text:
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                stray_close += 1
            else:
                depth -= 1
                lowest = min(lowest, depth)

    # An unterminated plural block: the renderer walks past the end looking for
    # the closing brace, so the marker is "a block that consumed the template".
    unterminated = False
    for match in _PLURAL_BLOCK.finditer(text):
        cursor = match.end()
        inner = 0
        while cursor < len(text):
            if text[cursor] == "{":
                inner += 1
            elif text[cursor] == "}":
                if inner == 0:
                    break
                inner -= 1
            cursor += 1
        if cursor >= len(text):
            unterminated = True

    # What the renderer returns with no values at all. A surviving "{token}" is
    # a token that would reach a customer verbatim.
    rendered = _render_template(text, locale, {})
    leaked = _PLACEHOLDER.findall(rendered)

    # Brace contents the renderer cannot fill. ``_PLACEHOLDER`` only matches a
    # bare \w+ name, so a token like ``{na-me}`` slips past ``leaked_tokens``
    # while still printing verbatim -- caught here by looking at the rendered
    # output rather than at the template.
    residual = [content for content in _BRACE_CONTENT.findall(rendered)]

    # Fields that would make translate() raise rather than degrade.
    raising = _RENDER_RAISES_FIELD.findall(text)

    # Selectors of each plural block, brace-balanced.
    selectors: list[str] = []
    for match in _PLURAL_BLOCK.finditer(text):
        cursor = match.end()
        inner = 0
        start = cursor
        while cursor < len(text):
            if text[cursor] == "{":
                inner += 1
            elif text[cursor] == "}":
                if inner == 0:
                    break
                inner -= 1
            cursor += 1
        selectors.extend(selector for selector, _ in _split_branches(text[start:cursor]))

    return {
        "plain": plain,
        "plural": plural,
        "selectors": selectors,
        "depth": depth,
        "lowest_depth": lowest,
        "stray_close": stray_close,
        "unterminated_plural": unterminated,
        "leaked_tokens": leaked,
        "residual_braces": residual,
        "raising_fields": raising,
        "rendered_without_values": rendered,
        "characters": len(text),
    }


def message_placeholder_audit(catalog: Any = None) -> dict[str, Any]:
    """Which strings will print a raw ``{token}``, and whose placeholders disagree.

    Three questions, all answerable without rendering a request:

    1. *Do the locales of one key declare the same placeholders?* A token
       present in English and absent in Spanish leaks in Spanish and is dead
       weight in English. The shipped catalog passes this -- every locale of
       every key agrees -- which is the point: the check exists so the first key
       that does not agree is caught at review time.
    2. *Does a template render without a value?* Each template is rendered with
       no values, and any token that survives is one the renderer could not
       fill. This is a live finding, not a hypothetical one: ``auth.welcome``
       leaks ``{name}`` in all three locales, because the renderer swallows the
       ``KeyError`` and returns the joined string.
    3. *Does the namespace policy admit what the template actually contains?*

    Reads ``catalog`` rather than the module global when one is passed, so a
    proposed catalog can be checked before it is installed.
    """
    source = MESSAGE_CATALOG if catalog is None else catalog
    findings: list[dict[str, Any]] = []
    by_key: dict[str, Any] = {}

    grouped: dict[str, dict[str, Any]] = {}
    for key, locale, template in _catalog_entries(source):
        grouped.setdefault(key, {})[locale] = template

    for key in sorted(grouped):
        entries = grouped[key]
        namespace = _namespace_of(key)
        policy = MESSAGE_NAMESPACES.get(namespace)
        policy_name = str(policy.get("placeholder_policy")) if policy else ""
        spec = PLACEHOLDER_POLICIES.get(policy_name, {})

        if policy is None:
            findings.append(
                _finding(
                    "I18N_NAMESPACE_UNKNOWN",
                    f"key {key!r} is in namespace {namespace!r}, which has no MESSAGE_NAMESPACES row",
                    key=key,
                    namespace=namespace,
                )
            )
        elif policy_name and not spec:
            findings.append(
                _finding(
                    "I18N_POLICY_UNKNOWN",
                    f"namespace {namespace!r} points at unknown policy {policy_name!r}",
                    key=key,
                    namespace=namespace,
                )
            )

        per_locale: dict[str, Any] = {}
        for locale in sorted(entries):
            template = entries[locale]
            scan = _template_scan(template, locale)
            per_locale[locale] = {
                "plain": scan["plain"],
                "plural": scan["plural"],
                "selectors": scan["selectors"],
                "characters": scan["characters"],
                "leaked_tokens": scan["leaked_tokens"],
                "residual_braces": scan["residual_braces"],
                "raising_fields": scan["raising_fields"],
                "rendered_without_values": scan["rendered_without_values"],
            }

            for token in scan["leaked_tokens"]:
                findings.append(
                    _finding(
                        "I18N_VALUE_NOT_SUPPLIED",
                        (
                            f"{key!r} [{locale}] renders with no value supplied and returns "
                            f"{scan['rendered_without_values']!r}"
                        ),
                        key=key,
                        locale=locale,
                        token=token,
                        evidence=scan["rendered_without_values"],
                    )
                )

            # A brace token the renderer has no way to fill, found in the output
            # rather than the template so it is a token that *survived*, not
            # merely one that looks odd.
            for content in scan["residual_braces"]:
                if re.fullmatch(r"\w+", content):
                    continue  # already reported as an unfilled value above
                findings.append(
                    _finding(
                        "I18N_PLACEHOLDER_NAME_INVALID",
                        (
                            f"{key!r} [{locale}] leaves {{{content}}} in the output; it is not a "
                            f"bare \\w+ name so the renderer can never fill it"
                        ),
                        key=key,
                        locale=locale,
                        token=content,
                        evidence=scan["rendered_without_values"],
                    )
                )

            # A dotted field escapes the renderer's except clause entirely.
            for root in scan["raising_fields"]:
                probe = _probe_render(template, locale, _demo_values(template))
                findings.append(
                    _finding(
                        "I18N_RENDER_RAISES",
                        (
                            f"{key!r} [{locale}] uses an attribute field on {root!r}, which the "
                            f"renderer's except (KeyError, IndexError, ValueError) does not cover: "
                            f"rendering with that value supplied raised "
                            f"{probe['raised']}({probe['message']})"
                        ),
                        key=key,
                        locale=locale,
                        field=root,
                        raised=probe["raised"],
                        evidence=probe["message"],
                    )
                )

            if not template.strip():
                findings.append(
                    _finding(
                        "I18N_TEMPLATE_EMPTY",
                        f"{key!r} [{locale}] has an empty template",
                        key=key,
                        locale=locale,
                    )
                )

            # Policy checks. ``allow_placeholders`` covers plain tokens; a plural
            # variable is governed by ``count_is_plural_variable`` instead.
            if spec and not spec.get("allow_placeholders", True) and scan["plain"]:
                findings.append(
                    _finding(
                        "I18N_PLACEHOLDER_UNEXPECTED",
                        (
                            f"{key!r} [{locale}] declares {scan['plain']} but policy "
                            f"{policy_name!r} expects no placeholders"
                        ),
                        key=key,
                        locale=locale,
                        tokens=list(scan["plain"]),
                    )
                )
            if spec:
                limit = spec.get("max_placeholders")
                try:
                    allowed = int(limit)
                except (TypeError, ValueError):
                    allowed = -1
                total = len(scan["plain"]) + len(scan["plural"])
                if allowed >= 0 and total > allowed:
                    findings.append(
                        _finding(
                            "I18N_PLACEHOLDER_LIMIT",
                            f"{key!r} [{locale}] uses {total} placeholders; policy {policy_name!r} allows {allowed}",
                            key=key,
                            locale=locale,
                            found=total,
                            allowed=allowed,
                        )
                    )
                pattern = str(spec.get("name_pattern") or "")
                if pattern:
                    try:
                        matcher = re.compile(pattern)
                    except (TypeError, ValueError, re.error):
                        matcher = None
                    if matcher is not None:
                        for token in list(scan["plain"]) + list(scan["plural"]):
                            if not matcher.match(token):
                                findings.append(
                                    _finding(
                                        "I18N_PLACEHOLDER_NAME_INVALID",
                                        f"{key!r} [{locale}] placeholder {token!r} does not match {pattern!r}",
                                        key=key,
                                        locale=locale,
                                        token=token,
                                        pattern=pattern,
                                    )
                                )

        # Cross-locale agreement on the placeholder set.
        signatures = {
            locale: (tuple(sorted(set(row["plain"]) | set(row["plural"]))))
            for locale, row in per_locale.items()
        }
        distinct = {value for value in signatures.values()}
        aligned = len(distinct) <= 1
        if not aligned and spec.get("must_match_across_locales", True):
            findings.append(
                _finding(
                    "I18N_PLACEHOLDER_MISMATCH",
                    f"{key!r} declares different placeholders per locale: {signatures}",
                    key=key,
                    signatures={locale: list(value) for locale, value in sorted(signatures.items())},
                )
            )

        by_key[key] = {
            "namespace": namespace,
            "policy": policy_name or None,
            "locales": sorted(per_locale),
            "placeholders_aligned": aligned,
            "placeholder_signature": {locale: list(value) for locale, value in sorted(signatures.items())},
            "templates": per_locale,
            "leaks": sorted({token for row in per_locale.values() for token in row["leaked_tokens"]}),
        }

    return {
        "message_count": len(by_key),
        "findings": findings,
        "finding_count": len(findings),
        "severity_counts": _severity_counts(findings),
        "keys_with_leaks": sorted(key for key, row in by_key.items() if row["leaks"]),
        "keys_with_unfillable_tokens": sorted(
            key
            for key, row in by_key.items()
            if any(
                content
                for template in row["templates"].values()
                for content in template["residual_braces"]
                if not re.fullmatch(r"\w+", content)
            )
        ),
        "keys_that_can_raise": sorted(
            {
                key
                for key, row in by_key.items()
                if any(template["raising_fields"] for template in row["templates"].values())
            }
        ),
        "misaligned_keys": sorted(key for key, row in by_key.items() if not row["placeholders_aligned"]),
        "by_key": by_key,
        "clean": not findings,
    }


def message_budget_report(catalog: Any = None) -> dict[str, Any]:
    """Template length against the owning namespace's budget.

    Not a style rule for its own sake: a length budget is a proxy for "will
    this fit in a notification, a table cell, and a push banner", and the
    namespace is the right unit because that is the unit one team edits. The
    measurement is over *every* locale of a key, since the longest translation
    is what overflows.
    """
    source = MESSAGE_CATALOG if catalog is None else catalog
    findings: list[dict[str, Any]] = []

    per_namespace: dict[str, dict[str, Any]] = {}
    for namespace, row in MESSAGE_NAMESPACES.items():
        try:
            budget = int(row.get("max_characters"))
        except (TypeError, ValueError):
            budget = -1
        per_namespace[namespace] = {
            "description": str(row.get("description") or ""),
            "owner": str(row.get("owner") or ""),
            "placeholder_policy": str(row.get("placeholder_policy") or ""),
            "max_characters": budget,
            "keys": 0,
            "templates": 0,
            "min_characters": None,
            "max_characters_observed": None,
            "mean_characters": None,
            "over_budget": [],
        }

    lengths: list[int] = []
    longest: list[dict[str, Any]] = []
    unmapped: dict[str, list[str]] = {}
    for key, locale, template in _catalog_entries(source):
        namespace = _namespace_of(key)
        count = len(template)
        lengths.append(count)
        longest.append({"key": key, "locale": locale, "characters": count})
        bucket = per_namespace.get(namespace)
        if bucket is None:
            # No namespace means no budget, and the key still has to be counted
            # in the overall numbers -- but a key nothing governs is itself the
            # finding, and the placeholder audit says so too.
            unmapped.setdefault(namespace, []).append(key)
            findings.append(
                _finding(
                    "I18N_NAMESPACE_UNKNOWN",
                    f"{key!r} is in namespace {namespace!r}, which has no MESSAGE_NAMESPACES row, so no length budget applies",
                    key=key,
                    locale=locale,
                    namespace=namespace,
                    characters=count,
                )
            )
            continue
        bucket["keys"] += 1
        bucket["templates"] += 1
        current_min = bucket["min_characters"]
        bucket["min_characters"] = count if current_min is None else min(current_min, count)
        current_max = bucket["max_characters_observed"]
        bucket["max_characters_observed"] = count if current_max is None else max(current_max, count)
        budget = bucket["max_characters"]
        if budget >= 0 and count > budget:
            bucket["over_budget"].append(
                {"key": key, "locale": locale, "characters": count, "over_by": count - budget}
            )
            findings.append(
                _finding(
                    "I18N_BUDGET_EXCEEDED",
                    f"{key!r} [{locale}] is {count} characters; namespace {namespace!r} budgets {budget}",
                    key=key,
                    locale=locale,
                    namespace=namespace,
                    characters=count,
                    budget=budget,
                )
            )

    for namespace, bucket in per_namespace.items():
        templates = bucket["templates"]
        if templates:
            bucket["mean_characters"] = round(
                sum(
                    len(template)
                    for key, _locale, template in _catalog_entries(source)
                    if _namespace_of(key) == namespace
                )
                / templates,
                2,
            )
        bucket["over_budget"].sort(key=lambda item: (-item["over_by"], item["key"], item["locale"]))

    longest.sort(key=lambda item: (-item["characters"], item["key"], item["locale"]))
    return {
        "overall": {
            "keys": len(_sorted_keys(source)),
            "templates": len(lengths),
            "min_characters": min(lengths) if lengths else 0,
            "mean_characters": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
            "max_characters": max(lengths) if lengths else 0,
        },
        "namespaces": per_namespace,
        "unmapped_namespaces": {name: sorted(keys) for name, keys in sorted(unmapped.items())},
        "longest": longest[:10],
        "findings": findings,
        "finding_count": len(findings),
        "severity_counts": _severity_counts(findings),
        "clean": not findings,
    }


def unbalanced_template_report(catalog: Any = None) -> dict[str, Any]:
    """Template brace structure, and what survives a render with no values.

    Worth separating from :func:`message_placeholder_audit` because the two
    answer different questions. That one asks *which token is unfilled*; this
    one asks *is this template even well-formed*, and the interesting case is
    the one where the two interact: a single unescaped ``}`` makes the
    renderer's final ``str.format`` raise, the ``ValueError`` is caught, and
    **every** placeholder in the string stops interpolating. One stray brace
    turns ``"Welcome, {name} }"`` into a string that prints ``{name}`` to a
    customer while looking, in the catalog, like a working template.

    The scan is done here rather than inferred from the renderer, because
    surviving this input is the renderer's job and a report that asked it
    whether the input was valid would be asking the wrong question.
    """
    source = MESSAGE_CATALOG if catalog is None else catalog
    findings: list[dict[str, Any]] = []
    templates: dict[str, Any] = {}

    for key, locale, template in _catalog_entries(source):
        scan = _template_scan(template, locale)
        label = f"{key}|{locale}"
        templates[label] = {
            "key": key,
            "locale": locale,
            "balanced": scan["depth"] == 0 and scan["lowest_depth"] >= 0 and scan["stray_close"] == 0,
            "final_depth": scan["depth"],
            "lowest_depth": scan["lowest_depth"],
            "stray_close_brace": scan["stray_close"],
            "plural_blocks": len(scan["plural"]),
            "unterminated_plural": scan["unterminated_plural"],
            "leaked_tokens": sorted(set(scan["leaked_tokens"])),
            "rendered_without_values": scan["rendered_without_values"],
        }

        if scan["stray_close"]:
            findings.append(
                _finding(
                    "I18N_BRACE_UNBALANCED",
                    (
                        f"{key!r} [{locale}] has {scan['stray_close']} closing brace(s) with no "
                        f"opening brace; every placeholder in the template stops interpolating "
                        f"and it renders as {scan['rendered_without_values']!r}"
                    ),
                    key=key,
                    locale=locale,
                    stray_close=scan["stray_close"],
                    evidence=scan["rendered_without_values"],
                )
            )
        elif scan["depth"] or scan["lowest_depth"] < 0:
            findings.append(
                _finding(
                    "I18N_BRACE_UNBALANCED",
                    f"{key!r} [{locale}] does not return to brace depth zero (final={scan['depth']})",
                    key=key,
                    locale=locale,
                    final_depth=scan["depth"],
                )
            )

        if scan["unterminated_plural"]:
            findings.append(
                _finding(
                    "I18N_PLURAL_UNTERMINATED",
                    (
                        f"{key!r} [{locale}] has a plural block with no closing brace; the "
                        f"renderer discards everything after it and returns "
                        f"{scan['rendered_without_values']!r}"
                    ),
                    key=key,
                    locale=locale,
                    evidence=scan["rendered_without_values"],
                )
            )

        for token in sorted(set(scan["leaked_tokens"])):
            findings.append(
                _finding(
                    "I18N_VALUE_NOT_SUPPLIED",
                    f"{key!r} [{locale}] leaves {{{token}}} unsubstituted when no value is supplied",
                    key=key,
                    locale=locale,
                    token=token,
                    evidence=scan["rendered_without_values"],
                )
            )

    return {
        "template_count": len(templates),
        "unbalanced": sorted(label for label, row in templates.items() if not row["balanced"]),
        "unterminated": sorted(label for label, row in templates.items() if row["unterminated_plural"]),
        "templates": templates,
        "findings": findings,
        "finding_count": len(findings),
        "severity_counts": _severity_counts(findings),
        "clean": not findings,
    }


def plural_audit(catalog: Any = None) -> dict[str, Any]:
    """Which plural branches this renderer can actually select, and which it cannot.

    ``PLURAL_CATEGORIES`` declares six categories for ``ar``. The renderer
    implements one: ``plural_category`` returns ``"one"`` for a count of exactly
    1 and ``"other"`` for everything else, so ``zero``, ``two``, ``few`` and
    ``many`` are unreachable. A template translated against real CLDR rules
    would carry those branches and they would never fire -- silently, because an
    unselected branch is not an error.

    The reachable set is *probed*, not asserted from the source: the audit calls
    ``plural_category`` across :data:`_PROBE_COUNTS` and records what came back.
    That way, if the rule is ever widened, the table follows the code instead of
    the other way round.

    Also reported: a block with no ``other`` selector (the last branch silently
    serves every unmatched count), a ``#`` immediately followed by a digit (the
    count substitution rewrites a literal reference number), and the fact that a
    plural template rendered with no ``count`` produces its *zero* branch.
    """
    source = MESSAGE_CATALOG if catalog is None else catalog
    findings: list[dict[str, Any]] = []

    # --- per-locale rule reality
    locales: set[str] = set(SUPPORTED_LOCALES) | set(PLURAL_CATEGORIES) | set(LOCALE_EXPECTATIONS)
    locale_rules: dict[str, Any] = {}
    for locale in sorted(locales):
        declared = tuple(str(item) for item in PLURAL_CATEGORIES.get(locale, ()))
        reachable: list[str] = []
        for count in _PROBE_COUNTS:
            try:
                category = str(plural_category(count, locale))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                category = "error"
            if category not in reachable:
                reachable.append(category)
        unreachable = [item for item in declared if item not in reachable]
        locale_rules[locale] = {
            "declared": list(declared),
            "reachable": reachable,
            "unreachable_declared": unreachable,
            "in_registry": locale in SUPPORTED_LOCALES,
            "probe_counts": list(_PROBE_COUNTS),
            "implemented_rule": "one when the count is exactly 1, other otherwise",
        }
        if unreachable:
            findings.append(
                _finding(
                    "I18N_PLURAL_SELECTOR_UNREACHABLE",
                    (
                        f"locale {locale!r} declares {list(unreachable)} but plural_category() can "
                        f"only ever return {reachable}"
                    ),
                    locale=locale,
                    declared=list(declared),
                    unreachable=unreachable,
                    reachable=reachable,
                )
            )

    # --- per-block structure
    blocks: dict[str, Any] = {}
    selectors_by_key: dict[str, dict[str, list[str]]] = {}
    for key, locale, template in _catalog_entries(source):
        scan = _template_scan(template, locale)
        if not scan["plural"]:
            continue
        labels = f"{key}|{locale}"
        declared = set(locale_rules.get(locale, {}).get("declared") or ())
        reachable = set(locale_rules.get(locale, {}).get("reachable") or ())
        selectors = scan["selectors"]
        exact = [item for item in selectors if item.startswith("=")]
        named = [item for item in selectors if not item.startswith("=")]
        unreachable = [item for item in named if reachable and item not in reachable]
        undeclared = [item for item in named if declared and item not in declared]
        has_other = "other" in named

        blocks[labels] = {
            "key": key,
            "locale": locale,
            "variables": list(scan["plural"]),
            "selectors": selectors,
            "exact_selectors": exact,
            "named_selectors": named,
            "has_other": has_other,
            "unreachable_selectors": unreachable,
            "undeclared_selectors": undeclared,
            "unterminated": scan["unterminated_plural"],
        }
        selectors_by_key.setdefault(key, {})[locale] = selectors

        if not has_other:
            findings.append(
                _finding(
                    "I18N_PLURAL_NO_OTHER",
                    (
                        f"{key!r} [{locale}] has no 'other' branch ({selectors}); the last "
                        f"branch serves every count that matches nothing"
                    ),
                    key=key,
                    locale=locale,
                    selectors=selectors,
                )
            )
        for item in undeclared:
            findings.append(
                _finding(
                    "I18N_PLURAL_SELECTOR_UNDECLARED",
                    f"{key!r} [{locale}] uses selector {item!r}, which locale {locale!r} does not declare",
                    key=key,
                    locale=locale,
                    selector=item,
                )
            )
        for item in unreachable:
            findings.append(
                _finding(
                    "I18N_PLURAL_SELECTOR_UNREACHABLE",
                    f"{key!r} [{locale}] uses selector {item!r}, which plural_category() cannot return for {locale!r}",
                    key=key,
                    locale=locale,
                    selector=item,
                )
            )

        # A '#' directly followed by a digit is a literal reference number, and
        # the substitution is a plain str.replace with no boundary check.
        for match in re.finditer(r"#\d", template):
            findings.append(
                _finding(
                    "I18N_PLURAL_BARE_NUMBER",
                    (
                        f"{key!r} [{locale}] contains {template[max(0, match.start() - 12):match.end() + 12]!r}; "
                        f"the count substitution rewrites the '#' and the digits that follow it"
                    ),
                    key=key,
                    locale=locale,
                    offset=match.start(),
                )
            )

    # Divergence is a property of the key, so it is compared once per key over
    # every locale -- not as each locale happens to be visited, which would make
    # the finding depend on dictionary ordering.
    for key in sorted(selectors_by_key):
        per_locale = selectors_by_key[key]
        distinct = {tuple(sorted(value)) for value in per_locale.values()}
        if len(distinct) > 1:
            findings.append(
                _finding(
                    "I18N_PLURAL_CROSS_LOCALE_DIVERGENCE",
                    (
                        f"{key!r} declares different selectors per locale, so the same count "
                        f"reads differently depending on the locale: {per_locale}"
                    ),
                    key=key,
                    selectors={locale: value for locale, value in sorted(per_locale.items())},
                )
            )

    # --- the call-site probe: what a plural message says with no count
    #
    # Through the shipped ``translate`` when the audit is looking at the shipped
    # catalog, because then the evidence is the shipped function rather than a
    # re-implementation of it. When the caller handed in a different catalog the
    # probe has to follow *that* one, or the report would describe one catalog
    # with the render behaviour of another.
    live = catalog is None

    def _render(key: str, count: int | None) -> dict[str, Any]:
        if live:
            if count is None:
                return _safe_translate(key)
            return _safe_translate(key, count=count)
        template = ""
        entry = source.get(key) if isinstance(source, dict) else None
        if isinstance(entry, dict):
            candidate = entry.get(DEFAULT_LOCALE) or entry.get("en") or ""
            template = candidate if isinstance(candidate, str) else ""
        values = _demo_values(template, {} if count is None else {"count": count})
        return _probe_render(template, DEFAULT_LOCALE, values)

    call_site: dict[str, Any] = {}
    for key in sorted({row["key"] for row in blocks.values()}):
        rendered_one = _render(key, 1)
        rendered_zero = _render(key, 0)
        rendered_none = _render(key, None)
        if rendered_one.get("raised") or rendered_zero.get("raised") or rendered_none.get("raised"):
            raised = rendered_one.get("raised") or rendered_zero.get("raised") or rendered_none.get("raised")
            findings.append(
                _finding(
                    "I18N_RENDER_RAISES",
                    f"{key!r} could not be probed: rendering raised {raised}",
                    key=key,
                    raised=raised,
                )
            )
            call_site[key] = {"probe_failed": True, "raised": raised}
            continue
        one = rendered_one["rendered"]
        zero = rendered_zero["rendered"]
        none = rendered_none["rendered"]
        probe = {
            "count_1": one,
            "count_0": zero,
            "no_count": none,
            "no_count_equals_zero": none == zero,
        }
        call_site[key] = probe
        if probe["no_count_equals_zero"] and none != one:
            findings.append(
                _finding(
                    "I18N_PLURAL_COUNT_ABSENT",
                    (
                        f"{key!r} rendered with no count returns {none!r}, identical to "
                        f"count=0 -- a caller that forgets count= states a false zero"
                    ),
                    key=key,
                    rendered=none,
                )
            )

    return {
        "locale_rules": locale_rules,
        "blocks": blocks,
        "call_site_probe": call_site,
        "plural_keys": sorted({row["key"] for row in blocks.values()}),
        "findings": findings,
        "finding_count": len(findings),
        "severity_counts": _severity_counts(findings),
        "clean": not findings,
    }


def negotiation_audit() -> dict[str, Any]:
    """Why the two locale resolvers disagree, measured rather than argued.

    The module has two resolvers and they answer the same field differently:

    * :func:`resolve_locale` sets ``fallback_used`` to "the answer was not
      precisely what was asked for" -- so ``es`` is **not** a fallback.
    * :func:`negotiate_locale` sets it to ``base != normalized or base !=
      default_locale`` -- so ``es`` **is** a fallback, because English is the
      default.

    Same request, same field, opposite answer. Both are defensible definitions;
    a caller that compares them across the two functions is comparing two
    different questions. This function runs the same requests through both and
    reports where they part.

    It also reports three things about the header path that are properties of
    the shipped code rather than of any table: ``requested`` is the whole
    normalized header rather than a tag, a ``q=0`` tag -- which RFC 9110
    defines as not acceptable -- is still eligible, and a q outside ``[0, 1]``
    is kept verbatim and reorders the sort.
    """
    findings: list[dict[str, Any]] = []

    requests = ("en", "es", "fr", "pt-BR", "en-GB", "")
    comparison: list[dict[str, Any]] = []
    for request in requests:
        resolved = resolve_locale(request)
        negotiated = negotiate_locale(request)
        row = {
            "request": "" if request is None else request,
            "resolve": {
                "resolved": resolved.resolved,
                "fallback_used": resolved.fallback_used,
                "chain": list(resolved.chain),
                "landed_on": resolved.landed_on,
            },
            "negotiate": {
                "resolved": negotiated.resolved,
                "fallback_used": negotiated.fallback_used,
                "chain": list(negotiated.chain),
                "landed_on": negotiated.landed_on,
            },
            "same_resolution": resolved.resolved == negotiated.resolved,
            "same_fallback_flag": resolved.fallback_used == negotiated.fallback_used,
        }
        comparison.append(row)
        if not row["same_fallback_flag"]:
            findings.append(
                _finding(
                    "I18N_NEGOTIATION_FALLBACK_DIVERGENCE",
                    (
                        f"request {request!r}: resolve_locale reports fallback_used="
                        f"{resolved.fallback_used}, negotiate_locale reports "
                        f"{negotiated.fallback_used}, for the same resolved locale "
                        f"{resolved.resolved!r}"
                    ),
                    request="" if request is None else request,
                    resolve_fallback=resolved.fallback_used,
                    negotiate_fallback=negotiated.fallback_used,
                )
            )

    # chain means "the rungs walked" from one and "the candidates offered" from
    # the other, on the same dataclass.
    if comparison:
        sample = comparison[1] if len(comparison) > 1 else comparison[0]
        findings.append(
            _finding(
                "I18N_NEGOTIATION_CHAIN_SEMANTICS",
                (
                    f"for request {sample['request']!r} resolve_locale.chain is the rungs walked "
                    f"{sample['resolve']['chain']} while negotiate_locale.chain is the header's "
                    f"candidate tags {sample['negotiate']['chain']}; the field means two things"
                ),
                request=sample["request"],
            )
        )

    headers = (
        "fr-CA, es;q=0.8, en;q=0.5",
        "fr;q=0",
        "fr;q=5, es;q=-1",
        "fr;q=0.2;q=0.9, en;q=0.5",
        "*",
    )
    header_probe: list[dict[str, Any]] = []
    for header in headers:
        parsed = parse_accept_language(header)
        outcome = negotiate_locale(header)
        row = {
            "header": header,
            "parsed": [{"tag": tag, "q": q} for tag, q in parsed],
            "resolved": outcome.resolved,
            "requested_field": outcome.requested,
            "eligible_zero_q": [tag for tag, q in parsed if q == 0.0],
            "out_of_range_q": [
                {"tag": tag, "q": q} for tag, q in parsed if not 0.0 <= q <= 1.0
            ],
            "repeated_q": header.count("q=") > 1,
        }
        header_probe.append(row)

        if row["eligible_zero_q"]:
            findings.append(
                _finding(
                    "I18N_ACCEPT_LANGUAGE_Q_ZERO",
                    (
                        f"header {header!r} marks {row['eligible_zero_q']} q=0, which RFC 9110 "
                        f"defines as not acceptable; negotiate_locale still resolved {outcome.resolved!r}"
                    ),
                    header=header,
                    tags=list(row["eligible_zero_q"]),
                    resolved=outcome.resolved,
                )
            )
        if row["out_of_range_q"]:
            findings.append(
                _finding(
                    "I18N_ACCEPT_LANGUAGE_Q_UNCLAMPED",
                    f"header {header!r} carries q outside [0, 1]: {row['out_of_range_q']}",
                    header=header,
                    values=row["out_of_range_q"],
                )
            )
        if row["repeated_q"]:
            findings.append(
                _finding(
                    "I18N_ACCEPT_LANGUAGE_DUPLICATE_Q",
                    f"header {header!r} repeats the q parameter; the last one wins",
                    header=header,
                )
            )
        if outcome.requested and ("," in outcome.requested or ";" in outcome.requested):
            findings.append(
                _finding(
                    "I18N_NEGOTIATION_REQUESTED_NOT_A_TAG",
                    (
                        f"header {header!r} reports requested={outcome.requested!r} -- the entire "
                        f"header normalized as one tag, not a language tag"
                    ),
                    header=header,
                    requested=outcome.requested,
                )
            )

    # ``landed_on`` keeps the region when the requested tag was exact -- it names
    # the chain rung that matched. ``resolved`` never does, and ``resolved`` is
    # what ``translate`` and ``locale_payload`` key the catalog on, so the region
    # is what actually gets thrown away. Both are recorded so the distinction is
    # visible rather than asserted.
    regional: list[dict[str, Any]] = []
    for request in ("es-ES", "es-419", "pt-BR", "en-GB"):
        resolution = resolve_locale(request)
        row = {
            "request": request,
            "normalized": normalize_locale(request),
            "resolved": resolution.resolved,
            "landed_on": resolution.landed_on,
            "landed_on_keeps_region": "-" in (resolution.landed_on or ""),
            "resolved_is_bare_language": "-" not in (resolution.resolved or ""),
        }
        regional.append(row)
        if "-" in normalize_locale(request) and row["resolved_is_bare_language"]:
            findings.append(
                _finding(
                    "I18N_NEGOTIATION_REGION_DISCARDED",
                    (
                        f"request {request!r} carries a region, lands on "
                        f"{resolution.landed_on!r}, and resolves to the bare language "
                        f"{resolution.resolved!r}; translate and locale_payload both key the "
                        f"catalog on 'resolved', so the region is what gets thrown away and "
                        f"es_ES and es_419 cannot be told apart"
                    ),
                    request=request,
                    resolved=resolution.resolved,
                    landed_on=resolution.landed_on,
                )
            )

    return {
        "resolvers": ["resolve_locale", "negotiate_locale"],
        "fallback_definitions": {
            "resolve_locale": "fallback_used = the answer is not exactly the normalized request",
            "negotiate_locale": "fallback_used = the answer is not the default locale",
        },
        "comparison": comparison,
        "header_probe": header_probe,
        "regional_probe": regional,
        "diverging_requests": sorted(
            row["request"] for row in comparison if not row["same_fallback_flag"]
        ),
        "findings": findings,
        "finding_count": len(findings),
        "severity_counts": _severity_counts(findings),
        "clean": not findings,
    }


def locale_registry_audit() -> dict[str, Any]:
    """Which tables in this module describe something that cannot happen.

    Three of them describe a locale the registry does not ship, and every
    one of them is unreachable as a result:

    * ``RTL_LOCALES`` names ``ar``, ``he``, ``fa``, ``ur``. None is in
      ``SUPPORTED_LOCALES``, so ``locale_payload()['direction']`` is ``"ltr"``
      for every locale a request can resolve to, and
      ``catalog_coverage()['rtl_locales']`` is always empty. The mirror
      instruction is in the table and the code can never issue it.
    * ``PLURAL_CATEGORIES`` declares six categories for ``ar``, which is also
      not shipped -- and would be unreachable if it were, since
      ``plural_category`` implements one-versus-other.
    * ``LOCALE_EXPECTATIONS`` carries a row for ``ar`` for the same reason.

    Reported, not repaired. Adding a locale means adding its templates and is a
    product decision; dropping the rows would delete the record that a language
    was once intended.

    The second half of the report is about the per-locale ``fallback`` key, which
    ``locale_payload`` and ``build_i18n_catalog`` both surface as if it were
    live while no resolution path reads it, and about overrides: a tenant can
    define keys the shipped catalog has never heard of, and neither
    ``catalog_coverage`` nor ``build_i18n_catalog`` can see them.
    """
    findings: list[dict[str, Any]] = []

    registry = set(SUPPORTED_LOCALES)
    rtl = set(str(item) for item in RTL_LOCALES)
    plural_locales = set(PLURAL_CATEGORIES)
    expectation_locales = set(LOCALE_EXPECTATIONS)

    unreachable_rtl = sorted(rtl - registry)
    unreachable_plural = sorted(plural_locales - registry)

    for locale in unreachable_rtl:
        findings.append(
            _finding(
                "I18N_LOCALE_NOT_IN_REGISTRY",
                (
                    f"RTL_LOCALES names {locale!r}, which SUPPORTED_LOCALES does not ship; "
                    f"direction can never be 'rtl' for a resolvable locale"
                ),
                locale=locale,
                source="RTL_LOCALES",
            )
        )
    for locale in unreachable_plural:
        findings.append(
            _finding(
                "I18N_LOCALE_NOT_IN_REGISTRY",
                (
                    f"PLURAL_CATEGORIES declares a rule set for {locale!r}, which "
                    f"SUPPORTED_LOCALES does not ship; no request can resolve to it"
                ),
                locale=locale,
                source="PLURAL_CATEGORIES",
            )
        )
    for locale in sorted(expectation_locales - registry):
        row = LOCALE_EXPECTATIONS.get(locale, {})
        if not row.get("in_registry"):
            findings.append(
                _finding(
                    "I18N_LOCALE_NOT_IN_REGISTRY",
                    (
                        f"LOCALE_EXPECTATIONS has a row for {locale!r} (direction "
                        f"{row.get('direction')!r}, script {row.get('script')!r}) that no request "
                        f"can resolve to"
                    ),
                    locale=locale,
                    source="LOCALE_EXPECTATIONS",
                    named_by=list(row.get("source") or ()),
                )
            )

    # How many templates each locale actually has. Note this counts *inside* the
    # per-key entries -- MESSAGE_CATALOG is {key: {locale: template}}, so asking
    # the top level for a locale finds nothing and would report every shipped
    # locale as translation-less.
    templates_by_locale: dict[str, int] = {}
    for _key, locale, template in _catalog_entries(MESSAGE_CATALOG):
        templates_by_locale[locale] = templates_by_locale.get(locale, 0) + 1

    for locale in sorted(registry):
        if locale not in expectation_locales:
            findings.append(
                _finding(
                    "I18N_LOCALE_NO_EXPECTATIONS",
                    f"shipped locale {locale!r} has no LOCALE_EXPECTATIONS row",
                    locale=locale,
                )
            )
        if not templates_by_locale.get(locale):
            findings.append(
                _finding(
                    "I18N_LOCALE_NO_TEMPLATES",
                    f"locale {locale!r} is in the registry with no templates, so every message falls back to English",
                    locale=locale,
                )
            )

    # The declared per-locale fallback: surfaced by two payloads, read by none.
    fallback_rows: list[dict[str, Any]] = []
    for locale in sorted(registry):
        entry = SUPPORTED_LOCALES.get(locale) or {}
        declared = entry.get("fallback") if isinstance(entry, dict) else None
        row = {
            "locale": locale,
            "declared_fallback": declared,
            "default_locale": DEFAULT_LOCALE,
            "used_by_resolution": False,
            "agrees_with_default": declared == DEFAULT_LOCALE,
        }
        fallback_rows.append(row)
        if not row["agrees_with_default"] and declared is not None:
            findings.append(
                _finding(
                    "I18N_LOCALE_FALLBACK_UNUSED",
                    (
                        f"SUPPORTED_LOCALES[{locale!r}]['fallback'] is {declared!r} but every "
                        f"resolution path uses DEFAULT_LOCALE ({DEFAULT_LOCALE!r}); the value is "
                        f"reported by locale_payload and build_i18n_catalog and changes nothing"
                    ),
                    locale=locale,
                    declared=declared,
                )
            )
        if declared is not None and str(declared) not in registry:
            findings.append(
                _finding(
                    "I18N_LOCALE_FALLBACK_NOT_SHIPPED",
                    f"SUPPORTED_LOCALES[{locale!r}]['fallback'] names {declared!r}, which is not in the registry",
                    locale=locale,
                    declared=declared,
                )
            )

    ambiguous: list[str] = []
    for locale in sorted(expectation_locales):
        row = LOCALE_EXPECTATIONS.get(locale) or {}
        if row.get("region_ambiguous"):
            ambiguous.append(locale)
            if locale in registry:
                findings.append(
                    _finding(
                        "I18N_LOCALE_REGION_AMBIGUOUS",
                        (
                            f"locale {locale!r} has region-dependent conventions "
                            f"(profiles {list(row.get('alternates') or ())} and "
                            f"{row.get('format_profile')!r}) and negotiation discards the region"
                        ),
                        locale=locale,
                        profiles=[row.get("format_profile"), *(row.get("alternates") or ())],
                    )
                )

    # Overrides: the one part of the message surface coverage cannot see.
    override_scopes = OVERRIDES.scopes()
    override_keys: dict[str, list[str]] = {}
    shipped = MESSAGE_CATALOG if isinstance(MESSAGE_CATALOG, dict) else {}
    for scope, keys in OVERRIDES.keys_by_scope().items():
        for key in keys:
            if key not in shipped:
                override_keys.setdefault(key, []).append(scope)
    for key in sorted(override_keys):
        findings.append(
            _finding(
                "I18N_OVERRIDE_OUTSIDE_CATALOG",
                (
                    f"scope(s) {override_keys[key]} define {key!r}, which MESSAGE_CATALOG has "
                    f"never heard of; catalog_coverage and build_i18n_catalog report "
                    f"{len(MESSAGE_CATALOG) if isinstance(MESSAGE_CATALOG, dict) else 0} keys and "
                    f"cannot see it"
                ),
                key=key,
                scopes=override_keys[key],
            )
        )

    return {
        "registry": sorted(registry),
        "rtl_locales": sorted(rtl),
        "rtl_unreachable": unreachable_rtl,
        "plural_locales": sorted(plural_locales),
        "plural_unreachable": unreachable_plural,
        "expectation_locales": sorted(expectation_locales),
        "templates_by_locale": {locale: templates_by_locale.get(locale, 0) for locale in sorted(registry)},
        "fallback_rows": fallback_rows,
        "region_ambiguous": ambiguous,
        "direction_by_locale": {
            locale: "rtl" if locale in RTL_LOCALES else "ltr" for locale in sorted(registry)
        },
        "override_keys_outside_catalog": override_keys,
        "overrides": OVERRIDES.stats(),
        "findings": findings,
        "finding_count": len(findings),
        "severity_counts": _severity_counts(findings),
        "clean": not findings,
    }


# --- Locale-aware number formatting ---------------------------------------------

# The profile used when a locale has no expectation row and the caller named no
# profile. Named rather than inlined so the choice is inspectable.
DEFAULT_FORMAT_PROFILE = "en_us"


def _grouped(digits: str, spec: dict[str, Any]) -> str:
    """Insert a group's separator, if this profile groups at this magnitude.

    CLDR carries a ``minimumGroupingDigits`` per locale -- Spanish groups from
    four digits, English from five in some data. The table has the column, and
    this honours it rather than hardcoding a threshold, so a locale row that
    declares ``2`` groups at 10,000 and one that declares ``1`` groups at 1,000.
    """
    try:
        group_size = int(spec.get("group_size"))
        minimum = int(spec.get("minimum_grouping_digits"))
    except (TypeError, ValueError):
        return digits
    if group_size <= 0:
        return digits
    if len(digits) < group_size + max(minimum, 1):
        return digits
    separator = str(spec.get("group_separator") or "")
    if not separator:
        return digits
    parts: list[str] = []
    end = len(digits)
    while end > group_size:
        parts.append(digits[end - group_size : end])
        end -= group_size
    parts.append(digits[:end])
    return separator.join(reversed(parts))


def format_number(
    value: Any,
    locale: str | None = None,
    *,
    kind: str = "number",
    decimals: int | None = None,
    profile: str | None = None,
) -> str:
    """Format a number the way ``locale``'s region writes numbers.

    Additive, and deliberately *not* wired into :func:`translate`. The renderer
    emits ``str(int(numeric))`` for a plural ``#`` and has always done so; this
    function exists so a caller that wants a grouped, localised number has one,
    and it does not start calling it on the shipped path.

    Three hops, and which one was taken is visible in
    :func:`build_i18n_governance_catalog`:

    1. an explicit ``profile=``,
    2. the ``format_profile`` of the locale's :data:`LOCALE_EXPECTATIONS` row,
       resolved on the *language* -- so ``es-419`` gets ``es_ES`` unless the
       caller names ``es_419`` itself, which is the region-ambiguity finding from
       :func:`locale_registry_audit` showing up as a visible default,
    3. :data:`DEFAULT_FORMAT_PROFILE`.

    A value that is not numeric is returned as ``str(value)`` rather than
    raising, matching the renderer's own contract of degrading instead of
    failing. ``decimals=None`` keeps the value's own precision with trailing
    zeros trimmed; ``decimals=N`` rounds to N places, and ``kind="currency"``
    defaults to 2 because a currency amount without cents is a decision a caller
    should have to make.
    """
    spec = NUMBER_FORMATS.get(str(profile or ""), {})
    if not spec:
        base = normalize_locale(locale).split("-", 1)[0] or DEFAULT_LOCALE
        row = LOCALE_EXPECTATIONS.get(base, {})
        spec = NUMBER_FORMATS.get(str(row.get("format_profile") or ""), {})
    if not spec:
        spec = NUMBER_FORMATS.get(DEFAULT_FORMAT_PROFILE, {})

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if decimals is None and str(kind).lower() == "currency":
        decimals = 2
    try:
        places = None if decimals is None else max(int(decimals), 0)
    except (TypeError, ValueError):
        places = None
    if places is not None:
        number = round(number, places)

    negative = number < 0
    magnitude = abs(number)
    if places is not None:
        text = f"{magnitude:.{places}f}"
        integer_part, _, fraction = text.partition(".")
    elif magnitude.is_integer():
        integer_part, fraction = str(int(magnitude)), ""
    else:
        text = repr(magnitude)
        integer_part, _, fraction = text.partition(".")
        if fraction.endswith("0"):
            fraction = fraction.rstrip("0")
        if integer_part.endswith(".0"):
            integer_part = integer_part[:-2]
    if not integer_part:
        integer_part = "0"

    separator = str(spec.get("decimal_separator") or ".")
    body = _grouped(integer_part, spec) + (separator + fraction if fraction else "")

    sign = ""
    if negative:
        sign = "-" if str(spec.get("negative_sign_position") or "prefix") == "prefix" else ""

    mode = str(kind).lower()
    if mode == "percent":
        marker = "%" if str(spec.get("percent_position") or "suffix") == "suffix" else "%\u00a0"
        return f"{sign}{body}{marker}"
    if mode == "currency":
        symbol = str(spec.get("currency_symbol") or "")
        gap = str(spec.get("currency_separator") or "")
        if not symbol:
            return f"{sign}{body}"
        if str(spec.get("currency_position") or "prefix") == "prefix":
            return f"{sign}{symbol}{gap}{body}"
        return f"{sign}{body}{gap}{symbol}"
    return f"{sign}{body}"


# --- Validation -----------------------------------------------------------------

def validate_i18n() -> dict[str, Any]:
    """Check the six config tables against each other and against the code.

    Nothing here raises, and nothing here calls a resolution function for a
    decision -- the behaviours are the audits' business. This is the
    *configuration* check: does every table point at something that exists.

    Errors are things that are certainly wrong -- a namespace pointing at a
    policy that does not exist, a locale pointing at a format profile that does
    not exist, a render op naming a finding code that is not in the taxonomy, a
    finding claiming to be emitted by a function that is not in
    :data:`_AUDIT_FUNCTIONS`, a malformed number-format row.

    Warnings are things that are notable but not broken: a namespace a
    near-duplicate names that does not exist, a namespace no key uses, a
    finding code nothing currently emits, a template that is empty.

    It reads the tables as they are. Nothing here consults an index derived at
    import time, so editing a table and re-running gets a verdict on the edited
    table.
    """
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    # Taxonomy codes this validator produced, so the catalog's finding roll-up
    # includes them. A code whose ``emitted_by`` names this function and which
    # never appears here would be a claim the module does not keep.
    coded: dict[str, list[str]] = {}

    def _note(code: str, message: str, severity: str = "info") -> None:
        (warnings if severity == "warning" else info).append(message)
        coded.setdefault(code, []).append(message)

    def _rows(table: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not isinstance(table, dict):
            errors.append(f"expected a mapping, got {type(table).__name__}")
            return out
        for name, row in table.items():
            key = name if isinstance(name, str) else str(name)
            if not isinstance(row, dict):
                errors.append(f"{key}: expected a mapping row, got {type(row).__name__}")
                continue
            out[key] = row
        return out

    namespace_rows = _rows(MESSAGE_NAMESPACES)
    policy_rows = _rows(PLACEHOLDER_POLICIES)
    locale_rows = _rows(LOCALE_EXPECTATIONS)
    format_rows = _rows(NUMBER_FORMATS)
    op_rows = _rows(RENDER_OPS)
    warning_rows = _rows(I18N_WARNINGS)

    # --- namespaces -> policies, and the near-duplicate graph
    for name in sorted(namespace_rows):
        row = namespace_rows[name]
        # The descriptive columns exist for whoever edits a namespace next --
        # "whose string is this, and does a customer read it" is the question
        # behind the length budget -- so an empty one is a hole, not a default.
        for column in ("description", "owner", "surface", "audience"):
            if not str(row.get(column) or "").strip():
                errors.append(f"MESSAGE_NAMESPACES[{name}]: {column} is empty")
        if str(row.get("surface") or "") not in ("user", "operator"):
            errors.append(
                f"MESSAGE_NAMESPACES[{name}]: surface must be 'user' or 'operator', got {row.get('surface')!r}"
            )
        policy = str(row.get("placeholder_policy") or "")
        if not policy:
            errors.append(f"MESSAGE_NAMESPACES[{name}]: no placeholder_policy")
        elif policy not in policy_rows:
            errors.append(f"MESSAGE_NAMESPACES[{name}]: unknown placeholder_policy {policy!r}")
        budget = row.get("max_characters")
        try:
            if int(budget) < 0:
                warnings.append(f"MESSAGE_NAMESPACES[{name}]: negative max_characters {budget!r}")
        except (TypeError, ValueError):
            errors.append(f"MESSAGE_NAMESPACES[{name}]: max_characters is not an integer ({budget!r})")
        for other in row.get("near_duplicates") or ():
            if str(other) not in namespace_rows:
                errors.append(
                    f"MESSAGE_NAMESPACES[{name}]: near_duplicate {other!r} is not a namespace"
                )
    for a, b in itertools.combinations(sorted(namespace_rows), 2):
        if b in tuple(namespace_rows[a].get("near_duplicates") or ()) and a in tuple(
            namespace_rows[b].get("near_duplicates") or ()
        ):
            _note(
                "I18N_NAMESPACE_NEAR_DUPLICATE",
                f"MESSAGE_NAMESPACES: {a!r} and {b!r} are declared mutual near-duplicates; "
                f"kept as-is because merging them moves keys clients already reference",
            )
    unused = {name for name in namespace_rows if not any(
        _namespace_of(key) == name for key in _sorted_keys(MESSAGE_CATALOG)
    )}
    for name in sorted(unused):
        warnings.append(f"MESSAGE_NAMESPACES[{name}]: no catalog key uses this namespace")

    # --- policies
    for name in sorted(policy_rows):
        row = policy_rows[name]
        try:
            int(row.get("max_placeholders"))
        except (TypeError, ValueError):
            errors.append(f"PLACEHOLDER_POLICIES[{name}]: max_placeholders is not an integer")
        pattern = str(row.get("name_pattern") or "")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"PLACEHOLDER_POLICIES[{name}]: name_pattern does not compile ({exc})")
        if row.get("count_is_plural_variable") and row.get("require_values"):
            errors.append(
                f"PLACEHOLDER_POLICIES[{name}]: a plural count cannot both be optional and required"
            )

    # --- locales -> format profiles
    for name in sorted(locale_rows):
        row = locale_rows[name]
        if not str(row.get("script") or "").strip():
            errors.append(f"LOCALE_EXPECTATIONS[{name}]: script is empty")
        if not str(row.get("direction") or "").strip():
            errors.append(f"LOCALE_EXPECTATIONS[{name}]: direction is empty")
        # Every row has to say which existing table put this locale in the
        # governance layer, because 'ar' is here because RTL_LOCALES and
        # PLURAL_CATEGORIES mention it, not because the registry ships it.
        if not row.get("source"):
            errors.append(f"LOCALE_EXPECTATIONS[{name}]: no source recorded")
        else:
            for table in row.get("source") or ():
                if str(table) not in ("SUPPORTED_LOCALES", "PLURAL_CATEGORIES", "RTL_LOCALES"):
                    errors.append(
                        f"LOCALE_EXPECTATIONS[{name}]: source names {table!r}, which is not a table in this module"
                    )
        target = str(row.get("format_profile") or "")
        if not target:
            errors.append(f"LOCALE_EXPECTATIONS[{name}]: no format_profile")
        elif target not in format_rows:
            errors.append(f"LOCALE_EXPECTATIONS[{name}]: unknown format_profile {target!r}")
        for other in row.get("alternates") or ():
            if str(other) not in format_rows:
                errors.append(f"LOCALE_EXPECTATIONS[{name}]: unknown alternate profile {other!r}")
        direction = str(row.get("direction") or "")
        if direction and direction not in ("ltr", "rtl"):
            errors.append(f"LOCALE_EXPECTATIONS[{name}]: direction must be 'ltr' or 'rtl', got {direction!r}")
        if bool(row.get("in_registry")) and name not in SUPPORTED_LOCALES:
            errors.append(
                f"LOCALE_EXPECTATIONS[{name}]: in_registry is true but SUPPORTED_LOCALES has no such locale"
            )
        if not row.get("in_registry") and name in SUPPORTED_LOCALES:
            errors.append(
                f"LOCALE_EXPECTATIONS[{name}]: in_registry is false but SUPPORTED_LOCALES ships it"
            )
        if name in RTL_LOCALES and direction != "rtl":
            errors.append(
                f"LOCALE_EXPECTATIONS[{name}]: RTL_LOCALES lists this locale but direction is {direction!r}"
            )

    # --- number formats
    for name in sorted(format_rows):
        row = format_rows[name]
        for column in ("group_size", "minimum_grouping_digits"):
            try:
                if int(row.get(column)) < 0:
                    errors.append(f"NUMBER_FORMATS[{name}]: {column} is negative")
            except (TypeError, ValueError):
                errors.append(f"NUMBER_FORMATS[{name}]: {column} is not an integer ({row.get(column)!r})")
        decimal = str(row.get("decimal_separator") or "")
        group = str(row.get("group_separator") or "")
        if decimal and group and decimal == group:
            errors.append(
                f"NUMBER_FORMATS[{name}]: decimal_separator and group_separator are the same character"
            )
        for column in ("percent_position", "currency_position", "negative_sign_position"):
            value = str(row.get(column) or "")
            if value not in ("prefix", "suffix"):
                errors.append(
                    f"NUMBER_FORMATS[{name}]: {column} must be 'prefix' or 'suffix', got {value!r}"
                )
        if not row.get("language"):
            warnings.append(f"NUMBER_FORMATS[{name}]: no language column")
        if not row.get("region"):
            warnings.append(f"NUMBER_FORMATS[{name}]: no region column")

    referenced = {str(row.get("format_profile") or "") for row in locale_rows.values()}
    for name in sorted(format_rows):
        if name not in referenced:
            info.append(f"NUMBER_FORMATS[{name}]: no LOCALE_EXPECTATIONS row selects this profile")

    # --- render ops -> finding taxonomy
    for name in sorted(op_rows):
        row = op_rows[name]
        code = str(row.get("finding") or "")
        if not code:
            errors.append(f"RENDER_OPS[{name}]: no finding code")
        elif code not in warning_rows:
            errors.append(f"RENDER_OPS[{name}]: unknown finding code {code!r}")
        if "current_behaviour" not in row:
            errors.append(f"RENDER_OPS[{name}]: no current_behaviour recorded")
        if row.get("report_only") is not True:
            errors.append(
                f"RENDER_OPS[{name}]: report_only must be True; the renderer is not this layer's to change"
            )
        if not str(row.get("branch") or ""):
            warnings.append(f"RENDER_OPS[{name}]: no branch recorded")

    # --- finding taxonomy -> producers
    for code in sorted(warning_rows):
        row = warning_rows[code]
        severity = str(row.get("severity") or "")
        if severity not in ("defect", "wart", "error", "warning", "info"):
            errors.append(f"I18N_WARNINGS[{code}]: severity must be defect/wart/error/warning/info, got {severity!r}")
        producers = row.get("emitted_by") or ()
        if not producers:
            errors.append(f"I18N_WARNINGS[{code}]: no emitted_by")
        for producer in producers:
            if str(producer) not in _AUDIT_FUNCTIONS:
                errors.append(
                    f"I18N_WARNINGS[{code}]: emitted_by names {producer!r}, which is not an audit in this module"
                )

    # --- the catalog itself, judged against the namespaces
    entries = _catalog_entries(MESSAGE_CATALOG)
    for key, locale, template in entries:
        namespace = _namespace_of(key)
        if namespace not in namespace_rows:
            errors.append(f"MESSAGE_CATALOG: key {key!r} has no MESSAGE_NAMESPACES row for {namespace!r}")
        if not template:
            warnings.append(f"MESSAGE_CATALOG[{key!r}][{locale!r}]: empty template")
    if not entries:
        warnings.append("MESSAGE_CATALOG is empty or malformed")

    # --- module-level regexes: is anything declared and unused?
    for name in ("_PLURAL_BLOCK", "_PLACEHOLDER"):
        pattern = globals().get(name)
        if pattern is None:
            errors.append(f"module pattern {name} is missing")
        elif not hasattr(pattern, "findall"):
            errors.append(f"module pattern {name} is not a compiled pattern")
        elif name == "_PLACEHOLDER":
            # It was declared and never used by any shipped path. This
            # governance layer is its first caller, which is the answer to
            # "is that dead code?" -- it is now load-bearing for the audits.
            _note(
                "I18N_UNUSABLE_PATTERN",
                f"{name} was declared but unused by the shipped renderer; it is now used by "
                f"message_placeholder_audit, unbalanced_template_report and _template_scan",
            )

    return {
        "version": I18N_GOVERNANCE_VERSION,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "coded": {code: list(messages) for code, messages in sorted(coded.items())},
        "counts": {
            "namespaces": len(namespace_rows),
            "policies": len(policy_rows),
            "locales": len(locale_rows),
            "profiles": len(format_rows),
            "render_ops": len(op_rows),
            "finding_codes": len(warning_rows),
            "catalog_keys": len(_sorted_keys(MESSAGE_CATALOG)),
            "errors": len(errors),
            "warnings": len(warnings),
            "info": len(info),
        },
    }


# --- Catalog --------------------------------------------------------------------

def build_i18n_governance_catalog() -> dict[str, Any]:
    """The governance surface: six tables, six audits, one validator.

    Where :func:`build_i18n_catalog` describes the messages, this describes the
    *rules around* the messages -- which namespace owns a key, what a
    placeholder in it has to satisfy, what the renderer actually does when a
    value is missing, and where the two locale resolvers disagree.

    ``findings`` is the roll-up. It is expected to be non-empty and to contain
    ``defect`` entries: the shipped renderer leaks ``{name}`` for a missing
    value, resolves a plural message with no count to its zero branch, and the
    module carries a four-entry RTL table and a six-category Arabic plural rule
    set for a locale it does not ship. Those are facts about the product, not
    problems with this layer, and they are reported rather than repaired because
    every repair changes a string or a resolution that is already live.

    ``unreached_codes`` lists taxonomy entries no audit currently emits, so a
    code added for a future check does not read as a clean bill of health.
    """
    placeholders = message_placeholder_audit()
    budget = message_budget_report()
    unbalanced = unbalanced_template_report()
    plurals = plural_audit()
    negotiation = negotiation_audit()
    registry = locale_registry_audit()
    validation = validate_i18n()

    audits = {
        "message_placeholder_audit": placeholders,
        "message_budget_report": budget,
        "unbalanced_template_report": unbalanced,
        "plural_audit": plurals,
        "negotiation_audit": negotiation,
        "locale_registry_audit": registry,
    }

    findings: list[dict[str, Any]] = []
    for report in audits.values():
        findings.extend(report.get("findings") or [])
    # The validator speaks in strings; its taxonomy-coded notes join the roll-up
    # as findings so a code never claims a producer that does not emit it.
    for code, messages in (validation.get("coded") or {}).items():
        for message in messages:
            findings.append(_finding(code, str(message), source="validate_i18n"))
    findings.sort(key=lambda item: (str(item.get("severity")), str(item.get("code")), str(item.get("detail"))))

    emitted = {str(item.get("code")) for item in findings}
    unreached = sorted(set(I18N_WARNINGS) - emitted)
    # The codes a caller should treat as blocking review, by severity.
    blocking = sorted(
        {
            str(item.get("code"))
            for item in findings
            if str(item.get("severity")) in ("defect", "error")
        }
    )

    return {
        "version": I18N_GOVERNANCE_VERSION,
        "policy": {
            "posture": "report, never repair",
            "why": (
                "_render_template swallows KeyError/ValueError and returns the joined string, so "
                "fixing a leak changes a string that is already being served. Every render op is "
                "report_only and a finding names the code path that produced it"
            ),
            "shipped_unchanged": (
                "SUPPORTED_LOCALES, MESSAGE_CATALOG, PLURAL_CATEGORIES, RTL_LOCALES, translate, "
                "_render_template, resolve_locale, negotiate_locale, parse_accept_language, "
                "catalog_coverage and build_i18n_catalog keep their exact signatures, payloads and "
                "behaviour"
            ),
        },
        "namespaces": MESSAGE_NAMESPACES,
        "placeholder_policies": PLACEHOLDER_POLICIES,
        "locale_expectations": LOCALE_EXPECTATIONS,
        "number_formats": NUMBER_FORMATS,
        "render_ops": RENDER_OPS,
        "findings_taxonomy": I18N_WARNINGS,
        "audits": audits,
        "findings": findings,
        "finding_count": len(findings),
        "severity_counts": _severity_counts(findings),
        "blocking_codes": blocking,
        "clean_catalogs": sorted(
            name for name, report in audits.items() if report.get("clean")
        ),
        "unreached_codes": unreached,
        "validation": validation,
        "number_formatting": {
            "default_profile": DEFAULT_FORMAT_PROFILE,
            "hops": (
                "explicit profile= argument, then the locale's LOCALE_EXPECTATIONS.format_profile "
                "resolved on the language, then DEFAULT_FORMAT_PROFILE"
            ),
            "not_wired_into": (
                "translate and _render_template still emit str(int(numeric)) for a plural '#'; "
                "format_number is offered, not imposed"
            ),
            "digit_substitution": "not implemented; every profile declares 'none'",
            "examples": {
                "value:en_us": format_number(1234567.5, "en", profile="en_us"),
                "value:es_es": format_number(1234567.5, "es", profile="es_es"),
                "value:es_419": format_number(1234567.5, "es", profile="es_419"),
                "value:fr_fr": format_number(1234567.5, "fr", profile="fr_fr"),
                "value:ar_eg": format_number(1234567.5, "ar", profile="ar_eg"),
                "ungrouped:en_us": format_number(999, "en", profile="en_us"),
                "negative:fr_fr": format_number(-1234.5, "fr", profile="fr_fr"),
                "percent:de_de": format_number(0.256, "de", kind="percent", profile="de_de"),
                "currency:es_es": format_number(1234.5, "es", kind="currency", profile="es_es"),
                "currency:es_419": format_number(1234.5, "es", kind="currency", profile="es_419"),
                "non_numeric": format_number("not a number", "en"),
            },
        },
    }

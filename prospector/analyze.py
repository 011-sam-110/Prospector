"""Optional LLM synthesis — turn evidence clusters into one-paragraph theses.

This is the *only* place a language model touches prospector, and it is held to
the same evidence-bound contract as the renderer: the model is given nothing but
the verbatim quotes the engine actually fetched and is explicitly forbidden from
introducing any fact, number, product name, or claim not present in those quotes.
If no OpenAI-compatible endpoint is configured the whole thing degrades to ``{}``
and the report falls back to stats-only clusters — there is never a hard LLM
dependency.

Endpoint resolution (first match wins):
  * base url  — ``FREELLMAPI_BASE_URL`` or ``OPENAI_BASE_URL``
  * api key   — ``FREELLMAPI_KEY`` or ``OPENAI_API_KEY``

The gateway (e.g. ``freellmapi.co``) is OpenAI-compatible, so we prefer the
``openai`` package if it is importable and otherwise fall back to a plain
``httpx`` POST to ``{base_url}/chat/completions``. ``model='auto'`` lets the
gateway choose a backend.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from prospector.models import EvidenceItem, Profile


# --------------------------------------------------------------------------- #
# Endpoint discovery                                                          #
# --------------------------------------------------------------------------- #
def _endpoint() -> Optional[tuple[str, str]]:
    """Return ``(base_url, api_key)`` from the environment, or ``None``.

    ``base_url`` has any trailing slash stripped so callers can safely append
    ``/chat/completions``.
    """
    base = os.environ.get("FREELLMAPI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("FREELLMAPI_KEY") or os.environ.get("OPENAI_API_KEY")
    if base and key:
        return base.rstrip("/"), key
    return None


def available() -> bool:
    """True iff an OpenAI-compatible endpoint (base url + key) is configured."""
    return _endpoint() is not None


# --------------------------------------------------------------------------- #
# Prompt construction (evidence-constrained)                                   #
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are a careful product-research analyst. You will be shown a candidate "
    "unmet-need theme and a set of VERBATIM Reddit quotes that are the ONLY "
    "evidence you may use. Write exactly ONE concise paragraph (3-5 sentences) "
    "arguing why this could be an underserved gap.\n"
    "HARD CONSTRAINTS:\n"
    "- Use ONLY the quotes provided. Introduce NO facts, statistics, product or "
    "company names, dates, or claims that are not present in the quotes.\n"
    "- Do not assert clinical, medical, regulatory, or market conclusions.\n"
    "- Frame the conclusion as a HYPOTHESIS TO VALIDATE, not a proven need.\n"
    "- No preamble, no headings, no bullet list — return the paragraph only."
)


def _build_user_prompt(cluster: object, profile: "Profile") -> str:
    """Render the user message: the theme + up to a handful of quotes."""
    label = getattr(cluster, "label", "")
    evidence = list(getattr(cluster, "evidence", []) or [])
    lines = [
        f"Research domain: {getattr(profile, 'description', '') or profile.name}",
        f"Candidate gap theme: {label}",
        "",
        "Evidence (verbatim quotes — your only permitted source):",
    ]
    for ev in evidence[:8]:
        quote = " ".join(str(getattr(ev, "quote", "")).split())
        sub = getattr(ev, "subreddit", "")
        author = getattr(ev, "author", "")
        lines.append(f'- "{quote}" (r/{sub}, u/{author})')
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLM transport (openai package preferred, httpx fallback)                     #
# --------------------------------------------------------------------------- #
def _chat(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    *,
    timeout: float = 30.0,
) -> str:
    """Single chat completion against an OpenAI-compatible endpoint.

    Tries the ``openai`` package first (lazy import); if it is not installed,
    falls back to a direct ``httpx`` POST. Returns the assistant text (stripped).
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    try:  # optional dependency — never required at import time
        import openai  # type: ignore
    except Exception:
        openai = None  # type: ignore

    if openai is not None:
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()

    # httpx fallback (a declared dependency)
    import httpx

    url = f"{base_url}/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def synthesize(
    clusters: list,
    profile: "Profile",
    model: str = "auto",
) -> dict[str, str]:
    """Return ``{cluster.label: one-paragraph thesis}`` for each cluster.

    The model is constrained to the quotes carried on each cluster's
    ``evidence`` list (see ``_SYSTEM``). Degrades gracefully:

    * if no endpoint is configured (``available()`` is False) → ``{}``;
    * if an individual cluster's call fails → that cluster is skipped, the rest
      still synthesize.

    The caller (``report.render_report``) still enforces ``evidence_ok`` and only
    ever cites the evidence objects the engine fetched — this function adds
    narrative, never new links or facts.
    """
    if not available():
        return {}
    creds = _endpoint()
    if creds is None:  # pragma: no cover - defensive; available() already checked
        return {}
    base_url, api_key = creds

    out: dict[str, str] = {}
    for cluster in clusters:
        label = getattr(cluster, "label", None)
        if not label:
            continue
        try:
            user = _build_user_prompt(cluster, profile)
            thesis = _chat(base_url, api_key, model, _SYSTEM, user)
        except Exception:
            # Never let a flaky/empty LLM response break the report.
            continue
        if thesis:
            out[label] = thesis
    return out

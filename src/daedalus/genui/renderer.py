"""The renderer: structure in, one self-contained page out (SPEC §3.6, §7.4).

```
spec (structure, from the model)  ─┐
local values (never in a prompt)  ─┼─▶  bind  ─▶  validate  ─▶  tokens  ─▶  HTML
the mood token set (owner's)      ─┘
```

Three properties, and each is enforced rather than intended:

**Values are bound locally, and the model never saw them.** The model composes
*keys* — `{"bind": "disk.free"}` — and this is where a key becomes the owner's
data. That is what makes "structure travels, values stay local" (§6) structural
rather than a rule someone has to remember: a `local_only` fact reaches the
owner's dashboard because it was never in the spec, not because it was filtered
out of one.

**Everything that renders was validated.** A bound value goes through the *same*
door as a literal one, so the guarantee is not "the model's spec was checked" —
it is "what actually rendered was checked", which is the one that matters when
the value came from a calendar.

**The page fetches nothing.** No scripts, no external stylesheets, no fonts, no
images: one document with its stylesheet inline. A `Content-Security-Policy` says
so too — `default-src 'none'` — so the promise is kept by the browser, not only
by the test that asserts it. A dashboard the owner opens on a phone is the
owner's own data in a file, not a page that phones home.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

from .components import RENDERERS, VOCABULARY, render
from .spec import Spec
from .theming import Tokens, resolve, sheet

__all__ = ["CSP", "FORM_CSP", "render_fragment", "render_page"]

#: Anything not listed is refused by the browser. `style-src` is inline because
#: the stylesheet *is* the document; there is no script source, deliberately.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'"
)

#: The policy a page that carries **controls** is served under. The one difference
#: from :data:`CSP` is `form-action`: a *generative* page — composed by the model
#: — may not act at all, and `'none'` is what makes a model-authored form a
#: dead button. The dashboard's strip is built by code, not by the model, and its
#: forms POST to this same server and then go through the approval split — so
#: `'self'` is as far as it goes, and there is still no script source.
FORM_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'self'"
)

_DOCTYPE = "<!doctype html>"
_FALLBACK_TITLE = "Daedalus"


def render_page(
    spec: Spec,
    *,
    values: Mapping[str, Any] | None = None,
    mood: str = "",
    override: str = "",
    vocabulary: Mapping[str, Any] | None = None,
    appendix: str = "",
    extra_css: str = "",
    csp: str = CSP,
    overrides: Mapping[str, str] | None = None,
    tokens: "Tokens | None" = None,
) -> str:
    """Bind, validate, theme and render — into one self-contained document.

    `mood` is what the caller *inferred* from local signals; `override` is the
    owner's one word, and it wins. `overrides` are the theme values the owner has
    **adopted** (T9) — they sit on top of whichever mood is chosen. `tokens`
    replaces resolution outright: that is what a draft preview is, the page
    rendered with a *pending* set. `appendix` is markup the model did not
    compose — the control strip, which is deliberately outside the spec.
    """
    bound = spec.bind(values or {}, vocabulary=vocabulary or VOCABULARY)
    # The model may propose a mood — it is meant to be generative in *styling
    # choices* — but a mood from local signals outranks it, and the owner's one
    # word outranks both. `tokens` given outright is a draft preview: the page
    # rendered with a pending set, and nothing resolved.
    if tokens is None:
        tokens = resolve(
            inferred=mood or bound.mood, override=override, overrides=overrides or {}
        )

    title = bound.title or _FALLBACK_TITLE
    body = render_fragment(bound)
    heading = (
        f'<h1 class="dashboard__title">{escape(bound.title)}</h1>\n' if bound.title else ""
    )
    tail = f"\n{appendix}\n" if appendix else "\n"
    styles = f"{sheet(tokens)}\n{extra_css}" if extra_css else sheet(tokens)
    return (
        f"{_DOCTYPE}\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(csp, quote=True)}">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>\n{styles}\n</style>\n"
        "</head>\n"
        "<body>\n"
        '<main class="dashboard">\n'
        f"{heading}"
        f"{body}"
        f"{tail}"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def render_fragment(spec: Spec) -> str:
    """The dashboard's blocks, with no document around them.

    Kept separate because a fragment is what a *draft* preview needs: the same
    rendering, dropped into a page that is already open.
    """
    missing = [block.component for block in spec.blocks if block.component not in RENDERERS]
    if missing:  # pragma: no cover — the door refuses unknown components first
        raise ValueError(f"no renderer for {', '.join(sorted(set(missing)))}")
    return "\n".join(render(block) for block in spec.blocks)

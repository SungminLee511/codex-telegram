"""How hard the model should reason, and what to call each level.

Codex takes this as ``-c model_reasoning_effort=<level>``. Only these words
are accepted, and anything else is refused at the command rather than at the
next message.

Leaving it unset is a real choice and the default one: the model's own default
effort applies, which is right for most turns.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Tuple


class EffortChoice(NamedTuple):
    alias: str  # what you type, and what the CLI is given
    note: str


# Ordered cheapest to most thorough, which is how the list reads best.
CHOICES: Tuple[EffortChoice, ...] = (
    EffortChoice("minimal", "almost no reasoning — fastest"),
    EffortChoice("low", "light reasoning — fast, cheap"),
    EffortChoice("medium", "moderate reasoning"),
    EffortChoice("high", "reasons hard"),
    EffortChoice("xhigh", "reasons very hard — slowest"),
)

# Typing `/effort auto` is how you undo a choice and hand the decision back.
AUTO = "auto"

_BY_ALIAS = {c.alias: c for c in CHOICES}


def resolve(text: str) -> Optional[EffortChoice]:
    """Find a level by name. None if it is not one of the five."""
    return _BY_ALIAS.get((text or "").strip().lower())


def is_auto(text: str) -> bool:
    """Whether this asks to clear the setting rather than set one."""
    return (text or "").strip().lower() in (AUTO, "default", "none", "off")


def label_for(effort: Optional[str]) -> str:
    """What the banner shows; an unset effort reads `auto` rather than vanishing."""
    if not effort:
        return AUTO
    return effort if effort in _BY_ALIAS else str(effort)

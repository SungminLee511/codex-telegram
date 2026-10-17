"""The models a chat may switch to, and what to call them.

Four buttons — the GPT-6 family (astra, sol, luna) plus gpt-5.6-terra — each
an exact model id passed to Codex as ``-m <id>``.

Any other model is chosen by typing ``/model <name>``. There is no live probe
(the bot never spends a request just to check a name); a typed name only has
to look like a model id, and the reply says it is unverified so a bad one is
recognised when the next message errors.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional


class ModelChoice(NamedTuple):
    alias: str  # what you type, and what the CLI is given
    note: str


CHOICES = (
    ModelChoice("gpt-6-astra", "GPT-6 Astra — largest, long coding jobs"),
    ModelChoice("gpt-6-sol", "GPT-6 Sol — capable, balanced"),
    ModelChoice("gpt-6-luna", "GPT-6 Luna — fast, cheap"),
    ModelChoice("gpt-5.6-terra", "GPT-5.6 Terra — small"),
)

_BY_ALIAS = {c.alias: c for c in CHOICES}

# Letters, digits and the punctuation real model ids use (``gpt-5.6-terra``,
# ``o4-mini``, ``org/model:tag``). Anything else never reaches the CLI.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]:/]{0,79}$")


def resolve(text: str) -> Optional[ModelChoice]:
    """The button model matching this id, or None."""
    return _BY_ALIAS.get((text or "").strip().lower())


def is_wellformed(text: str) -> bool:
    """Whether this could be a model name at all."""
    return bool(_NAME.match(text or ""))


def label_for(model_id: Optional[str]) -> Optional[str]:
    """Short display name for a model id, for the banner.

    A trailing snapshot date (``-2026-09-22`` or ``-20260922``) is dropped:
    noise to a human, and it keeps a dated id from widening the banner.
    """
    if not model_id:
        return None
    trimmed = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", model_id)
    return trimmed or model_id

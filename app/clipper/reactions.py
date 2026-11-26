"""Reaction moods: what the AI may call a comedic beat, and what a meme and its sound look like for each.

A meme belongs on a moment a viewer would react to, and the edit pass marks those by meaning
(a punchline, a fail, a shocking reveal), never by rhythm. Each mark carries one mood from
this list; the mood picks the reaction GIF search and the sting that plays with it, so a
laughing GIF never arrives with a sad violin.
"""
from __future__ import annotations

SHOCK, LAUGH, FAIL, AWKWARD, HYPE, MINDBLOWN = "shock", "laugh", "fail", "awkward", "hype", "mindblown"

# English and Hindi GIF searches per mood, rotated across clips by the meme history.
QUERIES: dict[str, tuple[str, ...]] = {
    SHOCK: ("shocked reaction", "jaw drop reaction", "surprised reaction"),
    LAUGH: ("laughing reaction", "lol reaction", "cant stop laughing"),
    FAIL: ("fail reaction", "facepalm reaction", "disappointed reaction"),
    AWKWARD: ("awkward reaction", "confused reaction", "side eye reaction"),
    HYPE: ("lets go reaction", "hype reaction", "celebration reaction"),
    MINDBLOWN: ("mind blown reaction", "amazed reaction", "wow reaction"),
}
HI_QUERIES: dict[str, tuple[str, ...]] = {
    SHOCK: ("bollywood shocked reaction", "indian shocked reaction"),
    LAUGH: ("bollywood funny reaction", "indian laughing reaction"),
    FAIL: ("bollywood facepalm", "indian fail reaction"),
    AWKWARD: ("bollywood confused reaction", "indian awkward reaction"),
    HYPE: ("bollywood celebration", "indian hype reaction"),
    MINDBLOWN: ("bollywood amazed reaction", "indian mind blown"),
}
# Words in an SFX pack file's name that suit the mood, best first. Matched against the stem,
# so the owner's pack (vine-boom-sound.mp3, bruh.mp3, ...) and MyInstants downloads both fit.
STINGS: dict[str, tuple[str, ...]] = {
    SHOCK: ("vine-boom", "dun-dun", "shocked", "gasp", "are-baap-re", "suspense"),
    LAUGH: ("haha", "laugh", "funny"),
    FAIL: ("bruh", "faaah", "emotional-damage", "cartoon-slip", "metal-pipe", "fail"),
    AWKWARD: ("aayein", "record-scratch", "kuch-to-gadbad", "confused", "cricket"),
    HYPE: ("anime-wow", "wow", "legendary", "rizz", "alag-hi-level"),
    MINDBLOWN: ("anime-wow", "wow", "vine-boom", "impressed"),
}
MOODS: tuple[str, ...] = tuple(QUERIES)


def normalise(raw: object) -> str:
    """A known mood, or "" for anything else (the reaction is then dropped)."""
    mood = str(raw or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    return mood if mood in QUERIES else ""


def queries(mood: str, language: str = "") -> tuple[str, ...]:
    hindi = str(language or "").strip().lower().startswith("hi")
    own = (HI_QUERIES if hindi else QUERIES).get(mood, ())
    return own + (QUERIES.get(mood, ()) if hindi else ())

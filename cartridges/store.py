# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2026 PandaAkiraNakai

"""Store pages for games: description, screenshots and metadata from Steam."""

import hashlib
import itertools
import json
import logging
import re
import time
from html.parser import HTMLParser
from json import JSONDecodeError
from pathlib import Path
from typing import Any, NamedTuple, Self, override
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gi.repository import Gdk, GLib

CACHE_DIR = Path(GLib.get_user_cache_dir(), "cartridges", "store")
_IMAGES_DIR = CACHE_DIR / "images"

_API = "https://store.steampowered.com/api"
_PERCENTAGES_API = (
    "https://api.steampowered.com/ISteamUserStats"
    "/GetGlobalAchievementPercentagesForApp/v0002/?gameid={}"
)
_STORE_URL = "https://store.steampowered.com/app/{}"
_TIMEOUT = 10
_MAX_AGE = 30 * 24 * 60 * 60
_MISS_MAX_AGE = 7 * 24 * 60 * 60
_CACHE_VERSION = 1

# https://partner.steamgames.com/doc/store/localization/languages
_LANGUAGES = {
    "bg": "bulgarian",
    "cs": "czech",
    "da": "danish",
    "de": "german",
    "el": "greek",
    "es": "spanish",
    "fi": "finnish",
    "fr": "french",
    "hu": "hungarian",
    "id": "indonesian",
    "it": "italian",
    "ja": "japanese",
    "ko": "koreana",
    "nb": "norwegian",
    "nl": "dutch",
    "pl": "polish",
    "pt": "portuguese",
    "pt_BR": "brazilian",
    "ro": "romanian",
    "ru": "russian",
    "sv": "swedish",
    "th": "thai",
    "tr": "turkish",
    "uk": "ukrainian",
    "vi": "vietnamese",
    "zh_CN": "schinese",
    "zh_TW": "tchinese",
}

_logger = logging.getLogger(__name__)


class Screenshot(NamedTuple):
    """A screenshot in two sizes."""

    thumbnail: str
    full: str


class StoreInfo(NamedTuple):
    """The store page of a game."""

    appid: int
    short_description: str
    about: str
    genres: tuple[str, ...]
    release_date: str
    developers: tuple[str, ...]
    publishers: tuple[str, ...]
    metacritic: int | None
    screenshots: tuple[Screenshot, ...]

    @property
    def url(self) -> str:
        """The URL of the store page."""
        return _STORE_URL.format(self.appid)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Self:
        """Create store info from a Steam `appdetails` response."""
        return cls(
            appid=int(data["steam_appid"]),
            short_description=_to_markup(data.get("short_description") or ""),
            about=_to_markup(data.get("about_the_game") or ""),
            genres=tuple(g["description"] for g in data.get("genres") or ()),
            release_date=(data.get("release_date") or {}).get("date") or "",
            developers=tuple(data.get("developers") or ()),
            publishers=tuple(data.get("publishers") or ()),
            metacritic=(data.get("metacritic") or {}).get("score"),
            screenshots=tuple(
                Screenshot(s["path_thumbnail"], s["path_full"])
                for s in data.get("screenshots") or ()
            ),
        )

    @classmethod
    def from_cache(cls, data: dict[str, Any]) -> Self:
        """Create store info from data saved by `to_cache`."""
        data = dict(data)
        data["screenshots"] = tuple(Screenshot(*s) for s in data["screenshots"])
        for key in "genres", "developers", "publishers":
            data[key] = tuple(data[key])
        return cls(**data)

    def to_cache(self) -> dict[str, Any]:
        """Data that can be saved as JSON."""
        return self._asdict()


def fetch(game_id: str, source: str, name: str) -> StoreInfo | None:
    """Get the store page of a game, from the cache if it is recent enough.

    This blocks, so it should be run in a thread.
    """
    path = CACHE_DIR / f"{game_id}.json"
    language, country = _locale()

    try:
        with path.open(encoding="utf-8") as f:
            cached = json.load(f)
        if (
            cached["version"] == _CACHE_VERSION
            and cached["name"] == name
            and cached["language"] == language
            and time.time() - cached["fetched"]
            < (_MAX_AGE if cached["info"] else _MISS_MAX_AGE)
        ):
            return StoreInfo.from_cache(cached["info"]) if cached["info"] else None
    except (OSError, JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
        pass

    try:
        info = _fetch(source, game_id, name, language, country)
    except (URLError, OSError, JSONDecodeError, KeyError, TypeError, ValueError):
        # Network or API errors are not cached, so the next visit tries again
        _logger.debug("Couldn't get the store page of %s", name, exc_info=True)
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "version": _CACHE_VERSION,
                "name": name,
                "language": language,
                "fetched": int(time.time()),
                "info": info.to_cache() if info else None,
            },
            f,
        )

    return info


def language() -> str:
    """The Steam language for the user's locale."""
    return _locale()[0]


def fetch_achievement_percentages(appid: str) -> dict[str, float]:
    """Get the percentage of players who unlocked each achievement of `appid`.

    This blocks, so it should be run in a thread.
    """
    path = CACHE_DIR / f"achievements_{appid}.json"
    try:
        if time.time() - path.stat().st_mtime < _MISS_MAX_AGE:
            with path.open(encoding="utf-8") as f:
                return {str(k): float(v) for k, v in json.load(f).items()}
    except (OSError, JSONDecodeError, UnicodeDecodeError, AttributeError, ValueError):
        pass

    try:
        with urlopen(_request(_PERCENTAGES_API.format(appid)), timeout=_TIMEOUT) as r:
            data = json.load(r)
        percentages = {
            str(a["name"]): float(a["percent"])
            for a in data["achievementpercentages"]["achievements"]
        }
    except (URLError, OSError, JSONDecodeError, KeyError, TypeError, ValueError):
        return {}

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(percentages, f)

    return percentages


def load_image(url: str) -> Gdk.Texture | None:
    """Load the image at `url`, from the cache if possible.

    This blocks, so it should be run in a thread.
    """
    path = _IMAGES_DIR / hashlib.sha256(url.split("?")[0].encode()).hexdigest()

    try:
        contents = path.read_bytes()
    except OSError:
        try:
            with urlopen(_request(url), timeout=_TIMEOUT) as response:
                contents = response.read()
        except (URLError, OSError):
            return None

        _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(contents))
    except GLib.Error:
        path.unlink(missing_ok=True)
        return None


def _fetch(
    source: str, game_id: str, name: str, language: str, country: str
) -> StoreInfo | None:
    if source == "steam":
        appid = game_id.removeprefix("steam_")
    elif not (appid := _search(name, language, country)):
        return None

    response = _get_json("appdetails", appids=appid, l=language, cc=country)
    result = response.get(str(appid)) or {}
    if not result.get("success") or (data := result.get("data", {})).get(
        "type"
    ) not in {"game", "demo", "mod", "dlc"}:
        return None

    return StoreInfo.from_data(data)


def _search(name: str, language: str, country: str) -> str | None:
    wanted = _normalize(name)
    if not wanted:
        return None

    # The search doesn't find some names with punctuation, like "WorldBox - God
    # Simulator", so also try with only the part before it. Names are compared in
    # English first, as the localized ones often differ from the ones in launchers.
    terms = dict.fromkeys((name, re.split(r"\s[-:–—]\s?|:\s", name, maxsplit=1)[0]))
    for term, lang in itertools.product(terms, dict.fromkeys(("english", language))):
        response = _get_json("storesearch/", term=term, l=lang, cc=country)
        for item in response.get("items") or ():
            if item.get("type") == "app" and _normalize(item.get("name", "")) == wanted:
                return str(item["id"])

    return None


def _get_json(endpoint: str, **params: str) -> dict[str, Any]:
    url = f"{_API}/{endpoint}?{urlencode(params)}"
    with urlopen(_request(url), timeout=_TIMEOUT) as response:
        data = json.load(response)

    if not isinstance(data, dict):
        raise TypeError

    return data


def _request(url: str) -> Request:
    return Request(url, headers={"User-Agent": "Cartridges"})  # noqa: S310


def _normalize(name: str) -> str:
    return re.sub(r"[\W_]+", "", re.sub(r"[™®©]", "", name).casefold())


def _locale() -> tuple[str, str]:
    """Get the Steam language and the country code for the user's locale."""
    for locale in GLib.get_language_names():
        code = locale.split(".")[0].split("@")[0]
        lang, _, country = code.partition("_")

        if lang == "es" and country and country != "ES":
            return "latam", country
        if steam := _LANGUAGES.get(code) or _LANGUAGES.get(lang):
            return steam, country or "US"

    return "english", "US"


class _MarkupParser(HTMLParser):
    """Convert Steam's description HTML into Pango markup."""

    _STYLES = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u"}
    _HEADINGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))
    _BLOCKS = frozenset(("p", "div", "ul", "ol"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open: list[str] = []
        self.list_items = 0

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.list_items += 1
            self.parts.append("\n• ")
        elif tag in self._BLOCKS and not self.list_items:
            self.parts.append("\n\n")
        elif tag in self._HEADINGS:
            self.parts.append("\n\n<b>")
            self.open.append("b")
        elif style := self._STYLES.get(tag):
            self.parts.append(f"<{style}>")
            self.open.append(style)

    @override
    def handle_endtag(self, tag: str):
        if tag == "li":
            self.list_items = max(0, self.list_items - 1)
        elif tag in self._BLOCKS and not self.list_items:
            self.parts.append("\n\n")
        elif (
            style := "b" if tag in self._HEADINGS else self._STYLES.get(tag)
        ) and style in self.open:
            # Close everything opened after this tag too, so the markup stays valid
            while self.open:
                closed = self.open.pop()
                self.parts.append(f"</{closed}>")
                if closed == style:
                    break
            if tag in self._HEADINGS:
                self.parts.append("\n")

    @override
    def handle_data(self, data: str):
        self.parts.append(GLib.markup_escape_text(re.sub(r"\s+", " ", data)))

    def result(self) -> str:
        self.close()
        self.parts.extend(f"</{tag}>" for tag in reversed(self.open))
        text = "".join(self.parts)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"(?<=• )\s+", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Remove styles that ended up wrapping only whitespace
        text = re.sub(r"<([biu])>(\s*)</\1>", r"\2", text)
        return text.strip()


def _to_markup(html: str) -> str:
    parser = _MarkupParser()
    parser.feed(html)
    return parser.result()

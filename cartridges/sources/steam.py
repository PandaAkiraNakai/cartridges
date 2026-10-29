# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2023 Geoffrey Coulaud
# SPDX-FileCopyrightText: Copyright 2022-2026 kramo

import itertools
import logging
import re
import struct
from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import suppress
from gettext import gettext as _
from os import SEEK_CUR
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple, Self, cast

from gi.repository import Gdk

from cartridges import cover
from cartridges.games import Game

from . import APPLICATION_SUPPORT, DATA, FLATPAK, OPEN, PROGRAM_FILES_X86

ID, NAME = "steam", _("Steam")

_DATA_PATHS = (
    Path.home() / ".steam" / "steam",
    DATA / "Steam",
    FLATPAK / "com.valvesoftware.Steam" / "data" / "Steam",
    PROGRAM_FILES_X86 / "Steam",
    APPLICATION_SUPPORT / "Steam",
)

_MANIFEST_INSTALLED_MASK = 4
_RELEVANT_TYPES = "game", "demo", "mod"
_CAPSULE_NAMES = "library_600x900.jpg", "library_capsule.jpg", "capsule_231x87.jpg"
_CAPSULE_KEYS = ("library_assets_full", "library_capsule", "image"), ("small_capsule",)
_APPINFO_MAGIC = b")DV\x07"
_ACHIEVEMENT_ICON = (
    "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{}/{}"
)
_VDF_TYPES = {
    b"\x00": lambda fp, table: dict(_load_binary_vdf(fp, table)),
    b"\x01": lambda fp, _: _read_string(fp),
    b"\x02": lambda fp, _: struct.unpack("<i", fp.read(4))[0],
    b"\x03": lambda fp, _: struct.unpack("<f", fp.read(4))[0],
    b"\x04": lambda fp, _: struct.unpack("<i", fp.read(4))[0],
    b"\x05": lambda fp, _: _read_string(fp, wide=True),
    b"\x06": lambda fp, _: struct.unpack("<i", fp.read(4))[0],
    b"\x07": lambda fp, _: struct.unpack("<Q", fp.read(8))[0],
    b"\x0a": lambda fp, _: struct.unpack("<q", fp.read(8))[0],
}

_logger = logging.getLogger(__name__)


class _App(NamedTuple):
    name: str
    appid: str
    stateflags: str | None = None
    lastplayed: str | None = None

    @classmethod
    def from_manifest(cls, path: Path) -> Self:
        data = path.read_text("utf-8", "replace")
        try:
            return cls(
                *(
                    m.group(1)
                    if (m := re.search(rf'"{field}"\s+"(.*?)"\n', data, re.IGNORECASE))
                    else cls._field_defaults[field]
                    for field in cls._fields
                )
            )
        except KeyError as e:
            raise ValueError from e


class Achievement(NamedTuple):
    """A Steam achievement and whether the user unlocked it."""

    name: str
    display_name: str
    description: str
    hidden: bool
    icon: str
    icon_gray: str
    unlocked: int | None


class Playtime(NamedTuple):
    """Minutes the user played a Steam game."""

    total: int
    last_two_weeks: int


class _AppInfo(NamedTuple):
    type: str | None = None
    developer: str | None = None
    capsule: str | None = None

    @classmethod
    def from_vdf(cls, fp: BinaryIO, key_table: Sequence[str]) -> Self:
        try:
            common = dict(_load_binary_vdf(fp, key_table))["appinfo"]["common"]
        except (SyntaxError, TypeError, KeyError):
            return cls()

        try:
            developer = ", ".join(
                association["name"]
                for association in common["associations"].values()
                if association.get("type") == "developer"
            )
        except (TypeError, AttributeError, KeyError):
            developer = None

        capsule = None
        for keys in _CAPSULE_KEYS:
            value = common
            with suppress(AttributeError, KeyError):
                for key in keys:
                    value = value.get(key)

                capsule = value.get("english", value.popitem()[1])
                break

        return cls(common.get("type"), developer, capsule)


def get_games() -> Generator[Game]:
    """Installed Steam games."""
    librarycache = _data_dir() / "appcache" / "librarycache"
    with (_data_dir() / "appcache" / "appinfo.vdf").open("rb") as fp:
        appinfo = defaultdict(_AppInfo, _parse_appinfo_vdf(fp))

    appids = set()
    for manifest in _manifests():
        try:
            name, appid, stateflags, lastplayed = _App.from_manifest(manifest)
        except ValueError:
            continue

        duplicate = appid in appids
        installed = (
            int(stateflags) & _MANIFEST_INSTALLED_MASK
            if stateflags and stateflags.isdigit()
            else True
        )

        if duplicate or not installed:
            continue

        type_, developer, capsule = appinfo[appid]
        if type_ and (type_.lower() not in _RELEVANT_TYPES):
            continue

        appids.add(appid)
        yield Game(
            executable=f"{OPEN} steam://rungameid/{appid}",
            game_id=f"{ID}_{appid}",
            source=ID,
            last_played=int(lastplayed) if lastplayed and lastplayed.isdigit() else 0,
            name=name,
            developer=developer,
            cover=_find_cover(librarycache / appid, capsule),
        )


def get_playtime(appid: str) -> Playtime | None:
    """Get how long the user played the game with `appid`, from Steam's local data."""
    try:
        users = list((_data_dir() / "userdata").iterdir())
    except OSError:
        return None

    best = None
    for user in users:
        try:
            config = _load_text_vdf(
                (user / "config" / "localconfig.vdf").read_text("utf-8", "replace")
            )
            app = _get_path(
                config, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid
            )
            playtime = Playtime(
                int(app.get("Playtime", 0)), int(app.get("Playtime2wks", 0))
            )
        except (OSError, SyntaxError, KeyError, AttributeError, ValueError):
            continue

        if not best or playtime.total > best.total:
            best = playtime

    return best


def get_achievements(appid: str, languages: Sequence[str]) -> list[Achievement]:
    """Get the achievements of `appid`, from Steam's local data.

    Names and descriptions are in the first of `languages` available.
    """
    try:
        stats = _data_dir() / "appcache" / "stats"
        with (stats / f"UserGameStatsSchema_{appid}.bin").open("rb") as fp:
            schema = dict(_load_inline_binary_vdf(fp))[appid]["stats"]
    except (OSError, SyntaxError, KeyError, TypeError, struct.error):
        return []

    # Use the most recently updated stats if several users played the game
    user_stats: dict[str, Any] = {}
    with suppress(ValueError, OSError, SyntaxError, KeyError, TypeError, struct.error):
        path = max(
            stats.glob(f"UserGameStats_*_{appid}.bin"), key=lambda p: p.stat().st_mtime
        )
        with path.open("rb") as fp:
            user_stats = dict(_load_inline_binary_vdf(fp))["cache"]

    achievements = []
    for stat_id, stat in schema.items():
        if not isinstance(stat, dict) or not isinstance(bits := stat.get("bits"), dict):
            continue

        progress = user_stats.get(stat_id)
        data = progress.get("data", 0) if isinstance(progress, dict) else 0
        times = progress.get("AchievementTimes", {}) if isinstance(progress, dict) else {}

        for bit, info in bits.items():
            try:
                display = info["display"]
                unlocked = (data & 0xFFFFFFFF) >> int(bit) & 1
                achievements.append(
                    Achievement(
                        name=str(info["name"]),
                        display_name=_localized(display.get("name"), languages),
                        description=_localized(display.get("desc"), languages),
                        hidden=bool(int(display.get("hidden", 0))),
                        icon=_ACHIEVEMENT_ICON.format(appid, display.get("icon", "")),
                        icon_gray=_ACHIEVEMENT_ICON.format(
                            appid, display.get("icon_gray", "")
                        ),
                        unlocked=(int(times.get(bit, 0)) or None) if unlocked else None,
                    )
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue

    return achievements


def _localized(strings: Any, languages: Sequence[str]) -> str:
    if not isinstance(strings, dict):
        return str(strings or "")

    lowered = {k.lower(): v for k, v in strings.items()}
    for language in (*languages, "english"):
        if value := lowered.get(language.lower()):
            return str(value)

    return str(next(iter(lowered.values()), ""))


def _get_path(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Get a nested value from a VDF, ignoring the case of keys like Steam does."""
    for key in keys:
        try:
            data = next(v for k, v in data.items() if k.lower() == key.lower())
        except StopIteration as e:
            raise KeyError(key) from e
    return data


def _load_text_vdf(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack = [root]
    key = None

    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        string, brace = match.groups()
        if brace == "{":
            if key is None:
                raise SyntaxError
            stack[-1][key] = child = {}
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) == 1:
                raise SyntaxError
            stack.pop()
        elif key is None:
            key = string
        else:
            stack[-1][key] = string
            key = None

    return root


def _load_inline_binary_vdf(fp: BinaryIO) -> Generator[tuple[str, Any]]:
    """Load a binary VDF with the keys inline, like the ones for user stats."""
    for type_ in iter(lambda: fp.read(1), b"\x08"):
        if not type_:
            raise SyntaxError

        key = _read_string(fp)
        try:
            yield key, (
                dict(_load_inline_binary_vdf(fp))
                if type_ == b"\x00"
                else _VDF_TYPES[type_](fp, ())
            )
        except KeyError as e:
            raise SyntaxError from e


def _data_dir() -> Path:
    for path in _DATA_PATHS:
        if path.is_dir():
            return path

    raise FileNotFoundError


def _library_folders() -> Generator[Path]:
    vdf = _data_dir() / "steamapps" / "libraryfolders.vdf"
    return (
        steamapps
        for folder in re.findall(
            r'"path"\s+"(.*?)"\n',
            vdf.read_text("utf-8", "replace"),
            re.IGNORECASE,
        )
        if (steamapps := Path(folder) / "steamapps").is_dir()
    )


def _manifests() -> Generator[Path]:
    return (
        manifest
        for folder in _library_folders()
        for manifest in folder.glob("appmanifest_*.acf")
        if manifest.is_file()
    )


def _parse_appinfo_vdf(fp: BinaryIO) -> Generator[tuple[str, _AppInfo]]:
    if fp.read(4) != _APPINFO_MAGIC:
        _logger.warning("Magic number mismatch, parsing appinfo.vdf will likely fail.")

    fp.seek(4, SEEK_CUR)
    table_offset = struct.unpack("q", fp.read(8))[0]
    offset = fp.tell()
    fp.seek(table_offset)
    table = tuple(_read_string(fp) for _ in range(struct.unpack("<I", fp.read(4))[0]))

    fp.seek(offset)
    while appid := struct.unpack("<I", fp.read(4))[0]:
        fp.seek(64, SEEK_CUR)
        yield str(appid), _AppInfo.from_vdf(fp, table)


def _load_binary_vdf(
    fp: BinaryIO, key_table: Sequence[str]
) -> Generator[tuple[str, Any]]:
    for type_ in iter(lambda: fp.read(1), b"\x08"):
        try:
            key = key_table[cast(int, struct.unpack("<i", fp.read(4))[0])]
            yield key, _VDF_TYPES[type_](fp, key_table)
        except (IndexError, KeyError) as e:
            raise SyntaxError from e


def _read_string(fp: BinaryIO, *, wide: bool = False) -> str:
    size, encoding = (2, "utf-16") if wide else (1, "utf-8")

    string = b""
    for char in iter(lambda: fp.read(size), b"\x00" * size):
        if char == b"":
            raise SyntaxError

        string += char

    return string.decode(encoding, "replace")


def _find_cover(path: Path, capsule: str | None = None) -> Gdk.Paintable | None:
    paths = [*itertools.chain.from_iterable(path.rglob(p) for p in _CAPSULE_NAMES)]
    if capsule:
        paths.insert(0, path / capsule)

    for p in paths:
        if c := cover.at_path(p):
            return c

    return None

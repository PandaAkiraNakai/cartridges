# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2022-2026 kramo
# SPDX-FileCopyrightText: Copyright 2025 Jamie Gravendeel


import asyncio
import locale
import sys
from datetime import UTC, datetime
from gettext import gettext as _
from typing import Any, cast
from urllib.parse import quote

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from cartridges import store
from cartridges.sources import steam
from cartridges.config import PREFIX
from cartridges.games import Game

from . import closures
from .collections import CollectionActions, CollectionsBox
from .cover import Cover  # noqa: F401
from .games import GameActions, GameEditable


_ACHIEVEMENTS_SHOWN = 6
_SCREENSHOT_SECONDS = 4


@Gtk.Template(resource_path=f"{PREFIX}/game-details.ui")
@closures.add(closures.bool_, closures.format_, closures.if_)
class GameDetails(Adw.NavigationPage):
    """The details of a game."""

    __gtype_name__ = __qualname__

    collections_box: CollectionsBox = Gtk.Template.Child()
    name_entry: Adw.EntryRow = Gtk.Template.Child()

    carousel: Adw.Carousel = Gtk.Template.Child()
    thumbnails_box: Gtk.Box = Gtk.Template.Child()
    short_description_label: Gtk.Label = Gtk.Template.Child()
    genres_box: Adw.WrapBox = Gtk.Template.Child()
    info_grid: Gtk.Grid = Gtk.Template.Child()
    about_label: Gtk.Label = Gtk.Template.Child()
    achievements_label: Gtk.Label = Gtk.Template.Child()
    achievements_progress: Gtk.ProgressBar = Gtk.Template.Child()
    achievements_list: Gtk.ListBox = Gtk.Template.Child()

    game_actions: GameActions = Gtk.Template.Child()
    collection_actions: CollectionActions = Gtk.Template.Child()
    game_editable: GameEditable = Gtk.Template.Child()
    game_signals: GObject.SignalGroup = Gtk.Template.Child()

    game = GObject.Property(type=Game)
    editing = GObject.Property(type=bool, default=False)

    store_loading = GObject.Property(type=bool, default=False)
    store_revealed = GObject.Property(type=bool, default=False)
    has_store_info = GObject.Property(type=bool, default=False)
    has_screenshots = GObject.Property(type=bool, default=False)
    about_expanded = GObject.Property(type=bool, default=False)
    about_long = GObject.Property(type=bool, default=False)

    playtime = GObject.Property(type=str)
    playtime_tooltip = GObject.Property(type=str)
    has_achievements = GObject.Property(type=bool, default=False)
    achievements_expanded = GObject.Property(type=bool, default=False)
    achievements_long = GObject.Property(type=bool, default=False)

    _store_info: store.StoreInfo | None = None
    _store_task: asyncio.Task[None] | None = None
    _steam_task: asyncio.Task[None] | None = None

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)

        group = Gio.SimpleActionGroup()
        group.add_action_entries((
            ("edit", lambda *_: self.edit()),
            ("cancel", lambda *_: self._cancel()),
            ("apply", lambda *_: self._apply()),
            (
                "search-on",
                lambda _action, param, *_: Gio.AppInfo.launch_default_for_uri(
                    param.get_string().format(quote(cast(Game, self.game).name))
                ),
                "s",
            ),
        ))
        self.insert_action_group("details", group)

        self.game_editable.bind_property(
            "valid",
            cast(Gio.SimpleAction, group.lookup_action("apply")),
            "enabled",
            GObject.BindingFlags.SYNC_CREATE,
        )

        self.insert_action_group("game", self.game_actions)
        self.insert_action_group("collection", self.collection_actions)

        for name in "hidden", "removed":
            self.game_signals.connect_closure(
                f"notify::{name}",
                lambda *_: self.activate_action("navigation.pop"),
                after=False,
            )

        self._screenshot_tasks: set[asyncio.Task[None]] = set()
        self._full_screenshots: set[int] = set()
        self._achievement_rows: list[tuple[Adw.ActionRow, Gtk.Image, str]] = []
        self._loaded_icons: set[int] = set()

        self.connect("notify::game", lambda *_: self._load_store_info())
        self.connect("notify::editing", lambda *_: self._update_store_revealed())
        self.connect("notify::about-expanded", lambda *_: self._update_about_lines())
        self.connect(
            "notify::achievements-expanded", lambda *_: self._update_achievement_rows()
        )
        self._update_about_lines()

        self._autoplay_id = 0
        self._hovering_screenshots = False
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._set_hovering_screenshots(True))
        motion.connect("leave", lambda *_: self._set_hovering_screenshots(False))
        cast(Gtk.Widget, self.carousel.props.parent).add_controller(motion)
        self.carousel.connect("map", lambda *_: self._restart_autoplay())
        self.carousel.connect("unmap", lambda *_: self._stop_autoplay())

    def add(self):
        """Add a new game."""
        self.game = None
        self.edit()

    def edit(self):
        """Enter edit mode."""
        self.editing = True
        self.name_entry.grab_focus()

    def _apply(self):
        self.game_editable.apply()
        self.game = self.game_editable.game
        self.editing = False

    @Gtk.Template.Callback()
    def _activate_apply(self, _entry):
        self.activate_action("details.apply")

    @Gtk.Template.Callback()
    def _cancel(self, *_args):
        if not (self.editing and self.game):
            self.activate_action("navigation.pop")
        self.editing = False

    def _load_store_info(self):
        for task in self._store_task, self._steam_task, *self._screenshot_tasks:
            if task:
                task.cancel()
        self._screenshot_tasks.clear()
        self._full_screenshots.clear()
        self._stop_autoplay()

        self._store_info = None
        self.has_store_info = False
        self.has_screenshots = False
        self.store_loading = False
        self.about_expanded = False
        self.short_description_label.props.label = ""
        self.about_label.props.label = ""
        self.genres_box.props.visible = False
        _remove_children(self.info_grid)

        self.playtime = ""
        self.playtime_tooltip = ""
        self.has_achievements = False
        self._achievement_rows.clear()
        self._loaded_icons.clear()
        self.achievements_expanded = False
        _remove_children(self.achievements_list)

        self._update_store_revealed()

        if not (game := cast(Game | None, self.game)):
            return

        app = cast(Gio.Application, Gio.Application.get_default())
        self._store_task = app.create_asyncio_task(self._update_store_info(game))
        if game.source == steam.ID:
            appid = game.game_id.removeprefix(f"{steam.ID}_")
            self._steam_task = app.create_asyncio_task(self._update_steam_stats(appid))

    async def _update_steam_stats(self, appid: str):
        language = store.language()
        languages = (language, "spanish") if language == "latam" else (language,)

        playtime, achievements = await asyncio.gather(
            asyncio.to_thread(steam.get_playtime, appid),
            asyncio.to_thread(steam.get_achievements, appid, languages),
        )

        if playtime and playtime.total:
            self.playtime = _format_minutes(playtime.total)
            if playtime.last_two_weeks:
                self.playtime_tooltip = _("{} in the last two weeks").format(
                    _format_minutes(playtime.last_two_weeks)
                )

        if not achievements:
            return

        percentages = await asyncio.to_thread(store.fetch_achievement_percentages, appid)
        self._build_achievements(achievements, percentages)
        self._update_store_revealed()

    def _build_achievements(
        self, achievements: list[steam.Achievement], percentages: dict[str, float]
    ):
        # Like Steam: unlocked ones first, most recent first, then the locked
        # ones from the most to the least common
        achievements.sort(
            key=lambda a: (
                a.unlocked is None,
                -(a.unlocked or 0),
                -percentages.get(a.name, 0),
            )
        )

        unlocked = sum(1 for a in achievements if a.unlocked is not None)
        total = len(achievements)
        self.achievements_label.props.label = _("{} of {} ({}%)").format(
            unlocked, total, round(unlocked / total * 100)
        )
        self.achievements_progress.props.fraction = unlocked / total

        for achievement in achievements:
            locked = achievement.unlocked is None
            secret = locked and achievement.hidden

            row = Adw.ActionRow(
                title=_("Hidden Achievement") if secret else achievement.display_name,
                subtitle=(
                    _("Details for this achievement will be revealed once unlocked")
                    if secret
                    else achievement.description
                ),
                use_markup=False,
                css_classes=["achievement", "locked"] if locked else ["achievement"],
            )

            icon = Gtk.Image(pixel_size=48, overflow=Gtk.Overflow.HIDDEN)
            icon.add_css_class("achievement-icon")
            row.add_prefix(icon)

            suffix = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            if achievement.unlocked:
                suffix.append(
                    Gtk.Label(
                        label=_("Unlocked {}").format(
                            datetime.fromtimestamp(achievement.unlocked).strftime(
                                "%x"
                            )
                        ),
                        xalign=1,
                        css_classes=["caption"],
                    )
                )
            if (percent := percentages.get(achievement.name)) is not None:
                suffix.append(
                    Gtk.Label(
                        label=_("{}% of players").format(
                            locale.format_string("%.1f", percent)
                        ),
                        xalign=1,
                        css_classes=["caption", "dim-label"],
                    )
                )
            row.add_suffix(suffix)

            self.achievements_list.append(row)
            self._achievement_rows.append(
                (row, icon, achievement.icon_gray if locked else achievement.icon)
            )

        self.achievements_long = total > _ACHIEVEMENTS_SHOWN
        self.has_achievements = True
        self._update_achievement_rows()

    def _update_achievement_rows(self):
        app = cast(Gio.Application, Gio.Application.get_default())
        for index, (row, icon, url) in enumerate(self._achievement_rows):
            row.props.visible = self.achievements_expanded or index < _ACHIEVEMENTS_SHOWN
            # Only download the icons of the achievements that are shown
            if row.props.visible and index not in self._loaded_icons:
                self._loaded_icons.add(index)
                self._spawn(app, self._load_icon(url, icon))

    async def _load_icon(self, url: str, icon: Gtk.Image):
        if texture := await asyncio.to_thread(store.load_image, url):
            icon.props.paintable = texture

    @Gtk.Template.Callback()
    def _toggle_achievements(self, *_args):
        self.achievements_expanded = not self.achievements_expanded

    async def _update_store_info(self, game: Game):
        self.store_loading = True
        try:
            info = await asyncio.to_thread(
                store.fetch, game.game_id, game.source, game.name
            )
        finally:
            self.store_loading = False

        if not info:
            return

        self._store_info = info
        self.has_store_info = True
        self.short_description_label.props.label = info.short_description
        self.about_label.props.label = info.about
        self.about_long = len(info.about) > 800 or info.about.count("\n") > 8

        _remove_children(self.genres_box)
        for genre in info.genres:
            self.genres_box.append(Gtk.Label(label=genre, css_classes=["store-tag"]))
        if info.metacritic:
            self.genres_box.append(
                Gtk.Label(
                    label=_("Metacritic {}").format(info.metacritic),
                    css_classes=["store-tag", "metacritic"],
                )
            )
        self.genres_box.props.visible = bool(self.genres_box.get_first_child())

        _remove_children(self.info_grid)
        rows = (
            (_("Release Date"), info.release_date),
            (_("Developer"), ", ".join(info.developers)),
            (_("Publisher"), ", ".join(info.publishers)),
        )
        for row, (title, value) in enumerate((r for r in rows if r[1])):
            self.info_grid.attach(
                Gtk.Label(label=title, xalign=0, css_classes=["dim-label"]), 0, row, 1, 1
            )
            self.info_grid.attach(
                Gtk.Label(label=value, xalign=0, wrap=True, selectable=True),
                1,
                row,
                1,
                1,
            )

        self._build_screenshots(info)
        self._update_store_revealed()

    def _build_screenshots(self, info: store.StoreInfo):
        while page := self.carousel.get_first_child():
            self.carousel.remove(page)
        _remove_children(self.thumbnails_box)

        self.has_screenshots = bool(info.screenshots)
        app = cast(Gio.Application, Gio.Application.get_default())

        for index, screenshot in enumerate(info.screenshots):
            picture = Gtk.Picture(
                content_fit=Gtk.ContentFit.COVER,
                hexpand=True,
                overflow=Gtk.Overflow.HIDDEN,
                css_classes=["store-screenshot"],
            )
            self.carousel.append(picture)

            thumbnail = Gtk.Picture(
                content_fit=Gtk.ContentFit.COVER,
                width_request=128,
                height_request=72,
                overflow=Gtk.Overflow.HIDDEN,
            )
            button = Gtk.Button(
                child=thumbnail,
                css_classes=["store-thumbnail"],
                tooltip_text=_("Screenshot {}").format(index + 1),
            )
            button.connect("clicked", self._on_thumbnail_clicked, index)
            self.thumbnails_box.append(button)

            self._spawn(app, self._load_thumbnail(screenshot.thumbnail, picture, thumbnail))

        self._screenshot_changed(self.carousel, 0)

    def _spawn(self, app: Gio.Application, coro: Any):
        task = app.create_asyncio_task(coro)
        self._screenshot_tasks.add(task)
        task.add_done_callback(self._screenshot_tasks.discard)

    async def _load_thumbnail(self, url: str, *pictures: Gtk.Picture):
        texture = await asyncio.to_thread(store.load_image, url)
        for picture in pictures:
            # Don't replace a full size screenshot that loaded first
            if not picture.props.paintable:
                picture.props.paintable = texture

    async def _load_full_screenshot(self, url: str, picture: Gtk.Picture):
        if texture := await asyncio.to_thread(store.load_image, url):
            picture.props.paintable = texture

    def _update_about_lines(self):
        self.about_label.props.lines = -1 if self.about_expanded else 8
        self.about_label.props.ellipsize = (
            Pango.EllipsizeMode.NONE if self.about_expanded else Pango.EllipsizeMode.END
        )

    def _update_store_revealed(self):
        self.store_revealed = (
            self.has_store_info or self.has_achievements
        ) and not self.editing

    @Gtk.Template.Callback()
    def _screenshot_changed(self, carousel: Adw.Carousel, index: int):
        if not self._store_info:
            return

        self._restart_autoplay()

        for position, button in enumerate(_children(self.thumbnails_box)):
            if position == index:
                button.add_css_class("selected")
                # Keep the thumbnail of the current screenshot visible
                if isinstance(viewport := self.thumbnails_box.props.parent, Gtk.Viewport):
                    viewport.scroll_to(button, None)
            else:
                button.remove_css_class("selected")

        # Load the full size screenshots around the current one only
        app = cast(Gio.Application, Gio.Application.get_default())
        screenshots = self._store_info.screenshots
        for position in index, index + 1:
            if position >= len(screenshots) or position in self._full_screenshots:
                continue
            self._full_screenshots.add(position)
            picture = cast(Gtk.Picture, carousel.get_nth_page(position))
            self._spawn(app, self._load_full_screenshot(screenshots[position].full, picture))

    def _restart_autoplay(self):
        self._stop_autoplay()
        if self.carousel.props.n_pages > 1 and not self._hovering_screenshots:
            self._autoplay_id = GLib.timeout_add_seconds(
                _SCREENSHOT_SECONDS, self._autoplay
            )

    def _stop_autoplay(self):
        if self._autoplay_id:
            GLib.source_remove(self._autoplay_id)
            self._autoplay_id = 0

    def _autoplay(self) -> bool:
        self._autoplay_id = 0
        # Changing the page restarts the timer
        self._scroll_screenshots(1)
        return GLib.SOURCE_REMOVE

    def _set_hovering_screenshots(self, hovering: bool):
        self._hovering_screenshots = hovering
        self._restart_autoplay()

    def _on_thumbnail_clicked(self, _button, index: int):
        self.carousel.scroll_to(self.carousel.get_nth_page(index), True)

    def _scroll_screenshots(self, delta: int):
        if not (n_pages := self.carousel.props.n_pages):
            return
        index = (round(self.carousel.props.position) + delta) % n_pages
        self.carousel.scroll_to(self.carousel.get_nth_page(index), True)

    @Gtk.Template.Callback()
    def _previous_screenshot(self, *_args):
        self._scroll_screenshots(-1)

    @Gtk.Template.Callback()
    def _next_screenshot(self, *_args):
        self._scroll_screenshots(1)

    @Gtk.Template.Callback()
    def _toggle_about(self, *_args):
        self.about_expanded = not self.about_expanded

    @Gtk.Template.Callback()
    def _open_store(self, *_args):
        if self._store_info:
            Gio.AppInfo.launch_default_for_uri(self._store_info.url)

    @Gtk.Template.Callback()
    def _setup_collections(self, button: Gtk.MenuButton, *_args):
        if button.props.active:
            self.collections_box.build()
        else:
            self.collections_box.finish()

    @Gtk.Template.Callback()
    @staticmethod
    def _downscale(this: Gtk.Widget, cover: Gdk.Paintable | None) -> Gdk.Texture | None:
        if cover and (renderer := cast(Gtk.Native, this.props.root).get_renderer()):
            cover.snapshot(snapshot := Gtk.Snapshot(), 3, 3)
            if node := snapshot.to_node():
                return renderer.render_texture(node)

        return None

    @Gtk.Template.Callback()
    @staticmethod
    def _relative_date(_this, timestamp: int) -> str:
        date = datetime.fromtimestamp(timestamp, UTC)
        now = datetime.now(UTC)
        return (
            _("Never")
            if not timestamp
            else _("Today")
            if (n_days := (now - date).days) == 0
            else _("Yesterday")
            if n_days == 1
            else date.strftime("%A")
            if n_days <= (day_of_week := now.weekday())
            else _("Last Week")
            if n_days <= day_of_week + 7
            else _("This Month")
            if n_days <= (day_of_month := now.day)
            else _("Last Month")
            if n_days <= day_of_month + 30
            else date.strftime("%B")
            if n_days < (day_of_year := now.timetuple().tm_yday)
            else _("Last Year")
            if n_days <= day_of_year + 365
            else date.strftime("%Y")
        )

    @Gtk.Template.Callback()
    @staticmethod
    def _format_more_info(_this, label: str) -> str:
        executable = _("program")
        filename = _("file.txt")
        path = _("/path/to/{}")
        command = "xdg-open"

        if sys.platform.startswith("darwin"):
            command = "open"
        elif sys.platform.startswith("win32"):
            executable += ".exe"
            path = _(r"C:\path\to\{}")
            command = "start"

        return label.format(
            executable,
            path.format(executable),
            filename,
            command,
            path.format(filename),
        )


class AspectBin(Adw.Bin):
    """A bin whose height follows its width, with a 16:9 aspect ratio."""

    __gtype_name__ = __qualname__

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        # AdwBin measures through its layout manager, which ignores the vfuncs
        self.props.layout_manager = None

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(
        self, orientation: Gtk.Orientation, for_size: int
    ) -> tuple[int, int, int, int]:
        if orientation == Gtk.Orientation.VERTICAL:
            height = round(max(for_size, 0) * 9 / 16)
            return height, height, -1, -1

        return 0, 0, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int):
        if child := self.props.child:
            child.allocate(width, height, baseline, None)


def _format_minutes(minutes: int) -> str:
    if minutes < 60:
        return _("{} min").format(minutes)

    return _("{} h").format(locale.format_string("%.1f", minutes / 60))


def _children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    children: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child:
        children.append(child)
        child = child.get_next_sibling()
    return children


def _remove_children(widget: Gtk.Box | Gtk.Grid | Gtk.ListBox | Adw.WrapBox):
    for child in _children(widget):
        widget.remove(child)

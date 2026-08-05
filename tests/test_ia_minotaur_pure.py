"""Tests for the pure, importable helpers in ia_minotaur.py.

We only test functions that can run without a real curses terminal and
without touching the user's media root. conftest.py sets IA_MEDIA_ROOT
to a temp path before this module is imported.
"""
import io
import os
import curses

import ia_minotaur
import ia_paths
from ia_minotaur import (
    RetroWaveIA,
    shaded_progress_bar,
)


# -------------------------------------------------------- env override smoke
def test_media_root_respects_env():
    # conftest sets IA_MEDIA_ROOT=/tmp/ia-test-root before import.
    assert ia_minotaur.MEDIA_ROOT == "/tmp/ia-test-root"
    assert ia_minotaur.STAGING_ROOT == "/tmp/ia-test-root/.ia_staging"
    assert ia_minotaur.BUCKET_TV == "/tmp/ia-test-root/TV"
    assert ia_minotaur.BUCKET_MOVIES == "/tmp/ia-test-root/Movies"
    assert ia_minotaur.LOG_PATH == "/tmp/ia-test-root/.ia_dl.log"
    assert ia_paths.MEDIA_ROOT == "/tmp/ia-test-root"


def test_shaded_progress_bar_uses_fixed_width():
    assert shaded_progress_bar(0, 100, 10) == "░" * 10
    assert len(shaded_progress_bar(50, 100, 10)) == 10
    assert shaded_progress_bar(100, 100, 10) == "█" * 10


def test_shaded_progress_bar_handles_unknown_total():
    assert shaded_progress_bar(42, 0, 6) == "▒" * 6
    assert shaded_progress_bar(42, 100, 0) == ""


def test_is_enter_key_handles_common_terminal_codes():
    assert ia_minotaur.is_enter_key(10) is True
    assert ia_minotaur.is_enter_key(13) is True
    assert ia_minotaur.is_enter_key(curses.KEY_ENTER) is True
    assert ia_minotaur.is_enter_key(ord("a")) is False


def test_is_backspace_key_does_not_treat_delete_as_backspace():
    assert ia_minotaur.is_backspace_key(curses.KEY_BACKSPACE) is True
    assert ia_minotaur.is_backspace_key(127) is True
    assert ia_minotaur.is_backspace_key(curses.KEY_DC) is False


def test_mouse_wheel_direction_handles_ncurses_button_masks(monkeypatch):
    monkeypatch.setattr(curses, "BUTTON4_PRESSED", 0x10000, raising=False)
    monkeypatch.setattr(curses, "BUTTON5_PRESSED", 0x200000, raising=False)

    assert ia_minotaur.mouse_wheel_direction(curses.BUTTON4_PRESSED) == -1
    assert ia_minotaur.mouse_wheel_direction(curses.BUTTON5_PRESSED) == 1
    assert ia_minotaur.mouse_wheel_direction(0) == 0


def test_scroll_index_moves_by_wheel_step_and_clamps():
    assert ia_minotaur.scroll_index(3, 1, 20) == 7
    assert ia_minotaur.scroll_index(3, -1, 20) == 0
    assert ia_minotaur.scroll_index(18, 1, 20) == 19
    assert ia_minotaur.scroll_index(5, 0, 20) == 5


def test_compact_error_text_keeps_simple_errors():
    assert ia_minotaur.compact_error_text("metadata timed out") == "metadata timed out"


def test_compact_error_text_collapses_traceback_to_final_exception():
    detail = "\n".join(
        [
            "File load failed for item1: Traceback (most recent call last):",
            "  File \"/opt/ia/venv/lib/python3.11/site-packages/urllib3/connection.py\", line 571, in getresponse",
            "    httplib_response = super().getresponse()",
            "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='archive.org', port=443): Read timed out.",
        ]
    )

    compact = ia_minotaur.compact_error_text(detail)

    assert compact == (
        "File load failed for item1: requests.exceptions.ReadTimeout: "
        "HTTPSConnectionPool(host='archive.org', port=443): Read timed out."
    )


# ------------------------------------------------------------- TUI state
class TestRetroWaveIAState:
    def test_normalize_save_bucket_accepts_tv(self):
        assert ia_minotaur.normalize_save_bucket("TV", "Movies") == "TV"
        assert ia_minotaur.normalize_save_bucket("tv", "Movies") == "TV"

    def test_init_uses_config_defaults(self, monkeypatch):
        monkeypatch.setattr(
            ia_minotaur,
            "APP_CONFIG",
            {
                "default_bucket": "Music",
                "default_filter": "audio",
                "default_sort": "downloads desc",
                "title_only": True,
                "license_gate": True,
                "no_change_timestamp": False,
                "rows_per_page": 30,
                "media_root": "/tmp/unused",
            },
        )
        monkeypatch.setattr(ia_minotaur, "ia_ok", lambda: (True, "ia 1.0"))
        monkeypatch.setattr(ia_minotaur.RetroWaveIA, "load_favs", lambda _self: ia_minotaur.ia_state.default_favs())

        app = RetroWaveIA(object())

        assert app.last_bucket == "Music"
        assert app.filter == "audio"
        assert app.sort_by == "downloads desc"
        assert app.title_only is True
        assert app.enforce_license_gate is True
        assert app.mode == "RESULTS"

    def test_music_folder_favorites_stay_in_music_bucket(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.favs = {"items": [], "files": [], "folders": {"TV": [], "Movies": [], "Music": [], "Other": []}}
        app.save_favs = lambda: None

        app.add_folder_fav("Music", "Album Name")

        assert app.favs["folders"]["Music"] == ["Album Name"]
        assert app.favs["folders"]["Other"] == []

    def test_favorites_menu_exposes_remove_action(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FAVS"
        app.favs_tab = "ITEMS"

        actions = [action for _label, action in app.get_menu_items()]

        assert "primary" in actions
        assert "remove" in actions

    def test_files_menu_prioritizes_preview_before_keyword(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.results = []
        app.files = []
        app.sel_f = 0
        app.file_kw = ""
        app.video_only = False
        app.last_bucket = "TV"
        app.selected_result = lambda: None
        app.get_visible_files = lambda: []

        actions = [action for _label, action in app.get_menu_items()]

        assert actions.index("preview") < actions.index("keyword")
        assert actions.index("download") < actions.index("keyword")

    def test_files_menu_hides_file_actions_while_opening(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.theme_name = "Retro"
        app._file_load_loading = True

        labels_actions = app.get_menu_items()
        actions = [action for _label, action in labels_actions]

        assert ("Opening...", "noop") in labels_actions
        assert "preview" not in actions
        assert "download" not in actions

    def test_selected_item_header_shows_opening_state(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app._file_load_loading = True
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        assert app.selected_item_header() == "Opening item | Title | item1 | waiting for IA file metadata"

    def test_results_menu_is_trimmed_and_uses_search_tools(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.results = []
        app.sel_r = 0
        app.filter = "movies"
        app.result_filter = ""
        app.sort_by = ""
        app.title_only = False
        app.enforce_license_gate = False
        app.theme_name = "Retro"
        app.selected_result = lambda: None

        actions = [action for _label, action in app.get_menu_items()]

        assert "search_tools" in actions
        assert "collection_search" not in actions
        assert "within_collection" not in actions
        assert "history" not in actions

    def test_keyword_action_returns_focus_to_list(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = []
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 3
        app.focus = "MENU"
        app.prompt_list = lambda _title, _options, default_idx=0: "Set keyword..."
        app.prompt = lambda _label, _default="": "mp4"
        app.save_current_file_view_state = lambda: None

        app.activate_menu_action("keyword")

        assert app.file_kw == "mp4"
        assert app.sel_f == 0
        assert app.focus == "LIST"

    def test_keyword_action_can_clear_without_text_prompt(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = []
        app.file_kw = "mp4"
        app.video_only = True
        app.sel_f = 2
        app.focus = "MENU"
        app.prompt_list = lambda _title, _options, default_idx=0: "Clear keyword"
        app.prompt = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not prompt for text"))
        app.save_current_file_view_state = lambda: None

        app.activate_menu_action("keyword")

        assert app.file_kw == ""
        assert app.video_only is True
        assert app.sel_f == 0
        assert app.focus == "LIST"

    def test_hint_bar_is_mode_specific_and_mentions_help(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.help_overlay = False

        hint = app.hint_bar()

        assert "Enter/p preview" in hint
        assert "o folder" in hint
        assert "d marked" in hint
        assert "D all visible" in hint
        assert "m range" in hint
        assert "f filter" in hint
        assert "? help" in hint

    def test_menu_items_show_concise_results_actions(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.result_filter = ""
        app.filter = "movies"
        app.sort_by = "downloads desc"
        app.selected_result = lambda: None

        labels = [label for label, _action in app.get_menu_items()]

        assert "IA Search" in labels
        assert "YT Search" in labels
        assert "Source" in labels
        assert "Open" in labels
        assert "Help" in labels

    def test_results_menu_exposes_favorite_item_action(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.result_filter = ""
        app.filter = "movies"
        app.sort_by = ""
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "One")
        app.is_fav_item = lambda _identifier: False

        items = app.get_menu_items()

        assert ("Fav Item", "fav_item") in items

    def test_youtube_result_summary_exposes_badge(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        result = ia_minotaur.SearchResult(
            identifier="yt-abc123",
            title="Video",
            source="youtube",
            video_id="abc123",
            uploader="Channel",
            duration=42,
            upload_date="20260609",
        )

        assert app.result_source_badge(result) == "[YT]"
        assert app.result_meta_summary(result) == "Channel | 42s | 20260609"

    def test_ia_result_source_badge_is_explicit(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        result = ia_minotaur.SearchResult(identifier="item1", title="Archive Item")

        assert app.result_source_badge(result) == "[IA]"

    def test_youtube_result_details_omit_ia_only_fields(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_built = "ytsearch10:sample"
        result = ia_minotaur.SearchResult(
            identifier="yt-abc123",
            title="Video",
            source="youtube",
            video_id="abc123",
            uploader="Channel",
            duration=42,
            upload_date="20260609",
            downloads=999,
            collection="prelinger",
            licenseurl="https://example.invalid/license",
        )

        details = "\n".join(app.youtube_result_details_lines(result))

        assert "Source: [YT] YouTube" in details
        assert "Channel: Channel" in details
        assert "Video ID: abc123" in details
        assert "Downloads:" not in details
        assert "Collection:" not in details
        assert "License hint:" not in details

    def test_youtube_results_menu_hides_ia_only_actions(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.search_source = "youtube"
        app.selected_result = lambda: ia_minotaur.SearchResult(
            identifier="yt-abc123",
            title="Video",
            source="youtube",
            video_id="abc123",
        )

        labels = [label for label, _action in app.get_menu_items()]

        assert "YT Search" in labels
        assert "Source" in labels
        assert not any(label.startswith("Local") for label in labels)
        assert not any(label.startswith("Filter:") for label in labels)
        assert not any(label.startswith("Sort:") for label in labels)

    def test_source_switch_routes_to_selected_search_action(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.prompt_list = lambda _title, _options, default_idx=0: "YouTube search"
        called = []

        def fake_activate(action):
            called.append(action)

        app.activate_menu_action = fake_activate

        RetroWaveIA.choose_search_source(app)

        assert called == ["youtube_search"]

    def test_source_switch_exposes_combined_search(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        seen = {}
        app.prompt_list = lambda _title, options, default_idx=0: seen.setdefault("options", list(options)) and None

        RetroWaveIA.choose_search_source(app)

        assert "Combined IA + YouTube" in seen["options"]

    def test_action_palette_groups_file_actions(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"

        labels = [label for label, _action in app.action_palette_options()]

        assert "Open / preview selected file" in labels
        assert "Download / marked files" in labels
        assert "Filter / file filter menu" in labels
        assert "Select / toggle file mark" in labels
        assert "Select / mark file range" in labels
        assert "Select / mark all visible files" in labels
        assert "Select / invert visible marks" in labels

    def test_files_action_specs_drive_filter_and_mark_aliases(self):
        app = RetroWaveIA.__new__(RetroWaveIA)

        specs = app.files_action_specs()
        lookup = {action: (label, hint) for label, action, hint in specs}

        assert lookup["preview"][1] == "Enter/p"
        assert lookup["folder"][1] == "o"
        assert lookup["toggle_file_mark"][1] == "Space"
        assert lookup["mark_file_range"][1] == "m"
        assert lookup["clear_file_marks"][1] == "U"
        assert lookup["keyword"][1] == "f/F"
        assert lookup["video_only"][1] == "v"

    def test_result_palette_labels_include_search_aliases(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"

        labels = [label for label, _action in app.action_palette_options()]

        assert any("license" in label and "rights" in label for label in labels)
        assert any("date downloads title relevance" in label for label in labels)
        assert any("collections" in label for label in labels)
        assert any("audit summary" in label for label in labels)
        assert any("favorite selected item" in label for label in labels)
        assert any("YouTube direct URL" in label for label in labels)
        assert any("Search / tools" in label for label in labels)

    def test_results_action_specs_drive_local_filter_aliases(self):
        app = RetroWaveIA.__new__(RetroWaveIA)

        specs = app.results_action_specs()
        lookup = {action: (label, hint) for label, action, hint in specs}

        assert lookup["result_filter"][1] == "l/f"
        assert lookup["clear_result_filter"][1] == "L/F"
        assert lookup["search_tools"][1] == "a"

    def test_results_state_chips_show_search_and_filter_state(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app._ensure_search_cache_state()
        app.query_text = "chaplin"
        app.filter = "movies"
        app.title_only = True
        app.sort_by = "downloads desc"
        app.result_filter = "silent"
        app.total_results = ia_minotaur.ROWS_PER_PAGE + 1
        app.page = 2
        with app._search_cache_lock:
            app._all_results_loading = True
            app._all_results_loaded_pages = 1
            app._all_results_total_pages = 3
            app._all_results_cache = [ia_minotaur.SearchResult("one", "One")] * 30

        chips = app.results_state_chips()

        assert "Query: chaplin" in chips
        assert "Media: movies" in chips
        assert "Title only: On" in chips
        assert "Local: silent" in chips
        assert any(chip.startswith("Sort:") for chip in chips)
        assert any(chip.startswith("Page: 2/") for chip in chips)
        assert "scanning 1/3 pages (30 loaded)" in chips

    def test_help_overlay_mentions_results_filter_aliases(self, monkeypatch):
        class FakeScreen:
            def getmaxyx(self):
                return (30, 90)

            def addstr(self, *_args, **_kwargs):
                pass

        monkeypatch.setattr(ia_minotaur.curses, "color_pair", lambda _n: 0)

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.help_overlay = False
        app.stdscr = FakeScreen()
        lines = []
        app.safe_addstr = lambda _y, _x, text, *_args, **_kwargs: lines.append(str(text))

        app.draw_help_overlay()

        assert any("Tab switches MENU/LIST" in line for line in lines)
        assert any("Open Enter/o | Search / | Details r | Local l/f | Clear L/F" in line for line in lines)
        assert any("Actions a | Page n/p" in line for line in lines)

    def test_show_audit_summary_updates_status(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""

        monkeypatch.setattr(
            ia_minotaur.ia_audit,
            "analyze_library",
            lambda root, probe=False, max_probe=0: {
                "summary": {
                    "weird_filenames": 1,
                    "duplicate_movies": 2,
                    "duplicate_episodes": 3,
                    "metadata_issues": 4,
                    "rename_suggestions": 5,
                    "cleanup_candidates": 6,
                }
            },
        )

        app.show_audit_summary()

        assert "Audit: weird 1" in app.status
        assert "dup movies 2" in app.status
        assert "Run ia-audit for details." in app.status

    def test_audit_action_uses_audit_summary(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.show_audit_summary = lambda: setattr(app, "status", "audit ran")
        app.status = ""

        app.activate_menu_action("audit")

        assert app.status == "audit ran"

    def test_search_tools_action_opens_grouped_prompt(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.prompt_list = lambda _title, options, default_idx=0: "Sort order"
        app.activate_menu_action = lambda action: setattr(app, "status", action)

        RetroWaveIA.open_search_tools(app)

        assert app.status == "sort"

    def test_fuzzy_match_accepts_abbreviations(self):
        app = RetroWaveIA.__new__(RetroWaveIA)

        assert app.fuzzy_match("filter / license gate", "lic")
        assert app.fuzzy_match("download / marked files", "dmf")
        assert not app.fuzzy_match("sort / result order", "lic")

    def test_filter_options_uses_fuzzy_terms(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        options = ["Filter / license gate", "Sort / result order", "Download / marked files"]

        assert app.filter_options(options, "lic") == ["Filter / license gate"]

    def test_help_overlay_toggle_preserves_mode(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.help_overlay = False
        app.status = ""

        app.toggle_help_overlay()
        app.toggle_help_overlay()

        assert app.mode == "FILES"
        assert app.help_overlay is False
        assert app.status == "Help closed"

    def test_breadcrumb_includes_selected_file_item(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.selected_result = lambda: ia_minotaur.SearchResult("identifier-1", "Title")

        assert app.breadcrumb() == "Search > Results > Files:identifier-1"

    def test_toggle_current_file_mark_marks_and_unmarks(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = [ia_minotaur.IAFile("one.mp4", 10, "MPEG4")]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = set()
        app.save_current_file_view_state = lambda: None

        app.toggle_current_file_mark()
        assert app.selected_file_names == {"one.mp4"}

        app.toggle_current_file_mark()
        assert app.selected_file_names == set()

    def test_mark_current_file_and_advance_marks_then_moves_down(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = [
            ia_minotaur.IAFile("one.mp4", 10, "MPEG4"),
            ia_minotaur.IAFile("two.mp4", 20, "MPEG4"),
            ia_minotaur.IAFile("three.mp4", 30, "MPEG4"),
        ]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = set()
        app.save_current_file_view_state = lambda: None

        app.mark_current_file_and_advance()

        assert app.selected_file_names == {"one.mp4"}
        assert app.selected_file_order == ["one.mp4"]
        assert app.sel_f == 1

    def test_mark_file_range_marks_visible_span(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = [
            ia_minotaur.IAFile("one.mp4", 10, "MPEG4"),
            ia_minotaur.IAFile("two.mp4", 20, "MPEG4"),
            ia_minotaur.IAFile("three.mp4", 30, "MPEG4"),
            ia_minotaur.IAFile("four.mp4", 40, "MPEG4"),
        ]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 1
        app.selected_file_names = set()
        app.selected_file_order = []
        app.save_current_file_view_state = lambda: None
        app.prompt = lambda _label, default="": "4"
        app.status = ""

        app.mark_file_range()

        assert app.selected_file_names == {"two.mp4", "three.mp4", "four.mp4"}
        assert app.selected_file_order == ["two.mp4", "three.mp4", "four.mp4"]
        assert "Marked 3 file(s)" in app.status

    def test_set_preview_for_marked_uses_marked_files(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = [
            ia_minotaur.IAFile("one.mp4", 10, "MPEG4"),
            ia_minotaur.IAFile("two.mp4", 20, "MPEG4"),
        ]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = {"two.mp4"}
        app.selected_file_order = ["two.mp4"]
        app.cur_meta = {"metadata": {"licenseurl": "https://creativecommons.org/licenses/by/4.0/"}}
        app.enforce_license_gate = False
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        app.set_preview_for_marked()

        assert app.preview_prefix == "__SELECTED__"
        assert [f.name for f in app.preview_files] == ["two.mp4"]
        assert app.preview_plan_kind() == "Marked files"
        assert app.preview_file_count_and_total() == (1, 20)

    def test_set_preview_for_marked_preserves_selection_order(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = [
            ia_minotaur.IAFile("one.mp4", 10, "MPEG4"),
            ia_minotaur.IAFile("two.mp4", 20, "MPEG4"),
            ia_minotaur.IAFile("three.mp4", 30, "MPEG4"),
        ]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = {"one.mp4", "three.mp4"}
        app.selected_file_order = ["three.mp4", "one.mp4"]
        app.cur_meta = {"metadata": {"licenseurl": "https://creativecommons.org/licenses/by/4.0/"}}
        app.enforce_license_gate = False
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        app.set_preview_for_marked()

        assert [f.name for f in app.preview_files] == ["three.mp4", "one.mp4"]

    def test_file_filter_chips_show_active_filters_and_marks(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.file_kw = "mp4"
        app.video_only = True
        app.selected_file_names = {"one.mp4", "two.mp4"}
        app.selected_file_order = ["two.mp4", "one.mp4"]
        app._file_load_loading = False

        assert app.file_filter_chips() == ["Keyword: mp4", "Video only: On", "Marked: 2"]

    def test_file_filter_chips_show_opening_state(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.file_kw = ""
        app.video_only = False
        app.selected_file_names = set()
        app._file_load_loading = True

        assert app.file_filter_chips() == ["Opening item"]

    def test_mark_all_and_invert_visible_files(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.files = [
            ia_minotaur.IAFile("one.mp4", 10, "MPEG4"),
            ia_minotaur.IAFile("two.mp4", 20, "MPEG4"),
            ia_minotaur.IAFile("note.txt", 1, "Text"),
        ]
        app.file_kw = ""
        app.video_only = True
        app.selected_file_names = set()
        app.selected_file_order = []
        app.save_current_file_view_state = lambda: None

        app.mark_all_visible_files()
        assert app.selected_file_names == {"one.mp4", "two.mp4"}
        assert app.selected_file_order == ["one.mp4", "two.mp4"]

        app.invert_visible_file_marks()
        assert app.selected_file_names == set()
        assert app.selected_file_order == []

    def test_prefix_suggestions_include_directory_prefixes(self):
        app = RetroWaveIA.__new__(RetroWaveIA)

        suggestions = app.prefix_suggestions_for_file("series/season01/episode01.mp4")

        assert suggestions[:2] == ["series/", "series/season01/"]

    def test_requires_strong_bulk_confirm_only_for_large_full_item(self):
        app = RetroWaveIA.__new__(RetroWaveIA)

        assert app.requires_strong_bulk_confirm("__FULL_ITEM__", ia_minotaur.BULK_CONFIRM_FILE_THRESHOLD, 1)
        assert app.requires_strong_bulk_confirm("__FULL_ITEM__", 1, ia_minotaur.BULK_CONFIRM_BYTES_THRESHOLD)
        assert not app.requires_strong_bulk_confirm("__SELECTED__", 1000, ia_minotaur.BULK_CONFIRM_BYTES_THRESHOLD * 2)
        assert not app.requires_strong_bulk_confirm("__FULL_ITEM__", 1, 1)

    def test_queue_status_summary_counts_states(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.queue_status = []

        app.init_queue_status([ia_minotaur.IAFile("one.mp4", 10), ia_minotaur.IAFile("two.mp4", 20)])
        app.set_queue_status("one.mp4", "done")
        app.set_queue_status("two.mp4", "failed", "network")

        assert app.queue_summary() == "Queue: done:1 failed:1"

    def test_queue_status_renders_unknown_instead_of_zero_bytes(self, monkeypatch):
        monkeypatch.setattr(curses, "color_pair", lambda _n: 0)
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.queue_status = []
        app.dl_current_name = ""

        app.init_queue_status([ia_minotaur.IAFile("yt-video.mp4", 0, "YouTube video")])
        rows = app.queue_table_rows(80)

        assert "unknown" in rows[1][0]
        assert "0B" not in rows[1][0]

    def test_refresh_preview_import_info_records_existing_and_destinations(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.preview_item = ia_minotaur.SearchResult("item1", "Movie 2001")
        app.preview_file = ia_minotaur.IAFile("movie.mp4", 10)
        app.preview_files = []
        app.last_bucket = "Movies"
        app.find_existing_media_file = lambda _name, _size=0: "/media/Movie/movie.mp4"

        app.refresh_preview_import_info()

        assert app.preview_existing == ["/media/Movie/movie.mp4"]
        assert app.preview_destinations

    def test_completed_download_location_prefers_complete_staged_file(self, monkeypatch, tmp_path):
        root = tmp_path / "media"
        staging = root / ".ia_staging"
        for module in (ia_paths, ia_minotaur):
            monkeypatch.setattr(module, "STAGING_ROOT", str(staging))

        staged = staging / "item1" / "Disc One.ISO"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"1234")

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.find_existing_media_file = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("final lookup not needed"))

        assert app._completed_download_location("item1", "Disc One.ISO", 4) == str(staged)

    def test_choose_bucket_and_path_scans_iso_without_import_prompt(self, monkeypatch, tmp_path):
        root = tmp_path / "media"
        staging = root / ".ia_staging"
        for module in (ia_paths, ia_minotaur):
            monkeypatch.setattr(module, "STAGING_ROOT", str(staging))

        staged = staging / "item1" / "Disc One.ISO"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"iso")

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.scan_staged_dvd_iso = lambda path: f"scanned {path}"
        app.prompt = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not prompt"))
        app.prompt_list = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not prompt"))

        assert app.choose_bucket_and_path("item1", "Disc One.ISO", "Show") == f"scanned {staged}"

    def test_batch_destination_path_uses_single_folder(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        batch = {"bucket": "Movies", "folder": "Movie Folder"}

        path = app.batch_destination_path(batch, "source.mp4", "Ignored")

        assert path.endswith("/Movies/Movie Folder/Movie Folder.mp4")

    def test_movie_filename_for_folder_preserves_extension(self):
        app = RetroWaveIA.__new__(RetroWaveIA)

        assert app.movie_filename_for_folder("Chosen Movie (1934)", "archive_original.mkv") == "Chosen Movie (1934).mkv"

    def test_choose_import_filename_allows_override_and_strips_paths(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.prompt = lambda _label, default="": "../Better Name.mkv"

        assert app.choose_import_filename("Default.mp4", "source.mp4") == "Better Name.mkv"

    def test_choose_import_filename_cancel_leaves_staging(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.prompt = lambda _label, default="": None

        assert app.choose_import_filename("Default.mp4", "source.mp4") is None

    def test_choose_import_foldername_allows_override_and_sanitizes(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.prompt = lambda _label, default="": "../Better Folder"

        assert app.choose_import_foldername("Default Folder") == "..Better Folder"

    def test_pick_folder_name_puts_custom_before_recent_folders(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.favs = {"items": [], "files": [], "folders": {"Movies": ["Old One", "Old Two"]}}
        seen = []
        app.prompt = lambda _label, default="": "Typed Movie"

        def prompt_list(_title, options, default_idx=0):
            seen.extend(options)
            return "Type custom..."

        app.prompt_list = prompt_list

        assert app.pick_folder_name("Movies", "Suggested Movie", "Movie folder: ") == "Typed Movie"
        assert seen == ["Suggested Movie", "Type custom...", "Old One", "Old Two"]

    def test_editable_import_folder_dir_uses_show_folder_for_tv_season_path(self, monkeypatch, tmp_path):
        root = tmp_path / "media"
        monkeypatch.setattr(ia_minotaur, "BUCKET_TV", str(root / "TV"))
        app = RetroWaveIA.__new__(RetroWaveIA)
        final_path = root / "TV" / "A Show" / "Season 02" / "A Show - S02E03.mp4"

        assert app.editable_import_folder_dir(str(final_path)) == str(root / "TV" / "A Show")

    def test_retry_failed_downloads_builds_selected_preview(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.failed_queue = [ia_minotaur.IAFile("one.mp4", 10)]
        app.preview_item = ia_minotaur.SearchResult("item1", "Title")
        app.selected_result = lambda: None
        app.refresh_preview_import_info = lambda: None
        app.status = ""

        app.retry_failed_downloads()

        assert app.mode == "PREVIEW_DL"
        assert app.preview_prefix == "__SELECTED__"
        assert [f.name for f in app.preview_files] == ["one.mp4"]

    def test_resume_or_retry_prefers_failed_queue(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.failed_queue = [ia_minotaur.IAFile("one.mp4", 10)]
        calls = []
        app.retry_failed_downloads = lambda: calls.append("retry")
        app.resume_pending_download = lambda: calls.append("resume")

        app.resume_or_retry_download()

        assert calls == ["retry"]

    def test_resume_or_retry_uses_pending_when_no_failed_queue(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.failed_queue = []
        calls = []
        app.retry_failed_downloads = lambda: calls.append("retry")
        app.resume_pending_download = lambda: calls.append("resume")

        app.resume_or_retry_download()

        assert calls == ["resume"]

    def test_result_details_action_updates_status(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.selected_result = lambda: ia_minotaur.SearchResult(
            "item1",
            "Title",
            mediatype="movies",
            licenseurl="https://creativecommons.org/licenses/by/4.0/",
        )
        app.status = ""

        app.activate_menu_action("details")

        assert "item1" in app.status
        assert "license open" in app.status

    def test_file_view_state_restores_per_item_filters_and_marks(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = [ia_minotaur.IAFile("one.mp4", 10, "MPEG4")]
        app.file_kw = "mp4"
        app.video_only = True
        app.sel_f = 2
        app.selected_file_names = {"one.mp4"}
        app.selected_file_order = ["one.mp4"]
        app.file_view_state = {}
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        app.save_current_file_view_state()

        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = set()
        app.selected_file_order = []
        app.restore_file_view_state("item1")

        assert app.file_kw == "mp4"
        assert app.video_only is True
        assert app.sel_f == 2
        assert app.selected_file_names == {"one.mp4"}
        assert app.selected_file_order == ["one.mp4"]

    def test_file_hotkey_download_works_without_list_focus(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.focus = "MENU"
        calls = []
        app.set_preview_for_marked = lambda: calls.append("download")

        handled = app.handle_files_hotkey(ord("d"))

        assert handled is True
        assert calls == ["download"]

    def test_file_hotkey_uppercase_d_downloads_all_visible(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.focus = "LIST"
        calls = []
        app.set_preview_for_marked = lambda: calls.append("marked")
        app.set_preview_for_item = lambda: calls.append("item")

        handled = app.handle_files_hotkey(ord("D"))

        assert handled is True
        assert calls == ["item"]

    def test_file_hotkey_o_uses_folder_prefix(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.focus = "LIST"
        calls = []
        app.set_preview_for_selected = lambda: calls.append("selected")
        app.set_preview_for_prefix = lambda: calls.append("prefix")

        handled = app.handle_files_hotkey(ord("o"))

        assert handled is True
        assert calls == ["prefix"]

    def test_file_hotkey_uppercase_r_is_reserved_for_retry_resume(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.focus = "LIST"
        app.files = [ia_minotaur.IAFile("one.mp4", 10)]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = set()
        app.status = ""

        handled = app.handle_files_hotkey(ord("R"))

        assert handled is False
        assert app.status == ""

    def test_open_selected_result_uses_shared_open_path(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.show_welcome = True
        calls = []
        app.load_files = lambda async_load=False: calls.append(("open", async_load))

        app.open_selected_result()

        assert app.show_welcome is False
        assert calls == [("open", True)]

    def test_space_hotkey_marks_and_advances(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.focus = "LIST"
        app.files = [ia_minotaur.IAFile("one.mp4", 10), ia_minotaur.IAFile("two.mp4", 20)]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = set()
        app.selected_file_order = []
        app.save_current_file_view_state = lambda: None

        handled = app.handle_files_hotkey(ord(" "))

        assert handled is True
        assert app.selected_file_names == {"one.mp4"}
        assert app.selected_file_order == ["one.mp4"]
        assert app.sel_f == 1

    def test_mouse_wheel_scrolls_files_by_larger_step(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.files = [
            ia_minotaur.IAFile(f"{i}.mp4", 10)
            for i in range(10)
        ]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0

        handled = app.scroll_active_list(1)

        assert handled is True
        assert app.sel_f == ia_minotaur.MOUSE_WHEEL_LINES

    def test_file_hotkey_range_mark_uses_prompt(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.focus = "LIST"
        app.files = [ia_minotaur.IAFile("one.mp4", 10), ia_minotaur.IAFile("two.mp4", 20)]
        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = set()
        app.save_current_file_view_state = lambda: None
        app.prompt = lambda _label, default="": "2"
        app.status = ""

        handled = app.handle_files_hotkey(ord("m"))

        assert handled is True
        assert app.selected_file_names == {"one.mp4", "two.mp4"}

    def test_choose_filter_cancel_does_not_change_filter(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.filter = "movies"
        app.status = ""
        app.prompt_list = lambda _title, _options, default_idx=0: None

        changed = app.choose_filter()

        assert changed is False
        assert app.filter == "movies"
        assert app.status == "Filter unchanged."

    def test_choose_filter_changes_only_after_selection(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.filter = "movies"
        app.query_text = ""
        app.status = ""
        app.prompt_list = lambda _title, _options, default_idx=0: "audio"

        changed = app.choose_filter()

        assert changed is True
        assert app.filter == "audio"
        assert app.status == "Filter set to: audio"

    def test_choose_filter_rewrites_explicit_mediatype_query(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.filter = "audio"
        app.query_text = 'title:"foo" AND mediatype:audio'
        app.query_built = 'title:"foo" AND mediatype:audio'
        app.status = ""
        app.prompt_list = lambda _title, _options, default_idx=0: "movies"

        changed = app.choose_filter()

        assert changed is True
        assert app.filter == "movies"
        assert app.query_text == 'title:"foo" AND mediatype:movies'
        assert app.query_built == 'title:"foo" AND mediatype:movies'
        assert app.status == "Filter set to: movies"

    def test_choose_filter_clears_stale_built_query_without_media_clause(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.filter = "audio"
        app.query_text = "foo"
        app.query_built = "identifier:foo"
        app.status = ""
        app.prompt_list = lambda _title, _options, default_idx=0: "movies"

        changed = app.choose_filter()

        assert changed is True
        assert app.filter == "movies"
        assert app.query_text == "foo"
        assert app.query_built == ""
        assert app.status == "Filter set to: movies"

    def test_filter_action_refreshes_only_when_filter_changes(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.query_text = "chaplin"
        app.filter = "movies"
        calls = []
        app.choose_filter = lambda: False
        app.do_search = lambda reset_page=True: calls.append(reset_page)

        app.activate_menu_action("filter")

        assert calls == []

    def test_choose_sort_cancel_does_not_change_sort(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.sort_by = "downloads desc"
        app.status = ""
        app.prompt_list = lambda _title, _options, default_idx=0: None

        changed = app.choose_sort()

        assert changed is False
        assert app.sort_by == "downloads desc"
        assert app.status == "Sort unchanged."

    def test_result_meta_summary_includes_search_metadata(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        r = ia_minotaur.SearchResult(
            "item1",
            "Title",
            year="1930",
            mediatype="movies",
            downloads=1250,
            licenseurl="https://creativecommons.org/licenses/by/4.0/",
        )

        assert app.result_meta_summary(r) == "1930 | movies | 1.2K dl | lic:open"

    def test_local_result_filter_matches_metadata(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [
            ia_minotaur.SearchResult("one", "Metropolis", year="1927", mediatype="movies", creator="Lang"),
            ia_minotaur.SearchResult("two", "Radio Show", year="1930", mediatype="audio", creator="Host"),
        ]
        app.result_filter = "audio host"

        assert [r.identifier for r in app.get_visible_results()] == ["two"]

    def test_local_result_filter_starts_background_page_scan(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult("page1", "First Page", mediatype="movies")]
        app.query_text = "noir"
        app.query_built = ""
        app.sort_by = ""
        app.title_only = False
        app.filter = "movies"
        app.page = 1
        app.total_results = ia_minotaur.ROWS_PER_PAGE + 1
        app.result_filter = ""
        app.status = ""
        app.sel_r = 3
        app._save_session = lambda: None

        calls = []
        started = []

        def fake_search(query, rows, page, sort=""):
            calls.append((query, rows, page, sort))
            return ([ia_minotaur.SearchResult("page2", "Omega Match", mediatype="movies", description="omega")], ia_minotaur.ROWS_PER_PAGE + 1, "")

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target
                started.append(self)

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(ia_minotaur, "ia_search_via_curl", fake_search)
        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)

        app._reset_search_cache()
        app._prime_search_cache(app._search_cache_key(), app.page, app.results, 2)

        app.set_result_filter("omega")

        assert app.sel_r == 0
        assert app.get_visible_results() == []
        assert len(calls) == 0
        assert started

        started[0]._target()
        visible = app.get_visible_results()
        assert [r.identifier for r in visible] == ["page2"]
        assert len(calls) == 1

    def test_year_local_result_filter_refines_plain_title_search(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "Cop Land"
        app.query_built = '(title:("Cop Land") OR Cop Land) AND mediatype:movies'
        app.sort_by = ""
        app.title_only = False
        app.filter = "movies"
        app.results = [ia_minotaur.SearchResult("unrelated", "Cop Land interview", year="2004")]
        app.total_results = 1
        app.page = 1
        app.sel_r = 0
        app.result_filter = ""
        app._save_session = lambda: None
        calls = []

        def fake_search(query, rows, page, sort=""):
            calls.append((query, rows, page, sort))
            if query == 'title:("Cop Land") AND year:1997 AND mediatype:movies':
                return [ia_minotaur.SearchResult("cop-land-1997", "Cop Land", year="1997", mediatype="movies")], 1, ""
            return [], 0, ""

        monkeypatch.setattr(ia_minotaur, "ia_search_via_curl", fake_search)

        app.set_result_filter("1997")

        visible = app.get_visible_results()
        assert [r.identifier for r in visible] == ["cop-land-1997"]
        assert calls == [('title:("Cop Land") AND year:1997 AND mediatype:movies', ia_minotaur.ROWS_PER_PAGE, 1, "")]
        assert app.status.startswith("Local result filter: 1997 (1 visible")

    def test_empty_local_filter_results_show_background_scan_progress(self, monkeypatch):
        class FakeScreen:
            def getmaxyx(self):
                return (30, 100)

        monkeypatch.setattr(ia_minotaur.curses, "color_pair", lambda _n: 0)

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.stdscr = FakeScreen()
        app.mode = "RESULTS"
        app.focus = "LIST"
        app.results = [ia_minotaur.SearchResult("page1", "First Page", mediatype="movies")]
        app.query_text = "noir"
        app.query_built = ""
        app.sort_by = ""
        app.title_only = False
        app.filter = "movies"
        app.page = 1
        app.total_results = ia_minotaur.ROWS_PER_PAGE + 1
        app.result_filter = "omega"
        app.sel_r = 0
        app.last_search_attempts = []
        app.last_error_detail = ""
        app.download_log = []
        app.selected_result = lambda: None
        lines = []
        app.safe_addstr = lambda _y, _x, text, *_args, **_kwargs: lines.append(str(text))

        app._reset_search_cache()
        app._prime_search_cache(app._search_cache_key(), app.page, app.results, 2)
        with app._search_cache_lock:
            app._all_results_loading = True
            app._all_results_loaded_pages = 1
            app._all_results_total_pages = 2

        app.draw_panels(0)

        assert any("No current matches for \"omega\"; scanning 1/2 pages" in line for line in lines)

    def test_local_result_filter_changes_do_not_restart_active_page_scan(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult("page1", "First Page", mediatype="movies")]
        app.query_text = "noir"
        app.query_built = ""
        app.sort_by = ""
        app.title_only = False
        app.filter = "movies"
        app.page = 1
        app.total_results = ia_minotaur.ROWS_PER_PAGE + 1
        app.status = ""
        app.sel_r = 0
        app._save_session = lambda: None

        calls = []
        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target
                started.append(self)

            def start(self):
                pass

            def is_alive(self):
                return True

        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)
        monkeypatch.setattr(ia_minotaur, "ia_search_via_curl", lambda *_args, **_kwargs: calls.append(_args) or ([], 0, ""))
        app._reset_search_cache()
        app._prime_search_cache(app._search_cache_key(), app.page, app.results, 2)

        app.set_result_filter("omega")
        assert app.get_visible_results() == []
        assert started
        assert len(calls) == 0

        with app._search_cache_lock:
            app._all_results_pages[1] = [ia_minotaur.SearchResult("page2", "Omega Match", mediatype="movies", description="omega")]
            app._all_results_cache = [r for page_list in app._all_results_pages if page_list for r in page_list]
            app._all_results_loaded_pages = sum(1 for page_list in app._all_results_pages if page_list)

        assert [r.identifier for r in app.get_visible_results()] == ["page2"]
        app.set_result_filter("match")
        assert [r.identifier for r in app.get_visible_results()] == ["page2"]

        assert len(calls) == 0
        assert len(started) == 1

    def test_sync_page_to_result_uses_cached_page_for_local_filter(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app._reset_search_cache()
        app.results = [ia_minotaur.SearchResult("page1", "First Page")]
        app.query_text = "noir"
        app.query_built = ""
        app.sort_by = ""
        app.title_only = False
        app.page = 1
        app.sel_r = 0
        app.result_filter = "omega"
        page2 = [ia_minotaur.SearchResult("page2", "Omega Match")]
        app._prime_search_cache(app._search_cache_key(), 1, app.results, 2)
        with app._search_cache_lock:
            app._all_results_pages[1] = page2
            app._all_results_cache = app.results + page2
            app._all_results_loaded_pages = 2

        app._sync_page_to_result(page2[0])

        assert app.page == 2
        assert app.results == page2
        assert app.sel_r == 0

    def test_load_files_reports_error_without_leaving_results(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.results = [ia_minotaur.SearchResult("item1", "One")]
        app.sel_r = 0
        app.result_filter = ""
        app.page = 1
        app._ensure_search_cache_state()
        app.save_current_file_view_state = lambda: None
        app.render = lambda: None
        app.status = ""

        monkeypatch.setattr(ia_minotaur, "ia_files", lambda _identifier: ([], None, "boom"))

        app.load_files()

        assert app.mode == "RESULTS"
        assert app.status == "boom"
        assert "item1" in app.last_error_detail

    def test_async_file_load_completes_without_blocking_ui(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.results = [ia_minotaur.SearchResult("item1", "One")]
        app.sel_r = 0
        app.result_filter = ""
        app.page = 1
        app.file_view_state = {}
        app.save_current_file_view_state = lambda: None
        app.render = lambda: None
        app.status = ""

        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target
                self._daemon = daemon
                started.append(self)

            def start(self):
                return None

        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)
        monkeypatch.setattr(
            ia_minotaur,
            "ia_files",
            lambda _identifier: ([ia_minotaur.IAFile("one.mp4", 10, "MPEG4")], {"metadata": {}}, ""),
        )

        app.load_files(async_load=True)

        assert app.mode == "FILES"
        assert "Loading file list for item1" in app.status
        assert started

        started[0]._target()
        assert app.finish_file_load_if_ready() is True

        assert app.mode == "FILES"
        assert [f.name for f in app.files] == ["one.mp4"]

    def test_async_file_load_error_remains_visible(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.results = [ia_minotaur.SearchResult("item1", "One")]
        app.sel_r = 0
        app.result_filter = ""
        app.page = 1
        app.save_current_file_view_state = lambda: None
        app.render = lambda: None
        app.status = ""

        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target
                started.append(self)

            def start(self):
                return None

        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)
        monkeypatch.setattr(ia_minotaur, "ia_files", lambda _identifier: ([], None, "metadata timed out"))

        app.load_files(async_load=True)
        started[0]._target()

        assert app.finish_file_load_if_ready() is True
        assert app.mode == "FILES"
        assert app.status == "metadata timed out"
        assert "item1" in app.last_error_detail

    def test_async_file_load_traceback_is_compacted_for_ui(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.results = [ia_minotaur.SearchResult("item1", "One")]
        app.sel_r = 0
        app.result_filter = ""
        app.page = 1
        app.save_current_file_view_state = lambda: None
        app.render = lambda: None
        app.status = ""

        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target
                started.append(self)

            def start(self):
                return None

        err = "\n".join(
            [
                "Traceback (most recent call last):",
                "  File \"/opt/ia/venv/lib/python3.11/site-packages/urllib3/connection.py\", line 571, in getresponse",
                "    httplib_response = super().getresponse()",
                "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='archive.org', port=443): Read timed out.",
            ]
        )
        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)
        monkeypatch.setattr(ia_minotaur, "ia_files", lambda _identifier: ([], None, err))

        app.load_files(async_load=True)
        started[0]._target()

        assert app.finish_file_load_if_ready() is True
        assert app.mode == "FILES"
        assert app.status == (
            "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='archive.org', port=443): Read timed out."
        )
        assert app.last_error_detail == (
            "File load failed for item1: requests.exceptions.ReadTimeout: "
            "HTTPSConnectionPool(host='archive.org', port=443): Read timed out."
        )

    def test_set_result_filter_resets_selection_and_status(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult("one", "Metropolis")]
        app.sel_r = 3
        app.status = ""
        app._ensure_search_cache_state()
        with app._search_cache_lock:
            app._all_results_loading = True
            app._all_results_loaded_pages = 1
            app._all_results_total_pages = 2
            app._all_results_cache = app.results

        app.set_result_filter("metro")

        assert app.sel_r == 0
        assert "1 visible" in app.status
        assert "scanning 1/2 pages" in app.status

    def test_footer_status_recomputes_stale_local_filter_status(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.results = [ia_minotaur.SearchResult("one", "Metropolis", year="1927")]
        app.result_filter = "1927"
        app.status = "Local result filter: 1927 (0 visible; scanning 1/2 pages (30 loaded))"
        app._ensure_search_cache_state()
        with app._search_cache_lock:
            app._all_results_loading = False
            app._all_results_loaded_pages = 2
            app._all_results_total_pages = 2
            app._all_results_cache = app.results

        status = app.footer_status_text()

        assert status == "Local result filter: 1927 (1 visible; scan complete (1 loaded))"

    def test_collection_choices_count_current_results(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [
            ia_minotaur.SearchResult("one", "One", collection="prelinger, movies"),
            ia_minotaur.SearchResult("two", "Two", collection="prelinger"),
            ia_minotaur.SearchResult("three", "Three", collection="audio"),
        ]

        assert app.collection_choices_from_results()[:2] == ["prelinger (2)", "audio (1)"]

    def test_do_search_falls_back_to_fielded_query(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "chaplin"
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = ""
        app.focus = "MENU"
        app.render = lambda: None
        calls = []

        def fake_search(query, rows, page, sort=""):
            calls.append(query)
            if "creator" in query:
                return [ia_minotaur.SearchResult("one", "One")], 1, ""
            return [], 0, ""

        monkeypatch.setattr(ia_minotaur, "ia_search_via_curl", fake_search)

        app.do_search()

        assert len(calls) == 3
        assert calls[0] == "identifier:chaplin AND mediatype:movies"
        assert "creator" in app.query_built
        assert "fields match" in app.status

    def test_collection_search_action_builds_collection_query(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.prompt = lambda _label, _default="", history=None: "jazz"
        calls = []
        app.set_query_and_search = lambda text, built_query=None: calls.append((text, built_query))

        app.activate_menu_action("collection_search")

        assert calls == [("jazz", '(title:("jazz") OR subject:("jazz") OR description:("jazz") OR jazz) AND mediatype:collection')]

    def test_search_preset_action_builds_archive_preset_query(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        prompts = iter(["Chaplin"])
        app.prompt = lambda _label, _default="", history=None: next(prompts)
        app.prompt_list = lambda _label, options, default_idx=0: options[0]
        calls = []
        app.set_query_and_search = lambda text, built_query=None: calls.append((text, built_query))

        app.activate_menu_action("search_preset")

        assert calls
        assert calls[0][0] == "Chaplin"
        assert calls[0][1].startswith("(mediatype:movies AND")
        assert "title:(\"Chaplin\")" in calls[0][1]

    def test_do_search_preserves_local_filter_for_same_query(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "chaplin"
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = "silent"
        app.focus = "MENU"
        app.render = lambda: None
        app._save_session = lambda: None
        app.last_search_text = "chaplin"
        app.last_search_attempts = []
        app.last_search_used_label = ""

        monkeypatch.setattr(
            ia_minotaur,
            "ia_search_via_curl",
            lambda query, rows, page, sort="": ([ia_minotaur.SearchResult("one", "One")], 1, ""),
        )

        app.do_search(reset_page=True)

        assert app.result_filter == "silent"
        assert app.last_search_used_label in {"title", "identifier", "fields", "plain"}
        assert app.last_search_attempts

    def test_do_search_clears_local_filter_for_new_query(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "chaplin"
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = "silent"
        app.focus = "MENU"
        app.render = lambda: None
        app._save_session = lambda: None
        app.last_search_text = "keaton"
        app.last_search_attempts = []
        app.last_search_used_label = ""

        monkeypatch.setattr(
            ia_minotaur,
            "ia_search_via_curl",
            lambda query, rows, page, sort="": ([ia_minotaur.SearchResult("one", "One")], 1, ""),
        )

        app.do_search(reset_page=True)

        assert app.result_filter == ""

    def test_jump_to_result_number_without_total_results(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult("one", "One"), ia_minotaur.SearchResult("two", "Two")]
        app.result_filter = ""
        app.total_results = 0
        app.sel_r = 0

        app.jump_to_result_number(2)

        assert app.sel_r == 1
        assert app.status == "Selected result 2."

    def test_short_result_page_caps_total_for_next_page(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult(f"item{i}", f"Item {i}") for i in range(22)]
        app.result_filter = ""
        app.total_results = 100
        app.page = 1
        app.query_text = "collection search"
        app.search_source = "ia"
        app.focus = "MENU"
        app.menu_idx = 0
        app.start_search_async = lambda reset_page=False: (_ for _ in ()).throw(AssertionError("should not load page 2"))

        app.next_page()

        assert app.page == 1
        assert app.total_results == 22
        assert app.status == "Already on last page."

    def test_short_result_page_rejects_jump_past_visible_total(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult(f"item{i}", f"Item {i}") for i in range(22)]
        app.result_filter = ""
        app.total_results = 100
        app.page = 1
        app.sel_r = 0

        app.jump_to_result_number(31)

        assert app.sel_r == 0
        assert app.total_results == 22
        assert app.status == "Result must be 1-22."

    def test_sort_labels_are_date_not_year(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.sort_by = "date desc"

        assert app._sort_label() == "date (new)"

    def test_session_persists_preferences_not_search_state(self, monkeypatch, tmp_path):
        session_path = tmp_path / "session.json"
        monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(session_path))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "query"
        app.filter = "movies"
        app.title_only = True
        app.page = 3
        app.sort_by = "downloads desc"
        app.enforce_license_gate = True
        app.result_filter = "silent"
        app.last_search_text = "query"
        app.search_source = "youtube"
        app.search_history = ["query"]
        app._save_session()

        restored = RetroWaveIA.__new__(RetroWaveIA)
        restored.query_text = ""
        restored.filter = "any"
        restored.title_only = False
        restored.page = 1
        restored.sort_by = ""
        restored.enforce_license_gate = False
        restored.result_filter = ""
        restored.last_search_text = ""
        restored.search_source = "ia"
        restored.search_history = []
        restored._restore_session()

        assert restored.enforce_license_gate is True
        assert restored.filter == "movies"
        assert restored.title_only is True
        assert restored.sort_by == "downloads desc"
        assert restored.query_text == ""
        assert restored.page == 1
        assert restored.result_filter == ""
        assert restored.last_search_text == ""
        assert restored.search_source == "ia"
        assert restored.search_history == ["query"]

        saved = ia_minotaur.ia_state.load_session(str(session_path))
        assert "query_text" not in saved
        assert "result_filter" not in saved
        assert "search_source" not in saved

    def test_loop_does_not_restore_query_or_auto_search(self, monkeypatch):
        class FakeScreen:
            def keypad(self, _flag):
                pass

            def timeout(self, _ms):
                pass

            def getch(self):
                return ord("q")

        monkeypatch.setattr(ia_minotaur, "ensure_dirs", lambda: None)
        monkeypatch.setattr(curses, "curs_set", lambda *_args, **_kwargs: None)

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.stdscr = FakeScreen()
        app.ia_present = True
        app.ia_version = "ia 1.0"
        app.query_text = ""
        app.search_source = "ia"
        app.exit_requested = False
        app.help_overlay = False
        app.mode = "RESULTS"
        app.init_colors = lambda: None
        app.render = lambda: None
        app.finish_file_load_if_ready = lambda: None
        app._load_pending = lambda: None
        app._save_session = lambda: None
        app._restore_session = lambda: None
        app.do_search = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not auto-search"))

        app.loop()

        assert app.status == "Ready (ia: ia 1.0). Choose [Search]."

    def test_do_search_saves_history_immediately(self, monkeypatch, tmp_path):
        session_path = tmp_path / "session.json"
        monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(session_path))
        monkeypatch.setattr(ia_minotaur, "ia_search_via_curl", lambda *_args, **_kwargs: ([], 0, ""))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "noir"
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = ""
        app.last_search_text = ""
        app.focus = "MENU"
        app.enforce_license_gate = False
        app.render = lambda: None

        app.do_search()

        saved = ia_minotaur.ia_state.load_session(str(session_path))
        assert saved["search_history"] == ["noir"]
        assert "last_search_text" not in saved

    def test_do_search_does_not_prefetch_extra_pages(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "noir"
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = ""
        app.last_search_text = ""
        app.last_search_attempts = []
        app.last_search_used_label = ""
        app.focus = "MENU"
        app.enforce_license_gate = False
        app.render = lambda: None
        app._save_session = lambda: None

        monkeypatch.setattr(
            ia_minotaur,
            "ia_search_via_curl",
            lambda query, rows, page, sort="": (
                [ia_minotaur.SearchResult(f"item{i}", f"Item {i}") for i in range(ia_minotaur.ROWS_PER_PAGE)],
                500,
                "",
            ),
        )
        monkeypatch.setattr(
            ia_minotaur.threading,
            "Thread",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not prefetch")),
        )

        app.do_search()

        assert app.total_results == 500

    def test_start_search_async_does_not_run_search_inline(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "noir"
        app.query_built = ""
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = ""
        app.last_search_text = ""
        app.focus = "MENU"
        app.enforce_license_gate = False
        app.show_welcome = True
        app.cancel_file_load = lambda: None
        app._save_session = lambda: None

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target

            def start(self):
                pass

        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)
        monkeypatch.setattr(
            ia_minotaur,
            "ia_search_via_curl",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("search ran inline")),
        )

        app.start_search_async()

        assert app._search_load_loading is True
        assert app.query_built == "identifier:noir AND mediatype:movies"
        assert app.status.startswith("Searching...")

    def test_finish_search_load_applies_background_results(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "noir"
        app.query_built = ""
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = ""
        app.last_search_text = ""
        app.last_search_attempts = []
        app.last_search_used_label = ""
        app.focus = "MENU"
        app.enforce_license_gate = False
        app.show_welcome = True
        app.cancel_file_load = lambda: None
        app._save_session = lambda: None

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)
        monkeypatch.setattr(
            ia_minotaur,
            "ia_search_via_curl",
            lambda *_args, **_kwargs: ([ia_minotaur.SearchResult("one", "One")], 1, ""),
        )

        app.start_search_async()

        assert app.finish_search_load_if_ready() is True
        assert [r.identifier for r in app.results] == ["one"]
        assert app.mode == "RESULTS"
        assert app.focus == "LIST"

    def test_combined_search_keeps_youtube_results_when_ia_is_empty(self, monkeypatch):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "Nightshift - Henry Winkler"
        app.query_built = ""
        app.filter = "movies"
        app.title_only = False
        app.sort_by = ""
        app.search_history = []
        app.page = 1
        app.sel_r = 0
        app.result_filter = ""
        app.last_search_text = ""
        app.last_search_attempts = []
        app.last_search_used_label = ""
        app.focus = "MENU"
        app.enforce_license_gate = False
        app.show_welcome = True
        app.cancel_file_load = lambda: None
        app._save_session = lambda: None

        class FakeThread:
            def __init__(self, target=None, daemon=False):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(ia_minotaur.threading, "Thread", FakeThread)
        monkeypatch.setattr(ia_minotaur, "ia_search_via_curl", lambda *_args, **_kwargs: ([], 0, ""))
        monkeypatch.setattr(
            ia_minotaur,
            "yt_search",
            lambda *_args, **_kwargs: (
                [ia_minotaur.SearchResult("yt-abc123", "Night Shift clip", source="youtube", video_id="abc123")],
                1,
                "",
            ),
        )

        app.start_combined_search_async("Nightshift - Henry Winkler")

        assert app.finish_search_load_if_ready() is True
        assert app.search_source == "all"
        assert [r.identifier for r in app.results] == ["yt-abc123"]
        assert app.status == "Combined — 1 result(s): IA 0, YouTube 1."

    def test_results_menu_exposes_local_filter(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.result_filter = "silent"
        app.filter = "movies"
        app.sort_by = ""

        labels = [label for label, _action in app.get_menu_items()]

        assert "Local: silent" in labels
        assert "Clear Local" in labels

    def test_clear_result_filter_clears_and_saves(self, monkeypatch, tmp_path):
        session_path = tmp_path / "session.json"
        monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(session_path))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.result_filter = "silent"
        app.sel_r = 2
        app.search_history = []
        app.query_text = "chaplin"
        app.filter = "movies"
        app.title_only = False
        app.page = 1
        app.sort_by = ""
        app.enforce_license_gate = False

        app.clear_result_filter()

        assert app.result_filter == ""
        assert app.sel_r == 0
        saved = ia_minotaur.ia_state.load_session(str(session_path))
        assert "result_filter" not in saved

    def test_result_filter_hotkeys_edit_and_clear(self, monkeypatch, tmp_path):
        session_path = tmp_path / "session.json"
        monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(session_path))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"
        app.focus = "LIST"
        app.results = [ia_minotaur.SearchResult("one", "One")]
        app.query_text = "chaplin"
        app.filter = "movies"
        app.title_only = False
        app.page = 1
        app.sort_by = ""
        app.search_history = []
        app.sel_r = 0
        app.result_filter = "silent"
        app.enforce_license_gate = False
        app.render = lambda: None
        app.prompt = lambda _label, default="", history=None: "audio"
        app._save_session = lambda: None

        assert app.handle_results_hotkey(ord("f")) is True
        assert app.result_filter == "audio"

        app.result_filter = "silent"
        assert app.handle_results_hotkey(ord("F")) is True
        assert app.result_filter == ""

        app.result_filter = "silent"
        assert app.handle_results_hotkey(ord("l")) is True
        assert app.result_filter == "audio"

        app.result_filter = "silent"
        assert app.handle_results_hotkey(ord("L")) is True
        assert app.result_filter == ""


# --------------------------------------------------------- environment check
class TestEnvironmentChecks:
    def test_check_writable_dir_creates_and_tests_path(self, tmp_path):
        target = tmp_path / "new-dir"

        ok, msg = ia_minotaur.check_writable_dir(str(target))

        assert ok
        assert msg == str(target)
        assert target.is_dir()

    def test_environment_checks_reports_binaries_and_paths(self, monkeypatch, tmp_path):
        root = tmp_path / "media"
        monkeypatch.setattr(ia_minotaur, "MEDIA_ROOT", str(root))
        monkeypatch.setattr(ia_minotaur, "STAGING_ROOT", str(root / ".ia_staging"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_TV", str(root / "TV"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_MOVIES", str(root / "Movies"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_MUSIC", str(root / "Music"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_OTHER", str(root / "Other"))
        monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(tmp_path / "home" / ".session.json"))
        monkeypatch.setattr(ia_minotaur, "PENDING_PATH", str(tmp_path / "home" / ".pending.json"))
        monkeypatch.setattr(ia_minotaur, "LOG_PATH", str(root / ".ia_dl.log"))

        def fake_run_cmd(cmd, timeout=60):
            if cmd == ["ia", "--version"]:
                return 0, "ia 1.0\n", ""
            if cmd == ["curl", "--version"]:
                return 0, "curl 8.0\n", ""
            if cmd == [ia_minotaur.APP_CONFIG["yt_dlp_path"], "--version"]:
                return 0, "2026.06.09\n", ""
            return 1, "", "unexpected"

        monkeypatch.setattr(ia_minotaur, "run_cmd", fake_run_cmd)

        checks = ia_minotaur.environment_checks()

        assert all(ok for _label, ok, _msg in checks)
        assert ("ia CLI", True, "ia 1.0") in checks
        assert ("curl", True, "curl 8.0") in checks

    def test_environment_checks_propagates_missing_curl(self, monkeypatch, tmp_path):
        root = tmp_path / "media"
        monkeypatch.setattr(ia_minotaur, "MEDIA_ROOT", str(root))
        monkeypatch.setattr(ia_minotaur, "STAGING_ROOT", str(root / ".ia_staging"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_TV", str(root / "TV"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_MOVIES", str(root / "Movies"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_MUSIC", str(root / "Music"))
        monkeypatch.setattr(ia_minotaur, "BUCKET_OTHER", str(root / "Other"))
        monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(tmp_path / "home" / ".session.json"))
        monkeypatch.setattr(ia_minotaur, "PENDING_PATH", str(tmp_path / "home" / ".pending.json"))
        monkeypatch.setattr(ia_minotaur, "LOG_PATH", str(root / ".ia_dl.log"))

        def fake_run_cmd(cmd, timeout=60):
            if cmd == ["ia", "--version"]:
                return 0, "ia 1.0\n", ""
            if cmd == ["curl", "--version"]:
                return 127, "", "command not found"
            return 1, "", "unexpected"

        monkeypatch.setattr(ia_minotaur, "run_cmd", fake_run_cmd)

        checks = ia_minotaur.environment_checks()

        assert ("curl", False, "command not found") in checks


# --------------------------------------------------------- import paths
class TestChooseBucketAndPath:
    def build_app(self, responses):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.files = []
        app.last_bucket = "Other"
        app.favs = {"items": [], "files": [], "folders": {"TV": [], "Movies": [], "Music": [], "Other": []}}
        app.save_favs = lambda: None

        answers = iter(responses)

        def prompt(_label, default=""):
            try:
                answer = next(answers)
            except StopIteration:
                return default
            return default if answer == "" else answer

        def prompt_list(_title, options, default_idx=0):
            try:
                answer = next(answers)
            except StopIteration:
                return options[default_idx]
            return options[default_idx] if answer == "" else answer

        app.prompt = prompt
        app.prompt_list = prompt_list
        return app

    def set_roots(self, monkeypatch, tmp_path):
        root = tmp_path / "media"
        monkeypatch.setenv("IA_RADARR_ENABLED", "false")
        for module in (ia_minotaur, ia_paths):
            monkeypatch.setattr(module, "MEDIA_ROOT", str(root))
            monkeypatch.setattr(module, "STAGING_ROOT", str(root / ".ia_staging"))
            monkeypatch.setattr(module, "BUCKET_TV", str(root / "TV"))
            monkeypatch.setattr(module, "BUCKET_MOVIES", str(root / "Movies"))
            monkeypatch.setattr(module, "BUCKET_MUSIC", str(root / "Music"))
            monkeypatch.setattr(module, "BUCKET_OTHER", str(root / "Other"))
            monkeypatch.setattr(module, "LOG_PATH", str(root / ".ia_dl.log"))
        return root

    def test_prompt_accepts_keypad_enter_after_editing(self, monkeypatch):
        class FakeScreen:
            def __init__(self):
                self.keys = iter([curses.KEY_BACKSPACE, curses.KEY_BACKSPACE, ord("0"), ord("5"), curses.KEY_ENTER])
                self.nodelay_flag = True

            def getmaxyx(self):
                return (24, 80)

            def addstr(self, *_args, **_kwargs):
                pass

            def erase(self):
                pass

            def move(self, *_args, **_kwargs):
                pass

            def refresh(self):
                pass

            def getch(self):
                return next(self.keys)

            def nodelay(self, flag):
                self.nodelay_flag = flag

        monkeypatch.setattr(curses, "curs_set", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(curses, "color_pair", lambda _n: 0)

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.stdscr = FakeScreen()

        assert app.prompt("Season number (01..): ", "01") == "05"
        assert app.stdscr.nodelay_flag is False

    def test_prompt_list_restores_blocking_mode(self, monkeypatch):
        class FakeScreen:
            def __init__(self):
                self.keys = iter([curses.KEY_ENTER])
                self.nodelay_flag = True

            def getmaxyx(self):
                return (24, 80)

            def addstr(self, *_args, **_kwargs):
                pass

            def erase(self):
                pass

            def move(self, *_args, **_kwargs):
                pass

            def refresh(self):
                pass

            def getch(self):
                return next(self.keys)

            def nodelay(self, flag):
                self.nodelay_flag = flag

        monkeypatch.setattr(curses, "color_pair", lambda _n: 0)

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.stdscr = FakeScreen()

        assert app.prompt_list("TV folder", ["A Show", "B Show"]) == "A Show"
        assert app.stdscr.nodelay_flag is False

    def stage_file(self, identifier, filename, content=b"x"):
        path = ia_minotaur.staging_file_path(identifier, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_movie_import_uses_clean_folder_and_filename(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "The.Big.Movie.1999.1080p.mkv")
        app = self.build_app(["Movies", ""])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "The Big Movie", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "The.Big.Movie.1999.1080p.mkv", "")

        final_path = root / "Movies" / "The Big Movie (1999)" / "The Big Movie (1999).mkv"
        assert msg == f"Saved: {final_path}"
        assert final_path.read_bytes() == b"x"

    def test_movie_import_uses_chosen_folder_as_filename(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "ia_file_12345.mp4")
        app = self.build_app(["Movies", "Custom Movie (1942)"])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Messy Source", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "ia_file_12345.mp4", "Messy Source")

        final_path = root / "Movies" / "Custom Movie (1942)" / "Custom Movie (1942).mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.read_bytes() == b"x"

    def test_movie_import_invokes_radarr_after_final_move(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "Metropolis.1927.mp4")
        calls = []
        app = self.build_app(["Movies", ""])
        app.download_log = []
        app.cur_meta = {"metadata": {"external-identifier": "tmdb:19"}}
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Metropolis", year="1927", mediatype="movies")

        def fake_register(path, **kwargs):
            calls.append((path, kwargs))
            assert os.path.exists(path)
            return ia_minotaur.ia_radarr.RadarrResult(True, "added", "Radarr movie added; refresh requested.", True)

        monkeypatch.setattr(ia_minotaur.ia_radarr, "register_completed_movie", fake_register)

        msg = app.choose_bucket_and_path("item1", "Metropolis.1927.mp4", "Metropolis")

        final_path = root / "Movies" / "Metropolis (1927)" / "Metropolis (1927).mp4"
        assert calls[0][0] == str(final_path)
        assert calls[0][1]["metadata"] == {"metadata": {"external-identifier": "tmdb:19"}}
        assert msg == f"Saved: {final_path} | Radarr: Radarr movie added; refresh requested."

    def test_tv_import_does_not_invoke_radarr(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "pilot.S02E03.mp4")
        calls = []
        app = self.build_app(["TV", "A Show"])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "A Show", mediatype="movies")
        monkeypatch.setattr(ia_minotaur.ia_radarr, "register_completed_movie", lambda *_args, **_kwargs: calls.append("radarr"))

        msg = app.choose_bucket_and_path("item1", "pilot.S02E03.mp4", "A Show")

        final_path = root / "TV" / "A Show" / "Season 02" / "A Show - S02E03.mp4"
        assert msg == f"Saved: {final_path}"
        assert calls == []

    def test_radarr_failure_does_not_corrupt_completed_movie(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "Metropolis.1927.mp4", b"done")
        app = self.build_app(["Movies", ""])
        app.download_log = []
        app.cur_meta = {}
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Metropolis", year="1927", mediatype="movies")
        monkeypatch.setattr(
            ia_minotaur.ia_radarr,
            "register_completed_movie",
            lambda *_args, **_kwargs: ia_minotaur.ia_radarr.RadarrResult(False, "radarr_failed", "Radarr unavailable."),
        )

        msg = app.choose_bucket_and_path("item1", "Metropolis.1927.mp4", "Metropolis")

        final_path = root / "Movies" / "Metropolis (1927)" / "Metropolis (1927).mp4"
        assert final_path.read_bytes() == b"done"
        assert msg == f"Saved: {final_path} | Radarr: Radarr unavailable."

    def test_final_import_prompt_can_edit_movie_folder_and_filename(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "ia_file_12345.mp4")
        app = self.build_app([
            "Movies",
            "",
            "",
            "Edit folder",
            "Better Movie (1942)",
            "Edit filename",
            "Better Movie (1942).mp4",
            "",
        ])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Messy Source", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "ia_file_12345.mp4", "Messy Source")

        final_path = root / "Movies" / "Better Movie (1942)" / "Better Movie (1942).mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.read_bytes() == b"x"

    def test_tv_import_renames_detected_episode(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "pilot.S02E03.mp4")
        app = self.build_app(["TV", "A Show"])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "A Show", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "pilot.S02E03.mp4", "A Show")

        final_path = root / "TV" / "A Show" / "Season 02" / "A Show - S02E03.mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.exists()

    def test_final_import_prompt_edits_tv_show_folder_not_season_folder(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "pilot.S02E03.mp4")
        app = self.build_app(["TV", "A Show", "", "Edit folder", "Better Show", ""])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "A Show", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "pilot.S02E03.mp4", "A Show")

        final_path = root / "TV" / "Better Show" / "Season 02" / "A Show - S02E03.mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.exists()

    def test_tv_folder_menu_and_season_prompt_finish_with_keypad_enter(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "source.mp4")

        class FakeScreen:
            def __init__(self):
                self.keys = iter([
                    curses.KEY_ENTER,  # save destination -> TV
                    curses.KEY_ENTER,  # TV folder -> default folder
                    curses.KEY_BACKSPACE,
                    curses.KEY_BACKSPACE,
                    ord("0"),
                    ord("5"),
                    curses.KEY_ENTER,  # season prompt
                    curses.KEY_ENTER,  # episode prompt keeps blank name
                    curses.KEY_ENTER,  # final filename prompt
                    curses.KEY_ENTER,  # final path confirm
                ])

            def getmaxyx(self):
                return (24, 80)

            def addstr(self, *_args, **_kwargs):
                pass

            def erase(self):
                pass

            def move(self, *_args, **_kwargs):
                pass

            def refresh(self):
                pass

            def getch(self):
                return next(self.keys)

            def nodelay(self, _flag):
                pass

        monkeypatch.setattr(curses, "curs_set", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(curses, "color_pair", lambda _n: 0)
        monkeypatch.setattr(ia_minotaur, "infer_bucket", lambda *_args, **_kwargs: ("TV", "test"))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.stdscr = FakeScreen()
        app.favs = {"items": [], "files": [], "folders": {"TV": [], "Movies": [], "Music": [], "Other": []}}
        app.save_favs = lambda: None
        app.render = lambda: None
        app.last_bucket = "Movies"
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "A Show", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "source.mp4", "A Show")

        final_path = root / "TV" / "A Show" / "Season 05" / "source.mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.exists()

    def test_tv_folder_favorite_is_saved_after_season_prompt(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "source.mp4")
        calls = []
        app = self.build_app(["TV", "A Show"])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "A Show", mediatype="movies")
        app.render = lambda: None

        answers = iter(["05", "", ""])

        def add_folder_fav(bucket, folder_name):
            calls.append(("fav", bucket, folder_name))

        def prompt(label, default=""):
            calls.append(("prompt", label))
            answer = next(answers)
            return default if answer == "" else answer

        app.add_folder_fav = add_folder_fav
        app.prompt = prompt

        msg = app.choose_bucket_and_path("item1", "source.mp4", "A Show")

        final_path = root / "TV" / "A Show" / "Season 05" / "source.mp4"
        assert msg == f"Saved: {final_path}"
        assert calls[0][0] == "prompt"
        assert calls[1][0] == "prompt"
        assert calls[2] == ("fav", "TV", "A Show")

    def test_tv_import_uses_menu_driven_destination_selection(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "pilot.S02E03.mp4")
        prompts = []
        menus = []
        app = self.build_app(["TV", "", ""])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "A Show", mediatype="movies")

        original_prompt = app.prompt
        original_prompt_list = app.prompt_list

        def prompt(label, default=""):
            prompts.append((label, default))
            return original_prompt(label, default)

        def prompt_list(title, options, default_idx=0):
            menus.append((title, list(options), default_idx))
            return original_prompt_list(title, options, default_idx)

        app.prompt = prompt
        app.prompt_list = prompt_list

        msg = app.choose_bucket_and_path("item1", "pilot.S02E03.mp4", "A Show")

        final_path = root / "TV" / "A Show" / "Season 02" / "A Show - S02E03.mp4"
        assert msg == f"Saved: {final_path}"
        assert menus[0][0].startswith("Save destination")
        assert menus[0][1] == ["TV", "Movies", "Music", "Other"]
        assert menus[1][0] == "TV folder"
        assert menus[2][0] == f"Final path: {final_path}"
        assert menus[2][1] == ["Accept", "Edit folder", "Edit filename", "Cancel"]
        assert prompts == [("Filename (Enter accepts, Esc leaves in staging): ", "A Show - S02E03.mp4")]

    def test_duplicate_movie_gets_timestamp_suffix(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        final_path = root / "Movies" / "Movie (2001)" / "Movie (2001).mp4"
        os.makedirs(final_path.parent, exist_ok=True)
        final_path.write_bytes(b"old")
        self.stage_file("item1", "Movie.2001.mp4", b"new")
        monkeypatch.setattr(ia_minotaur.time, "strftime", lambda _fmt: "20260102_030405")
        app = self.build_app(["Movies", ""])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Movie", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "Movie.2001.mp4", "")

        stamped = root / "Movies" / "Movie (2001)" / "Movie (2001)_20260102_030405.mp4"
        assert msg == f"Saved: {stamped}"
        assert final_path.read_bytes() == b"old"
        assert stamped.read_bytes() == b"new"

    def test_traversal_filename_is_left_in_staging(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        staging_path = self.stage_file("item1", "../../../escape.mp4")
        app = self.build_app(["Other", "Misc"])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        msg = app.choose_bucket_and_path("item1", "../../../escape.mp4", "Title")

        assert msg.startswith("Refused: staging path escapes item staging dir:")
        assert os.path.exists(staging_path)

    def test_missing_staging_file_reports_without_creating_destination(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        app = self.build_app([])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        msg = app.choose_bucket_and_path("item1", "missing.mp4", "Title")

        assert msg.startswith("Downloaded, but staging file not found:")
        assert not (root / "Movies").exists()

    def test_movie_import_defaults_to_movies_not_sticky_tv(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "You.Only.Live.Twice.1967.mp4")
        menus = []
        app = self.build_app(["", ""])
        app.last_bucket = "TV"
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "You Only Live Twice", mediatype="movies")

        original_prompt_list = app.prompt_list

        def prompt_list(title, options, default_idx=0):
            menus.append((title, list(options), default_idx))
            return original_prompt_list(title, options, default_idx)

        app.prompt_list = prompt_list
        msg = app.choose_bucket_and_path("item1", "You.Only.Live.Twice.1967.mp4", "You Only Live Twice")

        final_path = root / "Movies" / "You Only Live Twice (1967)" / "You Only Live Twice (1967).mp4"
        assert msg == f"Saved: {final_path}"
        assert menus[0][0].startswith("Save destination")
        assert menus[0][1] == ["TV", "Movies", "Music", "Other"]
        assert final_path.exists()

    def test_tv_import_stays_in_tv_bucket(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "pilot.S02E03.mp4")
        app = self.build_app(["TV", "A Show"])
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "A Show", mediatype="movies")

        msg = app.choose_bucket_and_path("item1", "pilot.S02E03.mp4", "A Show")

        final_path = root / "TV" / "A Show" / "Season 02" / "A Show - S02E03.mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.exists()

    def test_likely_import_destination_ignores_sticky_tv_for_movie_title(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.last_bucket = "TV"
        app.preview_item = None
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "You Only Live Twice", mediatype="movies")

        path = app.likely_import_destination("You.Only.Live.Twice.1967.mp4", "You Only Live Twice")

        assert path.endswith("/Movies/You Only Live Twice (1967)/You Only Live Twice (1967).mp4")

    def test_resume_imports_complete_staged_file_without_redownloading(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        pending_path = tmp_path / "pending.json"
        monkeypatch.setattr(ia_minotaur, "PENDING_PATH", str(pending_path))
        staged = self.stage_file("item1", "done.mp4", b"abcd")
        ia_minotaur.ia_state.save_pending(
            str(pending_path),
            {
                "identifier": "item1",
                "item_title": "Title",
                "files": [{"name": "done.mp4", "size": 4, "fmt": "MPEG4"}],
                "preview_prefix": "__FULL_ITEM__",
                "glob_pat": "",
                "completed_names": [],
            },
        )

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.results = []
        app.sel_r = 0
        app.mode = "FILES"
        app.focus = "LIST"
        app.download_log = []
        app.prompt = lambda _label, _default="": ""
        app.render = lambda: None
        app.find_existing_media_file = lambda _name, _size=0: None
        app.choose_bucket_and_path = lambda identifier, filename, title: f"imported {identifier}/{filename}/{title}"
        app._download_one_with_progress = lambda *_args: (_ for _ in ()).throw(AssertionError("should not redownload"))

        app.resume_pending_download()

        assert os.path.exists(staged)
        assert app.status == "Resume complete. 1 file(s) handled."
        assert not pending_path.exists()

    def test_cancel_single_file_download_saves_resume_state(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        pending_path = tmp_path / "pending.json"
        monkeypatch.setattr(ia_minotaur, "PENDING_PATH", str(pending_path))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.results = []
        app.sel_r = 0
        app.mode = "FILES"
        app.focus = "LIST"
        app.download_log = []
        app.failed_queue = []
        app.preview_prefix = ""
        app.cur_meta = {"licenseurl": "https://creativecommons.org/licenses/by/4.0/", "rights": ""}
        app.enforce_license_gate = False
        app.preview_item = ia_minotaur.SearchResult(
            "item1",
            "Title",
            licenseurl="https://creativecommons.org/licenses/by/4.0/",
        )
        app.preview_file = ia_minotaur.IAFile("one.mp4", 4, "MPEG4")
        app.preview_files = []
        app.prompt = lambda _label, _default="": ""
        app.render = lambda: None
        app.choose_bucket_and_path = lambda *_args, **_kwargs: "imported"
        app.record_failed_file = lambda *_args, **_kwargs: None
        app.init_queue_status = lambda *_args, **_kwargs: None
        app.set_queue_status = lambda *_args, **_kwargs: None
        app._download_one_with_progress = lambda *_args: (False, "Canceled.")

        app.perform_download_plan()

        pending = ia_minotaur.ia_state.load_pending(str(pending_path))
        assert pending is not None
        assert pending["identifier"] == "item1"
        assert [f["name"] for f in pending["files"]] == ["one.mp4"]
        assert pending["completed_names"] == []
        assert app.status == "Canceled.  (press R to resume)"

    def test_successful_download_stays_on_completion_screen_until_dismissed(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.results = []
        app.sel_r = 0
        app.mode = "FILES"
        app.focus = "LIST"
        app.download_log = []
        app.failed_queue = []
        app.preview_prefix = ""
        app.cur_meta = {"licenseurl": "https://creativecommons.org/licenses/by/4.0/", "rights": ""}
        app.enforce_license_gate = False
        app.preview_item = ia_minotaur.SearchResult(
            "item1",
            "Title",
            licenseurl="https://creativecommons.org/licenses/by/4.0/",
        )
        app.preview_file = ia_minotaur.IAFile("one.mp4", 4, "MPEG4")
        app.preview_files = []
        app.prompt = lambda _label, _default="": ""
        app.render = lambda: None
        app.choose_bucket_and_path = lambda *_args, **_kwargs: "imported"
        app.record_failed_file = lambda *_args, **_kwargs: None
        app.init_queue_status = lambda *_args, **_kwargs: None
        app.set_queue_status = lambda *_args, **_kwargs: None
        app._download_one_with_progress = lambda *_args: (True, "")

        app.perform_download_plan()

        assert app.mode == "DOWNLOADING"
        assert app.dl_complete_notice == "Done. Downloaded 1 file."
        assert app.status == "Done. Downloaded 1 file."

        handled = app.handle_download_complete_key(curses.KEY_ENTER)

        assert handled is True
        assert app.mode == "FILES"
        assert app.dl_complete_notice == ""
        assert app.status == "Back to files"

    def test_youtube_download_stats_final_staged_file_size(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        staged = self.stage_file("yt-abc123", "Video [abc123].mp4", b"1234567")

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.results = []
        app.sel_r = 0
        app.mode = "FILES"
        app.focus = "LIST"
        app.download_log = []
        app.failed_queue = []
        app.preview_prefix = ""
        app.cur_meta = {}
        app.enforce_license_gate = False
        app.preview_item = ia_minotaur.SearchResult(
            "yt-abc123",
            "Video",
            source="youtube",
            webpage_url="https://www.youtube.com/watch?v=abc123",
            video_id="abc123",
        )
        app.preview_file = ia_minotaur.IAFile("Video [abc123].mp4", 0, "YouTube video")
        app.preview_files = []
        app.prompt = lambda _label, _default="": ""
        app.render = lambda: None
        app.choose_bucket_and_path = lambda identifier, filename, title: f"Saved: {staged}"
        app.record_failed_file = lambda *_args, **_kwargs: None
        app._download_one_with_progress = lambda *_args: (True, "")

        app.perform_download_plan()

        assert app.queue_status[0]["size"] == 7
        assert app.preview_file is None
        assert app.dl_complete_notice == "Done. Downloaded 1 file (7B)."

    def test_download_left_in_staging_is_pending_import_not_done(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        pending_path = tmp_path / "pending.json"
        monkeypatch.setattr(ia_minotaur, "PENDING_PATH", str(pending_path))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.results = []
        app.sel_r = 0
        app.mode = "FILES"
        app.focus = "LIST"
        app.download_log = []
        app.failed_queue = []
        app.preview_prefix = ""
        app.cur_meta = {"licenseurl": "https://creativecommons.org/licenses/by/4.0/", "rights": ""}
        app.enforce_license_gate = False
        app.preview_item = ia_minotaur.SearchResult(
            "item1",
            "Title",
            licenseurl="https://creativecommons.org/licenses/by/4.0/",
        )
        app.preview_file = ia_minotaur.IAFile("one.mp4", 4, "MPEG4")
        app.preview_files = []
        app.prompt = lambda _label, _default="": ""
        app.render = lambda: None
        app.choose_bucket_and_path = lambda *_args, **_kwargs: "Left in staging: /tmp/root/.ia_staging/item1/one.mp4"
        app.record_failed_file = lambda *_args, **_kwargs: None
        app._download_one_with_progress = lambda *_args: (True, "")

        app.perform_download_plan()

        assert app.queue_status[0]["status"] == "staged"
        assert app.dl_complete_notice.startswith("Downloaded 1 file; import pending")
        pending = ia_minotaur.ia_state.load_pending(str(pending_path))
        assert pending is not None
        assert pending["identifier"] == "item1"
        assert [f["name"] for f in pending["files"]] == ["one.mp4"]
        assert pending["completed_names"] == []

    def test_import_done_requests_jellyfin_rescan_on_exit(self, monkeypatch):
        calls = []
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.download_log = []
        app.render = lambda: None
        app.jellyfin_rescan_needed = False
        monkeypatch.setattr(
            ia_minotaur.ia_jellyfin,
            "request_library_rescan",
            lambda: calls.append("rescan") or (True, "Jellyfin library rescan requested."),
        )

        app.note_import_status("done")
        app.request_jellyfin_rescan_if_needed()

        assert calls == ["rescan"]
        assert app.jellyfin_rescan_needed is False
        assert app.status == "Jellyfin library rescan requested."

    def test_staged_import_does_not_request_jellyfin_rescan(self, monkeypatch):
        calls = []
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.status = ""
        app.download_log = []
        app.render = lambda: None
        app.jellyfin_rescan_needed = False
        monkeypatch.setattr(
            ia_minotaur.ia_jellyfin,
            "request_library_rescan",
            lambda: calls.append("rescan") or (True, "Jellyfin library rescan requested."),
        )

        app.note_import_status("staged")
        app.request_jellyfin_rescan_if_needed()

        assert calls == []
        assert app.jellyfin_rescan_needed is False

    def test_download_auto_retries_after_stall(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        monkeypatch.setattr(ia_minotaur.ia_downloads, "STAGING_ROOT", ia_minotaur.STAGING_ROOT)
        monkeypatch.setattr(ia_minotaur.ia_downloads, "open_process_log", lambda: io.StringIO())
        monkeypatch.setattr(ia_minotaur, "STALL_RETRY_DELAY_S", 0)

        class FakeScreen:
            def nodelay(self, _flag):
                pass

            def getch(self):
                return -1

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.stdscr = FakeScreen()
        app.render = lambda: None
        app.status = ""

        calls = []

        def fake_run(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                return False, "Download stalled — no progress for 120s. Try again."
            path = ia_minotaur.staging_file_path("item1", "file.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"abcd")
            return True, ""

        monkeypatch.setattr(ia_minotaur.ia_downloads, "run_download_with_progress", fake_run)

        ok, msg = app._download_one_with_progress("item1", "file.mp4", 4)

        assert (ok, msg) == (True, "")
        assert len(calls) == 2

    def test_download_treats_complete_staged_file_as_success_after_stall(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        monkeypatch.setattr(ia_minotaur.ia_downloads, "STAGING_ROOT", ia_minotaur.STAGING_ROOT)
        monkeypatch.setattr(ia_minotaur.ia_downloads, "open_process_log", lambda: io.StringIO())
        monkeypatch.setattr(ia_minotaur, "STALL_RETRY_DELAY_S", 0)
        self.stage_file("item1", "file.mp4", b"abcd")

        class FakeScreen:
            def nodelay(self, _flag):
                pass

            def getch(self):
                return -1

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.stdscr = FakeScreen()
        app.render = lambda: None
        app.status = ""
        calls = []

        def fake_run(*_args, **_kwargs):
            calls.append(1)
            return False, "Download stalled — no progress for 120s. Try again."

        monkeypatch.setattr(ia_minotaur.ia_downloads, "run_download_with_progress", fake_run)

        ok, msg = app._download_one_with_progress("item1", "file.mp4", 4)

        assert (ok, msg) == (True, "")
        assert len(calls) == 1

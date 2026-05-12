"""Tests for the pure, importable helpers in ia_minotaur.py.

We only test functions that can run without a real curses terminal and
without touching the user's media root. conftest.py sets IA_MEDIA_ROOT
to a temp path before this module is imported.
"""
import io
import os

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


# ------------------------------------------------------------- TUI state
class TestRetroWaveIAState:
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

        assert "d marked/selected" in hint
        assert "f filter" in hint
        assert "? help" in hint

    def test_action_palette_groups_file_actions(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"

        labels = [label for label, _action in app.action_palette_options()]

        assert "Open / preview selected file" in labels
        assert "Download / marked files" in labels
        assert "Filter / file filter menu" in labels
        assert "Select / toggle file mark" in labels
        assert "Select / mark all visible files" in labels
        assert "Select / invert visible marks" in labels

    def test_result_palette_labels_include_search_aliases(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "RESULTS"

        labels = [label for label, _action in app.action_palette_options()]

        assert any("license" in label and "rights" in label for label in labels)
        assert any("date downloads title relevance" in label for label in labels)
        assert any("collections" in label for label in labels)

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
        app.cur_meta = {"metadata": {"licenseurl": "https://creativecommons.org/licenses/by/4.0/"}}
        app.enforce_license_gate = False
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        app.set_preview_for_marked()

        assert app.preview_prefix == "__SELECTED__"
        assert [f.name for f in app.preview_files] == ["two.mp4"]
        assert app.preview_plan_kind() == "Marked files"
        assert app.preview_file_count_and_total() == (1, 20)

    def test_file_filter_chips_show_active_filters_and_marks(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.file_kw = "mp4"
        app.video_only = True
        app.selected_file_names = {"one.mp4", "two.mp4"}

        assert app.file_filter_chips() == ["Keyword: mp4", "Video only: On", "Marked: 2"]

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
        app.save_current_file_view_state = lambda: None

        app.mark_all_visible_files()
        assert app.selected_file_names == {"one.mp4", "two.mp4"}

        app.invert_visible_file_marks()
        assert app.selected_file_names == set()

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

    def test_batch_destination_path_uses_single_folder(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        batch = {"bucket": "Movies", "folder": "Movie Folder"}

        path = app.batch_destination_path(batch, "source.mp4", "Ignored")

        assert path.endswith("/Movies/Movie Folder/Movie Folder.mp4")

    def test_movie_filename_for_folder_preserves_extension(self):
        app = RetroWaveIA.__new__(RetroWaveIA)

        assert app.movie_filename_for_folder("Chosen Movie (1934)", "archive_original.mkv") == "Chosen Movie (1934).mkv"

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
        app.file_view_state = {}
        app.selected_result = lambda: ia_minotaur.SearchResult("item1", "Title")

        app.save_current_file_view_state()

        app.file_kw = ""
        app.video_only = False
        app.sel_f = 0
        app.selected_file_names = set()
        app.restore_file_view_state("item1")

        assert app.file_kw == "mp4"
        assert app.video_only is True
        assert app.sel_f == 2
        assert app.selected_file_names == {"one.mp4"}

    def test_file_hotkey_download_works_without_list_focus(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.mode = "FILES"
        app.focus = "MENU"
        calls = []
        app.set_preview_for_marked = lambda: calls.append("download")

        handled = app.handle_files_hotkey(ord("d"))

        assert handled is True
        assert calls == ["download"]

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
        app.status = ""
        app.prompt_list = lambda _title, _options, default_idx=0: "audio"

        changed = app.choose_filter()

        assert changed is True
        assert app.filter == "audio"
        assert app.status == "Filter set to: audio"

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

    def test_set_result_filter_resets_selection_and_status(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult("one", "Metropolis")]
        app.sel_r = 3
        app.status = ""

        app.set_result_filter("metro")

        assert app.sel_r == 0
        assert "1 visible" in app.status

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
        assert calls[0] == "identifier:chaplin"
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

    def test_jump_to_result_number_without_total_results(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.results = [ia_minotaur.SearchResult("one", "One"), ia_minotaur.SearchResult("two", "Two")]
        app.result_filter = ""
        app.total_results = 0
        app.sel_r = 0

        app.jump_to_result_number(2)

        assert app.sel_r == 1
        assert app.status == "Selected result 2."

    def test_sort_labels_are_date_not_year(self):
        app = RetroWaveIA.__new__(RetroWaveIA)
        app.sort_by = "date desc"

        assert app._sort_label() == "date (new)"

    def test_session_persists_license_gate(self, monkeypatch, tmp_path):
        session_path = tmp_path / "session.json"
        monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(session_path))

        app = RetroWaveIA.__new__(RetroWaveIA)
        app.query_text = "query"
        app.filter = "movies"
        app.title_only = True
        app.page = 3
        app.sort_by = "downloads desc"
        app.enforce_license_gate = True
        app.search_history = ["query"]
        app._save_session()

        restored = RetroWaveIA.__new__(RetroWaveIA)
        restored.query_text = ""
        restored.filter = "any"
        restored.title_only = False
        restored.page = 1
        restored.sort_by = ""
        restored.enforce_license_gate = False
        restored.search_history = []
        restored._restore_session()

        assert restored.enforce_license_gate is True
        assert restored.query_text == "query"

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
        app.focus = "MENU"
        app.enforce_license_gate = False
        app.render = lambda: None

        app.do_search()

        saved = ia_minotaur.ia_state.load_session(str(session_path))
        assert saved["search_history"] == ["noir"]


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
            answer = next(answers)
            return default if answer == "" else answer

        app.prompt = prompt
        app.prompt_list = lambda _title, _options: None
        return app

    def set_roots(self, monkeypatch, tmp_path):
        root = tmp_path / "media"
        for module in (ia_minotaur, ia_paths):
            monkeypatch.setattr(module, "MEDIA_ROOT", str(root))
            monkeypatch.setattr(module, "STAGING_ROOT", str(root / ".ia_staging"))
            monkeypatch.setattr(module, "BUCKET_TV", str(root / "TV"))
            monkeypatch.setattr(module, "BUCKET_MOVIES", str(root / "Movies"))
            monkeypatch.setattr(module, "BUCKET_MUSIC", str(root / "Music"))
            monkeypatch.setattr(module, "BUCKET_OTHER", str(root / "Other"))
            monkeypatch.setattr(module, "LOG_PATH", str(root / ".ia_dl.log"))
        return root

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

        msg = app.choose_bucket_and_path("item1", "The.Big.Movie.1999.1080p.mkv", "")

        final_path = root / "Movies" / "The Big Movie (1999)" / "The Big Movie (1999).mkv"
        assert msg == f"Saved: {final_path}"
        assert final_path.read_bytes() == b"x"

    def test_movie_import_uses_chosen_folder_as_filename(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "ia_file_12345.mp4")
        app = self.build_app(["Movies", "Custom Movie (1942)"])

        msg = app.choose_bucket_and_path("item1", "ia_file_12345.mp4", "Messy Source")

        final_path = root / "Movies" / "Custom Movie (1942)" / "Custom Movie (1942).mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.read_bytes() == b"x"

    def test_tv_import_renames_detected_episode(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        self.stage_file("item1", "pilot.S02E03.mp4")
        app = self.build_app(["TV", "A Show"])

        msg = app.choose_bucket_and_path("item1", "pilot.S02E03.mp4", "A Show")

        final_path = root / "TV" / "A Show" / "Season 02" / "A Show - S02E03.mp4"
        assert msg == f"Saved: {final_path}"
        assert final_path.exists()

    def test_duplicate_movie_gets_timestamp_suffix(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        final_path = root / "Movies" / "Movie (2001)" / "Movie (2001).mp4"
        os.makedirs(final_path.parent, exist_ok=True)
        final_path.write_bytes(b"old")
        self.stage_file("item1", "Movie.2001.mp4", b"new")
        monkeypatch.setattr(ia_minotaur.time, "strftime", lambda _fmt: "20260102_030405")
        app = self.build_app(["Movies", ""])

        msg = app.choose_bucket_and_path("item1", "Movie.2001.mp4", "")

        stamped = root / "Movies" / "Movie (2001)" / "Movie (2001)_20260102_030405.mp4"
        assert msg == f"Saved: {stamped}"
        assert final_path.read_bytes() == b"old"
        assert stamped.read_bytes() == b"new"

    def test_traversal_filename_is_left_in_staging(self, monkeypatch, tmp_path):
        self.set_roots(monkeypatch, tmp_path)
        staging_path = self.stage_file("item1", "../../../escape.mp4")
        app = self.build_app(["Other", "Misc"])

        msg = app.choose_bucket_and_path("item1", "../../../escape.mp4", "Title")

        assert msg.startswith("Refused: staging path escapes item staging dir:")
        assert os.path.exists(staging_path)

    def test_missing_staging_file_reports_without_creating_destination(self, monkeypatch, tmp_path):
        root = self.set_roots(monkeypatch, tmp_path)
        app = self.build_app([])

        msg = app.choose_bucket_and_path("item1", "missing.mp4", "Title")

        assert msg.startswith("Downloaded, but staging file not found:")
        assert not (root / "Movies").exists()

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

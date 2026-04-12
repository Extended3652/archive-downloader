"""Tests for FavoritesStore — isolated, no curses needed."""
import json

import pytest

from ia_common import IAFile, SearchResult
from ia_minotaur import FavoritesStore


@pytest.fixture
def store(tmp_path):
    """A FavoritesStore backed by a temp JSON file."""
    return FavoritesStore(str(tmp_path / "favs.json"))


# --------------------------------------------------------- load / save
class TestLoadSave:
    def test_fresh_store_has_empty_collections(self, store):
        assert store.data["items"] == []
        assert store.data["files"] == []
        assert store.data["folders"] == {"TV": [], "Movies": [], "Other": []}

    def test_save_and_reload(self, store):
        store.data["items"].append({"identifier": "x", "title": "X"})
        store.save()
        store2 = FavoritesStore(store.path)
        assert len(store2.data["items"]) == 1
        assert store2.data["items"][0]["identifier"] == "x"

    def test_corrupt_json_returns_base(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json!!!")
        s = FavoritesStore(str(p))
        assert s.data["items"] == []

    def test_missing_file_returns_base(self, tmp_path):
        s = FavoritesStore(str(tmp_path / "nonexistent.json"))
        assert s.data["items"] == []

    def test_partial_json_fills_missing_keys(self, tmp_path):
        p = tmp_path / "partial.json"
        p.write_text(json.dumps({"items": [{"identifier": "a"}]}))
        s = FavoritesStore(str(p))
        assert len(s.data["items"]) == 1
        assert s.data["files"] == []
        assert "TV" in s.data["folders"]

    def test_save_silently_fails_if_parent_missing(self, tmp_path):
        s = FavoritesStore(str(tmp_path / "nope" / "favs.json"))
        s.data["items"].append({"identifier": "z"})
        s.save()  # should not raise


# ------------------------------------------------------------ items
class TestItems:
    def test_is_fav_item_empty(self, store):
        assert not store.is_fav_item("anything")

    def test_toggle_adds_then_removes(self, store):
        r = SearchResult(identifier="id1", title="Title 1", year="2020", creator="Author")
        msg = store.toggle_fav_item(r)
        assert msg == "Added favorite item."
        assert store.is_fav_item("id1")

        msg = store.toggle_fav_item(r)
        assert msg == "Removed favorite item."
        assert not store.is_fav_item("id1")

    def test_toggle_empty_identifier_noop(self, store):
        r = SearchResult(identifier="", title="T")
        assert store.toggle_fav_item(r) == ""

    def test_toggle_persists_to_disk(self, store):
        r = SearchResult(identifier="id2", title="T")
        store.toggle_fav_item(r)
        store2 = FavoritesStore(store.path)
        assert store2.is_fav_item("id2")

    def test_is_fav_item_strips_whitespace(self, store):
        r = SearchResult(identifier="  id3  ", title="T")
        store.toggle_fav_item(r)
        assert store.is_fav_item("id3")
        assert store.is_fav_item("  id3  ")

    def test_remove_item(self, store):
        store.toggle_fav_item(SearchResult(identifier="a", title="A"))
        store.toggle_fav_item(SearchResult(identifier="b", title="B"))
        # insert(0, ...) means b is first
        name = store.remove_item(0)
        assert name == "B"
        assert len(store.data["items"]) == 1
        assert store.data["items"][0]["identifier"] == "a"

    def test_remove_item_out_of_range(self, store):
        assert store.remove_item(0) is None
        assert store.remove_item(-1) is None


# ------------------------------------------------------------- files
class TestFiles:
    def test_is_fav_file_empty(self, store):
        assert not store.is_fav_file("id", "file.mp4")

    def test_toggle_file_adds_and_removes(self, store):
        item = SearchResult(identifier="id", title="Title", year="2020", creator="C")
        f = IAFile(name="file.mp4", size=1024, fmt="h264")
        msg = store.toggle_fav_file(item, f)
        assert msg == "Added favorite file."
        assert store.is_fav_file("id", "file.mp4")

        msg = store.toggle_fav_file(item, f)
        assert msg == "Removed favorite file."
        assert not store.is_fav_file("id", "file.mp4")

    def test_toggle_file_empty_ident_noop(self, store):
        item = SearchResult(identifier="", title="T")
        f = IAFile(name="f.mp4", size=0)
        assert store.toggle_fav_file(item, f) == ""

    def test_toggle_file_empty_filename_noop(self, store):
        item = SearchResult(identifier="id", title="T")
        f = IAFile(name="", size=0)
        assert store.toggle_fav_file(item, f) == ""

    def test_file_fav_key_format(self):
        assert FavoritesStore.file_fav_key("a", "b") == "a::b"
        assert FavoritesStore.file_fav_key("  a  ", "  b  ") == "a::b"

    def test_remove_file(self, store):
        item = SearchResult(identifier="id", title="T")
        f = IAFile(name="f1.mp4", size=0)
        store.toggle_fav_file(item, f)
        name = store.remove_file(0)
        assert name == "f1.mp4"
        assert len(store.data["files"]) == 0

    def test_remove_file_out_of_range(self, store):
        assert store.remove_file(5) is None


# ----------------------------------------------------------- folders
class TestFolders:
    def test_add_folder_fav(self, store):
        store.add_folder_fav("TV", "Breaking Bad")
        assert "Breaking Bad" in store.folders("TV")

    def test_add_folder_dedup_case_insensitive(self, store):
        store.add_folder_fav("TV", "Show")
        store.add_folder_fav("TV", "show")
        assert len(store.folders("TV")) == 1

    def test_add_folder_unknown_bucket_falls_to_other(self, store):
        store.add_folder_fav("Music", "Artist")
        assert "Artist" in store.folders("Other")

    def test_add_folder_max_limit(self, store):
        for i in range(35):
            store.add_folder_fav("TV", f"Show{i}")
        assert len(store.folders("TV")) == 30

    def test_folders_empty(self, store):
        assert store.folders("TV") == []

    def test_flat_folders(self, store):
        store.add_folder_fav("TV", "Show A")
        store.add_folder_fav("Movies", "Film B")
        flat = store.flat_folders()
        assert len(flat) == 2
        assert flat[0] == ("TV", "Show A")
        assert flat[1] == ("Movies", "Film B")

    def test_remove_folder(self, store):
        store.add_folder_fav("TV", "Show A")
        store.add_folder_fav("Movies", "Film B")
        name = store.remove_folder(0)
        assert name == "Show A"
        assert len(store.flat_folders()) == 1

    def test_remove_folder_out_of_range(self, store):
        assert store.remove_folder(5) is None

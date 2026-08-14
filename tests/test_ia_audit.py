import json
import os
import stat
import tempfile

import ia_audit
from ia_audit import (
    apply_move_plan,
    apply_rename_plan,
    ProbeInfo,
    analyze_library,
    bitrate_bucket,
    build_rename_plan,
    build_triage_move_plan,
    build_duplicate_keys,
    cleaned_movie_basename,
    detect_metadata_issues,
    load_plan,
    load_rename_plan,
    looks_weird_filename,
    parse_media_entry,
    prompt_manual_triage_plan,
    rename_suggestion,
    triage_suggestion_for_entry,
    unresolved_triage_suggestions,
    validate_rename_plan,
)


class TestLooksWeirdFilename:
    def test_main_sets_process_umask(self, monkeypatch, tmp_path, capsys):
        calls = []
        monkeypatch.setattr(ia_audit, "set_process_umask", lambda: calls.append("umask"))

        assert ia_audit.main(["--root", str(tmp_path), "--json"]) == 0
        assert calls == ["umask"]

    def test_scene_tags_flag_weird_name(self):
        reasons = looks_weird_filename("Movie.1999.1080p.BluRay.x264-YIFY.mkv")
        assert "release/sample tag" in reasons or "scene tags remain" in reasons

    def test_clean_name_not_flagged(self):
        assert looks_weird_filename("The Movie (1999).mkv") == []

    def test_generic_stem_flagged(self):
        assert "generic stem" in looks_weird_filename("video12.mp4")


class TestRenameSuggestions:
    def test_cleaned_movie_basename(self):
        assert (
            cleaned_movie_basename("Lost.in.Translation.2003.1080p.BluRay.x264.YIFY.mp4")
            == "Lost in Translation (2003).mp4"
        )

    def test_movie_rename_suggestion(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Movies", "Lost in Translation (2003)", "Lost.in.Translation.2003.1080p.BluRay.x264.YIFY.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            assert rename_suggestion(entry) == "Lost in Translation (2003).mp4"

    def test_movie_rename_prefers_folder_title_for_scene_group_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Movies", "Life During Wartime (2009)", "aaf-life.during.wartime.2009.dvdrip.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            assert rename_suggestion(entry) == "Life During Wartime (2009).mp4"

    def test_movie_rename_prefers_folder_title_for_low_information_filename(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Movies", "The 5,000 Fingers of Dr. T (1953)", "5000_FINGERS_OF_DR_T_T1.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            assert rename_suggestion(entry) == "The 5,000 Fingers of Dr. T (1953).mp4"

    def test_build_rename_plan(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Lost in Translation (2003)")
            os.makedirs(movie_dir, exist_ok=True)
            src = os.path.join(movie_dir, "Lost.in.Translation.2003.1080p.BluRay.x264.YIFY.mp4")
            with open(src, "wb") as fh:
                fh.write(b"x")
            plan = build_rename_plan(
                root,
                [{"path": os.path.relpath(src, root), "suggested_name": "Lost in Translation (2003).mp4"}],
            )
            assert plan[0]["from"].endswith("Lost.in.Translation.2003.1080p.BluRay.x264.YIFY.mp4")
            assert plan[0]["to"].endswith("Lost in Translation (2003).mp4")
            assert not plan[0]["collides"]
            assert not plan[0]["duplicate_target"]

    def test_build_rename_plan_detects_collision(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Movie (1999)")
            os.makedirs(movie_dir, exist_ok=True)
            src = os.path.join(movie_dir, "Movie.1999.1080p.mkv")
            dst = os.path.join(movie_dir, "Movie (1999).mkv")
            for path in (src, dst):
                with open(path, "wb") as fh:
                    fh.write(b"x")
            plan = build_rename_plan(
                root,
                [{"path": os.path.relpath(src, root), "suggested_name": "Movie (1999).mkv"}],
            )
            assert plan[0]["collides"]

    def test_validate_rename_plan_blocks_existing_target(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Movie (1999)")
            os.makedirs(movie_dir, exist_ok=True)
            src = os.path.join(movie_dir, "Movie.1999.1080p.mkv")
            dst = os.path.join(movie_dir, "Movie (1999).mkv")
            for path in (src, dst):
                with open(path, "wb") as fh:
                    fh.write(b"x")
            validated = validate_rename_plan(
                root,
                [{"from": os.path.relpath(src, root), "to": os.path.relpath(dst, root)}],
            )
            assert validated[0]["status"] == "blocked"
            assert "target already exists" in validated[0]["reasons"]

    def test_apply_rename_plan_dry_run(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Movie (1999)")
            os.makedirs(movie_dir, exist_ok=True)
            src = os.path.join(movie_dir, "Movie.1999.1080p.mkv")
            dst = os.path.join(movie_dir, "Movie (1999).mkv")
            with open(src, "wb") as fh:
                fh.write(b"x")
            result = apply_rename_plan(
                root,
                [{"from": os.path.relpath(src, root), "to": os.path.relpath(dst, root)}],
                execute=False,
            )
            assert result["renamed"] == 0
            assert result["results"][0]["status"] == "dry-run"
            assert os.path.exists(src)
            assert not os.path.exists(dst)

    def test_apply_rename_plan_execute(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Movie (1999)")
            os.makedirs(movie_dir, exist_ok=True)
            src = os.path.join(movie_dir, "Movie.1999.1080p.mkv")
            dst = os.path.join(movie_dir, "Movie (1999).mkv")
            with open(src, "wb") as fh:
                fh.write(b"x")
            result = apply_rename_plan(
                root,
                [{"from": os.path.relpath(src, root), "to": os.path.relpath(dst, root)}],
                execute=True,
            )
            assert result["renamed"] == 1
            assert result["results"][0]["status"] == "renamed"
            assert not os.path.exists(src)
            assert os.path.exists(dst)

    def test_load_rename_plan(self):
        with tempfile.TemporaryDirectory() as root:
            plan_path = os.path.join(root, "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump([{"from": "a", "to": "b"}], fh)
            loaded = load_rename_plan(plan_path)
            assert loaded == [{"from": "a", "to": "b"}]


class TestParseMediaEntry:
    def test_tv_episode_from_filename(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "TV", "Show Name", "Season 01", "Show Name - S01E02.mkv")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            assert entry.bucket == "TV"
            assert entry.show == "Show Name"
            assert (entry.season, entry.episode) == (1, 2)

    def test_movie_year_from_folder(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Movies", "Metropolis (1927)", "Metropolis (1927).mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            assert entry.bucket == "Movies"
            assert entry.movie_title == "Metropolis (1927)"
            assert entry.movie_year == "1927"

    def test_movie_folder_title_preserves_punctuation_when_year_present(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Movies", "The 5,000 Fingers of Dr. T (1953)", "clip.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            assert entry.movie_title == "The 5,000 Fingers of Dr. T (1953)"
            assert entry.movie_year == "1953"


class TestTriageSuggestions:
    def test_other_video_with_year_suggests_movies(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Other", "Video", "The Langoliers (1995)", "clip.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            suggestion = triage_suggestion_for_entry(entry)
            assert suggestion is not None
            assert suggestion.suggested_bucket == "Movies"
            assert suggestion.confidence == "high"

    def test_hidden_bucket_suggests_recovered_other(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, ".temp_disc", "clip.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            suggestion = triage_suggestion_for_entry(entry)
            assert suggestion is not None
            assert suggestion.suggested_bucket == "Other"
            assert suggestion.confidence == "medium"

    def test_build_triage_move_plan_filters_low_confidence(self):
        with tempfile.TemporaryDirectory() as root:
            suggestions = [
                {
                    "path": "Other/Video/Movie (1999)/clip.mp4",
                    "suggested_bucket": "Movies",
                    "suggested_folder": "Movie (1999)",
                    "suggested_name": "Movie (1999).mp4",
                    "confidence": "high",
                    "reason": "video file with movie-style year/title",
                },
                {
                    "path": "Other/Misc/odd.mov",
                    "suggested_bucket": "Other",
                    "suggested_folder": "Misc",
                    "suggested_name": "odd.mov",
                    "confidence": "low",
                    "reason": "manual classification needed",
                },
            ]
            plan = build_triage_move_plan(root, suggestions)
            assert len(plan) == 1
            assert plan[0]["to"] == os.path.join("Movies", "Movie (1999)", "Movie (1999).mp4")

    def test_unresolved_triage_suggestions_filters_low_confidence(self):
        suggestions = [
            {"path": "a", "confidence": "low"},
            {"path": "b", "confidence": "high"},
        ]
        assert unresolved_triage_suggestions(suggestions) == [{"path": "a", "confidence": "low"}]

    def test_prompt_manual_triage_plan_builds_plan(self):
        with tempfile.TemporaryDirectory() as root:
            suggestions = [
                {
                    "path": "Other/Misc/item.mov",
                    "suggested_bucket": "Other",
                    "suggested_folder": "Misc",
                    "suggested_name": "item.mov",
                    "confidence": "low",
                    "reason": "manual classification needed",
                }
            ]
            answers = iter(["m", "Chosen Movie (1999)", "Chosen Movie (1999).mov"])
            lines = []
            plan = prompt_manual_triage_plan(
                root,
                suggestions,
                input_fn=lambda _prompt: next(answers),
                output_fn=lines.append,
            )
            assert len(plan) == 1
            assert plan[0]["to"] == os.path.join("Movies", "Chosen Movie (1999)", "Chosen Movie (1999).mov")
            assert any("Manual triage candidates: 1" in line for line in lines)

    def test_prompt_manual_triage_plan_skip(self):
        with tempfile.TemporaryDirectory() as root:
            suggestions = [
                {
                    "path": "Other/Misc/item.mov",
                    "suggested_bucket": "Other",
                    "suggested_folder": "Misc",
                    "suggested_name": "item.mov",
                    "confidence": "low",
                    "reason": "manual classification needed",
                }
            ]
            answers = iter(["s"])
            plan = prompt_manual_triage_plan(
                root,
                suggestions,
                input_fn=lambda _prompt: next(answers),
                output_fn=lambda _line: None,
            )
            assert plan == []

    def test_apply_move_plan_execute(self):
        with tempfile.TemporaryDirectory() as root:
            src_dir = os.path.join(root, "Other", "Video", "Movie (1999)")
            dst_dir = os.path.join(root, "Movies", "Movie (1999)")
            os.makedirs(src_dir, exist_ok=True)
            src = os.path.join(src_dir, "clip.mp4")
            dst = os.path.join(dst_dir, "Movie (1999).mp4")
            with open(src, "wb") as fh:
                fh.write(b"x")
            os.chmod(src, 0o644)
            result = apply_move_plan(
                root,
                [{"from": os.path.relpath(src, root), "to": os.path.relpath(dst, root)}],
                execute=True,
            )
            assert result["moved"] == 1
            assert result["results"][0]["status"] == "moved"
            assert not os.path.exists(src)
            assert os.path.exists(dst)
            assert stat.S_IMODE(os.stat(dst_dir).st_mode) == 0o2775
            assert stat.S_IMODE(os.stat(dst).st_mode) == 0o664

    def test_apply_rename_plan_execute_normalizes_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Movie (1999)")
            os.makedirs(movie_dir, exist_ok=True)
            src = os.path.join(movie_dir, "clip.mp4")
            dst = os.path.join(movie_dir, "Movie (1999).mp4")
            with open(src, "wb") as fh:
                fh.write(b"x")
            os.chmod(movie_dir, 0o2755)
            os.chmod(src, 0o644)

            result = apply_rename_plan(
                root,
                [{"from": os.path.relpath(src, root), "to": os.path.relpath(dst, root)}],
                execute=True,
            )

            assert result["renamed"] == 1
            assert not os.path.exists(src)
            assert os.path.exists(dst)
            assert stat.S_IMODE(os.stat(movie_dir).st_mode) == 0o2775
            assert stat.S_IMODE(os.stat(dst).st_mode) == 0o664

    def test_load_plan(self):
        with tempfile.TemporaryDirectory() as root:
            plan_path = os.path.join(root, "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump([{"from": "a", "to": "b"}], fh)
            loaded = load_plan(plan_path)
            assert loaded == [{"from": "a", "to": "b"}]


class TestMetadataIssues:
    def test_tv_missing_episode_pattern(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "TV", "Show Name", "Season 01", "episode-two.mkv")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            issues = detect_metadata_issues(entry)
            assert "missing SxxEyy pattern" in issues

    def test_other_bucket_is_flagged(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Other", "Misc", "odd.mp4")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            issues = detect_metadata_issues(entry)
            assert "unclassified media in Other" in issues

    def test_podcasts_bucket_not_flagged_as_nonstandard(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "Podcasts", "Show", "episode.mp3")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x")
            entry = parse_media_entry(root, path)
            issues = detect_metadata_issues(entry)
            assert not any("nonstandard top-level bucket" in issue for issue in issues)


class TestDuplicateKeys:
    def test_duplicate_episode_grouping(self):
        with tempfile.TemporaryDirectory() as root:
            p1 = os.path.join(root, "TV", "Show", "Season 01", "Show - S01E01.mkv")
            p2 = os.path.join(root, "TV", "Show", "Season 01", "Show - S01E01.mp4")
            os.makedirs(os.path.dirname(p1), exist_ok=True)
            for path in (p1, p2):
                with open(path, "wb") as fh:
                    fh.write(b"x")
            entries = [parse_media_entry(root, p1), parse_media_entry(root, p2)]
            episode_map, movie_map = build_duplicate_keys(entries)
            assert len(episode_map["show|S01E01"]) == 2
            assert movie_map == {}


class TestBitrateBucket:
    def test_unknown(self):
        assert bitrate_bucket(0) == "unknown"

    def test_midrange(self):
        assert bitrate_bucket(3_000_000) == "2-4 Mbps"


class TestAnalyzeLibrary:
    def test_report_collects_core_sections(self):
        with tempfile.TemporaryDirectory() as root:
            tv_dir = os.path.join(root, "TV", "Show", "Season 01")
            movie_dir = os.path.join(root, "Movies", "Loose Movie")
            staging_dir = os.path.join(root, ".ia_staging", "item1")
            os.makedirs(tv_dir, exist_ok=True)
            os.makedirs(movie_dir, exist_ok=True)
            os.makedirs(staging_dir, exist_ok=True)

            with open(os.path.join(tv_dir, "Show.S01E01.1080p.mkv"), "wb") as fh:
                fh.write(b"x" * 10)
            with open(os.path.join(tv_dir, "Show.S01E01.mp4"), "wb") as fh:
                fh.write(b"x" * 10)
            with open(os.path.join(movie_dir, "video12.mp4"), "wb") as fh:
                fh.write(b"x" * 10)
            stale_path = os.path.join(staging_dir, "leftover.part")
            with open(stale_path, "wb") as fh:
                fh.write(b"x")
            os.utime(stale_path, (1, 1))

            def fake_probe(_path: str) -> ProbeInfo:
                return ProbeInfo(video_codec="mpeg4", audio_codec="dca", bit_rate=3_000_000)

            report = analyze_library(root, probe=True, max_probe=5, stale_days=14, probe_runner=fake_probe)
            assert report["summary"]["media_files"] == 3
            assert report["summary"]["duplicate_episodes"] == 1
            assert report["summary"]["cleanup_candidates"] >= 1
            assert report["summary"]["rename_suggestions"] >= 1
            assert len(report["rename_plan"]) >= 1
            assert report["codec_report"]["transcode_risks"]
            assert report["bitrate_heatmap"]["2-4 Mbps"] == 3

    def test_subtitles_not_cleanup_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Movie (1999)")
            os.makedirs(movie_dir, exist_ok=True)
            with open(os.path.join(movie_dir, "Movie (1999).mp4"), "wb") as fh:
                fh.write(b"x")
            subtitle = os.path.join(movie_dir, "Movie (1999).en.srt")
            with open(subtitle, "wb") as fh:
                fh.write(b"x")
            os.utime(subtitle, (1, 1))
            report = analyze_library(root, probe=False, stale_days=14)
            assert report["summary"]["cleanup_candidates"] == 0

    def test_json_serializable(self):
        with tempfile.TemporaryDirectory() as root:
            movie_dir = os.path.join(root, "Movies", "Metropolis (1927)")
            os.makedirs(movie_dir, exist_ok=True)
            with open(os.path.join(movie_dir, "Metropolis (1927).mp4"), "wb") as fh:
                fh.write(b"x")
            report = analyze_library(root, probe=False)
            json.dumps(report)

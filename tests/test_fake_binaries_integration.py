"""Integration-style tests using fake ia/curl executables on PATH."""
import os
import stat

import ia_dl
import ia_minotaur


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def install_fake_ia(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    args_path = tmp_path / "ia-download-args.txt"
    fake_ia = bindir / "ia"
    write_executable(
        fake_ia,
        f"""#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "ia 9.9.9"
  exit 0
fi
if [ "$1" = "search" ]; then
  printf '%s\\n' '{{"identifier":"movie_one","title":"Movie One","year":"1945","creator":"Director"}}'
  printf '%s\\n' 'not-json'
  printf '%s\\n' '{{"identifier":"movie_two","title":"Movie Two"}}'
  exit 0
fi
if [ "$1" = "metadata" ]; then
  cat <<'JSON'
{{"files":[
  {{"name":"small.txt","size":"3","format":"Text"}},
  {{"name":"big.mp4","size":"200","format":"MPEG4"}},
  {{"name":"small.mp4","size":"10","format":"MPEG4"}}
]}}
JSON
  exit 0
fi
if [ "$1" = "download" ]; then
  printf '%s\\n' "$@" > "{args_path}"
  exit 0
fi
echo "unexpected ia args: $*" >&2
exit 2
""",
    )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return args_path


def install_fake_curl(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake_curl = bindir / "curl"
    write_executable(
        fake_curl,
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "curl 9.9.9"
  exit 0
fi
cat <<'JSON'
{"response":{"numFound":1,"docs":[{"identifier":"curl_item","title":"Curl Item","year":"1930","mediatype":"movies","downloads":1200,"licenseurl":"https://creativecommons.org/licenses/by/4.0/"}]}}
JSON
exit 0
""",
    )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")


def test_ia_dl_search_uses_fake_curl_on_path(tmp_path, monkeypatch, capsys):
    install_fake_curl(tmp_path, monkeypatch)

    rc = ia_dl.main(["search", "title:test", "--rows", "5"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "curl_item\tCurl Item (1930)\tmovies | 1.2K dl | lic:open" in captured.out


def test_ia_dl_list_uses_fake_metadata_command(tmp_path, monkeypatch, capsys):
    install_fake_ia(tmp_path, monkeypatch)

    rc = ia_dl.main(["list", "movie_one", "--ext", "mp4"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "big.mp4" in captured.out
    assert "small.mp4" in captured.out
    assert "small.txt" not in captured.out


def test_ia_dl_download_biggest_invokes_fake_download(tmp_path, monkeypatch, capsys):
    args_path = install_fake_ia(tmp_path, monkeypatch)
    dest = tmp_path / "downloads"

    rc = ia_dl.main(["download", "movie_one", "--ext", "mp4", "--biggest", "--dest", str(dest)])

    captured = capsys.readouterr()
    assert rc == 0
    assert "Auto-selecting biggest: big.mp4" in captured.out
    assert args_path.read_text(encoding="utf-8").splitlines() == [
        "download",
        "movie_one",
        "--destdir",
        str(dest),
        "--files",
        "big.mp4",
    ]


def test_minotaur_check_uses_fake_ia_and_curl(tmp_path, monkeypatch, capsys):
    install_fake_ia(tmp_path, monkeypatch)
    install_fake_curl(tmp_path, monkeypatch)
    root = tmp_path / "media"
    monkeypatch.setattr(ia_minotaur, "MEDIA_ROOT", str(root))
    monkeypatch.setattr(ia_minotaur, "STAGING_ROOT", str(root / ".ia_staging"))
    monkeypatch.setattr(ia_minotaur, "BUCKET_TV", str(root / "TV"))
    monkeypatch.setattr(ia_minotaur, "BUCKET_MOVIES", str(root / "Movies"))
    monkeypatch.setattr(ia_minotaur, "BUCKET_MUSIC", str(root / "Music"))
    monkeypatch.setattr(ia_minotaur, "BUCKET_OTHER", str(root / "Other"))
    monkeypatch.setattr(ia_minotaur, "LOG_PATH", str(root / ".ia_dl.log"))
    monkeypatch.setattr(ia_minotaur, "SESSION_PATH", str(tmp_path / "home" / ".session.json"))
    monkeypatch.setattr(ia_minotaur, "PENDING_PATH", str(tmp_path / "home" / ".pending.json"))

    rc = ia_minotaur.cli_main(["--check"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "OK    ia CLI" in captured.out
    assert "ia 9.9.9" in captured.out
    assert "OK    curl" in captured.out
    assert "curl 9.9.9" in captured.out

"""yt-dlp command construction and staging helpers."""
import os
from typing import List, Optional

from ia_paths import STAGING_ROOT, staging_identifier_dir

YT_OUTPUT_TEMPLATE = "%(title).180B [%(id)s].%(ext)s"
YT_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"


def youtube_staging_dir(identifier: str) -> str:
    return staging_identifier_dir(identifier)


def single_video_download_cmd(yt_dlp_path: str, url: str, identifier: str) -> List[str]:
    return [
        yt_dlp_path,
        "-f",
        YT_FORMAT,
        "--merge-output-format",
        "mp4",
        "-P",
        STAGING_ROOT,
        "-o",
        f"{identifier}/{YT_OUTPUT_TEMPLATE}",
        "--no-playlist",
        url,
    ]


def display_filename(title: str, video_id: str) -> str:
    title_part = " ".join(str(title or "video").split()).strip() or "video"
    title_part = title_part.replace("/", " ").replace("\\", " ").strip() or "video"
    if len(title_part.encode("utf-8", "ignore")) > 180:
        title_part = title_part[:180].rstrip()
    id_part = str(video_id or "unknown").strip() or "unknown"
    return f"{title_part} [{id_part}].mp4"


def find_downloaded_video_file(identifier: str, video_id: str) -> Optional[str]:
    root = youtube_staging_dir(identifier)
    id_part = str(video_id or "").strip()
    if not id_part or not os.path.isdir(root):
        return None
    matches = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            if f"[{id_part}]" not in name:
                continue
            path = os.path.join(base, name)
            matches.append((os.path.getmtime(path), os.path.relpath(path, root)))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]

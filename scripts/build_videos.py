import os
import sys
import time
import base64
import subprocess
from pathlib import Path

import requests
import boto3
from botocore.config import Config

RECITATION_ID = 8
QF_CLIENT_ID = os.environ["QF_CLIENT_ID"]
QF_CLIENT_SECRET = os.environ["QF_CLIENT_SECRET"]
R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "quran-tiktok-videos")

START_PAGE = int(os.environ.get("START_PAGE", "1"))
END_PAGE = int(os.environ.get("END_PAGE", "3"))

WORK_DIR = Path("work")
WORK_DIR.mkdir(exist_ok=True)

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


def get_token():
    pair = f"{QF_CLIENT_ID}:{QF_CLIENT_SECRET}"
    basic = base64.b64encode(pair.encode()).decode()
    resp = requests.post(
        "https://oauth2.quran.foundation/oauth2/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "client_credentials", "scope": "content"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"], time.time()


def download_page_audio(page, token):
    uri = (
        f"https://apis.quran.foundation/content/api/v4/recitations/"
        f"{RECITATION_ID}/by_page/{page}?per_page=50&fields=chapter_id,verse_number,verse_key,juz_number"
    )
    resp = requests.get(
        uri,
        headers={"x-auth-token": token, "x-client-id": QF_CLIENT_ID},
    )
    resp.raise_for_status()
    audio_files = resp.json()["audio_files"]

    clip_paths = []
    for i, item in enumerate(audio_files):
        clip_path = WORK_DIR / f"{page:03d}_{i:02d}.mp3"
        url = "https://verses.quran.foundation/" + item["url"]
        r = requests.get(url)
        r.raise_for_status()
        clip_path.write_bytes(r.content)
        clip_paths.append(clip_path)
    return clip_paths


def stitch_audio(page, clip_paths):
    list_path = WORK_DIR / f"{page:03d}_list.txt"
    with open(list_path, "w") as f:
        for p in sorted(clip_paths):
            f.write(f"file '{p.resolve().as_posix()}'\n")

    out_mp3 = WORK_DIR / f"{page:03d}.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(out_mp3)],
        check=True,
    )
    return out_mp3


def merge_video(page, audio_path):
    png_path = Path("pages_png") / f"{page:03d}.png"
    out_mp4 = WORK_DIR / f"{page:03d}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(png_path),
            "-i", str(audio_path),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=white",
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            str(out_mp4),
        ],
        check=True,
    )
    return out_mp4


def upload_video(page, mp4_path):
    key = f"videos/{page:03d}.mp4"
    s3.upload_file(str(mp4_path), R2_BUCKET_NAME, key)
    return key


def cleanup(page):
    for p in WORK_DIR.glob(f"{page:03d}*"):
        p.unlink(missing_ok=True)


def process_page(page, token):
    for attempt in range(1, 4):
        try:
            clips = download_page_audio(page, token)
            audio = stitch_audio(page, clips)
            video = merge_video(page, audio)
            key = upload_video(page, video)
            cleanup(page)
            print(f"[page {page:03d}] uploaded -> {key}")
            return True
        except Exception as e:
            print(f"[page {page:03d}] attempt {attempt} failed: {e}")
            time.sleep(3)
    print(f"[page {page:03d}] GAVE UP after 3 attempts")
    return False


def main():
    token, token_time = get_token()
    failed = []
    for page in range(START_PAGE, END_PAGE + 1):
        if time.time() - token_time > 3000:
            token, token_time = get_token()
        ok = process_page(page, token)
        if not ok:
            failed.append(page)

    print("---")
    print(f"Done. Range {START_PAGE}-{END_PAGE}.")
    if failed:
        print(f"FAILED PAGES: {failed}")
        sys.exit(1)
    else:
        print("All pages succeeded.")


if __name__ == "__main__":
    main()

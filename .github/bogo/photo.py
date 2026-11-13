"""사진 바꾸기 이슈를 읽어 액자 사진을 바꾼다(.github/workflows/photo.yml이 실행).

저장소 주인·협력자가 올린 것만 반영한다. 사진은 images/<번호>.jpg로 저장하고(긴 쪽 2048 이하),
'되돌리기'를 고르면 처음 넣어 둔 번호 그림(.github/bogo/numbers)으로 돌린다. 결과는 이슈 답글로 남긴다.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IMAGES = ROOT / "images"
ALLOWED = {"OWNER", "MEMBER", "COLLABORATOR"}
EXTS = [".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"]
MAX_BYTES = 30 * 1024 * 1024


def numbers():
    data = json.loads((HERE / "slots.json").read_text(encoding="utf-8"))
    out = []
    for s in data["slots"]:
        out += s["parts"] if "parts" in s else [s["name"]]
    return out


def section(body, title):
    m = re.search(r"###\s*" + re.escape(title) + r"\s*\n(.*?)(?=\n###\s|\Z)", body, re.S)
    return m.group(1).strip() if m else ""


def image_url(text):
    for pat in (r"!\[[^\]]*\]\((https://[^)\s]+)\)", r'<img[^>]+src="(https://[^"]+)"', r"(https://github\.com/user-attachments/assets/[\w-]+)", r"(https://\S+?\.(?:png|jpe?g|webp|gif|heic))"):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return None


def download(url, token, path):
    req = urllib.request.Request(url, headers={"User-Agent": "bogo-data"})
    if token and url.startswith("https://github.com/"):
        # 깃허브 첨부는 인증 뒤 다른 주소로 넘겨주므로, 넘어간 곳에는 인증을 보내지 않는다
        req.add_unredirected_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("too big")
    path.write_bytes(data)


def clear_other(frame, keep):
    for p in IMAGES.iterdir():
        if p.is_file() and p.stem.strip().lower() == frame and p.suffix.lower() in EXTS and p.name != keep:
            p.unlink()


def run():
    body = os.environ.get("ISSUE_BODY", "")
    if os.environ.get("AUTHOR_ASSOC", "") not in ALLOWED:
        return None, "이 저장소에 수정 권한이 있는 사람만 사진을 바꿀 수 있습니다. 저장소 주인에게 초대를 받아 주세요."
    raw = section(body, "액자 번호")
    frame = re.sub(r"[^0-9]", "", raw)
    if not frame or frame not in numbers():
        return None, f"액자 번호 '{raw}'를 찾지 못했습니다. 사진 관리 페이지에서 [사진 바꾸기]를 눌러 다시 올려 주세요."
    IMAGES.mkdir(exist_ok=True)
    target = IMAGES / f"{frame}.jpg"
    if re.search(r"-\s*\[[xX]\]", section(body, "되돌리기")):
        shutil.copyfile(HERE / "numbers" / f"{frame}.jpg", target)
        clear_other(frame, target.name)
        return frame, f"{frame}번 액자를 번호 그림으로 되돌렸습니다. 1~2분 뒤 월드에 반영됩니다."
    url = image_url(section(body, "사진"))
    if not url:
        return None, "사진이 없습니다. '사진' 칸에 사진을 끌어다 놓고 다시 올려 주세요."
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "upload"
        try:
            download(url, os.environ.get("GH_TOKEN", ""), src)
            im = ImageOps.exif_transpose(Image.open(src))
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
        except Exception as e:
            print("open failed:", e)
            return None, "사진을 열 수 없습니다. jpg나 png 사진으로 다시 올려 주세요."
    im.thumbnail((2048, 2048), Image.LANCZOS)
    im.save(target, "JPEG", quality=90)
    clear_other(frame, target.name)
    return frame, f"{frame}번 액자 사진을 바꿨습니다. 1~2분 뒤 월드에 반영됩니다."


def main():
    frame, message = run()
    print(message)
    out = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "result.md"
    out.write_text(message + "\n", encoding="utf-8")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if frame else 'false'}\nframe={frame or ''}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""보고프구 월드 데이터 만들기.

GitHub Actions(.github/workflows/publish.yml)가 저장소에 무언가 올라올 때마다 실행한다.
images 폴더의 사진(파일 이름 = 액자 번호)을 칸(.github/bogo/slots.json) 비율로 가운데를 잘라 맞추고 칸 크기보다 크면 줄여 _site/images/<번호>.jpg로 만든다.
월드는 _site/images.txt(칸 이름과 내용 해시)를 보고 바뀐 칸만 다시 받는다.
사진 하나가 잘못되어도 나머지는 그대로 올리고, 확인할 내용은 실행 요약(Summary)에 적는다.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF = True
except Exception:
    HEIF = False

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "images"
OUT = ROOT / "_site"
EXTS = [".jpg", ".jpeg", ".png", ".webp"] + ([".heic", ".heif"] if HEIF else [])
QUALITY = 88
LIST_HEAD = "# bogo-data images"


def committed_at(path):
    """마지막으로 올린 시각(같은 칸에 파일이 여러 개면 가장 최근 것을 쓴다)."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct", "--", str(path)], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        return int(out) if out else 0
    except Exception:
        return 0


def uploads():
    found, ignored = {}, []
    if not SRC.exists():
        return found, ignored
    for p in sorted(SRC.rglob("*")):
        if not p.is_file() or p.name.lower() in ("readme.md", ".gitkeep"):
            continue
        if p.suffix.lower() not in EXTS:
            ignored.append((p, "사진 파일이 아니라 쓰지 않았습니다 (jpg, png, webp" + (", heic" if HEIF else "") + "만 됩니다)"))
            continue
        found.setdefault(p.stem.strip().lower(), []).append(p)
    return found, ignored


def open_rgb(path):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def crop_to(im, w, h):
    """칸 비율로 가운데를 잘라 맞춘다."""
    sw, sh = im.size
    want = w / h
    if sw / sh > want:
        nw = max(1, round(sh * want))
        left = (sw - nw) // 2
        im = im.crop((left, 0, left + nw, sh))
    elif sw / sh < want:
        nh = max(1, round(sw / want))
        top = (sh - nh) // 2
        im = im.crop((0, top, sw, top + nh))
    return im


def fit(im, w, h, grow=False):
    im = crop_to(im, w, h)
    if im.size[0] > w or grow:
        im = im.resize((w, h), Image.LANCZOS)
    return im


def save(im, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=QUALITY, optimize=True, progressive=False)
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


def pick(found, name, notes):
    files = found.get(name)
    if not files:
        return None
    files = sorted(files, key=lambda p: (committed_at(p), p.suffix.lower() == ".jpg"), reverse=True)
    for other in files[1:]:
        notes.append((other, f"{name}번에 파일이 여러 개라 가장 최근에 올린 `{files[0].relative_to(ROOT)}`를 썼습니다. 안 쓰는 파일은 지워 주세요"))
    return files[0]


def main():
    data = json.loads((Path(__file__).resolve().parent / "slots.json").read_text(encoding="utf-8"))
    slots = data["slots"]
    blank = tuple(data.get("blank", [249, 226, 220]))
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "images").mkdir(parents=True)

    found, ignored = uploads()
    notes = list(ignored)
    used = set()
    rows, lines = [], [LIST_HEAD]

    for s in slots:
        name, w, h = s["name"], s["w"], s["h"]
        if "parts" in s:
            gw, gh = s.get("grid", [5, 2])
            cw, ch = w // gw, h // gh
            sheet = Image.new("RGB", (w, h), blank)
            filled = []
            for part, (cx, cy) in zip(s["parts"], s["cells"]):
                src = pick(found, part, notes)
                used.add(part)
                if src is None:
                    continue
                try:
                    sheet.paste(fit(open_rgb(src), cw, ch, grow=True), (cx * cw, cy * ch))
                    filled.append(part)
                    rows.append((part, src, "정사각형으로 잘라 폴라로이드 판에 넣음"))
                except Exception as e:
                    notes.append((src, f"그림을 열 수 없습니다: {e}"))
            if filled:
                lines.append(f"{name} {save(sheet, OUT / 'images' / (name + '.jpg'))}")
            continue
        src = pick(found, name, notes)
        used.add(name)
        if src is None:
            continue
        try:
            im = open_rgb(src)
            before = im.size
            im = fit(im, w, h)
            lines.append(f"{name} {save(im, OUT / 'images' / (name + '.jpg'))}")
            rows.append((name, src, f"{before[0]}x{before[1]} → {im.size[0]}x{im.size[1]}"))
            if im.size[0] < w * 0.5:
                notes.append((src, f"사진이 작아서({before[0]}x{before[1]}) 흐리게 보일 수 있습니다. {w}x{h} 정도를 권합니다"))
        except Exception as e:
            notes.append((src, f"그림을 열 수 없습니다: {e}"))

    for key, files in found.items():
        if key not in used:
            for p in files:
                notes.append((p, "액자 번호와 맞지 않아 쓰지 않았습니다. 파일 이름을 월드에 보이는 번호로 바꿔 주세요 (예: 7.jpg)"))

    (OUT / "images.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    admins_src = ROOT / "admins.txt"
    text = admins_src.read_text(encoding="utf-8-sig") if admins_src.exists() else ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    (OUT / "admins.txt").write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    admins = [l.strip() for l in text.split("\n") if l.strip() and not l.strip().startswith("#")]

    # 월드 데이터 관리 페이지(비밀번호 페이지, worker.js)가 보여 줄 작은 그림. 공개 주소에는 관리 화면을 두지 않는다
    (OUT / "thumbs").mkdir()
    for s in slots:
        cw, ch = (s["w"] // s.get("grid", [5, 2])[0], s["h"] // s.get("grid", [5, 2])[1]) if "parts" in s else (s["w"], s["h"])
        for num in (s["parts"] if "parts" in s else [s["name"]]):
            src = pick(found, num, [])
            if src is None:
                continue
            try:
                th = fit(open_rgb(src), cw, ch, grow=True)
                th.thumbnail((240, 240), Image.LANCZOS)
                th.save(OUT / "thumbs" / f"{num}.jpg", "JPEG", quality=80)
            except Exception:
                pass
    (OUT / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    summary = [f"## 월드에 나가는 내용", f"- 관리자 {len(admins)}명: {', '.join(admins) if admins else '없음'}", f"- 사진 {len(lines) - 1}칸 / 전체 {len(slots)}칸 (폴라로이드 10장은 한 칸으로 셉니다)", ""]
    if rows:
        summary += ["| 칸 | 올린 파일 | 처리 |", "|---|---|---|"]
        summary += [f"| `{n}` | `{p.relative_to(ROOT)}` | {d} |" for n, p, d in rows]
        summary.append("")
    if notes:
        summary += ["### 확인이 필요한 파일"] + [f"- `{p.relative_to(ROOT)}`: {m}" for p, m in notes]
    else:
        summary.append("확인이 필요한 파일은 없습니다.")
    text = "\n".join(summary) + "\n"
    print(text)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    sys.exit(main())

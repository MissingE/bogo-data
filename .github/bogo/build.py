"""보고프구 월드 데이터 만들기.

GitHub Actions(.github/workflows/publish.yml)가 저장소에 무언가 올라올 때마다 실행한다.
images 폴더의 사진(파일 이름 = 액자 번호)을 칸(.github/bogo/slots.json) 비율로 가운데를 잘라 맞춘다.
VRChat은 사진을 5초에 한 장만 받으므로 액자 사진을 2048 판 몇 장(_site/sheets/<판>.jpg)에 모은다.
월드는 _site/world.txt(관리자 명단, 판 해시, 칸마다 판 안 자리) 하나만 보고 해시가 바뀐 판만 다시 받는다.
판 배치는 slots.json만으로 정해져 사진을 바꿔도 칸의 판·자리는 그대로다. 스크린 대기 화면은 머티리얼에 통째로 넣으므로 혼자 판 하나를 쓴다.
판 방식 이전에 올린 월드를 위해 칸마다 한 장씩(_site/images/<번호>.jpg, images.txt, admins.txt)도 계속 만든다.
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
WORLD_HEAD = "# bogo-data world 1"
# 판: VRChat이 받는 그림의 최대 크기. 칸을 76%로 줄여 넣으면 액자 45칸이 판 5장에 들어가고, 스크린 대기 화면 1장을 더해 6장(약 30초)이다
# 여백에는 사진 가장자리를 늘여 채워 먼 거리(작은 밉맵)에서도 옆 사진 색이 섞이지 않게 한다
SHEET = 2048
SHEET_SCALE = 0.76
GUTTER = 16
# 월드에 박아 둔 판 주소 수(월드 BogoRemote.MaxSheets와 같아야 한다)
MAX_SHEETS = 12


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


def layout(slots):
    """칸마다 (판, x, y, w, h)와 판마다 (가로, 세로). 큰 칸부터(같으면 slots.json 순서) 앞 판의 빈자리 중 남는 넓이가 가장 적은 곳에 넣는다(MaxRects)."""
    places, sizes, frees = {}, [], []
    pad = GUTTER // 2

    def cut(free, x, y, w, h):
        out = []
        for fx, fy, fw, fh in free:
            if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
                out.append((fx, fy, fw, fh))
                continue
            if x > fx:
                out.append((fx, fy, x - fx, fh))
            if x + w < fx + fw:
                out.append((x + w, fy, fx + fw - x - w, fh))
            if y > fy:
                out.append((fx, fy, fw, y - fy))
            if y + h < fy + fh:
                out.append((fx, y + h, fw, fy + fh - y - h))
        return [a for a in out if not any(a != b and a[0] >= b[0] and a[1] >= b[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3] for b in out)]

    def spot(free, w, h):
        best = None
        for fx, fy, fw, fh in free:
            if w <= fw and h <= fh:
                score = (fw * fh - w * h, min(fw - w, fh - h))
                if best is None or score < best[0]:
                    best = (score, fx, fy)
        return best

    packed = sorted([s for s in slots if s.get("group") != "screen"], key=lambda s: -s["w"] * s["h"])
    for s in packed:
        w, h = round(s["w"] * SHEET_SCALE), round(s["h"] * SHEET_SCALE)
        pw, ph = w + GUTTER, h + GUTTER
        for k, free in enumerate(frees):
            b = spot(free, pw, ph)
            if b:
                break
        else:
            frees.append([(0, 0, SHEET, SHEET)])
            sizes.append([0, 0])
            k = len(frees) - 1
            b = spot(frees[k], pw, ph)
        frees[k] = cut(frees[k], b[1], b[2], pw, ph)
        places[s["name"]] = (k, b[1] + pad, b[2] + pad, w, h)
        sizes[k] = [max(sizes[k][0], b[1] + pw), max(sizes[k][1], b[2] + ph)]
    # 쓰지 않은 오른쪽·아래는 잘라 낸다(4의 배수)
    sizes = [[min(SHEET, (sw + 3) // 4 * 4), min(SHEET, (sh + 3) // 4 * 4)] for sw, sh in sizes]
    for s in slots:
        if s.get("group") == "screen":
            places[s["name"]] = (len(sizes), 0, 0, s["w"], s["h"])
            sizes.append([s["w"], s["h"]])
    return places, sizes


def sheets(slots, built, blank, notes):
    """사진이 있는 판만 만든다. 돌려주는 값: world.txt의 's'·'p' 줄."""
    places, sizes = layout(slots)
    pad = GUTTER // 2
    pages = [Image.new("RGB", tuple(sz), blank) for sz in sizes]
    filled = [False] * len(sizes)
    rows = []
    for s in slots:
        name = s["name"]
        if name not in built:
            continue
        k, x, y, w, h = places[name]
        if k >= MAX_SHEETS:
            notes.append((ROOT / "images", f"{name}번 칸이 {k + 1}번째 판에 들어가 월드가 받지 못합니다. 월드의 판 주소 수({MAX_SHEETS})를 늘려 주세요"))
            continue
        im = built[name].resize((w, h), Image.LANCZOS) if built[name].size != (w, h) else built[name]
        if s.get("group") != "screen":
            # 가장자리 늘이기: 조금 키운 사진을 여백까지 먼저 깔고 제자리에 원본을 놓는다
            pages[k].paste(im.resize((w + 2 * pad, h + 2 * pad), Image.BILINEAR), (x - pad, y - pad))
        pages[k].paste(im, (x, y))
        filled[k] = True
        rows.append(f"p {name} {k} {x} {y} {w} {h}")
    heads = []
    (OUT / "sheets").mkdir(parents=True, exist_ok=True)
    for k, page in enumerate(pages):
        if filled[k]:
            heads.append(f"s {k} {save(page, OUT / 'sheets' / f'{k}.jpg')}")
    return heads + rows, sum(filled)


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
    # 판에 넣을 칸 그림(칸 비율로 자른 원본 크기)
    built = {}

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
                built[name] = sheet
            continue
        src = pick(found, name, notes)
        used.add(name)
        if src is None:
            continue
        try:
            im = open_rgb(src)
            before = im.size
            built[name] = crop_to(im, w, h)
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

    world, sheet_count = sheets(slots, built, blank, notes)
    (OUT / "world.txt").write_text("\n".join([WORLD_HEAD] + [f"a {a}" for a in admins] + world) + "\n", encoding="utf-8")

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

    summary = [f"## 월드에 나가는 내용", f"- 관리자 {len(admins)}명: {', '.join(admins) if admins else '없음'}", f"- 사진 {len(lines) - 1}칸 / 전체 {len(slots)}칸 (폴라로이드 10장은 한 칸으로 셉니다)", f"- 사진 판 {sheet_count}장 (월드는 판을 5초에 한 장씩, 가까운 판부터 받습니다)", ""]
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

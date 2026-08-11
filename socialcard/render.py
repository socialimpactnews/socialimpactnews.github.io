"""카드뉴스 이미지 생성(1080x1350, 4:5 카러셀).

색상과 로고는 소임뉴 로고에서 가져왔다(에메랄드 #00A76B, 차콜 #3C3C3C, 라이트 #89F394).
줄바꿈은 socialcard.typeset의 구문 단위 조판을 쓴다.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from . import typeset
from .config import Settings
from .errors import RenderError
from .models import CardNews

log = logging.getLogger(__name__)

# 굵기별 한글 폰트 후보. 앞에서부터 실제로 존재하는 것을 쓴다.
FONT_CANDIDATES = {
    "bold": [
        (str(Path.home() / "Library/Fonts/KoPubWorld Dotum Bold.ttf"), 0),
        ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 8),
        ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", 0),
    ],
    "medium": [
        (str(Path.home() / "Library/Fonts/KoPubWorld Dotum Medium.ttf"), 0),
        ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 5),
        ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", 0),
    ],
    "regular": [
        (str(Path.home() / "Library/Fonts/KoPubWorld Dotum Light.ttf"), 0),
        ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 3),
        ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", 0),
    ],
}

_font_cache = {}
_logo_cache = {}

MARGIN = 88
# 푸터는 캔버스 아래에 붙인다. 세로 길이가 바뀌어도 위쪽 요소는 그대로 두고
# 늘어난 공간이 본문 영역으로 돌아가게 하기 위해, 바닥에서의 거리로 정의한다.
FOOTER_FROM_BOTTOM = 112  # 바이라인 기준선
RULE_FROM_BOTTOM = 164  # 푸터 위 구분선


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    value = (value or "").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except (ValueError, IndexError):
        return (0, 167, 107)


def _mix(a: Sequence[int], b: Sequence[int], t: float) -> Tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _shade(rgb: Sequence[int], factor: float) -> Tuple[int, int, int]:
    """factor<1 이면 어둡게, >1 이면 밝게."""
    return tuple(max(0, min(255, int(round(c * factor)))) for c in rgb)  # type: ignore[return-value]


def load_font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key in _font_cache:
        return _font_cache[key]
    last_error: Optional[Exception] = None
    for path, index in FONT_CANDIDATES.get(weight, []):
        if not Path(path).exists():
            continue
        try:
            font = ImageFont.truetype(path, size, index=index)
        except Exception as exc:
            last_error = exc
            try:
                font = ImageFont.truetype(path, size)
            except Exception as exc2:
                last_error = exc2
                continue
        _font_cache[key] = font
        return font
    raise RenderError(
        "한글 폰트를 찾지 못했습니다({} {}pt). 마지막 오류: {}".format(weight, size, last_error)
    )


def load_logo(path: Path, size: int) -> Optional[Image.Image]:
    """로고를 원형으로 잘라 RGBA로 돌려준다. 파일이 없으면 None."""
    key = (str(path), size)
    if key in _logo_cache:
        return _logo_cache[key]
    if not path.exists():
        log.warning("로고 파일이 없어 마크 없이 렌더링합니다: %s", path)
        _logo_cache[key] = None
        return None
    try:
        logo = Image.open(path).convert("RGBA")
    except Exception as exc:
        log.warning("로고를 열 수 없습니다(%s): %s", path, exc)
        _logo_cache[key] = None
        return None
    scale = 4  # 안티에일리어싱용 오버샘플링
    logo = logo.resize((size * scale, size * scale), Image.LANCZOS)
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size * scale - 1, size * scale - 1], fill=255)
    logo.putalpha(mask)
    logo = logo.resize((size, size), Image.LANCZOS)
    _logo_cache[key] = logo
    return logo


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    weight: str,
    max_width: int,
    max_lines: int,
    start_size: int,
    min_size: int,
) -> Tuple[ImageFont.FreeTypeFont, List[str]]:
    """줄 수 제한에 맞을 때까지 폰트를 줄이며 구문 단위로 조판한다."""
    size = start_size
    font = load_font(weight, size)
    lines: List[str] = []
    while size >= min_size:
        font = load_font(weight, size)
        lines, _ = typeset.layout(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
        size -= 3
    lines = lines[:max_lines]
    if lines:
        lines[-1] = lines[-1].rstrip()[: max(1, len(lines[-1]) - 1)] + "…"
    return font, lines


QUOTE_PAIRS = {"“": "”", "‘": "’", '"': '"', "'": "'", "「": "」", "『": "』"}
OPENERS = tuple(QUOTE_PAIRS)


def _quote_indent(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[str],
    font: ImageFont.FreeTypeFont,
) -> float:
    """인용이 다음 줄로 이어질 때, 둘째 줄부터 밀어 넣을 폭.

    여는 따옴표만큼 밀어야 둘째 줄 첫 글자가 첫 줄 첫 글자와 세로로 맞는다.
    공백 문자로는 맞출 수 없다. 여는 큰따옴표가 공백 1.41칸 폭이라 한 칸이면
    왼쪽으로, 두 칸이면 오른쪽으로 밀린다. 그래서 폭을 직접 재서 쓴다.

    ‘엄마의 그림책’처럼 첫 줄에서 따옴표가 닫히면 인용이 아니라 낱말 표시이므로
    밀지 않는다. 닫혔는데도 밀면 둘째 줄부터 이유 없이 어긋난다.
    """
    if len(lines) < 2:
        return 0.0
    head = lines[0]
    opener = head[:1]
    if opener not in QUOTE_PAIRS:
        return 0.0
    closer = QUOTE_PAIRS[opener]
    closed = head.count(opener) % 2 == 0 if opener == closer else closer in head
    if closed:
        return 0.0
    return draw.textlength(opener, font=font)


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[str],
    xy: Tuple[int, int],
    font: ImageFont.FreeTypeFont,
    fill,
    line_gap: float = 1.45,
) -> int:
    x, y = xy
    step = int(font.size * line_gap)
    indent = _quote_indent(draw, lines, font)
    for i, line in enumerate(lines):
        # 따옴표로 다시 시작하는 줄은 그 자체로 첫 글자가 따옴표라 밀지 않는다.
        shift = indent if i and not line.startswith(OPENERS) else 0.0
        draw.text((x + shift, y), line, font=font, fill=fill)
        y += step
    return y


def _block_height(font: ImageFont.FreeTypeFont, lines: Sequence[str], line_gap: float) -> int:
    return int(font.size * line_gap) * max(0, len(lines) - 1) + int(font.size * 1.2)


def _gradient(size: int, top: Sequence[int], bottom: Sequence[int], height: Optional[int] = None) -> Image.Image:
    height = height or size
    image = Image.new("RGB", (size, height), tuple(top))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line([(0, y), (size, y)], fill=_mix(top, bottom, y / float(height - 1)))
    return image


def _pill(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
    text_fill,
    outline=None,
    pad_x: int = 26,
    pad_y: int = 13,
) -> int:
    """알약 배지를 그리고 오른쪽 끝 x좌표를 돌려준다."""
    x, y = xy
    width = draw.textlength(text, font=font)
    height = int(font.size * 1.5) + pad_y
    right = int(x + width + pad_x * 2)
    draw.rounded_rectangle(
        [x, y, right, y + height], radius=height // 2, fill=fill, outline=outline, width=2
    )
    draw.text((x + pad_x, y + pad_y // 2 + 2), text, font=font, fill=text_fill)
    return right


def _footer(
    draw: ImageDraw.ImageDraw,
    settings: Settings,
    text_color,
    rule_color,
    index: int,
    total: int,
    accent,
    size: int,
    height: int,
) -> None:
    """구분선 + 바이라인 + 진행 표시. 캔버스 아래에 붙는다."""
    RULE_Y = height - RULE_FROM_BOTTOM
    FOOTER_Y = height - FOOTER_FROM_BOTTOM
    draw.line([(MARGIN, RULE_Y), (size - MARGIN, RULE_Y)], fill=rule_color, width=2)

    name_font = load_font("bold", 27)
    mail_font = load_font("regular", 27)
    x = MARGIN
    draw.text((x, FOOTER_Y), settings.brand_name, font=name_font, fill=text_color)
    x += int(draw.textlength(settings.brand_name, font=name_font)) + 18
    draw.text((x, FOOTER_Y), settings.brand_email, font=mail_font, fill=text_color)

    pager_font = load_font("bold", 25)
    label = "{:02d}/{:02d}".format(index, total)
    label_w = draw.textlength(label, font=pager_font)
    draw.text((size - MARGIN - label_w, FOOTER_Y + 1), label, font=pager_font, fill=text_color)

    # 진행 바: 구분선 위에 현재 카드까지의 비율을 덧그린다.
    filled = int((size - MARGIN * 2) * (index / float(total)))
    draw.line([(MARGIN, RULE_Y), (MARGIN + filled, RULE_Y)], fill=accent, width=4)


def render_cardnews(cardnews: CardNews, out_dir: Path, settings: Settings) -> List[Path]:
    """카드뉴스 한 세트를 PNG 파일들로 렌더링하고 경로를 반환한다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    size = settings.card_size            # 가로
    height = settings.card_height or size  # 세로(기본 4:5)
    text_width = size - MARGIN * 2
    RULE_Y = height - RULE_FROM_BOTTOM
    FOOTER_Y = height - FOOTER_FROM_BOTTOM

    brand = _hex_to_rgb(settings.brand_color)  # 에메랄드
    accent = _hex_to_rgb(settings.accent_color)  # 라이트 그린
    ink = _hex_to_rgb(settings.ink_color)  # 차콜
    paper = _hex_to_rgb(settings.paper_color)

    brand_deep = _shade(brand, 0.72)
    mint = _mix(accent, (255, 255, 255), 0.45)
    on_brand_muted = _mix(brand, (255, 255, 255), 0.62)
    ink_muted = _mix(ink, paper, 0.5)
    hairline = _mix(ink, paper, 0.88)

    logo_path = settings.logo_path
    total = len(cardnews.cards)
    paths: List[Path] = []

    for card in cardnews.cards:
        if card.kind == "cover":
            image = _gradient(size, brand, brand_deep, height)
            draw = ImageDraw.Draw(image)

            # 오른쪽 아래 워터마크 링 — 여백을 심심하지 않게 하는 최소한의 장식
            ring = _mix(brand_deep, (255, 255, 255), 0.08)
            draw.ellipse([size - 210, height - 250, size + 260, height + 220], outline=ring, width=3)

            logo = load_logo(logo_path, 92)
            if logo is not None:
                image.paste(logo, (MARGIN, 84), logo)

            chip_font = load_font("bold", 26)
            chip = (cardnews.article.section or "소셜임팩트").strip()
            _pill(
                draw, (MARGIN, 232), chip, chip_font,
                fill=None, text_fill=mint, outline=mint,
            )

            # 제목과 부제를 합친 높이가 배지 아래 ~ 푸터 위 공간을 넘지 않도록 단계적으로 줄인다.
            top_limit, gap = 344, 74
            budget = RULE_Y - 52 - top_limit
            steps = ((4, 90, 3, 38), (4, 82, 2, 36), (3, 76, 2, 34), (4, 68, 2, 32), (4, 60, 2, 29))
            # 편집자가 줄바꿈을 지정했으면 그 줄 수를 지킨다. 지정한 곳에서 끊었는데
            # 폰트가 커서 한 줄이 더 접히면 '내가 / 사회적경제를 안다고 / 말할 수 있을까?'처럼
            # 의도하지 않은 세 줄이 된다. 그 줄 수로 못 맞추면 원래 한도로 되돌린다.
            forced_lines = len([p for p in card.title.split("\n") if p.strip()])

            for max_title_lines, title_start, max_hook_lines, hook_start in steps:
                cap = min(max_title_lines, forced_lines) if forced_lines > 1 else max_title_lines
                title_font, title_lines = fit_text(
                    draw, card.title, "bold", text_width, cap, title_start, 52
                )
                if cap < max_title_lines and title_lines and title_lines[-1].endswith("…"):
                    title_font, title_lines = fit_text(
                        draw, card.title, "bold", text_width, max_title_lines, title_start, 52
                    )
                hook_font, hook_lines = fit_text(
                    draw, card.body, "regular", text_width, max_hook_lines, hook_start, 26
                )
                title_h = _block_height(title_font, title_lines, 1.3)
                hook_h = _block_height(hook_font, hook_lines, 1.55)
                if title_h + gap + hook_h <= budget:
                    break

            # 배지 아래 ~ 푸터 위 공간의 가운데에 둔다. 아래에 붙여두면 4:5처럼
            # 세로가 길어졌을 때 늘어난 높이가 전부 위쪽 빈 공간으로 남는다.
            block_h = title_h + gap + hook_h
            block_top = top_limit + max(0, (RULE_Y - 52 - top_limit - block_h) // 2)
            y = _draw_lines(draw, title_lines, (MARGIN, block_top), title_font, (255, 255, 255), 1.3)

            draw.rectangle([MARGIN, y + 32, MARGIN + 88, y + 38], fill=accent)
            _draw_lines(draw, hook_lines, (MARGIN, y + gap), hook_font, mint, 1.55)

            _footer(draw, settings, on_brand_muted, _mix(brand, brand_deep, 0.5), card.index, total, accent, size, height)

        elif card.kind == "body":
            image = Image.new("RGB", (size, height), paper)
            draw = ImageDraw.Draw(image)
            draw.rectangle([0, 0, size, 10], fill=brand)

            label_font = load_font("bold", 27)
            label_right = _pill(
                draw, (MARGIN, 158), card.title, label_font, fill=brand, text_fill=(255, 255, 255)
            )

            if card.highlight:
                # 핵심 숫자·일정은 장식보다 정보다. 킥커 옆에 배지로 붙인다.
                _pill(
                    draw, (label_right + 16, 158), card.highlight, label_font,
                    fill=_mix(brand, paper, 0.86), text_fill=_shade(brand, 0.8),
                )
            else:
                # 배지가 없을 때만 연한 인덱스 숫자로 여백을 잡아준다.
                ghost_font = load_font("bold", 150)
                ghost = "{:02d}".format(card.index)
                ghost_w = draw.textlength(ghost, font=ghost_font)
                draw.text(
                    (size - MARGIN - ghost_w, 128), ghost, font=ghost_font,
                    fill=_mix(brand, paper, 0.90),
                )

            body_font, body_lines = fit_text(draw, card.body, "medium", text_width, 7, 60, 38)
            area_top, area_bottom = 300, RULE_Y - 60
            block_h = _block_height(body_font, body_lines, 1.62)
            body_top = area_top + max(0, (area_bottom - area_top - block_h) // 2)
            _draw_lines(draw, body_lines, (MARGIN, body_top), body_font, ink, 1.62)

            _footer(draw, settings, ink_muted, hairline, card.index, total, brand, size, height)

        else:  # outro
            image = _gradient(size, _shade(ink, 1.02), _shade(ink, 0.78), height)
            draw = ImageDraw.Draw(image)

            title_font, title_lines = fit_text(draw, card.title, "bold", text_width, 3, 66, 46)
            note_font = load_font("medium", 32)
            body_font = load_font("regular", 30)
            note_lines: List[str] = []
            if card.footnote:
                note_lines, _ = typeset.layout(draw, card.footnote, note_font, text_width)
                note_lines = note_lines[:3]
            body_lines = [l for l in (card.body or "").split("\n") if l.strip()]

            # 로고 + 제목 + 구분선 + 멘션 + 안내문 전체를 카드 중앙에 맞춘다.
            logo_size = 150
            block_h = (
                logo_size + 66
                + _block_height(title_font, title_lines, 1.3)
                + 78
                + (int(note_font.size * 1.5) * len(note_lines) + (12 if note_lines else 0))
                + int(body_font.size * 1.5) * len(body_lines)
            )
            top = max(150, (RULE_Y - block_h) // 2)

            logo = load_logo(logo_path, logo_size)
            if logo is not None:
                image.paste(logo, ((size - logo_size) // 2, top), logo)

            y = top + logo_size + 66
            for line in title_lines:
                width = draw.textlength(line, font=title_font)
                draw.text(((size - width) / 2, y), line, font=title_font, fill=(255, 255, 255))
                y += int(title_font.size * 1.3)

            draw.rectangle([(size - 88) // 2, y + 34, (size + 88) // 2, y + 40], fill=accent)
            y += 78

            for line in note_lines:
                width = draw.textlength(line, font=note_font)
                draw.text(((size - width) / 2, y), line, font=note_font, fill=mint)
                y += int(note_font.size * 1.5)
            if note_lines:
                y += 12

            for line in body_lines:
                width = draw.textlength(line, font=body_font)
                draw.text(((size - width) / 2, y), line, font=body_font, fill=_mix(ink, paper, 0.55))
                y += int(body_font.size * 1.5)

            _footer(
                draw, settings, _mix(ink, paper, 0.55), _mix(ink, paper, 0.2),
                card.index, total, accent, size, height,
            )

        path = out_dir / "{}_{:02d}.png".format(cardnews.article.article_id, card.index)
        image.save(path, format="PNG", optimize=True)
        card.image_path = path
        paths.append(path)

    if not paths:
        raise RenderError("생성된 카드 이미지가 없습니다: {}".format(cardnews.article.url))
    return paths

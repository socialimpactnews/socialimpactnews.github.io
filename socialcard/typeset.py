"""한국어 카드뉴스 조판 — 구문 단위 끊어읽기 줄바꿈.

일반적인 greedy 줄바꿈은 줄 끝을 꽉 채우느라 '충남청년센터가 청년정책 플랫폼 /
열고닫기와' 처럼 의미 덩어리 한가운데를 끊는다. 카드뉴스는 한 줄이 곧 한 호흡이라
이런 줄바꿈이 가독성을 크게 떨어뜨린다.

그래서 여기서는 어절을 최소 단위로 두고(=단어는 절대 쪼개지 않음, '단어 내림'),
어절 뒤에서 끊었을 때 얼마나 자연스러운지를 점수로 매긴 뒤 전체 문단의 비용이
최소가 되도록 동적 계획법으로 줄을 나눈다.

  끊기 좋은 자리: 쉼표/마침표 뒤 > 연결어미(~하고, ~통해, ~위해) 뒤 > 조사 뒤 > 그 외
"""
from __future__ import annotations

import re
from typing import Callable, List, Sequence, Tuple

# 줄 끝에 오면 자연스러운 어절의 꼬리들. 값이 클수록 끊기 좋은 자리다.
_PUNCT_TAIL = ("…", ".", ",", "·", "!", "?", ":", ";", ")", "]", "”", "’", "'")
_CONNECTIVE_TAIL = (
    "하고", "하며", "되며", "이며", "면서", "지만", "으나", "는데", "어서", "아서",
    "통해", "위해", "따라", "대해", "포함해", "라며",
)
# 뒤 어절에 붙어 읽히는 부사·관형사. 여기서 끊으면 오히려 읽기 나빠진다.
_BINDING = (
    "함께", "매우", "더욱", "가장", "다시", "직접", "곧", "총", "약",
    "오는", "지난", "이번", "새", "각", "모든", "주요", "첫",
)
_JOSA_TAIL_2 = (
    "에서", "으로", "에게", "부터", "까지", "와의", "과의", "에는", "에도", "로서",
    "로써", "이라", "라는", "이란", "만큼", "처럼", "보다", "조차", "마저",
)
_JOSA_TAIL_1 = ("은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만", "께")

# 줄을 하나 더 쓰는 데 드는 기본 비용. 이게 없으면 끊기 좋은 자리마다 줄을 나눠버린다.
_LINE_COST = 7.0
# 마지막 줄에 짧은 어절 하나만 남는 경우(고아 줄) 페널티.
_ORPHAN_PENALTY = 45.0
_SLACK_WEIGHT = 100.0


def break_score(token: str) -> float:
    """이 어절 뒤에서 줄을 끊었을 때의 자연스러움 점수(음수면 끊지 말아야 할 자리)."""
    tail = token.rstrip("\"'”’)]}")
    if not tail:
        return 0.0
    if tail in _BINDING:
        return -6.0
    if token.endswith(_PUNCT_TAIL):
        return 11.0
    if tail.endswith(_CONNECTIVE_TAIL):
        return 7.0
    if tail.endswith(_JOSA_TAIL_2):
        return 4.5
    if tail.endswith(_JOSA_TAIL_1):
        return 2.5
    return 0.0


def _atoms(paragraph: str, measure: Callable[[str], float], max_width: float) -> List[str]:
    """어절 목록. 한 어절이 줄 너비보다 길면 그때만 글자 단위로 쪼갠다."""
    atoms: List[str] = []
    for token in paragraph.split():
        if measure(token) <= max_width:
            atoms.append(token)
            continue
        chunk = ""
        for ch in token:
            if measure(chunk + ch) <= max_width:
                chunk += ch
            else:
                if chunk:
                    atoms.append(chunk)
                chunk = ch
        if chunk:
            atoms.append(chunk)
    return atoms


def wrap_paragraph(paragraph: str, measure: Callable[[str], float], max_width: float) -> List[str]:
    """한 문단을 구문 단위로 끊어 여러 줄로 나눈다."""
    tokens = _atoms(paragraph, measure, max_width)
    if not tokens:
        return []
    n = len(tokens)

    # widths[i][j] 를 매번 재지 않도록 누적 폭을 미리 잰다.
    token_w = [measure(t) for t in tokens]
    space_w = measure("가 가") - measure("가가")
    if space_w <= 0:
        space_w = measure(" ") or 4.0

    best = [float("inf")] * (n + 1)
    nxt = [n] * (n + 1)
    best[n] = 0.0

    for i in range(n - 1, -1, -1):
        width = 0.0
        for j in range(i, n):
            width = token_w[j] if j == i else width + space_w + token_w[j]
            if width > max_width and j > i:
                break
            is_last = j == n - 1
            slack = max(0.0, max_width - width)
            if is_last:
                cost = _LINE_COST
                if j == i and i > 0 and len(tokens[j]) <= 5:
                    cost += _ORPHAN_PENALTY  # 마지막 줄에 짧은 단어 하나만 남기지 않는다
            else:
                cost = _LINE_COST + _SLACK_WEIGHT * (slack / max_width) ** 2 - break_score(tokens[j])
            total = cost + best[j + 1]
            if total < best[i]:
                best[i] = total
                nxt[i] = j + 1

    lines: List[str] = []
    i = 0
    while i < n:
        j = nxt[i]
        lines.append(" ".join(tokens[i:j]))
        i = j
    return lines


def wrap_text(text: str, measure: Callable[[str], float], max_width: float) -> List[str]:
    """빈 줄을 보존하며 여러 문단을 조판한다."""
    lines: List[str] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(wrap_paragraph(paragraph.strip(), measure, max_width))
    return lines


_ALIGN_MARK = re.compile(r"^\[\[(.*?)\]\]")
_SENTENCE_TAIL = re.compile(r"(다|요)[.!?]$")


def balance(lines: Sequence[str]) -> List[str]:
    """마지막 줄이 지나치게 짧으면 앞줄에서 한 어절을 내려 균형을 맞춘다(단어 내림)."""
    result = list(lines)
    if len(result) < 2:
        return result
    last = result[-1]
    prev = result[-2]
    if len(last) <= 4 and " " in prev:
        head, _, tail = prev.rpartition(" ")
        result[-2] = head
        result[-1] = "{} {}".format(tail, last)
    return result


def is_sentence_end(line: str) -> bool:
    return bool(_SENTENCE_TAIL.search(line.strip()))


def measure_with(draw, font) -> Callable[[str], float]:
    def _measure(text: str) -> float:
        return draw.textlength(text, font=font)

    return _measure


def layout(
    draw,
    text: str,
    font,
    max_width: float,
) -> Tuple[List[str], float]:
    """조판 결과와 가장 긴 줄의 폭을 함께 돌려준다.

    줄바꿈 문자가 들어 있으면 그 위치는 그대로 지킨다. 자동 조판이 좋은 지점을 찾지만
    커버 문구처럼 편집자가 호흡을 직접 정하고 싶을 때가 있어 남겨둔 통로다.
    """
    measure = measure_with(draw, font)
    lines: List[str] = []
    for part in str(text or "").split("\n"):
        # [[앞말]]은 "이 앞말의 폭만큼 밀어라"는 표시다. 폭만 빌려 쓰고 글자는 그리지
        # 않으므로, 조판에서는 떼어내고 첫 줄에 다시 붙여 render로 넘긴다.
        marker = ""
        m = _ALIGN_MARK.match(part)
        if m:
            marker, part = m.group(0), part[m.end():]
        # 앞 공백은 지우지 않는다. 인용 부호로 시작하는 커버에서 둘째 줄을 첫 줄의
        # 글자에 맞추려면 편집자가 넣은 들여쓰기가 살아 있어야 한다.
        indent = part[: len(part) - len(part.lstrip(" 　"))]
        part = part.strip()
        if not part:
            continue
        pad = measure(marker[2:-2]) if marker else (measure(indent) if indent else 0.0)
        wrapped = balance(wrap_text(part, measure, max_width - pad))
        if wrapped:
            wrapped[0] = marker + indent + wrapped[0] if marker else indent + wrapped[0]
        lines.extend(wrapped)
    widest = max((measure(line) for line in lines), default=0.0)
    return lines, widest

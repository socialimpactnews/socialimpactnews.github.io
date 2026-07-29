"""사람이 손으로 덮어쓰는 커버 문구.

폴백(`rule`)은 기사에 있는 사실만 앞으로 당길 수 있어서, 협약·MOU처럼 '기관이 무엇을 했나'를
'독자가 무엇을 얻나'로 뒤집어야 하는 기사에서는 헤드라인이 기사 제목에 가깝게 남는다.
그런 몇 건만 편집자가 두 줄씩 채워 넣는 통로가 이 파일이다.

기사 CSV의 컬럼이 아니라 별도 파일로 둔 이유는 정기 실행이 RSS 수집이기 때문이다. 기사 CSV에
컬럼을 두면 오프라인 테스트에서만 쓸 수 있고, 매일 도는 RSS 실행에는 닿지 않는다.

    article_id,url,headline,hook,read_more
    SIN6833,,충남 청년정책 전국에서 검색된다,충남청년포털과 '열고닫기'가 연결된다.,

빈 칸은 덮어쓰지 않는다. 파일이 없으면 아무 일도 일어나지 않는다.
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from .models import CardNews

log = logging.getLogger(__name__)

FIELDS = ("headline", "hook", "read_more")  # 덮어쓸 문구
DROP = "drop_cards"  # 빼버릴 본문 카드 번호(예: "3" 또는 "3,4")
EXCLUDE = "exclude_tags"  # 이 기사에서만 태그하지 않을 계정(예: "mysc.official")
# 본문 카드를 통째로 다시 쓰는 통로. '|'로 카드를 나누고, '::' 앞은 킥커(생략 가능).
#   무슨 일이::협약을 맺었다.|핵심은::15개사를 뽑는다.
BODY = "body_cards"
HEADER = ("article_id", "url", "headline", "hook", "read_more", BODY, DROP, EXCLUDE)


class OverrideBook:
    """기사(URL 또는 article_id) → 덮어쓸 문구."""

    def __init__(self, rows: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self.rows: Dict[str, Dict[str, str]] = rows or {}

    def __len__(self) -> int:
        return len(self.rows)

    def _lookup(self, cardnews: CardNews) -> Optional[str]:
        for key in (cardnews.article.url.strip(), cardnews.article.article_id.strip()):
            if key and key in self.rows:
                return key
        return None

    def apply(self, cardnews: CardNews) -> List[str]:
        """덮어쓴 항목 이름을 돌려준다. 커버·아웃트로 카드에도 같이 반영한다."""
        key = self._lookup(cardnews)
        if key is None:
            return []

        row = self.rows[key]
        changed: List[str] = []
        for field in FIELDS:
            # CSV 한 칸에 실제 줄바꿈을 넣기 어려우므로 '\n' 두 글자로 적게 하고 여기서 되돌린다.
            value = (row.get(field) or "").replace("\\n", "\n").strip()
            if not value or value == getattr(cardnews, field):
                continue
            setattr(cardnews, field, value)
            changed.append(field)

        if changed:
            # 카드 텍스트는 build_cardnews에서 이미 복사돼 있으므로 함께 갱신해야 한다.
            cover = cardnews.cards[0]
            cover.title, cover.body = cardnews.headline, cardnews.hook
            cardnews.cards[-1].footnote = cardnews.read_more
            if "headline" in changed or "hook" in changed:
                cardnews.hook_type = "manual"

        # 본문 카드를 편집자가 직접 쓴 것으로 갈아끼운다. 커버·아웃트로는 그대로 둔다.
        raw = row.get(BODY, "")
        if raw.strip():
            from .models import Card
            from .summarize import (
                DEFAULT_KICKERS, MAX_CARD_BODY, MAX_KICKER,
                _clip_phrase, _clip_sentence, find_highlight,
            )

            bodies: List[Card] = []
            for i, spec in enumerate([s.strip() for s in raw.split("|") if s.strip()]):
                kicker, sep, text = spec.partition("::")
                if not sep:
                    kicker, text = DEFAULT_KICKERS[i % len(DEFAULT_KICKERS)], spec
                text = text.replace("\\n", "\n").strip()
                if "\n" in text and len(text) <= MAX_CARD_BODY:
                    # 지정한 줄바꿈을 지켜야 하므로 공백을 정리하지 않는다.
                    pass
                else:
                    text = _clip_sentence(text.replace("\n", " "), MAX_CARD_BODY)
                bodies.append(Card(
                    kind="body",
                    title=_clip_phrase(kicker.strip(), MAX_KICKER),
                    body=text,
                    highlight=find_highlight(text),
                ))
            if bodies:
                cardnews.cards = [cardnews.cards[0]] + bodies + [cardnews.cards[-1]]
                for i, card in enumerate(cardnews.cards, start=1):
                    card.index = i
                changed.append(BODY)

        # 중복되는 본문 카드를 빼는 통로. 커버와 아웃트로는 구조상 뺄 수 없다.
        drop = {int(n) for n in re.findall(r"\d+", row.get(DROP, ""))}
        if drop:
            kept = [c for c in cardnews.cards if not (c.kind == "body" and c.index in drop)]
            if len(kept) < len(cardnews.cards):
                cardnews.cards = kept
                # 번호는 페이지 표기(03/05)·진행 바·파일명에 모두 쓰이므로 다시 매긴다.
                for i, card in enumerate(kept, start=1):
                    card.index = i
                changed.append(DROP)

        # 계정 매핑은 그대로 두고 이 기사에서만 태그를 뺀다.
        excluded = [h.strip().lstrip("@") for h in row.get(EXCLUDE, "").split(",") if h.strip()]
        if excluded:
            cardnews.excluded_handles = excluded
            dropped = {h.lower() for h in excluded}
            kept = [m for m in cardnews.mentions if m.handle.lstrip("@").lower() not in dropped]
            if len(kept) < len(cardnews.mentions):
                cardnews.mentions = kept
                changed.append(EXCLUDE)

        return changed


def load_overrides(path: Path) -> OverrideBook:
    """파일이 없으면 빈 책을 돌려준다(덮어쓰기는 선택 기능이다)."""
    if not path.exists():
        return OverrideBook()

    rows: Dict[str, Dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        if not fields & set(FIELDS + (BODY, DROP, EXCLUDE)):
            log.warning("덮어쓰기 파일에 %s 컬럼이 없습니다: %s", "/".join(FIELDS), path)
            return OverrideBook()
        for raw in reader:
            row = {k: (v or "").strip() for k, v in raw.items() if k}
            if not any(row.get(f) for f in FIELDS + (BODY, DROP, EXCLUDE)):
                continue  # 값이 하나도 없는 줄은 아직 안 채운 템플릿이다
            for key in (row.get("url"), row.get("article_id")):
                if key:
                    rows[key] = row
    return OverrideBook(rows)


def write_template(path: Path, cardnews_dicts: List[Dict[str, str]]) -> int:
    """실행 결과에서 현재 문구를 채운 템플릿을 만든다. 편집자는 고칠 줄만 손보면 된다.

    이미 있는 파일은 덮어쓰지 않고, 아직 없는 기사만 뒤에 덧붙인다.
    """
    existing: set = set()
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                for key in (row.get("url"), row.get("article_id")):
                    if (key or "").strip():
                        existing.add(key.strip())

    new_rows = [d for d in cardnews_dicts if d["article_id"] not in existing and d["url"] not in existing]
    if not new_rows:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(HEADER))
        if is_new:
            writer.writeheader()
        for row in new_rows:
            writer.writerow({k: row.get(k, "") for k in HEADER})
    return len(new_rows)

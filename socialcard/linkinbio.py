"""링크인바이오 페이지 생성.

인스타그램 캡션의 URL은 클릭되지 않고, 공식 Graph API는 스토리 링크 스티커도 지원하지 않는다.
(비공식 API로 링크 스티커를 붙이는 방법이 있으나 계정 정지 위험이 있어 쓰지 않는다.)

그래서 발행된 게시물 → 기사 전문으로 가는 경로를 이렇게 만든다.

  프로필 바이오 링크 → 이 페이지(최근 발행 기사 목록) → 기사 전문

페이지는 발행 이력(SQLite)에서 최근 기사를 읽어 매 실행마다 다시 만들고,
카드 이미지와 같은 정적 호스팅에 함께 올리면 된다.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{brand} · 오늘의 기사</title>
<style>
  :root {{
    --brand: {brand_color};
    --ink: {ink_color};
    --paper: #FBFCFB;
    --line: #E6EDE9;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px 20px 56px;
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
    background: var(--paper); color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 520px; margin: 0 auto; }}
  header {{ text-align: center; margin-bottom: 28px; }}
  .logo {{ width: 84px; height: 84px; border-radius: 50%; object-fit: cover; }}
  h1 {{ font-size: 20px; margin: 14px 0 4px; letter-spacing: -0.02em; }}
  .sub {{ font-size: 14px; color: #7A8781; margin: 0; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li + li {{ margin-top: 12px; }}
  a.item {{
    display: block; padding: 18px 20px; border: 1px solid var(--line); border-radius: 16px;
    background: #fff; text-decoration: none; color: inherit; transition: border-color .15s, transform .15s;
  }}
  a.item:hover, a.item:focus {{ border-color: var(--brand); transform: translateY(-1px); }}
  .kicker {{ font-size: 12px; font-weight: 700; color: var(--brand); letter-spacing: .04em; }}
  .title {{ font-size: 16px; font-weight: 700; line-height: 1.45; margin: 6px 0 0; letter-spacing: -0.01em; }}
  .meta {{ font-size: 12px; color: #93A09A; margin-top: 8px; }}
  footer {{ text-align: center; margin-top: 34px; font-size: 12px; color: #93A09A; line-height: 1.7; }}
  footer a {{ color: var(--brand); text-decoration: none; }}
  .empty {{ text-align: center; color: #93A09A; font-size: 14px; padding: 40px 0; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --paper: #171B19; --ink: #EDF2EF; --line: #2C3531; }}
    a.item {{ background: #1E2422; }}
    .sub, .meta, footer {{ color: #8A9792; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    {logo_tag}
    <h1>{brand}</h1>
    <p class="sub">인스타그램에 소개한 기사 전문을 여기서 볼 수 있습니다</p>
  </header>
  {body}
  <footer>
    {brand} · <a href="mailto:{email}">{email}</a><br>
    {updated} 기준
  </footer>
</div>
</body>
</html>
"""


def _item_html(entry: Dict[str, Any]) -> str:
    title = html.escape(entry.get("title") or "제목 없음")
    url = html.escape(entry.get("article_url") or "")
    kicker = html.escape(entry.get("kicker") or "기사 전문")
    meta = html.escape(entry.get("meta") or "")
    return (
        '    <li><a class="item" href="{url}" target="_blank" rel="noopener">'
        '<span class="kicker">{kicker}</span>'
        '<p class="title">{title}</p>'
        '{meta_html}</a></li>'
    ).format(
        url=url,
        kicker=kicker,
        title=title,
        meta_html='<p class="meta">{}</p>'.format(meta) if meta else "",
    )


def collect_entries(store: Store, limit: int) -> List[Dict[str, Any]]:
    """발행 이력에서 최근 기사를 최신순으로 가져온다."""
    rows = store.conn.execute(
        "SELECT article_url, title, published_at, created_at, permalink FROM published "
        "WHERE mode != 'seed' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    entries: List[Dict[str, Any]] = []
    for row in rows:
        published = (row["published_at"] or "")[:10]
        entries.append(
            {
                "article_url": row["article_url"],
                "title": row["title"],
                "kicker": "기사 전문",
                "meta": published.replace("-", ".") if published else "",
            }
        )
    return entries


def build_page(entries: List[Dict[str, Any]], settings: Settings) -> str:
    logo_tag = ""
    if settings.logo_path.exists():
        logo_tag = '<img class="logo" src="logo.png" alt="{}">'.format(html.escape(settings.brand_name))

    if entries:
        body = "  <ul>\n{}\n  </ul>".format("\n".join(_item_html(e) for e in entries))
    else:
        body = '  <p class="empty">아직 발행된 기사가 없습니다.</p>'

    return PAGE_TEMPLATE.format(
        brand=html.escape(settings.brand_name),
        email=html.escape(settings.brand_email),
        brand_color=settings.brand_color,
        ink_color=settings.ink_color,
        logo_tag=logo_tag,
        body=body,
        updated=datetime.now(KST).strftime("%Y.%m.%d %H:%M"),
    )


def write_page(settings: Settings, store: Store, out_dir: Optional[Path] = None) -> Optional[Path]:
    """링크인바이오 페이지를 파일로 쓰고 경로를 돌려준다."""
    # 저장소 최상위에 둔다. 자체 도메인을 붙이면 저장소 이름이 경로에서 사라지므로
    # 페이지가 도메인 루트가 되고, 프로필에 거는 주소가 가장 짧아진다.
    out_dir = out_dir or settings.out_dir
    try:
        entries = collect_entries(store, settings.linkinbio_limit)
        out_dir.mkdir(parents=True, exist_ok=True)
        page = out_dir / "index.html"
        page.write_text(build_page(entries, settings), encoding="utf-8")
        if settings.logo_path.exists():
            (out_dir / "logo.png").write_bytes(settings.logo_path.read_bytes())

        # 예전에 쓰던 /linkinbio/ 경로도 같은 내용으로 함께 만든다.
        # 프로필 바이오에 걸어둔 주소는 한 번 나가면 회수할 수 없으므로,
        # 페이지 위치를 옮기더라도 옛 주소가 404가 되면 안 된다.
        legacy = out_dir / "linkinbio"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "index.html").write_text(build_page(entries, settings), encoding="utf-8")
        if settings.logo_path.exists():
            (legacy / "logo.png").write_bytes(settings.logo_path.read_bytes())

        log.info("링크인바이오 페이지 갱신: %s (기사 %d건)", page, len(entries))
        return page
    except OSError as exc:
        log.warning("링크인바이오 페이지 생성 실패: %s", exc)
        return None

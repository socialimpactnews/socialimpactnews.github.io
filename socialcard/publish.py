"""인스타그램 발행 어댑터.

graph   : Instagram Graph API로 캐러셀/스토리 직접 발행 (기본)
webhook : Make/Zapier 등 중계 웹훅으로 페이로드 전달
console : 실제 호출 없이 콘솔 출력만 (드라이런 기본값)

Graph API는 로컬 파일을 업로드하지 못하고 '공개 접근 가능한 이미지 URL'만 받는다.
그래서 렌더링된 PNG는 out/ 아래에 저장한 뒤 PUBLIC_IMAGE_BASE_URL로 매핑해 전달한다.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .config import Settings, missing_publish_config
from .errors import PublishError
from .models import CardNews, PublishResult

log = logging.getLogger(__name__)


def build_image_urls(cardnews: CardNews, out_root: Path, settings: Settings) -> List[str]:
    """렌더링된 카드 파일 경로를 공개 URL로 바꾼다."""
    base = (settings.public_image_base_url or "").rstrip("/")
    urls: List[str] = []
    for card in cardnews.cards:
        if card.image_path is None:
            raise PublishError("렌더링되지 않은 카드가 있습니다: {}".format(card.index))
        try:
            rel = card.image_path.resolve().relative_to(out_root.resolve())
        except ValueError:
            rel = Path(card.image_path.name)
        urls.append("{}/{}".format(base, str(rel).replace("\\", "/")) if base else card.image_path.as_uri())
    return urls


_TAG_ERROR_HINTS = ("user_tags", "username", "tag", "not found", "태그")


def _looks_like_tag_error(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _TAG_ERROR_HINTS)


class Publisher:
    name = "base"

    def publish(self, cardnews: CardNews, image_urls: List[str]) -> PublishResult:  # pragma: no cover
        raise NotImplementedError


class ConsolePublisher(Publisher):
    """드라이런용. 실제 발행은 하지 않고 무엇을 올릴지 기록만 남긴다."""

    name = "console"

    def publish(self, cardnews: CardNews, image_urls: List[str]) -> PublishResult:
        tags = cardnews.usernames()
        log.info(
            "[DRY-RUN] 발행하지 않음 | %s | 카드 %d장 | 캡션 %d자 | 태그 %s",
            cardnews.article.title,
            len(image_urls),
            len(cardnews.full_caption()),
            " ".join("@" + t for t in tags) if tags else "없음",
        )
        return PublishResult(
            article_url=cardnews.article.url,
            status="dry_run",
            detail="카드 {}장 생성, 계정 태그 {}건 예정, 실제 발행 생략".format(
                len(image_urls), len(tags)
            ),
        )


class WebhookPublisher(Publisher):
    """외부 자동화 도구(Make/Zapier 등)로 발행 페이로드를 넘긴다."""

    name = "webhook"

    def __init__(self, settings: Settings):
        if not settings.publish_webhook_url:
            raise PublishError("PUBLISH_WEBHOOK_URL이 설정되지 않았습니다")
        self.settings = settings

    def publish(self, cardnews: CardNews, image_urls: List[str]) -> PublishResult:
        payload = {
            "article": cardnews.article.to_dict(),
            "article_url": cardnews.article.url,
            "caption": cardnews.full_caption(),
            "image_urls": image_urls,
            "user_tags": cardnews.usernames(),
            "link_url": cardnews.link_url,
            "target": self.settings.ig_publish_target,
        }
        try:
            resp = requests.post(
                self.settings.publish_webhook_url,
                json=payload,
                timeout=self.settings.request_timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PublishError("발행 웹훅 호출 실패: {}".format(exc)) from exc
        return PublishResult(
            article_url=cardnews.article.url,
            status="published",
            detail="webhook {}".format(resp.status_code),
        )


class GraphPublisher(Publisher):
    """Instagram Graph API 컨테이너 → 발행 2단계 흐름."""

    name = "graph"

    def __init__(self, settings: Settings):
        missing = missing_publish_config(settings)
        if missing:
            raise PublishError(
                "발행 설정이 없어 실행을 중단합니다: {} (설정 없이 확인하려면 --dry-run)".format(
                    ", ".join(missing)
                )
            )
        self.settings = settings
        self.base = "https://graph.facebook.com/{}/{}".format(
            settings.ig_api_version, settings.ig_user_id
        )
        self.session = requests.Session()
        self.tag_warning = ""

    def _post(self, path: str, params: Dict[str, str]) -> Dict[str, str]:
        url = "{}/{}".format(self.base, path) if path else self.base
        params = dict(params)
        params["access_token"] = self.settings.ig_access_token or ""
        try:
            resp = self.session.post(url, data=params, timeout=self.settings.request_timeout)
        except requests.RequestException as exc:
            raise PublishError("Graph API 요청 실패({}): {}".format(path, exc)) from exc
        try:
            data = resp.json()
        except ValueError:
            raise PublishError(
                "Graph API 응답 파싱 실패({}): HTTP {} {}".format(path, resp.status_code, resp.text[:200])
            )
        if resp.status_code >= 400 or "error" in data:
            detail = json.dumps(data.get("error", data), ensure_ascii=False)[:400]
            raise PublishError("Graph API 오류({}): HTTP {} {}".format(path, resp.status_code, detail))
        return data

    def _wait_ready(self, creation_id: str, attempts: int = 12, interval: int = 5) -> None:
        """캐러셀 컨테이너가 FINISHED 될 때까지 기다린다."""
        url = "https://graph.facebook.com/{}/{}".format(self.settings.ig_api_version, creation_id)
        for _ in range(attempts):
            try:
                resp = self.session.get(
                    url,
                    params={
                        "fields": "status_code,status",
                        "access_token": self.settings.ig_access_token,
                    },
                    timeout=self.settings.request_timeout,
                )
                data = resp.json()
            except (requests.RequestException, ValueError):
                time.sleep(interval)
                continue
            status = data.get("status_code")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise PublishError("미디어 컨테이너 처리 실패: {}".format(data.get("status", "")))
            time.sleep(interval)
        raise PublishError("미디어 컨테이너가 준비되지 않았습니다(타임아웃): {}".format(creation_id))

    def _user_tags_param(self, cardnews: CardNews) -> Optional[str]:
        """카드 이미지에 글자로 찍는 대신, 업로드 시 계정을 기능적으로 태그한다.

        사진 태그는 좌표가 필수다(x/y가 없으면 error_subcode 2207063으로 거절된다).
        좌표는 0.0~1.0의 이미지 상대 위치이고, 같은 자리에 겹치면 역시 거절되므로
        커버 아래쪽 여백에 세로로 벌려 놓는다. 태그 위치는 화면에 보이지 않는다.
        """
        usernames = cardnews.usernames()[:20]  # 이미지당 태그 상한
        if not usernames:
            return None
        tags = []
        for i, name in enumerate(usernames):
            # 0.62에서 시작해 0.06씩 내려가되 이미지 밖(1.0)으로는 나가지 않는다.
            tags.append({"username": name, "x": 0.5, "y": round(min(0.62 + i * 0.06, 0.95), 2)})
        return json.dumps(tags, ensure_ascii=False)

    def _create_media(self, params: Dict[str, str], user_tags: Optional[str]) -> Dict[str, str]:
        """user_tags를 붙여 컨테이너를 만들고, 태그 때문에 거절되면 태그 없이 재시도한다.

        상대 계정이 태그를 허용하지 않거나 계정명이 바뀌면 발행 전체가 실패할 수 있다.
        태그는 부가 기능이므로, 게시물 자체를 살리는 쪽을 택한다.
        """
        if not user_tags:
            return self._post("media", params)
        try:
            return self._post("media", dict(params, user_tags=user_tags))
        except PublishError as exc:
            if not _looks_like_tag_error(str(exc)):
                raise
            log.warning("계정 태그가 거절되어 태그 없이 발행합니다: %s", exc)
            self.tag_warning = str(exc)[:200]
            return self._post("media", params)

    def publish(self, cardnews: CardNews, image_urls: List[str]) -> PublishResult:
        caption = cardnews.full_caption()
        user_tags = self._user_tags_param(cardnews)
        self.tag_warning = ""

        if self.settings.ig_publish_target == "story":
            container = self._create_media(
                {"image_url": image_urls[0], "media_type": "STORIES"}, user_tags
            )
            creation_id = container["id"]
        else:
            children: List[str] = []
            for position, url in enumerate(image_urls[:10]):  # 캐러셀 최대 10장
                params = {"image_url": url, "is_carousel_item": "true"}
                # 태그는 커버(첫 장)에만 붙인다. 슬라이드마다 반복하면 알림만 중복된다.
                child = self._create_media(params, user_tags if position == 0 else None)
                children.append(child["id"])
            container = self._post(
                "media",
                {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption},
            )
            creation_id = container["id"]
            self._wait_ready(creation_id)

        published = self._post("media_publish", {"creation_id": creation_id})
        media_id = published.get("id", "")

        permalink = None
        try:
            resp = self.session.get(
                "https://graph.facebook.com/{}/{}".format(self.settings.ig_api_version, media_id),
                params={"fields": "permalink", "access_token": self.settings.ig_access_token},
                timeout=self.settings.request_timeout,
            )
            permalink = resp.json().get("permalink")
        except (requests.RequestException, ValueError):
            log.debug("permalink 조회 실패(발행은 성공)")

        detail = "{} 발행".format(self.settings.ig_publish_target)
        if user_tags and not self.tag_warning:
            detail += " · 계정 태그 {}건".format(len(cardnews.usernames()))
        elif self.tag_warning:
            detail += " · 계정 태그 생략({})".format(self.tag_warning)

        return PublishResult(
            article_url=cardnews.article.url,
            status="published",
            media_id=media_id,
            permalink=permalink,
            detail=detail,
        )


def make_publisher(settings: Settings, dry_run: bool) -> Publisher:
    if dry_run or settings.publisher == "console":
        return ConsolePublisher()
    if settings.publisher == "graph":
        return GraphPublisher(settings)
    if settings.publisher == "webhook":
        return WebhookPublisher(settings)
    raise PublishError("알 수 없는 PUBLISHER: {}".format(settings.publisher))


def notify(settings: Settings, title: str, message: str, level: str = "error") -> Optional[str]:
    """실패/완료 알림 웹훅. 설정이 없으면 조용히 건너뛴다."""
    if not settings.alert_webhook_url:
        return None
    payload = {"level": level, "title": title, "text": "{}\n{}".format(title, message)}
    try:
        resp = requests.post(settings.alert_webhook_url, json=payload, timeout=10)
        return "알림 전송 {}".format(resp.status_code)
    except requests.RequestException as exc:
        log.warning("알림 웹훅 실패: %s", exc)
        return "알림 전송 실패: {}".format(exc)

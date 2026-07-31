"""기획서 '직접 확인할 3가지'(TEST-01/02/03)를 자동 검증한다.

실행: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from socialcard import pipeline as pipeline_mod  # noqa: E402
from socialcard.collect import collect_from_csv, fetch_rss_entries  # noqa: E402
from socialcard.config import Settings  # noqa: E402
from socialcard.errors import CollectError  # noqa: E402
from socialcard.models import PublishResult  # noqa: E402
from socialcard.pipeline import run_pipeline  # noqa: E402
from socialcard.publish import GraphPublisher, build_image_urls  # noqa: E402
from socialcard.store import Store  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = PROJECT / "latest_articles.csv"

EMPTY_RSS = (
    b'<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel>'
    b"<title>empty</title></channel></rss>"
)


class RecordingPublisher:
    """실제 인스타그램 대신 호출 내용을 기록하는 발행기."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def publish(self, cardnews, image_urls):
        self.calls.append(
            {
                "url": cardnews.article.url,
                "caption": cardnews.full_caption(),
                "images": list(image_urls),
            }
        )
        return PublishResult(
            article_url=cardnews.article.url,
            status="published",
            media_id="media_{}".format(len(self.calls)),
            permalink="https://www.instagram.com/p/fake{}/".format(len(self.calls)),
            detail="테스트 발행",
        )


class FakeResponse:
    def __init__(self, payload: Dict[str, Any], status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)
        self.content = EMPTY_RSS

    def json(self) -> Dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("HTTP {}".format(self.status_code))


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="socialcard-test-"))
        self.settings = Settings()
        self.settings.out_dir = self.tmp / "out"
        self.settings.db_path = self.tmp / "state" / "test.sqlite3"
        self.settings.accounts_path = PROJECT / "config" / "accounts.json"
        # 편집자가 실제로 쓰는 config/overrides.csv 를 읽지 않도록 격리한다.
        # (그 파일이 테스트 기사와 같은 URL을 담으면 카드 수가 달라져 엉뚱한 곳에서 깨진다)
        self.settings.overrides_path = self.tmp / "no-overrides.csv"
        self.settings.ai_provider = "rule"  # 테스트는 API 키 없이 결정적으로 동작
        self.settings.publish_delay_seconds = 0
        self.settings.min_articles = 1

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class Test01SuccessFlow(PipelineTestCase):
    """TEST-01 — 대표 성공 흐름: 기사 10건이 카드뉴스로 발행된다."""

    def test_ten_articles_are_rendered_and_published(self) -> None:
        recorder = RecordingPublisher()
        self.settings.article_count = 10
        self.settings.min_articles = 10

        with mock.patch.object(pipeline_mod, "make_publisher", return_value=recorder):
            report = run_pipeline(
                self.settings,
                source="csv",
                csv_path=SAMPLE_CSV,
                limit=10,
                dry_run=False,
                run_id="test01",
            )

        self.assertEqual(report.status, "success", report.error)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.collected, 10)
        self.assertEqual(report.published, 10)
        self.assertEqual(report.failed, 0)
        self.assertEqual(len(recorder.calls), 10, "10건 모두 발행 호출이 있어야 한다")

        # 카러셀 5장(커버+본문3+아웃트로) × 10건 = 50장
        images = sorted((self.settings.out_dir / "test01").glob("*.png"))
        self.assertEqual(len(images), 50)
        for call in recorder.calls:
            self.assertEqual(len(call["images"]), 5)
            self.assertIn("socialimpactnews.net", call["caption"], "캡션에 원문 링크가 있어야 한다")

        # 발행 이력이 DB에 남아 다음 실행의 중복 판단 근거가 된다.
        with Store(self.settings.db_path) as store:
            self.assertEqual(len(store.run_items("test01")), 10)
            with SAMPLE_CSV.open(encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    self.assertTrue(store.is_published(row["url"]))

        report_file = self.settings.out_dir / "test01" / "report.json"
        self.assertTrue(report_file.exists(), "실행 로그(리포트)가 저장되어야 한다")

    def test_graph_publisher_issues_carousel_calls(self) -> None:
        """Graph API 어댑터가 컨테이너 → 캐러셀 → 발행 순서로 호출하고, 커버에만 태그를 붙이는지."""
        from socialcard.accounts import AccountDirectory

        self.settings.ig_user_id = "17841400000000000"
        self.settings.ig_access_token = "TEST_TOKEN"
        self.settings.public_image_base_url = "https://cdn.example.com/cardnews"

        publisher = GraphPublisher(self.settings)
        posts: List[str] = []
        sent: List[Dict[str, Any]] = []

        def fake_post(url, data=None, timeout=None):
            posts.append(url.rsplit("/", 1)[-1])
            sent.append(dict(data or {}))
            if url.endswith("media_publish"):
                return FakeResponse({"id": "media_final"})
            return FakeResponse({"id": "child_{}".format(len(posts))})

        def fake_get(url, params=None, timeout=None):
            if params and params.get("fields") == "status_code,status":
                return FakeResponse({"status_code": "FINISHED"})
            return FakeResponse({"permalink": "https://www.instagram.com/p/abc/"})

        from socialcard.render import render_cardnews
        from socialcard.summarize import build_cardnews

        directory = AccountDirectory([{"name": "충남청년센터", "handle": "cn_youth_test", "kind": "org"}])
        articles = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)
        cardnews = build_cardnews(articles[0], self.settings, directory)
        self.assertEqual(cardnews.usernames(), ["cn_youth_test"])

        render_cardnews(cardnews, self.settings.out_dir / "graph", self.settings)
        urls = build_image_urls(cardnews, self.settings.out_dir, self.settings)
        self.assertTrue(urls[0].startswith("https://cdn.example.com/cardnews/graph/"))

        with mock.patch.object(publisher.session, "post", side_effect=fake_post), mock.patch.object(
            publisher.session, "get", side_effect=fake_get
        ):
            result = publisher.publish(cardnews, urls)

        self.assertEqual(result.status, "published")
        self.assertEqual(result.media_id, "media_final")
        # 카드 5장 컨테이너 + 캐러셀 컨테이너 1 + 발행 1
        self.assertEqual(posts, ["media"] * 6 + ["media_publish"])

        # user_tags는 커버(첫 자식)에만 붙는다.
        self.assertIn("user_tags", sent[0])
        # 좌표가 빠지면 그래프 API가 error_subcode 2207063으로 거절한다.
        tags = json.loads(sent[0]["user_tags"])
        self.assertEqual([t["username"] for t in tags], ["cn_youth_test"])
        for tag in tags:
            self.assertTrue(0.0 <= tag["x"] <= 1.0 and 0.0 <= tag["y"] <= 1.0)
        for child in sent[1:5]:
            self.assertNotIn("user_tags", child)
        self.assertIn("계정 태그 1건", result.detail)

    def test_publish_survives_rejected_user_tags(self) -> None:
        """상대 계정이 태그를 막아둔 경우, 태그를 빼고라도 게시물은 올라간다."""
        from socialcard.accounts import AccountDirectory
        from socialcard.render import render_cardnews
        from socialcard.summarize import build_cardnews

        self.settings.ig_user_id = "1784140000"
        self.settings.ig_access_token = "TEST_TOKEN"
        self.settings.public_image_base_url = "https://cdn.example.com/cardnews"
        publisher = GraphPublisher(self.settings)

        def fake_post(url, data=None, timeout=None):
            if "user_tags" in (data or {}):
                return FakeResponse({"error": {"message": "Invalid username in user_tags"}}, status=400)
            if url.endswith("media_publish"):
                return FakeResponse({"id": "media_final"})
            return FakeResponse({"id": "child"})

        def fake_get(url, params=None, timeout=None):
            return FakeResponse({"status_code": "FINISHED"})

        directory = AccountDirectory([{"name": "충남청년센터", "handle": "blocked_acct", "kind": "org"}])
        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        cardnews = build_cardnews(article, self.settings, directory)
        render_cardnews(cardnews, self.settings.out_dir / "graph2", self.settings)
        urls = build_image_urls(cardnews, self.settings.out_dir, self.settings)

        with mock.patch.object(publisher.session, "post", side_effect=fake_post), mock.patch.object(
            publisher.session, "get", side_effect=fake_get
        ):
            result = publisher.publish(cardnews, urls)

        self.assertEqual(result.status, "published")
        self.assertIn("계정 태그 생략", result.detail)


class Test02EmptyInput(PipelineTestCase):
    """TEST-02 — 빈 입력/수집 실패: 오류를 내고 발행하지 않는다."""

    def test_empty_rss_feed_aborts_before_publishing(self) -> None:
        recorder = RecordingPublisher()

        with mock.patch("socialcard.collect.requests.Session.get", return_value=FakeResponse({})), \
             mock.patch.object(pipeline_mod, "make_publisher", return_value=recorder):
            report = run_pipeline(self.settings, source="rss", dry_run=True, run_id="test02-empty")

        self.assertEqual(report.status, "failed")
        self.assertEqual(report.exit_code, 2)
        self.assertIn("기사 항목이 없습니다", report.error or "")
        self.assertEqual(recorder.calls, [], "수집 실패 시 발행 호출이 없어야 한다")

    def test_unreachable_url_is_reported(self) -> None:
        self.settings.rss_url = "http://127.0.0.1:9/does-not-exist.xml"
        self.settings.request_timeout = 3
        recorder = RecordingPublisher()

        with mock.patch.object(pipeline_mod, "make_publisher", return_value=recorder):
            report = run_pipeline(self.settings, source="rss", dry_run=True, run_id="test02-dead")

        self.assertEqual(report.status, "failed")
        self.assertIn("RSS 요청 실패", report.error or "")
        self.assertEqual(recorder.calls, [])

        # 실패도 실행 로그에 남는다(통과 증거).
        with Store(self.settings.db_path) as store:
            row = [r for r in store.recent_runs(5) if r["run_id"] == "test02-dead"][0]
            self.assertEqual(row["status"], "failed")
            self.assertIn("RSS 요청 실패", row["error"])

    def test_empty_csv_raises_collect_error(self) -> None:
        empty = self.tmp / "empty.csv"
        empty.write_text("article_id,title,url,content,published_at\n", encoding="utf-8")
        with self.assertRaises(CollectError):
            collect_from_csv(empty, self.settings)

    def test_missing_credentials_block_live_run(self) -> None:
        """자격증명이 없으면 카드도 만들지 않고 즉시 중단한다."""
        report = run_pipeline(
            self.settings, source="csv", csv_path=SAMPLE_CSV, limit=1, dry_run=False, run_id="test02-cred"
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("IG_USER_ID", report.error or "")

    def test_alert_webhook_fires_on_failure(self) -> None:
        self.settings.alert_webhook_url = "https://hooks.example.com/alert"
        self.settings.rss_url = "http://127.0.0.1:9/does-not-exist.xml"
        self.settings.request_timeout = 3

        with mock.patch("socialcard.publish.requests.post", return_value=FakeResponse({})) as posted:
            run_pipeline(self.settings, source="rss", dry_run=True, run_id="test02-alert")

        self.assertEqual(posted.call_count, 1, "실패 시 알림 웹훅이 1회 호출되어야 한다")
        payload = posted.call_args.kwargs["json"]
        self.assertEqual(payload["level"], "error")
        self.assertIn("RSS 요청 실패", payload["text"])


class Test03DryRunAndDedupe(PipelineTestCase):
    """TEST-03 — Dry-run 및 중복 방지."""

    def _seed_published(self, limit: int) -> List[str]:
        urls: List[str] = []
        with Store(self.settings.db_path) as store:
            with SAMPLE_CSV.open(encoding="utf-8-sig") as fh:
                for row in list(csv.DictReader(fh))[:limit]:
                    store.mark_published(
                        article_url=row["url"],
                        article_id=row["article_id"],
                        title=row["title"],
                        published_at=row["published_at"],
                        run_id="seed",
                        mode="seed",
                    )
                    urls.append(row["url"])
        return urls

    def test_dry_run_skips_duplicates_and_never_publishes(self) -> None:
        self._seed_published(10)
        recorder = RecordingPublisher()

        with mock.patch.object(pipeline_mod, "make_publisher", return_value=recorder):
            report = run_pipeline(
                self.settings, source="csv", csv_path=SAMPLE_CSV, limit=10, dry_run=True, run_id="test03"
            )

        self.assertEqual(report.collected, 10)
        self.assertEqual(report.skipped_duplicate, 10, "이미 발행된 10건은 모두 스킵되어야 한다")
        self.assertEqual(report.published, 0)
        self.assertEqual(report.status, "success")
        self.assertEqual(recorder.calls, [], "드라이런에서는 발행 호출이 없어야 한다")

        with Store(self.settings.db_path) as store:
            statuses = [r["status"] for r in store.run_items("test03")]
        self.assertEqual(statuses, ["skipped_duplicate"] * 10)

    def test_dry_run_generates_cards_without_publishing(self) -> None:
        report = run_pipeline(
            self.settings, source="csv", csv_path=SAMPLE_CSV, limit=3, dry_run=True, run_id="test03-cards"
        )
        self.assertEqual(report.status, "success")
        self.assertEqual(report.processed, 3)
        self.assertEqual(report.published, 0, "드라이런은 발행 카운트를 올리지 않는다")
        self.assertEqual(len(list((self.settings.out_dir / "test03-cards").glob("*.png"))), 15)

        # 드라이런은 발행 이력을 남기지 않아 다음 실제 실행이 막히지 않는다.
        with Store(self.settings.db_path) as store:
            articles = collect_from_csv(SAMPLE_CSV, self.settings, limit=3)
            for article in articles:
                self.assertFalse(store.is_published(article.url))

    def test_partial_duplicates_publish_only_new_ones(self) -> None:
        self._seed_published(4)
        recorder = RecordingPublisher()

        with mock.patch.object(pipeline_mod, "make_publisher", return_value=recorder):
            report = run_pipeline(
                self.settings, source="csv", csv_path=SAMPLE_CSV, limit=10, dry_run=False, run_id="test03-mix"
            )

        self.assertEqual(report.skipped_duplicate, 4)
        self.assertEqual(report.published, 6)
        self.assertEqual(len(recorder.calls), 6)

    def test_force_flag_republishes(self) -> None:
        self._seed_published(2)
        recorder = RecordingPublisher()

        with mock.patch.object(pipeline_mod, "make_publisher", return_value=recorder):
            report = run_pipeline(
                self.settings, source="csv", csv_path=SAMPLE_CSV, limit=2,
                dry_run=False, force=True, run_id="test03-force",
            )

        self.assertEqual(report.skipped_duplicate, 0)
        self.assertEqual(report.published, 2)


class TestContentQuality(PipelineTestCase):
    """카드뉴스 텍스트가 형식 제약을 지키는지."""

    def test_cards_fit_format_constraints(self) -> None:
        from socialcard.summarize import MAX_CARD_BODY, MAX_KICKER, build_cardnews

        articles = collect_from_csv(SAMPLE_CSV, self.settings, limit=10)
        for article in articles:
            cardnews = build_cardnews(article, self.settings)
            self.assertEqual(len(cardnews.cards), self.settings.total_cards)
            self.assertEqual(cardnews.cards[0].kind, "cover")
            self.assertEqual(cardnews.cards[-1].kind, "outro")
            for card in cardnews.cards:
                self.assertTrue(card.title.strip(), "카드 제목이 비어 있으면 안 된다")
            for card in cardnews.cards[1:-1]:
                self.assertLessEqual(len(card.title), MAX_KICKER)
                self.assertLessEqual(len(card.body), MAX_CARD_BODY)
            self.assertLessEqual(len(cardnews.full_caption()), 2200)

    def test_no_account_is_guessed(self) -> None:
        """등록되지 않은 기관은 절대 @태그되지 않고 unmatched로만 보고된다."""
        from socialcard.accounts import load_directory
        from socialcard.summarize import build_cardnews

        directory = load_directory(self.settings.accounts_path)
        articles = collect_from_csv(SAMPLE_CSV, self.settings, limit=5)
        for article in articles:
            cardnews = build_cardnews(article, self.settings, directory)
            for mention in cardnews.mentions:
                self.assertTrue(directory.lookup(mention.name)[0], "매핑에 없는 계정이 태그됐다")

    def test_rule_summary_uses_only_source_text(self) -> None:
        """규칙 기반 요약은 원문 문장만 쓰므로 카드 본문이 원문에 포함돼야 한다."""
        from socialcard.summarize import build_cardnews

        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        cardnews = build_cardnews(article, self.settings)
        normalized = article.content.replace(" ", "")
        for card in cardnews.cards[1:-1]:
            body = card.body.rstrip("…").replace(" ", "")
            self.assertIn(body[:20], normalized)


class TestTypesetting(unittest.TestCase):
    """구문 단위 끊어읽기 줄바꿈."""

    @staticmethod
    def _measure(text: str) -> float:
        # 한글 1자 = 2, 영문/공백 = 1 로 단순화한 가상 폰트
        return sum(2.0 if ord(ch) > 0x2000 else 1.0 for ch in text)

    def test_words_are_never_split(self) -> None:
        from socialcard.typeset import wrap_paragraph

        text = "도도한콜라보와 충남청년센터가 청년정책 플랫폼 열고닫기와 충남청년포털을 연계해 접근성을 높인다"
        lines = wrap_paragraph(text, self._measure, 40)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(self._measure(line), 40)
        # 원문 어절이 하나도 쪼개지지 않았는지
        self.assertEqual(" ".join(lines).split(), text.split())

    def test_breaks_prefer_phrase_boundaries(self) -> None:
        from socialcard.typeset import wrap_paragraph

        from socialcard.typeset import break_score

        # 끊어읽기 우선순위: 문장부호 > 연결어미 > 두 글자 조사 > 한 글자 조사 > 그 외
        self.assertGreater(break_score("제작,"), break_score("공유하고"))
        self.assertGreater(break_score("공유하고"), break_score("플랫폼에서"))
        self.assertGreater(break_score("플랫폼에서"), break_score("정책을"))
        self.assertGreater(break_score("정책을"), break_score("콘텐츠"))
        self.assertLess(break_score("함께"), 0)

        # 슬랙이 비슷하면 조사 경계를 골라 의미 덩어리를 지킨다.
        text = "충남청년센터가 청년정책 플랫폼을 전국으로 넓힌다"
        lines = wrap_paragraph(text, self._measure, 30)
        self.assertTrue(
            all(break_score(line.split()[-1]) >= 0 for line in lines[:-1]),
            "붙여 읽어야 할 자리에서 줄이 끊겼다: {}".format(lines),
        )

    def test_does_not_break_after_binding_adverb(self) -> None:
        from socialcard.typeset import wrap_paragraph

        # '함께 / 추진한다' 처럼 뒤 어절에 붙는 부사 뒤에서는 끊지 않는다.
        text = "양 기관은 청년정책 정보를 함께 알린다"
        lines = wrap_paragraph(text, self._measure, 30)
        for line in lines[:-1]:
            self.assertFalse(
                line.rstrip().endswith("함께"), "부사 뒤에서 줄이 끊겼다: {}".format(lines)
            )

    def test_no_orphan_last_line(self) -> None:
        from socialcard.typeset import wrap_paragraph

        text = "제주창조경제혁신센터가 제주 로컬브랜드의 수도권 진출을 지원한다"
        lines = wrap_paragraph(text, self._measure, 46)
        if len(lines) > 1:
            self.assertGreater(len(lines[-1]), 4, "마지막 줄에 짧은 단어 하나만 남으면 안 된다")

    def test_long_token_falls_back_to_character_split(self) -> None:
        from socialcard.typeset import wrap_paragraph

        lines = wrap_paragraph("가" * 60, self._measure, 20)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(self._measure(line), 20)


class TestBrandAndTagging(PipelineTestCase):
    def test_footer_uses_press_email_byline(self) -> None:
        self.assertEqual(self.settings.brand_email, "press@soimnews.net")
        self.assertEqual(self.settings.brand_handle, "@soimnews")

    def test_tags_are_functional_not_printed_on_cards(self) -> None:
        """계정 태그는 카드 이미지·캡션에 글자로 찍히지 않고 user_tags로만 나간다."""
        from socialcard.accounts import AccountDirectory
        from socialcard.summarize import build_cardnews

        directory = AccountDirectory(
            [{"name": "수퍼빈", "handle": "superbin_official", "kind": "org"}]
        )
        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=3)[2]  # 수퍼빈 기사
        cardnews = build_cardnews(article, self.settings, directory)

        self.assertEqual(cardnews.usernames(), ["superbin_official"])
        self.assertNotIn(
            "@superbin_official", cardnews.full_caption(), "기본 설정에서는 캡션에 멘션을 넣지 않는다"
        )
        for card in cardnews.cards:
            for field_value in (card.title, card.body, card.footnote, card.highlight):
                self.assertNotIn("@superbin_official", field_value, "카드에 계정이 노출됐다")

        # 필요하면 캡션 멘션을 켤 수 있다.
        self.settings.caption_mentions = True
        cardnews = build_cardnews(article, self.settings, directory)
        self.assertIn("@superbin_official", cardnews.full_caption())

    def test_caption_drives_traffic_to_the_article(self) -> None:
        from socialcard.summarize import build_cardnews

        self.settings.linkinbio_base_url = "https://cdn.example.com/soim/links/"
        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        cardnews = build_cardnews(article, self.settings)
        cardnews.link_url = self.settings.linkinbio_base_url

        caption = cardnews.full_caption()
        self.assertIn(article.url, caption, "기사 원문 URL이 캡션에 있어야 한다")
        self.assertIn(self.settings.link_cta, caption, "프로필 링크 유도 문구가 있어야 한다")
        self.assertIn(self.settings.linkinbio_base_url, caption)
        self.assertLessEqual(len(caption), 2200)
        # 인스타그램은 앞 두 줄만 펼쳐 보이므로 요약이 맨 앞에 와야 한다.
        self.assertFalse(caption.startswith("▶"))
        self.assertTrue(caption.startswith(cardnews.caption.strip()[:20]))

    def test_outro_card_teases_the_article_instead_of_listing_handles(self) -> None:
        from socialcard.summarize import build_cardnews

        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        cardnews = build_cardnews(article, self.settings)
        outro = cardnews.cards[-1]
        self.assertEqual(outro.kind, "outro")
        self.assertIn("전문", outro.title)
        self.assertTrue(outro.footnote, "원문을 봐야 할 이유가 적혀 있어야 한다")
        # 인스타그램 이미지는 클릭되지 않으므로, 눈으로 읽고 찾아갈 주소를 노출한다.
        self.assertIn(self.settings.brand_site, outro.body)

    def test_outro_falls_back_to_profile_link(self) -> None:
        """매체 주소를 비워두면 예전처럼 프로필 링크를 안내한다."""
        from socialcard.summarize import build_cardnews

        self.settings.brand_site = ""
        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        outro = build_cardnews(article, self.settings).cards[-1]
        self.assertIn(self.settings.brand_handle, outro.body)

    def test_own_account_is_not_mentioned(self) -> None:
        """매체 바이라인 때문에 본문에 늘 등장하는 자기 계정은 태그하지 않는다."""
        from socialcard.accounts import AccountDirectory
        from socialcard.summarize import build_cardnews

        directory = AccountDirectory([{"name": "소셜임팩트뉴스", "handle": "soimnews", "kind": "org"}])
        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        article.content += "\n소셜임팩트뉴스는 사회적경제 전문 매체다."
        cardnews = build_cardnews(article, self.settings, directory)
        self.assertNotIn("@soimnews", [m.handle for m in cardnews.mentions])

    def test_resolver_rejects_low_confidence_and_mismatched_handles(self) -> None:
        from socialcard.accounts import AccountResolver

        accept = AccountResolver._accept
        good = {
            "found": True, "handle": "superbin_official", "confidence": "high",
            "profile_url": "https://www.instagram.com/superbin_official/", "reason": "",
        }
        self.assertEqual(accept(good), "superbin_official")

        for bad in (
            dict(good, confidence="medium"),
            dict(good, found=False),
            dict(good, profile_url="https://example.com/superbin_official/"),
            dict(good, handle="superbin"),  # URL의 handle과 불일치
            dict(good, handle="", profile_url="https://www.instagram.com//"),
        ):
            self.assertIsNone(accept(bad), "거절되어야 하는 결과가 통과했다: {}".format(bad))


class TestLinkInBio(PipelineTestCase):
    """캡션 URL은 클릭되지 않으므로, 프로필 링크가 걸릴 랜딩 페이지를 만든다."""

    def test_page_lists_published_articles_with_links(self) -> None:
        from socialcard.linkinbio import write_page

        recorder = RecordingPublisher()
        with mock.patch.object(pipeline_mod, "make_publisher", return_value=recorder):
            run_pipeline(
                self.settings, source="csv", csv_path=SAMPLE_CSV, limit=3,
                dry_run=False, run_id="link01",
            )

        # 자체 도메인을 붙이면 저장소 이름이 경로에서 사라지므로 페이지는 최상위에 둔다.
        page = self.settings.out_dir / "index.html"
        self.assertTrue(page.exists(), "실행 후 링크 페이지가 생성되어야 한다")
        html_text = page.read_text(encoding="utf-8")

        with SAMPLE_CSV.open(encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))[:3]
        for row in rows:
            self.assertIn(row["url"], html_text, "발행한 기사 링크가 페이지에 있어야 한다")
        self.assertIn('target="_blank"', html_text)
        self.assertIn(self.settings.brand_email, html_text)

        # 재실행해도 같은 경로에 최신 목록으로 다시 쓰인다.
        with Store(self.settings.db_path) as store:
            again = write_page(self.settings, store)
        self.assertEqual(again, page)

    def test_empty_history_renders_placeholder(self) -> None:
        from socialcard.linkinbio import write_page

        with Store(self.settings.db_path) as store:
            page = write_page(self.settings, store)
        self.assertIsNotNone(page)
        self.assertIn("아직 발행된 기사가 없습니다", page.read_text(encoding="utf-8"))


class TestCardContentQuality(PipelineTestCase):
    """카드에 담긴 내용이 홍보물로서 쓸모가 있는지."""

    def test_bodies_are_complete_sentences(self) -> None:
        from socialcard.summarize import MAX_CARD_BODY, build_cardnews

        for article in collect_from_csv(SAMPLE_CSV, self.settings, limit=10):
            cardnews = build_cardnews(article, self.settings)
            for card in cardnews.cards[1:-1]:
                self.assertLessEqual(len(card.body), MAX_CARD_BODY)
                self.assertTrue(
                    card.body.endswith(("다.", "요.", "다", "…")),
                    "카드 본문이 문장으로 끝나지 않는다: {}".format(card.body[-25:]),
                )

    def test_lead_sentence_leads(self) -> None:
        """첫 본문 카드는 기사 리드(핵심 요약)를 담는다."""
        from socialcard.summarize import build_cardnews, strip_asides

        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        cardnews = build_cardnews(article, self.settings)
        lead = strip_asides(article.content).replace(" ", "")[:20]
        self.assertIn(lead, cardnews.cards[1].body.replace(" ", ""))

    def test_highlight_picks_dates_and_numbers(self) -> None:
        from socialcard.summarize import find_highlight

        self.assertEqual(find_highlight("오는 9월 5일부터 6일까지 열린다"), "9월 5일")
        self.assertEqual(find_highlight("약 15개 기업을 모집한다"), "15개")
        self.assertEqual(find_highlight("수익금 전액을 돌려준다"), "")
        # 만·억이 낀 수는 앞부터 잡아야 한다. 뒷자리만 잡으면 금액이 틀린다.
        self.assertEqual(find_highlight("정가 1만 2000원의 잡지를 판다"), "1만 2000원")
        self.assertEqual(find_highlight("누적 수익은 63억 원에 달한다"), "63억 원")

    def test_asides_are_stripped_for_readability(self) -> None:
        from socialcard.summarize import strip_asides

        self.assertEqual(
            strip_asides("제주창조경제혁신센터(대표이사 이병선, 이하 제주센터)가 나섰다"),
            "제주창조경제혁신센터가 나섰다",
        )


class TestHookTypes(PipelineTestCase):
    """커버 후킹을 네 유형 중에서 고르게 한 변경."""

    def _payload(self, **over: object) -> dict:
        payload = {
            "hook_type": "deadline",
            "headline": "신청은 이달 31일까지",
            "hook": "제주 로컬브랜드 15곳이 성수동 팝업스토어에 선다.",
            "cards": [
                {
                    "kicker": "무슨 일이",
                    "body": "제주센터가 성수동 팝업스토어에 나갈 참가 기업을 모집한다.",
                    "highlight": "15개사",
                }
                for _ in range(self.settings.body_card_count)
            ],
            "read_more": "심사 기준은 원문에",
            "caption": "제주 로컬브랜드가 성수동에 선다.",
            "hashtags": ["#제주"],
            "entities": [{"name": "제주창조경제혁신센터", "kind": "org"}],
        }
        payload.update(over)
        return payload

    def test_schema_forces_a_choice(self) -> None:
        from socialcard.summarize import HOOK_TYPES, _schema_for

        schema = _schema_for(self.settings)["input_schema"]
        self.assertEqual(schema["properties"]["hook_type"]["enum"], list(HOOK_TYPES))
        self.assertIn("hook_type", schema["required"])

    def test_prompt_explains_every_type(self) -> None:
        from socialcard.summarize import HOOK_TYPES, SYSTEM_PROMPT

        for hook_type in HOOK_TYPES:
            self.assertIn(hook_type, SYSTEM_PROMPT)

    def test_valid_type_survives_validation(self) -> None:
        from socialcard.summarize import validate_payload

        clean = validate_payload(self._payload(hook_type=" Scale "), self.settings)
        self.assertEqual(clean["hook_type"], "scale")

    def test_unknown_type_is_dropped_not_fatal(self) -> None:
        """라벨이 어긋나도 기사 1건을 통째로 버리지 않는다."""
        from socialcard.summarize import validate_payload

        clean = validate_payload(self._payload(hook_type="clickbait"), self.settings)
        self.assertEqual(clean["hook_type"], "")
        self.assertEqual(clean["headline"], "신청은 이달 31일까지")

    def test_fallback_type_is_recorded(self) -> None:
        """폴백도 유형을 적용하면 기록한다. 적용하지 못하면 빈 값으로 남긴다."""
        from socialcard.summarize import HOOK_TYPES, build_cardnews

        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        cardnews = build_cardnews(article, self.settings)
        self.assertEqual(cardnews.generator, "rule")
        self.assertIn(cardnews.hook_type, ("",) + HOOK_TYPES)
        self.assertIn("hook_type", cardnews.to_dict())


class TestOverrides(PipelineTestCase):
    """폴백이 못 만드는 후킹을 편집자가 두 줄로 덮어쓰는 통로."""

    def _cardnews(self) -> Any:
        from socialcard.summarize import build_cardnews

        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        return build_cardnews(article, self.settings)

    def _write(self, rows: List[Dict[str, str]]) -> Path:
        from socialcard.overrides import HEADER

        path = Path(self.tmp) / "overrides.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(HEADER))
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in HEADER})
        return path

    def test_missing_file_changes_nothing(self) -> None:
        from socialcard.overrides import load_overrides

        book = load_overrides(Path(self.tmp) / "없는파일.csv")
        cardnews = self._cardnews()
        before = cardnews.headline
        self.assertEqual(book.apply(cardnews), [])
        self.assertEqual(cardnews.headline, before)

    def test_headline_and_hook_are_replaced_on_the_cover(self) -> None:
        from socialcard.overrides import load_overrides

        cardnews = self._cardnews()
        path = self._write([{
            "article_id": cardnews.article.article_id,
            "headline": "충남 청년정책, 전국에서 검색된다",
            "hook": "충남청년포털과 ‘열고닫기’가 연결된다.",
        }])
        changed = load_overrides(path).apply(cardnews)

        self.assertEqual(sorted(changed), ["headline", "hook"])
        self.assertEqual(cardnews.headline, "충남 청년정책, 전국에서 검색된다")
        # 카드 텍스트는 build_cardnews에서 이미 복사됐으므로 커버도 같이 바뀌어야 한다.
        self.assertEqual(cardnews.cards[0].title, cardnews.headline)
        self.assertEqual(cardnews.cards[0].body, cardnews.hook)
        self.assertEqual(cardnews.hook_type, "manual")

    def test_url_also_matches(self) -> None:
        from socialcard.overrides import load_overrides

        cardnews = self._cardnews()
        path = self._write([{"url": cardnews.article.url, "headline": "URL로 찾은 문구"}])
        self.assertEqual(load_overrides(path).apply(cardnews), ["headline"])
        self.assertEqual(cardnews.headline, "URL로 찾은 문구")

    def test_blank_cells_do_not_overwrite(self) -> None:
        """헤드라인만 고치고 훅은 그대로 두는 것이 기본 사용법이다."""
        from socialcard.overrides import load_overrides

        cardnews = self._cardnews()
        hook_before = cardnews.hook
        path = self._write([
            {"article_id": cardnews.article.article_id, "headline": "새 헤드라인", "hook": ""}
        ])
        self.assertEqual(load_overrides(path).apply(cardnews), ["headline"])
        self.assertEqual(cardnews.hook, hook_before)

    def test_other_articles_are_untouched(self) -> None:
        from socialcard.overrides import load_overrides

        cardnews = self._cardnews()
        before = cardnews.headline
        path = self._write([{"article_id": "SIN9999", "headline": "다른 기사 문구"}])
        self.assertEqual(load_overrides(path).apply(cardnews), [])
        self.assertEqual(cardnews.headline, before)

    def test_drop_cards_removes_and_renumbers(self) -> None:
        """중복되는 본문 카드를 빼면 남은 카드 번호가 1부터 다시 매겨져야 한다.

        번호는 페이지 표기(03/05)·진행 바·파일명에 모두 쓰이므로, 빼기만 하고
        다시 매기지 않으면 '02/04 다음에 04/04'처럼 어긋난다.
        """
        from socialcard.overrides import load_overrides

        cardnews = self._cardnews()
        before = len(cardnews.cards)
        kept_body = cardnews.cards[3].body  # 04번(빼지 않을 카드)의 본문
        path = self._write([{"article_id": cardnews.article.article_id, "drop_cards": "3"}])

        self.assertEqual(load_overrides(path).apply(cardnews), ["drop_cards"])
        self.assertEqual(len(cardnews.cards), before - 1)
        self.assertEqual([c.index for c in cardnews.cards], list(range(1, before)))
        self.assertEqual(cardnews.cards[2].body, kept_body)
        # 구조상 커버와 아웃트로는 남아 있어야 한다.
        self.assertEqual(cardnews.cards[0].kind, "cover")
        self.assertEqual(cardnews.cards[-1].kind, "outro")
        # 문구를 손대지 않았으므로 후킹 유형은 manual 이 아니다.
        self.assertNotEqual(cardnews.hook_type, "manual")

    def test_drop_cards_ignores_cover_and_outro(self) -> None:
        from socialcard.overrides import load_overrides

        cardnews = self._cardnews()
        before = len(cardnews.cards)
        path = self._write([
            {"article_id": cardnews.article.article_id, "drop_cards": "1,{}".format(before)}
        ])
        self.assertEqual(load_overrides(path).apply(cardnews), [])
        self.assertEqual(len(cardnews.cards), before)

    def test_exclude_tags_drops_only_this_article(self) -> None:
        """계정 매핑은 그대로 두고 이 기사에서만 태그를 뺀다."""
        from socialcard.accounts import AccountDirectory
        from socialcard.overrides import load_overrides
        from socialcard.summarize import apply_directory, build_cardnews

        directory = AccountDirectory([
            {"name": "현신경영연구소", "handle": "bk.dia", "kind": "org"},
            {"name": "MYSC", "handle": "mysc.official", "kind": "org"},
        ])
        article = collect_from_csv(SAMPLE_CSV, self.settings, limit=1)[0]
        article.content += "\n현신경영연구소와 MYSC가 함께 참여했다."
        cardnews = build_cardnews(article, self.settings, directory)
        self.assertIn("@mysc.official", cardnews.mention_line())

        path = self._write([
            {"article_id": article.article_id, "exclude_tags": "mysc.official"}
        ])
        self.assertEqual(load_overrides(path).apply(cardnews), ["exclude_tags"])
        self.assertNotIn("@mysc.official", cardnews.mention_line())
        self.assertIn("@bk.dia", cardnews.mention_line())

        # 계정 자동 등록 뒤 태그를 다시 계산해도 제외가 풀리면 안 된다.
        apply_directory(cardnews, directory, exclude_handle=self.settings.brand_handle)
        self.assertNotIn("@mysc.official", cardnews.mention_line())
        # 매핑 자체는 남아 있어야 다른 기사에서 태그된다.
        self.assertTrue(directory.resolve(["MYSC"])[0])

    def test_template_does_not_duplicate_rows(self) -> None:
        """매일 --from-run 을 돌려도 이미 손본 줄을 덮어쓰지 않는다."""
        from socialcard.overrides import write_template

        path = Path(self.tmp) / "overrides.csv"
        rows = [{"article_id": "SIN6833", "url": "https://x/1", "headline": "원래 문구", "hook": "훅"}]
        self.assertEqual(write_template(path, rows), 1)
        self.assertEqual(write_template(path, rows), 0)
        self.assertEqual(write_template(path, rows + [
            {"article_id": "SIN6834", "url": "https://x/2", "headline": "다른 기사", "hook": "훅"}
        ]), 1)

    def test_pipeline_reports_overridden_articles(self) -> None:
        from socialcard.overrides import HEADER

        articles = collect_from_csv(SAMPLE_CSV, self.settings, limit=2)
        path = Path(self.tmp) / "overrides.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(HEADER))
            writer.writeheader()
            writer.writerow({
                "article_id": articles[0].article_id, "url": "",
                "headline": "손으로 쓴 커버", "hook": "", "read_more": "",
            })
        self.settings.overrides_path = path

        report = run_pipeline(
            self.settings, dry_run=True, source="csv", csv_path=SAMPLE_CSV,
            limit=2, resolve_accounts=False,
        )
        self.assertEqual(report.overridden, [articles[0].article_id])
        data = json.loads(
            (Path(report.out_dir) / "{}.json".format(articles[0].article_id)).read_text(encoding="utf-8")
        )
        self.assertEqual(data["headline"], "손으로 쓴 커버")
        self.assertEqual(data["cards"][0]["title"], "손으로 쓴 커버")


class TestForcedLineBreaks(unittest.TestCase):
    """편집자가 지정한 줄바꿈을 조판이 지키는지."""

    def _draw(self) -> Any:
        from PIL import Image, ImageDraw

        return ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def test_layout_keeps_explicit_breaks(self) -> None:
        from socialcard import typeset
        from socialcard.render import load_font

        draw, font = self._draw(), load_font("bold", 84)
        lines, _ = typeset.layout(draw, "내가 사회적경제를 안다고\n말할 수 있을까?", font, 904)
        self.assertEqual(lines, ["내가 사회적경제를 안다고", "말할 수 있을까?"])

    def test_long_segment_still_wraps(self) -> None:
        """지정한 줄바꿈 안에서도 폭을 넘으면 어절 단위로 접힌다."""
        from socialcard import typeset
        from socialcard.render import load_font

        draw, font = self._draw(), load_font("medium", 60)
        text = "폐업하러 갔다가 밭에 남았다.\n고향에 돌아와 다시 적응하는 과정이었다."
        lines, _ = typeset.layout(draw, text, font, 904)
        self.assertEqual(lines[0], "폐업하러 갔다가 밭에 남았다.")
        self.assertGreater(len(lines), 2, "뒷문장은 폭을 넘어 더 접혀야 한다")

    def test_no_break_marker_behaves_as_before(self) -> None:
        from socialcard import typeset
        from socialcard.render import load_font

        draw, font = self._draw(), load_font("bold", 84)
        lines, _ = typeset.layout(draw, "농사를 사내벤처로 시작했습니다", font, 904)
        self.assertTrue(lines)
        self.assertNotIn("", lines)


class TestBrokenAccountsFile(PipelineTestCase):
    """쉼표 하나로 태그 전체가 조용히 꺼지는 일이 없도록."""

    def _broken(self) -> Path:
        path = Path(self.tmp) / "accounts.json"
        # 두 항목 사이 쉼표 누락 — 손으로 편집할 때 가장 흔한 실수
        path.write_text(
            '{"accounts": [\n'
            '  {"name": "빅이슈코리아", "handle": "bigissuekorea", "kind": "org"}\n'
            '  {"name": "수퍼빈", "handle": "superbin_official", "kind": "org"}\n'
            ']}\n',
            encoding="utf-8",
        )
        return path

    def test_load_error_is_reported_not_swallowed(self) -> None:
        from socialcard.accounts import load_directory

        directory = load_directory(self._broken())
        self.assertEqual(len(directory), 0)
        self.assertTrue(directory.load_error, "깨진 이유가 남아 있어야 한다")
        self.assertIn("accounts.json", directory.load_error)

    def test_pipeline_surfaces_the_error(self) -> None:
        self.settings.accounts_path = self._broken()
        report = run_pipeline(
            self.settings, dry_run=True, source="csv", csv_path=SAMPLE_CSV,
            limit=1, resolve_accounts=False,
        )
        self.assertTrue(
            any("계정 매핑" in w for w in report.warnings),
            "실행 요약에 경고가 올라와야 한다: {}".format(report.warnings),
        )

    def test_valid_file_has_no_error(self) -> None:
        from socialcard.accounts import load_directory

        directory = load_directory(PROJECT / "config" / "accounts.json")
        self.assertEqual(directory.load_error, "")
        self.assertGreater(len(directory), 0)


class TestRuleFallbackHook(PipelineTestCase):
    """API 없이 도는 폴백이 기사에 있는 사실을 커버로 끌어올리는지."""

    def _article(self, title: str, content: str) -> Any:
        from socialcard.models import Article

        return Article(
            article_id="SIN0001",
            title=title,
            url="https://www.socialimpactnews.net/news/articleView.html?idxno=1",
            content=content,
            published_at="2026-07-24T09:00:00+09:00",
            section="비즈니스",
        )

    def test_deadline_is_pulled_into_the_headline(self) -> None:
        from socialcard.summarize import build_cardnews

        article = self._article(
            "제주센터, 성수동 팝업스토어 참여기업 모집…제주 로컬브랜드 판로 확대",
            "제주창조경제혁신센터가 서울 성수동에서 팝업스토어를 열고 오는 31일까지 참가 기업을 모집한다.\n"
            "행사는 오는 9월 5일부터 6일까지 이틀간 열린다.\n"
            "제주센터는 심사를 거쳐 약 15개사를 선정할 예정이다.",
        )
        cardnews = build_cardnews(article, self.settings)
        self.assertEqual(cardnews.hook_type, "deadline")
        self.assertIn("31일까지", cardnews.headline)

    def test_org_subject_is_dropped_from_the_headline(self) -> None:
        """지면 제목은 기관이 주어지만, 커버에서 먼저 읽혀야 하는 것은 '무엇을'이다."""
        from socialcard.summarize import _topic_phrase

        self.assertEqual(
            _topic_phrase("헬로우뮤지움, 국제기획전 ‘헬로, 패밀리: 안녕, 우리 집’ 개최"),
            "국제기획전 ‘헬로, 패밀리: 안녕, 우리 집’ 개최",
        )
        # 기관명이 아닌 앞머리는 건드리지 않는다.
        self.assertEqual(
            _topic_phrase("충남 청년정책, 전국 플랫폼 ‘열고닫기’로 만난다"),
            "충남 청년정책, 전국 플랫폼 ‘열고닫기’로 만난다",
        )

    def test_hook_is_a_whole_sentence(self) -> None:
        """잘린 문장은 훅이 되지 못한다. 통째로 들어가는 문장을 고른다."""
        from socialcard.summarize import _pick_hook

        long_lead = "청년정책 데이터 플랫폼을 운영하는 회사가 지난 21일 충남청년센터와 정보 접근성을 높이기 위한 협약을 체결했다"
        short_fact = "양 기관은 오는 9월까지 15개 사업을 함께 추진한다."
        self.assertEqual(_pick_hook([long_lead, short_fact]), short_fact)
        self.assertNotIn("…", _pick_hook([long_lead, short_fact]))

    def test_bullet_dump_is_not_used_as_hook(self) -> None:
        from socialcard.summarize import _pick_hook

        bullets = "선정 기업에 ▲전용 공간 ▲편도 항공권 ▲3박 숙박비 등을 지원한다."
        plain = "행사는 오는 9월 5일부터 6일까지 열린다."
        self.assertEqual(_pick_hook([bullets, plain]), plain)

    def test_body_cards_prefer_sentences_that_fit(self) -> None:
        """전망 문장을 고를 때도 카드에 들어가는 것을 먼저 본다."""
        from socialcard.summarize import MAX_CARD_BODY, _pick_body_sentences

        long_future = "양 기관은 앞으로 " + "다양한 분야에서 협력을 확대해 나갈 " * 6 + "예정이다."
        short_future = "두 기관은 오는 9월까지 15개 사업을 추진할 계획이다."
        self.assertGreater(len(long_future), MAX_CARD_BODY)
        picked = _pick_body_sentences(
            ["리드 문장입니다. 협약이 체결됐다고 밝혔다.", long_future, short_future], 2
        )
        self.assertIn(short_future, picked)
        self.assertNotIn(long_future, picked)

    def test_long_enumeration_loses_to_a_sentence_that_fits(self) -> None:
        """카드에 통째로 들어가지 못하는 나열문이 길이만으로 이기지 않는다."""
        from socialcard.summarize import MAX_CARD_BODY, _fact_score

        dump = "양 기관은 협약에 따라 " + " ".join("▲협력 분야 {}".format(i) for i in range(12)) + " 등에서 협력한다."
        fits = "제주센터는 심사를 거쳐 약 15개사를 선정할 예정이다."
        self.assertGreater(len(dump), MAX_CARD_BODY)
        self.assertGreater(_fact_score(fits), _fact_score(dump))


class TestRssParsing(unittest.TestCase):
    def test_empty_channel_raises(self) -> None:
        settings = Settings()
        with mock.patch("socialcard.collect.requests.Session.get", return_value=FakeResponse({})):
            with self.assertRaises(CollectError):
                fetch_rss_entries(settings)


if __name__ == "__main__":
    unittest.main(verbosity=2)

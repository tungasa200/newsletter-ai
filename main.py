"""
AI Daily Newsletter
- 글로벌 AI 뉴스 RSS 피드를 수집해 Claude로 요약하고 Gmail로 발송합니다.
"""
import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

import anthropic
import feedparser

# ─────────────────────────────────────────────
# 1. RSS 소스 (필요 시 자유롭게 추가/삭제)
# ─────────────────────────────────────────────
RSS_FEEDS = {
    # 매체
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "MIT Tech Review AI": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "Ars Technica AI": "https://arstechnica.com/ai/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "Wired AI": "https://www.wired.com/feed/tag/ai/latest/rss",
    # 빅테크 / 연구소 블로그
    "OpenAI Blog": "https://openai.com/blog/rss.xml",
    "Google AI Blog": "https://blog.google/technology/ai/rss/",
    "Anthropic News": "https://www.anthropic.com/news/rss.xml",
    "DeepMind Blog": "https://deepmind.google/blog/rss.xml",
    "Hugging Face Blog": "https://huggingface.co/blog/feed.xml",
    # 커뮤니티
    "Hacker News (AI)": "https://hnrss.org/newest?q=AI+OR+LLM+OR+%22machine+learning%22&points=50",
}

LOOKBACK_HOURS = 24            # 몇 시간 이내 기사를 수집할지
MAX_PER_SOURCE = 8             # 각 소스에서 최대 몇 개를 가져올지
CLAUDE_MODEL = "claude-sonnet-4-6"  # 비용/품질 밸런스가 좋음. opus를 쓰려면 "claude-opus-4-7"


# ─────────────────────────────────────────────
# 2. RSS 수집
# ─────────────────────────────────────────────
def fetch_recent_articles(hours: int = LOOKBACK_HOURS) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles = []

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            count = 0
            for entry in feed.entries:
                if count >= MAX_PER_SOURCE:
                    break

                # 발행 시각 파싱 (피드마다 형식이 달라서 fallback 처리)
                pub_struct = (
                    getattr(entry, "published_parsed", None)
                    or getattr(entry, "updated_parsed", None)
                )
                if pub_struct:
                    pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
                else:
                    pub_dt = datetime.now(timezone.utc)

                if pub_dt < cutoff:
                    continue

                summary = entry.get("summary", "") or entry.get("description", "")
                # HTML 태그 제거 (간단)
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:600]

                articles.append({
                    "source": source,
                    "title": entry.title,
                    "link": entry.link,
                    "summary": summary.strip(),
                    "published": pub_dt.isoformat(),
                })
                count += 1
        except Exception as e:
            print(f"[WARN] {source} 수집 실패: {e}")

    print(f"[INFO] 총 {len(articles)}개 기사 수집됨")
    return articles


# ─────────────────────────────────────────────
# 3. Claude로 한국어 뉴스레터 생성
# ─────────────────────────────────────────────
def build_newsletter(articles: list[dict]) -> str:
    if not articles:
        return "<p>오늘은 새로운 AI 뉴스가 수집되지 않았습니다.</p>"

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 너무 많으면 토큰 낭비 → 최대 60개로 제한 (소스별 가중치 없이 단순 컷)
    articles_for_prompt = articles[:60]
    articles_block = "\n\n".join(
        f"[{a['source']}] {a['title']}\nURL: {a['link']}\n{a['summary']}"
        for a in articles_for_prompt
    )

    today_kr = datetime.now().strftime("%Y년 %m월 %d일")

    system_prompt = """너는 한국 독자를 위한 AI 산업 뉴스레터 에디터다.
주어진 영문 기사 리스트를 분석해 한국어로 명확하고 깊이 있는 뉴스레터를 작성한다.
번역가가 아니라 큐레이터로서, 중요한 것만 골라 인사이트를 더해 전달한다."""

    user_prompt = f"""아래는 지난 {LOOKBACK_HOURS}시간 동안 수집된 글로벌 AI 관련 기사 목록이야.
이걸 한국어 데일리 뉴스레터로 가공해줘.

# 작성 규칙
1. **오늘의 핵심 (Today's Briefing)**: 첫 부분에 오늘의 흐름을 2~3문장으로 요약.
2. **카테고리 분류**: 기사들을 아래 중 적절한 카테고리로 묶어. 카테고리당 최소 1개, 카테고리는 필요 없으면 생략 가능.
   - 🚀 신규 모델 / 제품 출시
   - 🔬 연구 & 논문
   - 💼 산업 & 비즈니스 동향
   - 📜 정책 / 규제 / 윤리
   - 🛠️ 개발자 도구 / 오픈소스
3. **선별**: 중복되거나 사소한 가십성 기사는 제외. 영향력 있는 5~10개로 압축.
4. **각 기사 형식**: 굵은 한국어 헤드라인 + 2~3문장 한국어 요약 + 출처 명시 + "원문 보기" 링크 (a 태그).
5. **인사이트**: 마지막에 "💡 오늘의 인사이트" 섹션을 두고, 오늘 뉴스를 종합해 시사점/주목할 흐름을 3문장 이내로 정리.
6. **출력 형식**: 완전한 HTML 본문만 출력. <html>, <body> 태그는 포함하지 마. <h2>, <h3>, <p>, <a>, <hr>만 사용.
7. **링크**: 모든 링크는 반드시 target="_blank" 추가.
8. 한국어 자연스러운 어투(존댓말 X, 정보전달형 평어). 이모지는 카테고리 헤더에만 사용.

# 오늘 날짜
{today_kr}

# 수집된 기사
{articles_block}

이제 위 규칙대로 HTML 뉴스레터 본문만 출력해줘. 다른 설명은 붙이지 마."""

    print(f"[INFO] Claude({CLAUDE_MODEL}) 호출 중...")
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text


# ─────────────────────────────────────────────
# 4. Gmail SMTP 발송
# ─────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
          max-width: 720px; margin: 0 auto; padding: 24px; color: #1f2328; line-height: 1.65; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #111; padding-bottom: 8px; }}
  h2 {{ font-size: 18px; margin-top: 28px; color: #0d1117; }}
  h3 {{ font-size: 15px; margin-bottom: 4px; }}
  a  {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .meta {{ color: #656d76; font-size: 12px; margin-bottom: 24px; }}
  hr {{ border: none; border-top: 1px solid #d0d7de; margin: 20px 0; }}
  .footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #d0d7de;
             color: #656d76; font-size: 12px; }}
</style>
</head>
<body>
  <h1>🤖 AI Daily — {date}</h1>
  <div class="meta">지난 24시간의 글로벌 AI 이슈를 큐레이션한 자동 발송 뉴스레터</div>
  {body}
  <div class="footer">
    이 메일은 GitHub Actions에서 자동 생성·발송되었습니다.<br>
    소스: {sources}
  </div>
</body>
</html>"""


def send_email(html_body: str, recipient: str) -> None:
    sender = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    full_html = HTML_TEMPLATE.format(
        date=datetime.now().strftime("%Y.%m.%d (%a)"),
        body=html_body,
        sources=", ".join(RSS_FEEDS.keys()),
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🤖 AI Daily — {datetime.now().strftime('%Y.%m.%d')}"
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    print(f"[INFO] Gmail로 발송 중... → {recipient}")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)
    print("[INFO] 발송 완료 ✅")


# ─────────────────────────────────────────────
# 5. Entry Point
# ─────────────────────────────────────────────
def main():
    articles = fetch_recent_articles()
    newsletter_html = build_newsletter(articles)
    send_email(newsletter_html, os.environ["RECIPIENT_EMAIL"])


if __name__ == "__main__":
    main()

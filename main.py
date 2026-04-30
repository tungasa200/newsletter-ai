"""
AI Daily Newsletter — Newspaper Edition
- RSS 수집 → 이미지 추출(RSS + og:image) → Claude 큐레이션 → 신문 스타일 HTML → Gmail 발송
"""
import json
import os
import re
import smtplib
import ssl
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

import anthropic
import feedparser
import requests

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
RSS_FEEDS = {
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "MIT Tech Review AI": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "Ars Technica AI": "https://arstechnica.com/ai/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "Wired AI": "https://www.wired.com/feed/tag/ai/latest/rss",
    "OpenAI Blog": "https://openai.com/blog/rss.xml",
    "Google AI Blog": "https://blog.google/technology/ai/rss/",
    "Anthropic News": "https://www.anthropic.com/news/rss.xml",
    "DeepMind Blog": "https://deepmind.google/blog/rss.xml",
    "Hugging Face Blog": "https://huggingface.co/blog/feed.xml",
    "Hacker News (AI)": "https://hnrss.org/newest?q=AI+OR+LLM+OR+%22machine+learning%22&points=50",
}

LOOKBACK_HOURS = 24
MAX_PER_SOURCE = 8
CLAUDE_MODEL = "claude-sonnet-4-6"
USER_AGENT = "Mozilla/5.0 (compatible; AINewsletterBot/1.0)"

KR_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


# ─────────────────────────────────────────────
# 1. RSS 수집 + 이미지 추출
# ─────────────────────────────────────────────
def extract_image_from_entry(entry) -> str | None:
    """RSS 엔트리 내부에서 이미지 URL 추출 (HTTP 호출 없음)"""
    for thumb in entry.get("media_thumbnail") or []:
        if thumb.get("url"):
            return thumb["url"]

    for m in entry.get("media_content") or []:
        url = m.get("url")
        if url and (m.get("medium") == "image" or "image" in (m.get("type") or "")):
            return url
    contents = entry.get("media_content") or []
    if contents and contents[0].get("url"):
        return contents[0]["url"]

    for enc in entry.get("enclosures") or []:
        if "image" in (enc.get("type") or "").lower():
            return enc.get("href") or enc.get("url")

    for field in ("summary", "description"):
        text = entry.get(field) or ""
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text)
        if m:
            return m.group(1)

    for c in entry.get("content") or []:
        v = c.get("value") or ""
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', v)
        if m:
            return m.group(1)

    return None


def fetch_og_image(url: str) -> str | None:
    """기사 페이지를 GET해서 og:image / twitter:image 메타 태그 추출"""
    try:
        resp = requests.get(
            url,
            timeout=6,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        if not resp.ok:
            return None
        html = resp.text[:80000]  # head만 보면 충분
        patterns = [
            r'<meta\s+property=["\']og:image(?::secure_url)?["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
            r'<meta\s+name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
        ]
        for p in patterns:
            m = re.search(p, html, re.IGNORECASE)
            if m:
                img = m.group(1).strip()
                if img.startswith("//"):
                    img = "https:" + img
                return img
    except Exception as e:
        print(f"[WARN] og:image 추출 실패 {url}: {e}")
    return None


def clean_text(text: str, max_len: int = 600) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


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
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_dt = (
                    datetime(*pub[:6], tzinfo=timezone.utc)
                    if pub
                    else datetime.now(timezone.utc)
                )
                if pub_dt < cutoff:
                    continue

                articles.append({
                    "source": source,
                    "title": (entry.get("title") or "").strip(),
                    "link": entry.get("link", ""),
                    "summary": clean_text(
                        entry.get("summary") or entry.get("description") or ""
                    ),
                    "image_url": extract_image_from_entry(entry),
                    "published": pub_dt.isoformat(),
                })
                count += 1
        except Exception as e:
            print(f"[WARN] {source} 수집 실패: {e}")

    with_img = sum(1 for a in articles if a["image_url"])
    print(f"[INFO] {len(articles)}개 기사 수집 (RSS 이미지 보유: {with_img})")
    return articles


# ─────────────────────────────────────────────
# 2. Claude로 큐레이션 (JSON 응답)
# ─────────────────────────────────────────────
def curate_with_claude(articles: list[dict]) -> dict:
    if not articles:
        return {"briefing": "", "stories": [], "insight": ""}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    blocks = "\n\n".join(
        f"ID: {i}\n출처: {a['source']}\n제목: {a['title']}\n"
        f"이미지: {'있음' if a['image_url'] else '없음'}\n"
        f"요약: {a['summary'][:400]}"
        for i, a in enumerate(articles[:60])
    )

    today_kr = datetime.now().strftime("%Y년 %m월 %d일")

    system_prompt = (
        "너는 한국 독자를 위한 AI 산업 뉴스레터의 베테랑 에디터다. "
        "번역가가 아니라 큐레이터로서, 사소한 가십을 빼고 영향력 있는 기사만 골라 "
        "인사이트를 더해 한국어로 전달한다. 출력은 반드시 유효한 JSON만, 다른 설명 없이."
    )

    user_prompt = f"""다음은 지난 {LOOKBACK_HOURS}시간 동안 수집된 글로벌 AI 기사 목록이다.
이 중 영향력 있는 6~9개를 선별해 한국어 뉴스레터로 정리해줘.

# 카테고리 (필요한 것만 사용)
- 🚀 신규 모델 / 제품 출시
- 🔬 연구 & 논문
- 💼 산업 & 비즈니스 동향
- 📜 정책 / 규제 / 윤리
- 🛠️ 개발자 도구 / 오픈소스

# JSON 출력 형식 (이 구조 그대로)
{{
  "briefing": "오늘 AI 흐름을 2~3문장으로 요약 (한국어, 평어체)",
  "stories": [
    {{
      "id": 0,
      "category": "🚀 신규 모델 / 제품 출시",
      "headline_ko": "한국어 헤드라인 (15~30자, 임팩트 있게, 따옴표 X)",
      "summary_ko": "2~3 문장 한국어 요약. 핵심 사실 + 의미.",
      "is_featured": false
    }}
  ],
  "insight": "오늘 뉴스 종합 시사점 2~3문장 (한국어, 평어체)"
}}

# 규칙
- 정확히 1개의 story만 is_featured=true (오늘 가장 영향력 큰 것). featured는 반드시 image="있음" 기사 중에서 선택.
- 중복/유사 주제는 1개로 통합.
- 헤드라인은 정보전달형, 클릭베이트 X.
- 카테고리당 최대 3개.
- 오늘 날짜: {today_kr}

# 기사 목록
{blocks}

이제 위 형식의 JSON만 출력."""

    print(f"[INFO] Claude({CLAUDE_MODEL}) 큐레이션 중...")
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "{"},  # prefill로 JSON 시작 강제
        ],
    )

    text = "{" + msg.content[0].text
    text = re.sub(r"```json\s*|\s*```", "", text)
    last = text.rfind("}")
    if last > 0:
        text = text[: last + 1]

    try:
        result = json.loads(text)
        print(f"[INFO] {len(result.get('stories', []))}개 기사 선별됨")
        return result
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON 파싱 실패: {e}\nRaw: {text[:500]}")
        raise


# ─────────────────────────────────────────────
# 3. 누락 이미지 보충 (og:image 병렬 추출)
# ─────────────────────────────────────────────
def enrich_missing_images(articles: list[dict], story_ids: list[int]):
    targets = [(i, articles[i]) for i in story_ids
               if 0 <= i < len(articles) and not articles[i]["image_url"]]
    if not targets:
        return
    print(f"[INFO] og:image 병렬 추출 ({len(targets)}개)...")

    def work(item):
        i, a = item
        return i, fetch_og_image(a["link"])

    with ThreadPoolExecutor(max_workers=5) as ex:
        for i, img in ex.map(work, targets):
            if img:
                articles[i]["image_url"] = img
                print(f"[INFO]  ↳ id={i} 이미지 확보")


# ─────────────────────────────────────────────
# 4. 신문 스타일 HTML 렌더링
# ─────────────────────────────────────────────
PLACEHOLDER_IMG = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 600 320'>"
    "<rect width='600' height='320' fill='%23eaeaea'/>"
    "<text x='50%25' y='50%25' font-family='Georgia' font-size='28' fill='%23999' "
    "text-anchor='middle' dominant-baseline='middle'>AI DAILY</text></svg>"
)


def img_or_placeholder(url):
    return url if url else PLACEHOLDER_IMG


def render_featured(story, article):
    return f"""
<tr><td style="padding:0 32px 28px;">
  <a href="{article['link']}" target="_blank" style="text-decoration:none;color:inherit;display:block;">
    <img src="{img_or_placeholder(article['image_url'])}" width="616" alt=""
         style="width:100%;max-width:616px;height:auto;display:block;border:none;background:#eaeaea;">
    <div style="margin-top:14px;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;color:#c0392b;">
      {story['category']} · {article['source']}
    </div>
    <h2 style="margin:10px 0 14px;font-family:Georgia,'Times New Roman',serif;font-size:30px;line-height:1.2;font-weight:bold;color:#0a0a0a;">
      {story['headline_ko']}
    </h2>
    <p style="margin:0;font-family:Georgia,serif;font-size:16px;line-height:1.65;color:#333;">
      {story['summary_ko']}
    </p>
    <div style="margin-top:14px;font-family:Arial,sans-serif;font-size:12px;color:#0969da;font-weight:bold;">
      원문 보기 →
    </div>
  </a>
</td></tr>
"""


def render_section_header(category_name):
    return f"""
<tr><td style="padding:12px 32px 4px;">
  <div style="border-top:2px solid #0a0a0a;padding-top:10px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;color:#0a0a0a;">
    {category_name}
  </div>
</td></tr>
"""


def render_two_column(items):
    pairs = [items[i:i + 2] for i in range(0, len(items), 2)]
    rows = []
    for pair in pairs:
        cells = []
        for s, a in pair:
            cells.append(f"""
<td width="50%" valign="top" style="padding:14px 8px;">
  <a href="{a['link']}" target="_blank" style="text-decoration:none;color:inherit;display:block;">
    <img src="{img_or_placeholder(a['image_url'])}" width="290" alt=""
         style="width:100%;max-width:290px;height:auto;display:block;border:none;background:#eaeaea;">
    <div style="margin-top:10px;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#888;">
      {a['source']}
    </div>
    <h3 style="margin:6px 0 8px;font-family:Georgia,serif;font-size:18px;line-height:1.3;font-weight:bold;color:#0a0a0a;">
      {s['headline_ko']}
    </h3>
    <p style="margin:0;font-family:Georgia,serif;font-size:14px;line-height:1.55;color:#444;">
      {s['summary_ko']}
    </p>
  </a>
</td>
""")
        while len(cells) < 2:
            cells.append('<td width="50%"></td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")

    return f"""
<tr><td style="padding:0 24px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    {''.join(rows)}
  </table>
</td></tr>
"""


def render_newspaper_html(curation: dict, articles: list[dict]) -> str:
    now = datetime.now()
    today_kr = now.strftime(f"%Y년 %m월 %d일 ({KR_WEEKDAYS[now.weekday()]})")

    valid = [
        (s, articles[s["id"]])
        for s in curation.get("stories", [])
        if 0 <= s.get("id", -1) < len(articles)
    ]

    if not valid:
        body = '<tr><td style="padding:32px;">오늘은 큐레이션할 기사가 없습니다.</td></tr>'
    else:
        # featured 분리 (이미지 있는 것 우선)
        featured = next(
            ((s, a) for s, a in valid if s.get("is_featured") and a["image_url"]),
            next(((s, a) for s, a in valid if s.get("is_featured")), valid[0])
        )
        others = [p for p in valid if p != featured]

        by_cat = OrderedDict()
        for s, a in others:
            by_cat.setdefault(s["category"], []).append((s, a))

        body_parts = [render_featured(*featured)]
        for cat, items in by_cat.items():
            body_parts.append(render_section_header(cat))
            body_parts.append(render_two_column(items))
        body = "\n".join(body_parts)

    insight_block = ""
    if curation.get("insight"):
        insight_block = f"""
<tr><td style="padding:24px 32px 8px;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6e3;border-left:4px solid #c0392b;">
    <tr><td style="padding:20px 24px;">
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:bold;letter-spacing:1.5px;color:#c0392b;text-transform:uppercase;">
        💡 EDITOR'S INSIGHT
      </div>
      <p style="margin:8px 0 0;font-family:Georgia,serif;font-size:15px;line-height:1.65;color:#333;">
        {curation['insight']}
      </p>
    </td></tr>
  </table>
</td></tr>
"""

    sources = ", ".join(RSS_FEEDS.keys())

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Daily — {now.strftime('%Y.%m.%d')}</title>
</head>
<body style="margin:0;padding:0;background:#f0ece4;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0ece4;">
<tr><td align="center" style="padding:24px 12px;">
<table align="center" cellpadding="0" cellspacing="0" border="0" width="680"
       style="max-width:680px;background:#fffdf8;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

  <!-- Masthead -->
  <tr><td style="padding:36px 32px 20px;border-bottom:3px double #0a0a0a;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td valign="bottom" style="font-family:Georgia,'Times New Roman',serif;font-size:46px;font-weight:bold;letter-spacing:-1px;color:#0a0a0a;line-height:1;">
        AI DAILY
      </td>
      <td valign="bottom" align="right" style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#777;line-height:1.6;">
        {today_kr}<br>
        <span style="color:#c0392b;font-weight:bold;">EDITED BY CLAUDE</span>
      </td>
    </tr></table>
  </td></tr>

  <!-- Briefing -->
  <tr><td style="padding:24px 32px 8px;">
    <div style="font-family:Georgia,serif;font-size:16px;line-height:1.7;color:#222;font-style:italic;border-left:3px solid #c0392b;padding-left:16px;">
      {curation.get('briefing', '')}
    </div>
  </td></tr>

  <!-- Featured + Sections -->
  {body}

  <!-- Insight -->
  {insight_block}

  <!-- Footer -->
  <tr><td style="padding:24px 32px 28px;border-top:1px solid #ccc;">
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#888;line-height:1.6;">
      이 메일은 GitHub Actions에서 자동 생성·발송되었습니다.<br>
      Powered by <strong style="color:#c0392b;">Claude</strong> · feedparser · Python<br>
      <span style="color:#aaa;">Sources: {sources}</span>
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ─────────────────────────────────────────────
# 5. Gmail 발송
# ─────────────────────────────────────────────
def send_email(html: str, recipient: str):
    sender = os.environ["GMAIL_ADDRESS"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🗞️ AI Daily — {datetime.now().strftime('%Y.%m.%d')}"
    msg["From"] = f"AI Daily <{sender}>"
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html, "html", "utf-8"))

    print(f"[INFO] Gmail 발송 → {recipient}")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(sender, pwd)
        s.send_message(msg)
    print("[INFO] 발송 완료 ✅")


# ─────────────────────────────────────────────
# 6. Entry Point
# ─────────────────────────────────────────────
def main():
    articles = fetch_recent_articles()
    if not articles:
        print("수집된 기사 없음. 종료.")
        return

    curation = curate_with_claude(articles)
    selected_ids = [s["id"] for s in curation.get("stories", []) if "id" in s]
    enrich_missing_images(articles, selected_ids)

    html = render_newspaper_html(curation, articles)

    # 로컬 디버그용: DEBUG=1 환경변수 설정 시 preview.html 저장
    if os.environ.get("DEBUG"):
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("[INFO] preview.html 저장됨")

    send_email(html, os.environ["RECIPIENT_EMAIL"])


if __name__ == "__main__":
    main()

# 🤖 AI Daily Newsletter

매일 아침 글로벌 AI 뉴스를 자동 수집·요약해서 본인 Gmail로 보내주는 자동화 프로그램입니다.

## 🧱 구성

| 단계 | 기술 | 역할 |
|---|---|---|
| 1. 수집 | `feedparser` + RSS | 12개 주요 AI 매체/블로그에서 24시간 내 기사 수집 |
| 2. 요약 | Claude API (`claude-sonnet-4-6`) | 중요도 선별 + 한국어 요약 + 카테고리 분류 + HTML 생성 |
| 3. 발송 | Gmail SMTP | 본인 Gmail로 자동 발송 |
| 4. 스케줄 | GitHub Actions cron | 매일 한국시간 오전 7시 자동 실행 (PC 꺼져 있어도 동작) |

## 📋 사전 준비

1. **GitHub 계정** — 이 코드를 올릴 저장소가 필요합니다 (private 추천).
2. **Anthropic API Key** — https://console.anthropic.com 에서 발급. 결제 수단 등록 필요(매일 발송 시 월 1~3달러 수준).
3. **Gmail 계정 + 앱 비밀번호** — 본인 Gmail에서 자동 발송용 앱 비밀번호를 만듭니다.

## 🚀 셋업 (10분 소요)

### 1) 저장소 생성
이 폴더 4개 파일(`main.py`, `requirements.txt`, `README.md`, `.github/workflows/newsletter.yml`)을 본인 GitHub의 새 private 저장소에 푸시합니다.

### 2) Gmail 앱 비밀번호 발급
1. Google 계정에서 **2단계 인증을 먼저 활성화**해야 합니다.
2. https://myaccount.google.com/apppasswords 접속
3. 앱 이름은 자유롭게 (예: `AI Newsletter`) 입력 → 16자리 비밀번호가 발급됩니다.
4. 이 16자리를 안전한 곳에 복사 (다시 못 봅니다).

### 3) GitHub Secrets 등록
저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** 에서 아래 4개 등록:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` (Anthropic 콘솔에서 발급한 키) |
| `GMAIL_ADDRESS` | 본인 Gmail 주소 (예: `myname@gmail.com`) |
| `GMAIL_APP_PASSWORD` | 위 2)에서 발급한 16자리 앱 비밀번호 (공백 없이) |
| `RECIPIENT_EMAIL` | 받을 이메일 주소 (본인 Gmail이어도 OK) |

### 4) 첫 실행 (수동 테스트)
저장소 → **Actions** 탭 → **Daily AI Newsletter** → **Run workflow** 버튼 클릭.
1~2분 후 Gmail로 뉴스레터가 도착하면 성공입니다.

이후로는 매일 한국시간 **오전 7시**에 자동 발송됩니다.

## ⚙️ 커스터마이징

### 발송 시간 변경
`.github/workflows/newsletter.yml`의 cron 식을 수정 (UTC 기준):
```yaml
- cron: "0 22 * * *"   # 한국 7am
- cron: "0 23 * * *"   # 한국 8am
- cron: "30 21 * * *"  # 한국 6:30am
```
> ⚠️ GitHub Actions cron은 부하 상황에 따라 5~15분 지연될 수 있습니다.

### RSS 소스 추가/변경
`main.py`의 `RSS_FEEDS` 딕셔너리에 항목을 추가하세요. 한국 매체를 넣으려면:
```python
"AI타임스": "https://www.aitimes.com/rss/allArticle.xml",
"디지털데일리 AI": "https://m.ddaily.co.kr/rss/rss_allArticle.xml",
```

### 모델 변경 (비용/품질 조절)
`main.py`의 `CLAUDE_MODEL` 변수:
- `claude-haiku-4-5-20251001` — 가장 저렴, 빠름
- `claude-sonnet-4-6` — **기본값, 권장**
- `claude-opus-4-7` — 최고 품질, 비용 ↑

### 요약 톤/포맷 변경
`build_newsletter()` 함수 안의 `user_prompt` 문자열을 수정. 카테고리, 어투, 길이 등 자유롭게 조정 가능.

### 로컬에서 테스트하기
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
export GMAIL_ADDRESS="myname@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export RECIPIENT_EMAIL="myname@gmail.com"
python main.py
```

## 💰 예상 비용
- **GitHub Actions**: 월 2,000분 무료. 이 작업은 1회 ~2분이라 무료 한도 내.
- **Anthropic API**: Sonnet 기준 1회 발송에 약 $0.03~0.10 → **월 약 $1~3**.
- **Gmail SMTP**: 무료.

## 🛠 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `Username and Password not accepted` | 앱 비밀번호가 아니라 일반 비밀번호를 넣은 경우. 2단계 인증 후 앱 비밀번호 재발급. |
| `ANTHROPIC_API_KEY 없음` | GitHub Secrets에 정확히 `ANTHROPIC_API_KEY` 이름으로 등록했는지 확인. |
| 메일은 오는데 기사가 0개 | 일부 RSS가 일시적으로 죽었을 수 있음. Actions 로그 확인. |
| 스팸함으로 들어감 | 처음 몇 번은 그럴 수 있음. "스팸 아님"으로 표시 후 안정화됨. |
| cron이 정시에 안 옴 | GitHub Actions cron은 5~15분 지연이 흔함. 발송 시간을 더 일찍 잡으세요. |

## 📝 라이선스
자유롭게 사용·수정하세요.

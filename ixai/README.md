# ixaistudio

외부 AI 추론 API 없이 PyTorch로 실행되는 작은 인과 Transformer와 웹 앱입니다. 모델은 UTF-8 바이트 토큰, 임베딩, 사인/코사인 위치 인코딩, 인과 마스크가 있는 멀티헤드 셀프 어텐션, 피드포워드 층, LayerNorm, 교차 엔트로피 손실, AdamW 역전파로 구성됩니다. 서버 시작 시 내장 기초 자료를 저장하고 별도 프로세스에서 자동 학습을 시작합니다. 범용 사전학습 모델은 아니므로 답변 능력은 제한적입니다. 사용자 제공 데이터는 동의와 관리자 검토 후에만 사용합니다.

사이트 이름은 ixaistudio입니다.

## 포함된 기능

- Google OpenID Connect 로그인·가입과 HMAC 서명 세션, 관리자 역할 (기존 이메일 계정은 로그인만 허용)
- 계정별 대화·기억 분리, 대화 삭제, 계정별 개인 데이터 삭제
- 사용자 제공 텍스트 및 TXT/Markdown/CSV/JSON/PDF/DOCX 업로드
- 텍스트 정제와 SHA-256 중복 제거, 자동 분류, 명시적 동의, 관리자 승인/거부
- 별도 OS 프로세스의 재개 가능한 학습, 학습 손실·검증 손실·다음 토큰 정확도 기록
- 검증 손실이 기존 활성 모델보다 낮을 때만 승격, 불변 체크포인트 버전과 롤백
- 자체 작성 한국어 기초 자료 24개를 서버 시작 시 저장하고 기본 활성화된 자동 학습 시작
- 새 승인 데이터가 추가되면 자동 재학습, 검증 개선 시에만 승격
- 학습 센터에서 최근 학습에 사용한 자료의 제목·분류·본문 미리보기 표시
- Google OpenID Connect 로그인 구현(서버 OAuth 자격 증명 설정 후 활성화)
- 서버 재시작 후에도 유지되는 SQLite 데이터, 체크포인트와 학습 작업
- 사용자 전용 키워드 기반 장기 기억 검색
- 모바일 대응 채팅·학습·관리 화면, 다크 모드
- Docker와 영구 볼륨 설정

## 로컬 실행

Python 3.13과 PyTorch CPU 빌드를 설치할 수 있는 환경에서 실행합니다. PyTorch는 설치 용량이 크며 GPU 사용은 설치된 PyTorch 빌드와 장치에 따라 달라집니다.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
$env:SECRET_KEY = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:BOOTSTRAP_ADMIN_EMAIL = 'admin@example.com'
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

브라우저에서 `http://127.0.0.1:8000`을 열고 처음 계정을 등록합니다. 첫 계정은 관리자입니다. `BOOTSTRAP_ADMIN_EMAIL`과 일치하는 계정도 관리자가 됩니다. 서버와 학습 작업자는 같은 Python 환경, 체크포인트 경로, SQLite 파일을 사용해야 합니다.

데이터는 기본적으로 프로젝트의 `data/`에 저장됩니다. `DATA_DIR` 또는 `DATABASE_PATH`/`MODEL_DIR`로 위치를 바꿀 수 있습니다.

## Docker 배포

```powershell
Copy-Item .env.example .env
# .env의 SECRET_KEY를 충분히 긴 임의 비밀 값으로 바꿉니다.
docker compose up --build -d
```

TLS 종료, 도메인, 백업과 방화벽은 배포 환경에서 구성해야 합니다. 볼륨 `model-data`에 SQLite와 모델 버전을 보관합니다. 컨테이너를 두 개 이상 띄울 때에는 SQLite와 로컬 활성 모델 파일을 공유하는 현재 구성 대신 PostgreSQL, 공유 작업 큐, 모델 파일 잠금/객체 저장소를 추가해야 합니다.

## 설정

| 변수 | 기본값 | 설명 |
|---|---:|---|
| `SECRET_KEY` | 개발용 고정값 | 배포 때 반드시 교체할 토큰 서명 키 |
| `BOOTSTRAP_ADMIN_EMAIL` | 빈 값 | 이 이메일로 가입하면 관리자 권한 부여 |
| `GOOGLE_CLIENT_ID` | 빈 값 | Google Cloud에서 발급한 OAuth 웹 클라이언트 ID |
| `GOOGLE_CLIENT_SECRET` | 빈 값 | 서버 전용 OAuth 클라이언트 비밀 키 |
| `GOOGLE_REDIRECT_URI` | 빈 값 | 클라이언트에 등록한 정확한 콜백 주소 |
| `DATA_DIR` | `./data` | DB·체크포인트 디렉터리 |
| `MODEL_BLOCK_SIZE` | 128 | 토큰 문맥 길이 |
| `MODEL_EMBED_DIM` | 96 | 임베딩 폭 |
| `MODEL_HEADS` | 4 | 어텐션 헤드 수; 임베딩 폭의 약수 |
| `MODEL_LAYERS` | 2 | Transformer 층 수 |
| `MAX_RESPONSE_TOKENS` | 96 | 응답 최대 생성 토큰 수 |

RAM·GPU를 자동으로 감지해 모델 폭을 확대하지는 않습니다. 대시보드의 사용량과 학습 속도를 확인하고 모델 설정을 조정한 뒤 새 데이터 디렉터리에서 시작하세요. GPU가 없으면 CPU에서 작은 배치로 학습합니다.

## 데이터와 자기 개선 경계

대화는 자동으로 학습에 들어가지 않습니다. 사용자는 데이터 제출 화면의 동의 상자를 체크해야 하고, 관리자가 이를 승인한 뒤 학습 작업을 시작해야 합니다. 답변 피드백도 사용자가 `개선 학습에 승인`을 체크한 경우에만 검토 대기 데이터가 됩니다.

서버가 시작되면 전용 내부 시스템 계정이 프로젝트 자체 작성 한국어 기초 자료 24개를 승인 상태로 저장합니다. 사용자가 가입하거나 질문하지 않아도 기본 활성화된 스케줄러가 별도 프로세스에서 첫 학습을 시작합니다. 새로 승인된 자료가 있으면 자동 재학습합니다. 같은 데이터만 반복해 외우는 작업은 만들지 않으므로, 새로운 지식을 계속 배우려면 동의한 새 자료가 필요합니다. 이 작은 초기 자료는 대규모 사전학습 말뭉치를 대신하지 않으며, 학습 이후에도 답변 품질은 제한적일 수 있습니다.

현재 검증 분리는 문서 단위(문서가 하나면 끝부분 10%)이며, 기록 지표는 바이트 단위 다음 토큰 손실과 정확도입니다. 일반적인 언어 이해 능력의 검증 점수는 아닙니다. 데이터가 적으면 검증 값의 변동이 크며, 낮은 검증 손실이 실제 대화 품질 향상을 보장하지 않습니다. 승인된 데이터는 전 사용자 모델의 가중치에 영향을 줄 수 있으므로 민감 정보는 올리지 마세요. 학습된 정보는 데이터 행을 삭제해도 가중치에서 제거되지 않습니다. 삭제 이후에는 남은 승인 데이터로 재학습하고 모델을 교체해야 영향을 줄일 수 있고, 이전 체크포인트에는 기존 영향이 남습니다.

장기 기억 검색은 현재 키워드 중복 방식이며, 벡터 임베딩 검색이나 자동 사실 추출은 구현되어 있지 않습니다. 응답 생성은 바이트 토큰 단위 스트리밍입니다. 초기 무작위 모델은 자연스러운 답변을 보장하지 않습니다. 작은 모델을 만드는 것으로 대규모 사전학습 모델의 품질을 주장하지 않습니다.

Google 로그인을 사용하려면 Google Cloud Console에서 OAuth 웹 클라이언트를 만든 뒤 배포 환경 변수에 세 값을 설정합니다. `ixai.p-e.kr` 배포 시 `GOOGLE_REDIRECT_URI=https://ixai.p-e.kr/api/auth/google/callback`을 설정하고 이 주소를 OAuth 클라이언트의 승인된 리디렉션 URI와 정확히 일치시켜야 합니다. 승인된 JavaScript 원본에도 `https://ixai.p-e.kr`을 추가합니다. 로그인은 `openid`, `email`, `profile` 범위만 요청하며 Drive·메일 데이터는 요청하지 않습니다.

## GitHub 및 커스텀 도메인 배포

배포용 파일은 `GITHUB_DEPLOY.md`를 참고하세요. GitHub에 소스 코드를 올린 뒤에는 Docker 컨테이너와 영구 디스크(`/data`)를 지원하는 앱 호스트가 필요합니다. GitHub Pages는 정적 파일만 제공하므로 PyTorch 추론, FastAPI, 자동 학습 작업을 실행할 수 없습니다. 도메인 `ixai.p-e.kr`은 컨테이너 호스트를 먼저 배포한 다음 호스트가 안내하는 DNS 값으로 연결합니다. Google OAuth에는 `https://ixai.p-e.kr/api/auth/google/callback`을 등록해야 합니다.

## 테스트

```powershell
python -m unittest discover -s tests -v
```

테스트는 모델의 실제 가중치 변경/저장/불러오기/생성, 인증과 계정 격리, 데이터 동의·검토, 대화 스트리밍, 별도 학습 프로세스와 모델 롤백을 확인합니다. `python -m backend.worker --job-id <id>`는 웹 서버에서 실행되는 작업 프로세스 명령입니다.

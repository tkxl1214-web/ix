# GitHub 배포 준비

## 1. GitHub에 올릴 폴더

`ixai` 폴더 전체를 새 GitHub 저장소에 업로드합니다. `.gitignore`는 `.env`, 데이터베이스, 모델 체크포인트, 가상환경을 제외합니다. 배포용 `.env` 파일을 GitHub에 올리지 마세요.

## 2. 앱 호스트 설정

GitHub Pages는 HTML/CSS/JavaScript 정적 사이트만 실행합니다. 이 프로젝트는 PyTorch Transformer, FastAPI API, 별도 학습 프로세스가 필요하므로 Docker 컨테이너를 계속 실행할 수 있는 서버형 앱 호스트에 GitHub 저장소를 연결하세요.

호스트는 다음을 지원해야 합니다.

- 프로젝트의 `Dockerfile`로 빌드하고 컨테이너를 실행
- `/data`에 연결되는 영구 디스크 (SQLite, 모델 가중치와 학습 작업 저장)
- HTTPS, 장기 실행 HTTP 연결(Server-Sent Events), 백그라운드 프로세스
- 초기에는 앱 인스턴스 하나로 실행 (SQLite와 자동 학습 스케줄러 사용)

배포 환경에 다음 값을 비밀 환경 변수로 설정합니다.

| 변수 | 값 |
| --- | --- |
| `SECRET_KEY` | 새롭고 긴 무작위 비밀 문자열 |
| `BOOTSTRAP_ADMIN_EMAIL` | 관리자 계정으로 등록할 Google 이메일 |
| `GOOGLE_CLIENT_ID` | Google Cloud OAuth 웹 클라이언트 ID |
| `GOOGLE_CLIENT_SECRET` | 같은 OAuth 클라이언트 비밀 키 |
| `GOOGLE_REDIRECT_URI` | `https://ixai.p-e.kr/api/auth/google/callback` |
| `DATA_DIR` | `/data` |

모델 크기는 호스트 메모리에 맞춰 조절합니다. 기본 CPU 설정은 작지만 추론·학습 속도는 제한됩니다.

## 3. Google 로그인

Google Cloud OAuth 웹 클라이언트에 다음 주소를 등록합니다.

- 승인된 JavaScript 원본: `https://ixai.p-e.kr`
- 승인된 리디렉션 URI: `https://ixai.p-e.kr/api/auth/google/callback`

OAuth 동의 화면이 테스트 모드라면 테스트 사용자로 등록된 Google 계정만 로그인할 수 있습니다. 다른 사용자가 가입하게 하려면 Google OAuth 앱 게시 절차를 마쳐야 합니다.

## 4. 도메인 연결

호스트에서 서비스용 도메인으로 `ixai.p-e.kr`을 추가한 다음, 호스트 대시보드가 제시하는 DNS 레코드를 `p-e.kr` 도메인의 DNS 관리 화면에 입력합니다. CNAME 대상이나 검증 레코드는 서비스 호스트마다 다르므로 대시보드 값을 그대로 사용하세요. DNS 연결과 TLS 인증서 발급이 완료된 뒤 Google OAuth를 테스트합니다.

## 5. 데이터 유지 확인

서비스를 재배포한 뒤에도 `/data` 영구 디스크가 계속 연결되어 있는지 확인하세요. 일반 임시 컨테이너 파일시스템에 SQLite를 두면 재배포 시 대화, 승인 학습 데이터, 모델 체크포인트가 사라질 수 있습니다. 현재 저장소 구성은 SQLite와 단일 서버 인스턴스 기준이며 다중 인스턴스 운영에는 PostgreSQL과 별도 작업 큐 구성이 필요합니다.

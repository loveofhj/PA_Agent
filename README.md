# WP8 PA Agent

LT2 FRT 도어트림 융착기 전공정의 Local PA Agent 프로토타입입니다. Python 표준 라이브러리, SQLite, 정적 웹 화면으로 구성됩니다. 실행기는 모든 모드에서 Sandbox입니다.

## 워크스테이션에서 실행

Docker Engine과 Compose v2 또는 Docker Desktop을 설치합니다. GitHub에 코드가 게시된 뒤 아래 명령을 사용합니다.

```sh
git clone https://github.com/loveofhj/PA_Agent.git
cd PA_Agent
cp .env.example .env
ollama pull qwen3:14b
docker compose up --build -d
docker compose logs -f pa-agent
```

브라우저에서 `http://localhost:8080`을 열고 **LLM 상태 점검**을 확인합니다. 호스트에서 Ollama 서비스가 실행 중이어야 합니다. 서비스가 별도로 실행되지 않는 환경은 `ollama serve`로 구동합니다. Compose의 HTTP 포트는 호스트 루프백에 게시합니다.

| 변수 | 기본값 |
|---|---|
| LLM_PROVIDER | ollama |
| LLM_MODEL | qwen3:14b |
| LLM_TIMEOUT_SEC | 600, 전체 계획 호출 및 재질의 예산 |
| OLLAMA_URL | http://host.docker.internal:11434 |

Docker Desktop(macOS, Windows 또는 Desktop for Linux)은 기본 Compose부터 실행합니다. 컨테이너에서 이름을 해석하지 못한 경우 먼저 다음 명령으로 확인합니다.

```sh
docker compose exec pa-agent python -c "import socket; print(socket.getaddrinfo('host.docker.internal', 11434))"
```

네이티브 Linux Docker Engine에서 이름 해석 실패가 확인되면 아래 오버라이드를 사용합니다.

```sh
docker compose -f compose.yaml -f compose.linux-host.yaml up -d
```

이름은 해석되지만 연결이 거절되면 Ollama 호스트에서 `curl http://127.0.0.1:11434/api/tags`로 서버 및 모델을 확인합니다. Linux 브리지에서는 Ollama가 컨테이너에서 도달 가능한 호스트 인터페이스에 바인딩되어야 합니다. Ollama 서비스 설정의 `OLLAMA_HOST`를 해당 인터페이스와 포트로 지정하고, 방화벽에서 Docker 접근 범위로 제한합니다. Windows는 Docker Desktop 호스트와 WSL 어느 쪽에서 Ollama가 실행 중인지 함께 확인합니다.

## Ollama 없이 시연

```sh
LLM_PROVIDER=mock docker compose up --build -d
```

PowerShell은 `$env:LLM_PROVIDER = "mock"`을 지정한 뒤 Compose를 실행합니다. 화면에 Ollama 대체 시연 모드가 표시됩니다. Ollama 설정으로 돌아가려면 프로세스 환경에서 Mock 변수를 제거하고 다시 실행합니다. Ollama 연결·모델 오류에는 자동 대체가 없으며 타임아웃은 해당 TaskID의 FAILED 및 LLM_TIMEOUT 이벤트로 남습니다.

1. **작업·인터록**에서 `LT2 FRT 도어트림 LH 1개 조립`을 계획합니다. READY와 S1~S8 순서를 확인하고 Mock 실행을 승인합니다.
2. 각 시나리오마다 새 계획을 만듭니다. 지그 불일치는 Skill 결과 0건과 HOLD, 안전 허가 없음은 SAFE-STOP, 체결 실패 후 복구는 복구 이벤트와 DONE을 확인합니다. 최종 검사 NG에서는 S8 실행이 차단됩니다.
3. **Episode·품질**에서 Mock 수집을 실행해 A등급을 확인합니다. **데이터셋**에서 에피소드를 선택하고 등록한 뒤, 같은 ID로 다시 등록해 새 버전을 확인합니다.
4. **이벤트 로그**에서 EventID, 관련 호출, 검사 결과와 EvidenceURI를 확인합니다. Outbox 전송 대상은 로컬 SQLite입니다.

```sh
python -m unittest discover -s tests -v
LLM_PROVIDER=mock PA_HOST=127.0.0.1 python -m pa.server
```

Compose 볼륨 `pa-data`에 SQLite와 로컬 증빙이 유지됩니다. 직접 실행에서는 `data/`를 사용하며 `PA_DATA_DIR`로 변경할 수 있습니다. 물리 기준은 `config.json`에서 null(TBD)로 관리합니다. Mock 복구 한도와 필수 모달 목록은 시연 설정입니다.

## 범위와 제약

- 지시에 LH/RH와 수량을 명시합니다. 2개 이상은 S1~S8과 LOT 구성을 미리 볼 수 있으며, LOT 기본 크기는 10개입니다. Mock 실행은 1개 작업을 지원합니다. 다제품 실행 및 두 스테이션 스케줄링은 후속 범위입니다.
- Ollama는 계획 결정만 생성합니다. JSON을 검증하고 최대 한 번 재질의합니다. HOLD와 SAFE-STOP의 현장 해제 절차는 승인 주체 확정 후 연동합니다.
- 안전 허가와 설비 신호는 Mock 입력입니다. 현장 PLC의 안전 기능과 Gateway 연동, VLM/VLA 추론, Isaac Sim, 모델 학습, WP1 ICD 연동은 후속 범위입니다.
- MP4는 원본 보관입니다. 프레임 품질은 센서 레코드의 frame_index를 이용합니다. 영상 변환·비식별화·학습 포맷 내보내기·라이브 센서 수집은 후속 범위입니다.
- 접근 권한·보안 등급·보관 기간은 카탈로그 메타데이터입니다. 다중 사용자 인증 및 보존 정책 자동 집행은 후속 범위입니다. 외부 데이터 전송과 원본 파일 다운로드 엔드포인트는 제공 범위에 포함하지 않습니다.

[API 및 데이터 스키마](docs/api.md) · [첨부자료 매핑 및 TBD](docs/design.md)

[FAKE 100개 검증 시나리오](docs/fake100.md)에서 LOT별 합성 검사 데이터와 기존 HTTP 흐름을 자동 검증하는 방법을 확인할 수 있습니다.

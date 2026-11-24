# 로컬 REST API

POST 요청은 application/json입니다. 생성 및 실행 결과는 201, 입력 오류는 400입니다. 계획 처리 실패는 저장된 Task 상태 FAILED 또는 HOLD로 반환합니다. 예제의 호스트는 워크스테이션 기준입니다.

| 메서드 | 경로 | 반환 |
|---|---|---|
| GET | /api/health | provider, model, ready, message |
| GET | /api/dashboard | 상태별 작업 수, 복구 작업 수, 최근 작업과 이벤트 |
| GET | /api/steps, /api/skills | STEP 순서와 카탈로그 |
| POST | /api/tasks | Task, 구조화 계획 또는 보류·실패 상태 |
| GET | /api/tasks, /api/tasks/{TaskID} | 목록 및 상세 |
| POST | /api/tasks/{TaskID}/run | 승인 및 Mock 시나리오 실행 결과 |
| GET | /api/events | 계획·상태·인터록·호출 결과·품질·승격 이벤트 |
| POST / GET | /api/episodes | 수집·정규화·품질 판정 / 이력 |
| POST / GET | /api/datasets | 버전 등록 / 카탈로그와 상세 |
| POST | /api/outbox/replay | {"replayed":1} |

```sh
curl -s http://localhost:8080/api/tasks -H 'Content-Type: application/json' \
  -d '{"instruction":"LT2 FRT 도어트림 LH 1개 조립","JigID":"JIG-01","ToolID":"TOOL-01"}'
curl -s http://localhost:8080/api/tasks/TASK_ID/run -H 'Content-Type: application/json' \
  -d '{"scenario":"recover"}'
curl -s http://localhost:8080/api/episodes -H 'Content-Type: application/json' \
  -d '{"mode":"mock","step_code":"S7","skill_id":"SKL-011"}'
curl -s http://localhost:8080/api/datasets -H 'Content-Type: application/json' \
  -d '{"dataset_id":"wp8-demo","members":[{"episode_id":"EPISODE_ID","split":"train"}]}'
curl -s 'http://localhost:8080/api/datasets?step=S7&skill=SKL-011&source=Sim&grade=A'
```

응답 발췌

```json
{"TaskID":"TASK_ID","EpisodeID":"EXECUTION_EPISODE_ID","RecipeID":"LH","state":"READY"}
{"TaskID":"TASK_ID","state":"DONE","current_step":"S8","inspection":"OK"}
{"episode_id":"EPISODE_ID","source":"Sim","step_code":"S7","quality":{"grade":"A","ready":true}}
{"dataset_id":"wp8-demo","version":1,"source":"Sim","episode_count":1}
```

카탈로그는 dataset_id 및 version을 함께 필터해 특정 버전의 전체 멤버·품질·리니지를 조회합니다. 하나의 버전에서 같은 product_id, jig_id 또는 session_id가 서로 다른 split으로 가면 등록을 거부합니다.

## 수집 계약

파일 수집은 mode=upload, format=csv/json/jsonl, UTF-8 content를 사용합니다. JSON은 배열이고 JSONL은 줄마다 객체입니다. CSV는 스칼라 열용입니다. 최대 10,000개 레코드, 전체 HTTP 본문 32 MiB, UI 파일 합계 20 MiB입니다. video는 선택적 Base64 MP4 원본입니다.

요청 메타데이터는 order_id, task_id, lot_id, jig_id, product_id, session_id, recipe_id, tool_id, instruction, skill_version, model_version, rule_version, calibration_version, final_inspection, label_confirmed를 사용합니다. 최종 레이블은 OK/NG입니다. STEP·Skill 기본값은 S7·SKL-011이며 steps 맵으로 다중 STEP의 step_id를 제공할 수 있습니다. 복구 계열은 series=recovery 및 recovery_history 배열이 필요합니다.

```jsonl
{"timestamp":"2026-11-24T10:00:00+09:00","modality":"robot","value":0.01,"unit":"m","step_code":"S7"}
{"timestamp":"2026-11-24T01:00:00Z","modality":"inspection","result":"OK","step_code":"S7"}
```

수집 record의 공통 메타데이터는 세션 데이터와 대조합니다. robot_id/asset_id는 선택 필드입니다. timestamp는 오프셋 포함 ISO 8601, modality는 video/robot/force/equipment/amr/context/event/teleop/simulation/inspection입니다. received_at, status, stream_id, frame_index, channel, EventID, xyz, frame, coordinate_unit은 품질 검사·표준화의 선택 필드입니다. 변환은 설정의 calibration_version → 원천 frame → 3×4 행렬을 이용합니다. 원본은 보존하고 정규화본과 SHA-256을 등록합니다. 기준 미설정 항목은 thresholds_TBD 및 트리거 결과에서 확인합니다.

배치 계획은 `PREVIEW_ONLY` 및 `batch_preview` (quantity, lot_size, lot_count, remainder)를 반환합니다. 배치 계획의 실행 요청은 HTTP 400입니다.

## 상태 전이

IDLE → PLANNING → READY → VALIDATING → RUNNING → DONE. 각 호출 직전 인터록을 재검사합니다. 조건 차단은 HOLD 또는 SAFE-STOP으로 종료하며, 유효하지 않은 결과는 STEP을 진행시키지 않습니다. 재시도는 RECOVERING → VALIDATING을 거치고 새 SkillInvocationID를 사용합니다. 계획 통신 오류는 FAILED입니다. 이벤트의 EventID는 중복 삽입을 방지합니다. 저장 실패 이벤트는 파일 Outbox로 버퍼링합니다. 실행 완료와 이벤트 저장 사이에서 프로세스가 중단되면 자동 복구·재실행하지 않습니다.

## 운영 대화

`POST /api/chat`은 `{"question":"최근 실패 원인은?","history":[]}`를 받아 `{"answer":"…","provider":"ollama"}`를 반환합니다. 최근 작업 상태, 이상 기록, 수집 집계를 읽어서 응답합니다. 상태 변경과 실행 권한은 없습니다. Ollama 연결 또는 응답 오류는 503으로 반환하며, Mock 모드에서는 로컬 기록을 규칙으로 요약합니다.

## WP1 셀 스케줄

`POST /api/schedules`은 WP1 시연 작업계획을 받아 스케줄을 저장하고, `GET /api/schedules`는 저장된 계획과 개정 이력을 반환합니다. 화면의 **셀 스케줄 → 예제 불러오기**로 샘플을 실행할 수 있습니다. 아래 스키마는 프로토타입 계약입니다.

```json
{
  "plan_id": "WP1-DEMO-001",
  "revision": 1,
  "start_at": "2026-11-24T09:00:00+09:00",
  "orders": [
    {"order_id": "LH-100", "recipe_id": "LH", "quantity": 100, "priority": 50},
    {"order_id": "RH-20", "recipe_id": "RH", "quantity": 20, "priority": 80,
     "release_at": "2026-11-24T09:00:00+09:00", "due_at": "2026-11-24T09:30:00+09:00"}
  ]
}
```

착수 가능 시각 이후에 사용 가능한 셀 중 승인 레시피와 전체 S1~S8 Skill을 지원하는 셀에만 LOT를 배치합니다. 착수 가능 시각이 같은 LOT는 우선순위(높을수록 우선), 납기 순으로 선택합니다. 셀 선택 시 전환 시간이 짧은 셀이 우선이며, 같으면 지원 레시피가 적은 셀을 먼저 사용합니다. 배정 불가 주문과 납기 초과 예상 LOT는 별도 표시합니다. 같은 계획·개정의 동일 입력은 저장 결과를 재사용하고, 입력을 바꾸려면 개정 번호를 올립니다.

셀과 소요시간은 `config.json`의 Mock 설정입니다. 스케줄 한 건 안에서 셀 시간 중복을 방지하며 여러 계획에 걸친 예약, 공유 자원 제약, 실시간 설비 상태, 자동 실행은 후속 연동 범위입니다.

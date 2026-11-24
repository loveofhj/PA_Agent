# WP8 설계 추적

| 자료 | 적용 및 차이 |
|---|---|
| Agentic Architecture v7 | 공통 Cell OCS, 실행 관리, Gateway와 현장 제어의 분리 |
| PA Agent 기능구조 v0.2 | D-01~04 및 S1~S8, 상태 전이, IF-01~08 참조, PascalCase 실행 계약 |
| WP8 기능정의 v0.2 | workbook.json에 12개 시트의 셀 주소·내용, features.json에 26개 기능의 상세 입력·출력·검증과 총괄 원문 필드. 별도 우선순위 열은 없어 null. 05 시트는 참조 매핑이고 config.json은 Mock 순서 |
| VLA Skill v0.1 | SK 작업유형과 SKL 호출 단위는 다대다. SK-01 → 005/006/022 및 VLA 경로 021, SK-02 → 017, SK-03 → 018, SK-04 → 019, SK-05 → 004/013/014, SK-06 → 011. 023은 S8의 추가 LOAD, IL-00은 독립 규칙 |
| 데이터수집 v0.1 | snake_case 수집 스키마. step_id는 인스턴스, step_code는 S1~S8. 원본 로컬, 품질 검사, 불변 데이터셋 버전 |
| 프롬프트 v0.1 | 이벤트별 JSON 결정과 엄격 검증, 1회 재질의. 이번 범위는 계획 결정이며 실행·복구는 상태 머신에서 수행 |

공통 실행 키는 OrderID·TaskID·LotID·JigID·EpisodeID·SubTaskID·StepCode·SkillID·SkillInvocationID·EventID이며, 준비 단계 등 대상이 아직 없는 필드는 null이다. 문서 간 적용 STEP 차이는 workbook.json의 각 표와 features.json의 상세/총괄 필드에 보존한다. 신규 SKL-017~023은 제안 표기다.

TBD는 ISS-01~08 및 수집·VLA 초안의 품질/물리 임계값, 도킹·동기·압입 오차, 승인·재개 주체, ICD, 레시피 기준, 캘리브레이션, 보관 기한, 모델·DT 검증이다. Mock 재시도 1회는 시연 설정이며 운영값은 null이다.

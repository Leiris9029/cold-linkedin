# Agent 아키텍처 전환 계획

## 개요

현재 **파이프라인/오케스트레이터** 구조를 **3-Agent 구조**로 전환.
Claude에게 도구를 주고, 스스로 판단/실행하게 한다.

## Phase 1: 기반 + Agent 1 (회사 리스팅)

### 1-1. `orchestrator/agent.py` 생성 — 공통 Agent 루프

```python
class BaseAgent:
    """Anthropic tool use 기반 Agent 루프"""
    - __init__(tools, system_prompt, model)
    - run(user_request) → str  # 루프: Claude 호출 → tool_use 처리 → 반복 → 완료
    - _execute_tool(name, input) → str
    - _register_tools() → list[dict]  # 서브클래스에서 오버라이드
```

핵심 동작:
1. `messages.create(tools=self.tools, ...)` 호출
2. `response.stop_reason == "tool_use"` → 도구 실행 → `tool_result` 반환 → 다시 호출
3. `response.stop_reason == "end_turn"` → 최종 텍스트 반환
4. 안전장치: 최대 루프 횟수 25회 (무한루프 방지)
5. 콜백: `on_tool_call(name, input)`, `on_tool_result(name, result)` — UI에서 진행상황 표시용

### 1-2. `CompanyListingAgent(BaseAgent)` 구현

**도구 목록 (6개):**

| 도구명 | 출처 | 설명 |
|--------|------|------|
| `search_web` | `research_client._web_search()` | DuckDuckGo 검색 |
| `fetch_webpage` | `research_client._fetch_page_text()` | 웹페이지 텍스트 추출 |
| `search_clinicaltrials` | `research_client.search_trials()` | 임상시험 검색 |
| `search_pubmed` | `research_client.search_pubmed()` + `fetch_pubmed_summaries()` | 논문 검색 |
| `read_file` | `Path.read_text()` | data/ 파일 읽기 (our_products.md 등) |
| `save_results` | 내부 | 최종 결과 JSON 저장 (DB 또는 session_state) |

**시스템 프롬프트:**
- `target_finder/SKILL.md` 내용 + 제품 정보 + 피드백 로그
- `_NO_TOOLS_INSTRUCTION` 제거 (이제 도구를 실제로 사용하니까)
- 대신 "사용 가능한 도구" 안내 추가

**현재 흐름 vs Agent 흐름:**

```
현재 (app_ui.py 394-427번줄):
  1. claude.generate_search_queries() → 쿼리 리스트
  2. rc.search_for_targets(queries) → 웹 컨텍스트 텍스트
  3. claude.find_targets(web_context=...) → 회사 JSON
  4. _auto_verify(result_text) → 검증

Agent:
  사용자: "일본 제약사 중 EEG 관련 회사 찾아줘"
  → Agent가 스스로:
    1. read_file("our_products.md") → 제품 이해
    2. search_web("top EEG pharmaceutical companies Japan") × 여러번
    3. fetch_webpage(리스트 URL) × 여러번
    4. search_clinicaltrials(sponsor="...", condition="EEG") × 여러번
    5. search_pubmed("...", topic="EEG") × 여러번
    6. save_results(최종 JSON)
```

### 1-3. `app_ui.py` Agent 1 연동

- "AI 타겟 추천 실행" 버튼 → `CompanyListingAgent.run(prompt)` 호출
- 기존 3-step 진행바 → Agent 콜백 기반 실시간 진행 표시
- `_auto_verify()` 로직 → Agent 내부로 이동 (Agent가 직접 검증 도구 호출)
- 결과 파싱, Tier 이동, 삭제, 프리셋 저장 등 UI 로직은 유지

### 1-4. 삭제/정리 대상

Agent 1 완성 후 기존 코드 정리:
- `claude_client.generate_search_queries()` → Agent가 직접 검색하므로 불필요
- `claude_client.find_targets()` → Agent의 시스템 프롬프트로 흡수
- `claude_client.cross_check_evidence()` → Agent 내부 도구로 흡수
- `research_client.search_for_targets()` → 개별 도구(search_web, fetch_webpage)로 분해
- `app_ui._auto_verify()` → Agent가 직접 수행

## Phase 2: Agent 2 (이메일 탐색) — 추후

Clay + WHOIS 통합 시 구현. `clay_client.py` + `whois_client.py` 필요.

## Phase 3: Agent 3 (콜드메일 작성/발송) — 추후

coldmail/coldmail_en 스킬 + GMass/Sheets 도구 제공.

---

## 파일 변경 요약 (Phase 1)

| 작업 | 파일 | 변경 내용 |
|------|------|----------|
| 생성 | `orchestrator/agent.py` | BaseAgent + CompanyListingAgent |
| 수정 | `orchestrator/app_ui.py` | 타겟 발굴 탭 → Agent 호출로 교체 |
| 유지 | `orchestrator/research_client.py` | 그대로 (도구로 감싸서 사용) |
| 유지 | `orchestrator/claude_client.py` | Agent 2,3에서 계속 사용. 정리는 나중에 |
| 유지 | `orchestrator/config.py` | 변경 없음 |
| 유지 | `.claude/skills/shared/target_finder/SKILL.md` | Agent 시스템 프롬프트로 활용 |

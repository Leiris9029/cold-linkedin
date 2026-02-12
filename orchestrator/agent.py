"""
Agent framework — Anthropic tool-use based autonomous agents.

Each agent gets a set of tools and a system prompt, then runs in a loop:
  Claude decides which tool to call → we execute it → return result → repeat
  until Claude decides it's done (stop_reason == "end_turn").

Three agents:
  1. CompanyListingAgent — finds target companies via web research
  2. EmailFinderAgent — finds contact emails (Clay + Hunter + WHOIS + Web)
  3. ColdMailAgent — writes and sends cold emails
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from config import CLAUDE_API_KEY, CLAUDE_MODEL, CLAUDE_MODEL_LIGHT, SKILLS_DIR, DATA_DIR

logger = logging.getLogger(__name__)


class BaseAgent:
    """Anthropic tool-use agent loop."""

    MAX_TURNS = 30  # Safety limit to prevent infinite loops

    def __init__(
        self,
        model: str = CLAUDE_MODEL,
        api_key: str = CLAUDE_API_KEY,
        on_tool_call: Callable[[str, dict], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_text: Callable[[str], None] | None = None,
    ):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_text = on_text

    def _get_tools(self) -> list[dict]:
        """Override in subclass to define available tools."""
        raise NotImplementedError

    def _get_system_prompt(self, user_request: str) -> str:
        """Override in subclass to build the system prompt."""
        raise NotImplementedError

    def _execute_tool(self, name: str, input_data: dict) -> str:
        """Override in subclass to dispatch tool calls to actual implementations."""
        raise NotImplementedError

    def _should_continue(self) -> str | None:
        """Hook for subclasses to force continuation when agent tries to end.

        Return a string message to inject and continue, or None to allow ending.
        """
        return None

    def _maybe_reset_conversation(
        self, messages: list[dict], turn: int
    ) -> list[dict] | None:
        """Hook: return new messages list to reset conversation, or None to keep going.

        Override in subclass to periodically compact the conversation history,
        e.g. when context gets too long after processing many items.
        """
        return None

    def _load_skill(self, skill_name: str) -> str:
        """Load a SKILL.md file content."""
        search_paths = [
            SKILLS_DIR / "japan" / skill_name / "SKILL.md",
            SKILLS_DIR / "shared" / skill_name / "SKILL.md",
            SKILLS_DIR / skill_name / "SKILL.md",
        ]
        for path in search_paths:
            if path.exists():
                return path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Skill not found: {skill_name}")

    def _load_data_file(self, filename: str) -> str:
        """Load a data file from data/ directory."""
        path = DATA_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    _RATE_LIMIT_MAX_RETRIES = 5
    _RATE_LIMIT_BASE_WAIT = 65  # seconds — rate limit is per minute

    def _api_call_with_retry(self, **kwargs) -> Any:
        """Call Claude API with automatic retry on 429 rate limit errors."""
        import anthropic

        for attempt in range(self._RATE_LIMIT_MAX_RETRIES):
            try:
                return self.client.messages.create(**kwargs)
            except anthropic.RateLimitError as e:
                wait = self._RATE_LIMIT_BASE_WAIT * (attempt + 1)
                logger.warning(
                    f"Rate limit hit (attempt {attempt + 1}/{self._RATE_LIMIT_MAX_RETRIES}), "
                    f"waiting {wait}s..."
                )
                if self.on_text:
                    self.on_text(f"⏳ Rate limit — {wait}초 대기 후 재시도 ({attempt + 1}/{self._RATE_LIMIT_MAX_RETRIES})")
                time.sleep(wait)
            except anthropic.APIStatusError as e:
                if e.status_code == 529:  # overloaded
                    wait = 30 * (attempt + 1)
                    logger.warning(f"API overloaded, waiting {wait}s...")
                    if self.on_text:
                        self.on_text(f"⏳ API 과부하 — {wait}초 대기 후 재시도")
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"Rate limit exceeded after {self._RATE_LIMIT_MAX_RETRIES} retries")

    def run(self, user_request: str) -> str:
        """Run the agent loop until completion or max turns.

        Returns the final text output from Claude.
        """
        tools = self._get_tools()
        system = self._get_system_prompt(user_request)
        messages = [{"role": "user", "content": user_request}]

        # Model-aware max_tokens: Haiku caps at 8192
        _max_tokens = 8192 if "haiku" in self.model else 16384

        for turn in range(self.MAX_TURNS):
            # Hook: subclass can reset conversation to free context
            new_msgs = self._maybe_reset_conversation(messages, turn)
            if new_msgs is not None:
                messages = new_msgs

            response = self._api_call_with_retry(
                model=self.model,
                max_tokens=_max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )

            # Collect text and tool_use blocks from response
            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            # Emit any text blocks
            for block in assistant_content:
                if block.type == "text" and block.text.strip():
                    if self.on_text:
                        self.on_text(block.text)

            # Check for tool_use blocks first
            tool_blocks = [b for b in assistant_content if b.type == "tool_use"]

            # Handle max_tokens truncation (no tool_use, not end_turn)
            if response.stop_reason == "max_tokens" and not tool_blocks:
                # Response was cut off mid-text — check if we should continue
                continue_msg = self._should_continue()
                if continue_msg:
                    messages.append({"role": "user", "content": continue_msg})
                    continue
                # Otherwise, ask agent to wrap up concisely
                messages.append({
                    "role": "user",
                    "content": (
                        "응답이 잘렸습니다. 요약은 생략하고 남은 작업이 있으면 "
                        "도구를 호출해서 계속 진행하세요. "
                        "모든 작업이 끝났으면 간단히 완료라고만 말해주세요."
                    ),
                })
                continue

            # If no tool use and end_turn, check if subclass wants to force continuation
            if response.stop_reason == "end_turn" and not tool_blocks:
                continue_msg = self._should_continue()
                if continue_msg:
                    messages.append({"role": "user", "content": continue_msg})
                    continue
                # Extract final text
                text_parts = [b.text for b in assistant_content if b.type == "text"]
                return "\n".join(text_parts)

            for b in tool_blocks:
                if self.on_tool_call:
                    self.on_tool_call(b.name, b.input)

            if len(tool_blocks) > 1:
                # Execute I/O-bound tools in parallel
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _exec(block):
                    try:
                        return block.id, self._execute_tool(block.name, block.input), None
                    except Exception as e:
                        logger.warning(f"Tool {block.name} failed: {e}")
                        return block.id, None, e

                results_map = {}
                with ThreadPoolExecutor(max_workers=min(len(tool_blocks), 8)) as pool:
                    futures = {pool.submit(_exec, b): b for b in tool_blocks}
                    for f in as_completed(futures):
                        bid, res, err = f.result()
                        results_map[bid] = res if res is not None else f"Error: {err}"

                tool_results = []
                for b in tool_blocks:
                    result = results_map[b.id]
                    if self.on_tool_result:
                        self.on_tool_result(b.name, result[:500])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": result,
                    })
            else:
                tool_results = []
                for b in tool_blocks:
                    try:
                        result = self._execute_tool(b.name, b.input)
                    except Exception as e:
                        result = f"Error: {e}"
                        logger.warning(f"Tool {b.name} failed: {e}")
                    if self.on_tool_result:
                        self.on_tool_result(b.name, result[:500])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": result,
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        # Safety: max turns reached — ask agent to save what it has
        logger.warning(f"Agent hit max turns ({self.MAX_TURNS})")
        messages.append({
            "role": "user",
            "content": (
                "⚠️ 턴 한도에 도달했습니다. 지금까지 찾은 결과를 즉시 add_contacts로 저장하세요. "
                "아직 처리하지 못한 회사가 있어도, 지금까지의 결과만이라도 저장해주세요."
            ),
        })
        # Give one more turn to save
        try:
            response = self._api_call_with_retry(
                model=self.model,
                max_tokens=8192,
                system=self._get_system_prompt(""),
                messages=messages,
                tools=self._get_tools(),
            )
            for block in response.content:
                if block.type == "tool_use":
                    try:
                        result = self._execute_tool(block.name, block.input)
                        if self.on_tool_result:
                            self.on_tool_result(block.name, result[:500])
                    except Exception as e:
                        logger.warning(f"Final save tool failed: {e}")
                elif block.type == "text" and block.text.strip():
                    if self.on_text:
                        self.on_text(block.text)
        except Exception as e:
            logger.warning(f"Final save attempt failed: {e}")

        return "(Agent reached maximum turns — partial results saved)"


# ═══════════════════════════════════════════════════════════════
# Agent 1: Company Listing
# ═══════════════════════════════════════════════════════════════

class CompanyListingAgent(BaseAgent):
    """Finds target companies via 3-phase pipeline (query→search→analyze).

    Phase 1: Claude generates search queries (1 API call)
    Phase 2: Python executes all searches (0 API calls)
    Phase 3: Claude analyzes results and produces JSON (1 API call)

    Total: 2 API calls instead of 20+.
    """

    def __init__(self, extra_feedback: str = "", **kwargs):
        kwargs.setdefault("model", CLAUDE_MODEL)
        super().__init__(**kwargs)
        from research_client import ResearchClient
        self._rc = ResearchClient()
        self._final_result: str | None = None
        self._extra_feedback = extra_feedback

    @property
    def result_json(self) -> str | None:
        return self._final_result

    # -- not needed for pipeline mode, kept for compatibility --
    def _get_tools(self) -> list[dict]:
        return []

    def _get_system_prompt(self, user_request: str) -> str:
        return ""

    def _execute_tool(self, name: str, input_data: dict) -> str:
        return ""

    def run(self, user_request: str) -> str:
        """3-phase pipeline: query generation → batch search → analysis."""
        skill = self._load_skill("target_finder")
        feedback = self._extra_feedback.strip()
        feedback_section = f"\n\n## 과거 피드백 (반드시 우선 반영)\n{feedback}" if feedback else ""

        # Debug: log feedback status
        logger.info(
            "[Agent1] feedback=%d chars, feedback_section=%d chars",
            len(feedback), len(feedback_section),
        )
        if self.on_text and self._extra_feedback:
            self.on_text(f"📋 프로필 피드백 적용됨 ({len(self._extra_feedback)}자)")

        # ── Phase 1: Generate search queries ──────────────
        if self.on_text:
            self.on_text("🔍 Phase 1: 검색 쿼리 생성 중...")

        query_prompt = (
            "You are a market research assistant. "
            "Given the product description below, generate 12-15 diverse web search queries "
            "that would find potential customer companies.\n\n"
            "Requirements:\n"
            "- All queries in English for best coverage\n"
            "- Mix of: industry keywords, company lists, directories, market reports, "
            "competitor analyses, trade publications\n"
            "- Include region-specific queries if a target region is mentioned\n"
            "- Include queries for adjacent markets and verticals\n"
            "- Do NOT generate generic queries — be specific to the product's domain\n\n"
            f"## Product/Request\n{user_request}\n"
            f"{feedback_section}\n\n"
            "Output ONLY a JSON array of query strings, nothing else. Example:\n"
            '[\"EEG biomarker companies clinical trials\", \"neuroscience CRO list 2025\", ...]'
        )

        response = self._api_call_with_retry(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": query_prompt}],
        )
        query_text = response.content[0].text.strip()

        # Parse queries from JSON
        try:
            # Handle markdown code blocks
            if "```" in query_text:
                query_text = query_text.split("```")[1]
                if query_text.startswith("json"):
                    query_text = query_text[4:]
            queries = json.loads(query_text)
            if not isinstance(queries, list):
                queries = [query_text]
        except json.JSONDecodeError:
            # Fallback: split by newlines
            queries = [q.strip().strip('"').strip("'") for q in query_text.split("\n") if q.strip()]

        queries = queries[:15]  # Cap at 15

        if self.on_text:
            self.on_text(f"📋 {len(queries)}개 검색 쿼리 생성 완료")
        if self.on_tool_call:
            self.on_tool_call("search_queries", {"queries": queries})

        # ── Phase 2: Execute all searches in Python ───────
        if self.on_text:
            self.on_text("🌐 Phase 2: 웹 검색 실행 중...")

        def _search_progress(current, total, query):
            if self.on_tool_call:
                self.on_tool_call("search_web", {"query": query, "progress": f"{current+1}/{total}"})

        search_results = self._rc.search_for_targets(
            queries=queries,
            max_per_query=10,
            progress_callback=_search_progress,
        )

        if self.on_text:
            result_len = len(search_results)
            self.on_text(f"📊 검색 완료: {result_len:,}자 데이터 수집")
        if self.on_tool_result:
            self.on_tool_result("search_for_targets", f"Collected {len(search_results):,} chars of search data")

        # ── Phase 3: Analyze and produce JSON ─────────────
        if self.on_text:
            self.on_text("🧠 Phase 3: 결과 분석 및 회사 분류 중...")

        # Truncate search results if too large (keep under ~100K chars for Haiku)
        max_context = 80000
        if len(search_results) > max_context:
            search_results = search_results[:max_context] + "\n\n... (결과 일부 생략)"

        analysis_prompt = (
            f"{skill}\n\n---\n\n"
            f"## 사용자 요청\n{user_request}\n\n"
            f"## 웹 검색 결과 (아래 데이터에서만 회사를 추출할 것)\n\n"
            f"{search_results}\n\n"
            f"---\n"
            f"{feedback_section}\n\n---\n\n"
            f"위 검색 결과를 분석하여 타겟 회사 목록을 JSON으로 출력하세요.\n"
            f"- 검색 결과에서 발견된 회사만 추천 (내장 지식으로 추가 금지)\n"
            f"- evidence는 검색에서 확인된 구체적 사실만 기재\n"
            f"- Tier 1: 최소 30개, Tier 2: 최소 20개 → 합계 50개 이상 목표\n"
            f"- JSON만 출력 (설명 텍스트 불필요)\n"
            f"- ⚠️ 과거 피드백이 있으면 반드시 우선 반영할 것\n"
        )

        response = self._api_call_with_retry(
            model=self.model,
            max_tokens=16384,
            messages=[{"role": "user", "content": analysis_prompt}],
        )

        result_text = response.content[0].text.strip()

        # Extract JSON from response
        json_text = result_text
        if "```" in json_text:
            parts = json_text.split("```")
            for part in parts[1:]:
                candidate = part.strip()
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
                if candidate.startswith("{"):
                    json_text = candidate
                    break

        # Validate and save
        try:
            parsed = json.loads(json_text)
            self._final_result = json.dumps(parsed, ensure_ascii=False)
            t1 = len(parsed.get("tier1_companies", []))
            t2 = len(parsed.get("tier2_companies", []))
            if self.on_text:
                self.on_text(f"✅ 완료: Tier 1: {t1}개, Tier 2: {t2}개, 합계: {t1+t2}개")
            if self.on_tool_result:
                self.on_tool_result("save_results", f"Tier 1: {t1}, Tier 2: {t2}, Total: {t1+t2}")
            return result_text
        except json.JSONDecodeError:
            # If JSON parsing fails, save raw text and let UI handle it
            self._final_result = None
            if self.on_text:
                self.on_text("⚠️ JSON 파싱 실패 — 원본 텍스트를 반환합니다")
            return result_text


# ═══════════════════════════════════════════════════════════════
# Agent 2: Email Finder
# ═══════════════════════════════════════════════════════════════

# ── Title matching utilities ───────────────────────────────

# Abbreviation → expanded forms (bidirectional matching)
_TITLE_EXPANSIONS = {
    "r&d": ["research", "development", "research and development", "r&d"],
    "bd": ["business development", "bd", "biz dev"],
    "cso": ["chief scientific officer", "cso"],
    "cmo": ["chief medical officer", "cmo"],
    "cto": ["chief technology officer", "cto"],
    "cbo": ["chief business officer", "cbo"],
    "vp": ["vice president", "vp", "v.p."],
    "svp": ["senior vice president", "svp", "sr vice president"],
    "evp": ["executive vice president", "evp"],
    "dir": ["director", "dir"],
    "sr": ["senior", "sr", "sr."],
    "assoc": ["associate", "assoc"],
    "mgr": ["manager", "mgr"],
    "hd": ["head", "hd"],
}

# Departments that are always irrelevant for pharma/biotech outreach
_EXCLUDE_DEPARTMENTS = {
    "finance", "accounting", "legal", "compliance", "hr", "human resources",
    "human resource", "talent", "recruiting", "recruitment", "payroll",
    "facilities", "office manager", "receptionist", "administrative",
    "it support", "helpdesk", "help desk", "network admin",
}


def _normalize_title(title: str) -> str:
    """Lowercase, strip, and expand common abbreviations."""
    t = title.lower().strip()
    # Expand abbreviations
    for abbrev, expansions in _TITLE_EXPANSIONS.items():
        if abbrev in t.split():
            for exp in expansions:
                if exp not in t:
                    t = t + " " + exp
    return t


def _extract_title_keywords(titles_str: str) -> set[str]:
    """Extract meaningful keywords from a comma-separated title list.

    "VP BD, Director Research, Head of Translational Medicine"
    → {"vp", "vice president", "bd", "business development", "director",
       "research", "head", "translational", "medicine"}
    """
    if not titles_str:
        return set()

    keywords = set()
    stop_words = {"of", "the", "and", "a", "an", "in", "for", "at", "&"}

    for title in titles_str.split(","):
        normalized = _normalize_title(title)
        words = normalized.split()
        for w in words:
            w = w.strip(".,;:()")
            if w and w not in stop_words and len(w) > 1:
                keywords.add(w)

    return keywords


def _filter_contacts_by_title(
    contacts: list[dict],
    target_titles: str,
) -> tuple[list[dict], list[dict]]:
    """Filter contacts by fuzzy title matching.

    Returns: (matched, unmatched)
    - matched: contacts whose position matches target title keywords
    - unmatched: rest (returned separately so agent can still review if needed)
    """
    if not target_titles.strip():
        return contacts, []  # No filter specified, return all

    keywords = _extract_title_keywords(target_titles)
    if not keywords:
        return contacts, []

    matched = []
    unmatched = []

    for c in contacts:
        position = (c.get("position") or "").lower().strip()
        if not position:
            unmatched.append(c)
            continue

        # Exclude clearly irrelevant departments
        if any(dept in position for dept in _EXCLUDE_DEPARTMENTS):
            unmatched.append(c)
            continue

        # Check if position contains any target keywords
        position_normalized = _normalize_title(position)
        position_words = set(position_normalized.split())

        overlap = keywords & position_words
        if overlap:
            c["_match_keywords"] = list(overlap)
            matched.append(c)
        else:
            # Also try substring matching for multi-word terms
            if any(kw in position_normalized for kw in keywords if len(kw) > 3):
                matched.append(c)
            else:
                unmatched.append(c)

    return matched, unmatched


class EmailFinderAgent(BaseAgent):
    """Finds contact emails at target companies using multiple data sources.

    Tools: findymail_search, findymail_linkedin, whois_lookup,
           hunter_find_email, hunter_verify_email, hunter_domain_search,
           search_web, fetch_webpage, read_file, add_contacts

    Primary: Hunter Domain Search (bulk employee list per company)
    Supplementary: Findymail (verification), WHOIS, Web scraping
    """

    MAX_TURNS = 50  # default, overridden dynamically in __init__

    def __init__(self, search_id: int | None = None, num_companies: int = 0, **kwargs):
        # Default to Haiku for email finding (tool-calling task, no complex reasoning needed)
        kwargs.setdefault("model", CLAUDE_MODEL_LIGHT)
        super().__init__(**kwargs)
        self._findymail = None
        self._hunter = None
        self._whois = None
        self._rc = None
        self._search_id = search_id
        self._final_result: str | None = None
        self._credits_used = {"findymail": 0, "hunter": 0}
        self._accumulated_contacts: list[dict] = []  # Accumulated across add_contacts calls
        self._num_companies = num_companies
        self._force_continue_count = 0  # how many times we've forced continuation
        self._max_force_continues = 3   # give up after this many forced continuations
        self._original_request = ""     # saved on run() for context resets
        self._coverage_at_last_reset = 0  # track progress across resets
        self._max_resets = 3              # max number of context resets
        if num_companies > 0:
            self.MAX_TURNS = max(50, num_companies * 4 + 20)

    def run(self, user_request: str) -> str:
        self._original_request = user_request
        return super().run(user_request)

    def _should_continue(self) -> str | None:
        """Force agent to keep going if not all companies are covered."""
        if not self._num_companies or self._num_companies <= 0:
            return None

        companies_covered = set(
            c.get("company", "").strip().lower()
            for c in self._accumulated_contacts if c.get("company")
        )
        remaining = self._num_companies - len(companies_covered)

        if remaining <= 0:
            return None  # all companies covered, allow ending

        if self._force_continue_count >= self._max_force_continues:
            logger.warning(
                f"Agent2 forced continuation limit reached. "
                f"Covered {len(companies_covered)}/{self._num_companies}."
            )
            return None  # give up to avoid infinite loop

        self._force_continue_count += 1
        logger.info(
            f"Agent2 tried to end early! Covered {len(companies_covered)}/{self._num_companies}. "
            f"Forcing continuation (attempt {self._force_continue_count})."
        )
        covered_list = ", ".join(sorted(companies_covered))
        return (
            f"⛔ 조기 종료 불가! 아직 {remaining}개 회사가 미처리입니다. "
            f"(현재 {len(companies_covered)}/{self._num_companies} 커버)\n\n"
            f"이미 완료된 회사: {covered_list}\n\n"
            f"원래 요청에서 위 목록에 없는 회사를 찾아 아래 단계를 진행하세요:\n"
            f"1. search_web(\"[회사명] official website\")으로 올바른 도메인 확인\n"
            f"2. hunter_domain_search(domain, company_name, target_titles)로 연락처 검색\n"
            f"3. Hunter 0 → findymail_search(name, domain)으로 보충\n\n"
            f"⚠️ 'Unknown'이나 빈 이름은 시스템에서 자동 거부됩니다. "
            f"실제 사람 이름을 찾을 수 없는 회사는 건너뛰세요."
        )

    # ------------------------------------------------------------------
    # Context-reset: keep Haiku context manageable by resetting messages
    # ------------------------------------------------------------------
    _RESET_AFTER_MESSAGES = 60  # ~30 turns worth of messages

    def _maybe_reset_conversation(
        self, messages: list[dict], turn: int
    ) -> list[dict] | None:
        """Reset conversation when it grows too long, preserving progress."""
        if len(messages) < self._RESET_AFTER_MESSAGES:
            return None

        covered = set(
            c.get("company", "").strip().lower()
            for c in self._accumulated_contacts if c.get("company")
        )
        num_covered = len(covered)
        remaining = self._num_companies - num_covered

        if remaining <= 0:
            return None  # almost done, let it finish naturally

        # Don't reset if no new companies since last reset (stuck)
        new_since_last = num_covered - self._coverage_at_last_reset
        if new_since_last <= 0:
            logger.info(
                f"Agent2 skip reset: no new companies since last reset "
                f"({num_covered}/{self._num_companies}). Letting _should_continue handle it."
            )
            return None

        # Enforce max resets
        self._max_resets -= 1
        if self._max_resets < 0:
            logger.info(
                f"Agent2 max resets reached. Covered {num_covered}/{self._num_companies}."
            )
            return None

        # Reset force-continue counter for the new batch
        self._force_continue_count = 0
        self._coverage_at_last_reset = num_covered

        with_email = sum(
            1 for c in self._accumulated_contacts if c.get("email")
        )
        covered_list = ", ".join(sorted(covered))

        logger.info(
            f"Agent2 context reset: {num_covered}/{self._num_companies} covered "
            f"(+{new_since_last} new), {remaining} remaining. Messages: {len(messages)} → 1"
        )
        if self.on_text:
            self.on_text(
                f"🔄 컨텍스트 리셋 — {num_covered}/{self._num_companies} 완료, "
                f"{remaining}개 남음"
            )

        return [{"role": "user", "content": (
            f"=== 컨텍스트 리셋 — 이전 대화 기록은 무시하고 여기서부터 새로 시작 ===\n\n"
            f"## 이미 처리 완료된 회사 ({num_covered}개) — 이 회사들은 건너뛰세요:\n"
            f"{covered_list}\n\n"
            f"## 남은 작업\n"
            f"원래 요청에는 {self._num_companies}개 회사가 있었고, "
            f"위 {num_covered}개는 이미 완료입니다.\n"
            f"**아래 원래 요청에서 위 목록에 없는 회사만 찾아서 처리하세요.**\n"
            f"워크플로우: search_web으로 도메인 확인 → hunter_domain_search → Findymail 보충\n\n"
            f"## 원래 요청\n"
            f"{self._original_request}"
        )}]

    def _get_findymail(self):
        if self._findymail is None:
            from findymail_client import FindymailClient
            self._findymail = FindymailClient()
        return self._findymail

    def _get_hunter(self):
        if self._hunter is None:
            from hunter_client import HunterClient
            self._hunter = HunterClient()
        return self._hunter

    def _get_whois(self):
        if self._whois is None:
            from whois_client import WhoisClient
            self._whois = WhoisClient()
        return self._whois

    def _get_research(self):
        if self._rc is None:
            from research_client import ResearchClient
            self._rc = ResearchClient()
        return self._rc

    @property
    def result_json(self) -> str | None:
        """The saved JSON result from save_contacts tool, if called."""
        return self._final_result

    @property
    def credits_used(self) -> dict:
        return self._credits_used.copy()

    def _get_tools(self) -> list[dict]:
        return [
            {
                "name": "findymail_search",
                "description": (
                    "Find a person's verified email using Findymail (PRIMARY source). "
                    "Requires full name + company domain. Returns verified email instantly. "
                    "Costs 1 credit only if email is found. No charge if not found. "
                    "Use this for each person whose name and domain you know."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Person's full name (e.g. 'John Smith')",
                        },
                        "domain": {
                            "type": "string",
                            "description": "Company domain (e.g. 'eisai.com')",
                        },
                    },
                    "required": ["name", "domain"],
                },
            },
            {
                "name": "findymail_linkedin",
                "description": (
                    "Find a person's verified email from their LinkedIn URL using Findymail. "
                    "Use when you have a LinkedIn URL but not the domain. "
                    "Costs 1 credit only if found."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "linkedin_url": {
                            "type": "string",
                            "description": "Full LinkedIn profile URL",
                        },
                    },
                    "required": ["linkedin_url"],
                },
            },
            {
                "name": "whois_lookup",
                "description": (
                    "Free WHOIS domain lookup to find registrant/admin emails. "
                    "Returns domain-level emails (admin@, info@), NOT personal emails. "
                    "Useful for small companies. Many domains have privacy protection. "
                    "No credits cost."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Domain to lookup (e.g. 'example.com')",
                        },
                    },
                    "required": ["domain"],
                },
            },
            {
                "name": "hunter_domain_search",
                "description": (
                    "Search ALL people at a company domain using Hunter.io. "
                    "Matched contacts are AUTO-SAVED to DB — no need to call add_contacts for Hunter results. "
                    "Returns a text summary (not full JSON). "
                    "Costs 1 Hunter credit per call. "
                    "★ USE THIS FIRST for each company — gets multiple contacts at once! "
                    "★ ALWAYS pass company_name AND target_titles! "
                    "For LARGE companies: if has_more=true, use offset or department/seniority filter."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Company domain (e.g. 'praxismedicines.com')",
                        },
                        "company_name": {
                            "type": "string",
                            "description": "Company name for DB storage (e.g. 'Praxis Medicines'). REQUIRED for auto-save.",
                        },
                        "target_titles": {
                            "type": "string",
                            "description": "Comma-separated target job titles to filter by (e.g. 'VP BD, Director Research, Head of Translational Medicine'). Contacts matching these titles (including similar/equivalent titles) are returned; irrelevant departments (HR, Finance, Legal) are excluded.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results per call (max 100, default 100)",
                            "default": 100,
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Skip first N results for pagination (default 0). Use when has_more=true.",
                            "default": 0,
                        },
                        "department": {
                            "type": "string",
                            "description": "Filter by department: executive, it, finance, management, sales, legal, support, hr, marketing, communication, education, design, health, operations",
                        },
                        "seniority": {
                            "type": "string",
                            "description": "Filter by seniority level: junior, senior, executive",
                        },
                    },
                    "required": ["domain", "company_name"],
                },
            },
            {
                "name": "hunter_find_email",
                "description": (
                    "Find a specific person's email at a company using Hunter.io. "
                    "Requires domain + first_name + last_name. Costs 1 Hunter credit. "
                    "Returns email with confidence score (0-100). "
                    "Use this when you already know someone's name but need their email."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Company domain (e.g. 'eisai.com')",
                        },
                        "first_name": {"type": "string"},
                        "last_name": {"type": "string"},
                    },
                    "required": ["domain", "first_name", "last_name"],
                },
            },
            {
                "name": "hunter_verify_email",
                "description": (
                    "Verify if an email address is deliverable using Hunter.io. "
                    "Costs 0.5 Hunter credits. Returns status: deliverable|risky|undeliverable|unknown. "
                    "Only verify emails with confidence < 90."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "Email to verify"},
                    },
                    "required": ["email"],
                },
            },
            {
                "name": "search_web",
                "description": (
                    "Search the web for contact information, LinkedIn profiles, "
                    "or company team/contact pages. Free but slower. "
                    "Use for hard-to-find contacts or to discover LinkedIn URLs."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (English preferred)",
                        },
                        "max_results": {
                            "type": "integer",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch_webpage",
                "description": (
                    "Fetch and extract text from a webpage. "
                    "Use on company team/about/contact pages or LinkedIn profiles."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"},
                        "max_chars": {"type": "integer", "default": 5000},
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "read_file",
                "description": (
                    "Read a data file. Available: sender_profile.md, "
                    "target_feedback_log.md."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                    },
                    "required": ["filename"],
                },
            },
            {
                "name": "add_contacts",
                "description": (
                    "Add found contacts to the result. Call this MULTIPLE TIMES as you find contacts — "
                    "e.g. after each batch of hunter_domain_search results. "
                    "Each call appends to the running list. Contacts are saved to DB immediately. "
                    "Pass a simple array of contact objects."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contacts": {
                            "type": "array",
                            "description": "Array of contact objects",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "contact_name": {"type": "string", "description": "Full name"},
                                    "email": {"type": "string", "description": "Email address (can be empty)"},
                                    "company": {"type": "string", "description": "Company name"},
                                    "title": {"type": "string", "description": "Job title/position"},
                                    "email_confidence": {"type": "string", "description": "verified/high/medium/low"},
                                    "source": {"type": "string", "description": "hunter/findymail/web"},
                                    "linkedin_url": {"type": "string"},
                                    "location": {"type": "string"},
                                    "fit_score": {"type": "integer", "description": "1-10"},
                                    "fit_reason": {"type": "string"},
                                },
                                "required": ["contact_name", "company"],
                            },
                        },
                    },
                    "required": ["contacts"],
                },
            },
        ]

    def _get_system_prompt(self, user_request: str) -> str:
        skill = self._load_skill("email_finder")

        from config import FINDYMAIL_API_KEY, HUNTER_API_KEY
        available = []
        if FINDYMAIL_API_KEY:
            available.append("Findymail (PRIMARY — name+domain → verified email, real-time)")
        if HUNTER_API_KEY:
            available.append("Hunter.io (email specialist, real-time)")
        available.append("WHOIS (free, domain-level)")
        available.append("Web search (free, unlimited)")

        return (
            f"{skill}\n\n---\n\n"
            f"## 사용 가능한 데이터 소스\n{', '.join(available)}\n\n"
            f"---\n\n"
            f"## 당신은 Email Finder Agent입니다\n\n"
            f"사용자의 요청에 따라 **직접** 도구를 호출하여 연락처 이메일을 찾으세요.\n"
            f"각 회사에서 **가능한 한 많은 연락처** (최소 3명, 5명 이상 권장)를 확보하세요. "
            f"사용자가 지정한 직함에 해당하는 사람이라면 모두 포함하세요.\n\n"
            f"### ⛔ 최우선 절대 규칙 — 조기 종료 절대 금지\n"
            f"- **사용자가 지정한 모든 회사를 빠짐없이 처리할 때까지 절대로 종료하지 마세요**\n"
            f"- 시스템이 자동으로 처리된 회사 수를 추적합니다. 미처리 회사가 있으면 **강제로 계속**됩니다\n"
            f"- \"이만하면 충분\", \"나머지는 데이터가 없다\", \"대부분 처리됨\" — 이런 판단 자체가 금지입니다\n"
            f"- **회사 수 추적을 반드시 유지하세요** (예: \"12/34 완료, 미처리: CompanyA, CompanyB...\")\n"
            f"- Hunter에서 0이면 → 도메인 재확인 → Findymail → 웹 검색 순으로 **모두 시도** 후에야 다음으로\n"
            f"- **hunter_domain_search 결과는 자동으로 DB에 저장됩니다** — add_contacts 불필요!\n"
            f"- Findymail/웹 검색으로 찾은 연락처만 `add_contacts`로 저장하세요\n\n"
            f"### 필수 규칙\n"
            f"- **사용자가 지정한 직함 및 동등/유사 직함을 검색** — 회사마다 타이틀 표기가 다를 수 있으므로 같은 역할의 변형은 포함 (예: 'VP R&D' → SVP Research, EVP R&D도 OK). 단, 완전히 다른 부서(CEO, CFO, HR, Legal, Sales)는 제외\n"
            f"- 사용자 요청에 회사, 직함, 지역이 모두 명시되어 있으므로 추가 파일을 읽을 필요 없음\n\n"
            f"### 효율성 규칙 (★ 최우선 — 반드시 따르세요)\n"
            f"- **한 턴에 최대 8개 도구를 병렬 호출 가능** — 이것이 속도의 핵심!\n"
            f"- **hunter_domain_search를 한 턴에 5~8개 회사씩 배치 호출** (아래 워크플로우 참조)\n"
            f"- findymail_search도 한 턴에 5~8명씩 배치 호출\n"
            f"- **fetch_webpage 최소화**: 검색 snippet에서 이름+직함이 충분하면 페이지를 따로 열지 말 것\n"
            f"- **모든 회사를 빠짐없이 커버하는 것이 최우선 목표** — 한 회사에 너무 오래 머물지 말 것\n\n"
            f"### 워크플로우 (★ 중요 — 이 순서를 반드시 따르세요)\n\n"
            f"**Step 1: 도메인 확인** (2~3턴)\n"
            f"- 사용자 요청에서 회사 목록, 타겟 직함 파악\n"
            f"- **도메인 추측 금지!** 바이오텍 회사는 도메인이 회사명과 다른 경우가 매우 많음\n"
            f"  (예: Sage Therapeutics → sagerx.com, Cognition Therapeutics → cogrx.com)\n"
            f"- **모든 회사**에 대해 `search_web(\"[회사명] official website\")`로 올바른 도메인 확인\n"
            f"- 한 턴에 8개씩 병렬로 검색 → 빠르게 도메인 확보\n"
            f"- 검색 결과 snippet에서 도메인을 추출 (URL에서 호스트 부분)\n"
            f"- ⚠️ **도메인 목록을 텍스트로 나열하지 마세요** — 바로 Step 2 도구 호출로 넘어가세요\n\n"
            f"**Step 2: Hunter Domain Search 배치 호출** (★ 핵심 — 3~6턴)\n"
            f"- **한 턴에 5~8개 회사의 hunter_domain_search를 동시에 호출!**\n"
            f"  예: 34개 회사 → 5턴(8+8+8+8+2)이면 전체 커버 가능\n"
            f"- **★ 반드시 company_name과 target_titles를 전달하세요!**\n"
            f"  예: hunter_domain_search(domain='axsome.com', company_name='Axsome Therapeutics', target_titles='VP BD, Director Research')\n"
            f"- 매칭된 연락처는 **자동으로 DB에 저장**됩니다 — add_contacts 호출 불필요!\n"
            f"- **대형 회사**: has_more=true이면 offset 또는 department/seniority 필터 사용\n"
            f"- **Hunter에서 matched=0인 회사 목록을 따로 기록** → Step 3에서 처리\n\n"
            f"**Step 3: Hunter 0 결과 회사 → Findymail + 웹 검색으로 보충** (★ 중요 — 3~5턴)\n"
            f"- Hunter에서 matched=0이었던 회사를 **반드시** 여기서 처리 — 건너뛰기 금지!\n"
            f"- 방법 A: `search_web(\"[회사명] [타겟직함] site:linkedin.com\")`로 담당자 이름 확인\n"
            f"  → 이름+도메인을 `findymail_search(name, domain)`으로 이메일 찾기\n"
            f"- 방법 B: 회사 웹사이트의 /team, /about, /leadership 페이지에서 이름 추출\n"
            f"  → `findymail_search(name, domain)`\n"
            f"- **Findymail은 이름+도메인만 있으면 이메일을 찾을 수 있습니다** (무료가 아니지만 정확도 높음)\n"
            f"- 한 턴에 5~8명씩 배치 호출\n\n"
            f"**Step 4: Findymail 검증** (필요시 1~2턴)\n"
            f"- Hunter에서 confidence가 낮은(<70) 이메일만 findymail_search로 검증\n\n"
            f"**저장 방법**\n"
            f"- **Hunter 결과 → 자동 저장** (add_contacts 호출 불필요)\n"
            f"- **Findymail/웹 결과 → add_contacts로 수동 저장** (이름, 회사, 직함 필수)\n"
            f"- 여러 번 호출해도 서버에서 자동으로 누적됨\n\n"
            f"### 도메인 확인 팁\n"
            f"- ⚠️ **절대 추측하지 마세요** — 30%의 바이오텍 회사는 회사명과 다른 도메인 사용\n"
            f"- 항상 `search_web(\"[회사명] official website\")`로 확인\n"
            f"- 작은 스텔스 회사는 도메인이 없을 수 있음 → `search_web`으로 LinkedIn 검색\n"
        )

    def _execute_tool(self, name: str, input_data: dict) -> str:
        if name == "findymail_search":
            try:
                fm = self._get_findymail()
                result = fm.find_email(
                    name=input_data["name"],
                    domain=input_data["domain"],
                )
                # Findymail returns {"contact": {"email": "...", ...}}
                contact = result.get("contact") or {}
                email = contact.get("email") or result.get("email") or ""
                if email:
                    self._credits_used["findymail"] += 1
                return json.dumps({
                    "email": email,
                    "verified": True if email else False,
                    "domain": input_data["domain"],
                    "name": input_data["name"],
                    "job_title": contact.get("job_title", ""),
                }, ensure_ascii=False)
            except Exception as e:
                return f"Error: Findymail search failed — {e}. Try Hunter.io or web search instead."

        elif name == "findymail_linkedin":
            try:
                fm = self._get_findymail()
                result = fm.find_email_by_linkedin(input_data["linkedin_url"])
                contact = result.get("contact") or {}
                email = contact.get("email") or result.get("email") or ""
                if email:
                    self._credits_used["findymail"] += 1
                return json.dumps({
                    "email": email,
                    "verified": True if email else False,
                    "linkedin_url": input_data["linkedin_url"],
                }, ensure_ascii=False)
            except Exception as e:
                return f"Error: Findymail LinkedIn search failed — {e}"

        elif name == "whois_lookup":
            try:
                wh = self._get_whois()
                result = wh.lookup_domain(input_data["domain"])
                contacts = wh.find_contact_emails(input_data["domain"])
                result["extracted_contacts"] = contacts
                return json.dumps(result, ensure_ascii=False, default=str)
            except Exception as e:
                return f"Error: WHOIS lookup failed — {e}"

        elif name == "hunter_domain_search":
            try:
                hunter = self._get_hunter()
                result = hunter.search_domain(
                    domain=input_data["domain"],
                    limit=input_data.get("limit", 100),
                    offset=input_data.get("offset", 0),
                    department=input_data.get("department", ""),
                    seniority=input_data.get("seniority", ""),
                )
                self._credits_used["hunter"] += 1
                emails = result.get("data", {}).get("emails", [])
                all_contacts = []
                for e in emails:
                    all_contacts.append({
                        "name": f"{e.get('first_name', '')} {e.get('last_name', '')}".strip(),
                        "email": e.get("value", ""),
                        "position": e.get("position", ""),
                        "confidence": e.get("confidence", 0),
                        "department": e.get("department", ""),
                        "seniority": e.get("seniority", ""),
                        "linkedin": e.get("linkedin", ""),
                    })
                total = result.get("meta", {}).get("results", len(all_contacts))
                offset_used = input_data.get("offset", 0)

                # Apply title filtering if target_titles provided
                target_titles = input_data.get("target_titles", "")
                if target_titles:
                    matched, unmatched = _filter_contacts_by_title(all_contacts, target_titles)
                else:
                    matched = all_contacts
                    unmatched = []

                # Sort by confidence (desc)
                matched.sort(key=lambda x: x.get("confidence", 0), reverse=True)

                # Auto-save ALL matched contacts to DB (with Findymail verification for low conf)
                company_name = input_data.get("company_name", input_data["domain"])
                high_count = sum(1 for c in matched if c.get("confidence", 0) >= 70)
                low_count = len(matched) - high_count
                auto_saved = self._auto_save_hunter_contacts(matched, company_name)

                # Return compact summary only (no full contact JSON)
                summary_lines = []
                for c in matched[:15]:  # show up to 15 names in summary
                    conf = c.get("confidence", 0)
                    summary_lines.append(
                        f"  - {c['name']} | {c.get('position','')} | "
                        f"conf:{conf} | {'✉' if c.get('email') else '❌'}"
                    )
                if len(matched) > 15:
                    summary_lines.append(f"  ... and {len(matched)-15} more")

                verify_note = ""
                if low_count > 0:
                    verify_note = f" ({high_count} high-conf direct, {low_count} low-conf → Findymail verified)"

                return (
                    f"Hunter: {input_data['domain']} → "
                    f"{total} total, {len(matched)} matched, {len(unmatched)} filtered out.\n"
                    f"✅ {auto_saved} contacts auto-saved to DB as '{company_name}'.{verify_note}\n"
                    f"{'has_more: use offset to get next page' if total > offset_used + len(all_contacts) else ''}\n"
                    f"Matched contacts:\n" + "\n".join(summary_lines)
                )
            except Exception as e:
                return f"Error: Hunter domain search failed — {e}. Try search_web instead."

        elif name == "hunter_find_email":
            try:
                hunter = self._get_hunter()
                result = hunter.find_email(
                    domain=input_data["domain"],
                    first_name=input_data["first_name"],
                    last_name=input_data["last_name"],
                )
                self._credits_used["hunter"] += 1
                data = result.get("data", {})
                return json.dumps({
                    "email": data.get("email", ""),
                    "score": data.get("score", 0),
                    "position": data.get("position", ""),
                    "domain": data.get("domain", ""),
                }, ensure_ascii=False)
            except Exception as e:
                return f"Error: Hunter find_email failed — {e}"

        elif name == "hunter_verify_email":
            try:
                hunter = self._get_hunter()
                result = hunter.verify_email(input_data["email"])
                self._credits_used["hunter"] += 1  # 0.5 rounded up
                data = result.get("data", {})
                return json.dumps({
                    "email": data.get("email", ""),
                    "status": data.get("status", "unknown"),
                    "score": data.get("score", 0),
                    "result": data.get("result", "unknown"),
                }, ensure_ascii=False)
            except Exception as e:
                return f"Error: Hunter verify failed — {e}"

        elif name == "search_web":
            rc = self._get_research()
            results = rc._web_search(
                input_data["query"],
                max_results=min(input_data.get("max_results", 5), 10),
            )
            formatted = [
                {"title": r.get("title", ""), "snippet": r.get("body", "")[:300], "url": r.get("href", "")}
                for r in results
            ]
            return json.dumps(formatted, ensure_ascii=False)

        elif name == "fetch_webpage":
            rc = self._get_research()
            text = rc._fetch_page_text(
                input_data["url"],
                max_chars=input_data.get("max_chars", 5000),
            )
            return text if text else "(Failed to fetch page or empty content)"

        elif name == "read_file":
            content = self._load_data_file(input_data["filename"])
            return content if content else f"(File not found: {input_data['filename']})"

        elif name == "add_contacts":
            return self._add_contacts(input_data)

        # Legacy support: if agent calls save_contacts, treat as add_contacts
        elif name == "save_contacts":
            if "result_json" in input_data:
                raw = input_data["result_json"]
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                return self._add_contacts(parsed)
            elif "contacts" in input_data:
                return self._add_contacts(input_data)
            else:
                return self._add_contacts(input_data)

        return f"Error: Unknown tool '{name}'"

    def _auto_save_hunter_contacts(self, matched: list[dict], company_name: str) -> int:
        """Auto-save Hunter matched contacts to DB.

        Contacts with confidence >= 70 are saved directly.
        Contacts with confidence < 70 are verified via Findymail first.
        Returns count saved.
        """
        if not matched:
            return 0

        domain = None  # extract from first email
        for c in matched:
            email = c.get("email", "")
            if email and "@" in email:
                domain = email.split("@")[1]
                break

        # Split by confidence
        high_conf = []
        needs_verify = []
        for c in matched:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            if c.get("confidence", 0) >= 70:
                high_conf.append(c)
            else:
                needs_verify.append(c)

        # Verify low-confidence contacts via Findymail (parallel)
        verified_map = {}  # name -> verified_email
        if needs_verify and domain:
            try:
                fm = self._get_findymail()
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _verify(contact):
                    try:
                        result = fm.find_email(
                            name=contact["name"],
                            domain=domain,
                        )
                        c_data = result.get("contact") or {}
                        return contact["name"], c_data.get("email") or result.get("email") or ""
                    except Exception:
                        return contact["name"], ""

                with ThreadPoolExecutor(max_workers=min(len(needs_verify), 5)) as pool:
                    futures = [pool.submit(_verify, c) for c in needs_verify]
                    for f in as_completed(futures):
                        name, email = f.result()
                        if email:
                            verified_map[name] = email
                            self._credits_used["findymail"] += 1
            except Exception as e:
                logger.warning(f"Findymail batch verify failed: {e}")

        # Build contacts for save
        contacts_for_save = []
        for c in high_conf:
            contacts_for_save.append({
                "contact_name": c["name"],
                "email": c.get("email", ""),
                "company": company_name,
                "title": c.get("position", ""),
                "linkedin_url": c.get("linkedin", ""),
                "email_confidence": "high",
                "source": "hunter",
            })
        for c in needs_verify:
            name = c["name"]
            fm_email = verified_map.get(name)
            if fm_email:
                # Findymail verified — use verified email
                contacts_for_save.append({
                    "contact_name": name,
                    "email": fm_email,
                    "company": company_name,
                    "title": c.get("position", ""),
                    "linkedin_url": c.get("linkedin", ""),
                    "email_confidence": "verified",
                    "source": "hunter+findymail",
                })
            else:
                # Findymail couldn't verify — save with unverified tag
                contacts_for_save.append({
                    "contact_name": name,
                    "email": c.get("email", ""),
                    "company": company_name,
                    "title": c.get("position", ""),
                    "linkedin_url": c.get("linkedin", ""),
                    "email_confidence": "unverified",
                    "source": "hunter",
                })

        if not contacts_for_save:
            return 0
        result = self._add_contacts({"contacts": contacts_for_save})
        import re
        m = re.search(r"\+(\d+) new contacts saved", result)
        return int(m.group(1)) if m else len(contacts_for_save)

    def _add_contacts(self, input_data: dict) -> str:
        """Add contacts incrementally. Called multiple times during agent run."""
        contacts = input_data.get("contacts", [])

        # If agent passed flat data without "contacts" key, treat entire input as a single contact
        if not contacts and input_data.get("contact_name"):
            contacts = [input_data]

        if not contacts:
            return "Error: No contacts provided. Pass {\"contacts\": [{\"contact_name\": \"...\", \"company\": \"...\", ...}]}"

        import db
        if self._search_id is None:
            self._search_id = db.create_prospect_search(
                name=f"Agent2_{time.strftime('%y%m%d_%H%M')}",
                search_params="{}",
                source="agent_email_finder",
            )

        # Filter out junk entries: "Unknown", metadata, placeholder names
        _JUNK_NAMES = {"unknown", "clinical team", "n/a", "none", "tbd", ""}
        saved = 0
        dupes = 0
        skipped = 0
        for c in contacts:
            name = (c.get("contact_name") or "").strip()
            # Reject empty, unknown, or metadata-like names
            if not name or name.lower() in _JUNK_NAMES:
                skipped += 1
                continue
            if name.startswith("[") or "verification" in name.lower() or "processing" in name.lower():
                skipped += 1
                continue
            try:
                pid = db.add_prospect(
                    search_id=self._search_id,
                    contact_name=c.get("contact_name", ""),
                    email=c.get("email", ""),
                    company=c.get("company", ""),
                    title=c.get("title", c.get("position", "")),
                    linkedin_url=c.get("linkedin_url", c.get("linkedin", "")),
                    location=c.get("location", ""),
                    fit_score=c.get("fit_score", 0),
                    fit_reason=c.get("fit_reason", ""),
                    email_confidence=c.get("email_confidence", "unknown"),
                    source=c.get("source", "agent"),
                    source_data=json.dumps(c, ensure_ascii=False),
                )
                # Always track in accumulated (for coverage counting), even if DB duplicate
                self._accumulated_contacts.append(c)
                if pid:
                    saved += 1
                else:
                    dupes += 1
            except Exception as e:
                logger.warning(f"Failed to save contact {c.get('contact_name', '?')}: {e}")

        # Update search record
        db.update_prospect_search(
            self._search_id,
            status="in_progress",
            total_found=len(self._accumulated_contacts),
        )

        # Build final result JSON for UI
        self._final_result = json.dumps({
            "contacts": self._accumulated_contacts,
            "search_summary": {
                "total_contacts_found": len(self._accumulated_contacts),
                "contacts_with_email": sum(1 for c in self._accumulated_contacts if c.get("email")),
            },
        }, ensure_ascii=False)

        # Coverage check
        companies_so_far = set(
            c.get("company", "").strip().lower()
            for c in self._accumulated_contacts if c.get("company")
        )
        coverage = f"{len(companies_so_far)}/{self._num_companies}" if self._num_companies else str(len(companies_so_far))

        with_email = sum(1 for c in self._accumulated_contacts if c.get("email"))
        extra = []
        if dupes:
            extra.append(f"{dupes} already in DB")
        if skipped:
            extra.append(f"{skipped} rejected")
        extra_msg = f" ({', '.join(extra)})" if extra else ""
        return (
            f"OK. +{saved} new contacts saved{extra_msg} "
            f"(total: {len(self._accumulated_contacts)}, with email: {with_email}). "
            f"Companies covered: {coverage}. "
            f"Keep going with remaining companies, then call add_contacts again."
        )


# ═══════════════════════════════════════════════════════════════
# Agent 3: Cold Mail Writer
# ═══════════════════════════════════════════════════════════════

class ColdMailAgent(BaseAgent):
    """Writes personalized cold emails with per-company web research.

    Tools: read_file, search_web, fetch_webpage, load_prospects,
           save_draft_email, finalize_campaign, upload_to_sheets,
           send_gmass_campaign

    Flow: read data → load prospects → research each company →
          write & save email → finalize campaign → (optional) send
    """

    MAX_TURNS = 80  # ~5 tool calls per company × 10 companies + overhead

    def __init__(
        self,
        product_number: int = 1,
        language: str = "ja",
        cta_type: str = "",
        extra_instructions: str = "",
        campaign_context: str = "",
        sender_profile_md: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._rc = None
        self._product_number = product_number
        self._language = language
        self._cta_type = cta_type
        self._extra_instructions = extra_instructions
        self._campaign_context = campaign_context
        self._sender_profile_md = sender_profile_md
        self._draft_emails: list[dict] = []
        self._campaign_id: int | None = None
        self._csv_content: str | None = None

    def _get_research(self):
        if self._rc is None:
            from research_client import ResearchClient
            self._rc = ResearchClient()
        return self._rc

    @property
    def draft_emails(self) -> list[dict]:
        """List of saved draft emails."""
        return list(self._draft_emails)

    @property
    def campaign_id(self) -> int | None:
        return self._campaign_id

    @property
    def csv_content(self) -> str | None:
        return self._csv_content

    def _get_tools(self) -> list[dict]:
        return [
            {
                "name": "read_file",
                "description": (
                    "Read a data file from the data/ directory. "
                    "Use to load: sender_profile.md, "
                    "feedback_log.md"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "File name in data/ directory",
                        }
                    },
                    "required": ["filename"],
                },
            },
            {
                "name": "search_web",
                "description": (
                    "Search the web for company news, hiring signals, "
                    "partnerships, or recent announcements. "
                    "Use this to research each company before writing their email."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (English or Japanese)",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max results (default 5, max 10)",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch_webpage",
                "description": (
                    "Fetch and extract text from a webpage URL. "
                    "Use to read company homepages, news articles, careers pages."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Max chars to return (default 5000)",
                        },
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "load_prospects",
                "description": (
                    "Load the prospect contact list. "
                    "Provide EITHER search_id (to load from DB) OR csv_text (raw CSV string). "
                    "Returns JSON array of prospects with contact_name, email, company, title, etc."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "search_id": {
                            "type": "integer",
                            "description": "Prospect search ID in database",
                        },
                        "csv_text": {
                            "type": "string",
                            "description": "Raw CSV text with column headers",
                        },
                    },
                },
            },
            {
                "name": "save_draft_email",
                "description": (
                    "Save a draft email for one prospect. Call this after researching "
                    "and writing each email. The body MUST use <br> for line breaks "
                    "(HTML format for GMass mail merge)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "contact_name": {"type": "string"},
                        "email": {"type": "string"},
                        "company": {"type": "string"},
                        "title": {
                            "type": "string",
                            "description": "Recipient's job title",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject line",
                        },
                        "body": {
                            "type": "string",
                            "description": "Email body (use <br> for line breaks)",
                        },
                        "language": {
                            "type": "string",
                            "description": "Language code: ja, en, or ko",
                        },
                        "framework": {
                            "type": "string",
                            "description": "Framework used (PAS/AIDA/BAB/Referral/Hiring-Signal)",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Brief explanation of approach chosen",
                        },
                    },
                    "required": ["contact_name", "email", "company", "subject", "body"],
                },
            },
            {
                "name": "finalize_campaign",
                "description": (
                    "Finalize all saved draft emails into a campaign. "
                    "Creates DB records and generates a CSV file. "
                    "Call this after ALL emails are written."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "campaign_name": {
                            "type": "string",
                            "description": "Campaign name (auto-generated if omitted)",
                        },
                    },
                },
            },
            {
                "name": "upload_to_sheets",
                "description": (
                    "Upload the finalized campaign CSV to Google Sheets for GMass. "
                    "Only call AFTER finalize_campaign has been called."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "campaign_name": {
                            "type": "string",
                            "description": "Worksheet tab name in Google Sheets",
                        },
                    },
                },
            },
            {
                "name": "send_gmass_campaign",
                "description": (
                    "Create GMass list + draft + send the campaign. "
                    "IMPORTANT: This ACTUALLY SENDS real emails. "
                    "Only call when the user explicitly requests sending."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def _get_system_prompt(self, user_request: str) -> str:
        # Load agent-specific skill
        agent_skill = self._load_skill("coldmail_agent")

        # Load full coldmail writing rules (language-specific)
        try:
            if self._language == "en":
                writing_rules = self._load_skill("coldmail_en")
            else:
                writing_rules = self._load_skill("coldmail")
        except FileNotFoundError:
            writing_rules = ""

        if self._sender_profile_md:
            sender = self._sender_profile_md
        else:
            sender = self._load_data_file("sender_profile.md")
        feedback = self._load_data_file("feedback_log.md")

        config_section = (
            f"\n\n---\n\n"
            f"## 이번 캠페인 설정\n"
            f"- 언어: {self._language}\n"
            f"- CTA: {self._cta_type or '직함 기반 자동 선택'}\n"
        )
        if self._extra_instructions:
            config_section += f"- 추가 지시사항: {self._extra_instructions}\n"

        campaign_section = ""
        if self._campaign_context:
            campaign_section = f"\n\n---\n\n{self._campaign_context}"

        return (
            f"{agent_skill}\n\n"
            f"---\n\n"
            f"## 상세 작성 규칙 (참조)\n\n{writing_rules}\n\n"
            f"---\n\n"
            f"## 발신자 프로필\n{sender}\n\n"
            f"## 피드백 로그 (반드시 반영)\n{feedback}"
            f"{config_section}"
            f"{campaign_section}"
        )

    def _execute_tool(self, name: str, input_data: dict) -> str:
        if name == "read_file":
            content = self._load_data_file(input_data["filename"])
            return content if content else f"(File not found: {input_data['filename']})"

        elif name == "search_web":
            rc = self._get_research()
            results = rc._web_search(
                input_data["query"],
                max_results=min(input_data.get("max_results", 5), 10),
            )
            formatted = [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("body", "")[:300],
                    "url": r.get("href", ""),
                }
                for r in results
            ]
            return json.dumps(formatted, ensure_ascii=False)

        elif name == "fetch_webpage":
            rc = self._get_research()
            text = rc._fetch_page_text(
                input_data["url"],
                max_chars=input_data.get("max_chars", 5000),
            )
            return text if text else "(Failed to fetch page or empty content)"

        elif name == "load_prospects":
            return self._load_prospects(input_data)

        elif name == "save_draft_email":
            return self._save_draft(input_data)

        elif name == "finalize_campaign":
            return self._finalize(input_data.get("campaign_name"))

        elif name == "upload_to_sheets":
            return self._upload_sheets(input_data.get("campaign_name"))

        elif name == "send_gmass_campaign":
            return self._send_gmass()

        return f"Error: Unknown tool '{name}'"

    # ── Tool Implementations ──────────────────────────────

    def _load_prospects(self, input_data: dict) -> str:
        """Load prospects from DB or CSV text."""
        if input_data.get("search_id"):
            import db
            prospects = db.get_prospects(search_id=input_data["search_id"])
            if not prospects:
                return "No prospects found for this search_id."
            result = []
            for p in prospects:
                result.append({
                    "contact_name": p["contact_name"],
                    "email": p.get("email", ""),
                    "company": p.get("company", ""),
                    "title": p.get("title", ""),
                    "linkedin_url": p.get("linkedin_url", ""),
                    "location": p.get("location", ""),
                    "fit_score": p.get("fit_score", 0),
                })
            return json.dumps(result, ensure_ascii=False)

        elif input_data.get("csv_text"):
            import csv as csv_mod
            import io
            reader = csv_mod.DictReader(io.StringIO(input_data["csv_text"]))
            rows = list(reader)
            return json.dumps(rows, ensure_ascii=False) if rows else "No data in CSV."

        return "Error: Provide either search_id or csv_text."

    def _save_draft(self, data: dict) -> str:
        """Save one draft email to the in-memory list."""
        email_draft = {
            "contact_name": data.get("contact_name", ""),
            "email": data.get("email", ""),
            "company": data.get("company", ""),
            "title": data.get("title", ""),
            "subject": data.get("subject", ""),
            "body": data.get("body", ""),
            "language": data.get("language", self._language),
            "product": self._product_number,
            "framework": data.get("framework", ""),
            "rationale": data.get("rationale", ""),
        }
        self._draft_emails.append(email_draft)
        idx = len(self._draft_emails)
        subj_preview = email_draft["subject"][:50]
        return (
            f"Draft #{idx} saved: {email_draft['contact_name']} "
            f"({email_draft['company']}) — {subj_preview}"
        )

    def _finalize(self, campaign_name: str | None = None) -> str:
        """Finalize drafts → campaign DB record + CSV file."""
        if not self._draft_emails:
            return "Error: No draft emails to finalize. Use save_draft_email first."

        import csv as csv_mod
        import io
        import db
        from config import OUTPUT_DIR

        if not campaign_name:
            campaign_name = f"Agent3_{time.strftime('%y%m%d_%H%M')}"

        # Generate CSV
        output = io.StringIO()
        fieldnames = [
            "contact_name", "email", "company", "title",
            "product", "language", "subject", "body",
        ]
        writer = csv_mod.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for draft in self._draft_emails:
            writer.writerow({k: draft.get(k, "") for k in fieldnames})

        self._csv_content = output.getvalue()

        # Save CSV to file
        csv_path = OUTPUT_DIR / f"coldmails_{time.strftime('%y%m%d')}.csv"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(self._csv_content, encoding="utf-8-sig")

        # Create campaign in DB
        self._campaign_id = db.create_campaign(
            name=campaign_name,
            csv_path=str(csv_path),
            product_number=self._product_number,
        )

        # Add recipients to DB
        for draft in self._draft_emails:
            db.add_recipient(
                campaign_id=self._campaign_id,
                email=draft["email"],
                name=draft["contact_name"],
                company=draft["company"],
                language=draft.get("language", self._language),
                subject=draft["subject"],
                body=draft["body"],
            )

        return (
            f"Campaign '{campaign_name}' finalized. "
            f"ID: {self._campaign_id}, "
            f"Emails: {len(self._draft_emails)}, "
            f"CSV saved: {csv_path}"
        )

    def _upload_sheets(self, campaign_name: str | None = None) -> str:
        """Upload finalized CSV to Google Sheets."""
        if not self._csv_content or not self._campaign_id:
            return "Error: No campaign to upload. Run finalize_campaign first."

        if not campaign_name:
            campaign_name = f"ColdMail_{time.strftime('%Y%m%d')}"

        try:
            import db
            from sheets_client import SheetsClient
            from config import OUTPUT_DIR

            csv_path = OUTPUT_DIR / f"coldmails_{time.strftime('%y%m%d')}.csv"
            sheets = SheetsClient()
            spreadsheet_id, worksheet_id = sheets.upload_mailmerge_csv(
                str(csv_path), campaign_name,
            )

            db.update_campaign(
                self._campaign_id,
                spreadsheet_id=spreadsheet_id,
                worksheet_id=worksheet_id,
            )

            return (
                f"Uploaded to Google Sheets. "
                f"Spreadsheet: {spreadsheet_id}, Worksheet: {worksheet_id}"
            )
        except Exception as e:
            return f"Error uploading to Sheets: {e}"

    def _send_gmass(self) -> str:
        """Send campaign via GMass."""
        if not self._campaign_id:
            return "Error: No campaign to send. Run finalize_campaign first."

        import db
        campaign = db.get_campaign(self._campaign_id)
        if not campaign:
            return "Error: Campaign not found in DB."

        spreadsheet_id = campaign.get("spreadsheet_id")
        worksheet_id = campaign.get("worksheet_id")

        if not spreadsheet_id or not worksheet_id:
            return "Error: Campaign not uploaded to Sheets. Run upload_to_sheets first."

        try:
            from gmass_client import GMassClient
            gmass = GMassClient()

            # Create list
            list_result = gmass.create_list(spreadsheet_id, worksheet_id)
            list_address = list_result.get("listAddress", "")
            db.update_campaign(self._campaign_id, gmass_list_id=list_address)

            # Create draft
            draft_result = gmass.create_draft(
                list_address=list_address,
                subject="{subject}",
                message="{body}",
            )
            draft_id = draft_result.get("campaignDraftId", "")
            db.update_campaign(self._campaign_id, gmass_draft_id=draft_id)

            # Send
            send_result = gmass.send_campaign(draft_id)
            gmass_campaign_id = send_result.get("campaignId", "")
            db.update_campaign(
                self._campaign_id,
                gmass_campaign_id=gmass_campaign_id,
                status="sent",
                sent_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )

            return (
                f"Campaign sent! GMass ID: {gmass_campaign_id}, "
                f"Recipients: {len(self._draft_emails)}"
            )
        except Exception as e:
            return f"Error sending via GMass: {e}"

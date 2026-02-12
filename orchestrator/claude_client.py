"""
Claude API client - wraps Anthropic API with SKILL.md system prompts.

Loads skill definitions from .claude/skills/ and uses them as system prompts
to replicate the same behavior as Claude Code's /coldmail, /review, /followup, /abtest.
"""
from pathlib import Path
from config import CLAUDE_API_KEY, CLAUDE_MODEL, SKILLS_DIR, DATA_DIR


class ClaudeClient:
    def __init__(self, api_key: str = CLAUDE_API_KEY, model: str = CLAUDE_MODEL):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self._skill_cache: dict[str, str] = {}

    def _load_skill(self, skill_name: str) -> str:
        """Load a SKILL.md file and return its content as system prompt.

        Searches in order: japan/, shared/, then root level for backwards compatibility.
        """
        if skill_name not in self._skill_cache:
            # Search paths in order of priority
            search_paths = [
                SKILLS_DIR / "japan" / skill_name / "SKILL.md",
                SKILLS_DIR / "shared" / skill_name / "SKILL.md",
                SKILLS_DIR / skill_name / "SKILL.md",  # backwards compatibility
            ]

            skill_path = None
            for path in search_paths:
                if path.exists():
                    skill_path = path
                    break

            if skill_path is None:
                raise FileNotFoundError(f"Skill not found: {skill_name} (searched in japan/, shared/)")

            self._skill_cache[skill_name] = skill_path.read_text(encoding="utf-8")
        return self._skill_cache[skill_name]

    def _load_data_file(self, filename: str) -> str:
        """Load a data file (sender_profile.md, feedback_log.md, etc.)."""
        path = DATA_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    # Instruction appended to all system prompts to prevent hallucinated tool use
    _NO_TOOLS_INSTRUCTION = (
        "\n\n---\n"
        "중요: 이 환경에서는 도구(tool)를 사용할 수 없습니다. "
        "<read_file>, <web_search>, <web_fetch> 등의 XML 태그를 출력하지 마세요. "
        "주어진 정보만으로 바로 결과를 작성하세요. "
        "리서치/검색 단계 없이 제공된 데이터를 기반으로 즉시 메일을 생성하세요."
    )

    def _call(self, system: str, user_message: str, max_tokens: int = 8192) -> str:
        """Make a Claude API call and return the text response.

        Uses streaming for large max_tokens to avoid Anthropic's 10-min timeout.
        """
        if max_tokens > 16384:
            # Stream for large outputs
            text_parts = []
            with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system + self._NO_TOOLS_INSTRUCTION,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                for text in stream.text_stream:
                    text_parts.append(text)
            return "".join(text_parts)
        else:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system + self._NO_TOOLS_INSTRUCTION,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text

    # ── Skill-based Operations ───────────────────────────

    def research(self, company: str, contact_name: str, contact_title: str = "") -> str:
        """Run /research skill for a single company/contact."""
        skill = self._load_skill("research")
        prompt = f"리서치 대상: {company}, {contact_name}, {contact_title}"
        return self._call(skill, prompt)

    def generate_coldmail(
        self,
        csv_content: str,
        product_number: int = 1,
        language: str = "ja",
        extra_instructions: str = "",
        sender_profile_md: str = "",
    ) -> str:
        """Run /coldmail skill to generate personalized cold emails."""
        skill = self._load_skill("coldmail")
        if sender_profile_md:
            sender_profile = sender_profile_md
        else:
            sender_profile = self._load_data_file("sender_profile.md")
        feedback_log = self._load_data_file("feedback_log.md")

        system_prompt = (
            f"{skill}\n\n---\n\n"
            f"## 발신자 프로필\n{sender_profile}\n\n"
            f"## 피드백 로그 (반드시 반영)\n{feedback_log}"
        )

        prompt = (
            f"{product_number}번 제품으로 콜드메일 작성해줘.\n"
            f"언어: {language}\n"
            f"{extra_instructions}\n\n"
            f"## CSV 데이터\n```\n{csv_content}\n```\n\n"
            f"## 추가 출력 요청\n"
            f"메일 작성 결과를 보여준 후, 마지막에 반드시 아래 형식의 CSV 블록도 포함해줘.\n"
            f"```csv\n"
            f"contact_name,email,company,title,product,language,subject,body\n"
            f"(각 행: 이름,이메일,회사,직책,제품번호,언어,제목,본문)\n"
            f"```\n"
            f"body 컬럼의 줄바꿈은 <br>로 변환해서 넣어줘. CSV 규격에 맞게 쉼표 포함 필드는 큰따옴표로 감싸줘."
        )
        return self._call(system_prompt, prompt, max_tokens=16384)

    def review(self, email_content: str, auto_fix: bool = False) -> str:
        """Run /review skill to fact-check and audit generated emails."""
        skill = self._load_skill("review")

        system_prompt = skill

        fix_instruction = "수정까지 해줘." if auto_fix else ""
        prompt = f"아래 메일을 검수해줘. {fix_instruction}\n\n{email_content}"
        return self._call(system_prompt, prompt, max_tokens=16384)

    def generate_followup(
        self,
        original_email: str,
        company: str,
        contact_name: str,
        stage: int,
        language: str = "ja",
        response_status: str = "no_response",
    ) -> str:
        """Run /followup skill to generate follow-up emails."""
        skill = self._load_skill("followup")

        prompt = (
            f"회사: {company}\n"
            f"담당자: {contact_name}\n"
            f"후속 단계: {stage}차\n"
            f"언어: {language}\n"
            f"응답 상태: {response_status}\n\n"
            f"## 이전 메일 내용\n{original_email}"
        )
        return self._call(skill, prompt)

    def generate_abtest(
        self,
        original_email: str,
        test_element: str = "subject",
        variants: int = 2,
    ) -> str:
        """Run /abtest skill to generate A/B test variants."""
        skill = self._load_skill("abtest")

        prompt = (
            f"테스트 요소: {test_element}\n"
            f"변형 수: {variants}\n\n"
            f"## 원본 메일\n{original_email}"
        )
        return self._call(skill, prompt)

    def analyze_reply(self, reply_text: str, original_email: str, company: str) -> str:
        """Analyze a reply to determine sentiment and suggest next action."""
        system = (
            "당신은 B2B 세일즈 전문가입니다. "
            "답장을 분석하여 감정(긍정/중립/부정), 의도(미팅 수락/질문/거절/기타), "
            "그리고 추천 다음 액션을 JSON으로 반환하세요.\n"
            "형식: {\"sentiment\": \"...\", \"intent\": \"...\", \"action\": \"...\", \"summary\": \"...\"}"
        )
        prompt = (
            f"회사: {company}\n\n"
            f"## 원본 메일\n{original_email}\n\n"
            f"## 답장\n{reply_text}"
        )
        return self._call(system, prompt)

    def enrich_prospects(
        self,
        prospects_json: str,
        search_criteria: dict,
        existing_emails_for_pattern: list[dict] | None = None,
        research_context: list[dict] | None = None,
    ) -> str:
        """Run /prospect skill to analyze, score, and infer emails for prospects."""
        import json

        skill = self._load_skill("prospect")

        system_prompt = skill

        criteria_str = "\n".join(f"- {k}: {v}" for k, v in search_criteria.items() if v)

        pattern_section = ""
        if existing_emails_for_pattern:
            pattern_section = (
                f"\n\n## 이메일 패턴 추론 참고 데이터\n"
                f"같은 회사의 기존 이메일 목록 (패턴 분석용):\n"
                f"```json\n{json.dumps(existing_emails_for_pattern, ensure_ascii=False)}\n```"
            )

        research_section = ""
        if research_context:
            research_section = (
                f"\n\n## 업계 리서치 데이터 (ClinicalTrials.gov + PubMed)\n"
                f"각 회사의 최신 임상시험 및 논문 활동 데이터입니다. "
                f"적합도 평가 시 참고하세요:\n"
                f"```json\n{json.dumps(research_context, ensure_ascii=False)}\n```"
            )

        prompt = (
            f"아래 Apollo.io 검색 결과를 분석하고 인리치먼트해줘.\n\n"
            f"## 검색 기준\n{criteria_str}\n\n"
            f"## Apollo 검색 결과\n```json\n{prospects_json}\n```"
            f"{pattern_section}"
            f"{research_section}\n\n"
            f"## 출력 요청\n"
            f"결과를 CSV 형식으로 출력해줘:\n"
            f"```csv\n"
            f"contact_name,email,email_confidence,company,title,linkedin_url,"
            f"fit_score,fit_reason,location,source\n"
            f"```\n"
            f"fit_score 내림차순 정렬. email이 없고 추론도 어려운 경우 빈칸으로."
        )
        return self._call(system_prompt, prompt, max_tokens=16384)

    def generate_search_queries(self, product_description: str, region: str = "") -> list[str]:
        """Generate English search queries for finding target companies via web search."""
        import json
        system = (
            "You generate web search queries to find companies that would be good "
            "customers for a given product/service. Output a JSON array of 20-25 search "
            "queries in English. Cast a WIDE net — we want ~300 web results total.\n\n"
            "Query categories (cover ALL of these):\n"
            "- Direct product category + industry (3-4 queries)\n"
            "- Specific use cases + company types (3-4 queries)\n"
            "- Competitor/complementary products (2-3 queries)\n"
            "- Industry pain points the product solves (2-3 queries)\n"
            "- 'top companies' / 'leading companies' / 'startups' lists (3-4 queries)\n"
            "- Adjacent/emerging markets that could use this product (2-3 queries)\n"
            "- Regional market players (if region specified, 2-3 queries)\n\n"
            "Rules:\n"
            "- Queries must be in English for best web search results\n"
            "- Include terms like 'companies', 'market', 'list', 'top', 'startups' to find business entities\n"
            "- Mix broad queries ('top EEG companies') with specific ones ('EEG biomarker clinical trial companies')\n"
            "- Include queries targeting company LISTS, directories, market reports\n"
            "- Be specific enough to find relevant companies, not generic articles\n"
            "- Output ONLY a JSON array of strings, nothing else"
        )
        region_note = f"\nTarget region: {region}" if region else ""
        prompt = f"Product/service description:\n{product_description}{region_note}"
        raw = self._call(system, prompt, max_tokens=2048)
        try:
            queries = json.loads(raw)
            if isinstance(queries, list):
                return [q for q in queries if isinstance(q, str)][:25]
        except json.JSONDecodeError:
            pass
        # Fallback: extract lines that look like queries
        return [line.strip().strip('"').strip("'") for line in raw.split("\n")
                if line.strip() and len(line.strip()) > 10][:5]

    def find_targets(
        self,
        product_description: str,
        region: str = "",
        previous_result: str = "",
        feedback: str = "",
        web_context: str = "",
        exclude_companies: list[str] | None = None,
    ) -> str:
        """Run /target_finder skill to recommend companies and job titles.

        If web_context is provided, Claude uses it as the primary source (RAG mode).
        If previous_result and feedback are provided, refines the recommendation.
        If exclude_companies is provided, those companies are excluded from results.
        """
        skill = self._load_skill("target_finder")
        target_feedback = self._load_data_file("target_feedback_log.md")

        feedback_section = ""
        if target_feedback.strip():
            feedback_section = f"\n\n## 과거 피드백 이력 (항상 반영)\n{target_feedback}"

        web_section = ""
        if web_context:
            web_section = (
                f"\n\n## 웹 리서치 결과 (유일한 데이터 소스)\n"
                f"아래는 실제 웹 검색으로 수집한 데이터입니다. "
                f"**이 데이터에 등장하는 회사만 추천하세요. "
                f"내장 지식만으로 회사를 추가하지 마세요.** "
                f"내장 지식은 웹 데이터의 해석/분류에만 사용하세요.\n\n"
                f"{web_context}"
            )

        exclude_section = ""
        if exclude_companies:
            exclude_section = (
                f"\n\n## 제외 대상 회사 (절대 추천하지 말 것)\n"
                f"아래 회사는 이미 다른 프리셋에 포함되어 있으므로 **결과에서 완전히 제외하세요.**\n"
                f"{', '.join(exclude_companies)}\n"
            )

        system_prompt = (
            f"{skill}\n\n---\n\n"
            f"{web_section}{exclude_section}{feedback_section}"
        )

        region_line = f"\n지역 제한: {region}" if region else ""

        if previous_result and feedback:
            prompt = (
                f"이전에 아래 제품에 대해 타겟을 추천했습니다. "
                f"사용자 피드백을 반영하여 수정된 결과를 JSON으로 출력해줘.\n\n"
                f"## 제품 설명\n{product_description}"
                f"{region_line}\n\n"
                f"## 이전 추천 결과\n```json\n{previous_result}\n```\n\n"
                f"## 사용자 피드백\n{feedback}\n\n"
                f"피드백을 정확히 반영하여 수정된 전체 JSON을 출력해줘."
            )
        else:
            feedback_reminder = ""
            if target_feedback.strip():
                feedback_reminder = (
                    f"\n\n## 반드시 반영할 피드백\n"
                    f"아래 피드백은 과거에 사용자가 준 지시사항입니다. **반드시 반영하세요.**\n"
                    f"{target_feedback}\n"
                )
            exclude_reminder = ""
            if exclude_companies:
                exclude_reminder = (
                    f"\n\n**제외 대상:** 다음 회사는 결과에 포함하지 마세요: "
                    f"{', '.join(exclude_companies[:30])}"
                    f"{'...' if len(exclude_companies) > 30 else ''}\n"
                )
            prompt = (
                f"아래 제품/서비스에 대해 타겟 회사와 직종을 추천해줘.\n\n"
                f"## 제품 설명\n{product_description}"
                f"{region_line}\n\n"
                f"**중요: 웹 리서치 데이터에서 발견된 모든 관련 회사를 빠짐없이 추출하라.**\n"
                f"내장 지식으로 회사를 추가하지 말 것. 웹 데이터에 있는 회사만 추천.\n"
                f"15~20개에서 멈추지 말 것. 웹 데이터를 철저히 훑어 가능한 모든 회사를 포함.\n\n"
                f"JSON 형식으로 출력해줘."
                f"{exclude_reminder}"
                f"{feedback_reminder}"
            )
        return self._call(system_prompt, prompt, max_tokens=32768)

    def cross_check_evidence(self, companies_with_verification: list[dict], feedback: str = "") -> str:
        """Cross-check AI-claimed evidence against external verification data.

        Takes the combined list of {name, reason, evidence, verification: {...}}
        and asks Claude to compare AI claims with factual data, producing a
        per-company verdict explaining what's confirmed, unconfirmed, or wrong.

        Returns: JSON string with per-company verdicts.
        """
        import json

        system = (
            "당신은 팩트체커입니다. AI가 추천한 회사 목록에 대해, "
            "AI가 주장한 근거(evidence)와 외부에서 수집한 실제 데이터를 비교 분석합니다.\n\n"
            "## 입력 데이터 구조\n"
            "각 회사에는 다음이 포함됩니다:\n"
            "- `evidence`: AI가 주장한 해당 회사의 사업/연구 근거\n"
            "- `verification.web_results`: DuckDuckGo 웹 검색 결과 [{title, snippet, url}]\n"
            "- `verification.trial_details`: ClinicalTrials.gov 임상시험 [{nct_id, title, status, conditions}]\n"
            "- `verification.pub_topics`: PubMed 논문 제목 목록\n"
            "- `verification.trial_conditions`: 임상시험 적응증 목록\n\n"
            "## 출력 형식\n"
            "JSON 배열을 출력하세요. 각 요소:\n"
            "```json\n"
            '{"company": "회사명", "verdict": "confirmed|partial|unverified|wrong", '
            '"explanation": "한국어로 2~3문장. AI 근거의 어떤 부분이 확인되었고, 어떤 부분이 확인되지 않았는지 구체적으로 설명"}\n'
            "```\n\n"
            "## verdict 기준\n"
            "- **confirmed**: AI 근거의 핵심 주장이 외부 데이터로 직접 뒷받침됨\n"
            "- **partial**: 회사의 사업 분야는 맞지만, AI가 주장한 구체적 내용(제품명, 적응증, 기술 등)은 확인 불가\n"
            "- **unverified**: 외부 데이터가 부족하여 AI 근거를 검증할 수 없음\n"
            "- **wrong**: 외부 데이터가 AI 근거와 모순됨 (회사의 실제 사업과 다름)\n\n"
            "## 규칙\n"
            "- explanation에는 반드시 외부 데이터의 구체적 내용을 인용하여 근거를 제시\n"
            "- 웹 검색 결과의 snippet, 임상시험 제목, 논문 제목 등을 직접 언급\n"
            "- 추측하지 말 것. 외부 데이터에 없는 내용은 '확인 불가'로 처리\n"
            "- JSON 배열만 출력 (마크다운 코드블록 불필요)"
        )

        if feedback:
            system += (
                "\n\n## 사용자 피드백 (검증 시 반드시 참고)\n"
                "아래 피드백은 사용자가 지정한 분류/필터링 기준입니다. "
                "verdict 판정 시 이 기준에 부합하지 않는 회사는 explanation에 명시하세요.\n\n"
                f"{feedback}"
            )

        # Build compact verification payload
        payload = []
        for c in companies_with_verification:
            v = c.get("verification", {})
            payload.append({
                "company": c.get("name", ""),
                "evidence": c.get("evidence", c.get("reason", "")),
                "web_results": v.get("web_results", [])[:3],
                "trial_details": v.get("trial_details", [])[:3],
                "trial_conditions": v.get("trial_conditions", [])[:8],
                "pub_topics": v.get("pub_topics", [])[:3],
            })

        prompt = (
            f"아래 {len(payload)}개 회사에 대해 AI 근거와 외부 검증 데이터를 비교 분석해줘.\n\n"
            f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
        )
        return self._call(system, prompt, max_tokens=32768)

    def edit_skill(self, skill_content: str, feedback: str) -> str:
        """Apply user feedback to modify a skill file and return the updated content."""
        system = (
            "당신은 스킬 파일 편집 전문가입니다.\n\n"
            "사용자가 스킬 파일(SKILL.md 또는 공통 규칙 파일)에 대한 피드백을 제공하면, "
            "해당 피드백을 반영하여 수정된 전체 파일 내용을 반환합니다.\n\n"
            "규칙:\n"
            "1. 사용자가 요청한 변경 사항만 적용하세요.\n"
            "2. 파일의 전체 구조와 형식을 유지하세요.\n"
            "3. 기존 내용 중 변경되지 않은 부분은 그대로 유지하세요.\n"
            "4. 마크다운 형식을 유지하세요.\n"
            "5. 수정된 전체 파일 내용만 출력하세요. 설명이나 코드 블록 없이 파일 내용만 그대로 출력하세요.\n"
            "6. ```markdown 같은 코드 블록으로 감싸지 마세요."
        )
        prompt = (
            f"## 현재 스킬 파일 내용\n\n{skill_content}\n\n"
            f"---\n\n"
            f"## 사용자 피드백\n\n{feedback}\n\n"
            f"---\n\n"
            f"위 피드백을 반영하여 수정된 전체 파일 내용을 출력하세요."
        )
        return self._call(system, prompt, max_tokens=16384)

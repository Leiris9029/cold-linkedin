"""
Claude API client - wraps Anthropic API with SKILL.md system prompts.

Loads skill definitions from .claude/skills/ and uses them as system prompts
to replicate the same behavior as Claude Code's /coldmail, /review, /followup, /abtest.
"""
import anthropic
from pathlib import Path
from config import CLAUDE_API_KEY, CLAUDE_MODEL, SKILLS_DIR, DATA_DIR


class ClaudeClient:
    def __init__(self, api_key: str = CLAUDE_API_KEY, model: str = CLAUDE_MODEL):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self._skill_cache: dict[str, str] = {}

    def _load_skill(self, skill_name: str) -> str:
        """Load a SKILL.md file and return its content as system prompt."""
        if skill_name not in self._skill_cache:
            skill_path = SKILLS_DIR / skill_name / "SKILL.md"
            if not skill_path.exists():
                raise FileNotFoundError(f"Skill not found: {skill_path}")
            self._skill_cache[skill_name] = skill_path.read_text(encoding="utf-8")
        return self._skill_cache[skill_name]

    def _load_data_file(self, filename: str) -> str:
        """Load a data file (our_products.md, company_data.md, etc.)."""
        path = DATA_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def _call(self, system: str, user_message: str, max_tokens: int = 8192) -> str:
        """Make a Claude API call and return the text response."""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
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
    ) -> str:
        """Run /coldmail skill to generate personalized cold emails."""
        skill = self._load_skill("coldmail")
        products = self._load_data_file("our_products.md")
        company_data = self._load_data_file("company_data.md")

        system_prompt = f"{skill}\n\n---\n\n## 제품 정보\n{products}\n\n## 회사 사전 조사 데이터\n{company_data}"

        prompt = (
            f"{product_number}번 제품으로 콜드메일 작성해줘.\n"
            f"언어: {language}\n"
            f"{extra_instructions}\n\n"
            f"## CSV 데이터\n```\n{csv_content}\n```"
        )
        return self._call(system_prompt, prompt, max_tokens=16384)

    def review(self, email_content: str, auto_fix: bool = False) -> str:
        """Run /review skill to fact-check and audit generated emails."""
        skill = self._load_skill("review")
        products = self._load_data_file("our_products.md")
        company_data = self._load_data_file("company_data.md")

        system_prompt = f"{skill}\n\n---\n\n## 제품 정보\n{products}\n\n## 회사 사전 조사 데이터\n{company_data}"

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

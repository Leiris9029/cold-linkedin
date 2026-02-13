"""
Streamlit UI for Cold Email Campaign Management.

Provides a web interface for:
- CSV upload & contact preview
- Product/language/CTA selection
- Email generation, review, and preview
- Campaign send & status dashboard

Run with: streamlit run orchestrator/app_ui.py
"""
import sys
import os
import re
import csv
import io
import logging
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# Add orchestrator to path so imports work when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json

from config import OUTPUT_DIR, DATA_DIR, PROJECT_ROOT, HUNTER_API_KEY, FINDYMAIL_API_KEY
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_TARGET_FEEDBACK_PATH = DATA_DIR / "target_feedback_log.md"


def _append_target_feedback(feedback: str, product_summary: str = ""):
    """Append target-finding feedback to persistent log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n- [{timestamp}] "
    if product_summary:
        entry += f"({product_summary}) "
    entry += feedback.strip()
    with open(_TARGET_FEEDBACK_PATH, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def _rewrite_feedback_log(entries: list[str]):
    """Rewrite the feedback log with the given entries (for delete/clear)."""
    header = "# 타겟 발굴 피드백 로그\n\n이 파일에 누적된 피드백은 AI 타겟 추천 시 항상 반영됩니다.\n"
    body = "\n".join(entries) + "\n" if entries else ""
    with open(_TARGET_FEEDBACK_PATH, "w", encoding="utf-8") as f:
        f.write(header + body)


def _get_feedback_hash() -> str:
    """Return a short hash of the current feedback log content."""
    import hashlib
    try:
        content = _TARGET_FEEDBACK_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        content = ""
    return hashlib.md5(content.encode()).hexdigest()[:12]


class AgentProgressTracker:
    """Tracks agent progress via tool call callbacks and renders st.progress()."""

    # Tool-name → stage label mappings per agent type
    STAGE_MAP = {
        "agent1": {
            "search_queries": ("📋 검색 쿼리 생성", 0.10),
            "search_web": ("🔍 웹 검색", None),
            "search_for_targets": ("📊 결과 수집", 0.70),
            "save_results": ("💾 결과 저장", 0.90),
        },
        "agent2": {
            "read_file": ("📖 데이터 로딩", 0.03),
            "search_web": ("🔍 웹 검색", None),
            "fetch_webpage": ("🌐 페이지 분석", None),
            "findymail_search": ("📧 Findymail 검색", None),
            "findymail_linkedin": ("🔗 LinkedIn 이메일", None),
            "hunter_domain_search": ("🏢 Hunter 회사 조회", None),
            "hunter_find_email": ("🏹 Hunter 개별 검색", None),
            "hunter_verify_email": ("✅ 이메일 검증", None),
            "whois_lookup": ("🌐 WHOIS 조회", None),
            "add_contacts": ("💾 연락처 저장", None),
            "save_contacts": ("💾 결과 저장", 0.95),
        },
        "agent3": {
            "read_file": ("📖 데이터 로딩", 0.03),
            "load_prospects": ("📋 연락처 로딩", 0.08),
            "search_web": ("🔍 회사 리서치", None),
            "fetch_webpage": ("🌐 페이지 분석", None),
            "save_draft_email": ("✉️ 이메일 작성", None),
            "finalize_campaign": ("📦 캠페인 정리", 0.92),
            "upload_to_sheets": ("📊 시트 업로드", 0.96),
            "send_gmass_campaign": ("🚀 발송", 0.99),
        },
    }

    def __init__(self, agent_type: str, total_items: int = 0):
        self.agent_type = agent_type
        self.total_items = total_items  # companies or contacts
        self.tool_calls = 0
        self.item_count = 0  # tracks save_draft_email / per-company tools
        self.start_time = time.time()
        self.stage_map = self.STAGE_MAP.get(agent_type, {})
        self._progress_bar = st.progress(0)
        self._status = st.empty()
        self._log_area = st.empty()
        self._tool_log: list[str] = []
        self._current_progress = 0.0

        # File-based logging
        from pathlib import Path
        log_dir = Path(__file__).resolve().parent.parent / "output"
        log_dir.mkdir(exist_ok=True)
        self._log_file = log_dir / f"{agent_type}_{time.strftime('%y%m%d_%H%M%S')}.log"
        self._log_fh = open(self._log_file, "w", encoding="utf-8")
        self._log_fh.write(f"=== {agent_type} started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        self._log_fh.flush()

    def on_tool_call(self, name: str, input_data: dict):
        self.tool_calls += 1
        stage_info = self.stage_map.get(name)
        label = stage_info[0] if stage_info else f"🔧 {name}"
        fixed_pct = stage_info[1] if stage_info else None

        # Track per-item progress for agents 2 & 3
        if name == "save_draft_email":
            self.item_count += 1
        if name in ("findymail_search", "hunter_find_email", "findymail_linkedin"):
            self.item_count += 1
        if name == "hunter_domain_search":
            self.item_count += 1  # 1 call = 1 company covered

        # Calculate progress
        if fixed_pct is not None:
            pct = fixed_pct
        elif self.total_items > 0:
            # Estimate based on items processed
            if self.agent_type == "agent2":
                pct = min(0.05 + (self.item_count / self.total_items) * 0.88, 0.93)
            elif self.agent_type == "agent3":
                pct = min(0.10 + (self.item_count / self.total_items) * 0.80, 0.90)
            else:
                pct = min(0.05 + self.tool_calls * 0.04, 0.88)
        else:
            pct = min(0.05 + self.tool_calls * 0.04, 0.88)

        self._current_progress = max(self._current_progress, pct)
        self._progress_bar.progress(self._current_progress)

        # Status text
        detail = (
            input_data.get("query")
            or input_data.get("company")
            or input_data.get("name")
            or input_data.get("domain")
            or input_data.get("contact_name")
            or input_data.get("url", "")[:60]
            or input_data.get("filename")
            or str(input_data)[:50]
        )
        elapsed = int(time.time() - self.start_time)
        items_text = ""
        if self.total_items > 0 and self.item_count > 0:
            items_text = f" ({self.item_count}/{self.total_items})"
        self._status.info(f"⏱ {elapsed}초 | {label}{items_text} — {detail}")

        # Tool log
        log_line = f"[{elapsed:>3}s] {label}: {detail}"
        self._tool_log.append(log_line)
        self._write_log(log_line)
        self._log_area.code("\n".join(self._tool_log[-12:]), language=None)

    def on_tool_result(self, name: str, result_preview: str):
        log_line = f"       ✓ {name} → {result_preview[:150]}"
        self._tool_log.append(log_line)
        self._write_log(log_line)
        self._log_area.code("\n".join(self._tool_log[-12:]), language=None)

    def on_text(self, text: str):
        if text.strip():
            log_line = f"  💬 {text[:200]}"
            self._tool_log.append(log_line)
            self._write_log(log_line)
            self._log_area.code("\n".join(self._tool_log[-12:]), language=None)

    def _write_log(self, line: str):
        """Write to log file."""
        try:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()
        except Exception:
            pass

    def complete(self, message: str):
        elapsed = int(time.time() - self.start_time)
        self._progress_bar.progress(1.0)
        self._status.success(f"✅ {message} (⏱ {elapsed}초, 도구 {self.tool_calls}회)")
        self._write_log(f"=== COMPLETED: {message} ({elapsed}s, {self.tool_calls} tool calls) ===")
        try:
            self._log_fh.close()
        except Exception:
            pass

    def fail(self, error: str):
        elapsed = int(time.time() - self.start_time)
        self._progress_bar.progress(self._current_progress)
        self._status.error(f"❌ {error} (⏱ {elapsed}초)")
        self._write_log(f"=== FAILED: {error} ({elapsed}s) ===")
        try:
            self._log_fh.close()
        except Exception:
            pass

    @property
    def log_file_path(self) -> str:
        return str(self._log_file)

    @property
    def tool_log(self) -> list[str]:
        return list(self._tool_log)


def _render_company_card(company: dict, verification: dict | None, verdict: dict | None = None):
    """Render a company card with optional verification data and cross-check verdict."""
    name = company["name"]
    reason = company.get("reason", "")

    # Status icon based on verdict (if available) or verification
    if verdict:
        v_status = verdict.get("verdict", "unverified")
        icon = {"confirmed": "+", "partial": "~", "unverified": "?", "wrong": "X"}.get(v_status, "?")
        label = {"confirmed": "확인됨", "partial": "일부 확인", "unverified": "미검증", "wrong": "불일치"}.get(v_status, "?")
        header = f"[{icon}] **{name}** — {reason}  `{label}`"
    elif verification:
        status = verification.get("status", "no_data")
        icon = {"verified": "+", "partial": "~", "no_data": "-"}.get(status, "?")
        label = {"verified": "검증됨", "partial": "일부 확인", "no_data": "데이터 없음"}.get(status, "?")
        header = f"[{icon}] **{name}** — {reason}  `{label}`"
    else:
        header = f"**{name}** — {reason}"

    with st.expander(header, expanded=False):
        # AI's claimed evidence
        st.markdown(f"**AI 근거:** {company.get('evidence', reason)}")

        # Tier classification reason
        _tier_reason = company.get("tier_reason", "")
        if _tier_reason:
            st.markdown(f"**Tier 산정:** {_tier_reason}")

        # Cross-check verdict (the key new feature)
        if verdict and verdict.get("explanation"):
            v_status = verdict.get("verdict", "unverified")
            color = {"confirmed": "green", "partial": "orange", "unverified": "gray", "wrong": "red"}.get(v_status, "gray")
            emoji = {"confirmed": "✅", "partial": "⚠️", "unverified": "❓", "wrong": "❌"}.get(v_status, "❓")
            st.markdown(f"{emoji} **교차검증:** {verdict['explanation']}")

        # Verification data
        if verification:
            st.divider()

            with st.expander("외부 검증 데이터 상세", expanded=False):
                # Web search results
                web_results = verification.get("web_results", [])
                if web_results:
                    st.markdown("**웹 검색:**")
                    for wr in web_results[:3]:
                        st.markdown(f"- [{wr['title'][:60]}]({wr['url']})  \n"
                                    f"  {wr['snippet'][:150]}")

                # ClinicalTrials + PubMed — only shown for pharma/biotech
                if verification.get("is_pharma"):
                    trials_n = verification.get("trials_found", 0)
                    pubs_n = verification.get("publications_found", 0)

                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.metric("ClinicalTrials.gov", f"{trials_n}건")
                        conditions = verification.get("trial_conditions", [])
                        if conditions:
                            st.caption(f"적응증: {', '.join(conditions[:6])}")
                    with col_b:
                        st.metric("PubMed 논문", f"{pubs_n}건")
                        topics = verification.get("pub_topics", [])
                        if topics:
                            st.caption(topics[0][:80])

                    trial_details = verification.get("trial_details", [])
                    if trial_details:
                        for td in trial_details:
                            st.markdown(
                                f"- **{td['nct_id']}** ({td['status']}) — "
                                f"{td['title']} | {', '.join(td['conditions'][:3])}"
                            )

                if not web_results and not verification.get("is_pharma"):
                    st.warning("외부 소스에서 관련 데이터를 찾지 못했습니다.")


def _render_researcher_card(researcher: dict, verdict: dict | None = None):
    """Render a researcher card with institution and research details."""
    name = researcher.get("name", "")
    institution = researcher.get("institution", "")
    dept = researcher.get("department", "")
    title = researcher.get("title", "")
    reason = researcher.get("reason", "")

    # Status icon based on verdict
    if verdict:
        v_status = verdict.get("verdict", "unverified")
        icon = {"confirmed": "+", "partial": "~", "unverified": "?", "wrong": "X"}.get(v_status, "?")
        label = {"confirmed": "확인됨", "partial": "일부 확인", "unverified": "미검증", "wrong": "불일치"}.get(v_status, "?")
    else:
        icon = None

    header_parts = [f"**{name}**"]
    if title:
        header_parts.append(title)
    if institution:
        sub = institution
        if dept:
            sub += f" ({dept})"
        header_parts.append(sub)
    header = " — ".join(header_parts)
    if icon:
        header += f"  [{icon} {label}]"
    if reason:
        header += f"  \n{reason}"

    with st.expander(header, expanded=False):
        # Cross-check verdict
        if verdict and verdict.get("explanation"):
            v_status = verdict.get("verdict", "unverified")
            emoji = {"confirmed": "✅", "partial": "⚠️", "unverified": "❓", "wrong": "❌"}.get(v_status, "❓")
            st.markdown(f"{emoji} **교차검증:** {verdict['explanation']}")

        # Verification data summary
        verification = researcher.get("verification", {})
        if verification:
            vparts = []
            pubs_found = verification.get("publications_found", 0)
            if pubs_found:
                vparts.append(f"PubMed {pubs_found}건")
            trials_found = verification.get("trials_found", 0)
            if trials_found:
                vparts.append(f"임상시험 PI {trials_found}건")
            web_n = len(verification.get("web_results", []))
            if web_n:
                vparts.append(f"웹 결과 {web_n}건")
            if vparts:
                st.caption(f"외부 데이터: {' | '.join(vparts)}")

        research_area = researcher.get("research_area", "")
        if research_area:
            st.markdown(f"**연구 분야:** {research_area}")

        pubs = researcher.get("key_publications", "")
        if pubs:
            st.markdown(f"**주요 연구:** {pubs}")

        evidence = researcher.get("evidence", "")
        if evidence:
            st.markdown(f"**추천 근거:** {evidence}")

        tier_reason = researcher.get("tier_reason", "")
        if tier_reason:
            st.markdown(f"**Tier 산정:** {tier_reason}")

        clues = researcher.get("contact_clues", "")
        if clues:
            st.markdown(f"**연락처 단서:** {clues}")


def _auto_verify(result_text: str, feedback: str = ""):
    """Parse AI result → external verification → Claude cross-check."""
    import re as _re
    json_match = _re.search(r"```json\s*\n(.*?)```", result_text, _re.DOTALL)
    parsed = None
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
    if not parsed:
        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError:
            pass

    if parsed:
        all_companies = (parsed.get("tier1_companies", [])
                         + parsed.get("tier2_companies", []))
        if all_companies:
            n = len(all_companies)
            verify_bar = st.progress(0)
            verify_status = st.empty()

            # Step 2/3: External data collection
            verify_status.info(f"⏱ 2/3 — 외부 데이터 수집 중 ({n}개 회사: 웹 + ClinicalTrials + PubMed)...")
            verify_bar.progress(0.1)
            from research_client import ResearchClient
            rc = ResearchClient()
            verified_companies = rc.verify_companies_batch(all_companies)
            st.session_state.ai_target_verification = verified_companies
            verify_bar.progress(0.6)

            # Step 3/3: Claude cross-check (AI evidence vs external data)
            verify_status.info("⏱ 3/3 — AI 근거 교차검증 중 (Claude 분석)...")
            try:
                from claude_client import ClaudeClient
                claude = ClaudeClient()
                cross_check_raw = claude.cross_check_evidence(verified_companies, feedback=feedback)
                # Parse the verdict JSON (with truncation recovery)
                try:
                    verdicts = json.loads(cross_check_raw)
                except json.JSONDecodeError:
                    # Truncated JSON — try to recover complete entries
                    raw = cross_check_raw.strip()
                    if raw.startswith("["):
                        last_brace = raw.rfind("}")
                        if last_brace > 0:
                            raw = raw[:last_brace + 1] + "]"
                            verdicts = json.loads(raw)
                            logger.info(f"Recovered {len(verdicts)} verdicts from truncated JSON")
                        else:
                            raise
                    else:
                        raise
                # Build lookup by company name
                verdict_map = {v["company"]: v for v in verdicts if "company" in v}
                st.session_state.ai_target_verdicts = verdict_map
                verify_bar.progress(1.0)
                n_done = len(verdict_map)
                if n_done < n:
                    verify_status.warning(f"⚠️ {n_done}/{n}개 회사 검증 완료 (일부 잘림)")
                else:
                    verify_status.success(f"✅ {n}개 회사 검증 완료!")
            except Exception as e:
                logger.warning(f"Cross-check failed: {e}")
                st.session_state.ai_target_verdicts = {}
                verify_bar.progress(0.8)
                verify_status.warning(f"교차검증 실패: {e}")
        else:
            st.session_state.ai_target_verification = None
            st.session_state.ai_target_verdicts = {}
    else:
        st.session_state.ai_target_verification = None
        st.session_state.ai_target_verdicts = {}


def _auto_verify_researchers(result_text: str, feedback: str = ""):
    """Parse AI researcher result → external verification → Claude cross-check."""
    import re as _re
    json_match = _re.search(r"```json\s*\n(.*?)```", result_text, _re.DOTALL)
    parsed = None
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
    if not parsed:
        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError:
            pass

    if parsed:
        all_researchers = (parsed.get("tier1_researchers", [])
                           + parsed.get("tier2_researchers", []))
        if all_researchers:
            n = len(all_researchers)
            verify_bar = st.progress(0)
            verify_status = st.empty()

            # Step 2/3: External data collection
            verify_status.info(f"⏱ 2/3 — 외부 데이터 수집 중 ({n}명 연구자: 웹 + PubMed + ClinicalTrials)...")
            verify_bar.progress(0.1)
            from research_client import ResearchClient
            rc = ResearchClient()
            verified_researchers = rc.verify_researchers_batch(all_researchers)
            st.session_state.ai_researcher_verification = verified_researchers
            verify_bar.progress(0.6)

            # Step 3/3: Claude cross-check
            verify_status.info("⏱ 3/3 — AI 근거 교차검증 중 (Claude 분석)...")
            try:
                from claude_client import ClaudeClient
                claude = ClaudeClient()
                cross_check_raw = claude.cross_check_researcher_evidence(
                    verified_researchers, feedback=feedback
                )
                try:
                    verdicts = json.loads(cross_check_raw)
                except json.JSONDecodeError:
                    raw = cross_check_raw.strip()
                    if raw.startswith("["):
                        last_brace = raw.rfind("}")
                        if last_brace > 0:
                            raw = raw[:last_brace + 1] + "]"
                            verdicts = json.loads(raw)
                            logger.info(f"Recovered {len(verdicts)} researcher verdicts from truncated JSON")
                        else:
                            raise
                    else:
                        raise
                verdict_map = {v["researcher"]: v for v in verdicts if "researcher" in v}
                st.session_state.ai_researcher_verdicts = verdict_map
                verify_bar.progress(1.0)
                n_done = len(verdict_map)
                if n_done < n:
                    verify_status.warning(f"⚠️ {n_done}/{n}명 연구자 검증 완료 (일부 잘림)")
                else:
                    verify_status.success(f"✅ {n}명 연구자 검증 완료!")
            except Exception as e:
                logger.warning(f"Researcher cross-check failed: {e}")
                st.session_state.ai_researcher_verdicts = {}
                verify_bar.progress(0.8)
                verify_status.warning(f"교차검증 실패: {e}")
        else:
            st.session_state.ai_researcher_verification = None
            st.session_state.ai_researcher_verdicts = {}
    else:
        st.session_state.ai_researcher_verification = None
        st.session_state.ai_researcher_verdicts = {}


# ── Initialize DB ────────────────────────────────────────
db.init_db()

# ── Page Config ──────────────────────────────────────────
st.set_page_config(
    page_title="Cold Email Campaign Manager",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session State Initialization ─────────────────────────
if "generated_md" not in st.session_state:
    st.session_state.generated_md = None
if "generated_csv" not in st.session_state:
    st.session_state.generated_csv = None
if "review_result" not in st.session_state:
    st.session_state.review_result = None
if "csv_data" not in st.session_state:
    st.session_state.csv_data = None
if "step" not in st.session_state:
    st.session_state.step = "input"  # input → generate → review → preview → send


# ── Helper Functions ─────────────────────────────────────

def extract_csv_block(text: str) -> str | None:
    """Extract CSV block from Claude's markdown output."""
    pattern = r"```csv\s*\n(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        csv_text = match.group(1).strip()
        if csv_text.startswith("contact_name,"):
            return csv_text
    return None


def parse_csv_string(csv_string: str) -> list[dict]:
    """Parse a CSV string into a list of dicts."""
    reader = csv.DictReader(io.StringIO(csv_string))
    return list(reader)


def load_products() -> dict[int, str]:
    """Deprecated: product info now comes from campaign profile.
    Kept for backward compatibility but returns empty dict."""
    return {}


def get_all_campaigns() -> list[dict]:
    """Get all campaigns from the database."""
    conn = db.get_connection()
    rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _find_sent_email_body(email_address: str) -> str:
    """Find the original email body we sent to this address from output CSVs or DB."""
    # 1. Try local DB first
    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT subject, body FROM recipients WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            (email_address,),
        ).fetchone()
        conn.close()
        if row and row["body"]:
            return f"Subject: {row['subject']}\n\n{row['body']}"
    except Exception:
        pass

    # 2. Search output CSV files (most recent first)
    try:
        csv_files = sorted(OUTPUT_DIR.glob("*final*.csv"), reverse=True)
        csv_files += sorted(OUTPUT_DIR.glob("*mailmerge*.csv"), reverse=True)
        for csv_file in csv_files:
            content = csv_file.read_text(encoding="utf-8-sig")
            rows = parse_csv_string(content)
            for row in rows:
                if row.get("email", "") == email_address:
                    subject = row.get("subject", "")
                    body = row.get("body", "").replace("<br>", "\n")
                    return f"Subject: {subject}\n\n{body}"
    except Exception:
        pass

    return ""


def build_campaign_context(profile: dict | None) -> str:
    """Build a campaign context string from an active profile for agent injection."""
    if not profile:
        return ""
    parts = ["## 캠페인 컨텍스트"]
    if profile.get("product_name"):
        parts.append(f"- 제품명: {profile['product_name']}")
    if profile.get("product_description"):
        parts.append(f"- 제품 설명: {profile['product_description']}")
    if profile.get("target_region"):
        parts.append(f"- 타겟 지역: {profile['target_region']}")
    if profile.get("language"):
        parts.append(f"- 언어: {profile['language']}")
    if profile.get("tone"):
        parts.append(f"- 톤: {profile['tone']}")
    if profile.get("cta_type"):
        parts.append(f"- CTA: {profile['cta_type']}")
    if profile.get("sender_context"):
        parts.append(f"- 발신자: {profile['sender_context']}")
    if profile.get("extra_notes"):
        parts.append(f"- 메모: {profile['extra_notes']}")
    return "\n".join(parts)


# ── Session state for reply context ──────────────────────
if "reply_context" not in st.session_state:
    st.session_state.reply_context = None
if "active_page" not in st.session_state:
    st.session_state.active_page = None
if "agent_running" not in st.session_state:
    st.session_state.agent_running = False

# ── Sidebar: Navigation ─────────────────────────────────
st.sidebar.title("Cold Email Manager")

page_options = [
    "⚙️ 캠페인 설정",
    "🎯 타겟 발굴",
    "🔍 컨택 서칭",
    "📝 콜드메일",
    "📊 캠페인 현황",
    "💬 답장 작성",
    "📚 스킬 목록",
]

# Page redirect support — set session key BEFORE radio renders
if st.session_state.active_page in page_options:
    st.session_state.nav_page = st.session_state.active_page
    st.session_state.active_page = None

page = st.sidebar.radio(
    "메뉴",
    page_options,
    key="nav_page",
    label_visibility="collapsed",
)

# ── Agent running lock (CSS overlay via 2-phase rerun) ──────────
# On the rerun where agent_running=True, this CSS renders BEFORE the blocking
# agent.run() call, so the overlay is visible during execution.
if st.session_state.get("agent_running"):
    st.markdown("""
    <style>
    [data-testid="stSidebar"] > div:first-child {
        pointer-events: none !important;
        opacity: 0.5 !important;
    }
    </style>
    <div style="position:fixed;top:0;left:0;width:100vw;height:100vh;
        background:rgba(0,0,0,0.35);z-index:999999999;
        display:flex;align-items:center;justify-content:center;cursor:wait;">
        <div style="background:white;padding:28px 48px;border-radius:14px;
            font-size:17px;box-shadow:0 4px 24px rgba(0,0,0,0.25);text-align:center;">
            <div style="font-size:28px;margin-bottom:8px;">⏳</div>
            Agent 실행 중... 잠시만 기다려주세요<br>
            <span style="font-size:13px;color:#888;margin-top:4px;display:inline-block;">
            탭 전환 시 작업이 취소됩니다</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

# ── Sidebar: Active campaign profile indicator ─────────
if "active_profile_id" not in st.session_state:
    st.session_state.active_profile_id = None
if "active_profile" not in st.session_state:
    st.session_state.active_profile = None
if "active_sender_id" not in st.session_state:
    st.session_state.active_sender_id = None
if "active_sender" not in st.session_state:
    st.session_state.active_sender = None

# Load active profile
if st.session_state.active_profile_id:
    _ap = db.get_campaign_profile(st.session_state.active_profile_id)
    if _ap:
        st.session_state.active_profile = _ap
        st.sidebar.divider()
        st.sidebar.markdown(f"**활성 프로필:** {_ap['name']}")
        st.sidebar.caption(f"{_ap.get('product_name', '')} · {_ap.get('language', 'ja')}")
    else:
        st.session_state.active_profile_id = None
        st.session_state.active_profile = None

# Load active sender profile
if st.session_state.active_sender_id:
    _asp = db.get_sender_profile(st.session_state.active_sender_id)
    if _asp:
        st.session_state.active_sender = _asp
        st.sidebar.caption(f"발신자: {_asp.get('name_en') or _asp.get('name_ja', '')} ({_asp.get('company_en', '')})")
    else:
        st.session_state.active_sender_id = None
        st.session_state.active_sender = None


# ══════════════════════════════════════════════════════════
# PAGE: Campaign Setup (캠페인 설정)
# ══════════════════════════════════════════════════════════

if page == "⚙️ 캠페인 설정":
    st.title("캠페인 설정")
    st.caption("제품/서비스, 세일즈 목적, 타겟 등을 정의하면 Agent 1→2→3에 자동으로 전달됩니다.")

    # ── Existing profiles ──────────────────────────────
    profiles = db.get_campaign_profiles()

    if profiles:
        st.subheader("저장된 프로필")
        for p in profiles:
            pcol1, pcol2, pcol3 = st.columns([4, 1, 1])
            with pcol1:
                is_active = st.session_state.active_profile_id == p["id"]
                label = f"{'✅ ' if is_active else ''}{p['name']}"
                st.markdown(
                    f"**{label}**  \n"
                    f"{p.get('product_name', '')} · {p.get('cta_type', '')} · "
                    f"{p.get('target_region', '')} · {p.get('language', 'en')}"
                )
            with pcol2:
                if st.button("사용", key=f"use_profile_{p['id']}", disabled=is_active):
                    st.session_state.active_profile_id = p["id"]
                    st.session_state.active_profile = p
                    st.session_state._profile_just_activated = True
                    st.rerun()
            with pcol3:
                if st.button("삭제", key=f"del_profile_{p['id']}"):
                    db.delete_campaign_profile(p["id"])
                    if st.session_state.active_profile_id == p["id"]:
                        st.session_state.active_profile_id = None
                        st.session_state.active_profile = None
                    st.rerun()

        # Show activation feedback & next steps
        if st.session_state.get("_profile_just_activated"):
            st.session_state._profile_just_activated = False
            st.toast("프로필이 활성화되었습니다!")

        if st.session_state.active_profile_id:
            _ap = st.session_state.get("active_profile", {})
            st.success(f"활성 프로필: **{_ap.get('name', '')}** — {_ap.get('product_name', '')} · {_ap.get('language', 'en')}")
            nc1, nc2, nc3 = st.columns(3)
            with nc1:
                if st.button("🎯 타겟 발굴로 이동", use_container_width=True):
                    st.session_state.active_page = "🎯 타겟 발굴"
                    st.rerun()
            with nc2:
                if st.button("🔍 컨택 서칭으로 이동", use_container_width=True):
                    st.session_state.active_page = "🔍 컨택 서칭"
                    st.rerun()
            with nc3:
                if st.button("📝 콜드메일로 이동", use_container_width=True):
                    st.session_state.active_page = "📝 콜드메일"
                    st.rerun()

        st.divider()

    # ── Create / Edit form ─────────────────────────────
    st.subheader("새 프로필 생성")

    with st.form("campaign_profile_form", clear_on_submit=True):
        cp_name = st.text_input(
            "프로필 이름 *",
            placeholder="예: CNS 바이오텍 아웃리치",
        )

        cp_col1, cp_col2 = st.columns(2)

        with cp_col1:
            cp_product_name = st.text_input(
                "제품/서비스 이름",
                placeholder="예: AI Drug Discovery Platform",
            )
            cp_language = st.selectbox("언어", ["en", "ja", "ko"], index=0)

        with cp_col2:
            cp_target_region = st.text_input(
                "타겟 지역",
                placeholder="예: Japan, US, Global",
            )
            cp_cta_type = st.selectbox(
                "CTA 유형",
                [
                    "자동 선택",
                    "담당자 추천 요청 (기본)",
                    "15분 대화 요청 (탐색형)",
                    "피드백/의견 요청 (Design Partner)",
                    "자료/인사이트 공유 제안",
                    "Zoom/Web 미팅 제안",
                    "직접 입력",
                ],
            )
            if cp_cta_type == "직접 입력":
                cp_cta_type = st.text_input("CTA (직접 입력)", placeholder="예: 3월 도쿄 방문 시 30분 미팅 가능 여부 확인")
            cp_tone = st.selectbox(
                "톤",
                ["professional", "casual", "formal", "friendly"],
                index=0,
            )

        cp_product_desc = st.text_area(
            "제품/서비스 설명 *",
            placeholder=(
                "예: 우리 회사는 AI 기반 신약 개발 플랫폼을 제공합니다. "
                "타겟 식별부터 리드 최적화까지 전주기를 지원하며, "
                "기존 대비 개발 기간을 40% 단축한 실적이 있습니다."
            ),
            height=120,
        )

        _existing_senders = db.get_sender_profiles()
        # Auto-import sender_profile.md if no profiles exist yet
        if not _existing_senders:
            _sp_md_path = DATA_DIR / "sender_profile.md"
            if _sp_md_path.exists():
                _md_text = _sp_md_path.read_text(encoding="utf-8")
                _field_map = {
                    "이름 (영문)": "name_en", "이름 (일본어)": "name_ja",
                    "직함 (영문)": "title_en", "직함 (일본어)": "title_ja",
                    "회사명 (영문)": "company_en", "회사명 (일본어)": "company_ja",
                    "이메일": "email", "전화번호": "phone",
                }
                _parsed = {}
                for _label, _key in _field_map.items():
                    _m = re.search(rf"\*\*{re.escape(_label)}\*\*:\s*(.+)", _md_text)
                    if _m:
                        _parsed[_key] = _m.group(1).strip()
                _sig_blocks = re.findall(r"## 서명 \((.+?)\)\s*\n+```\n(.*?)```", _md_text, re.DOTALL)
                for _sig_label, _sig_body in _sig_blocks:
                    if "일본어" in _sig_label:
                        _parsed["signature_ja"] = _sig_body.strip()
                    elif "영문" in _sig_label:
                        _parsed["signature_en"] = _sig_body.strip()
                _pname = f"{_parsed.get('name_en', '')} ({_parsed.get('company_en', '')})".strip()
                if not _pname or _pname == "()":
                    _pname = "Default Profile"
                try:
                    db.save_sender_profile(
                        name=_pname,
                        name_en=_parsed.get("name_en", ""),
                        name_ja=_parsed.get("name_ja", ""),
                        title_en=_parsed.get("title_en", ""),
                        title_ja=_parsed.get("title_ja", ""),
                        company_en=_parsed.get("company_en", ""),
                        company_ja=_parsed.get("company_ja", ""),
                        email=_parsed.get("email", ""),
                        phone=_parsed.get("phone", ""),
                        signature_ja=_parsed.get("signature_ja", ""),
                        signature_en=_parsed.get("signature_en", ""),
                    )
                    _existing_senders = db.get_sender_profiles()
                except Exception:
                    pass
        _sender_options = ["직접 입력"] + [f"{s['name']} ({s.get('name_en', '')})" for s in _existing_senders]
        _sender_choice = st.selectbox("발신자 프로필", _sender_options)
        if _sender_choice == "직접 입력":
            cp_sender_context = st.text_area(
                "발신자 소개 (직접 입력)",
                placeholder="예: RISORIUS Inc. 공동창업자, AI/ML 기반 제약 솔루션 전문",
                height=60,
            )
        else:
            _sender_idx = _sender_options.index(_sender_choice) - 1
            _selected_sender = _existing_senders[_sender_idx]
            cp_sender_context = f"{_selected_sender.get('name_en', '')} | {_selected_sender.get('title_en', '')} | {_selected_sender.get('company_en', '')} | {_selected_sender.get('email', '')}"
            st.caption(f"선택됨: {cp_sender_context}")

        cp_extra = st.text_area(
            "추가 메모 (선택)",
            placeholder="예: 2월 방문 예정, 경쟁사 X사 대비 차별점 강조",
            height=60,
        )

        submitted = st.form_submit_button("💾 프로필 저장", use_container_width=True)

        if submitted:
            if not cp_name.strip():
                st.error("프로필 이름을 입력해주세요.")
            elif not cp_product_desc.strip():
                st.error("제품/서비스 설명을 입력해주세요.")
            else:
                new_id = db.save_campaign_profile(
                    name=cp_name.strip(),
                    product_name=cp_product_name.strip(),
                    product_description=cp_product_desc.strip(),
                    target_region=cp_target_region.strip(),
                    language=cp_language,
                    tone=cp_tone,
                    cta_type=cp_cta_type,
                    sender_context=cp_sender_context.strip(),
                    extra_notes=cp_extra.strip(),
                )
                st.session_state.active_profile_id = new_id
                st.success(f"프로필 '{cp_name}' 저장 완료! 자동으로 활성화되었습니다.")
                st.rerun()


# ══════════════════════════════════════════════════════════
# PAGE: Target Discovery (타겟 발굴)
# ══════════════════════════════════════════════════════════

elif page == "🎯 타겟 발굴":
    st.title("타겟 발굴")

    target_mode = st.radio(
        "타겟 유형", ["company", "researcher"],
        format_func={"company": "🏢 회사 타겟", "researcher": "🎓 연구자 타겟"}.__getitem__,
        horizontal=True, key="target_mode", label_visibility="collapsed",
    )

    if target_mode == "company":
        st.caption("제품 설명을 입력하면 AI가 적합한 회사와 직종을 추천하고, 프리셋으로 저장합니다.")

        if "ai_target_result" not in st.session_state:
            st.session_state.ai_target_result = None
        if "ai_target_verification" not in st.session_state:
            st.session_state.ai_target_verification = None
        if "ai_target_verdicts" not in st.session_state:
            st.session_state.ai_target_verdicts = {}
        if "agent_log" not in st.session_state:
            st.session_state.agent_log = []
        if "ai_target_parsed" not in st.session_state:
            st.session_state.ai_target_parsed = None
        if "_regen_preset" not in st.session_state:
            st.session_state._regen_preset = None

        # ── Input Section ─────────────────────────────
        st.subheader("제품/서비스 정보")

        ai_product_desc = st.text_area(
            "제품/서비스 설명 (필수)",
            height=150,
            placeholder="예: Dataset과 연구 목적을 프롬프트로 넣으면 임상시험 시뮬레이션과 바이오마커 발굴 리포트를 생성하는 AI co-scientist",
            key="ai_product_desc",
        )

        col1, col2 = st.columns(2)
        with col1:
            ai_target_hint = st.text_input(
                "희망 대상/관련 직종 (자유 입력, 선택)",
                placeholder="예: 바이오텍 R&D 담당자, CNS 연구 관련",
                key="ai_target_hint",
            )
        with col2:
            ai_region = st.text_input(
                "지역 제한 (선택)",
                placeholder="예: Japan, US, Europe 등 (비워두면 전체)",
                key="ai_target_region",
            )

        # Collect companies for exclusion option (from presets + current results)
        _existing_presets = db.get_presets()
        _preset_companies = set()
        for p in _existing_presets:
            for c in (p.get("companies") or "").split(","):
                c = c.strip()
                if c:
                    _preset_companies.add(c)

        _current_companies = set()
        if st.session_state.ai_target_parsed:
            for c in st.session_state.ai_target_parsed.get("tier1_companies", []):
                _current_companies.add(c.get("name", ""))
            for c in st.session_state.ai_target_parsed.get("tier2_companies", []):
                _current_companies.add(c.get("name", ""))
            _current_companies.discard("")

        _all_excludable = _preset_companies | _current_companies

        exclude_companies_set = set()
        if _all_excludable:
            ecol1, ecol2 = st.columns(2)
            with ecol1:
                if _preset_companies:
                    if st.checkbox(
                        f"프리셋 회사 제외 ({len(_preset_companies)}개)",
                        help=f"저장된 프리셋: {', '.join(list(_preset_companies)[:8])}{'...' if len(_preset_companies) > 8 else ''}",
                    ):
                        exclude_companies_set |= _preset_companies
            with ecol2:
                if _current_companies:
                    if st.checkbox(
                        f"현재 결과 회사 제외 ({len(_current_companies)}개)",
                        help="현재 화면에 표시된 회사를 제외하고 새로운 회사만 추천",
                    ):
                        exclude_companies_set |= _current_companies

        if st.button("🤖 AI 타겟 추천 실행", type="primary", disabled=not ai_product_desc or st.session_state.get("agent_running")):
            # Combine product desc with hint
            full_desc = ai_product_desc
            if ai_target_hint:
                full_desc += f"\n\n희망 대상/관련 직종: {ai_target_hint}"

            # Build exclusion list
            exclude_companies = sorted(exclude_companies_set)
            exclude_section = ""
            if exclude_companies:
                exclude_section = (
                    f"\n\n제외 대상 회사 (절대 추천하지 말 것): "
                    f"{', '.join(exclude_companies[:30])}"
                )

            region_line = f"\n지역 제한: {ai_region}" if ai_region else ""

            _ctx = build_campaign_context(st.session_state.get("active_profile"))
            _ctx_section = f"\n\n{_ctx}" if _ctx else ""
            agent_request = (
                f"아래 제품에 대해 타겟 회사를 찾아줘.\n\n"
                f"## 제품 설명\n{full_desc}"
                f"{region_line}{exclude_section}{_ctx_section}\n\n"
                f"다양한 검색어로 웹 리서치를 수행한 뒤, "
                f"결과를 Tier 1/Tier 2로 분류하고 save_results로 저장해줘."
            )

            # Phase 1: save params and rerun to show overlay
            _run_profile_id = st.session_state.get("active_profile_id")
            st.session_state._pending_agent1 = {
                "request": agent_request,
                "feedback": db.get_combined_feedback_text(_run_profile_id),
            }
            st.session_state.agent_running = True
            st.rerun()

        # Phase 2: execute pending Agent 1 task (overlay is already visible)
        if st.session_state.get("_pending_agent1"):
            _task = st.session_state.pop("_pending_agent1")
            try:
                from agent import CompanyListingAgent

                tracker = AgentProgressTracker("agent1")

                agent = CompanyListingAgent(
                    extra_feedback=_task["feedback"],
                    on_tool_call=tracker.on_tool_call,
                    on_tool_result=tracker.on_tool_result,
                    on_text=tracker.on_text,
                )

                agent_output = agent.run(_task["request"])

                st.session_state.agent_log = tracker.tool_log

                # Use saved JSON result if available, otherwise try parsing agent output
                result_json = agent.result_json
                if result_json:
                    st.session_state.ai_target_result = result_json
                else:
                    st.session_state.ai_target_result = agent_output

                st.session_state.ai_target_parsed = None
                st.session_state.ai_target_verification = None
                st.session_state.ai_target_verdicts = {}

                tracker.complete("타겟 탐색 완료! 근거 검증 시작...")

                # Auto-verify immediately after agent completes
                _auto_verify(st.session_state.ai_target_result, feedback=_task["feedback"])

            except Exception as e:
                if 'tracker' in dir():
                    tracker.fail(f"AI 타겟 추천 실패: {e}")
                else:
                    st.error(f"AI 타겟 추천 실패: {e}")
                import traceback
                st.code(traceback.format_exc())
            finally:
                st.session_state.agent_running = False
            st.rerun()

        # ── Results Section ───────────────────────────
        if st.session_state.ai_target_result:
            st.divider()
            result_text = st.session_state.ai_target_result

            # Parse JSON on first load, then use editable copy
            if st.session_state.ai_target_parsed is None:
                json_match = re.search(r"```json\s*\n(.*?)```", result_text, re.DOTALL)
                parsed = None
                if json_match:
                    try:
                        parsed = json.loads(json_match.group(1))
                    except json.JSONDecodeError:
                        pass
                if not parsed:
                    try:
                        parsed = json.loads(result_text)
                    except json.JSONDecodeError:
                        pass
                st.session_state.ai_target_parsed = parsed

            parsed = st.session_state.ai_target_parsed

            if parsed:
                st.subheader("추천 결과")
                st.success(f"**{parsed.get('product_summary', '')}**")

                # Show agent activity log
                if st.session_state.agent_log:
                    with st.expander(f"Agent 활동 로그 ({len(st.session_state.agent_log)}건)", expanded=False):
                        st.code("\n".join(st.session_state.agent_log), language=None)

                # Show analysis if present
                if parsed.get("analysis"):
                    with st.expander("제품 분석", expanded=True):
                        st.markdown(parsed["analysis"])

                tier1 = parsed.get("tier1_companies", [])
                tier2 = parsed.get("tier2_companies", [])

                # Build verification + verdict lookups
                _vmap = {}
                if st.session_state.ai_target_verification:
                    for v in st.session_state.ai_target_verification:
                        _vmap[v.get("name", "")] = v.get("verification", {})
                _verdict_map = st.session_state.get("ai_target_verdicts", {})

                _tier_tab = st.radio(
                    "결과 보기",
                    ["tier1", "tier2", "titles"],
                    format_func=lambda x: {
                        "tier1": f"Tier 1 ({len(tier1)}개)",
                        "tier2": f"Tier 2 ({len(tier2)}개)",
                        "titles": "추천 직종",
                    }[x],
                    horizontal=True,
                    label_visibility="collapsed",
                    key="ai_target_tier_tab",
                )

                if _tier_tab == "tier1":
                    if tier1:
                        for idx, c in enumerate(tier1):
                            col_card, col_actions = st.columns([5, 1])
                            with col_card:
                                _render_company_card(c, _vmap.get(c["name"]), _verdict_map.get(c["name"]))
                            with col_actions:
                                st.write("")  # spacing
                                if st.button("→ T2", key=f"t1to2_{idx}", help="Tier 2로 이동"):
                                    company = tier1.pop(idx)
                                    tier2.append(company)
                                    st.rerun()
                                if st.button("삭제", key=f"del_t1_{idx}", help="목록에서 제거"):
                                    tier1.pop(idx)
                                    st.rerun()
                    else:
                        st.info("Tier 1 회사 없음")

                elif _tier_tab == "tier2":
                    if tier2:
                        for idx, c in enumerate(tier2):
                            col_card, col_actions = st.columns([5, 1])
                            with col_card:
                                _render_company_card(c, _vmap.get(c["name"]), _verdict_map.get(c["name"]))
                            with col_actions:
                                st.write("")  # spacing
                                if st.button("→ T1", key=f"t2to1_{idx}", help="Tier 1으로 이동"):
                                    company = tier2.pop(idx)
                                    tier1.append(company)
                                    st.rerun()
                                if st.button("삭제", key=f"del_t2_{idx}", help="목록에서 제거"):
                                    tier2.pop(idx)
                                    st.rerun()
                    else:
                        st.info("Tier 2 회사 없음")

                else:  # titles
                    dm = parsed.get("decision_makers", [])
                    eu = parsed.get("end_users", [])
                    if dm:
                        st.markdown("**의사결정자 (Decision Makers):**")
                        for t in dm:
                            st.markdown(f"- {t}")
                    if eu:
                        st.markdown("**실제 사용자 (End Users):**")
                        for t in eu:
                            st.markdown(f"- {t}")

                # ── Verification Summary ──────────────
                if _verdict_map:
                    st.divider()
                    st.subheader("근거 교차검증 결과")
                    st.caption("외부 데이터(웹 + ClinicalTrials + PubMed) 수집 후 Claude가 AI 근거와 비교 분석")

                    total_v = len(_verdict_map)
                    confirmed = sum(1 for v in _verdict_map.values() if v.get("verdict") == "confirmed")
                    v_partial = sum(1 for v in _verdict_map.values() if v.get("verdict") == "partial")
                    unverified = sum(1 for v in _verdict_map.values() if v.get("verdict") == "unverified")
                    wrong = sum(1 for v in _verdict_map.values() if v.get("verdict") == "wrong")

                    vcol1, vcol2, vcol3, vcol4 = st.columns(4)
                    vcol1.metric("✅ 확인됨", f"{confirmed}/{total_v}")
                    vcol2.metric("⚠️ 일부 확인", f"{v_partial}/{total_v}")
                    vcol3.metric("❓ 미검증", f"{unverified}/{total_v}")
                    vcol4.metric("❌ 불일치", f"{wrong}/{total_v}")

                    if wrong > 0:
                        st.error(f"{wrong}개 회사의 AI 근거가 외부 데이터와 불일치합니다. 해당 회사를 확인하세요.")
                    if unverified > 0:
                        st.warning(f"{unverified}개 회사는 외부 데이터가 부족하여 검증 불가합니다.")
                elif st.session_state.ai_target_verification:
                    st.divider()
                    st.subheader("근거 검증 결과")
                    st.caption("외부 데이터 수집 완료 (교차검증 미완료)")

                    total_v = len(st.session_state.ai_target_verification)
                    verified = sum(1 for v in st.session_state.ai_target_verification
                                   if v.get("verification", {}).get("status") == "verified")
                    partial = sum(1 for v in st.session_state.ai_target_verification
                                  if v.get("verification", {}).get("status") == "partial")
                    no_data = sum(1 for v in st.session_state.ai_target_verification
                                  if v.get("verification", {}).get("status") == "no_data")

                    vcol1, vcol2, vcol3 = st.columns(3)
                    vcol1.metric("검증됨", f"{verified}/{total_v}")
                    vcol2.metric("일부 확인", f"{partial}/{total_v}")
                    vcol3.metric("데이터 없음", f"{no_data}/{total_v}")

                # ── Export Results as Markdown ─────────
                st.divider()
                st.subheader("결과 내보내기")

                def _build_company_export_md():
                    lines = [f"# 타겟 회사 추천 결과\n"]
                    lines.append(f"**제품 요약:** {parsed.get('product_summary', '')}\n")
                    _exp_analysis = parsed.get("analysis", "")
                    if _exp_analysis:
                        lines.append(f"## 분석\n{_exp_analysis}\n")

                    for tier_label, tier_list, tier_name in [
                        ("Tier 1 (핵심 타겟)", tier1, "tier1"),
                        ("Tier 2 (잠재적 타겟)", tier2, "tier2"),
                    ]:
                        lines.append(f"## {tier_label} — {len(tier_list)}개\n")
                        for i, c in enumerate(tier_list, 1):
                            c_name = c.get("name", "")
                            lines.append(f"### {i}. {c_name}")
                            if c.get("reason"):
                                lines.append(f"- **요약:** {c['reason']}")
                            if c.get("evidence"):
                                lines.append(f"- **근거:** {c['evidence']}")
                            if c.get("tier_reason"):
                                lines.append(f"- **Tier 산정:** {c['tier_reason']}")
                            # Add verdict if available
                            _v = _verdict_map.get(c_name, {})
                            if _v:
                                _emoji = {"confirmed": "✅", "partial": "⚠️", "unverified": "❓", "wrong": "❌"}.get(_v.get("verdict", ""), "")
                                lines.append(f"- **교차검증:** {_emoji} {_v.get('verdict', '')} — {_v.get('explanation', '')}")
                            lines.append("")

                    dm = parsed.get("decision_makers", [])
                    eu = parsed.get("end_users", [])
                    if dm or eu:
                        lines.append("## 추천 직종\n")
                        if dm:
                            lines.append(f"**의사결정자:** {', '.join(dm)}")
                        if eu:
                            lines.append(f"**실제 사용자:** {', '.join(eu)}")
                        lines.append("")

                    return "\n".join(lines)

                _export_md = _build_company_export_md()
                st.download_button(
                    "📥 Markdown으로 내보내기",
                    data=_export_md,
                    file_name="target_companies_result.md",
                    mime="text/markdown",
                    key="export_company_md",
                )

                # ── Feedback Section ───────────────────
                st.divider()
                st.subheader("피드백")
                st.caption("결과에 대한 피드백을 입력하면 AI가 반영해서 재추천합니다.")

                ai_feedback = st.text_area(
                    "피드백 (자유 입력)",
                    height=100,
                    placeholder="예: CRO는 빼줘, 바이오텍만 남겨, 일본 회사를 더 추가해줘, Tier 2에서 XX는 Tier 1으로 올려줘",
                    key="ai_feedback",
                )

                _has_profile = bool(st.session_state.get("active_profile_id"))
                fcol1, fcol2, fcol3 = st.columns([2, 2, 1])
                with fcol1:
                    _fb_global = st.checkbox("글로벌", value=True, key="fb_scope_global",
                                             help="모든 프로필에 공통 적용")
                    _fb_profile = st.checkbox(
                        "프로필 전용", value=_has_profile, key="fb_scope_profile",
                        disabled=not _has_profile,
                        help="활성 프로필에서만 적용",
                    )
                with fcol2:
                    if st.button("🔄 피드백 반영 재추천", type="primary", disabled=not ai_feedback or st.session_state.get("agent_running")):
                        # Save feedback to DB — global and/or profile-specific
                        _active_pid = st.session_state.get("active_profile_id")
                        if _fb_global:
                            db.add_target_feedback(
                                ai_feedback,
                                product_summary=parsed.get("product_summary", ""),
                                profile_id=None,
                            )
                        if _fb_profile and _active_pid:
                            db.add_target_feedback(
                                ai_feedback,
                                product_summary=parsed.get("product_summary", ""),
                                profile_id=_active_pid,
                            )
                        prev_json = json.dumps(parsed, ensure_ascii=False)
                        full_desc = ai_product_desc or ""
                        if ai_target_hint:
                            full_desc += f"\n\n희망 대상/관련 직종: {ai_target_hint}"

                        _fb_run_pid = st.session_state.get("active_profile_id")

                        # Phase 1: save params and rerun to show overlay
                        st.session_state._pending_fb_rerun = {
                            "request": (
                                f"이전 추천 결과에 대한 사용자 피드백을 반영하여 수정된 결과를 만들어줘.\n\n"
                                f"## 제품 설명\n{full_desc}\n\n"
                                f"## 이전 추천 결과\n```json\n{prev_json}\n```\n\n"
                                f"## 사용자 피드백\n{ai_feedback}\n\n"
                                f"피드백을 정확히 반영하여 수정해줘. "
                                f"필요하면 추가 웹 리서치를 해도 좋아. "
                                f"최종 결과는 반드시 save_results로 저장해줘."
                            ),
                            "feedback": db.get_combined_feedback_text(_fb_run_pid),
                        }
                        st.session_state.agent_running = True
                        st.rerun()

                # Phase 2: execute pending feedback re-recommendation
                if st.session_state.get("_pending_fb_rerun"):
                    _task = st.session_state.pop("_pending_fb_rerun")
                    try:
                        from agent import CompanyListingAgent

                        fb_tracker = AgentProgressTracker("agent1")
                        agent = CompanyListingAgent(
                            extra_feedback=_task["feedback"],
                            on_tool_call=fb_tracker.on_tool_call,
                            on_tool_result=fb_tracker.on_tool_result,
                        )
                        agent.run(_task["request"])

                        result_json = agent.result_json
                        if result_json:
                            st.session_state.ai_target_result = result_json
                        st.session_state.ai_target_parsed = None
                        st.session_state.ai_target_verification = None
                        st.session_state.ai_target_verdicts = {}
                        fb_tracker.complete("피드백 반영 완료!")
                    except Exception as e:
                        if 'fb_tracker' in dir():
                            fb_tracker.fail(f"재추천 실패: {e}")
                        else:
                            st.error(f"재추천 실패: {e}")
                    finally:
                        st.session_state.agent_running = False
                    st.rerun()
                with fcol2:
                    if st.button("🗑️ 결과 초기화"):
                        st.session_state.ai_target_result = None
                        st.session_state.ai_target_parsed = None
                        st.session_state.ai_target_verification = None
                        st.session_state.ai_target_verdicts = {}
                        st.session_state.agent_log = []
                        st.rerun()

                # ── Save as Preset ─────────────────────
                st.divider()
                st.subheader("프리셋으로 저장")
                st.caption("추천 결과를 프리셋으로 저장하면 '컨택 서칭' 페이지에서 바로 사용할 수 있습니다.")

                rec = parsed.get("recommended_search_params", {})
                tier1_names = [c["name"] for c in parsed.get("tier1_companies", [])]
                tier2_names = [c["name"] for c in parsed.get("tier2_companies", [])]
                # Combine all recommended titles (decision_makers + end_users)
                _all_titles = parsed.get("decision_makers", []) + parsed.get("end_users", [])
                _all_titles_str = ", ".join(_all_titles) if _all_titles else rec.get("titles", "")

                save_scope = st.radio(
                    "저장할 회사 범위",
                    ["Tier 1 + Tier 2 전체", "Tier 1만", "Tier 2만", "Tier 1 / Tier 2 각각 (2개 프리셋)"],
                    horizontal=True,
                    key="ai_save_scope",
                )

                if save_scope == "Tier 1 / Tier 2 각각 (2개 프리셋)":
                    _save_groups = [("_T1", tier1_names), ("_T2", tier2_names)]
                    companies_to_save = tier1_names + tier2_names  # for preview
                elif save_scope == "Tier 2만":
                    _save_groups = [("", tier2_names)]
                    companies_to_save = tier2_names
                elif save_scope == "Tier 1만":
                    _save_groups = [("", tier1_names)]
                    companies_to_save = tier1_names
                else:
                    _save_groups = [("", tier1_names + tier2_names)]
                    companies_to_save = tier1_names + tier2_names

                # Preview what will be saved
                with st.expander("저장될 프리셋 내용 미리보기", expanded=False):
                    st.markdown(f"**산업:** {rec.get('industry', '')}")
                    st.markdown(f"**직함:** {_all_titles_str}")
                    st.markdown(f"**키워드:** {rec.get('keywords', '')}")
                    if save_scope == "Tier 1 / Tier 2 각각 (2개 프리셋)":
                        st.markdown(f"**Tier 1 ({len(tier1_names)}개):** {', '.join(tier1_names[:10])}{'...' if len(tier1_names) > 10 else ''}")
                        st.markdown(f"**Tier 2 ({len(tier2_names)}개):** {', '.join(tier2_names[:10])}{'...' if len(tier2_names) > 10 else ''}")
                    else:
                        st.markdown(f"**회사 ({len(companies_to_save)}개):** {', '.join(companies_to_save[:10])}{'...' if len(companies_to_save) > 10 else ''}")

                if save_scope == "Tier 1 / Tier 2 각각 (2개 프리셋)":
                    _ncol1, _ncol2 = st.columns(2)
                    with _ncol1:
                        preset_name_t1 = st.text_input(
                            "Tier 1 프리셋 이름",
                            value=f"AI_{datetime.now().strftime('%y%m%d')}_T1",
                            key="ai_preset_name_t1",
                        )
                    with _ncol2:
                        preset_name_t2 = st.text_input(
                            "Tier 2 프리셋 이름",
                            value=f"AI_{datetime.now().strftime('%y%m%d')}_T2",
                            key="ai_preset_name_t2",
                        )
                    # Override _save_groups with individual names
                    _save_groups = [(preset_name_t1, tier1_names), (preset_name_t2, tier2_names)]
                    _can_save = bool(preset_name_t1 and preset_name_t2)
                else:
                    preset_name = st.text_input(
                        "프리셋 이름",
                        value=f"AI_{datetime.now().strftime('%y%m%d')}",
                        key="ai_preset_name",
                    )
                    # Use preset_name directly as the full name
                    _save_groups = [(preset_name, c) for _, c in _save_groups]
                    _can_save = bool(preset_name)

                if st.button("💾 프리셋 저장 → 컨택 서칭", type="primary", disabled=not _can_save):
                        for _name, _companies in _save_groups:
                            if not _companies:
                                continue
                            db.save_preset(
                                name=_name,
                                industry=rec.get("industry", ""),
                                titles=_all_titles_str,
                                locations=ai_region or "",
                                companies=", ".join(_companies),
                                keywords=rec.get("keywords", ""),
                                max_results=100,
                                feedback_hash=_get_feedback_hash(),
                                product_description=ai_product_desc or "",
                                target_hint=ai_target_hint or "",
                                target_region=ai_region or "",
                            )
                        _saved_names = ", ".join(f"'{n}'" for n, c in _save_groups if c)
                        st.session_state.ai_target_result = None
                        st.session_state.ai_target_parsed = None
                        st.session_state.ai_target_verification = None
                        st.session_state.ai_target_verdicts = {}
                        st.session_state.ai_web_context = ""
                        st.session_state.active_page = "🔍 컨택 서칭"
                        st.session_state.contact_search_mode = "manual"
                        st.session_state.prospect_step = "search"
                        st.success(f"프리셋 {_saved_names} 저장 완료! 컨택 서칭으로 이동합니다.")
                        st.rerun()
            else:
                # Couldn't parse JSON, show raw
                st.warning("JSON 파싱 실패. 원본 결과:")
                st.markdown(result_text[:3000])
                if len(result_text) > 3000:
                    st.caption("... (출력이 길어 일부만 표시)")
                if st.button("🗑️ 결과 초기화"):
                    st.session_state.ai_target_result = None
                    st.session_state.ai_target_parsed = None
                    st.session_state.ai_target_verification = None
                    st.session_state.ai_target_verdicts = {}
                    st.session_state.ai_web_context = ""
                    st.rerun()

        # ── Previous presets (for reference) ──────────
        st.divider()
        st.subheader("저장된 프리셋 목록")
        saved_presets = db.get_presets()
        current_fb_hash = _get_feedback_hash()
        if saved_presets:
            for sp in saved_presets:
                companies_preview = sp.get("companies", "")
                companies_count = len([c for c in companies_preview.split(",") if c.strip()]) if companies_preview else 0
                stale = sp.get("feedback_hash") and sp["feedback_hash"] != current_fb_hash
                stale_tag = " ⚠️ _피드백 변경됨_" if stale else ""
                has_product_desc = bool((sp.get("product_description") or "").strip())
                sp_col1, sp_col2, sp_col3 = st.columns([5, 1, 1])
                with sp_col1:
                    st.markdown(
                        f"- **{sp['name']}** — {sp.get('industry', '')} | "
                        f"직함: {sp.get('titles', '')[:30]} | "
                        f"회사: {companies_count}개{stale_tag}"
                    )
                with sp_col2:
                    regen_disabled = not has_product_desc
                    regen_help = "제품 설명 미저장 — 새 프리셋부터 재생성 가능" if regen_disabled else "현재 피드백으로 타겟 재탐색"
                    if st.button("재생성", key=f"regen_preset_{sp['id']}", disabled=regen_disabled or st.session_state.get("agent_running"), help=regen_help):
                        st.session_state._regen_preset = sp
                        st.session_state.agent_running = True
                        st.rerun()
                with sp_col3:
                    if st.button("삭제", key=f"del_preset_{sp['id']}"):
                        db.delete_preset(sp["id"])
                        st.rerun()

            # Phase 2: handle preset regeneration (overlay already visible)
            if st.session_state.get("_regen_preset") and st.session_state.get("agent_running"):
                rp = st.session_state._regen_preset
                st.info(f"프리셋 **{rp['name']}** 재생성 중... (피드백 반영)")
                regen_desc = rp.get("product_description", "")
                regen_hint = rp.get("target_hint", "")
                regen_region = rp.get("target_region", "")

                full_desc = regen_desc
                if regen_hint:
                    full_desc += f"\n\n희망 대상/관련 직종: {regen_hint}"

                region_line = f"\n지역 제한: {regen_region}" if regen_region else ""

                existing_companies = [c.strip() for c in rp.get("companies", "").split(",") if c.strip()]

                _profile_id = st.session_state.get("active_profile_id")
                _profile_fb = db.get_combined_feedback_text(_profile_id)

                _ctx = build_campaign_context(st.session_state.get("active_profile"))
                _ctx_section = f"\n\n{_ctx}" if _ctx else ""

                agent_request = (
                    f"아래 제품에 대해 타겟 회사를 찾아줘.\n\n"
                    f"## 제품 설명\n{full_desc}"
                    f"{region_line}{_ctx_section}\n\n"
                    f"다양한 검색어로 웹 리서치를 수행한 뒤, "
                    f"결과를 Tier 1/Tier 2로 분류하고 save_results로 저장해줘."
                )

                try:
                    from agent import CompanyListingAgent

                    regen_tracker = AgentProgressTracker("agent1")
                    agent = CompanyListingAgent(
                        extra_feedback=_profile_fb,
                        on_tool_call=regen_tracker.on_tool_call,
                        on_tool_result=regen_tracker.on_tool_result,
                        on_text=regen_tracker.on_text,
                    )
                    agent.run(agent_request)

                    result_json = agent.result_json
                    if result_json:
                        st.session_state.ai_target_result = result_json
                    else:
                        st.session_state.ai_target_result = None

                    st.session_state.ai_target_parsed = None
                    st.session_state.ai_target_verification = None
                    st.session_state.ai_target_verdicts = {}
                    regen_tracker.complete("재생성 완료!")

                    # Auto-verify
                    if st.session_state.ai_target_result:
                        _auto_verify(st.session_state.ai_target_result, feedback=_profile_fb)
                except Exception as e:
                    st.error(f"재생성 실패: {e}")
                    import traceback
                    st.code(traceback.format_exc())
                finally:
                    st.session_state._regen_preset = None
                    st.session_state.agent_running = False
                st.rerun()
        else:
            st.info("저장된 프리셋이 없습니다. AI 타겟 추천을 실행해서 프리셋을 만들어보세요.")

        # ── Feedback Log Management ──────────────────
        st.divider()
        st.subheader("피드백 이력 관리")

        _active_pid = st.session_state.get("active_profile_id")
        _active_profile_name = ""
        if _active_pid:
            _ap_data = db.get_campaign_profile(_active_pid)
            _active_profile_name = _ap_data["name"] if _ap_data else ""

        fb_tab_global, fb_tab_profile = st.tabs([
            "글로벌 (모든 프로필 공통)",
            f"프로필 전용 ({_active_profile_name})" if _active_profile_name else "프로필 전용 (미선택)",
        ])

        with fb_tab_global:
            st.caption("여기에 누적된 피드백은 **모든** 타겟 추천 시 자동 반영됩니다.")
            # Read and parse file-based global feedback entries
            feedback_entries = []
            if _TARGET_FEEDBACK_PATH.exists():
                raw = _TARGET_FEEDBACK_PATH.read_text(encoding="utf-8")
                for line in raw.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- ["):
                        feedback_entries.append(stripped)

            # Also show DB-based global feedback
            db_global_fb = db.get_target_feedback(profile_id=None)

            if feedback_entries:
                st.markdown("**파일 기반 (레거시)**")
                for i, entry in enumerate(feedback_entries):
                    col_text, col_del = st.columns([9, 1])
                    with col_text:
                        st.markdown(entry)
                    with col_del:
                        if st.button("x", key=f"del_fb_{i}"):
                            feedback_entries.pop(i)
                            _rewrite_feedback_log(feedback_entries)
                            st.rerun()

            if db_global_fb:
                for fb in db_global_fb:
                    col_text, col_del = st.columns([9, 1])
                    ts = fb["created_at"][:16] if fb.get("created_at") else ""
                    ps = f"({fb['product_summary']}) " if fb.get("product_summary") else ""
                    with col_text:
                        st.markdown(f"- [{ts}] {ps}{fb['feedback']}")
                    with col_del:
                        if st.button("x", key=f"del_dbfb_g_{fb['id']}"):
                            db.delete_target_feedback(fb["id"])
                            st.rerun()

            total_global = len(feedback_entries) + len(db_global_fb)
            if total_global:
                st.caption(f"총 {total_global}건")
            else:
                st.info("글로벌 피드백이 없습니다.")

        with fb_tab_profile:
            if not _active_pid:
                st.warning("캠페인 프로필을 먼저 활성화하세요. 이 탭은 활성 프로필 전용 피드백을 관리합니다.")
            else:
                st.caption(f"**{_active_profile_name}** 프로필에서 타겟 추천할 때만 적용되는 피드백입니다.")
                profile_fb = db.get_target_feedback(profile_id=_active_pid)
                if profile_fb:
                    for fb in profile_fb:
                        col_text, col_del = st.columns([9, 1])
                        ts = fb["created_at"][:16] if fb.get("created_at") else ""
                        ps = f"({fb['product_summary']}) " if fb.get("product_summary") else ""
                        with col_text:
                            st.markdown(f"- [{ts}] {ps}{fb['feedback']}")
                        with col_del:
                            if st.button("x", key=f"del_dbfb_p_{fb['id']}"):
                                db.delete_target_feedback(fb["id"])
                                st.rerun()
                    st.caption(f"총 {len(profile_fb)}건")
                else:
                    st.info(f"'{_active_profile_name}' 프로필 전용 피드백이 없습니다.")

        # ── Unified feedback input (below tabs) ──────────
        st.markdown("#### 피드백 추가")
        manual_fb = st.text_input(
            "피드백 내용",
            placeholder="예: CRO/CMO 회사는 항상 제외, 바이오텍만 남겨줘",
            key="manual_fb_unified",
        )
        _uf_c1, _uf_c2, _uf_c3 = st.columns([1, 1, 1])
        with _uf_c1:
            _uf_global = st.checkbox("글로벌 (모든 프로필)", value=True, key="uf_scope_global")
        with _uf_c2:
            _uf_profile = st.checkbox(
                f"프로필 전용 ({_active_profile_name})" if _active_profile_name else "프로필 전용 (미선택)",
                value=bool(_active_pid),
                key="uf_scope_profile",
                disabled=not _active_pid,
            )
        with _uf_c3:
            if st.button("추가", key="add_fb_unified", disabled=not manual_fb or (not _uf_global and not _uf_profile)):
                if _uf_global:
                    db.add_target_feedback(manual_fb, product_summary="수동 입력", profile_id=None)
                if _uf_profile and _active_pid:
                    db.add_target_feedback(manual_fb, product_summary="수동 입력", profile_id=_active_pid)
                st.rerun()



    elif target_mode == "researcher":
        st.caption("제품 설명을 입력하면 AI가 적합한 학술 연구자/교수를 추천합니다.")

        if "ai_researcher_result" not in st.session_state:
            st.session_state.ai_researcher_result = None
        if "ai_researcher_parsed" not in st.session_state:
            st.session_state.ai_researcher_parsed = None
        if "ai_researcher_verification" not in st.session_state:
            st.session_state.ai_researcher_verification = None
        if "ai_researcher_verdicts" not in st.session_state:
            st.session_state.ai_researcher_verdicts = {}

        # ── Input Section ─────────────────────────────
        st.subheader("제품/서비스 정보")

        _active_pid = st.session_state.get("active_profile_id")
        _run_profile_id = _active_pid

        # Build campaign context if profile active
        _r_campaign_ctx = ""
        if _active_pid:
            _r_campaign_ctx = build_campaign_context(st.session_state.get("active_profile"))
            if _r_campaign_ctx:
                with st.expander("활성 프로필 컨텍스트 (자동 포함)", expanded=False):
                    st.text(_r_campaign_ctx[:500])

        researcher_product_desc = st.text_area(
            "제품/서비스 설명 (필수)",
            height=150,
            placeholder="예: CNS dataset + 연구 목적을 입력하면 임상시험 시뮬레이션과 바이오마커 리포트를 생성하는 AI co-scientist",
            key="researcher_product_desc",
        )

        _rc1, _rc2 = st.columns(2)
        with _rc1:
            researcher_areas = st.text_input(
                "타겟 연구 분야 (선택)",
                placeholder="예: 신경과학, 정신의학, 뇌전증, 수면 연구",
                key="researcher_areas",
            )
        with _rc2:
            researcher_region = st.text_input(
                "지역 제한 (선택)",
                placeholder="예: Japan, US, Europe",
                key="researcher_region",
            )

        # ── Execute Button ────────────────────────────
        if st.button("🤖 AI 연구자 추천 실행", type="primary",
                      disabled=not researcher_product_desc or st.session_state.get("agent_running")):
            full_desc = researcher_product_desc
            if _r_campaign_ctx:
                full_desc = f"{_r_campaign_ctx}\n\n{researcher_product_desc}"
            if researcher_areas:
                full_desc += f"\n\n타겟 연구 분야: {researcher_areas}"
            region_line = f"\n지역 제한: {researcher_region}" if researcher_region else ""

            agent_request = (
                f"아래 제품에 적합한 학술 연구자/교수를 찾아줘.\n\n"
                f"## 제품 설명\n{full_desc}{region_line}\n\n"
                f"다양한 검색어로 웹 리서치를 수행한 뒤, "
                f"결과를 Tier 1/Tier 2로 분류하고 JSON으로 출력해줘."
            )

            st.session_state._pending_researcher_agent = {
                "request": agent_request,
                "feedback": db.get_combined_feedback_text(_run_profile_id),
            }
            st.session_state.agent_running = True
            st.rerun()

        # Phase 2: execute pending researcher agent
        if st.session_state.get("_pending_researcher_agent"):
            # Overlay
            st.markdown(
                '<div style="position:fixed;top:0;left:0;width:100vw;height:100vh;'
                'background:rgba(0,0,0,0.55);z-index:9999;display:flex;'
                'align-items:center;justify-content:center;">'
                '<div style="background:#1e1e2e;padding:2rem 3rem;border-radius:12px;'
                'color:white;text-align:center;font-size:1.2rem;">'
                '🔬 AI 연구자 추천 실행 중...<br>'
                '<small style="color:#aaa;">웹 검색 → 분석 → 연구자 추천 (1~3분 소요)</small>'
                '</div></div>',
                unsafe_allow_html=True,
            )

            _task = st.session_state.pop("_pending_researcher_agent")
            try:
                from agent import ResearcherFinderAgent
                tracker = AgentProgressTracker("agent1")
                agent = ResearcherFinderAgent(
                    extra_feedback=_task["feedback"],
                    on_tool_call=tracker.on_tool_call,
                    on_tool_result=tracker.on_tool_result,
                    on_text=tracker.on_text,
                )
                agent.run(_task["request"])
                result_json = agent.result_json
                if result_json:
                    st.session_state.ai_researcher_result = result_json
                    st.session_state.ai_researcher_parsed = None
                    st.session_state.ai_researcher_verification = None
                    st.session_state.ai_researcher_verdicts = {}

                tracker.complete("연구자 탐색 완료! 근거 검증 시작...")

                # Auto-verify immediately after agent completes
                if st.session_state.ai_researcher_result:
                    _auto_verify_researchers(
                        st.session_state.ai_researcher_result,
                        feedback=_task["feedback"],
                    )
            except Exception as e:
                logger.error(f"ResearcherFinderAgent failed: {e}")
                st.error(f"AI 연구자 추천 실패: {e}")
                import traceback
                st.code(traceback.format_exc())
            finally:
                st.session_state.agent_running = False
            st.rerun()

        # ── Results Display ───────────────────────────
        if st.session_state.ai_researcher_result:
            # Parse JSON from result
            if st.session_state.ai_researcher_parsed is None:
                _raw = st.session_state.ai_researcher_result
                try:
                    _parsed = json.loads(_raw)
                except (json.JSONDecodeError, TypeError):
                    # Try to extract JSON from markdown code block
                    import re
                    _m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", _raw, re.DOTALL)
                    if _m:
                        try:
                            _parsed = json.loads(_m.group(1))
                        except json.JSONDecodeError:
                            _parsed = None
                    else:
                        _parsed = None
                st.session_state.ai_researcher_parsed = _parsed

            parsed = st.session_state.ai_researcher_parsed

            if parsed:
                st.subheader("추천 결과")
                st.success(f"**{parsed.get('product_summary', '')}**")

                _analysis = parsed.get("analysis", "")
                if _analysis:
                    with st.expander("제품-연구 연결 분석", expanded=False):
                        st.markdown(_analysis)

                tier1 = parsed.get("tier1_researchers", [])
                tier2 = parsed.get("tier2_researchers", [])

                # Build verification + verdict lookups
                _r_vmap = {}
                if st.session_state.ai_researcher_verification:
                    for rv in st.session_state.ai_researcher_verification:
                        _r_vmap[rv.get("name", "")] = rv
                _r_verdict_map = st.session_state.get("ai_researcher_verdicts", {})

                _tier_tab = st.radio(
                    "결과 보기",
                    ["tier1", "tier2", "areas"],
                    format_func=lambda x: {
                        "tier1": f"Tier 1 ({len(tier1)}명)",
                        "tier2": f"Tier 2 ({len(tier2)}명)",
                        "areas": "연구 분야",
                    }[x],
                    horizontal=True,
                    label_visibility="collapsed",
                    key="researcher_tier_tab",
                )

                if _tier_tab == "tier1":
                    if tier1:
                        for idx, r in enumerate(tier1):
                            # Merge verification data into card
                            r_name = r.get("name", "")
                            r_with_v = {**r}
                            if r_name in _r_vmap:
                                r_with_v["verification"] = _r_vmap[r_name].get("verification", {})
                            col_card, col_actions = st.columns([5, 1])
                            with col_card:
                                _render_researcher_card(r_with_v, _r_verdict_map.get(r_name))
                            with col_actions:
                                st.write("")
                                if st.button("→ T2", key=f"r_t1to2_{idx}", help="Tier 2로 이동"):
                                    researcher = tier1.pop(idx)
                                    tier2.append(researcher)
                                    st.rerun()
                                if st.button("삭제", key=f"r_del_t1_{idx}", help="목록에서 제거"):
                                    tier1.pop(idx)
                                    st.rerun()
                    else:
                        st.info("Tier 1 연구자 없음")

                elif _tier_tab == "tier2":
                    if tier2:
                        for idx, r in enumerate(tier2):
                            r_name = r.get("name", "")
                            r_with_v = {**r}
                            if r_name in _r_vmap:
                                r_with_v["verification"] = _r_vmap[r_name].get("verification", {})
                            col_card, col_actions = st.columns([5, 1])
                            with col_card:
                                _render_researcher_card(r_with_v, _r_verdict_map.get(r_name))
                            with col_actions:
                                st.write("")
                                if st.button("→ T1", key=f"r_t2to1_{idx}", help="Tier 1으로 이동"):
                                    researcher = tier2.pop(idx)
                                    tier1.append(researcher)
                                    st.rerun()
                                if st.button("삭제", key=f"r_del_t2_{idx}", help="목록에서 제거"):
                                    tier2.pop(idx)
                                    st.rerun()
                    else:
                        st.info("Tier 2 연구자 없음")

                else:  # areas
                    areas = parsed.get("target_research_areas", [])
                    if areas:
                        st.markdown("**타겟 연구 분야:**")
                        for a in areas:
                            st.markdown(f"- {a}")
                    else:
                        st.info("연구 분야 정보 없음")

                # ── Verification Summary ──────────────
                if _r_verdict_map:
                    st.divider()
                    st.subheader("근거 교차검증 결과")
                    st.caption("외부 데이터(웹 + PubMed + ClinicalTrials) 수집 후 Claude가 AI 근거와 비교 분석")

                    total_v = len(_r_verdict_map)
                    confirmed = sum(1 for v in _r_verdict_map.values() if v.get("verdict") == "confirmed")
                    v_partial = sum(1 for v in _r_verdict_map.values() if v.get("verdict") == "partial")
                    unverified = sum(1 for v in _r_verdict_map.values() if v.get("verdict") == "unverified")
                    wrong = sum(1 for v in _r_verdict_map.values() if v.get("verdict") == "wrong")

                    vcol1, vcol2, vcol3, vcol4 = st.columns(4)
                    vcol1.metric("✅ 확인됨", confirmed)
                    vcol2.metric("⚠️ 일부 확인", v_partial)
                    vcol3.metric("❓ 미검증", unverified)
                    vcol4.metric("❌ 불일치", wrong)

                # ── Export Results as Markdown ─────────
                st.divider()
                st.subheader("결과 내보내기")

                def _build_researcher_export_md():
                    lines = [f"# 타겟 연구자 추천 결과\n"]
                    lines.append(f"**제품 요약:** {parsed.get('product_summary', '')}\n")
                    _exp_analysis = parsed.get("analysis", "")
                    if _exp_analysis:
                        lines.append(f"## 분석\n{_exp_analysis}\n")

                    for tier_label, tier_list in [
                        ("Tier 1 (핵심 타겟)", tier1),
                        ("Tier 2 (잠재적 타겟)", tier2),
                    ]:
                        lines.append(f"## {tier_label} — {len(tier_list)}명\n")
                        for i, r in enumerate(tier_list, 1):
                            r_name = r.get("name", "")
                            r_inst = r.get("institution", "")
                            r_dept = r.get("department", "")
                            r_title = r.get("title", "")
                            header = f"### {i}. {r_name}"
                            if r_title:
                                header += f" — {r_title}"
                            if r_inst:
                                header += f", {r_inst}"
                                if r_dept:
                                    header += f" ({r_dept})"
                            lines.append(header)
                            if r.get("research_area"):
                                lines.append(f"- **연구 분야:** {r['research_area']}")
                            if r.get("key_publications"):
                                lines.append(f"- **주요 연구:** {r['key_publications']}")
                            if r.get("reason"):
                                lines.append(f"- **요약:** {r['reason']}")
                            if r.get("evidence"):
                                lines.append(f"- **근거:** {r['evidence']}")
                            if r.get("tier_reason"):
                                lines.append(f"- **Tier 산정:** {r['tier_reason']}")
                            if r.get("contact_clues"):
                                lines.append(f"- **연락처 단서:** {r['contact_clues']}")
                            # Add verdict if available
                            _v = _r_verdict_map.get(r_name, {})
                            if _v:
                                _emoji = {"confirmed": "✅", "partial": "⚠️", "unverified": "❓", "wrong": "❌"}.get(_v.get("verdict", ""), "")
                                lines.append(f"- **교차검증:** {_emoji} {_v.get('verdict', '')} — {_v.get('explanation', '')}")
                            lines.append("")

                    _areas = parsed.get("target_research_areas", [])
                    if _areas:
                        lines.append("## 타겟 연구 분야\n")
                        for a in _areas:
                            lines.append(f"- {a}")
                        lines.append("")

                    return "\n".join(lines)

                _export_md = _build_researcher_export_md()
                st.download_button(
                    "📥 Markdown으로 내보내기",
                    data=_export_md,
                    file_name="target_researchers_result.md",
                    mime="text/markdown",
                    key="export_researcher_md",
                )

                # ── Feedback Section ──────────────────────
                st.divider()
                st.subheader("피드백 & 재추천")
                st.caption("피드백을 입력하면 기존 결과 + 피드백을 반영하여 재추천합니다. (타겟 피드백 DB 공유)")

                _r_feedback_text = st.text_area(
                    "피드백 입력",
                    placeholder="예: 일본 대학 위주로 추천해줘, CNS 임상시험 PI 위주로",
                    key="researcher_feedback_text",
                    height=80,
                )

                if st.button("🔄 피드백 반영 재추천", type="primary",
                             disabled=not _r_feedback_text or st.session_state.get("agent_running"),
                             key="researcher_re_recommend"):
                    # Build re-recommendation request with feedback
                    full_desc = researcher_product_desc or ""
                    if _r_campaign_ctx:
                        full_desc = f"{_r_campaign_ctx}\n\n{full_desc}"
                    if researcher_areas:
                        full_desc += f"\n\n타겟 연구 분야: {researcher_areas}"
                    region_line = f"\n지역 제한: {researcher_region}" if researcher_region else ""

                    prev_result = st.session_state.ai_researcher_result
                    agent_request = (
                        f"아래 제품에 적합한 학술 연구자/교수를 찾아줘.\n\n"
                        f"## 제품 설명\n{full_desc}{region_line}\n\n"
                        f"## 이전 추천 결과\n{prev_result}\n\n"
                        f"## 사용자 피드백 (반드시 반영)\n{_r_feedback_text}\n\n"
                        f"위 피드백을 반영하여 결과를 수정해줘."
                    )

                    # Save feedback to DB
                    db.add_target_feedback(
                        _r_feedback_text,
                        product_summary="연구자 추천 피드백",
                        profile_id=_run_profile_id,
                    )

                    st.session_state._pending_researcher_agent = {
                        "request": agent_request,
                        "feedback": db.get_combined_feedback_text(_run_profile_id),
                    }
                    st.session_state.agent_running = True
                    st.rerun()

                # ── Save as Preset ────────────────────────
                st.divider()
                st.subheader("프리셋으로 저장")
                st.caption("추천 결과를 프리셋으로 저장하면 '컨택 서칭' 페이지에서 바로 사용할 수 있습니다.")

                rec = parsed.get("recommended_search_params", {})
                tier1_names = [f"{r['name']} ({r.get('institution', '')})" for r in tier1]
                tier2_names = [f"{r['name']} ({r.get('institution', '')})" for r in tier2]
                all_institutions = list(set(
                    r.get("institution", "") for r in tier1 + tier2 if r.get("institution")
                ))
                all_areas = parsed.get("target_research_areas", [])

                save_scope = st.radio(
                    "저장할 범위",
                    ["Tier 1 + Tier 2 전체", "Tier 1만", "Tier 2만", "Tier 1 / Tier 2 각각 (2개 프리셋)"],
                    horizontal=True,
                    key="researcher_save_scope",
                )

                if save_scope == "Tier 1 / Tier 2 각각 (2개 프리셋)":
                    _save_groups = [("_T1", tier1_names), ("_T2", tier2_names)]
                elif save_scope == "Tier 2만":
                    _save_groups = [("", tier2_names)]
                elif save_scope == "Tier 1만":
                    _save_groups = [("", tier1_names)]
                else:
                    _save_groups = [("", tier1_names + tier2_names)]

                # Preview
                with st.expander("저장될 프리셋 내용 미리보기", expanded=False):
                    st.markdown(f"**연구 분야:** {', '.join(all_areas)}")
                    st.markdown(f"**기관:** {', '.join(all_institutions[:10])}")
                    st.markdown(f"**검색 키워드:** {rec.get('research_keywords', '')}")

                if save_scope == "Tier 1 / Tier 2 각각 (2개 프리셋)":
                    _nc1, _nc2 = st.columns(2)
                    with _nc1:
                        r_preset_name_t1 = st.text_input(
                            "Tier 1 프리셋 이름",
                            value=f"연구자_{datetime.now().strftime('%y%m%d')}_T1",
                            key="r_preset_name_t1",
                        )
                    with _nc2:
                        r_preset_name_t2 = st.text_input(
                            "Tier 2 프리셋 이름",
                            value=f"연구자_{datetime.now().strftime('%y%m%d')}_T2",
                            key="r_preset_name_t2",
                        )
                    _save_groups = [(r_preset_name_t1, tier1_names), (r_preset_name_t2, tier2_names)]
                    _r_can_save = bool(r_preset_name_t1 and r_preset_name_t2)
                else:
                    r_preset_name = st.text_input(
                        "프리셋 이름",
                        value=f"연구자_{datetime.now().strftime('%y%m%d')}",
                        key="r_preset_name",
                    )
                    _save_groups = [(r_preset_name, c) for _, c in _save_groups]
                    _r_can_save = bool(r_preset_name)

                if st.button("💾 프리셋 저장 → 컨택 서칭", type="primary",
                             disabled=not _r_can_save, key="save_researcher_preset"):
                    for _name, _researchers in _save_groups:
                        if not _researchers:
                            continue
                        db.save_preset(
                            name=_name,
                            industry=", ".join(all_areas),
                            titles="Professor, Associate Professor, PI, Lab Director",
                            locations=researcher_region or "",
                            companies=", ".join(_researchers),
                            keywords=rec.get("research_keywords", ""),
                            max_results=100,
                            feedback_hash=_get_feedback_hash(),
                            product_description=researcher_product_desc or "",
                            target_hint=researcher_areas or "",
                            target_region=researcher_region or "",
                            preset_type="researcher",
                            institutions=", ".join(all_institutions),
                            research_areas=", ".join(all_areas),
                        )
                    _saved = ", ".join(f"'{n}'" for n, r in _save_groups if r)
                    st.session_state.ai_researcher_result = None
                    st.session_state.ai_researcher_parsed = None
                    st.session_state.ai_researcher_verification = None
                    st.session_state.ai_researcher_verdicts = {}
                    st.session_state.active_page = "🔍 컨택 서칭"
                    st.session_state.contact_search_mode = "manual"
                    st.session_state.prospect_step = "search"
                    st.success(f"프리셋 {_saved} 저장 완료! 컨택 서칭으로 이동합니다.")
                    st.rerun()

            else:
                # JSON parsing failed — show raw result
                st.warning("AI 결과를 JSON으로 파싱하지 못했습니다. 원본 텍스트:")
                st.text(st.session_state.ai_researcher_result[:3000])

            # ── Reset button ──────────────────────────
            st.divider()
            if st.button("🗑️ 결과 초기화", key="reset_researcher"):
                st.session_state.ai_researcher_result = None
                st.session_state.ai_researcher_parsed = None
                st.session_state.ai_researcher_verification = None
                st.session_state.ai_researcher_verdicts = {}
                st.rerun()

# ══════════════════════════════════════════════════════════
# PAGE 1: Contact Search (컨택 서칭)
# ══════════════════════════════════════════════════════════

elif page == "🔍 컨택 서칭":
    st.title("컨택 서칭")

    # ── Mode selector ─────────────────────────────────
    if "contact_search_mode" not in st.session_state:
        st.session_state.contact_search_mode = "agent"
    if "agent2_log" not in st.session_state:
        st.session_state.agent2_log = []
    if "agent2_result" not in st.session_state:
        st.session_state.agent2_result = None
    if "agent2_credits" not in st.session_state:
        st.session_state.agent2_credits = None
    if "agent2_search_id" not in st.session_state:
        st.session_state.agent2_search_id = None
    if "prospect_step" not in st.session_state:
        st.session_state.prospect_step = "search"
    if "prospect_search_id" not in st.session_state:
        st.session_state.prospect_search_id = None

    search_mode = st.radio(
        "검색 모드",
        ["🤖 Agent 모드 (자동)", "🔧 수동 모드 (6단계)"],
        horizontal=True,
        index=0 if st.session_state.contact_search_mode == "agent" else 1,
    )
    st.session_state.contact_search_mode = "agent" if "Agent" in search_mode else "manual"
    st.divider()

    # ══════════════════════════════════════════════════
    # AGENT MODE
    # ══════════════════════════════════════════════════
    if st.session_state.contact_search_mode == "agent":
        st.caption("AI Agent가 Findymail, Hunter.io, WHOIS, 웹 검색을 자동으로 조합하여 이메일을 찾습니다.")

        # ── Input section with tabs ───────────────────
        st.subheader("타겟 정보 입력")
        input_tab1, input_tab2, input_tab3 = st.tabs(["✏️ 직접 입력", "🎯 Agent 1 결과에서", "📋 저장된 프리셋에서"])

        agent2_request = ""

        with input_tab1:
            a2_companies = st.text_area(
                "회사 목록 (줄바꿈 구분)",
                placeholder="Eisai\nShionogi\nDaiichi Sankyo",
                height=120,
                key="a2_companies_input",
            )
            a2c1, a2c2 = st.columns(2)
            with a2c1:
                a2_titles = st.text_input(
                    "타겟 직함 (콤마 구분)",
                    placeholder="VP BD, Director Licensing, Head of Research",
                    key="a2_titles_input",
                )
            with a2c2:
                a2_region = st.text_input(
                    "지역 (선택)",
                    placeholder="Japan, US, etc.",
                    key="a2_region_input",
                )

            if a2_companies.strip():
                companies = [c.strip() for c in a2_companies.strip().split("\n") if c.strip()]
                parts = [f"다음 {len(companies)}개 회사에서 이메일을 찾아줘 (전부 빠짐없이 처리할 것): {', '.join(companies)}"]
                if a2_titles.strip():
                    parts.append(f"타겟 직함: {a2_titles}")
                if a2_region.strip():
                    parts.append(f"지역: {a2_region}")
                agent2_request = "\n".join(parts)

        with input_tab2:
            if st.session_state.get("ai_target_parsed"):
                parsed = st.session_state.ai_target_parsed
                tier1 = parsed.get("tier1_companies", [])
                tier2 = parsed.get("tier2_companies", [])
                dm_titles = parsed.get("decision_makers", [])

                st.success(f"Agent 1 결과: Tier 1 {len(tier1)}개, Tier 2 {len(tier2)}개 회사")

                use_tier1 = st.checkbox(f"Tier 1 사용 ({len(tier1)}개)", value=True, key="a2_use_tier1")
                use_tier2 = st.checkbox(f"Tier 2 사용 ({len(tier2)}개)", value=False, key="a2_use_tier2")

                if dm_titles:
                    st.caption(f"추천 직함: {', '.join(dm_titles[:8])}")

                selected_companies = []
                if use_tier1:
                    selected_companies.extend([c["name"] for c in tier1])
                if use_tier2:
                    selected_companies.extend([c["name"] for c in tier2])

                if selected_companies:
                    tier_label = []
                    if use_tier1:
                        tier_label.append(f"Tier 1 {len(tier1)}개")
                    if use_tier2:
                        tier_label.append(f"Tier 2 {len(tier2)}개")
                    parts = [
                        f"다음 {len(selected_companies)}개 회사에서 이메일을 찾아줘 ({', '.join(tier_label)}, 전부 빠짐없이 처리할 것):",
                        ", ".join(selected_companies),
                    ]
                    if dm_titles:
                        parts.append(f"타겟 직함: {', '.join(dm_titles[:5])}")
                    agent2_request = "\n".join(parts)
            else:
                st.info("Agent 1 (타겟 발굴) 결과가 없습니다. 먼저 타겟 발굴을 실행하거나 '직접 입력' 탭을 사용하세요.")

        with input_tab3:
            saved_presets = db.get_presets()
            if saved_presets:
                current_fb_hash = _get_feedback_hash()
                _ptype_icon = lambda sp: "🎓" if sp.get("preset_type") == "researcher" else "🏢"
                preset_names = [f"{_ptype_icon(sp)} {sp['name']}" for sp in saved_presets]
                selected_preset_label = st.selectbox(
                    "프리셋 선택", preset_names, key="a2_preset_select"
                )
                _sel_idx = preset_names.index(selected_preset_label)
                sel = saved_presets[_sel_idx]

                # Warn if feedback changed since preset was saved
                if sel.get("feedback_hash") and sel["feedback_hash"] != current_fb_hash:
                    st.warning(
                        "이 프리셋 저장 이후 피드백이 변경되었습니다. "
                        "'🎯 타겟 발굴'에서 다시 추천받아 프리셋을 갱신하는 것을 권장합니다."
                    )

                # Show preset summary
                _is_researcher_preset = sel.get("preset_type") == "researcher"
                info_parts = []
                if sel.get("companies"):
                    _label = "연구자" if _is_researcher_preset else "회사"
                    info_parts.append(f"**{_label}**: {sel['companies']}")
                if sel.get("industry"):
                    _label = "연구 분야" if _is_researcher_preset else "산업"
                    info_parts.append(f"**{_label}**: {sel['industry']}")
                if sel.get("institutions") and _is_researcher_preset:
                    info_parts.append(f"**기관**: {sel['institutions']}")
                if sel.get("titles"):
                    info_parts.append(f"**직함**: {sel['titles']}")
                if sel.get("locations"):
                    info_parts.append(f"**지역**: {sel['locations']}")
                if sel.get("keywords"):
                    info_parts.append(f"**키워드**: {sel['keywords']}")
                if info_parts:
                    st.markdown(" | ".join(info_parts))

                # Build agent request from preset
                companies_str = sel.get("companies") or ""
                if companies_str.strip():
                    companies_list = [c.strip() for c in companies_str.split(",") if c.strip()]
                    if _is_researcher_preset:
                        parts = [f"다음 {len(companies_list)}명의 연구자 이메일을 찾아줘 (전부 빠짐없이 처리할 것): {', '.join(companies_list)}"]
                    else:
                        parts = [f"다음 {len(companies_list)}개 회사에서 이메일을 찾아줘 (전부 빠짐없이 처리할 것): {', '.join(companies_list)}"]
                    if sel.get("titles"):
                        parts.append(f"타겟 직함: {sel['titles']}")
                    if sel.get("locations"):
                        parts.append(f"지역: {sel['locations']}")
                    if sel.get("keywords"):
                        parts.append(f"키워드: {sel['keywords']}")
                    if sel.get("industry"):
                        _label = "연구 분야" if _is_researcher_preset else "산업"
                        parts.append(f"{_label}: {sel['industry']}")
                    if _is_researcher_preset:
                        if sel.get("institutions"):
                            parts.append(f"참고 기관: {sel['institutions']}")
                        parts.append("이 사람들은 학술 연구자입니다. 대학/연구기관 도메인에서 이메일을 찾아주세요.")
                    agent2_request = "\n".join(parts)
                else:
                    st.warning("이 프리셋에 대상 목록이 없습니다. 대상이 포함된 프리셋을 선택하거나 '직접 입력' 탭을 사용하세요.")
            else:
                st.info("저장된 프리셋이 없습니다. '🎯 타겟 발굴' 페이지에서 AI 추천 → 프리셋 저장을 먼저 해주세요.")

        # ── Run Agent button ──────────────────────────
        st.divider()
        if st.button("🤖 이메일 찾기 Agent 실행", type="primary", disabled=not agent2_request or st.session_state.get("agent_running")):
            # Phase 1: save params and rerun to show overlay
            st.session_state._pending_agent2 = {"request": agent2_request}
            st.session_state.agent_running = True
            st.rerun()

        # Phase 2: execute pending Agent 2 task (overlay already visible)
        if st.session_state.get("_pending_agent2"):
            _task = st.session_state.pop("_pending_agent2")
            try:
                from agent import EmailFinderAgent

                _a2_request = _task["request"]
                # Count companies from request
                _a2_lines = _a2_request.split("\n")
                _a2_company_count = 1
                for _line in _a2_lines:
                    _commas = _line.count(",")
                    if _commas >= 2:
                        _a2_company_count = max(_a2_company_count, _commas + 1)
                _a2_company_count = max(_a2_company_count, 1)

                tracker = AgentProgressTracker("agent2", total_items=_a2_company_count)

                agent = EmailFinderAgent(
                    num_companies=_a2_company_count,
                    on_tool_call=tracker.on_tool_call,
                    on_tool_result=tracker.on_tool_result,
                    on_text=tracker.on_text,
                )

                agent_output = agent.run(_a2_request)

                # Finalize: mark DB search as completed
                if agent._search_id and agent._accumulated_contacts:
                    import db as _db
                    _db.update_prospect_search(
                        agent._search_id,
                        status="completed",
                        total_found=len(agent._accumulated_contacts),
                        completed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    )

                st.session_state.agent2_log = tracker.tool_log
                st.session_state.agent2_credits = agent.credits_used
                st.session_state.agent2_search_id = agent._search_id

                if agent.result_json:
                    try:
                        result = json.loads(agent.result_json)
                        st.session_state.agent2_result = result
                        tracker.complete(f"{len(result.get('contacts', []))}명 연락처 발견")
                    except json.JSONDecodeError:
                        st.session_state.agent2_result = None
                        tracker.fail("Agent 결과 JSON 파싱 실패")
                else:
                    tracker.complete("이메일 검색 완료")
            except Exception as e:
                st.error(f"이메일 찾기 실패: {e}")
                import traceback
                st.code(traceback.format_exc())
            finally:
                st.session_state.agent_running = False
            st.rerun()

        # ── Display results ───────────────────────────
        if st.session_state.agent2_result:
            result = st.session_state.agent2_result
            contacts_raw = result.get("contacts", [])
            summary = result.get("search_summary", {})

            # Deduplicate by (email, company) — matches DB UNIQUE constraint
            seen = set()
            contacts = []
            for c in contacts_raw:
                email = (c.get("email") or "").strip().lower()
                company = (c.get("company") or "").strip().lower()
                name = (c.get("contact_name") or "").strip().lower()
                key = (email, company) if email else (name, company)
                if key not in seen:
                    seen.add(key)
                    contacts.append(c)

            dupes_removed = len(contacts_raw) - len(contacts)
            msg = f"✅ {len(contacts)}명의 연락처 발견"
            if dupes_removed > 0:
                msg += f" (중복 {dupes_removed}건 제거)"
            st.success(msg)

            # Metrics
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("총 연락처", len(contacts))
            m2.metric("이메일 확보", sum(1 for c in contacts if c.get("email")))
            m3.metric("회사 수", len(set(c.get("company", "") for c in contacts if c.get("company"))))
            credits = st.session_state.agent2_credits or {}
            m4.metric("크레딧 사용", f"F:{credits.get('findymail', 0)} H:{credits.get('hunter', 0)}")

            # Contacts table
            if contacts:
                import pandas as pd
                df = pd.DataFrame(contacts)
                display_cols = [c for c in ["contact_name", "email", "email_confidence", "company", "title", "source", "location"] if c in df.columns]
                st.dataframe(df[display_cols], use_container_width=True, height=400)

                # Export
                st.divider()
                exp1, exp2 = st.columns(2)
                with exp1:
                    csv_data = df[display_cols].to_csv(index=False)
                    st.download_button(
                        "📥 CSV 다운로드",
                        csv_data,
                        f"contacts_{time.strftime('%y%m%d')}.csv",
                        "text/csv",
                    )
                with exp2:
                    if st.button("📧 콜드메일 캠페인으로 보내기"):
                        st.session_state.csv_data = csv_data
                        st.session_state.a3_from_agent2 = st.session_state.get("agent2_search_id")
                        st.session_state.active_page = "📝 콜드메일"
                        st.rerun()

        # Agent activity log (full)
        if st.session_state.agent2_log:
            full_log = "\n".join(st.session_state.agent2_log)
            with st.expander(f"Agent 활동 로그 ({len(st.session_state.agent2_log)}건)", expanded=False):
                st.code(full_log, language=None)
                st.download_button(
                    "📥 로그 다운로드",
                    full_log,
                    f"agent2_log_{time.strftime('%y%m%d_%H%M')}.txt",
                    "text/plain",
                    key="a2_log_download",
                )

    # ══════════════════════════════════════════════════
    # MANUAL MODE (existing 6-step pipeline)
    # ══════════════════════════════════════════════════
    else:
        st.caption("Findymail + Hunter.io로 잠재 고객의 이메일을 찾고, AI로 적합도를 평가합니다.")

        # ── Step indicator ────────────────────────────────
        p_steps = ["① 검색", "② 결과", "③ 이메일", "④ 리서치", "⑤ AI 평가", "⑥ 내보내기"]
        p_step_map = {"search": 0, "results": 1, "hunter": 2, "research": 3, "enrich": 4, "export": 5}
        p_current = p_step_map.get(st.session_state.prospect_step, 0)

        pcols = st.columns(6)
        for i, (col, label) in enumerate(zip(pcols, p_steps)):
            if i < p_current:
                col.success(label)
            elif i == p_current:
                col.info(label)
            else:
                col.markdown(f"<span style='color:gray'>{label}</span>", unsafe_allow_html=True)
        st.divider()

    # ── Manual mode step logic (skipped in Agent mode) ──
    if st.session_state.contact_search_mode != "manual":
        pass  # Agent mode UI handled above

    elif st.session_state.prospect_step == "search":
        st.subheader("① 검색 조건 설정")

        # Load saved presets from DB
        saved_presets = db.get_presets()
        SAVED_PRESETS = {}
        for sp in saved_presets:
            _icon = "🎓" if sp.get("preset_type") == "researcher" else "🏢"
            _display_name = f"{_icon} {sp['name']}"
            SAVED_PRESETS[_display_name] = {
                "id": sp["id"],
                "industry": sp.get("industry") or "",
                "titles": sp.get("titles") or "",
                "locations": sp.get("locations") or "",
                "companies": sp.get("companies") or "",
                "keywords": sp.get("keywords") or "",
                "max_results": sp.get("max_results") or 100,
            }

        ALL_PRESETS = {"직접 설정": {}}
        ALL_PRESETS.update(SAVED_PRESETS)

        if not SAVED_PRESETS:
            st.info("프리셋이 없습니다. '🎯 타겟 발굴' 페이지에서 AI 추천 → 프리셋 저장을 먼저 해보세요.")

        preset_col, save_col = st.columns([3, 1])
        with preset_col:
            preset = st.selectbox("프리셋", list(ALL_PRESETS.keys()))
        preset_vals = ALL_PRESETS.get(preset, {})

        # Delete button for saved presets
        with save_col:
            st.markdown("<br>", unsafe_allow_html=True)
            if preset in SAVED_PRESETS:
                if st.button("🗑️ 삭제", key="delete_preset"):
                    db.delete_preset(SAVED_PRESETS[preset]["id"])
                    st.rerun()

        col1, col2 = st.columns(2)
        with col1:
            p_industry = st.text_input(
                "산업 (Industry)",
                value=preset_vals.get("industry", ""),
                placeholder="pharmaceutical, biotech, IT, etc.",
            )
            p_titles = st.text_input(
                "직함 키워드 (콤마 구분)",
                value=preset_vals.get("titles", ""),
                placeholder="Director, VP, Head, Manager",
            )
            p_keywords = st.text_input(
                "자유 검색어",
                value=preset_vals.get("keywords", ""),
                placeholder="CNS, neuroscience, BD, licensing",
            )

        with col2:
            p_locations = st.text_input(
                "지역 (콤마 구분)",
                value=preset_vals.get("locations", ""),
                placeholder="Japan, Tokyo, US, etc.",
            )
            p_companies = st.text_input(
                "특정 회사 (콤마 구분, 선택)",
                value=preset_vals.get("companies", ""),
                placeholder="Shionogi, Eisai, Daiichi Sankyo",
            )
            p_keyword_filter = st.checkbox(
                "회사 지정 시에도 키워드 필터 적용",
                value=False,
                help="회사를 직접 지정하면 이미 관련 회사만 검색합니다. "
                     "키워드 필터까지 적용하면 직함에 키워드가 없는 사람이 제외되어 결과가 매우 적어질 수 있습니다.",
            )
            p_max_results = st.slider(
                "최대 검색 수", 10, 500,
                value=preset_vals.get("max_results", 100),
                step=10,
            )

        # Save preset
        save_col1, save_col2 = st.columns([3, 1])
        with save_col1:
            new_preset_name = st.text_input(
                "프리셋 저장",
                placeholder="프리셋 이름을 입력하면 현재 설정이 저장됩니다",
                label_visibility="collapsed",
            )
        with save_col2:
            if st.button("💾 프리셋 저장", disabled=not new_preset_name):
                db.save_preset(
                    name=new_preset_name,
                    industry=p_industry,
                    titles=p_titles,
                    locations=p_locations,
                    companies=p_companies,
                    keywords=p_keywords,
                    max_results=p_max_results,
                    feedback_hash=_get_feedback_hash(),
                )
                st.success(f"프리셋 '{new_preset_name}' 저장 완료!")
                st.rerun()

        st.divider()

        p_search_name = st.text_input(
            "검색 이름",
            value=f"Search_{datetime.now().strftime('%y%m%d')}",
        )

        # API key check
        if not FINDYMAIL_API_KEY:
            st.error("Findymail API 키가 설정되지 않았습니다. .env에 FINDYMAIL_API_KEY를 추가해주세요.")

        # Search button
        if st.button("🔍 이메일 검색 시작", type="primary", disabled=not FINDYMAIL_API_KEY):
            from findymail_client import FindymailClient

            titles_list = [t.strip() for t in p_titles.split(",") if t.strip()] if p_titles else None
            companies_list = [c.strip() for c in p_companies.split(",") if c.strip()] if p_companies else None

            search_params_json = json.dumps({
                "industry": p_industry or None,
                "titles": titles_list,
                "companies": companies_list,
            }, ensure_ascii=False)

            search_id = db.create_prospect_search(
                name=p_search_name,
                search_params=search_params_json,
                source="findymail",
            )
            db.update_prospect_search(search_id, status="searching")
            st.session_state.prospect_search_id = search_id

            fm = FindymailClient()
            total_found = 0

            try:
                if not companies_list:
                    st.error("회사를 최소 1개 입력해주세요.")
                    st.stop()

                # Build prospect list for Findymail batch search
                prospects_to_search = []
                for company in companies_list:
                    # Infer domain from known domains or company name
                    from hunter_client import _KNOWN_DOMAINS
                    domain = ""
                    for key, dom in _KNOWN_DOMAINS.items():
                        if key in company.lower():
                            domain = dom
                            break
                    if not domain:
                        # Try company name as domain
                        domain = company.lower().replace(" ", "") + ".com"

                    if titles_list:
                        for title in titles_list:
                            prospects_to_search.append({
                                "company": company,
                                "domain": domain,
                                "title_keyword": title,
                            })
                    else:
                        prospects_to_search.append({
                            "company": company,
                            "domain": domain,
                        })

                progress = st.progress(0, text="Findymail로 이메일 검색 중...")
                total = len(prospects_to_search)

                for i, prospect in enumerate(prospects_to_search):
                    company = prospect["company"]
                    domain = prospect["domain"]
                    pct = min((i + 1) / max(total, 1), 0.95)
                    progress.progress(pct, text=f"검색 중: {company} ({i+1}/{total})")

                    try:
                        # Use Hunter domain search to find people at this company
                        if HUNTER_API_KEY:
                            from hunter_client import HunterClient
                            hunter = HunterClient()
                            domain_result = hunter.search_domain(domain, limit=5)
                            emails_data = domain_result.get("data", {}).get("emails", [])

                            for person in emails_data:
                                email = person.get("value", "")
                                name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
                                title = person.get("position", "")

                                if email and name:
                                    # Verify with Findymail for higher accuracy
                                    try:
                                        fm_result = fm.find_email(name, domain)
                                        verified_email = fm_result.get("email", email)
                                        is_verified = fm_result.get("verified", False)
                                    except Exception:
                                        verified_email = email
                                        is_verified = False

                                    db.add_prospect(
                                        search_id=search_id,
                                        contact_name=name,
                                        email=verified_email,
                                        company=company,
                                        title=title,
                                        email_confidence="verified" if is_verified else "high",
                                        source="findymail+hunter",
                                        source_data=json.dumps(person, ensure_ascii=False),
                                    )
                                    total_found += 1

                    except Exception as e:
                        logger.warning(f"Search failed for {company}: {e}")

                    db.update_prospect_search(search_id, total_found=total_found)

                progress.progress(1.0, text=f"완료! {total_found}명 발견")
                db.update_prospect_search(search_id, status="completed",
                                          completed_at=datetime.now().isoformat())
                st.session_state.prospect_step = "results"
                st.rerun()

            except Exception as e:
                db.update_prospect_search(search_id, status="completed",
                                          total_found=total_found,
                                          completed_at=datetime.now().isoformat())
                if total_found > 0:
                    st.warning(f"검색이 중단되었습니다. {total_found}명 저장됨. 이전 검색 기록에서 열 수 있습니다.")
                else:
                    st.error(f"검색 실패: {e}")
                logger.error(f"Findymail prospect search failed: {e}")

        # Previous searches
        st.divider()
        st.subheader("이전 검색 기록")
        # Session state for delete confirmation
        if "confirm_delete_search" not in st.session_state:
            st.session_state.confirm_delete_search = None

        prev_searches = db.get_prospect_searches()
        if prev_searches:
            for s in prev_searches[:10]:
                sid = s["id"]
                is_confirming = st.session_state.confirm_delete_search == sid

                if is_confirming:
                    # Confirmation row
                    st.warning(f"**{s['name']}** ({s['total_found']}명) 을 삭제하시겠습니까?")
                    ccol1, ccol2 = st.columns(2)
                    if ccol1.button("삭제 확인", key=f"confirm_del_{sid}", type="primary"):
                        db.delete_prospect_search(sid)
                        st.session_state.confirm_delete_search = None
                        st.rerun()
                    if ccol2.button("취소", key=f"cancel_del_{sid}"):
                        st.session_state.confirm_delete_search = None
                        st.rerun()
                else:
                    scol1, scol2, scol3, scol4 = st.columns([3, 1, 1, 1])
                    scol1.write(f"**{s['name']}** ({(s.get('created_at') or '')[:16]})")
                    scol2.write(f"{s['total_found']}명")
                    if scol3.button("열기", key=f"open_search_{sid}"):
                        st.session_state.prospect_search_id = sid
                        st.session_state.prospect_step = "results"
                        st.rerun()
                    if scol4.button("🗑️", key=f"del_search_{sid}"):
                        st.session_state.confirm_delete_search = sid
                        st.rerun()
        else:
            st.info("검색 기록이 없습니다.")

    # ── STEP 2: Search Results ────────────────────────
    elif st.session_state.prospect_step == "results":
        st.subheader("② 검색 결과")
        search_id = st.session_state.prospect_search_id

        if not search_id:
            st.warning("검색 결과가 없습니다.")
        else:
            search_info = db.get_prospect_search(search_id)
            prospects = db.get_prospects(search_id=search_id)

            if search_info:
                st.caption(f"검색: {search_info['name']} | 총 {len(prospects)}명 발견")

            if prospects:
                import pandas as pd

                email_count = sum(1 for p in prospects if p.get("email"))
                no_email_count = len(prospects) - email_count
                m1, m2, m3 = st.columns(3)
                m1.metric("총 인원", len(prospects))
                m2.metric("이메일 있음", email_count)
                m3.metric("이메일 없음", no_email_count)

                df = pd.DataFrame(prospects)
                display_cols = ["contact_name", "company", "title", "email", "linkedin_url", "location"]
                display_cols = [c for c in display_cols if c in df.columns]
                st.dataframe(df[display_cols], width="stretch", hide_index=True)
            else:
                st.info("검색 결과가 없습니다. 검색 조건을 조정해보세요.")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("⬅ 검색 조건으로"):
                st.session_state.prospect_step = "search"
                st.rerun()
        with col2:
            if st.button("➡ 이메일 찾기", type="primary", disabled=not prospects if search_id else True):
                st.session_state.prospect_step = "hunter"
                st.rerun()

    # ── STEP 3: Hunter.io Email Lookup ─────────────────
    elif st.session_state.prospect_step == "hunter":
        st.subheader("③ 이메일 찾기 (Hunter.io)")
        search_id = st.session_state.prospect_search_id
        prospects = db.get_prospects(search_id=search_id) if search_id else []

        has_email = sum(1 for p in prospects if p.get("email"))
        missing_email = len(prospects) - has_email
        m1, m2 = st.columns(2)
        m1.metric("이메일 있음", has_email)
        m2.metric("이메일 없음", missing_email)

        if not HUNTER_API_KEY:
            st.warning("Hunter.io API 키가 설정되지 않았습니다. .env에 HUNTER_API_KEY를 추가하거나 이 단계를 건너뛸 수 있습니다.")

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ 검색 결과로"):
                st.session_state.prospect_step = "results"
                st.rerun()
        with col2:
            if st.button("🔍 Hunter.io 이메일 찾기", type="primary",
                         disabled=not HUNTER_API_KEY or missing_email == 0):
                with st.spinner(f"Hunter.io에서 {missing_email}명의 이메일 검색 중..."):
                    try:
                        from hunter_client import HunterClient
                        hunter = HunterClient()
                        missing_prospects = db.get_prospects_missing_email(search_id)
                        results = hunter.batch_find_emails(missing_prospects,
                                                          all_prospects=prospects)
                        for hr in results:
                            db.update_prospect(hr["prospect_id"],
                                email=hr["email"],
                                email_confidence=hr["confidence"],
                                hunter_email=hr["email"],
                                hunter_confidence=hr.get("hunter_score", 0),
                                source="findymail+hunter",
                            )
                        st.success(f"Hunter.io: {len(results)}개 이메일 발견!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Hunter.io 실패: {e}")
        with col3:
            if st.button("⏭ 건너뛰기 → 리서치"):
                st.session_state.prospect_step = "research"
                st.rerun()

        # Show updated prospect table
        prospects = db.get_prospects(search_id=search_id) if search_id else []
        if prospects:
            import pandas as pd
            df = pd.DataFrame(prospects)
            display_cols = ["contact_name", "company", "title", "email", "email_confidence", "source"]
            display_cols = [c for c in display_cols if c in df.columns]
            st.dataframe(df[display_cols], width="stretch", hide_index=True)

        if st.button("➡ 업계 리서치로", type="primary"):
            st.session_state.prospect_step = "research"
            st.rerun()

    # ── STEP 4: Industry Research ──────────────────────
    elif st.session_state.prospect_step == "research":
        st.subheader("④ 업계 리서치 (ClinicalTrials + PubMed)")
        search_id = st.session_state.prospect_search_id
        prospects = db.get_prospects(search_id=search_id) if search_id else []

        has_research = sum(1 for p in prospects if p.get("research_context"))

        if has_research > 0:
            st.success(f"{has_research}명에 대한 리서치 데이터 수집 완료")
        else:
            therapeutic_area = st.text_input(
                "치료 영역 (PubMed 검색 키워드, 선택)",
                placeholder="CNS, oncology, immunology 등",
            )

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ 이메일 찾기로"):
                st.session_state.prospect_step = "hunter"
                st.rerun()
        with col2:
            if has_research == 0:
                if st.button("🔬 리서치 실행", type="primary"):
                    with st.spinner("ClinicalTrials.gov + PubMed 검색 중..."):
                        try:
                            from research_client import ResearchClient
                            research = ResearchClient()
                            unique_companies = list(set(
                                p["company"] for p in prospects if p.get("company")
                            ))
                            for company in unique_companies[:20]:
                                ctx = research.get_company_research_context(
                                    company=company,
                                    therapeutic_area=therapeutic_area if 'therapeutic_area' in dir() else None,
                                )
                                for p in prospects:
                                    if p.get("company", "").lower() == company.lower():
                                        db.update_prospect(p["id"],
                                            research_context=json.dumps(ctx, ensure_ascii=False, default=str)
                                        )
                            st.success("리서치 데이터 수집 완료!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"리서치 실패: {e}")
        with col3:
            if st.button("⏭ 건너뛰기 → AI 평가"):
                st.session_state.prospect_step = "enrich"
                st.rerun()

        # Show research summaries per company
        prospects = db.get_prospects(search_id=search_id) if search_id else []
        shown_companies = set()
        for p in prospects:
            if p.get("research_context") and p["company"] not in shown_companies:
                shown_companies.add(p["company"])
                ctx = json.loads(p["research_context"])
                with st.expander(f"📊 {p['company']}", expanded=False):
                    st.markdown(ctx.get("summary", ""))
                    if ctx.get("active_trials"):
                        st.caption(f"Active trials: {len(ctx['active_trials'])}")
                    if ctx.get("recent_publications"):
                        st.caption(f"Recent publications: {len(ctx['recent_publications'])}")

        if st.button("➡ AI 인리치먼트로", type="primary", key="research_next"):
            st.session_state.prospect_step = "enrich"
            st.rerun()

    # ── STEP 5: AI Enrichment ─────────────────────────
    elif st.session_state.prospect_step == "enrich":
        st.subheader("⑤ AI 인리치먼트")
        search_id = st.session_state.prospect_search_id
        search_info = db.get_prospect_search(search_id) if search_id else None
        prospects = db.get_prospects(search_id=search_id) if search_id else []

        # Check if already enriched
        enriched_count = sum(1 for p in prospects if p.get("status") == "enriched")
        if enriched_count > 0:
            st.success(f"{enriched_count}/{len(prospects)}명 인리치먼트 완료")
        else:
            with st.spinner("Claude가 이메일 추론 + 적합도 평가 중... (1~2분 소요)"):
                try:
                    from claude_client import ClaudeClient
                    claude = ClaudeClient()

                    search_params = json.loads(search_info["search_params"]) if search_info else {}

                    existing_emails = [
                        {"email": p["email"], "company": p["company"]}
                        for p in prospects if p.get("email")
                    ]

                    # Build research_context from prospect data
                    research_context_data = []
                    seen_companies = set()
                    for p in prospects:
                        if p.get("research_context") and p["company"] not in seen_companies:
                            seen_companies.add(p["company"])
                            research_context_data.append(json.loads(p["research_context"]))

                    enriched_text = claude.enrich_prospects(
                        prospects_json=json.dumps(
                            [{"name": p["contact_name"], "email": p["email"],
                              "company": p["company"], "title": p["title"],
                              "linkedin": p.get("linkedin_url", ""),
                              "location": p.get("location", "")}
                             for p in prospects],
                            ensure_ascii=False,
                        ),
                        search_criteria=search_params,
                        existing_emails_for_pattern=existing_emails,
                        research_context=research_context_data if research_context_data else None,
                    )

                    # Apply enrichment
                    from main import _apply_enrichment
                    _apply_enrichment(search_id, enriched_text)
                    db.update_prospect_search(search_id, total_enriched=len(prospects))
                    st.rerun()
                except Exception as e:
                    st.error(f"인리치먼트 실패: {e}")
                    logger.error(f"Enrichment failed: {e}")

        # Show enriched results
        prospects = db.get_prospects(search_id=search_id) if search_id else []
        if prospects:
            import pandas as pd

            df = pd.DataFrame(prospects)
            display_cols = ["contact_name", "company", "title", "email", "email_confidence",
                            "location"]
            display_cols = [c for c in display_cols if c in df.columns]
            st.dataframe(
                df[display_cols],
                width="stretch",
                hide_index=True,
            )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("⬅ 리서치로"):
                st.session_state.prospect_step = "research"
                st.rerun()
        with col2:
            if st.button("➡ 내보내기", type="primary"):
                st.session_state.prospect_step = "export"
                st.rerun()

    # ── STEP 6: Export ────────────────────────────────
    elif st.session_state.prospect_step == "export":
        st.subheader("⑥ 내보내기")
        search_id = st.session_state.prospect_search_id
        search_info = db.get_prospect_search(search_id) if search_id else None
        search_name = (search_info["name"] if search_info else "prospects").strip().replace(" ", "_")

        # Email verification section
        if HUNTER_API_KEY:
            unverified = db.get_unverified_prospects(search_id) if search_id else []
            if unverified:
                st.warning(f"{len(unverified)}개의 이메일이 아직 검증되지 않았습니다.")
                if st.button("✅ 이메일 검증 실행 (Hunter.io)"):
                    with st.spinner("이메일 검증 중..."):
                        try:
                            from hunter_client import HunterClient
                            hunter = HunterClient()
                            emails = [p["email"] for p in unverified if p.get("email")]
                            results = hunter.batch_verify_emails(emails)
                            for p in unverified:
                                if p["email"] in results:
                                    vr = results[p["email"]]
                                    db.update_prospect(p["id"],
                                        verification_status=vr["status"],
                                        verification_score=vr.get("score", 0),
                                    )
                                    db.add_email_verification(
                                        prospect_id=p["id"],
                                        email=p["email"],
                                        status=vr["status"],
                                        score=vr.get("score", 0),
                                    )
                            st.rerun()
                        except Exception as e:
                            st.error(f"이메일 검증 실패: {e}")
            else:
                # Show verification summary
                all_p = db.get_prospects(search_id=search_id) if search_id else []
                v_counts: dict = {}
                for p in all_p:
                    vs = p.get("verification_status") or "pending"
                    v_counts[vs] = v_counts.get(vs, 0) + 1
                if any(k != "pending" for k in v_counts):
                    vcols = st.columns(4)
                    vcols[0].metric("Deliverable", v_counts.get("deliverable", 0))
                    vcols[1].metric("Risky", v_counts.get("risky", 0))
                    vcols[2].metric("Undeliverable", v_counts.get("undeliverable", 0))
                    vcols[3].metric("Unknown", v_counts.get("unknown", 0) + v_counts.get("pending", 0))

        st.divider()

        prospects = db.get_prospects(search_id=search_id) if search_id else []
        prospects_with_email = [p for p in prospects if p.get("email")
                                and p.get("verification_status") != "undeliverable"]

        st.metric("내보내기 대상", f"{len(prospects_with_email)}명 (이메일 있는 건, undeliverable 제외)")

        if prospects_with_email:
            import pandas as pd

            df = pd.DataFrame(prospects_with_email)
            display_cols = ["contact_name", "email", "company", "title", "email_confidence", "verification_status"]
            display_cols = [c for c in display_cols if c in df.columns]
            st.dataframe(df[display_cols], width="stretch", hide_index=True)

        st.divider()

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ AI 평가로"):
                st.session_state.prospect_step = "enrich"
                st.rerun()
        with col2:
            csv_content = db.export_prospects_to_csv(search_id) if search_id else ""
            if csv_content.strip():
                today = datetime.now().strftime("%y%m%d")
                st.download_button(
                    "📥 CSV 다운로드",
                    data=csv_content,
                    file_name=f"{search_name}_{today}.csv",
                    mime="text/csv",
                )
        with col3:
            if st.button("📧 콜드메일 캠페인으로", type="primary", disabled=not csv_content.strip()):
                st.session_state.csv_data = csv_content
                st.session_state.step = "input"
                st.session_state.active_page = "📝 콜드메일"
                # Reset prospect state
                st.session_state.prospect_step = "search"
                st.rerun()


# ══════════════════════════════════════════════════════════
# PAGE 2: Cold Email (콜드메일)
# ══════════════════════════════════════════════════════════

elif page == "📝 콜드메일":
    st.title("콜드메일 생성")

    # ── Mode selector ─────────────────────────────────
    if "coldmail_mode" not in st.session_state:
        st.session_state.coldmail_mode = "agent"
    if "agent3_log" not in st.session_state:
        st.session_state.agent3_log = []
    if "agent3_drafts" not in st.session_state:
        st.session_state.agent3_drafts = None
    if "agent3_csv" not in st.session_state:
        st.session_state.agent3_csv = None
    if "agent3_campaign_id" not in st.session_state:
        st.session_state.agent3_campaign_id = None

    # Auto-switch to agent mode when coming from Agent 2
    _from_agent2 = st.session_state.get("a3_from_agent2")
    if _from_agent2 or st.session_state.get("csv_data"):
        st.session_state.coldmail_mode = "agent"

    coldmail_mode_sel = st.radio(
        "작성 모드",
        ["🤖 Agent 모드 (자동 리서치 + 작성)", "🔧 수동 모드 (5단계)"],
        horizontal=True,
        index=0 if st.session_state.coldmail_mode == "agent" else 1,
    )
    st.session_state.coldmail_mode = "agent" if "Agent" in coldmail_mode_sel else "manual"
    st.divider()

    # ══════════════════════════════════════════════════
    # AGENT MODE (ColdMailAgent)
    # ══════════════════════════════════════════════════
    if st.session_state.coldmail_mode == "agent":
        st.caption("AI Agent가 각 회사를 개별적으로 웹 리서치한 후, 개인화된 콜드메일을 자동 작성합니다.")

        # Show active profile info
        _active_prof = st.session_state.get("active_profile")
        if _active_prof:
            st.info(
                f"활성 프로필: **{_active_prof['name']}** — "
                f"{_active_prof.get('product_name', '')} · "
                f"{_active_prof.get('sales_goal', '')} · "
                f"{_active_prof.get('language', 'ja')}"
            )
        else:
            st.warning("캠페인 프로필이 선택되지 않았습니다. '⚙️ 캠페인 설정'에서 프로필을 먼저 생성해주세요.")

        # ── Input section ──────────────────────────────
        st.subheader("캠페인 설정")

        a3col1, a3col2 = st.columns(2)

        with a3col1:
            # Language
            a3_lang = st.selectbox("언어", ["en (영어)", "ja (일본어)"], index=0, key="a3_lang")
            a3_language_code = a3_lang.split(" ")[0]

        with a3col2:
            # CTA type
            a3_cta = st.selectbox(
                "CTA 유형",
                [
                    "자동 선택",
                    "담당자 추천 요청 (기본)",
                    "15분 대화 요청 (탐색형)",
                    "피드백/의견 요청 (Design Partner)",
                    "자료/인사이트 공유 제안",
                    "Zoom/Web 미팅 제안",
                    "직접 입력",
                ],
                index=0,
                key="a3_cta",
            )
            a3_cta_text = "" if a3_cta == "자동 선택" else a3_cta
            if a3_cta == "직접 입력":
                a3_cta_text = st.text_input("CTA 내용", key="a3_cta_custom")

            a3_visit = st.text_input(
                "방문 일정 (있으면 입력)",
                placeholder="예: 2月16日〜17日に訪日予定",
                key="a3_visit",
            )

        a3_extra = st.text_area(
            "추가 지시사항 (선택)",
            placeholder="예: 톤은 casual로, 본문 5줄 이내, 특정 뉴스 반드시 언급 등",
            height=80,
            key="a3_extra",
        )

        # ── Prospect source ───────────────────────────
        st.subheader("연락처 소스")

        a3_csv_text = None
        a3_search_id = None

        # Auto-detect: Agent 2 → Agent 3 handoff
        _from_agent2_sid = st.session_state.get("a3_from_agent2")

        # Agent 2 → Agent 3 handoff: csv_data가 있으면 우선 사용
        _a2_csv = st.session_state.get("csv_data", "")
        if _from_agent2_sid and _a2_csv and _a2_csv.strip():
            # CSV 데이터에서 로드 (DB search_id 제한 없이 전체 결과)
            _a2_rows = parse_csv_string(_a2_csv)
            _a2_with_email = [r for r in _a2_rows if r.get("email")]
            st.success(
                f"Agent 2 결과 자동 연결됨: "
                f"총 {len(_a2_rows)}명, 이메일 {len(_a2_with_email)}명"
            )
            a3_csv_text = _a2_csv

            if _a2_with_email:
                import pandas as pd
                df = pd.DataFrame(_a2_with_email)
                display_cols = [c for c in ["contact_name", "email", "company", "title"] if c in df.columns]
                st.dataframe(df[display_cols], use_container_width=True, height=300, hide_index=True)

            if st.button("다른 소스 사용하기"):
                st.session_state.a3_from_agent2 = None
                st.session_state.csv_data = ""
                st.rerun()
        elif _from_agent2_sid:
            # CSV 없으면 DB fallback
            _a2_prospects = db.get_prospects(search_id=_from_agent2_sid)
            _a2_with_email = [p for p in _a2_prospects if p.get("email")]
            st.success(
                f"Agent 2 결과 (search_id={_from_agent2_sid}): "
                f"총 {len(_a2_prospects)}명, 이메일 {len(_a2_with_email)}명"
            )
            a3_search_id = _from_agent2_sid

            if _a2_with_email:
                import pandas as pd
                df = pd.DataFrame(_a2_with_email)
                display_cols = [c for c in ["contact_name", "email", "company", "title"] if c in df.columns]
                st.dataframe(df[display_cols], use_container_width=True, height=300, hide_index=True)

            if st.button("다른 소스 사용하기"):
                st.session_state.a3_from_agent2 = None
                st.rerun()
        else:
            # Manual source selection
            a3_src_tab1, a3_src_tab2, a3_src_tab3 = st.tabs(
                ["📄 CSV 업로드", "🔍 Agent 2 결과에서", "📋 기존 CSV 데이터 사용"]
            )

            with a3_src_tab1:
                a3_uploaded = st.file_uploader(
                    "연락처 CSV (contact_name, email, company, title 필수)",
                    type=["csv"],
                    key="a3_csv_upload",
                )
                if a3_uploaded:
                    a3_bytes = a3_uploaded.read()
                    try:
                        a3_csv_text = a3_bytes.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        a3_csv_text = a3_bytes.decode("utf-8")

                    rows = parse_csv_string(a3_csv_text)
                    if rows:
                        st.success(f"{len(rows)}명 로드됨")
                        import pandas as pd
                        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

            with a3_src_tab2:
                # Load from DB prospect searches
                searches = db.get_prospect_searches()
                completed_searches = [s for s in searches if s.get("status") == "completed"]
                if completed_searches:
                    search_options = {
                        f"{s['name']} ({s['total_found']}명, {s['created_at'][:10]})": s["id"]
                        for s in completed_searches
                    }
                    selected_search = st.selectbox(
                        "검색 결과 선택",
                        list(search_options.keys()),
                        key="a3_search_select",
                    )
                    a3_search_id = search_options[selected_search]

                    prospects = db.get_prospects(search_id=a3_search_id)
                    email_prospects = [p for p in prospects if p.get("email")]
                    st.info(f"총 {len(prospects)}명 중 이메일 있는 연락처: {len(email_prospects)}명")
                    if email_prospects:
                        import pandas as pd
                        df = pd.DataFrame(email_prospects)
                        display_cols = [c for c in ["contact_name", "email", "company", "title"] if c in df.columns]
                        st.dataframe(df[display_cols], width="stretch", hide_index=True)
                else:
                    st.info("완료된 컨택 검색이 없습니다. Agent 2 또는 수동 검색을 먼저 실행하세요.")

            with a3_src_tab3:
                if st.session_state.csv_data:
                    rows = parse_csv_string(st.session_state.csv_data)
                    if rows:
                        st.success(f"기존 CSV 데이터: {len(rows)}명")
                        import pandas as pd
                        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                        a3_csv_text = st.session_state.csv_data
                else:
                    st.info("기존 CSV 데이터가 없습니다. 컨택 서칭 결과에서 '콜드메일 캠페인으로' 버튼을 눌러주세요.")

        # ── Run button ────────────────────────────────
        st.divider()
        can_run_a3 = a3_csv_text is not None or a3_search_id is not None

        if st.button(
            "🤖 콜드메일 Agent 실행",
            type="primary",
            disabled=not can_run_a3 or st.session_state.get("agent_running"),
            use_container_width=True,
        ):
            # Build extra instructions
            a3_inst_parts = []
            if a3_cta_text:
                a3_inst_parts.append(f"CTA: {a3_cta_text}")
            if a3_visit:
                a3_inst_parts.append(f"방문 일정: {a3_visit}")
            if a3_extra:
                a3_inst_parts.append(a3_extra)
            a3_full_instructions = "\n".join(a3_inst_parts)

            # Build user request
            if a3_search_id:
                a3_user_request = (
                    f"콜드메일 작성해줘.\n"
                    f"연락처는 DB search_id={a3_search_id}에서 로드해줘.\n"
                    f"언어: {a3_language_code}\n"
                )
            else:
                a3_user_request = (
                    f"콜드메일 작성해줘.\n"
                    f"언어: {a3_language_code}\n"
                    f"\n## CSV 데이터\n```\n{a3_csv_text}\n```"
                )

            if a3_full_instructions:
                a3_user_request += f"\n\n## 추가 지시사항\n{a3_full_instructions}"

            # Inject campaign context from active profile
            _a3_ctx = build_campaign_context(st.session_state.get("active_profile"))
            if _a3_ctx:
                a3_user_request += f"\n\n{_a3_ctx}"

            # Reset state
            st.session_state.agent3_log = []
            st.session_state.agent3_drafts = None
            st.session_state.agent3_csv = None
            st.session_state.agent3_campaign_id = None
            st.session_state.a3_from_agent2 = None

            # Estimate total contacts for progress
            _a3_total = 0
            if a3_csv_text:
                _a3_total = max(a3_csv_text.count("\n") - 1, 1)
            elif a3_search_id:
                _a3_prospects = db.get_prospects(a3_search_id)
                _a3_total = len([p for p in _a3_prospects if p.get("email")])

            # Build sender profile markdown from active sender
            _sender_md = ""
            _active_sender = st.session_state.get("active_sender")
            if _active_sender:
                _sender_md = db.render_sender_profile_md(_active_sender)

            # Phase 1: save params and rerun to show overlay
            st.session_state._pending_agent3 = {
                "request": a3_user_request,
                "language": a3_language_code,
                "cta_type": a3_cta_text,
                "extra_instructions": a3_full_instructions,
                "campaign_context": _a3_ctx,
                "sender_profile_md": _sender_md,
                "profile_id": st.session_state.get("active_profile_id"),
                "total_items": max(_a3_total, 1),
            }
            st.session_state.agent_running = True
            st.rerun()

        # Phase 2: execute pending Agent 3 task (overlay already visible)
        if st.session_state.get("_pending_agent3"):
            _task = st.session_state.pop("_pending_agent3")
            tracker = AgentProgressTracker("agent3", total_items=_task["total_items"])
            try:
                from agent import ColdMailAgent

                agent = ColdMailAgent(
                    language=_task["language"],
                    cta_type=_task["cta_type"],
                    extra_instructions=_task["extra_instructions"],
                    campaign_context=_task["campaign_context"],
                    sender_profile_md=_task["sender_profile_md"],
                    profile_id=_task.get("profile_id"),
                    on_tool_call=tracker.on_tool_call,
                    on_tool_result=tracker.on_tool_result,
                    on_text=tracker.on_text,
                )

                result_text = agent.run(_task["request"])

                st.session_state.agent3_log = tracker.tool_log
                st.session_state.agent3_drafts = agent.draft_emails
                st.session_state.agent3_csv = agent.csv_content
                st.session_state.agent3_campaign_id = agent.campaign_id

                tracker.complete(
                    f"{len(agent.draft_emails)}개 이메일 생성"
                    + (f" (캠페인 ID: {agent.campaign_id})" if agent.campaign_id else "")
                )

            except Exception as e:
                tracker.fail(f"Agent 실행 실패: {e}")
                logger.error(f"ColdMailAgent failed: {e}")
            finally:
                st.session_state.agent_running = False
            st.rerun()

        # ── Results display ───────────────────────────
        if st.session_state.agent3_drafts:
            st.subheader(f"생성된 이메일 ({len(st.session_state.agent3_drafts)}개)")

            for i, draft in enumerate(st.session_state.agent3_drafts):
                with st.expander(
                    f"📧 {draft.get('contact_name', 'N/A')} ({draft.get('company', 'N/A')}) — {draft.get('subject', '')}",
                    expanded=(i == 0),
                ):
                    mcol1, mcol2 = st.columns([1, 3])
                    with mcol1:
                        st.markdown(f"**To:** {draft.get('email', '')}")
                        st.markdown(f"**Framework:** {draft.get('framework', 'N/A')}")
                    with mcol2:
                        if draft.get("rationale"):
                            st.caption(f"전략: {draft['rationale']}")

                    st.markdown(f"**Subject:** {draft.get('subject', '')}")
                    st.divider()
                    body = draft.get("body", "")
                    st.markdown(body, unsafe_allow_html=True)

            # ── Action buttons ──────────────────────
            st.divider()
            acol1, acol2, acol3 = st.columns(3)

            with acol1:
                if st.session_state.agent3_csv:
                    today = datetime.now().strftime("%y%m%d")
                    st.download_button(
                        "📥 CSV 다운로드",
                        data=st.session_state.agent3_csv,
                        file_name=f"coldmails_{today}.csv",
                        mime="text/csv",
                    )

            with acol2:
                if st.session_state.agent3_campaign_id:
                    if st.button("📤 Google Sheets 업로드"):
                        try:
                            from agent import ColdMailAgent
                            # Create a minimal agent just for upload
                            agent = ColdMailAgent(language=a3_language_code)
                            agent._campaign_id = st.session_state.agent3_campaign_id
                            agent._csv_content = st.session_state.agent3_csv
                            result = agent._upload_sheets()
                            st.success(result)
                        except Exception as e:
                            st.error(f"업로드 실패: {e}")

            with acol3:
                if st.session_state.agent3_campaign_id:
                    campaign = db.get_campaign(st.session_state.agent3_campaign_id)
                    if campaign and campaign.get("spreadsheet_id"):
                        st.info("📊 캠페인 현황에서 발송")
                    else:
                        st.caption("Sheets 업로드 후 발송 가능")

        # ── Agent log (full) ──────────────────────────────
        if st.session_state.agent3_log:
            full_log3 = "\n".join(st.session_state.agent3_log)
            with st.expander(f"Agent 활동 로그 ({len(st.session_state.agent3_log)}건)", expanded=False):
                st.code(full_log3, language=None)
                st.download_button(
                    "📥 로그 다운로드",
                    full_log3,
                    f"agent3_log_{time.strftime('%y%m%d_%H%M')}.txt",
                    "text/plain",
                    key="a3_log_download",
                )

        # ── Email Writing Feedback ────────────────────────
        st.divider()
        with st.expander("📝 메일 작성 피드백 관리", expanded=False):
            # Build profile list for selector
            _all_profiles = db.get_campaign_profiles()
            _profile_options = {"🌐 글로벌 (모든 프로필 공통)": None}
            for p in _all_profiles:
                _profile_options[f"📋 {p['name']}"] = p["id"]

            # Show existing feedback — global + all profiles
            _global_fb = db.get_email_feedback(profile_id=None)
            if _global_fb:
                st.markdown("**🌐 글로벌 피드백** (모든 프로필 공통)")
                for fb in _global_fb:
                    fcol1, fcol2 = st.columns([9, 1])
                    fcol1.markdown(f"- `{fb['created_at'][:16]}` {fb['feedback']}")
                    if fcol2.button("🗑️", key=f"del_efb_g_{fb['id']}"):
                        db.delete_email_feedback(fb["id"])
                        st.rerun()

            for p in _all_profiles:
                _pfb = db.get_email_feedback(profile_id=p["id"])
                if _pfb:
                    st.markdown(f"**📋 {p['name']}**")
                    for fb in _pfb:
                        fcol1, fcol2 = st.columns([9, 1])
                        fcol1.markdown(f"- `{fb['created_at'][:16]}` {fb['feedback']}")
                        if fcol2.button("🗑️", key=f"del_efb_p_{fb['id']}"):
                            db.delete_email_feedback(fb["id"])
                            st.rerun()

            if not _global_fb and not any(db.get_email_feedback(profile_id=p["id"]) for p in _all_profiles):
                st.caption("저장된 피드백이 없습니다.")

            # Add new feedback
            st.markdown("---")
            _efb_target = st.selectbox(
                "피드백 저장 대상",
                list(_profile_options.keys()),
                key="efb_target_profile",
            )
            _efb_target_pid = _profile_options[_efb_target]

            _new_efb = st.text_area(
                "새 피드백 입력",
                placeholder="예: Subject에서 「の」 탈락 금지, 본문 5줄 이내로 등",
                height=80,
                key="new_email_feedback",
            )
            if st.button("💾 피드백 저장", disabled=not _new_efb):
                db.add_email_feedback(_new_efb, profile_id=_efb_target_pid)
                _saved_label = _efb_target.replace("🌐 ", "").replace("📋 ", "")
                st.success(f"'{_saved_label}' 피드백 저장 완료")
                st.rerun()

    # ══════════════════════════════════════════════════
    # MANUAL MODE (existing 5-step pipeline)
    # ══════════════════════════════════════════════════
    else:
        # ── Step indicator ───────────────────────────────────
        steps = ["① 입력", "② 생성", "③ 검수", "④ 미리보기/저장"]
        step_map = {"input": 0, "generate": 1, "review": 2, "preview": 3}
        current_step = step_map.get(st.session_state.step, 0)

        cols = st.columns(4)
        for i, (col, label) in enumerate(zip(cols, steps)):
            if i < current_step:
                col.success(label)
            elif i == current_step:
                col.info(label)
            else:
                col.empty()
                col.markdown(f"<span style='color:gray'>{label}</span>", unsafe_allow_html=True)

        st.divider()

    # ── Manual mode step logic (skipped in Agent mode) ──
    if st.session_state.coldmail_mode != "manual":
        pass

    elif st.session_state.step == "input":
        st.subheader("① 기본 설정")

        col1, col2 = st.columns(2)

        with col1:
            # Language
            language = st.selectbox("언어", ["ja (일본어)", "en (영어)"], index=0)
            language_code = language.split(" ")[0]

        with col2:
            # CTA type
            cta_type = st.selectbox(
                "CTA (Call To Action) 유형",
                [
                    "방문 미팅 제안",
                    "Zoom/Web 미팅 제안",
                    "자료(PDF) 송부 제안",
                    "방문 + Zoom 선택지 제공",
                    "직접 입력",
                ],
                index=3,
            )

            if cta_type == "직접 입력":
                cta_custom = st.text_input("CTA 내용을 직접 입력해주세요")
            else:
                cta_custom = ""

            # Visit schedule
            visit_schedule = st.text_input(
                "방문 일정 (있으면 입력)",
                placeholder="예: 2月16日〜17日に訪日予定",
            )

        st.subheader("② CSV 업로드")
        st.caption("필수 컬럼: contact_name, email, company, title")

        uploaded_file = st.file_uploader(
            "연락처 CSV 파일을 올려주세요",
            type=["csv"],
            help="contact_name, email, company, title 컬럼이 포함된 CSV",
        )

        if uploaded_file:
            csv_bytes = uploaded_file.read()
            # Try UTF-8-SIG first, fallback to UTF-8
            try:
                csv_text = csv_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                csv_text = csv_bytes.decode("utf-8")

            rows = parse_csv_string(csv_text)
            st.session_state.csv_data = csv_text

            if rows:
                st.success(f"{len(rows)}명의 연락처가 로드되었습니다.")
                # Preview table
                import pandas as pd
                df = pd.DataFrame(rows)
                display_cols = [c for c in ["contact_name", "email", "company", "title"] if c in df.columns]
                if display_cols:
                    st.dataframe(df[display_cols], width="stretch")
                else:
                    st.dataframe(df, width="stretch")
            else:
                st.warning("CSV에 데이터가 없습니다.")

        st.subheader("③ 추가 지시사항")
        extra_instructions = st.text_area(
            "Claude에게 전달할 추가 지시사항 (선택)",
            placeholder="예: 첫 문장에 상대 회사의 최근 뉴스를 언급해줘, 본문은 짧게 5줄 이내로 작성해줘 등",
            height=100,
        )

        # ── Generate Button ──────────────────────────────
        st.divider()

        can_generate = (
            st.session_state.csv_data is not None
            and len(st.session_state.csv_data.strip()) > 0
        )

        if st.button("🚀 메일 생성 시작", type="primary", disabled=not can_generate, width="stretch"):
            # Build extra instructions string
            instructions_parts = []

            # CTA instruction
            cta_map = {
                "방문 미팅 제안": "CTA: 직접 방문 미팅을 제안하세요.",
                "Zoom/Web 미팅 제안": "CTA: Zoom/웹 미팅을 제안하세요.",
                "자료(PDF) 송부 제안": "CTA: PDF 자료 송부를 제안하세요.",
                "방문 + Zoom 선택지 제공": "CTA: 직접 방문 또는 Zoom 미팅 중 선택할 수 있도록 제안하세요.",
            }
            if cta_type in cta_map:
                instructions_parts.append(cta_map[cta_type])
            elif cta_custom:
                instructions_parts.append(f"CTA: {cta_custom}")

            if visit_schedule:
                instructions_parts.append(f"방문 일정: {visit_schedule}")

            if extra_instructions:
                instructions_parts.append(extra_instructions)

            full_instructions = "\n".join(instructions_parts)

            # Generate
            with st.spinner("Claude가 메일을 생성 중입니다... (1~2분 소요)"):
                try:
                    from claude_client import ClaudeClient
                    claude = ClaudeClient()
                    _manual_profile_id = st.session_state.get("active_profile_id")
                    _manual_feedback = db.get_combined_email_feedback_text(_manual_profile_id)
                    result = claude.generate_coldmail(
                        csv_content=st.session_state.csv_data,
                        language=language_code,
                        extra_instructions=full_instructions,
                        feedback_text=_manual_feedback,
                    )

                    st.session_state.generated_md = result
                    csv_block = extract_csv_block(result)
                    st.session_state.generated_csv = csv_block

                    # Save files
                    today = datetime.now().strftime("%y%m%d")
                    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                    md_path = OUTPUT_DIR / f"coldmails_{today}.md"
                    md_path.write_text(result, encoding="utf-8")

                    if csv_block:
                        csv_path = OUTPUT_DIR / f"{today}final.csv"
                        csv_path.write_text(csv_block, encoding="utf-8-sig")

                    st.session_state.step = "generate"
                    st.rerun()

                except Exception as e:
                    st.error(f"생성 실패: {e}")
                    logger.error(f"Generation failed: {e}")

    # ── STEP 2: Generation Result ────────────────────────
    elif st.session_state.step == "generate":
        st.subheader("② 생성 결과")

        if st.session_state.generated_md:
            with st.expander("Claude 원본 출력 (Markdown)", expanded=False):
                st.markdown(st.session_state.generated_md[:5000])
                if len(st.session_state.generated_md) > 5000:
                    st.caption("... (출력이 길어 일부만 표시)")

        if st.session_state.generated_csv:
            st.success("CSV 블록이 성공적으로 추출되었습니다.")
            rows = parse_csv_string(st.session_state.generated_csv)
            if rows:
                import pandas as pd
                df = pd.DataFrame(rows)
                st.dataframe(df, width="stretch")
        else:
            st.warning("CSV 블록 추출에 실패했습니다. 원본 출력을 확인해주세요.")

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅ 다시 입력하기"):
                st.session_state.step = "input"
                st.rerun()
        with col2:
            if st.button("🔍 검수하기 (Review)", type="primary"):
                st.session_state.step = "review"
                st.rerun()
        with col3:
            if st.button("⏭ 검수 건너뛰기 → 미리보기"):
                st.session_state.step = "preview"
                st.rerun()

    # ── STEP 3: Review ───────────────────────────────────
    elif st.session_state.step == "review":
        st.subheader("③ 메일 검수 (Review)")

        if st.session_state.review_result:
            st.markdown(st.session_state.review_result[:8000])
            if len(st.session_state.review_result) > 8000:
                st.caption("... (검수 결과가 길어 일부만 표시)")
        else:
            with st.spinner("Claude가 메일을 검수 중입니다... (1~2분 소요)"):
                try:
                    from claude_client import ClaudeClient
                    claude = ClaudeClient()
                    content = st.session_state.generated_md or ""
                    result = claude.review(content, auto_fix=True)
                    st.session_state.review_result = result

                    # Save review report
                    today = datetime.now().strftime("%Y%m%d")
                    report_path = OUTPUT_DIR / f"review_{today}.md"
                    report_path.write_text(result, encoding="utf-8")

                    st.rerun()
                except Exception as e:
                    st.error(f"검수 실패: {e}")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("⬅ 생성 결과로 돌아가기"):
                st.session_state.step = "generate"
                st.rerun()
        with col2:
            if st.button("➡ 미리보기", type="primary"):
                st.session_state.step = "preview"
                st.rerun()

    # ── STEP 4: Preview ──────────────────────────────────
    elif st.session_state.step == "preview":
        st.subheader("④ 메일 미리보기")

        if not st.session_state.generated_csv:
            st.warning("생성된 CSV가 없습니다. 먼저 메일을 생성해주세요.")
            if st.button("⬅ 입력으로 돌아가기"):
                st.session_state.step = "input"
                st.rerun()
        else:
            rows = parse_csv_string(st.session_state.generated_csv)

            if rows:
                for i, row in enumerate(rows):
                    with st.expander(
                        f"📧 {row.get('contact_name', 'N/A')} ({row.get('company', 'N/A')}) — {row.get('subject', '')}",
                        expanded=(i == 0),
                    ):
                        st.markdown(f"**To:** {row.get('email', '')}")
                        st.markdown(f"**Subject:** {row.get('subject', '')}")
                        st.divider()
                        # Render body (HTML with <br> tags)
                        body = row.get("body", "")
                        st.markdown(body, unsafe_allow_html=True)

            st.divider()
            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("⬅ 검수로 돌아가기"):
                    st.session_state.step = "review"
                    st.rerun()
            with col2:
                # Download CSV
                today = datetime.now().strftime("%y%m%d")
                st.download_button(
                    "📥 CSV 다운로드",
                    data=st.session_state.generated_csv,
                    file_name=f"{today}final.csv",
                    mime="text/csv",
                )
            with col3:
                if st.button("💾 캠페인으로 저장", type="primary"):
                    with st.spinner("저장 중..."):
                        try:
                            today = datetime.now().strftime("%y%m%d")
                            campaign_name = f"ColdMail_{today}"
                            csv_path = OUTPUT_DIR / f"{today}final.csv"
                            csv_path.write_text(
                                st.session_state.generated_csv,
                                encoding="utf-8-sig",
                            )
                            campaign_id = db.create_campaign(campaign_name, str(csv_path))
                            st.success(f"캠페인 '{campaign_name}' 저장 완료 (ID: {campaign_id}). 📊 캠페인 현황에서 발송하세요.")

                            st.session_state.step = "input"
                            st.session_state.generated_md = None
                            st.session_state.generated_csv = None
                            st.session_state.review_result = None
                        except Exception as e:
                            st.error(f"저장 실패: {e}")


# ══════════════════════════════════════════════════════════
# PAGE 3: Campaign Status Dashboard (GMass Live)
# ══════════════════════════════════════════════════════════

elif page == "📊 캠페인 현황":
    st.title("캠페인 현황")

    import pandas as pd

    # ── 발송 대기 캠페인 (DB draft campaigns) ─────────────
    _draft_campaigns = []
    try:
        conn = db.get_connection()
        _draft_rows = conn.execute(
            "SELECT * FROM campaigns WHERE status = 'draft' ORDER BY id DESC"
        ).fetchall()
        conn.close()
        _draft_campaigns = [dict(r) for r in _draft_rows]
    except Exception:
        pass

    if _draft_campaigns:
        st.subheader("📝 발송 대기 캠페인")
        for dc in _draft_campaigns:
            dc_id = dc["id"]
            dc_name = dc.get("name", f"캠페인 #{dc_id}")
            dc_created = (dc.get("created_at") or "")[:16]
            dc_has_sheet = bool(dc.get("spreadsheet_id"))

            with st.expander(f"{dc_name} (ID: {dc_id}) — {dc_created}", expanded=True):
                # Show CSV preview if available
                csv_path = dc.get("csv_path", "")
                if csv_path and Path(csv_path).exists():
                    try:
                        csv_df = pd.read_csv(csv_path, encoding="utf-8-sig")
                        st.dataframe(csv_df, hide_index=True)
                        st.caption(f"{len(csv_df)}명 · CSV: {csv_path}")
                    except Exception:
                        st.caption(f"CSV: {csv_path}")

                sc1, sc2, sc3 = st.columns(3)

                with sc1:
                    if not dc_has_sheet:
                        if st.button("📤 Google Sheets 업로드", key=f"sheet_upload_{dc_id}"):
                            with st.spinner("업로드 중..."):
                                try:
                                    from agent import ColdMailAgent
                                    agent = ColdMailAgent()
                                    agent._campaign_id = dc_id
                                    if csv_path and Path(csv_path).exists():
                                        agent._csv_content = Path(csv_path).read_text(encoding="utf-8-sig")
                                    result = agent._upload_sheets()
                                    st.success(result)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"업로드 실패: {e}")
                    else:
                        st.success("Sheets 업로드 완료")

                with sc2:
                    if dc_has_sheet:
                        if st.button("🚀 GMass 발송", key=f"gmass_send_{dc_id}", type="primary"):
                            st.warning("실제 이메일이 발송됩니다!")
                            try:
                                from agent import ColdMailAgent
                                agent = ColdMailAgent()
                                agent._campaign_id = dc_id
                                result = agent._send_gmass()
                                st.success(result)
                                st.balloons()
                                st.rerun()
                            except Exception as e:
                                st.error(f"발송 실패: {e}")
                    else:
                        st.caption("Sheets 업로드 후 발송 가능")

                with sc3:
                    if st.button("🗑️ 삭제", key=f"del_draft_{dc_id}"):
                        db.update_campaign(dc_id, status="cancelled")
                        st.rerun()

        st.divider()

    # ── GMass 발송 완료 캠페인 (Live) ─────────────────────
    st.subheader("📊 발송된 캠페인 (GMass)")

    # Load GMass campaigns directly from API
    try:
        from gmass_client import GMassClient
        gmass = GMassClient()
        gmass_campaigns = gmass.get_campaigns()
    except Exception as e:
        st.error(f"GMass API 연결 실패: {e}")
        gmass_campaigns = []

    if not gmass_campaigns:
        st.info("GMass에 캠페인이 없습니다.")
    else:
        # Filter to campaigns with recipients > 0
        active_campaigns = [c for c in gmass_campaigns if c.get("statistics", {}).get("recipients", 0) > 0]
        other_campaigns = [c for c in gmass_campaigns if c.get("statistics", {}).get("recipients", 0) == 0]

        if not active_campaigns:
            st.info("발송된 캠페인이 없습니다.")

        for campaign in active_campaigns:
            cid = str(campaign.get("campaignId", ""))
            stats = campaign.get("statistics", {})
            recipients_count = stats.get("recipients", 0)
            opens_count = stats.get("opens", 0)
            replies_count = stats.get("replies", 0)
            bounces_count = stats.get("bounces", 0)
            blocks_count = stats.get("blocks", 0)
            unsubs_count = stats.get("unsubscribes", 0)
            clicks_count = stats.get("clicks", 0)
            open_rate = f"{opens_count / recipients_count * 100:.1f}%" if recipients_count else "0%"
            status = campaign.get("status", "N/A")
            sent_time = campaign.get("creationTime", "")[:16].replace("T", " ")

            with st.expander(
                f"Campaign {cid} — {recipients_count}명 | Open {open_rate} | Replies {replies_count}",
                expanded=(campaign == active_campaigns[0]),
            ):
                # ── Summary metrics (like GMass dashboard) ──
                st.caption(f"Sent: {sent_time} | Status: {status} | From: {campaign.get('fromLine', '')}")

                m1, m2, m3, m4, m5, m6 = st.columns(6)
                m1.metric("Recipients", recipients_count)
                m2.metric("Opens", f"{opens_count} ({open_rate})")
                m3.metric("Replies", f"{replies_count} ({replies_count/recipients_count*100:.1f}%)" if recipients_count else "0")
                m4.metric("Bounces", f"{bounces_count} ({bounces_count/recipients_count*100:.1f}%)" if recipients_count else "0")
                m5.metric("Blocks", blocks_count)
                m6.metric("Unsubscribes", unsubs_count)

                st.divider()

                # ── Detail tabs ──
                tab_opens, tab_replies, tab_bounces, tab_all = st.tabs(
                    ["Opens", "Replies", "Bounces/Blocks", "All Recipients"]
                )

                with tab_opens:
                    if opens_count > 0:
                        try:
                            opens_data = gmass.get_campaign_opens(cid)
                            if opens_data:
                                df_opens = pd.DataFrame(opens_data)
                                df_opens = df_opens.rename(columns={
                                    "emailAddress": "Email",
                                    "openCount": "Open Count",
                                    "lastOpenTime": "Last Open",
                                })
                                display_cols = [c for c in ["Email", "Open Count", "Last Open"] if c in df_opens.columns]
                                df_opens = df_opens[display_cols].sort_values("Open Count", ascending=False)
                                # Format time
                                if "Last Open" in df_opens.columns:
                                    df_opens["Last Open"] = df_opens["Last Open"].str[:16].str.replace("T", " ")
                                st.dataframe(df_opens, width="stretch", hide_index=True)
                            else:
                                st.info("오픈 데이터가 없습니다.")
                        except Exception as e:
                            st.error(f"Opens 조회 실패: {e}")
                    else:
                        st.info("아직 오픈한 수신자가 없습니다.")

                with tab_replies:
                    if replies_count > 0:
                        try:
                            replies_data = gmass.get_campaign_replies(cid)
                            if replies_data:
                                # Fetch actual reply content from Gmail IMAP
                                reply_emails_list = [r.get("emailAddress", "") for r in replies_data]
                                gmail_replies = {}
                                try:
                                    from gmail_reader import GmailReader
                                    reader = GmailReader()
                                    gmail_replies = reader.find_all_replies(reply_emails_list)
                                except Exception as gmail_err:
                                    st.caption(f"Gmail IMAP 연결 안 됨 (답장 원문 조회 불가): {gmail_err}")

                                for ridx, reply in enumerate(replies_data):
                                    reply_email = reply.get("emailAddress", "")
                                    reply_time = reply.get("replyTime", "")[:16].replace("T", " ")
                                    already_replied = reply.get("alreadyReplied", False)

                                    st.markdown(f"### {reply_email}")
                                    st.caption(f"Reply time: {reply_time}" + (" | (답장 완료)" if already_replied else ""))

                                    # Show reply content from Gmail
                                    gmail_data = gmail_replies.get(reply_email)
                                    if gmail_data:
                                        st.markdown(f"**Subject:** {gmail_data.get('subject', '')}")
                                        with st.expander("답장 원문 보기", expanded=True):
                                            st.text(gmail_data.get("body", "(본문 없음)"))

                                    if st.button(
                                        "✍️ 답장 작성",
                                        key=f"reply_btn_{cid}_{ridx}",
                                    ):
                                        original_body = _find_sent_email_body(reply_email)
                                        reply_body_text = gmail_data.get("body", "") if gmail_data else ""
                                        st.session_state.reply_context = {
                                            "email": reply_email,
                                            "original_body": original_body,
                                            "reply_body": reply_body_text,
                                            "reply_subject": gmail_data.get("subject", "") if gmail_data else "",
                                            "campaign_id": cid,
                                        }
                                        st.session_state.active_page = "💬 답장 작성"
                                        st.rerun()

                                    if ridx < len(replies_data) - 1:
                                        st.divider()
                            else:
                                st.info("답장 데이터가 없습니다.")
                        except Exception as e:
                            st.error(f"Replies 조회 실패: {e}")
                    else:
                        st.info("아직 답장이 없습니다.")

                with tab_bounces:
                    if bounces_count > 0 or blocks_count > 0:
                        try:
                            if bounces_count > 0:
                                st.markdown("**Bounces:**")
                                bounces_data = gmass.get_campaign_bounces(cid)
                                if bounces_data:
                                    for b in bounces_data:
                                        st.markdown(f"- `{b.get('emailAddress', '')}` — {b.get('bounceTime', '')[:16]}")

                            if blocks_count > 0:
                                st.markdown("**Blocks:**")
                                blocks_data = gmass.get_campaign_blocks(cid)
                                if blocks_data:
                                    for b in blocks_data:
                                        st.markdown(f"- `{b.get('emailAddress', '')}` — Security policy rejection")
                        except Exception as e:
                            st.error(f"Bounce/Block 조회 실패: {e}")
                    else:
                        st.info("바운스/차단 없음.")

                with tab_all:
                    try:
                        all_recipients = gmass.get_campaign_recipients(cid)
                        if all_recipients:
                            df_all = pd.DataFrame(all_recipients)
                            df_all = df_all.rename(columns={
                                "emailAddress": "Email",
                                "sentTime": "Sent Time",
                            })
                            display_cols = [c for c in ["Email", "Sent Time"] if c in df_all.columns]
                            df_all = df_all[display_cols]
                            if "Sent Time" in df_all.columns:
                                df_all["Sent Time"] = df_all["Sent Time"].str[:16].str.replace("T", " ")

                            # Merge open status
                            try:
                                opens_data = gmass.get_campaign_opens(cid)
                                open_emails = {o["emailAddress"]: o.get("openCount", 0) for o in opens_data} if opens_data else {}
                            except Exception:
                                open_emails = {}
                            try:
                                replies_data = gmass.get_campaign_replies(cid)
                                reply_emails = {r["emailAddress"] for r in replies_data} if replies_data else set()
                            except Exception:
                                reply_emails = set()
                            try:
                                bounce_data = gmass.get_campaign_bounces(cid)
                                bounce_emails = {b["emailAddress"] for b in bounce_data} if bounce_data else set()
                            except Exception:
                                bounce_emails = set()
                            try:
                                block_data = gmass.get_campaign_blocks(cid)
                                block_emails = {b["emailAddress"] for b in block_data} if block_data else set()
                            except Exception:
                                block_emails = set()

                            def get_status(email):
                                if email in reply_emails:
                                    return "Replied"
                                if email in bounce_emails:
                                    return "Bounced"
                                if email in block_emails:
                                    return "Blocked"
                                if email in open_emails:
                                    return f"Opened ({open_emails[email]}x)"
                                return "Sent"

                            df_all["Status"] = df_all["Email"].apply(get_status)
                            st.dataframe(df_all, width="stretch", hide_index=True)
                        else:
                            st.info("수신자 데이터가 없습니다.")
                    except Exception as e:
                        st.error(f"Recipients 조회 실패: {e}")

        # Show empty campaigns in a collapsed section
        if other_campaigns:
            with st.expander(f"기타 캠페인 ({len(other_campaigns)}개 — recipients=0)", expanded=False):
                for c in other_campaigns:
                    cid = c.get("campaignId", "")
                    st.caption(f"ID: {cid} | Subject: {c.get('subject', 'N/A')} | {c.get('creationTime', '')[:16]}")


# ══════════════════════════════════════════════════════════
# PAGE 4: Reply Composer
# ══════════════════════════════════════════════════════════

elif page == "💬 답장 작성":
    st.title("비즈니스 메일 답장 작성")
    st.caption("받은 메일에 대한 일본어 비즈니스 답장을 자동 생성합니다.")

    # Check if we came from campaign replies tab
    ctx = st.session_state.reply_context
    prefill_reply = ""
    if ctx:
        st.info(f"**{ctx['email']}** 에 대한 답장을 작성합니다.")
        if ctx.get("reply_subject"):
            st.caption(f"Subject: {ctx['reply_subject']}")
        if ctx.get("original_body"):
            with st.expander("우리가 보낸 원본 메일", expanded=False):
                original_html = ctx["original_body"].replace("<br>", "\n")
                st.text(original_html)
        if ctx.get("reply_body"):
            prefill_reply = ctx["reply_body"]
        # Clear button
        if st.button("초기화 (다른 메일에 답장)"):
            st.session_state.reply_context = None
            st.rerun()

    received_mail = st.text_area(
        "받은 메일 원문",
        height=200,
        value=prefill_reply,
        placeholder="상대방이 보낸 메일 전문을 붙여넣어주세요...",
        help="캠페인 현황 → Replies에서 '답장 작성' 버튼을 누르면 자동으로 채워집니다.",
    )

    intent = st.text_area(
        "답장에 담을 요지/의도 (한국어 가능)",
        height=100,
        placeholder="예: 검토 감사, Zoom도 가능, 자료 PDF로 보내겠다 등",
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        reply_lang = st.selectbox("언어", ["일본어", "영어", "혼합"], index=0, key="reply_lang")
    with col2:
        reply_tone = st.selectbox("톤", ["매우 정중", "정중", "캐주얼"], index=1, key="reply_tone")
    with col3:
        reply_length = st.selectbox("길이", ["짧게", "보통", "길게"], index=1, key="reply_length")

    meeting_option = st.text_input(
        "미팅 옵션 (선택)",
        placeholder="예: Web(Zoom) 우선, 대면도 가능",
    )

    if st.button("✍️ 답장 생성", type="primary", disabled=not (received_mail and intent)):
        with st.spinner("Claude가 답장을 작성 중입니다..."):
            try:
                from claude_client import ClaudeClient
                claude = ClaudeClient()

                # Build the reply skill prompt
                skill_text = (DATA_DIR.parent / ".claude" / "skills" / "japan" / "reply" / "SKILL.md").read_text(encoding="utf-8")

                constraints = (
                    f"언어: {reply_lang}\n"
                    f"톤: {reply_tone}\n"
                    f"길이: {reply_length}\n"
                )
                if meeting_option:
                    constraints += f"미팅 옵션: {meeting_option}\n"

                # Include original sent email as context if available
                original_context = ""
                if ctx and ctx.get("original_body"):
                    original_context = (
                        f"\n\n## 참고: 우리가 보낸 원본 메일\n"
                        f"<<<\n{ctx['original_body']}\n>>>\n"
                    )

                user_prompt = (
                    f"1) 상대 메일 원문:\n<<<\n{received_mail}\n>>>\n\n"
                    f"2) 답장에 담고 싶은 요지:\n{intent}\n\n"
                    f"3) 제약/선호:\n{constraints}\n"
                    f"4) 서명: 기존 서명 그대로 사용\n"
                    f"{original_context}"
                )

                result = claude._call(skill_text, user_prompt)

                st.divider()
                st.subheader("생성된 답장")
                st.markdown(result)

                # Copy-friendly text area
                with st.expander("복사용 텍스트"):
                    st.text_area("", value=result, height=300, key="reply_copy")

            except Exception as e:
                st.error(f"답장 생성 실패: {e}")


# ══════════════════════════════════════════════════════════
# PAGE 5: Skills List
# ══════════════════════════════════════════════════════════

elif page == "📚 스킬 목록":
    st.title("스킬 관리")

    # ── Sender Profile Management ─────────────────────────
    with st.expander("👤 발신자 프로필 관리", expanded=False):
        sender_profiles = db.get_sender_profiles()

        # Show existing profiles
        if sender_profiles:
            st.markdown("**저장된 발신자 프로필**")
            for sp in sender_profiles:
                sp_col1, sp_col2, sp_col3 = st.columns([4, 1, 1])
                with sp_col1:
                    is_active_sp = st.session_state.get("active_sender_id") == sp["id"]
                    sp_label = f"{'✅ ' if is_active_sp else ''}{sp['name']}"
                    sp_detail = f"{sp.get('name_en', '')} | {sp.get('company_en', '')} | {sp.get('email', '')}"
                    st.markdown(f"**{sp_label}**  \n{sp_detail}")
                with sp_col2:
                    if st.button("사용", key=f"use_sender_{sp['id']}"):
                        st.session_state.active_sender_id = sp["id"]
                        st.session_state.active_sender = sp
                        st.rerun()
                with sp_col3:
                    if st.button("삭제", key=f"del_sender_{sp['id']}"):
                        db.delete_sender_profile(sp["id"])
                        if st.session_state.get("active_sender_id") == sp["id"]:
                            st.session_state.active_sender_id = None
                            st.session_state.active_sender = None
                        st.rerun()
            st.divider()

        # Create new sender profile form
        st.markdown("**새 발신자 프로필 추가**")

        # Import from sender_profile.md
        sp_md_path = DATA_DIR / "sender_profile.md"
        if sp_md_path.exists():
            if st.button("📥 sender_profile.md에서 가져오기", key="import_sender_md"):
                md_text = sp_md_path.read_text(encoding="utf-8")
                # Parse fields
                field_map = {
                    "이름 (영문)": "name_en", "이름 (일본어)": "name_ja",
                    "직함 (영문)": "title_en", "직함 (일본어)": "title_ja",
                    "회사명 (영문)": "company_en", "회사명 (일본어)": "company_ja",
                    "이메일": "email", "전화번호": "phone",
                }
                parsed = {}
                for label, key in field_map.items():
                    m = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.+)", md_text)
                    if m:
                        parsed[key] = m.group(1).strip()
                # Parse signature blocks
                sig_blocks = re.findall(r"## 서명 \((.+?)\)\s*\n+```\n(.*?)```", md_text, re.DOTALL)
                for sig_label, sig_body in sig_blocks:
                    if "일본어" in sig_label:
                        parsed["signature_ja"] = sig_body.strip()
                    elif "영문" in sig_label:
                        parsed["signature_en"] = sig_body.strip()
                # Build profile name from company + name
                pname = f"{parsed.get('name_en', '')} ({parsed.get('company_en', '')})".strip()
                if not pname or pname == "()":
                    pname = "Imported Profile"
                try:
                    new_id = db.save_sender_profile(
                        name=pname,
                        name_en=parsed.get("name_en", ""),
                        name_ja=parsed.get("name_ja", ""),
                        title_en=parsed.get("title_en", ""),
                        title_ja=parsed.get("title_ja", ""),
                        company_en=parsed.get("company_en", ""),
                        company_ja=parsed.get("company_ja", ""),
                        email=parsed.get("email", ""),
                        phone=parsed.get("phone", ""),
                        signature_ja=parsed.get("signature_ja", ""),
                        signature_en=parsed.get("signature_en", ""),
                    )
                    st.session_state.active_sender_id = new_id
                    st.session_state.active_sender = db.get_sender_profile(new_id)
                    st.success(f"'{pname}' 프로필을 가져와서 저장했습니다!")
                    st.rerun()
                except Exception as e:
                    st.error(f"가져오기 실패: {e}")

        with st.form("sender_profile_form", clear_on_submit=True):
            sp_profile_name = st.text_input("프로필 이름 *", placeholder="예: 류임수 (RISORIUS)")

            sp_c1, sp_c2 = st.columns(2)
            with sp_c1:
                sp_name_en = st.text_input("이름 (영문)", placeholder="Imsoo Ryoo")
                sp_title_en = st.text_input("직함 (영문)", placeholder="Co-Founder & AI Engineer")
                sp_company_en = st.text_input("회사명 (영문)", placeholder="RISORIUS")
            with sp_c2:
                sp_name_ja = st.text_input("이름 (일본어)", placeholder="リュ・イムス")
                sp_title_ja = st.text_input("직함 (일본어)", placeholder="共同創業者 兼 AIエンジニア")
                sp_company_ja = st.text_input("회사명 (일본어)", placeholder="リソリウス")

            sp_c3, sp_c4 = st.columns(2)
            with sp_c3:
                sp_email = st.text_input("이메일", placeholder="leiris@risorious.com")
            with sp_c4:
                sp_phone = st.text_input("전화번호", placeholder="+82-10-9592-2268")

            sp_sig_ja = st.text_area(
                "서명 (일본어 메일용)",
                placeholder="リュ・イムス\n共同創業者 兼 AIエンジニア\nリソリウス\nEmail: leiris@risorious.com",
                height=100,
            )
            sp_sig_en = st.text_area(
                "서명 (영문 메일용)",
                placeholder="Imsoo Ryoo\nCo-Founder & AI Engineer\nRISORIUS\nEmail: leiris@risorious.com",
                height=100,
            )
            sp_extra = st.text_input("추가 정보 (선택)", placeholder="예: LinkedIn URL, 기타")

            sp_submitted = st.form_submit_button("💾 발신자 프로필 저장", use_container_width=True)
            if sp_submitted:
                if not sp_profile_name.strip():
                    st.error("프로필 이름을 입력해주세요.")
                elif not sp_name_en.strip() and not sp_name_ja.strip():
                    st.error("이름 (영문 또는 일본어)을 최소 하나 입력해주세요.")
                else:
                    new_sp_id = db.save_sender_profile(
                        name=sp_profile_name.strip(),
                        name_en=sp_name_en.strip(),
                        name_ja=sp_name_ja.strip(),
                        title_en=sp_title_en.strip(),
                        title_ja=sp_title_ja.strip(),
                        company_en=sp_company_en.strip(),
                        company_ja=sp_company_ja.strip(),
                        email=sp_email.strip(),
                        phone=sp_phone.strip(),
                        signature_ja=sp_sig_ja.strip(),
                        signature_en=sp_sig_en.strip(),
                        extra_info=sp_extra.strip(),
                    )
                    st.session_state.active_sender_id = new_sp_id
                    new_sp = db.get_sender_profile(new_sp_id)
                    st.session_state.active_sender = new_sp
                    st.success(f"발신자 프로필 '{sp_profile_name}' 저장 완료! 자동으로 활성화되었습니다.")
                    st.rerun()

    st.divider()

    # Load skills from .claude/skills directory
    skills_dir = PROJECT_ROOT / ".claude" / "skills"

    if not skills_dir.exists():
        st.warning("스킬 디렉토리를 찾을 수 없습니다.")
        st.info(f"예상 경로: {skills_dir}")
    else:
        # Collect all files organized by folder
        folder_files = {}

        # Define folder display names
        folder_names = {
            "_global": "🌐 전역 (Global)",
            "japan": "🇯🇵 일본 (Japan)",
            "shared": "🔗 공용 (Shared)",
        }

        # Scan folders
        for folder in skills_dir.iterdir():
            if not folder.is_dir():
                continue

            folder_key = folder.name
            if folder_key not in folder_names:
                continue

            folder_files[folder_key] = []

            # Skills in this folder
            for skill_path in folder.rglob("SKILL.md"):
                skill_name = skill_path.parent.name
                if skill_name.startswith("_"):
                    continue
                content = skill_path.read_text(encoding="utf-8")
                desc_match = re.search(r'description:\s*["\'](.+?)["\']', content)
                description = desc_match.group(1) if desc_match else ""
                folder_files[folder_key].append({
                    "type": "skill",
                    "name": f"/{skill_name}",
                    "description": description,
                    "path": skill_path,
                })

            # Common/global files in _common subfolder or direct .md files
            common_subdir = folder / "_common"
            if common_subdir.exists():
                for common_file in common_subdir.glob("*.md"):
                    folder_files[folder_key].append({
                        "type": "common",
                        "name": common_file.name,
                        "description": "공통 규칙",
                        "path": common_file,
                    })

            # Direct .md files in folder (like SENDER_PROFILE.md in _global)
            for md_file in folder.glob("*.md"):
                folder_files[folder_key].append({
                    "type": "config",
                    "name": md_file.name,
                    "description": "설정 파일",
                    "path": md_file,
                })

            # Sort files in folder
            folder_files[folder_key].sort(key=lambda x: (0 if x["type"] == "skill" else 1, x["name"]))

        # Build flat list for selection with folder prefixes
        all_files = []
        for folder_key in ["_global", "japan", "shared"]:
            if folder_key in folder_files:
                for f in folder_files[folder_key]:
                    f["folder"] = folder_key
                    f["display_name"] = f"{f['name']}"
                    all_files.append(f)

        # File selector
        col1, col2 = st.columns([1, 3])

        with col1:
            st.subheader("파일 목록")

            for folder_key in ["_global", "japan", "shared"]:
                if folder_key not in folder_files or not folder_files[folder_key]:
                    continue

                st.markdown(f"**{folder_names[folder_key]}**")

                for f in folder_files[folder_key]:
                    icon = "📝" if f["type"] == "skill" else ("📋" if f["type"] == "common" else "⚙️")
                    btn_key = f"btn_{folder_key}_{f['name']}"
                    if st.button(f"{icon} {f['name']}", key=btn_key, use_container_width=True):
                        st.session_state.selected_skill = f"{folder_key}::{f['name']}"

                st.markdown("")  # spacing

            # Initialize selected skill
            if "selected_skill" not in st.session_state and all_files:
                first = all_files[0]
                st.session_state.selected_skill = f"{first['folder']}::{first['name']}"

        with col2:
            if st.session_state.get("selected_skill"):
                # Parse folder::name
                parts = st.session_state.selected_skill.split("::", 1)
                if len(parts) == 2:
                    sel_folder, sel_name = parts
                    selected = next(
                        (f for f in all_files if f["folder"] == sel_folder and f["name"] == sel_name),
                        None
                    )
                else:
                    selected = None

                if selected:
                    folder_label = folder_names.get(selected["folder"], selected["folder"])
                    st.subheader(f"{selected['name']}")
                    st.caption(f"{folder_label} | {selected['description']}")

                    content = selected["path"].read_text(encoding="utf-8")

                    # Mode selector: 보기 / 직접 편집 / AI 수정
                    mode = st.radio(
                        "모드",
                        ["📖 보기", "✏️ 직접 편집", "🤖 AI 수정"],
                        horizontal=True,
                        key="skill_mode"
                    )

                    if mode == "📖 보기":
                        # Display as markdown
                        st.markdown(content)

                    elif mode == "✏️ 직접 편집":
                        new_content = st.text_area(
                            "내용 편집",
                            value=content,
                            height=500,
                            key=f"edit_{selected['folder']}_{selected['name']}"
                        )

                        if st.button("💾 저장", type="primary"):
                            try:
                                selected["path"].write_text(new_content, encoding="utf-8")
                                st.success("저장되었습니다!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"저장 실패: {e}")

                    elif mode == "🤖 AI 수정":
                        st.markdown("**피드백을 입력하면 Claude가 스킬 파일을 수정합니다.**")

                        # Show current content in expander
                        with st.expander("현재 내용 보기", expanded=False):
                            st.markdown(content)

                        # Feedback input
                        feedback = st.text_area(
                            "수정 요청 (피드백)",
                            height=100,
                            placeholder="예: CTA 기본값을 '자료 송부 제안'으로 변경해줘, 새로운 규칙 추가해줘: ~, 이 부분 삭제해줘 등",
                            key="skill_feedback"
                        )

                        # Session state for preview
                        preview_key = f"preview_{selected['folder']}_{selected['name']}"
                        if preview_key not in st.session_state:
                            st.session_state[preview_key] = None

                        col_gen, col_clear = st.columns([1, 1])

                        with col_gen:
                            if st.button("🔄 미리보기 생성", type="primary", disabled=not feedback):
                                with st.spinner("Claude가 수정 중..."):
                                    try:
                                        from claude_client import ClaudeClient
                                        claude = ClaudeClient()
                                        modified = claude.edit_skill(content, feedback)
                                        st.session_state[preview_key] = modified
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"수정 실패: {e}")

                        with col_clear:
                            if st.session_state[preview_key] and st.button("🗑️ 미리보기 취소"):
                                st.session_state[preview_key] = None
                                st.rerun()

                        # Show preview with diff
                        if st.session_state[preview_key]:
                            st.divider()
                            st.markdown("### 수정 미리보기")

                            modified_content = st.session_state[preview_key]

                            # Show diff
                            import difflib
                            original_lines = content.splitlines(keepends=True)
                            modified_lines = modified_content.splitlines(keepends=True)

                            diff = list(difflib.unified_diff(
                                original_lines,
                                modified_lines,
                                fromfile="원본",
                                tofile="수정본",
                                lineterm=""
                            ))

                            if diff:
                                # Format diff for display
                                diff_text = []
                                for line in diff:
                                    if line.startswith("+") and not line.startswith("+++"):
                                        diff_text.append(f"🟢 {line}")
                                    elif line.startswith("-") and not line.startswith("---"):
                                        diff_text.append(f"🔴 {line}")
                                    elif line.startswith("@@"):
                                        diff_text.append(f"📍 {line}")

                                with st.expander("변경 사항 (Diff)", expanded=True):
                                    st.code("\n".join(diff_text[:100]), language="diff")
                                    if len(diff_text) > 100:
                                        st.caption("... (변경 사항이 많아 일부만 표시)")

                            # Full preview
                            with st.expander("수정된 전체 내용", expanded=False):
                                st.text_area(
                                    "수정본",
                                    value=modified_content,
                                    height=400,
                                    key="preview_content",
                                    disabled=True
                                )

                            # Apply button
                            col_apply, col_reject = st.columns([1, 1])
                            with col_apply:
                                if st.button("✅ 적용하기", type="primary"):
                                    try:
                                        selected["path"].write_text(modified_content, encoding="utf-8")
                                        st.session_state[preview_key] = None
                                        st.success("저장되었습니다!")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"저장 실패: {e}")

                            with col_reject:
                                if st.button("❌ 취소"):
                                    st.session_state[preview_key] = None
                                    st.rerun()


# ── Footer ───────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.caption("RISORIUS Cold Email System v1.0")
st.sidebar.caption(f"Output dir: {OUTPUT_DIR}")

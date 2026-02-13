"""
Industry research API client - ClinicalTrials.gov v2 + PubMed E-utilities + DuckDuckGo web search.

Provides supplementary data for Claude enrichment:
- Clinical trial activity (investigators, indications, status)
- Publication history (recency, topics, collaborations)
- Web search results for company verification

No API keys required. Rate limits: ClinicalTrials not restricted, PubMed 3 req/sec.
"""
import logging
import time
import requests

logger = logging.getLogger(__name__)


class ResearchClient:
    CT_BASE_URL = "https://clinicaltrials.gov/api/v2"
    PUBMED_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self):
        pass

    def _get(self, url: str, params: dict, max_retries: int = 3) -> dict:
        """GET with exponential backoff on rate limit or server errors."""
        for attempt in range(max_retries):
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code in (429, 500, 503):
                wait = min(2 ** attempt * 2, 60)
                logger.warning(f"Research API {resp.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise Exception(f"Research API failed after {max_retries} retries")

    # ── ClinicalTrials.gov v2 ────────────────────────────

    def search_trials(
        self,
        condition: str | None = None,
        sponsor: str | None = None,
        status: str = "RECRUITING",
        page_size: int = 5,
    ) -> list[dict]:
        """Search ClinicalTrials.gov for trials."""
        params: dict = {
            "format": "json",
            "pageSize": page_size,
        }
        if condition:
            params["query.cond"] = condition
        if sponsor:
            params["query.spons"] = sponsor
        if status:
            params["filter.overallStatus"] = status

        try:
            result = self._get(f"{self.CT_BASE_URL}/studies", params)
            time.sleep(1)
            studies = result.get("studies", [])
            return [self._normalize_trial(s) for s in studies]
        except Exception as e:
            logger.warning(f"ClinicalTrials search failed: {e}")
            return []

    def search_trials_by_company(self, company: str, page_size: int = 5) -> list[dict]:
        """Search trials where company is sponsor."""
        return self.search_trials(sponsor=company, page_size=page_size)

    @staticmethod
    def _normalize_trial(raw: dict) -> dict:
        """Normalize a ClinicalTrials.gov study record."""
        protocol = raw.get("protocolSection", {})
        id_module = protocol.get("identificationModule", {})
        status_module = protocol.get("statusModule", {})
        sponsor_module = protocol.get("sponsorCollaboratorsModule", {})
        conditions_module = protocol.get("conditionsModule", {})
        contacts_module = protocol.get("contactsLocationsModule", {})

        lead_sponsor = sponsor_module.get("leadSponsor", {})
        overall_officials = contacts_module.get("overallOfficials", [])
        central_contacts = contacts_module.get("centralContacts", [])

        investigators = [
            {
                "name": official.get("name", ""),
                "role": official.get("role", ""),
                "affiliation": official.get("affiliation", ""),
            }
            for official in overall_officials
        ]

        return {
            "nct_id": id_module.get("nctId", ""),
            "title": id_module.get("briefTitle", ""),
            "status": status_module.get("overallStatus", ""),
            "sponsor": lead_sponsor.get("name", ""),
            "conditions": conditions_module.get("conditions", []),
            "investigators": investigators,
            "contacts": [
                {"name": c.get("name", ""), "email": c.get("email", "")}
                for c in central_contacts
            ],
        }

    # ── PubMed E-utilities ───────────────────────────────

    def search_pubmed(
        self,
        affiliation: str,
        topic: str | None = None,
        max_results: int = 10,
    ) -> list[str]:
        """Search PubMed by affiliation. Returns list of PMIDs."""
        term_parts = [f'"{affiliation}"[affil]']
        if topic:
            term_parts.append(f'"{topic}"[tiab]')
        term = " AND ".join(term_parts)

        try:
            result = self._get(f"{self.PUBMED_BASE_URL}/esearch.fcgi", {
                "db": "pubmed",
                "term": term,
                "retmax": max_results,
                "retmode": "json",
                "sort": "date",
            })
            time.sleep(0.4)  # 3 req/sec limit
            return result.get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            logger.warning(f"PubMed search failed: {e}")
            return []

    def fetch_pubmed_summaries(self, pmids: list[str]) -> list[dict]:
        """Fetch article summaries for a list of PMIDs."""
        if not pmids:
            return []

        try:
            result = self._get(f"{self.PUBMED_BASE_URL}/esummary.fcgi", {
                "db": "pubmed",
                "id": ",".join(pmids[:50]),
                "retmode": "json",
            })
            time.sleep(0.4)

            summaries = []
            uid_list = result.get("result", {}).get("uids", [])
            for uid in uid_list:
                article = result["result"].get(uid, {})
                if isinstance(article, dict):
                    summaries.append(self._normalize_pubmed(article))
            return summaries
        except Exception as e:
            logger.warning(f"PubMed fetch failed: {e}")
            return []

    @staticmethod
    def _normalize_pubmed(raw: dict) -> dict:
        """Normalize a PubMed summary record."""
        authors = raw.get("authors", [])
        author_names = [a.get("name", "") for a in authors[:5]]
        return {
            "pmid": raw.get("uid", ""),
            "title": raw.get("title", ""),
            "journal": raw.get("fulljournalname", ""),
            "pub_date": raw.get("pubdate", ""),
            "authors": author_names,
        }

    # ── Aggregate per-company research ───────────────────

    def get_company_research_context(
        self,
        company: str,
        therapeutic_area: str | None = None,
    ) -> dict:
        """Get combined research data for a company (for Claude enrichment context).

        Returns: {company, active_trials, recent_publications, summary}
        """
        trials = self.search_trials_by_company(company, page_size=5)

        pmids = self.search_pubmed(company, topic=therapeutic_area, max_results=10)
        publications = self.fetch_pubmed_summaries(pmids) if pmids else []

        # Build a text summary for Claude context injection
        summary_parts = []
        if trials:
            conditions_list = []
            for t in trials:
                conditions_list.extend(t.get("conditions", []))
            unique_conditions = list(set(conditions_list))[:10]
            summary_parts.append(
                f"{company} has {len(trials)} active/recruiting clinical trials "
                f"in areas: {', '.join(unique_conditions)}."
            )
        if publications:
            summary_parts.append(
                f"Recent publications ({len(publications)} found): "
                f"topics include {publications[0].get('title', '')[:80]}..."
            )

        return {
            "company": company,
            "active_trials": trials,
            "recent_publications": publications,
            "summary": " ".join(summary_parts) if summary_parts else f"No public research data found for {company}.",
        }

    # ── Company Verification ──────────────────────────────

    def _web_search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search the web. Uses Tavily if API key available, falls back to DuckDuckGo.

        Returns [{title, href, body}, ...] (normalized format).
        Tavily also returns 'content' (full page extract) when available.
        """
        from config import TAVILY_API_KEY

        # Tavily (preferred) — higher quality, rate-limit friendly
        if TAVILY_API_KEY:
            try:
                from tavily import TavilyClient
                tc = TavilyClient(api_key=TAVILY_API_KEY)
                resp = tc.search(
                    query=query,
                    max_results=max_results,
                    include_answer=False,
                )
                results = []
                for r in resp.get("results", []):
                    results.append({
                        "title": r.get("title", ""),
                        "href": r.get("url", ""),
                        "body": r.get("content", "")[:500],
                    })
                return results
            except Exception as e:
                logger.warning(f"Tavily search failed for '{query}': {e}, falling back to DuckDuckGo")

        # DuckDuckGo (fallback)
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            time.sleep(1)
            return results
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed for '{query}': {e}")
            return []

    _LIST_KEYWORDS = {"top", "best", "list", "companies", "startups", "directory",
                       "leading", "market", "players", "vendors", "ranking"}

    def _fetch_page_text(self, url: str, max_chars: int = 3000) -> str:
        """Fetch a URL and extract plain text (best-effort)."""
        import requests as _req
        try:
            resp = _req.get(url, timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            resp.raise_for_status()
            # Strip HTML tags to get plain text
            import re as _re
            text = _re.sub(r"<script[^>]*>.*?</script>", "", resp.text, flags=_re.DOTALL)
            text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL)
            text = _re.sub(r"<[^>]+>", " ", text)
            text = _re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            return ""

    def search_for_targets(self, queries: list[str], max_per_query: int = 15,
                           progress_callback=None) -> str:
        """Run multiple web searches and compile results into a text context for RAG.

        For URLs that look like company lists/directories, fetches full page content
        to extract more company names.
        Returns a formatted string with deduplicated search results.
        """
        seen_urls = set()
        all_sections = []
        list_candidates = []  # URLs that likely contain company lists

        for i, query in enumerate(queries):
            if progress_callback:
                progress_callback(i, len(queries), query)
            results = self._web_search(query, max_results=max_per_query)
            section_items = []
            for r in results:
                url = r.get("href", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                title = r.get("title", "").strip()
                body = r.get("body", "").strip()[:300]
                if title or body:
                    section_items.append(f"- **{title}**\n  {body}\n  URL: {url}")
                # Check if this URL likely contains a company list
                title_lower = title.lower()
                if any(kw in title_lower for kw in self._LIST_KEYWORDS):
                    list_candidates.append({"url": url, "title": title})
            if section_items:
                all_sections.append(
                    f"### 검색: \"{query}\"\n" + "\n".join(section_items)
                )

        # Phase 2: Fetch full page content from list-type URLs (top 10)
        if list_candidates:
            if progress_callback:
                progress_callback(len(queries), len(queries), "회사 리스트 페이지 수집 중...")
            page_sections = []
            for lc in list_candidates[:10]:
                page_text = self._fetch_page_text(lc["url"])
                if page_text and len(page_text) > 200:
                    page_sections.append(
                        f"### 페이지: {lc['title']}\n{page_text}"
                    )
            if page_sections:
                all_sections.append(
                    "## 회사 리스트 페이지 (전체 내용)\n\n" + "\n\n".join(page_sections)
                )

        if all_sections:
            return "\n\n".join(all_sections)
        return "(웹 검색 결과 없음)"

    @staticmethod
    def _extract_english_keywords(text: str, max_words: int = 6) -> str:
        """Extract English/Latin words from mixed Korean/English text."""
        import re as _re
        # Match English words, numbers, hyphens (e.g. "psilocybin", "COMP360", "EEG")
        tokens = _re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", text)
        # Deduplicate while preserving order, skip very common words
        skip = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "has", "have", "been"}
        seen = set()
        keywords = []
        for t in tokens:
            low = t.lower()
            if low not in skip and low not in seen:
                seen.add(low)
                keywords.append(t)
            if len(keywords) >= max_words:
                break
        return " ".join(keywords)

    _PHARMA_KEYWORDS = {
        # English
        "pharma", "pharmaceutical", "biotech", "biotechnology", "biopharmaceutical",
        "drug", "clinical", "therapy", "therapeutic", "therapeutics",
        "oncology", "immunology", "neuroscience", "pipeline", "fda",
        "trial", "molecule", "antibody", "peptide", "protein",
        # Korean
        "제약", "바이오", "임상", "치료제", "신약", "약물", "항체",
    }

    @staticmethod
    def _is_pharma(company_name: str, evidence: str, reason: str) -> bool:
        """Check if a company is likely pharma/biotech based on text signals."""
        text = f"{company_name} {evidence} {reason}".lower()
        return any(kw in text for kw in ResearchClient._PHARMA_KEYWORDS)

    def verify_company(self, company: str, claimed_evidence: str = "",
                       reason: str = "") -> dict:
        """Verify a company's actual research activity against claimed evidence.

        - All companies: DuckDuckGo web search
        - Pharma/biotech only: additionally queries ClinicalTrials.gov + PubMed

        Returns: {company, trials_found, trial_conditions, publications_found,
                  pub_topics, web_results, factual_summary, status, is_pharma}
        status: "verified" | "partial" | "no_data"
        """
        is_pharma = self._is_pharma(company, claimed_evidence, reason)

        # 1) Web search — all companies
        eng_keywords = self._extract_english_keywords(claimed_evidence) if claimed_evidence else ""
        if eng_keywords:
            query = f'"{company}" {eng_keywords}'
        else:
            query = f'"{company}" company products pipeline'
        web_results = self._web_search(query, max_results=5)
        web_snippets = [
            {"title": r.get("title", ""), "snippet": r.get("body", "")[:200], "url": r.get("href", "")}
            for r in web_results
        ]

        # 2) Clinical trials — pharma/biotech only
        all_trials = []
        unique_conditions = []
        if is_pharma:
            for status in ["RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED"]:
                trials = self.search_trials(sponsor=company, status=status, page_size=5)
                all_trials.extend(trials)
                if len(all_trials) >= 10:
                    break

            trial_conditions = []
            for t in all_trials:
                trial_conditions.extend(t.get("conditions", []))
            unique_conditions = sorted(set(trial_conditions))

        # 3) PubMed — pharma/biotech only
        pmids = []
        publications = []
        pub_topics = []
        if is_pharma:
            pmids = self.search_pubmed(company, max_results=10)
            publications = self.fetch_pubmed_summaries(pmids) if pmids else []
            pub_topics = [p.get("title", "")[:100] for p in publications[:5]]

        # Build factual summary
        parts = []
        if web_snippets:
            desc = next((s["snippet"] for s in web_snippets if s["snippet"]), "")
            if desc:
                parts.append(f"웹: {desc[:150]}")
        if all_trials:
            parts.append(f"ClinicalTrials.gov: {len(all_trials)}건. "
                         f"적응증: {', '.join(unique_conditions[:6])}")
        if publications:
            parts.append(f"PubMed: {len(pmids)}건")

        # Determine status
        if all_trials:
            status = "verified"
        elif web_snippets or publications:
            status = "partial"
        else:
            status = "no_data"

        return {
            "company": company,
            "is_pharma": is_pharma,
            "web_results": web_snippets,
            "trials_found": len(all_trials),
            "trial_conditions": unique_conditions[:15],
            "trial_details": [
                {"nct_id": t["nct_id"], "title": t["title"][:80], "status": t["status"],
                 "conditions": t["conditions"]}
                for t in all_trials[:5]
            ],
            "publications_found": len(pmids),
            "pub_topics": pub_topics,
            "factual_summary": " | ".join(parts) if parts else "외부 데이터 없음",
            "status": status,
        }

    def verify_companies_batch(
        self,
        companies: list[dict],
        progress_callback=None,
    ) -> list[dict]:
        """Verify a batch of companies from AI recommendations.

        Args:
            companies: list of {name, reason, evidence} dicts from AI output
            progress_callback: optional callable(current, total, company_name)

        Returns: list of verification results with original + verification data
        """
        results = []
        total = len(companies)
        for i, company in enumerate(companies):
            name = company.get("name", "")
            if progress_callback:
                progress_callback(i, total, name)

            verification = self.verify_company(
                name, company.get("evidence", ""), company.get("reason", "")
            )
            results.append({
                **company,
                "verification": verification,
            })

        if progress_callback:
            progress_callback(total, total, "완료")

        return results

    # ── Researcher Verification ──────────────────────────

    def verify_researcher(self, name: str, institution: str = "",
                          research_area: str = "", claimed_evidence: str = "") -> dict:
        """Verify a researcher's actual activity against claimed evidence.

        - Web search for researcher name + institution
        - PubMed author search for publications
        - ClinicalTrials.gov for PI involvement

        Returns: {name, web_results, publications_found, pub_topics,
                  trials_found, trial_titles, factual_summary, status}
        status: "verified" | "partial" | "no_data"
        """
        # 1) Web search — researcher name + institution
        query_parts = [f'"{name}"']
        if institution:
            query_parts.append(f'"{institution}"')
        eng_kw = self._extract_english_keywords(claimed_evidence) if claimed_evidence else ""
        if eng_kw:
            query_parts.append(eng_kw)
        else:
            query_parts.append("professor researcher")
        web_results = self._web_search(" ".join(query_parts), max_results=5)
        web_snippets = [
            {"title": r.get("title", ""), "snippet": r.get("body", "")[:200], "url": r.get("href", "")}
            for r in web_results
        ]

        # 2) PubMed author search
        pubmed_term_parts = [f'"{name}"[Author]']
        if research_area:
            pubmed_term_parts.append(f'({research_area})[tiab]')
        pubmed_term = " AND ".join(pubmed_term_parts)
        pmids = []
        publications = []
        pub_topics = []
        try:
            result = self._get(f"{self.PUBMED_BASE_URL}/esearch.fcgi", {
                "db": "pubmed",
                "term": pubmed_term,
                "retmax": 10,
                "retmode": "json",
                "sort": "date",
            })
            time.sleep(0.4)
            pmids = result.get("esearchresult", {}).get("idlist", [])
            if pmids:
                publications = self.fetch_pubmed_summaries(pmids)
                pub_topics = [p.get("title", "")[:100] for p in publications[:5]]
        except Exception as e:
            logger.warning(f"PubMed author search failed for {name}: {e}")

        # 3) ClinicalTrials.gov — search as PI/investigator
        all_trials = []
        try:
            for status in ["RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED"]:
                trials = self.search_trials(condition=research_area or None,
                                            sponsor=institution or None,
                                            status=status, page_size=5)
                # Filter trials where this researcher appears as investigator
                for t in trials:
                    investigators = t.get("investigators", [])
                    for inv in investigators:
                        if name.lower() in inv.get("name", "").lower():
                            all_trials.append(t)
                            break
                if len(all_trials) >= 5:
                    break
        except Exception as e:
            logger.warning(f"ClinicalTrials search failed for {name}: {e}")

        # Build factual summary
        parts = []
        if web_snippets:
            desc = next((s["snippet"] for s in web_snippets if s["snippet"]), "")
            if desc:
                parts.append(f"웹: {desc[:150]}")
        if publications:
            parts.append(f"PubMed: {len(pmids)}건 (저자 검색)")
        if all_trials:
            parts.append(f"ClinicalTrials: PI로 {len(all_trials)}건 확인")

        # Determine status
        if publications or all_trials:
            status = "verified"
        elif web_snippets:
            status = "partial"
        else:
            status = "no_data"

        return {
            "name": name,
            "web_results": web_snippets,
            "publications_found": len(pmids),
            "pub_topics": pub_topics,
            "trials_found": len(all_trials),
            "trial_titles": [t["title"][:80] for t in all_trials[:5]],
            "factual_summary": " | ".join(parts) if parts else "외부 데이터 없음",
            "status": status,
        }

    def verify_researchers_batch(
        self,
        researchers: list[dict],
        progress_callback=None,
    ) -> list[dict]:
        """Verify a batch of researchers from AI recommendations.

        Args:
            researchers: list of {name, institution, research_area, evidence, ...}
            progress_callback: optional callable(current, total, researcher_name)

        Returns: list of researcher dicts with verification data added
        """
        results = []
        total = len(researchers)
        for i, researcher in enumerate(researchers):
            name = researcher.get("name", "")
            if progress_callback:
                progress_callback(i, total, name)

            verification = self.verify_researcher(
                name,
                researcher.get("institution", ""),
                researcher.get("research_area", ""),
                researcher.get("evidence", ""),
            )
            results.append({
                **researcher,
                "verification": verification,
            })

        if progress_callback:
            progress_callback(total, total, "완료")

        return results

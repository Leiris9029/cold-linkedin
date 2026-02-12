"""
Hunter.io API client - email finder, verifier, and domain search.

API reference: https://hunter.io/api-documentation/v2
All endpoints: GET https://api.hunter.io/v2/...
Auth: api_key query param.
"""
import logging
import time
import requests
from config import HUNTER_API_KEY

logger = logging.getLogger(__name__)


class HunterClient:
    BASE_URL = "https://api.hunter.io/v2"

    def __init__(self, api_key: str = HUNTER_API_KEY):
        self.api_key = api_key

    def _get(self, path: str, params: dict, max_retries: int = 3) -> dict:
        """GET with exponential backoff on rate limit (429)."""
        params["api_key"] = self.api_key
        for attempt in range(max_retries):
            resp = requests.get(f"{self.BASE_URL}{path}", params=params)
            if resp.status_code == 429:
                wait = min(2 ** attempt * 2, 60)
                logger.warning(f"Hunter rate limited, retrying in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise Exception("Hunter.io API rate limit exceeded after retries")

    # ── Email Finder (15 req/sec, 1 credit) ──────────────

    def find_email(self, domain: str, first_name: str, last_name: str) -> dict:
        """Find an email address for a person at a domain.

        Returns: {data: {email, score, position, ...}, meta: {...}}
        """
        result = self._get("/email-finder", {
            "domain": domain,
            "first_name": first_name,
            "last_name": last_name,
        })
        time.sleep(1)  # rate limit courtesy delay
        return result

    # ── Email Verifier (10 req/sec, 0.5 credits) ────────

    def verify_email(self, email: str) -> dict:
        """Verify an email address.

        Returns: {data: {status, result, score, email, ...}}
        status: deliverable | risky | undeliverable | unknown
        """
        result = self._get("/email-verifier", {
            "email": email,
        })
        time.sleep(1)
        return result

    # ── Domain Search (15 req/sec, 1 credit) ─────────────

    def search_domain(self, domain: str, limit: int = 100,
                      offset: int = 0, department: str = "",
                      seniority: str = "") -> dict:
        """Search for email addresses at a domain.

        Args:
            domain: Company domain (e.g. 'pfizer.com')
            limit: Max results per call (max 100, default 100)
            offset: Skip first N results (for pagination)
            department: Filter by department (e.g. 'executive', 'management', 'it')
            seniority: Filter by seniority (e.g. 'senior', 'executive', 'junior')
        """
        params = {"domain": domain, "limit": min(limit, 100)}
        if offset > 0:
            params["offset"] = offset
        if department:
            params["department"] = department
        if seniority:
            params["seniority"] = seniority
        result = self._get("/domain-search", params)
        time.sleep(1)
        return result

    # ── Batch Operations ─────────────────────────────────

    def batch_find_emails(self, prospects: list[dict],
                          all_prospects: list[dict] | None = None) -> list[dict]:
        """Find emails for prospects that are missing emails.

        Args:
            prospects: list of dicts with keys: id, contact_name, company, email, source_data.
            all_prospects: optional full list (including those WITH emails) to build domain map.

        Returns: list of dicts with found emails: [{prospect_id, email, confidence, hunter_score, source}]
        """
        # Build company→domain map from peers who already have emails
        domain_map = _build_domain_map(all_prospects or prospects)
        results = []
        consecutive_failures = 0

        for p in prospects:
            if p.get("email"):
                continue

            name_parts = p.get("contact_name", "").strip().split()
            if len(name_parts) < 2:
                continue

            first_name = name_parts[0]
            last_name = name_parts[-1]
            company = p.get("company", "")
            domain = _infer_domain(company, p.get("source_data"), domain_map)
            if not domain:
                logger.info(f"No domain found for {company}, skipping {first_name} {last_name}")
                continue

            try:
                resp = self.find_email(domain, first_name, last_name)
                consecutive_failures = 0
                data = resp.get("data", {})
                if data.get("email"):
                    score = data.get("score", 0)
                    results.append({
                        "prospect_id": p.get("id"),
                        "email": data["email"],
                        "confidence": _score_to_confidence(score),
                        "hunter_score": score,
                        "source": "hunter",
                    })
            except Exception as e:
                logger.warning(f"Hunter find_email failed for {first_name} {last_name} @ {domain}: {e}")
                if "rate limit" in str(e).lower():
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        logger.error(f"Hunter rate limit: {consecutive_failures} consecutive failures, stopping batch. "
                                     f"Found {len(results)} emails so far.")
                        break

        return results

    def batch_verify_emails(self, emails: list[str]) -> dict[str, dict]:
        """Verify a batch of email addresses.

        Returns: {email: {status, score, result}}
        Stops early if 3 consecutive rate limit failures occur.
        """
        results = {}
        consecutive_failures = 0
        for email_addr in emails:
            if not email_addr:
                continue
            try:
                resp = self.verify_email(email_addr)
                data = resp.get("data", {})
                results[email_addr] = {
                    "status": data.get("status", "unknown"),
                    "score": data.get("score", 0),
                    "result": data.get("result", "unknown"),
                }
                consecutive_failures = 0
            except Exception as e:
                logger.warning(f"Hunter verify failed for {email_addr}: {e}")
                results[email_addr] = {"status": "unknown", "score": 0, "result": "error"}
                if "rate limit" in str(e).lower():
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        logger.error(f"Hunter rate limit: {consecutive_failures} consecutive failures, stopping batch. "
                                     f"Verified {len(results)}/{len(emails)} so far.")
                        break
        return results


# ── Utility ──────────────────────────────────────────────

def _score_to_confidence(score: int) -> str:
    """Convert Hunter.io score (0-100) to our confidence level."""
    if score >= 90:
        return "verified"
    elif score >= 70:
        return "high"
    elif score >= 40:
        return "medium"
    return "low"


# Known Japanese pharma company domains
_KNOWN_DOMAINS = {
    "eisai": "eisai.com",
    "shionogi": "shionogi.co.jp",
    "daiichi sankyo": "daiichisankyo.com",
    "astellas": "astellas.com",
    "takeda": "takeda.com",
    "otsuka": "otsuka.co.jp",
    "sumitomo pharma": "sumitomo-pharma.co.jp",
    "sumitomo dainippon": "ds-pharma.co.jp",
    "chugai": "chugai-pharm.co.jp",
    "mitsubishi tanabe": "mt-pharma.co.jp",
    "ono pharmaceutical": "ono-pharma.com",
    "kyowa kirin": "kyowakirin.com",
    "taiho": "taiho.co.jp",
    "nippon shinyaku": "ns-pharma.co.jp",
    "meiji seika": "meiji-seika-pharma.co.jp",
    "mochida": "mochida.co.jp",
    "kissei": "kissei.co.jp",
    "sawai": "sawai.co.jp",
    "pfizer": "pfizer.com",
    "novartis": "novartis.com",
    "roche": "roche.com",
    "merck": "merck.com",
    "abbvie": "abbvie.com",
    "bms": "bms.com",
    "bristol-myers squibb": "bms.com",
    "lilly": "lilly.com",
    "eli lilly": "lilly.com",
    "janssen": "its.jnj.com",
    "johnson & johnson": "its.jnj.com",
    "sanofi": "sanofi.com",
    "astrazeneca": "astrazeneca.com",
    "gsk": "gsk.com",
    "glaxosmithkline": "gsk.com",
    "boehringer ingelheim": "boehringer-ingelheim.com",
    "bayer": "bayer.com",
    "amgen": "amgen.com",
    "gilead": "gilead.com",
    "regeneron": "regeneron.com",
    "biogen": "biogen.com",
    "vertex": "vrtx.com",
}


def _build_domain_map(prospects: list[dict]) -> dict[str, str]:
    """Build company→domain map from prospects that already have emails."""
    domain_map: dict[str, str] = {}
    for p in prospects:
        email = p.get("email", "")
        company = (p.get("company") or "").lower().strip()
        if email and company and "@" in email:
            domain = email.split("@")[-1].lower()
            # Skip generic email providers
            if domain not in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                              "live.com", "aol.com", "icloud.com", "mail.com"):
                domain_map[company] = domain

        # Also try Apollo org data
        sd = p.get("source_data")
        if sd and company and company not in domain_map:
            try:
                import json
                data = json.loads(sd) if isinstance(sd, str) else sd
                org = data.get("organization", {}) or {}
                # Try website_url → extract domain
                website = org.get("website_url", "") or ""
                if website:
                    from urllib.parse import urlparse
                    parsed = urlparse(website)
                    host = parsed.hostname or ""
                    if host.startswith("www."):
                        host = host[4:]
                    if host:
                        domain_map[company] = host
            except Exception:
                pass

    return domain_map


def _infer_domain(company: str, source_data: str | None = None,
                  domain_map: dict[str, str] | None = None) -> str:
    """Infer a company's email domain from multiple sources.

    Priority: 1) peer email domain map, 2) Apollo org data, 3) hardcoded _KNOWN_DOMAINS.
    """
    if not company:
        return ""
    company_lower = company.lower().strip()

    # 1) Check domain map built from peer emails
    if domain_map and company_lower in domain_map:
        return domain_map[company_lower]

    # 2) Check Apollo org data for this specific prospect
    if source_data:
        try:
            import json
            data = json.loads(source_data) if isinstance(source_data, str) else source_data
            org = data.get("organization", {}) or {}
            website = org.get("website_url", "") or ""
            if website:
                from urllib.parse import urlparse
                parsed = urlparse(website)
                host = parsed.hostname or ""
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    return host
        except Exception:
            pass

    # 3) Hardcoded known domains
    for key, domain in _KNOWN_DOMAINS.items():
        if key in company_lower:
            return domain

    return ""

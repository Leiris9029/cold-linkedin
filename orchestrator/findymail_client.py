"""
Findymail API client — email finder and verifier.

API: https://app.findymail.com/api
Auth: Bearer token in Authorization header.
Credits: 1 per verified email found (no charge if not found).

Endpoints used:
  POST /search/name     — find email by name + domain
  POST /search/linkedin — find email by LinkedIn URL
  POST /verify          — verify an email address
  GET  /credits         — check remaining credits
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import FINDYMAIL_API_KEY

logger = logging.getLogger(__name__)


class FindymailClient:
    BASE_URL = "https://app.findymail.com/api"

    # Retry config
    MAX_RETRIES = 3
    INITIAL_DELAY = 1.0
    BACKOFF_FACTOR = 2

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or FINDYMAIL_API_KEY
        if not self.api_key:
            raise ValueError("FINDYMAIL_API_KEY not set. Add it to .env")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, data: dict) -> dict:
        """POST with retry on rate limit (429)."""
        delay = self.INITIAL_DELAY
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{self.BASE_URL}{path}",
                    json=data,
                    headers=self._headers(),
                    timeout=30,
                )
                if resp.status_code == 429:
                    wait = min(delay, 10.0)
                    logger.warning(f"Findymail rate limited, retrying in {wait}s...")
                    time.sleep(wait)
                    delay *= self.BACKOFF_FACTOR
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                logger.warning(f"Findymail timeout, attempt {attempt + 1}/{self.MAX_RETRIES}")
            except requests.exceptions.RequestException as e:
                if attempt == self.MAX_RETRIES - 1:
                    raise
                logger.warning(f"Findymail error: {e}, retrying...")
                time.sleep(delay)
                delay *= self.BACKOFF_FACTOR

        raise Exception("Findymail API failed after retries")

    def _get(self, path: str) -> dict:
        """GET request (for credits check)."""
        resp = requests.get(
            f"{self.BASE_URL}{path}",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Public Methods ────────────────────────────────────

    def find_email(self, name: str, domain: str) -> dict:
        """Find a person's email by name + company domain.

        Args:
            name: Full name (e.g. "John Smith")
            domain: Company domain (e.g. "pfizer.com")

        Returns:
            {"email": "john@pfizer.com", "verified": True, ...} or
            {"email": None} if not found.
        Credits: 1 if found, 0 if not found.
        """
        result = self._post("/search/name", {
            "name": name,
            "domain": domain,
        })
        return result

    def find_email_by_linkedin(self, linkedin_url: str) -> dict:
        """Find a work email from a LinkedIn profile URL.

        Credits: 1 if found, 0 if not found.
        """
        result = self._post("/search/linkedin", {
            "linkedin_url": linkedin_url,
        })
        return result

    def verify_email(self, email: str) -> dict:
        """Verify an email address.

        Returns: {"email": "...", "status": "valid"|"invalid"|"unknown", ...}
        Credits: 1 verifier credit.
        """
        result = self._post("/verify", {
            "email": email,
        })
        return result

    def get_credits(self) -> dict:
        """Check remaining credits.

        Returns: {"credits": N, ...}
        """
        return self._get("/credits")

    # ── Batch Operations ──────────────────────────────────

    def batch_find_emails(
        self,
        prospects: list[dict],
        max_concurrent: int = 10,
    ) -> list[dict]:
        """Find emails for multiple prospects concurrently.

        Args:
            prospects: list of dicts with keys:
                - contact_name (str): Full name
                - company (str): Company name
                - domain (str, optional): Company domain
                - linkedin_url (str, optional): LinkedIn profile URL
            max_concurrent: Max parallel requests (default 10)

        Returns:
            list of dicts: [{
                "contact_name": "...",
                "email": "...",
                "company": "...",
                "domain": "...",
                "verified": True/False,
                "source": "findymail",
            }]
        """
        results = []
        consecutive_failures = 0

        def _find_one(prospect: dict) -> dict | None:
            name = prospect.get("contact_name", "").strip()
            domain = prospect.get("domain", "").strip()
            company = prospect.get("company", "")
            linkedin = prospect.get("linkedin_url", "").strip()

            if not name:
                return None

            # Try name+domain first, fall back to LinkedIn
            try:
                if domain:
                    resp = self.find_email(name, domain)
                elif linkedin:
                    resp = self.find_email_by_linkedin(linkedin)
                else:
                    return None

                email = resp.get("email")
                if email:
                    return {
                        "contact_name": name,
                        "email": email,
                        "company": company,
                        "domain": domain,
                        "verified": resp.get("verified", False),
                        "source": "findymail",
                    }
            except Exception as e:
                logger.warning(f"Findymail lookup failed for {name}: {e}")

            return None

        # Run concurrently
        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            future_to_prospect = {
                executor.submit(_find_one, p): p
                for p in prospects
                if not p.get("email")  # skip if email already known
            }

            for future in as_completed(future_to_prospect):
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                except Exception as e:
                    logger.warning(f"Findymail batch task failed: {e}")
                    consecutive_failures += 1

                if consecutive_failures >= 5:
                    logger.error("Too many consecutive failures, stopping batch")
                    break

        return results

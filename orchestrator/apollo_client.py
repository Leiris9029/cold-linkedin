"""
Apollo.io API client - handles people search, enrichment, and organization search.

API reference: https://apolloio.github.io/apollo-api-docs/
All endpoints: POST https://api.apollo.io/api/v1/...
Free tier: ~300 credits/month.
"""
import logging
import time
import requests
from config import APOLLO_API_KEY

logger = logging.getLogger(__name__)


class ApolloClient:
    BASE_URL = "https://api.apollo.io/api/v1"

    def __init__(self, api_key: str = APOLLO_API_KEY):
        self.api_key = api_key

    def _post(self, path: str, data: dict, max_retries: int = 3) -> dict:
        """POST with exponential backoff on rate limit (429)."""
        headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}
        for attempt in range(max_retries):
            resp = requests.post(f"{self.BASE_URL}{path}", json=data, headers=headers)
            if resp.status_code == 429:
                wait = min(2 ** attempt * 2, 60)
                logger.warning(f"Apollo rate limited, retrying in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise Exception("Apollo API rate limit exceeded after retries")

    # ── People Search ──────────────────────────────────────

    def search_people(
        self,
        person_titles: list[str] | None = None,
        person_locations: list[str] | None = None,
        organization_names: list[str] | None = None,
        q_keywords: str | None = None,
        per_page: int = 25,
        page: int = 1,
        reveal: bool = True,
    ) -> dict:
        """Search for people matching criteria and optionally reveal full details.

        Apollo's api_search returns preview data (masked names, no emails).
        When reveal=True, each person is enriched via people/match to get
        full name, email, LinkedIn URL, and location.

        Returns: {people: [...], pagination: {total_entries, per_page, current_page, ...}}
        """
        # api_search only supports single company name, so if multiple
        # companies are given, search each one and merge results.
        if organization_names and len(organization_names) > 1:
            return self._search_multi_org(
                organization_names, person_titles, person_locations,
                q_keywords, per_page, page, reveal,
            )

        # When searching by company name, don't send q_keywords to Apollo
        # (Apollo ANDs keywords with company name → 0 results).
        # Instead, apply keyword filtering locally after fetching results.
        has_org = bool(organization_names)

        data: dict = {
            "per_page": per_page,
            "page": page,
        }
        if person_titles:
            data["person_titles"] = person_titles
        if person_locations:
            data["person_locations"] = person_locations
        if organization_names:
            data["q_organization_name"] = organization_names[0]
        if q_keywords and not has_org:
            data["q_keywords"] = q_keywords

        result = self._post("/mixed_people/api_search", data)
        time.sleep(1)

        if reveal and result.get("people"):
            result["people"] = self._reveal_people(result["people"])

        # Local keyword filtering when company is specified
        if q_keywords and has_org and result.get("people"):
            result["people"] = self._filter_by_keywords(result["people"], q_keywords)

        return result

    def _search_multi_org(
        self, organization_names, person_titles, person_locations,
        q_keywords, per_page, page, reveal,
    ) -> dict:
        """Search across multiple organizations one at a time and merge.

        q_keywords is NOT sent to Apollo (it ANDs with company name → 0 results).
        Instead, keyword filtering is applied locally after reveal.
        """
        all_people = []
        per_org = max(3, per_page // len(organization_names))

        for org_name in organization_names:
            data: dict = {"per_page": per_org, "page": 1}
            if person_titles:
                data["person_titles"] = person_titles
            if person_locations:
                data["person_locations"] = person_locations
            data["q_organization_name"] = org_name

            try:
                result = self._post("/mixed_people/api_search", data)
                people = result.get("people", [])
                all_people.extend(people)
                logger.info(f"Apollo: {org_name} → {len(people)} people")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Apollo search failed for {org_name}: {e}")

        if reveal and all_people:
            all_people = self._reveal_people(all_people)

        # Local keyword filtering
        if q_keywords and all_people:
            before = len(all_people)
            all_people = self._filter_by_keywords(all_people, q_keywords)
            logger.info(f"Keyword filter: {before} → {len(all_people)} (keywords: {q_keywords})")

        return {"people": all_people, "total_entries": len(all_people)}

    @staticmethod
    def _filter_by_keywords(people: list[dict], keywords_str: str) -> list[dict]:
        """Filter people locally by checking if ANY keyword matches their profile.

        Checks: title, headline, departments, and seniority fields.
        Keywords are comma-separated and matched with OR logic (any match passes).
        """
        keywords = [k.strip().lower() for k in keywords_str.split(",") if k.strip()]
        if not keywords:
            return people

        filtered = []
        for person in people:
            # Build searchable text from person's profile
            searchable = " ".join(filter(None, [
                (person.get("title") or "").lower(),
                (person.get("headline") or "").lower(),
                " ".join(person.get("departments", []) or []).lower(),
                (person.get("seniority") or "").lower(),
            ]))
            # OR logic: pass if ANY keyword matches
            if any(kw in searchable for kw in keywords):
                filtered.append(person)

        return filtered

    def _reveal_people(self, previews: list[dict]) -> list[dict]:
        """Reveal full contact details for preview results via people/match."""
        revealed = []
        for preview in previews:
            pid = preview.get("id")
            if not pid:
                continue
            try:
                full = self._post("/people/match", {"id": pid})
                person = full.get("person", {})
                revealed.append(person if person else preview)
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Failed to reveal person {pid}: {e}")
                revealed.append(preview)
        return revealed

    # ── People Enrichment ──────────────────────────────────

    def enrich_person(
        self,
        first_name: str | None = None,
        last_name: str | None = None,
        organization_name: str | None = None,
        linkedin_url: str | None = None,
    ) -> dict:
        """Enrich a single person. Uses 1 credit per call."""
        data: dict = {}
        if first_name:
            data["first_name"] = first_name
        if last_name:
            data["last_name"] = last_name
        if organization_name:
            data["organization_name"] = organization_name
        if linkedin_url:
            data["linkedin_url"] = linkedin_url
        return self._post("/people/match", data)

    # ── Organization Search ────────────────────────────────

    def search_organizations(
        self,
        organization_locations: list[str] | None = None,
        q_organization_keyword_tags: list[str] | None = None,
        organization_num_employees_ranges: list[str] | None = None,
        per_page: int = 25,
        page: int = 1,
    ) -> dict:
        """Search for organizations matching criteria."""
        data: dict = {
            "per_page": per_page,
            "page": page,
        }
        if organization_locations:
            data["organization_locations"] = organization_locations
        if q_organization_keyword_tags:
            data["q_organization_keyword_tags"] = q_organization_keyword_tags
        if organization_num_employees_ranges:
            data["organization_num_employees_ranges"] = organization_num_employees_ranges

        result = self._post("/mixed_companies/search", data)
        time.sleep(1)
        return result

    # ── Utility ────────────────────────────────────────────

    @staticmethod
    def normalize_person(raw: dict) -> dict:
        """Convert Apollo API person response to standard prospect format."""
        org = raw.get("organization", {}) or {}
        return {
            "contact_name": f"{raw.get('first_name', '')} {raw.get('last_name', '')}".strip(),
            "email": raw.get("email", "") or "",
            "company": org.get("name", "") or raw.get("organization_name", "") or "",
            "title": raw.get("title", "") or "",
            "linkedin_url": raw.get("linkedin_url", "") or "",
            "location": ", ".join(filter(None, [
                raw.get("city", ""),
                raw.get("state", ""),
                raw.get("country", ""),
            ])),
            "source": "apollo",
        }

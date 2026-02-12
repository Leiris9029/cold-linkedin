"""
WHOIS domain lookup client - extracts registrant/admin emails from domain records.

Uses the python-whois library. Best for:
- Small companies where no data exists in Clay/Apollo
- Getting domain-level contact emails (admin@, info@)
- Verifying company domain ownership

Note: Many domains use privacy protection, so WHOIS emails are often redacted.
This is a FREE data source with no API key required.

pip install python-whois
"""
import logging
import re

logger = logging.getLogger(__name__)


class WhoisClient:
    """WHOIS domain lookup for contact email extraction."""

    _SKIP_PREFIXES = {
        "abuse@", "noreply@", "hostmaster@", "domaincontrol@",
        "dnsadmin@", "postmaster@", "no-reply@",
    }

    _PRIVACY_INDICATORS = {
        "whoisguard", "privacyguard", "proxy", "redacted",
        "withheld", "contactprivacy", "whoisprivacy",
        "domainprivacy", "identityprotect", "privacy-protect",
        "domainsbyproxy", "whoisprotection",
    }

    def lookup_domain(self, domain: str) -> dict:
        """Perform WHOIS lookup on a domain.

        Returns dict with domain info and extracted emails.
        """
        import whois  # lazy import

        try:
            w = whois.whois(domain)
        except Exception as e:
            logger.warning(f"WHOIS lookup failed for {domain}: {e}")
            return {"domain": domain, "error": str(e), "all_emails": []}

        # Extract emails
        raw_emails = w.emails if hasattr(w, "emails") and w.emails else []
        if isinstance(raw_emails, str):
            raw_emails = [raw_emails]

        # Filter out generic/privacy emails
        useful_emails = []
        is_privacy = False
        for email in raw_emails:
            email_lower = email.lower()
            if any(email_lower.startswith(skip) for skip in self._SKIP_PREFIXES):
                continue
            if any(priv in email_lower for priv in self._PRIVACY_INDICATORS):
                is_privacy = True
                continue
            useful_emails.append(email)

        # Check registrant org for privacy
        registrant_org = (getattr(w, "org", "") or "")
        if any(p in registrant_org.lower() for p in self._PRIVACY_INDICATORS):
            is_privacy = True

        return {
            "domain": domain,
            "registrant_name": getattr(w, "name", "") or "",
            "registrant_org": registrant_org,
            "registrant_email": useful_emails[0] if useful_emails else "",
            "admin_email": useful_emails[1] if len(useful_emails) > 1 else "",
            "all_emails": useful_emails,
            "is_privacy_protected": is_privacy,
            "registrar": getattr(w, "registrar", "") or "",
            "creation_date": str(getattr(w, "creation_date", "") or ""),
            "expiration_date": str(getattr(w, "expiration_date", "") or ""),
        }

    def find_contact_emails(self, domain: str) -> list[dict]:
        """Extract usable contact emails from WHOIS data.

        Returns list of: {email, source, confidence, type}
        """
        result = self.lookup_domain(domain)

        contacts = []
        if result.get("registrant_email"):
            contacts.append({
                "email": result["registrant_email"],
                "source": "whois_registrant",
                "confidence": "low",
                "type": "registrant",
            })
        if result.get("admin_email") and result["admin_email"] != result.get("registrant_email"):
            contacts.append({
                "email": result["admin_email"],
                "source": "whois_admin",
                "confidence": "low",
                "type": "admin",
            })

        return contacts

    @staticmethod
    def infer_domain_from_company(company_name: str) -> str:
        """Best-effort domain inference from company name.

        E.g. 'Acme Corp' -> 'acme.com' (guess, needs verification)
        """
        clean = re.sub(
            r"\b(inc|corp|co|ltd|llc|gmbh|ag|sa|srl|pty|"
            r"pharmaceutical[s]?|pharma|kabushiki kaisha|kk)\b",
            "", company_name.lower(), flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(r"[^a-z0-9]", "", clean)
        if clean:
            return f"{clean}.com"
        return ""

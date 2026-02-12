"""
GMass API client - handles campaign creation, sending, and report retrieval.

API reference: https://api.gmass.co/docs
All endpoints: https://api.gmass.co/api/...?apikey=KEY
"""
import requests
from typing import Optional
from config import GMASS_API_KEY, GMASS_BASE_URL, GMASS_FROM_EMAIL, GMASS_FROM_NAME


class GMassClient:
    def __init__(self, api_key: str = GMASS_API_KEY):
        self.api_key = api_key
        self.base_url = GMASS_BASE_URL

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}?apikey={self.api_key}"

    def _post(self, path: str, data: dict) -> dict:
        resp = requests.post(self._url(path), json=data)
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self._url(path)
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Transactional (single email) ─────────────────────

    def send_single(
        self,
        to: str,
        subject: str,
        body: str,
        from_email: str = GMASS_FROM_EMAIL,
        from_name: str = GMASS_FROM_NAME,
        open_tracking: bool = True,
        click_tracking: bool = True,
    ) -> dict:
        """Send a single transactional email via GMass."""
        data = {
            "fromEmail": from_email,
            "fromName": from_name,
            "to": to,
            "subject": subject,
            "message": body,
            "openTracking": open_tracking,
            "clickTracking": click_tracking,
        }
        return self._post("/transactional", data)

    # ── List Management ──────────────────────────────────

    def create_list(
        self,
        spreadsheet_id: str,
        worksheet_id: str,
        update_sheet: bool = True,
        keep_duplicates: bool = False,
        filter_criteria: Optional[str] = None,
    ) -> dict:
        """Create a GMass list from a Google Sheet."""
        sheet_source = {
            "spreadsheetId": spreadsheet_id,
            "worksheetId": str(worksheet_id),
            "UpdateSheet": update_sheet,
            "KeepDuplicates": keep_duplicates,
        }
        if filter_criteria:
            sheet_source["FilterCriteria"] = filter_criteria
        data = {
            "listSource": {
                "listSourceSheet": sheet_source,
            }
        }
        return self._post("/lists", data)

    def get_lists(self) -> dict:
        """Retrieve all existing GMass lists."""
        return self._get("/lists")

    # ── Campaign Draft ───────────────────────────────────

    def create_draft(
        self,
        list_address: str,
        subject: str,
        message: str,
        from_email: str = GMASS_FROM_EMAIL,
        message_type: str = "html",
        open_tracking: bool = True,
        click_tracking: bool = True,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
    ) -> dict:
        """Create a campaign draft."""
        data = {
            "listAddress": list_address,
            "subject": subject,
            "message": message,
            "fromEmail": from_email,
            "messageType": message_type,
            "openTracking": open_tracking,
            "clickTracking": click_tracking,
        }
        if cc:
            data["cc"] = cc
        if bcc:
            data["bcc"] = bcc
        return self._post("/campaigndrafts", data)

    def create_draft_with_addresses(
        self,
        email_addresses: str,
        subject: str,
        message: str,
        from_email: str = GMASS_FROM_EMAIL,
        message_type: str = "html",
        open_tracking: bool = True,
        click_tracking: bool = True,
    ) -> dict:
        """Create a campaign draft with direct email addresses (comma-separated)."""
        data = {
            "emailAddresses": email_addresses,
            "subject": subject,
            "message": message,
            "fromEmail": from_email,
            "messageType": message_type,
            "openTracking": open_tracking,
            "clickTracking": click_tracking,
        }
        return self._post("/campaigndrafts", data)

    # ── Campaign Send ────────────────────────────────────

    def send_campaign(self, draft_id: str) -> dict:
        """Send a campaign from a draft."""
        return self._post(f"/campaigns/{draft_id}", {})

    # ── Campaign Reports ─────────────────────────────────

    def _extract_data(self, response: dict | list) -> list:
        """Extract 'data' array from GMass report response ({metadata, data} structure)."""
        if isinstance(response, dict) and "data" in response:
            return response["data"]
        if isinstance(response, list):
            return response
        return []

    def get_campaigns(self) -> list:
        """Get all campaigns."""
        return self._get("/campaigns")

    def get_campaign(self, campaign_id: str) -> dict:
        """Get a single campaign and its aggregate statistics."""
        return self._get(f"/campaigns/{campaign_id}")

    def get_campaign_recipients(self, campaign_id: str) -> list:
        """Get recipient-level data for a campaign."""
        return self._extract_data(self._get(f"/reports/{campaign_id}/recipients"))

    def get_campaign_opens(self, campaign_id: str) -> list:
        """Get recipients that opened a campaign."""
        return self._extract_data(self._get(f"/reports/{campaign_id}/opens"))

    def get_campaign_replies(self, campaign_id: str) -> list:
        """Get recipients that replied to a campaign."""
        return self._extract_data(self._get(f"/reports/{campaign_id}/replies"))

    def get_campaign_bounces(self, campaign_id: str) -> list:
        """Get recipients that bounced."""
        return self._extract_data(self._get(f"/reports/{campaign_id}/bounces"))

    def get_campaign_clicks(self, campaign_id: str) -> list:
        """Get recipients that clicked a URL."""
        return self._extract_data(self._get(f"/reports/{campaign_id}/clicks"))

    def get_campaign_unsubscribes(self, campaign_id: str) -> list:
        """Get recipients that unsubscribed."""
        return self._extract_data(self._get(f"/reports/{campaign_id}/unsubscribes"))

    def get_campaign_blocks(self, campaign_id: str) -> list:
        """Get blocked recipients."""
        return self._extract_data(self._get(f"/reports/{campaign_id}/blocks"))

    # ── Google Sheets ────────────────────────────────────

    def get_sheets(self) -> dict:
        """List all Google Sheets accessible by the GMass account."""
        return self._get("/sheets")

    def get_worksheets(self, sheet_id: str) -> dict:
        """Get worksheets within a specific Google Sheet."""
        return self._get(f"/sheets/{sheet_id}/worksheets")

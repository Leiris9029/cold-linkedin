"""
Google Sheets client - uploads CSV data to Google Sheets for GMass integration.

GMass reads recipient lists from Google Sheets, so we upload our CSV there
and then point GMass at the sheet.

Strategy: Use a single pre-shared spreadsheet ("ColdMail Campaign") owned by
the user's personal Google account. The service account has editor access.
Each campaign gets its own worksheet tab within that spreadsheet.

Requires: Google Service Account with Sheets API enabled.
"""
import csv
import gspread
from google.oauth2.service_account import Credentials
from config import GOOGLE_SERVICE_ACCOUNT_JSON, GMASS_FROM_EMAIL, GOOGLE_SPREADSHEET_ID

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# The shared spreadsheet name (must already exist and be shared with the service account)
DEFAULT_SPREADSHEET_NAME = "ColdMail Campaign"


class SheetsClient:
    def __init__(self, service_account_json: str = GOOGLE_SERVICE_ACCOUNT_JSON):
        creds = Credentials.from_service_account_file(service_account_json, scopes=SCOPES)
        self.gc = gspread.authorize(creds)

    def upload_csv(
        self,
        csv_path: str,
        spreadsheet_name: str = DEFAULT_SPREADSHEET_NAME,
        worksheet_name: str = "Sheet1",
    ) -> tuple[str, str]:
        """
        Upload a CSV file to a worksheet in the shared Google Spreadsheet.

        Returns:
            (spreadsheet_id, worksheet_id) for GMass list creation.
        """
        # Read CSV
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            raise ValueError(f"CSV is empty: {csv_path}")

        # Open existing shared spreadsheet by ID (avoids name collision)
        if GOOGLE_SPREADSHEET_ID:
            spreadsheet = self.gc.open_by_key(GOOGLE_SPREADSHEET_ID)
        else:
            spreadsheet = self.gc.open(spreadsheet_name)

        # Get or create worksheet
        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
            worksheet.clear()
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=max(len(rows), 100), cols=max(len(rows[0]), 10)
            )

        # Upload all rows
        worksheet.update(range_name="A1", values=rows)

        return spreadsheet.id, str(worksheet.id)

    def upload_mailmerge_csv(
        self,
        csv_path: str,
        campaign_name: str = "Campaign",
    ) -> tuple[str, str]:
        """
        Upload a mailmerge CSV as a new worksheet tab in the shared spreadsheet.

        The worksheet name is the campaign_name (e.g., "ColdMail_260204").
        GMass uses column names for mail merge:
        - {subject}, {body} become merge fields.
        - The 'email' column is used as the recipient address.

        Returns:
            (spreadsheet_id, worksheet_id)
        """
        return self.upload_csv(
            csv_path,
            spreadsheet_name=DEFAULT_SPREADSHEET_NAME,
            worksheet_name=campaign_name,
        )

    def read_tracking_data(self, spreadsheet_id: str, worksheet_name: str = "Sheet1") -> list[dict]:
        """
        Read tracking data from a Google Sheet that GMass has updated.
        GMass adds columns like MERGED_STATUS, OPENED, CLICKED, REPLIED, BOUNCED.

        Returns:
            List of dicts with per-recipient tracking data.
        """
        spreadsheet = self.gc.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(worksheet_name)
        return worksheet.get_all_records()

"""
Google Sheets client - uploads CSV data to Google Sheets for GMass integration.

GMass reads recipient lists from Google Sheets, so we upload our CSV there
and then point GMass at the sheet.

Requires: Google Service Account with Sheets API enabled.
"""
import csv
import io
import gspread
from google.oauth2.service_account import Credentials
from config import GOOGLE_SERVICE_ACCOUNT_JSON, GMASS_FROM_EMAIL

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsClient:
    def __init__(self, service_account_json: str = GOOGLE_SERVICE_ACCOUNT_JSON):
        creds = Credentials.from_service_account_file(service_account_json, scopes=SCOPES)
        self.gc = gspread.authorize(creds)

    def upload_csv(
        self,
        csv_path: str,
        spreadsheet_name: str = "ColdMail Campaign",
        worksheet_name: str = "Sheet1",
    ) -> tuple[str, str]:
        """
        Upload a CSV file to Google Sheets.

        Returns:
            (spreadsheet_id, worksheet_id) for GMass list creation.
        """
        # Read CSV
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            raise ValueError(f"CSV is empty: {csv_path}")

        # Create or open spreadsheet
        try:
            spreadsheet = self.gc.open(spreadsheet_name)
        except gspread.SpreadsheetNotFound:
            spreadsheet = self.gc.create(spreadsheet_name)
            # Share with the GMass account email so GMass can access it
            if GMASS_FROM_EMAIL:
                spreadsheet.share(GMASS_FROM_EMAIL, perm_type="user", role="writer")

        # Get or create worksheet
        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
            worksheet.clear()
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=worksheet_name, rows=len(rows), cols=len(rows[0])
            )

        # Upload all rows
        worksheet.update(range_name="A1", values=rows)

        return spreadsheet.id, str(worksheet.id)

    def upload_mailmerge_csv(
        self,
        csv_path: str,
        campaign_name: str = "ColdMail Campaign",
    ) -> tuple[str, str]:
        """
        Upload the coldmails_mailmerge.csv specifically.
        The CSV has columns: to_name, to_email, company, subject, body

        GMass uses column names to do mail merge:
        - {to_name}, {company}, {subject}, {body} become merge fields.
        - The 'to_email' column is used as the recipient address.

        Returns:
            (spreadsheet_id, worksheet_id)
        """
        return self.upload_csv(csv_path, spreadsheet_name=campaign_name)

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

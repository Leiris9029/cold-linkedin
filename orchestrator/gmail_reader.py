"""
Gmail IMAP reader - fetches reply emails from Gmail inbox.

Uses IMAP with App Password to read incoming replies.
This allows the UI to show the actual reply content alongside campaign tracking data.

Setup:
1. Enable 2-Step Verification on your Google account
2. Generate an App Password: https://myaccount.google.com/apppasswords
3. Add to .env:
   GMAIL_ADDRESS=leiris@risorious.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""
import imaplib
import email
import email.message
from email.header import decode_header
import logging
from datetime import datetime, timedelta
from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD

logger = logging.getLogger(__name__)

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993


class GmailReader:
    def __init__(self, address: str = GMAIL_ADDRESS, app_password: str = GMAIL_APP_PASSWORD):
        self.address = address
        self.app_password = app_password
        self._mail = None

    def _connect(self):
        """Connect to Gmail IMAP server."""
        if not self.address or not self.app_password:
            raise ValueError(
                "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env. "
                "See: https://myaccount.google.com/apppasswords"
            )
        self._mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        self._mail.login(self.address, self.app_password)

    def _disconnect(self):
        """Close IMAP connection."""
        if self._mail:
            try:
                self._mail.logout()
            except Exception:
                pass
            self._mail = None

    def _decode_header_value(self, value: str) -> str:
        """Decode email header (handles encoded subjects etc.)."""
        if not value:
            return ""
        decoded_parts = decode_header(value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(part)
        return "".join(result)

    def _extract_body(self, msg: email.message.Message) -> str:
        """Extract plain text body from email message."""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in content_disposition:
                    continue

                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")
                        break
                elif content_type == "text/html" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")

        return body.strip()

    def find_reply_from(self, sender_email: str, days_back: int = 30) -> dict | None:
        """
        Find the most recent email from a specific sender.

        Returns dict with: subject, body, date, from_email
        Or None if not found.
        """
        try:
            self._connect()
            self._mail.select("INBOX")

            # Search for emails from this sender within date range
            since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
            search_criteria = f'(FROM "{sender_email}" SINCE {since_date})'

            status, message_ids = self._mail.search(None, search_criteria)
            if status != "OK" or not message_ids[0]:
                return None

            # Get the most recent one (last in list)
            ids = message_ids[0].split()
            latest_id = ids[-1]

            status, msg_data = self._mail.fetch(latest_id, "(RFC822)")
            if status != "OK":
                return None

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = self._decode_header_value(msg.get("Subject", ""))
            from_header = self._decode_header_value(msg.get("From", ""))
            date_header = msg.get("Date", "")
            body = self._extract_body(msg)

            return {
                "subject": subject,
                "body": body,
                "date": date_header,
                "from_email": sender_email,
                "from_header": from_header,
            }

        except Exception as e:
            logger.error(f"Failed to read reply from {sender_email}: {e}")
            return None
        finally:
            self._disconnect()

    def find_all_replies(self, sender_emails: list[str], days_back: int = 30) -> dict[str, dict]:
        """
        Find replies from multiple senders.

        Returns dict mapping email address → reply data.
        """
        results = {}
        try:
            self._connect()
            self._mail.select("INBOX")

            since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")

            for sender in sender_emails:
                try:
                    search_criteria = f'(FROM "{sender}" SINCE {since_date})'
                    status, message_ids = self._mail.search(None, search_criteria)

                    if status != "OK" or not message_ids[0]:
                        continue

                    ids = message_ids[0].split()
                    latest_id = ids[-1]

                    status, msg_data = self._mail.fetch(latest_id, "(RFC822)")
                    if status != "OK":
                        continue

                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    subject = self._decode_header_value(msg.get("Subject", ""))
                    from_header = self._decode_header_value(msg.get("From", ""))
                    date_header = msg.get("Date", "")
                    body = self._extract_body(msg)

                    results[sender] = {
                        "subject": subject,
                        "body": body,
                        "date": date_header,
                        "from_email": sender,
                        "from_header": from_header,
                    }

                except Exception as e:
                    logger.warning(f"Error fetching reply from {sender}: {e}")
                    continue

        except Exception as e:
            logger.error(f"IMAP connection error: {e}")
        finally:
            self._disconnect()

        return results

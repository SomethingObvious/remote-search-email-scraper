import os
import time
import base64
import pickle
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Scopes for accessing Gmail
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def authenticate_gmail():
    """Authenticate and create a service to interact with the Gmail API."""
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    service = build('gmail', 'v1', credentials=creds)
    return service

def get_messages(service, query):
    """Retrieve unread messages matching the query."""
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    return messages

def get_message_content(service, msg_id):
    """Retrieve and print the content of a specific message."""
    message = service.users().messages().get(userId='me', id=msg_id).execute()
    for part in message['payload']['parts']:
        if part['mimeType'] == 'text/plain':
            msg_content = part['body']['data']
            msg_content = base64.urlsafe_b64decode(msg_content.encode('ASCII')).decode('utf-8')
            print(f"Message content: {msg_content}")

def monitor_emails(service, sender_email):
    """Monitor emails from a specific sender indefinitely."""
    print(f"Monitoring emails from: {sender_email}")
    while True:
        try:
            messages = get_messages(service, f'from:{sender_email} is:unread')
            if messages:
                for msg in messages:
                    get_message_content(service, msg['id'])
            time.sleep(30)  # Check every 30 seconds
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)  # Wait longer if an error occurs

if __name__ == '__main__':
    service = authenticate_gmail()
    sender_email = "specific-email@example.com"  # Replace with the email you want to monitor
    monitor_emails(service, sender_email)

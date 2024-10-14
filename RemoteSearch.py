import os
import time
import base64
import pickle
import re
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

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

def clean_text(text):
    """Remove specific phrases and replace consecutive newlines with a single space."""
    # Define regex patterns to remove unwanted phrases
    phrases_to_remove = [
        r'\bRogers MMS\b',
        r'\bThis message is brought to you by\b',
        r'\bRogers\b'
    ]
    # Combine the patterns into a single regex
    combined_pattern = '|'.join(phrases_to_remove)
    
    # Remove specific phrases
    text = re.sub(combined_pattern, '', text, flags=re.IGNORECASE)
    
    # Replace consecutive newlines with a single space
    text = re.sub(r'\n+', ' ', text)
    
    # Strip leading and trailing spaces
    return text.strip()

def extract_text_from_html(html_content):
    """Extract all text content from the HTML email."""
    soup = BeautifulSoup(html_content, 'html.parser')
    text = soup.get_text()
    return clean_text(text)

def get_message_content(service, msg_id):
    """Retrieve and print the content of a specific message."""
    message = service.users().messages().get(userId='me', id=msg_id).execute()
    payload = message['payload']

    # Check if there are multiple parts in the message
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/html':  # We're focusing on the HTML part
                html_content = base64.urlsafe_b64decode(part['body']['data'].encode('ASCII')).decode('utf-8')
                extracted_content = extract_text_from_html(html_content)
                if extracted_content:
                    print(f"Extracted Content: {extracted_content}")
                else:
                    print("No content found in the HTML message.")
    else:
        print("No multipart or HTML content found.")

def get_label_id(service, label_name):
    """Retrieve the ID of a label by its name."""
    try:
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        for label in labels:
            if label['name'].lower() == label_name.lower():
                return label['id'], label['name']  # Return label ID and name
        print(f"Label '{label_name}' not found.")
    except Exception as e:
        print(f"Error fetching label ID: {e}")
    return None, None

def scrape_latest_email(service, label_id, label_name):
    """Scrape the most recent email from a specific label."""
    try:
        # Search for the latest message under the specified label
        results = service.users().messages().list(userId='me', labelIds=[label_id], maxResults=1).execute()
        messages = results.get('messages', [])
        if messages:
            latest_email_id = messages[0]['id']
            return latest_email_id
        else:
            print(f"No messages found under label: {label_name}")
            return None
    except Exception as e:
        print(f"Error scraping latest email: {e}")
        return None

def monitor_emails(service, label_id, label_name):
    """Monitor emails under a specific label indefinitely."""
    print(f"Monitoring emails in label: {label_name} (ID: {label_id})")
    last_scraped_email_id = None  # Initialize last scraped email ID

    while True:
        try:
            latest_email_id = scrape_latest_email(service, label_id, label_name)

            # Compare latest email ID with the last scraped email ID
            if latest_email_id and latest_email_id != last_scraped_email_id:
                print(f"New email detected with ID: {latest_email_id}.")
                get_message_content(service, latest_email_id)  # Process the new email
                last_scraped_email_id = latest_email_id  # Update the last scraped email ID
            
            time.sleep(10)  # Check every 30 seconds
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)  # Wait longer if an error occurs

if __name__ == '__main__':
    service = authenticate_gmail()
    label_name = 'Remote Server'  # Replace with your label name

    # Fetch the correct label ID for "Remote Server"
    label_id, label_name = get_label_id(service, label_name)

    if label_id:
        # Start monitoring emails under the "Remote Server" label
        monitor_emails(service, label_id, label_name)
    else:
        print(f"Label '{label_name}' not found.")

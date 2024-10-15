import os
import time
import base64
import pickle
import re
import requests  # Import requests for web scraping
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from twilio.rest import Client  # Import Twilio Client
import urllib.parse  # Import urllib for URL parsing

def load_config(filename):
    """Load configuration from a specified file."""
    config = {}
    with open(filename, 'r') as f:
        for line in f:
            if line.strip() and not line.startswith('#'):  # Ignore empty lines and comments
                key, value = line.strip().split('=', 1)
                config[key.strip()] = value.strip()
    return config

# Load configuration
config = load_config('config.txt')

# Twilio configuration
TWILIO_ACCOUNT_SID = config.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = config.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_FROM = config.get('TWILIO_PHONE_FROM')
PHONE_TO = config.get('PHONE_TO')

# Scopes for accessing Gmail
SCOPES = [config.get('GMAIL_SCOPE')]

def authenticate_gmail():
    """Authenticate and create a service to interact with the Gmail API."""
    creds = None
    if os.path.exists(config.get('GMAIL_TOKEN_FILE')):
        with open(config.get('GMAIL_TOKEN_FILE'), 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(config.get('GMAIL_CREDENTIALS_FILE'), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(config.get('GMAIL_TOKEN_FILE'), 'wb') as token:
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
    phrases_to_remove = [
        r'\bRogers MMS\b',
        r'\bThis message is brought to you by\b',
        r'\bRogers\b'
    ]
    combined_pattern = '|'.join(phrases_to_remove)
    text = re.sub(combined_pattern, '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n+', ' ', text)
    return text.strip()

def extract_text_from_html(html_content):
    """Extract all text content from the HTML email."""
    soup = BeautifulSoup(html_content, 'html.parser')
    text = soup.get_text()
    return clean_text(text)

def clean_paragraph(paragraph):
    """Remove any occurrences of text in square brackets followed by numbers or letters."""
    return re.sub(r'\[[A-Za-z0-9]+\]', '', paragraph).strip()  # Remove patterns like [2], [abc], [2abc], etc.

def search_and_extract(query):
    """Search the web for the query and extract paragraphs until the total character count exceeds 500.
       Handles cases for Quora and Reddit searches with specific extraction logic.
    """
    headers = {'User-Agent': 'Mozilla/5.0'}  # Mimic a browser
    
    # Adjust the query URL based on whether it's for Quora or Reddit
    if 'quora' in query.lower():
        search_url = f"https://www.google.com/search?q={query}"  # No '+wiki' for Quora
    elif 'reddit' in query.lower():
        search_url = f"https://www.google.com/search?q={query}"  # No '+wiki' for Reddit
    else:
        search_url = f"https://www.google.com/search?q={query}+wiki"  # Default to wiki searches

    print(f"Searching URL: {search_url}")  # Print the URL being used for the search
    response = requests.get(search_url, headers=headers)

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find all search result links
        search_results = soup.find_all('a')

        # Look for the first valid link
        for result in search_results:
            link = result.get('href')
            if link and 'url?q=' in link:
                # Extract the actual URL from the link
                link_url = link.split('url?q=')[1].split('&')[0]
                link_url = urllib.parse.unquote(link_url)  # Decode the URL

                # For Quora, extract text from q-box elements and their nested spans
                if 'quora.com' in link_url:
                    print(f"Following Quora link: {link_url}")
                    page_response = requests.get(link_url, headers=headers)
                    if page_response.status_code == 200:
                        page_soup = BeautifulSoup(page_response.text, 'html.parser')
                        
                        # Find elements with class "q-box qu-userSelect--text"
                        quora_texts = page_soup.find_all('span', class_='q-box qu-userSelect--text', limit=2)
                        print(quora_texts)
                        accumulated_text = ""
                        
                        for span in quora_texts:
                            # Find nested spans inside the "q-box" span and extract their text
                            nested_spans = span.find_all('span')
                            for nested_span in nested_spans:
                                accumulated_text += clean_paragraph(nested_span.get_text()) + " "
                        
                        return accumulated_text.strip() if accumulated_text else "No suitable content found in Quora response."

                # For Reddit, extract content based on ID pattern
                elif 'reddit.com' in link_url:
                    print(f"Following Reddit link: {link_url}")
                    page_response = requests.get(link_url, headers=headers)
                    if page_response.status_code == 200:
                        page_soup = BeautifulSoup(page_response.text, 'html.parser')

                        # Find the first div where the id contains both 't3' and 'post-rtjson-content'
                        reddit_post = page_soup.find('div', id=lambda x: x and 't3' in x and 'post-rtjson-content' in x)
                        if reddit_post:
                            accumulated_text = clean_paragraph(reddit_post.get_text())
                            return accumulated_text.strip() if accumulated_text else "No suitable content found in Reddit post."

                # For Wikipedia or general search, continue with the original logic
                elif "wikipedia.org" in link_url:
                    print(f"Following Wikipedia link: {link_url}")
                    page_response = requests.get(link_url, headers=headers)
                    if page_response.status_code == 200:
                        page_soup = BeautifulSoup(page_response.text, 'html.parser')
                        paragraphs = page_soup.find_all('p')

                        # Initialize variables to store accumulated text
                        accumulated_text = ""
                        first_paragraph_found = False  # Flag to check if the first paragraph with <b> is found
                        
                        for p in paragraphs:
                            # Only process the first paragraph that contains <b>
                            if not first_paragraph_found and p.find('b'):
                                cleaned_text = clean_paragraph(p.get_text())  # Clean the paragraph text
                                accumulated_text += cleaned_text + " "  # Append cleaned text
                                first_paragraph_found = True  # Set the flag to True

                            # Append additional paragraphs until character limit is reached
                            if first_paragraph_found:
                                cleaned_text = clean_paragraph(p.get_text())  # Clean the paragraph text
                                accumulated_text += cleaned_text + " "
                                
                            # Check if the total length exceeds 500 characters
                            if len(accumulated_text) > 500:
                                return accumulated_text.strip()  # Return the accumulated text if it exceeds 500 characters

                    break  # Exit after following the first valid Wikipedia link

    return None  # Return None if no suitable content is found


def send_sms(message):
    """Send an SMS using Twilio."""
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)  # Create a Twilio client
    try:
        message = client.messages.create(
            to=PHONE_TO,
            from_=TWILIO_PHONE_FROM,
            body=message  # The content of the SMS
        )
        print(f"Message sent: {message.sid}")
    except Exception as e:
        print(f"Failed to send SMS: {e}")

def get_message_content(service, msg_id, is_first_message):
    """Retrieve and print the content of a specific message."""
    message = service.users().messages().get(userId='me', id=msg_id).execute()
    payload = message['payload']

    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/html':
                html_content = base64.urlsafe_b64decode(part['body']['data'].encode('ASCII')).decode('utf-8')
                extracted_content = extract_text_from_html(html_content)

                if is_first_message:
                    send_sms("Initialized")  # Send "Initialized" instead of the extracted content
                    print("Initialized message sent to Twilio.")
                elif extracted_content:
                    # Search for the extracted content on the web and send the first <p> containing <b>
                    search_result = search_and_extract(extracted_content)
                    if search_result:
                        send_sms(search_result)  # Send SMS with the search result
                    else:
                        print("No suitable paragraph found in the search results.")
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
    is_first_message = True  # Flag to check if it's the first message

    while True:
        try:
            latest_email_id = scrape_latest_email(service, label_id, label_name)

            if latest_email_id and latest_email_id != last_scraped_email_id:
                print(f"New email detected with ID: {latest_email_id}.")
                get_message_content(service, latest_email_id, is_first_message)  # Get and send message content
                last_scraped_email_id = latest_email_id  # Update the last scraped email ID
                is_first_message = False  # Set to False after the first message
                
            time.sleep(5)  # Check for new emails every 10 seconds
        except Exception as e:
            print(f"Error during email monitoring: {e}")

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

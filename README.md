# Email Monitor and SMS Notifier

This Python project monitors a specified Gmail label for new unread emails and sends notifications via SMS when new messages are detected. It extracts relevant information from the email content, performs a web search for additional context, and then sends the results to a designated phone number using Twilio.

## Features

- Monitors a specified Gmail label for unread messages.
- Sends SMS notifications when new emails are detected.
- Extracts and cleans text content from HTML emails.
- Performs a web search to find additional context and information related to the email content.
- Configurable via a `config.txt` file for sensitive credentials.

## Requirements

- Python 3.x
- Required Python packages:
  - `google-auth`
  - `google-auth-oauthlib`
  - `google-api-python-client`
  - `requests`
  - `beautifulsoup4`
  - `twilio`

You can install the required packages using pip:

```bash
pip install google-auth google-auth-oauthlib google-api-python-client requests beautifulsoup4 twilio
```

## Configuration

Create a `config.txt` file in the root of the project directory with the following structure:

```
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_PHONE_FROM=your_twilio_phone_number
PHONE_TO=recipient_phone_number
```

Replace the placeholders with your actual Twilio account SID, auth token, Twilio phone number, and the recipient's phone number.

## Usage

1. **Authenticate with Gmail**:
   - The first time you run the script, it will prompt you to log in to your Google account and authorize access to the Gmail API. A token will be saved for future access.

2. **Run the script**:
   - Execute the script using Python:
   ```bash
   python your_script.py
   ```

3. **Monitor emails**:
   - The script will continuously monitor the specified Gmail label for new unread emails and send notifications via SMS when new messages are detected.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request for any enhancements or bug fixes.

## Acknowledgments

- [Twilio](https://www.twilio.com/) for SMS notifications.
- [Google](https://developers.google.com/gmail/api) for the Gmail API.
- [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/) for HTML parsing.

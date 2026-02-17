import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from cryptography.fernet import Fernet
from flask import current_app

def get_fernet():
    """Returns a Fernet instance using the app's encryption key."""
    key = current_app.config['ENCRYPTION_KEY']
    if not key:
        raise ValueError("ENCRYPTION_KEY not set in configuration")
    return Fernet(key.encode() if isinstance(key, str) else key)

def encrypt_password(password: str) -> str:
    """Encrypts a password."""
    cipher = get_fernet()
    return cipher.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password: str) -> str:
    """Decrypts a password."""
    cipher = get_fernet()
    return cipher.decrypt(encrypted_password.encode()).decode()

def send_email_smtp(sender_email, sender_password, recipient_email, subject, html_content, attachments=None):
    """
    Sends an email using Gmail SMTP with optional attachment support.
    attachments: List of file paths (strings)
    Returns (True, None) on success, or (False, error_message) on failure.
    """
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = recipient_email
    msg['Subject'] = subject
    
    msg.attach(MIMEText(html_content, 'html'))
    
    if attachments:
        for filepath in attachments:
            try:
                if os.path.exists(filepath):
                   with open(filepath, "rb") as attachment:
                        part = MIMEBase("application", "octet-stream")
                        part.set_payload(attachment.read())
                    
                   encoders.encode_base64(part)
                   filename = os.path.basename(filepath)
                   part.add_header(
                        "Content-Disposition",
                        f"attachment; filename= {filename}",
                   )
                   msg.attach(part)
                else:
                    print(f"Attachment not found: {filepath}")
            except Exception as e:
                print(f"Error attaching file {filepath}: {e}")

    try:
        # Gmail SMTP configuration
        smtp_server = "smtp.gmail.com"
        smtp_port = 465
        
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipient_email, msg.as_string())
        
        return True, None
    except Exception as e:
        return False, str(e)

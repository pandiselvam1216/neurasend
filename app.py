from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, current_app, send_file
from dotenv import load_dotenv
load_dotenv()
from config import Config
from models import db, Settings, Campaign, EmailLog, CampaignAttachment
from utils import encrypt_password, decrypt_password, send_email_smtp
import pandas as pd
import threading
import time
import os
import re
from io import BytesIO
from datetime import datetime
from werkzeug.utils import secure_filename
from sqlalchemy import func

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# ensure upload folder exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Create tables on startup (Essential for Vercel/Serverless where persistence is ephemeral)
with app.app_context():
    db.create_all()

@app.template_filter('clean_error')
def clean_error_filter(s):
    if not s: return ""
    # Simplify common errors
    if 'getaddrinfo failed' in str(s): return "Connection Failed: Invalid Host"
    if '11001' in str(s): return "DNS Error: Check Internet or Hostname"
    if '10060' in str(s): return "Connection Timed Out"
    if '10061' in str(s): return "Connection Refused"
    # Fallback: Strip [Errno X]
    import re
    return re.sub(r'\[Errno \d+\]\s*', '', str(s))

# --- Global Error Handlers ---
@app.errorhandler(500)
def internal_error(error):
    # Log the actual error for debugging
    print(f"Server Error: {error}")
    return render_template('error.html'), 500

@app.errorhandler(Exception)
def handle_exception(e):
    # pass through HTTP errors
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e

    # handle non-HTTP errors (like DB connection, ProgrammingError)
    print(f"Unhandled Exception: {e}")
    return render_template('error.html'), 500

def send_campaign_background(app, campaign_id, base_url, sender_email=None, sender_password=None):
    """Background task to send emails for a campaign."""
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        
        # Resolve Credentials (Explicit > DB)
        if not sender_email or not sender_password:
            settings = Settings.query.first()
            if settings:
                sender_email = sender_email or settings.email
                try:
                    if not sender_password:
                        sender_password = decrypt_password(settings.encrypted_password)
                except Exception as e:
                    print(f"Decryption failed: {e}")
                    return
        
        if not campaign or not sender_email or not sender_password:
            print(f"Campaign {campaign_id} missing or Credentials missing.")
            return

        pending_emails = EmailLog.query.filter_by(campaign_id=campaign_id, status='pending').all()
        
        # Get attachments
        attachments = [att.filepath for att in campaign.attachments]

        for email_log in pending_emails:
            # Personalization
            subject = campaign.subject
            content = campaign.content_html
            
            # 1. Merge Tags
            if email_log.merge_data:
                try:
                    for key, value in email_log.merge_data.items():
                        if value:
                            placeholder = "{{" + str(key) + "}}"
                            subject = subject.replace(placeholder, str(value))
                            content = content.replace(placeholder, str(value))
                except Exception as e:
                    print(f"Personalization error: {e}")

            # 2. Inject Tracking Pixel (Open Rate)
            # Create tracking URL: base_url/track/open/<log_id>
            # We must use the log.id.
            
            tracking_pixel_url = f"{base_url}/track/open/{email_log.id}"
            tracking_pixel_html = f'<img src="{tracking_pixel_url}" width="1" height="1" style="display:none;" />'
            
            # Append to end of content
            if "</body>" in content:
                content = content.replace("</body>", f"{tracking_pixel_html}</body>")
            else:
                content += tracking_pixel_html

            # 3. Wrap Links (Click Rate) - Simple Regex
            # Find all <a href="..."> tags
            # We need to be careful not to break mailto: or layout links
            # Regex to find hrefs that start with http/https
            def replace_link(match):
                original_url = match.group(1)
                # Skip if already tracked or special protocol (though regex limits to http)
                if '/track/' in original_url: return match.group(0)
                
                # Encode target URL
                from urllib.parse import quote
                encoded_url = quote(original_url)
                tracking_link = f"{base_url}/track/click/{email_log.id}?url={encoded_url}"
                return f'href="{tracking_link}"'

            # Regex: href=" (http[s]?://...?) "
            # Handles double quotes. Todo: handle single quotes too if needed.
            content = re.sub(r'href="(http[s]?://[^"]+)"', replace_link, content)
            
            success, error = send_email_smtp(
                sender_email, 
                sender_password, 
                email_log.email, 
                subject, 
                content,
                attachments=attachments
            )
            
            if success:
                email_log.status = 'sent'
                campaign.sent_count += 1
            else:
                email_log.status = 'failed'
                email_log.error_message = error
                campaign.failed_count += 1
            
            db.session.commit()
            
            # Anti-blocking delay
            time.sleep(3) 

@app.route('/')
def dashboard():
    campaigns = Campaign.query.order_by(Campaign.created_at.desc()).all()
    return render_template('dashboard.html', campaigns=campaigns)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    settings = Settings.query.first()
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if settings:
            settings.email = email
            settings.encrypted_password = encrypt_password(password)
        else:
            new_settings = Settings(email=email, encrypted_password=encrypt_password(password))
            db.session.add(new_settings)
        
        db.session.commit()
        flash('Settings updated successfully!', 'success')
        return redirect(url_for('settings'))
        
    return render_template('settings.html', settings=settings)

@app.route('/campaign/new', methods=['GET', 'POST'])
def new_campaign():
    if request.method == 'POST':
        subject = request.form.get('subject')
        content = request.form.get('content') 
        csv_file = request.files.get('csv_file')
        manual_emails_raw = request.form.get('manual_emails')
        attachment_files = request.files.getlist('attachments')
        
        emails = []

        # 1. Process CSV/Excel if provided
        if csv_file and csv_file.filename:
            try:
                filename = csv_file.filename.lower()
                if filename.endswith('.csv'):
                    df = pd.read_csv(csv_file)
                elif filename.endswith(('.xls', '.xlsx')):
                    df = pd.read_excel(csv_file)
                else:
                    flash('Invalid file type. Please upload a CSV or Excel file.', 'error')
                    return redirect(url_for('new_campaign'))
                # Normalize column names to lowercase for easier matching, but keep original for data
                df.columns = [c.strip() for c in df.columns]
                
                email_col = next((col for col in df.columns if 'email' in col.lower()), None)
                
                if email_col:
                    # Convert to list of dicts: [{'email': '...', 'name': '...', ...}, ...]
                    records = df.where(pd.notnull(df), None).to_dict(orient='records')
                    
                    for record in records:
                        email = record.get(email_col)
                        if email:
                            # Add to emails list for unique check check (simple string list)
                            # But we need to store the full record. 
                            # Strategy: Store record in a separate dict mapped by email
                            if email not in [e['email'] for e in emails if isinstance(e, dict)]:
                                # Normalize record keys? No, keep as is.
                                # But we need to rename email_col to 'email' for internal consistency if it differs
                                record['email'] = email 
                                emails.append(record)
                else:
                     flash('CSV must contain an "email" column.', 'error')
                     return redirect(url_for('new_campaign'))

            except Exception as e:
                flash(f'Error processing CSV: {str(e)}', 'error')
                return redirect(url_for('new_campaign'))

        # 2. Process Manual Emails
        if manual_emails_raw:
            # Split by comma or newline, strip whitespace
            manual_list = [e.strip() for e in manual_emails_raw.replace('\n', ',').split(',') if e.strip()]
            for email in manual_list:
                emails.append({'email': email}) # No merge data for manual yet

        # Remove duplicates (distinct by email)
        unique_emails = {}
        for entry in emails:
            if isinstance(entry, dict) and entry.get('email'):
                unique_emails[entry['email']] = entry 
        
        final_list = list(unique_emails.values())

        if not final_list:
            flash('No recipients found! Please upload a CSV or enter emails manually.', 'error')
            return redirect(url_for('new_campaign'))
            
        # Create Campaign
        campaign = Campaign(
            subject=subject,
            content_html=content.replace('\n', '<br>'), 
            total_emails=len(final_list)
        )
        db.session.add(campaign)
        db.session.commit()
        
        # Handle Attachments
        if attachment_files:
            for file in attachment_files:
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(filepath)
                    
                    attachment = CampaignAttachment(
                        campaign_id=campaign.id,
                        filename=filename,
                        filepath=filepath
                    )
                    db.session.add(attachment)
            db.session.commit()

        # Create Email Logs
        for entry in final_list:
            # entry is a dict {'email': '...', 'name': '...', ...}
            email_address = entry['email']
            # Remove email from merge_data to avoid redundancy (optional, but cleaner)
            # Keep all data including email for personalization
            merge_data = entry
            
            log = EmailLog(
                campaign_id=campaign.id,
                email=email_address.strip(),
                status='pending',
                merge_data=merge_data 
            )
            db.session.add(log)
        db.session.commit()
        
        flash(f'Campaign created with {len(emails)} emails!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('campaign.html')

@app.route('/campaign/<int:campaign_id>/send', methods=['POST'])
def send_campaign(campaign_id):
    # Try to get credentials from the request (Stateless/LocalStorage support)
    sender_email = request.form.get('sender_email')
    sender_password = request.form.get('sender_password')

    # Fallback to DB if not provided
    if not sender_email or not sender_password:
        settings = Settings.query.first()
        if not settings:
            flash('Please configure settings first!', 'error')
            return redirect(url_for('settings'))
    
    # Pass credentials to background task
    base_url = request.url_root.rstrip('/')
    thread = threading.Thread(
        target=send_campaign_background, 
        args=(current_app._get_current_object(), campaign_id, base_url, sender_email, sender_password)
    )
    thread.start()
    
    flash('Campaign started! Emails are being sent in the background.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/campaign/<int:campaign_id>/status')
def campaign_status(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    return jsonify({
        'total': campaign.total_emails,
        'sent': campaign.sent_count,
        'failed': campaign.failed_count,
        'status': 'completed' if (campaign.sent_count + campaign.failed_count) >= campaign.total_emails else 'processing'
    })

@app.route('/api/send-test', methods=['POST'])
def send_test_email():
    """Send a single test email to the sender."""
    if not Settings.query.first():
        return jsonify({'error': 'Please configure settings first!'}), 400
        
    data = request.json
    subject = data.get('subject')
    content = data.get('content')
    
    if not subject or not content:
        return jsonify({'error': 'Subject and Content are required'}), 400
        
    settings = Settings.query.first()
    sender_email = settings.email
    try:
        sender_password = decrypt_password(settings.encrypted_password)
        # Send to self
        success, error = send_email_smtp(
            sender_email, 
            sender_password, 
            sender_email, # recipient is self
            f"[TEST] {subject}", 
            content.replace('\n', '<br>'),
            attachments=[] # No attachments for quick test
        )
        
        if success:
            return jsonify({'message': f'Test email sent to {sender_email}'})
        else:
            return jsonify({'error': f'Failed to send: {error}'}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/campaign/<int:campaign_id>')
def campaign_report(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    logs = EmailLog.query.filter_by(campaign_id=campaign_id).all()
    return render_template('report.html', campaign=campaign, logs=logs)

@app.route('/campaign/<int:campaign_id>/export')
def export_campaign_csv(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    logs = EmailLog.query.filter_by(campaign_id=campaign_id).all()
    
    data = []
    for log in logs:
        data.append({
            'Email': log.email,
            'Status': log.status,
            'Error': log.error_message or '',
            'Time': log.sent_at.strftime('%Y-%m-%d %H:%M:%S') if log.sent_at else ''
        })
        
    df = pd.DataFrame(data)
    
    # Send CSV file
    from io import BytesIO
    from flask import send_file
    
    output = BytesIO()
    df.to_csv(output, index=False)
    output.seek(0)
    
    return send_file(
        output,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'campaign_{campaign_id}_report.csv'
    )

@app.route('/settings/reset_db', methods=['POST'])
def reset_database():
    """Danger Zone: Clear all campaign data but keep settings."""
    verification_email = request.form.get('verification_email')
    settings = Settings.query.first()
    
    if not settings or settings.email != verification_email:
        flash('Verification failed: Incorrect email address.', 'error')
        return redirect(url_for('settings'))

    try:
        db.session.query(EmailLog).delete()
        db.session.query(CampaignAttachment).delete()
        db.session.query(Campaign).delete()
        db.session.commit()
        flash('Database reset successfully! All campaigns have been removed.', 'success')
        # Clean up upload folder
        for filename in os.listdir(app.config['UPLOAD_FOLDER']):
             file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
             try:
                 if os.path.isfile(file_path):
                     os.unlink(file_path)
             except Exception:
                 pass
    except Exception as e:
        db.session.rollback()
        flash(f'Error resetting database: {str(e)}', 'error')
        
    return redirect(url_for('settings'))

@app.route('/status')
def system_status_page():
    return render_template('status.html')

@app.route('/api/system-status')
def system_status_api():
    """Returns database connection status and size."""
    try:
        # Check Connection
        db.session.execute(func.now())
        db_status = "Connected"
        
        # Check Size (Postgres vs SQLite)
        db_size = "Unknown"
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        
        if 'sqlite' in db_url:
             # SQLite: Get file size
             db_path = db_url.replace('sqlite:///', '')
             if os.path.exists(db_path):
                 size_bytes = os.path.getsize(db_path)
                 db_size = f"{size_bytes / (1024*1024):.2f} MB"
        else:
             # Postgres: Query size
             try:
                 result = db.session.execute(func.pg_size_pretty(func.pg_database_size(func.current_database()))).scalar()
                 db_size = result
             except Exception:
                 db_size = "Unknown (Permissions)"

        return jsonify({
            'database_status': db_status,
            'database_size': db_size,
            'database_type': 'PostgreSQL' if 'postgres' in db_url else 'SQLite'
        })
    except Exception as e:
        return jsonify({
            'database_status': f"Error: {str(e)}",
            'database_size': "N/A",
            'database_type': "Unknown"
        }), 500

# --- Tracking Routes ---
@app.route('/track/open/<int:log_id>')
def track_open(log_id):
    """Records an email open event."""
    try:
        log = EmailLog.query.get(log_id)
        if log and not log.opened_at:
            log.opened_at = datetime.utcnow()
            db.session.commit()
    except Exception as e:
        print(f"Tracking error: {e}")
    # Return 1x1 transparent pixel
    return send_file(BytesIO(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\xd4\xd6\xe7\x00\x00\x00\x00IEND\xaeB`\x82'), mimetype='image/png')

@app.route('/track/click/<int:log_id>')
def track_click(log_id):
    """Records a link click and redirects."""
    target_url = request.args.get('url')
    if not target_url:
        return "Invalid Link", 400
        
    try:
        log = EmailLog.query.get(log_id)
        if log:
            log.clicked_at = datetime.utcnow() # Update last clicked
            # Append to history
            current_clicks = log.links_clicked or []
            if isinstance(current_clicks, list):
                 current_clicks.append({'url': target_url, 'time': datetime.utcnow().isoformat()})
                 log.links_clicked = current_clicks
            
            # Use flag_modified for JSON mutable tracking if needed, 
            # but re-assigning usually works in recent SQLAlchemy
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(log, "links_clicked")
            
            db.session.commit()
    except Exception as e:
        print(f"Click tracking error: {e}")
        
    return redirect(target_url)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)

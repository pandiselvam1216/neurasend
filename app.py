from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, current_app
from config import Config
from models import db, Settings, Campaign, EmailLog, CampaignAttachment
from utils import encrypt_password, decrypt_password, send_email_smtp
import pandas as pd
import threading
import time
import os
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

def send_campaign_background(app, campaign_id, sender_email=None, sender_password=None):
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
            success, error = send_email_smtp(
                sender_email, 
                sender_password, 
                email_log.email, 
                campaign.subject, 
                campaign.content_html,
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

        # 1. Process CSV if provided
        if csv_file and csv_file.filename:
            try:
                df = pd.read_csv(csv_file)
                if 'email' not in df.columns:
                    email_col = next((col for col in df.columns if 'email' in col.lower()), None)
                    if email_col:
                        df = df.rename(columns={email_col: 'email'})
                
                if 'email' in df.columns:
                    emails.extend(df['email'].dropna().unique().tolist())
            except Exception as e:
                flash(f'Error processing CSV: {str(e)}', 'error')
                return redirect(url_for('new_campaign'))

        # 2. Process Manual Emails
        if manual_emails_raw:
            # Split by comma or newline, strip whitespace
            manual_list = [e.strip() for e in manual_emails_raw.replace('\n', ',').split(',') if e.strip()]
            emails.extend(manual_list)

        # Remove duplicates
        emails = list(set(emails))

        if not emails:
            flash('No recipients found! Please upload a CSV or enter emails manually.', 'error')
            return redirect(url_for('new_campaign'))
            
        # Create Campaign
        campaign = Campaign(
            subject=subject,
            content_html=content.replace('\n', '<br>'), 
            total_emails=len(emails)
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
        for email in emails:
            log = EmailLog(
                campaign_id=campaign.id,
                email=email.strip(),
                status='pending'
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
    thread = threading.Thread(
        target=send_campaign_background, 
        args=(current_app._get_current_object(), campaign_id, sender_email, sender_password)
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

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)

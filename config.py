import os
from cryptography.fernet import Fernet

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or os.urandom(24)
    basedir = os.path.abspath(os.path.dirname(__file__))
    # Database Configuration
    # 1. Prefer DATABASE_URL (for Production/Vercel with Postgres)
    # 2. Fallback to SQLite (for Local Development)
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    
    if not SQLALCHEMY_DATABASE_URI:
        # If no DB URL is set, use SQLite
        if os.environ.get('VERCEL'):
             # Vercel Read-Only File System Fix (Ephemeral!)
             SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/database.db'
             UPLOAD_FOLDER = '/tmp/uploads'
        else:
             # Local Development
             SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'database.db')
             UPLOAD_FOLDER = os.path.join(basedir, 'uploads')
    else:
        # If using Postgres, use /tmp for uploads on Vercel
        if os.environ.get('VERCEL'):
            UPLOAD_FOLDER = '/tmp/uploads'
        else:
            UPLOAD_FOLDER = os.path.join(basedir, 'uploads')
            
    # Fix for Postgres URLs starting with postgres:// (SQLAlchemy requires postgresql://)
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload size
    
    # Encryption key for sensitive data (Settings)
    ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY') or Fernet.generate_key().decode()

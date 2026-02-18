import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get('DATABASE_URL')

def check_columns():
    if not url:
        print("Error: DATABASE_URL not found.")
        return

    try:
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'email_log';")
        columns = [row[0] for row in cur.fetchall()]
        
        print(f"Columns in 'email_log': {columns}")
        
        required = ['opened_at', 'clicked_at', 'links_clicked']
        missing = [col for col in required if col not in columns]
        
        if missing:
            print(f"❌ MISSING COLUMNS: {missing}")
            # Attempt last ditch fix
            conn.autocommit = True
            for col in missing:
                 print(f"Attemping to add {col}...")
                 type_ = 'JSON' if col == 'links_clicked' else 'TIMESTAMP'
                 try:
                    cur.execute(f"ALTER TABLE email_log ADD COLUMN IF NOT EXISTS {col} {type_};")
                    print(f"✅ Added {col}")
                 except Exception as e:
                    print(f"Failed to add {col}: {e}")
        else:
            print("✅ All tracking columns present.")

        conn.close()
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    check_columns()

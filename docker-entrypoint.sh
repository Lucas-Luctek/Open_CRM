#!/bin/bash
set -e

# Créer les dossiers persistants
mkdir -p /data/uploads /data/backups

# Initialiser la BDD et appliquer le mot de passe admin
python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, '/app')

from app import init_db
import sqlite3
from werkzeug.security import generate_password_hash

init_db()

admin_pass = os.environ.get('ADMIN_PASSWORD', '')
if admin_pass:
    db_name = os.path.join(os.environ.get('DATA_DIR', ''), 'crm.db') if os.environ.get('DATA_DIR') else 'crm.db'
    conn = sqlite3.connect(db_name)
    c = conn.cursor()
    c.execute("UPDATE users SET password = ? WHERE username = 'admin'", (generate_password_hash(admin_pass),))
    conn.commit()
    conn.close()
    print("[CRM] Mot de passe admin configuré.")

print("[CRM] Base de données prête.")
PYEOF

exec "$@"

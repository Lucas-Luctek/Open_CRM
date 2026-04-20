#!/usr/bin/env python3
"""Sauvegarde quotidienne de la base de données SQLite du CRM.

Usage:
    python backup.py

Ou via cron (sauvegarde quotidienne à 2h du matin):
    0 2 * * * cd /chemin/vers/crm && /chemin/vers/venv/bin/python backup.py
"""
import shutil
import os
from datetime import datetime

_DATA_DIR = os.environ.get('DATA_DIR', '')
BASE_DIR = _DATA_DIR if _DATA_DIR else os.path.dirname(os.path.abspath(__file__))
DB_SRC = os.path.join(BASE_DIR, 'crm.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
MAX_BACKUPS = 30

os.makedirs(BACKUP_DIR, exist_ok=True)

if not os.path.exists(DB_SRC):
    print(f"Base de données introuvable: {DB_SRC}")
    exit(1)

timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
dst = os.path.join(BACKUP_DIR, f'crm_{timestamp}.db')
shutil.copy2(DB_SRC, dst)
print(f'[{datetime.now()}] Backup créé : {dst}')

# Conserver seulement les MAX_BACKUPS derniers
backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
for old in backups[:-MAX_BACKUPS]:
    os.remove(os.path.join(BACKUP_DIR, old))
    print(f'[{datetime.now()}] Ancien backup supprimé : {old}')

#!/bin/bash
# ============================================================
# Script d'installation du CRM Open Source
# Testé sur Ubuntu 22.04 / Debian 12
# ============================================================

set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║      Installation du CRM Open Source  ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Choix du mot de passe admin ──────────────────────────
echo "─── Compte administrateur ───"
while true; do
    read -s -p "Choisissez le mot de passe admin (min. 6 caractères) : " ADMIN_PASS
    echo ""
    if [ ${#ADMIN_PASS} -lt 6 ]; then
        echo "  Mot de passe trop court, réessayez."
        continue
    fi
    read -s -p "Confirmez le mot de passe : " ADMIN_PASS2
    echo ""
    if [ "$ADMIN_PASS" != "$ADMIN_PASS2" ]; then
        echo "  Les mots de passe ne correspondent pas, réessayez."
    else
        break
    fi
done
echo "  ✓ Mot de passe enregistré."
echo ""

# ── 2. Choix du port ────────────────────────────────────────
echo "─── Port de l'application ───"
read -p "Port d'écoute [5000 par défaut, appuyez sur Entrée pour garder 5000] : " APP_PORT
APP_PORT=${APP_PORT:-5000}
echo "  ✓ Port : $APP_PORT"
echo ""

# ── 3. Vérifier Python 3 + dépendances système ──────────────
echo "─── Vérification de Python 3 ───"
if ! command -v python3 &>/dev/null; then
    echo "  Python3 non trouvé. Installation..."
    apt-get update -qq && apt-get install -y python3
fi
# Installer python3.X-venv systématiquement (ensurepip absent sur Debian minimal)
PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
apt-get install -y "python${PYTHON_VER}-venv" -qq
echo "  ✓ $(python3 --version)"
echo ""

echo "─── Bibliothèques système (WeasyPrint) ───"
apt-get install -y -qq \
    libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libffi-dev shared-mime-info fonts-liberation
# libgdk-pixbuf : nom différent selon la version Debian
apt-get install -y -qq libgdk-pixbuf-2.0-0 2>/dev/null || \
apt-get install -y -qq libgdk-pixbuf2.0-0 2>/dev/null || true
echo "  ✓ Bibliothèques installées."
echo ""

# ── 4. Créer l'environnement virtuel ────────────────────────
echo "─── Environnement virtuel ───"
if [ ! -f "venv/bin/activate" ]; then
    echo "  Création de l'environnement virtuel..."
    rm -rf venv
    python3 -m venv venv
fi
source venv/bin/activate
echo "  ✓ Environnement virtuel prêt."
echo ""

# ── 5. Installer les dépendances ────────────────────────────
echo "─── Installation des dépendances ───"
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✓ Dépendances installées."
echo ""

# ── 6. Créer le fichier .env ────────────────────────────────
echo "─── Configuration ───"
if [ ! -f ".env" ]; then
    cp .env.example .env
fi
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sed -i "s/changez-cette-cle-en-production-svp/$SECRET/" .env
# Mettre à jour le port dans .env
if grep -q "^PORT=" .env; then
    sed -i "s/^PORT=.*/PORT=$APP_PORT/" .env
else
    echo "PORT=$APP_PORT" >> .env
fi
echo "  ✓ Fichier .env configuré (clé secrète + port $APP_PORT)."
echo ""

# ── 7. Initialiser la base et définir le mot de passe ───────
echo "─── Initialisation de la base de données ───"
python3 - <<PYEOF
import sys, os
sys.path.insert(0, '.')
# Charger les variables .env manuellement
if os.path.exists('.env'):
    for line in open('.env'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

from app import init_db
import sqlite3
from werkzeug.security import generate_password_hash

init_db()

conn = sqlite3.connect('crm.db')
c = conn.cursor()
hashed = generate_password_hash('${ADMIN_PASS}')
c.execute("UPDATE users SET password = ? WHERE username = 'admin'", (hashed,))
conn.commit()
conn.close()
print("  ✓ Base de données initialisée.")
print("  ✓ Mot de passe admin configuré.")
PYEOF

# ── 8. Configurer le service systemd ────────────────────────
echo "─── Service systemd ───"
INSTALL_DIR="$(pwd)"
cat > /etc/systemd/system/crm.service <<EOF
[Unit]
Description=CRM Open Source
After=network.target

[Service]
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable crm --quiet
systemctl restart crm
echo "  ✓ Service CRM démarré et activé au démarrage."
echo ""

echo "╔══════════════════════════════════════════════════╗"
echo "║              Installation terminée !             ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║                                                  ║"
echo "║  Le CRM est démarré automatiquement.             ║"
echo "║                                                  ║"
echo "║  Commandes utiles :                              ║"
echo "║    systemctl status crm                          ║"
echo "║    systemctl restart crm                         ║"
echo "║    journalctl -u crm -f                          ║"
echo "║                                                  ║"
echo "║  Accès : http://$(hostname -I | awk '{print $1}'):$APP_PORT"
echo "║  Login : admin  /  (mot de passe choisi)         ║"
echo "║                                                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

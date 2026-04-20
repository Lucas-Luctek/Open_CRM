#!/bin/bash
# ============================================================
# Script d'installation du CRM Open Source
# Testé sur Ubuntu 22.04 / Debian 12
# ============================================================

set -e

echo "=== Installation du CRM ==="

# 1. Vérifier Python 3
if ! command -v python3 &>/dev/null; then
    echo "Python3 non trouvé. Installation..."
    sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv
fi

echo "Python: $(python3 --version)"

# 2. Créer l'environnement virtuel
if [ ! -d "venv" ]; then
    echo "Création de l'environnement virtuel..."
    python3 -m venv venv
fi

# 3. Activer et installer les dépendances
echo "Installation des dépendances..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Créer le fichier .env si absent
if [ ! -f ".env" ]; then
    echo "Création du fichier .env depuis .env.example..."
    cp .env.example .env
    # Générer une clé secrète aléatoire
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s/changez-cette-cle-en-production-svp/$SECRET/" .env
    echo "Clé secrète générée automatiquement dans .env"
fi

echo ""
echo "=== Installation terminée ==="
echo ""
echo "Pour lancer l'application:"
echo "  source venv/bin/activate"
echo "  python app.py"
echo ""
echo "Accès: http://localhost:5000"
echo "Identifiants par défaut: admin / admin123"
echo "IMPORTANT: Changer le mot de passe admin apres la premiere connexion!"
echo ""

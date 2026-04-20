#!/bin/bash
# ============================================================
# Script de mise à jour du CRM
# ============================================================

set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║        Mise à jour du CRM            ║"
echo "╚══════════════════════════════════════╝"
echo ""

# 1. Récupérer les dernières modifications
echo "─── Récupération des mises à jour ───"
git pull origin main
echo "  ✓ Code mis à jour."
echo ""

# 2. Mettre à jour les dépendances Python
echo "─── Mise à jour des dépendances ───"
source venv/bin/activate
pip install -r requirements.txt -q
echo "  ✓ Dépendances à jour."
echo ""

# 3. Redémarrer le service
echo "─── Redémarrage du CRM ───"
if systemctl is-active --quiet crm 2>/dev/null; then
    sudo systemctl restart crm
    echo "  ✓ Service systemd redémarré."
elif [ "$(docker compose ps -q crm 2>/dev/null)" != "" ]; then
    docker compose up -d --build
    echo "  ✓ Conteneur Docker relancé."
else
    echo "  ℹ Relancez manuellement :"
    echo "    source venv/bin/activate && python app.py"
fi

echo ""
echo "  ✓ Mise à jour terminée !"
echo ""

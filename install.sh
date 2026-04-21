#!/bin/bash
# ============================================================
# Bootstrap — installe les dépendances système puis lance setup.sh
# Usage : bash <(curl -fsSL https://raw.githubusercontent.com/Lucas-Luctek/Open_CRM/main/install.sh)
# ============================================================

set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║      Installation du CRM Open Source  ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Dépendances système ─────────────────────────────────────
echo "─── Dépendances système ───"
apt-get update -qq
apt-get install -y git curl
echo "  ✓ git et curl installés."
echo ""

# ── Clonage du dépôt ────────────────────────────────────────
echo "─── Téléchargement du CRM ───"
INSTALL_DIR="$HOME/crm"
if [ -d "$INSTALL_DIR" ]; then
    echo "  Le dossier $INSTALL_DIR existe déjà, mise à jour..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone https://github.com/Lucas-Luctek/Open_CRM.git "$INSTALL_DIR"
fi
echo "  ✓ Dépôt prêt dans $INSTALL_DIR"
echo ""

# ── Lancement de l'installation ─────────────────────────────
cd "$INSTALL_DIR"
chmod +x setup.sh
./setup.sh

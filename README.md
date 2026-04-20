# CRM Commercial Open Source

CRM léger pour la prospection commerciale. Développé en Python/Flask avec SQLite, sans dépendances lourdes.

## Fonctionnalités

- Tableau de bord avec KPIs, pipeline de conversion et carte géographique
- Annuaire des comptes avec filtres et pagination
- Fiche prospect avec historique des activités et des statuts
- Kanban drag-and-drop par statut commercial
- Agenda (calendrier FullCalendar) avec relances automatiques
- Planning Gantt et export PDF
- Actions de masse (mailing groupé)
- Cartographie des prospects (Leaflet + OpenStreetMap)
- Import/Export CSV
- Calculateur de marge
- Gestion multi-utilisateurs avec rôles (admin / commercial)
- Mode sombre / clair
- Protection CSRF sur tous les formulaires
- Journaux de connexion

---

## Installation rapide

### Méthode 1 — Script automatique (Linux/Mac)

```bash
chmod +x setup.sh
./setup.sh
source venv/bin/activate
python app.py
```

### Méthode 2 — Manuelle

```bash
# 1. Créer et activer l'environnement virtuel
python3 -m venv venv
source venv/bin/activate        # Linux/Mac
# OU
venv\Scripts\activate           # Windows

# 2. Installer les dépendances
pip install -r requirements.txt

# 3. Configurer l'environnement
cp .env.example .env
# Éditer .env avec nano ou votre éditeur:
nano .env

# 4. Lancer
python app.py
```

### Accès

- URL : http://localhost:5000
- Login par défaut : `admin` / `admin123`
- **Changer le mot de passe admin dès la première connexion** via /profil ou /admin

---

## Installation sur VM de laboratoire (Ubuntu/Debian)

```bash
# Prérequis système
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv git

# Cloner ou copier le projet
# (si depuis un dépôt git)
# git clone <url_du_repo> crm
# cd crm

# Lancer le script d'install
chmod +x setup.sh && ./setup.sh

# Activer l'environnement et démarrer
source venv/bin/activate
python app.py
```

### Rendre accessible sur le réseau de la VM

Par défaut l'application écoute sur `0.0.0.0:5000`, donc accessible depuis le réseau.
Modifier le port dans `.env` si nécessaire :

```
PORT=8080
```

### Lancer en arrière-plan (production légère)

```bash
# Avec nohup
nohup python app.py > crm.log 2>&1 &

# Voir les logs
tail -f crm.log

# Arrêter
kill $(cat crm.pid)   # ou kill %1
```

### Avec systemd (service au démarrage)

Créer `/etc/systemd/system/crm.service` :

```ini
[Unit]
Description=CRM Commercial
After=network.target

[Service]
User=votre_user
WorkingDirectory=/chemin/vers/crm_opensource
Environment=SECRET_KEY=votre_cle_secrete_ici
Environment=COMPANY_NAME=MON ENTREPRISE
ExecStart=/chemin/vers/crm_opensource/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable crm
sudo systemctl start crm
sudo systemctl status crm
```

---

## Configuration

### Variables d'environnement (fichier .env)

| Variable       | Description                          | Défaut                              |
|----------------|--------------------------------------|-------------------------------------|
| `SECRET_KEY`   | Clé secrète Flask (OBLIGATOIRE prod) | changez-cette-cle-en-production-svp |
| `COMPANY_NAME` | Nom affiché dans l'interface         | MON ENTREPRISE                      |
| `PORT`         | Port d'écoute                        | 5000                                |
| `FLASK_DEBUG`  | Mode debug (false en production)     | false                               |

Générer une clé secrète sécurisée :
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Nom de l'entreprise

Peut être changé à tout moment via le panneau **Admin > Personnalisation** sans redémarrage.

---

## Structure du projet

```
crm_opensource/
├── app.py              # Application Flask principale
├── backup.py           # Script de sauvegarde
├── requirements.txt    # Dépendances Python
├── setup.sh            # Script d'installation automatique
├── .env.example        # Modèle de configuration
├── crm.db              # Base SQLite (créée au premier lancement)
├── backups/            # Sauvegardes (créé par backup.py)
├── static/
│   ├── favicon.svg
│   └── style.css
└── templates/
    ├── login.html
    ├── index.html          # Tableau de bord
    ├── base_donnees.html   # Annuaire
    ├── prospect.html       # Fiche prospect
    ├── nouveau.html        # Création
    ├── editer_prospect.html
    ├── editer_intervention.html
    ├── kanban.html
    ├── calendrier.html
    ├── gantt.html
    ├── calculatrice.html
    ├── carte.html
    ├── action_multiple.html
    ├── import.html
    ├── admin.html
    ├── logs.html
    └── profil.html
```

---

## Sauvegarde de la base de données

```bash
# Sauvegarde manuelle
python backup.py

# Sauvegarde automatique quotidienne (cron)
# Éditer avec: crontab -e
0 2 * * * cd /chemin/vers/crm_opensource && venv/bin/python backup.py >> backups/backup.log 2>&1
```

Les sauvegardes sont stockées dans `backups/` (30 dernières conservées).

---

## Travailler avec Claude Code (version bureau)

1. Ouvrir le dossier `crm_opensource/` dans Claude Code
2. Le fichier `app.py` contient toute la logique backend
3. Les templates HTML sont dans `templates/`
4. Les styles dans `static/style.css`

Commandes utiles en développement :
```bash
# Lancer avec rechargement automatique
FLASK_DEBUG=true python app.py

# Réinitialiser la base (supprime crm.db)
rm crm.db && python app.py
```

---

## Dépendances tierces (CDN — nécessite internet)

- **Chart.js** — graphiques dashboard
- **Leaflet** — cartes géographiques
- **FullCalendar** — agenda
- **Google Charts** — Gantt
- **html2canvas + jsPDF** — export PDF Gantt
- **API adresse.data.gouv.fr** — géocodage des adresses françaises
- **API geo.api.gouv.fr** — auto-complétion code postal → ville

Pour une utilisation hors-ligne, télécharger ces bibliothèques localement et adapter les chemins dans les templates.

---

## Licence

MIT — Libre d'utilisation, modification et distribution.

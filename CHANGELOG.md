# Changelog

## v1.0.0 — 2026-04-20

Première version stable.

### Fonctionnalités

- Tableau de bord : KPIs, pipeline de conversion, graphiques par catégorie, relances du jour
- Annuaire des prospects : filtres, tri, tags libres, pagination, corbeille
- Fiche prospect : historique activités, pièces jointes, export PDF, historique statuts, export RGPD JSON
- Kanban par statut commercial
- Agenda (FullCalendar) + planning Gantt
- Mailing groupé avec variables de publipostage, modèles et prévisualisation
- Relances automatiques par email à 8h chaque matin (APScheduler)
- Sauvegarde automatique de la BDD chaque nuit à 2h
- Cartographie des prospects (Leaflet / OpenStreetMap)
- Import / Export CSV
- Gestion multi-utilisateurs avec rôles (admin / commercial)
- Mode sombre / clair
- Version mobile responsive (navbar hamburger, tableaux scrollables)
- Protection CSRF, rate limiting sur le login (5 tentatives / 5 min)
- Journaux de connexion et d'actions
- Conformité RGPD : opt-out, export JSON, registre des traitements (Art. 30), alertes inactivité
- Support Docker (Dockerfile + docker-compose)
- Script d'installation interactif `setup.sh` (choix mot de passe admin + port)

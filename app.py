from flask import Flask, render_template, request, redirect, url_for, session, Response, abort, send_from_directory, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import sqlite3
import os
import csv
import io
import secrets
import smtplib
import time
import subprocess
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from weasyprint import HTML as WeasyprintHTML

# ─── Rate limiting login ───────────────────────
_login_attempts = defaultdict(list)
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 300  # 5 minutes

os.chdir(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'changez-cette-cle-en-production-svp')
_DATA_DIR = os.environ.get('DATA_DIR', '')
DB_NAME = os.path.join(_DATA_DIR, 'crm.db') if _DATA_DIR else 'crm.db'
UPLOAD_FOLDER = os.path.join(_DATA_DIR, 'uploads') if _DATA_DIR else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS prospects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        etablissement TEXT NOT NULL,
        contact TEXT,
        telephone TEXT,
        email TEXT,
        adresse TEXT,
        categorie TEXT,
        statut TEXT,
        temperature TEXT,
        commentaire TEXT,
        date_relance DATE,
        catalogue INTEGER DEFAULT 0,
        grille INTEGER DEFAULT 0,
        devis INTEGER DEFAULT 0,
        date_ajout TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS interventions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prospect_id INTEGER,
        date_intervention TEXT,
        type_contact TEXT,
        compte_rendu TEXT,
        FOREIGN KEY(prospect_id) REFERENCES prospects(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS evenements_perso (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        titre TEXT NOT NULL,
        date_event DATE NOT NULL,
        type_event TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        action TEXT,
        ip TEXT,
        horodatage TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS historique_statut (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prospect_id INTEGER,
        ancien_statut TEXT,
        nouveau_statut TEXT,
        username TEXT,
        horodatage TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(prospect_id) REFERENCES prospects(id)
    )''')
    # Migration colonnes existantes (ignorée si déjà présentes)
    for col in ['catalogue', 'grille', 'devis']:
        try:
            c.execute(f"ALTER TABLE prospects ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    for col in ['code_postal', 'ville']:
        try:
            c.execute(f"ALTER TABLE prospects ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN signature TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    for col_def in [
        ("source",      "TEXT DEFAULT ''"),
        ("base_legale", "TEXT DEFAULT ''"),
        ("no_contact",  "INTEGER DEFAULT 0"),
        ("archived",    "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE prospects ADD COLUMN {col_def[0]} {col_def[1]}")
        except sqlite3.OperationalError:
            pass
    # Traçage utilisateur sur interventions
    try:
        c.execute("ALTER TABLE interventions ADD COLUMN username TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS email_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_email TEXT NOT NULL,
        to_name TEXT,
        prospect_id INTEGER,
        subject TEXT,
        status TEXT DEFAULT 'sent',
        error_msg TEXT,
        sender_username TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Tags libres
    c.execute('''CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT UNIQUE NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS prospect_tags (
        prospect_id INTEGER,
        tag_id INTEGER,
        PRIMARY KEY(prospect_id, tag_id),
        FOREIGN KEY(prospect_id) REFERENCES prospects(id),
        FOREIGN KEY(tag_id) REFERENCES tags(id)
    )''')
    # Pièces jointes sur interventions
    c.execute('''CREATE TABLE IF NOT EXISTS intervention_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intervention_id INTEGER,
        filename TEXT,
        original_name TEXT,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(intervention_id) REFERENCES interventions(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS mail_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        sujet TEXT NOT NULL,
        corps TEXT NOT NULL,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute("SELECT COUNT(*) FROM mail_templates")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO mail_templates (nom, sujet, corps) VALUES (?, ?, ?)", [
            ('Commercial', 'Notre offre pour {{etablissement}}',
             'Bonjour {{contact}},\n\nJe me permets de vous contacter au nom de {{company}}.\n\nNous proposons des solutions adaptées aux professionnels de votre secteur ({{categorie}}) et serions ravis de vous présenter notre offre.\n\nSeriez-vous disponible pour un échange cette semaine ?\n\nCordialement,\nL\'équipe {{company}}'),
            ('Relance', 'Suite à notre échange — {{etablissement}}',
             'Bonjour {{contact}},\n\nSuite à notre précédent échange, je reviens vers vous concernant {{etablissement}}.\n\nAvez-vous eu l\'occasion d\'examiner notre proposition ? Je reste disponible pour répondre à vos questions.\n\nCordialement,\nL\'équipe {{company}}'),
            ('Catalogue', 'Notre catalogue {{company}}',
             'Bonjour {{contact}},\n\nVeuillez trouver ci-joint notre catalogue {{company}}.\n\nVous y trouverez nos références et tarifs pour les professionnels du secteur {{categorie}}.\n\nCordialement,\nL\'équipe {{company}}'),
        ])
    # Nom de l'entreprise par défaut (modifiable via /admin)
    default_company = os.environ.get('COMPANY_NAME', 'MON ENTREPRISE')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('company_name', ?)", (default_company,))
    # Compte admin par défaut — CHANGER LE MOT DE PASSE EN PRODUCTION
    c.execute("SELECT * FROM users WHERE username = 'admin'")
    if not c.fetchone():
        hashed_pw = generate_password_hash('admin123')
        c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", ('admin', hashed_pw, 'admin'))
    conn.commit()
    conn.close()

def get_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

def validate_csrf():
    token = request.form.get('csrf_token')
    if not token or token != session.get('csrf_token'):
        abort(403)

def get_setting(key, default=''):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def get_smtp_config():
    keys = ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass', 'smtp_from', 'smtp_from_name', 'smtp_secure']
    return {k: get_setting(k, '') for k in keys}

@app.template_filter('tel')
def format_tel(value):
    if not value:
        return ''
    digits = ''.join(c for c in str(value) if c.isdigit())[:10]
    return ' '.join(digits[i:i+2] for i in range(0, len(digits), 2))

@app.before_request
def check_mailing_enabled():
    mailing_routes = {'mailing', 'mailing_send', 'mailing_preview', 'communications'}
    if request.endpoint in mailing_routes:
        if get_setting('mailing_enabled', '1') == '0':
            abort(404)

@app.before_request
def csrf_protect():
    if request.method == 'POST' and request.endpoint != 'login':
        if request.is_json:
            data = request.get_json(silent=True) or {}
            token = data.get('csrf_token')
            if not token or token != session.get('csrf_token'):
                abort(403)
        else:
            validate_csrf()

@app.context_processor
def inject_globals():
    logo_filename = get_setting('logo_filename', '')
    logo_url = f'/static/{logo_filename}' if logo_filename else ''
    custom_colors = {
        'primary': get_setting('color_primary', ''),
        'accent':  get_setting('color_accent', ''),
    }
    company_info = {
        'name':      get_setting('company_name', 'MON ENTREPRISE'),
        'adresse':   get_setting('company_adresse', ''),
        'cp':        get_setting('company_cp', ''),
        'ville':     get_setting('company_ville', ''),
        'region':    get_setting('company_region', ''),
        'telephone': get_setting('company_telephone', ''),
        'email':     get_setting('company_email', ''),
        'site':      get_setting('company_site', ''),
        'siret':     get_setting('company_siret', ''),
        'tva':       get_setting('company_tva', ''),
        'forme':     get_setting('company_forme', ''),
        'naf':       get_setting('company_naf', ''),
    }
    return dict(theme=session.get('theme', 'light'), csrf_token=get_csrf_token(),
                company_name=get_setting('company_name', 'MON ENTREPRISE'),
                logo_url=logo_url,
                smtp_config=get_smtp_config(),
                custom_colors=custom_colors,
                company_info=company_info,
                mailing_enabled=get_setting('mailing_enabled', '1') == '1')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        now = time.time()
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < RATE_LIMIT_WINDOW]
        if len(_login_attempts[ip]) >= RATE_LIMIT_MAX:
            remaining = int(RATE_LIMIT_WINDOW - (now - _login_attempts[ip][0]))
            error = f"Trop de tentatives. Réessayez dans {remaining // 60 + 1} minute(s)."
            return render_template('login.html', error=error)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            error = "Veuillez remplir tous les champs."
        else:
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE username = ?", (username,))
            user = c.fetchone()
            conn.close()
            if user and check_password_hash(user['password'], password):
                _login_attempts[ip] = []
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['role'] = user['role']
                conn2 = sqlite3.connect(DB_NAME); c2 = conn2.cursor()
                c2.execute("INSERT INTO logs (username, action, ip) VALUES (?, ?, ?)", (username, 'connexion', ip))
                conn2.commit(); conn2.close()
                return redirect(url_for('index'))
            else:
                _login_attempts[ip].append(now)
                remaining_attempts = RATE_LIMIT_MAX - len(_login_attempts[ip])
                error = f"Identifiants incorrects.{f' ({remaining_attempts} tentative(s) restante(s))' if remaining_attempts < RATE_LIMIT_MAX else ''}"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    if 'username' in session:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        c.execute("INSERT INTO logs (username, action, ip) VALUES (?, ?, ?)", (session['username'], 'déconnexion', ip))
        conn.commit(); conn.close()
    session.clear()
    return redirect(url_for('login'))

@app.route('/toggle-theme')
@login_required
def toggle_theme():
    current_theme = session.get('theme', 'light')
    session['theme'] = 'dark' if current_theme == 'light' else 'light'
    return redirect(request.referrer or url_for('index'))

# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects ORDER BY date_ajout DESC")
    prospects = c.fetchall()
    total_prospects = len(prospects)
    total_clients = sum(1 for p in prospects if p['statut'] == 'Client !')
    aujourdhui = datetime.now().strftime('%Y-%m-%d')
    debut_mois = datetime.now().strftime('%Y-%m-01')
    a_relancer = [p for p in prospects if p['date_relance'] and p['date_relance'] <= aujourdhui and p['statut'] != 'Client !']
    relances_auj = [p for p in a_relancer if p['date_relance'] == aujourdhui]
    funnel = {'contact': total_prospects, 'catalogue': 0, 'grille': 0, 'devis': 0, 'client': total_clients}
    temperatures = {'chaud': 0, 'tiede': 0, 'froid': 0}
    for p in prospects:
        if p['catalogue'] == 1: funnel['catalogue'] += 1
        if p['grille'] == 1: funnel['grille'] += 1
        if p['devis'] == 1: funnel['devis'] += 1
        t = p['temperature']
        if t == 'Chaud': temperatures['chaud'] += 1
        elif t == 'Tiède': temperatures['tiede'] += 1
        elif t == 'Froid': temperatures['froid'] += 1
    # KPIs enrichis
    taux_conversion = round((total_clients / total_prospects * 100) if total_prospects > 0 else 0, 1)
    # Prospects contactés ce mois (ont au moins une intervention ce mois)
    c.execute("SELECT COUNT(DISTINCT prospect_id) FROM interventions WHERE date_intervention >= ?", (debut_mois,))
    contactes_mois = c.fetchone()[0]
    # Prospects sans aucun suivi (0 interventions)
    c.execute("SELECT COUNT(*) FROM prospects WHERE id NOT IN (SELECT DISTINCT prospect_id FROM interventions) AND statut != 'Client !'")
    sans_suivi = c.fetchone()[0]
    # Nouveaux prospects ce mois
    c.execute("SELECT COUNT(*) FROM prospects WHERE date_ajout >= ?", (debut_mois,))
    nouveaux_mois = c.fetchone()[0]
    # Activité récente (7 derniers jours)
    il_y_a_7j = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    c.execute("""SELECT i.*, p.etablissement FROM interventions i
                 JOIN prospects p ON p.id = i.prospect_id
                 WHERE i.date_intervention >= ? ORDER BY i.id DESC LIMIT 10""", (il_y_a_7j,))
    activite_recente = c.fetchall()
    # Conversion par catégorie
    c.execute("""SELECT categorie,
                        COUNT(*) as total,
                        SUM(CASE WHEN statut='Client !' THEN 1 ELSE 0 END) as clients
                 FROM prospects
                 WHERE categorie IS NOT NULL AND categorie != ''
                 GROUP BY categorie ORDER BY total DESC""")
    stats_categories = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('index.html', prospects=prospects, total=total_prospects, clients=total_clients,
                           a_relancer=a_relancer, relances_auj=relances_auj, funnel=funnel, temperatures=temperatures,
                           taux_conversion=taux_conversion, contactes_mois=contactes_mois,
                           sans_suivi=sans_suivi, nouveaux_mois=nouveaux_mois, activite_recente=activite_recente,
                           stats_categories=stats_categories)

# ─────────────────────────────────────────────
# PROSPECTS
# ─────────────────────────────────────────────

@app.route('/base')
@login_required
def base_donnees():
    q = request.args.get('q', '').strip()
    f_statut = request.args.get('statut', '').strip()
    f_categorie = request.args.get('categorie', '').strip()
    f_temperature = request.args.get('temperature', '').strip()
    f_ville = request.args.get('ville', '').strip()
    f_tag = request.args.get('tag', '').strip()
    sort = request.args.get('sort', 'date_ajout').strip()
    order = request.args.get('order', 'desc').strip()
    try:
        page = max(1, int(request.args.get('page', 1) or 1))
    except (ValueError, TypeError):
        page = 1
    per_page = 50

    # Colonnes triables autorisées
    sort_cols = {'etablissement': 'p.etablissement', 'statut': 'p.statut',
                 'date_relance': 'p.date_relance', 'date_ajout': 'p.date_ajout',
                 'derniere_activite': 'derniere_activite'}
    sort_col = sort_cols.get(sort, 'p.date_ajout')
    order_sql = 'ASC' if order == 'asc' else 'DESC'

    conditions = ["(p.archived IS NULL OR p.archived = 0)"]
    params = []
    if q:
        conditions.append("(p.etablissement LIKE ? OR p.contact LIKE ? OR p.telephone LIKE ? OR p.email LIKE ? OR p.ville LIKE ?)")
        like = f'%{q}%'
        params += [like, like, like, like, like]
    if f_statut:
        conditions.append("p.statut = ?")
        params.append(f_statut)
    if f_categorie:
        conditions.append("p.categorie = ?")
        params.append(f_categorie)
    if f_temperature:
        conditions.append("p.temperature = ?")
        params.append(f_temperature)
    if f_ville:
        conditions.append("p.ville LIKE ?")
        params.append(f'%{f_ville}%')
    if f_tag:
        conditions.append("EXISTS (SELECT 1 FROM prospect_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.prospect_id=p.id AND t.nom=?)")
        params.append(f_tag)

    where = "WHERE " + " AND ".join(conditions)

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(f"""SELECT COUNT(DISTINCT p.id) FROM prospects p
                  LEFT JOIN interventions i ON i.prospect_id = p.id
                  {where}""", params)
    total_count = c.fetchone()[0]

    offset = (page - 1) * per_page
    c.execute(f"""SELECT p.*, MAX(i.date_intervention) as derniere_activite
                  FROM prospects p
                  LEFT JOIN interventions i ON i.prospect_id = p.id
                  {where}
                  GROUP BY p.id
                  ORDER BY {sort_col} {order_sql} NULLS LAST
                  LIMIT ? OFFSET ?""", params + [per_page, offset])
    prospects = c.fetchall()

    c.execute("SELECT DISTINCT statut FROM prospects WHERE statut IS NOT NULL AND statut != '' ORDER BY statut")
    statuts = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT categorie FROM prospects WHERE categorie IS NOT NULL AND categorie != '' ORDER BY categorie")
    categories = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT ville FROM prospects WHERE ville IS NOT NULL AND ville != '' ORDER BY ville")
    villes = [r[0] for r in c.fetchall()]
    c.execute("SELECT nom FROM tags ORDER BY nom ASC")
    all_tags = [r[0] for r in c.fetchall()]

    conn.close()
    total_pages = max(1, (total_count + per_page - 1) // per_page)

    return render_template('base_donnees.html', prospects=prospects,
                           total_count=total_count, page=page, total_pages=total_pages,
                           statuts=statuts, categories=categories, villes=villes, all_tags=all_tags,
                           q=q, f_statut=f_statut, f_categorie=f_categorie,
                           f_temperature=f_temperature, f_ville=f_ville, f_tag=f_tag,
                           sort=sort, order=order)

@app.route('/nouveau')
@login_required
def nouveau_prospect_form():
    return render_template('nouveau.html')

@app.route('/ajouter', methods=['POST'])
@login_required
def ajouter_prospect():
    etablissement = request.form.get('etablissement', '').strip()
    if not etablissement:
        return redirect(url_for('nouveau_prospect_form'))
    contact = request.form.get('contact', '').strip()
    telephone = request.form.get('telephone', '').strip()
    email = request.form.get('email', '').strip()
    adresse = request.form.get('adresse', '').strip()
    code_postal = request.form.get('code_postal', '').strip()
    ville = request.form.get('ville', '').strip()
    categorie = request.form.get('categorie', '').strip()
    statut = request.form.get('statut', '').strip()
    temperature = request.form.get('temperature', '').strip()
    date_relance = request.form.get('date_relance', '').strip() or None
    commentaire = request.form.get('commentaire', '').strip()
    catalogue = 1 if 'catalogue' in request.form else 0
    grille = 1 if 'grille' in request.form else 0
    devis = 1 if 'devis' in request.form else 0
    source = request.form.get('source', '').strip()
    base_legale = request.form.get('base_legale', '').strip()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO prospects (etablissement, contact, telephone, email, adresse, code_postal, ville, categorie, statut, temperature, date_relance, commentaire, catalogue, grille, devis, source, base_legale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (etablissement, contact, telephone, email, adresse, code_postal, ville, categorie, statut, temperature, date_relance, commentaire, catalogue, grille, devis, source, base_legale)
    )
    conn.commit()
    conn.close()
    return redirect(url_for('base_donnees'))

@app.route('/prospect/<int:id>')
@login_required
def voir_prospect(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects WHERE id = ?", (id,))
    prospect = c.fetchone()
    if not prospect:
        conn.close()
        return redirect(url_for('base_donnees'))
    c.execute("SELECT * FROM interventions WHERE prospect_id = ? ORDER BY date_intervention DESC", (id,))
    interventions = c.fetchall()
    c.execute("SELECT * FROM historique_statut WHERE prospect_id = ? ORDER BY horodatage DESC", (id,))
    historique_statut = c.fetchall()
    c.execute("SELECT * FROM mail_templates ORDER BY nom ASC")
    mail_templates = c.fetchall()
    # Tags du prospect
    c.execute("SELECT t.nom FROM tags t JOIN prospect_tags pt ON pt.tag_id=t.id WHERE pt.prospect_id=? ORDER BY t.nom", (id,))
    prospect_tags = [r[0] for r in c.fetchall()]
    # Pièces jointes par intervention
    intervention_ids = [i['id'] for i in interventions]
    attachments_map = {}
    if intervention_ids:
        placeholders = ','.join('?' for _ in intervention_ids)
        c.execute(f"SELECT * FROM intervention_attachments WHERE intervention_id IN ({placeholders})", intervention_ids)
        for att in c.fetchall():
            attachments_map.setdefault(att['intervention_id'], []).append(att)
    conn.close()
    return render_template('prospect.html', prospect=prospect, interventions=interventions,
                           historique_statut=historique_statut, mail_templates=mail_templates,
                           prospect_tags=prospect_tags, attachments_map=attachments_map)

@app.route('/prospect/<int:id>/intervention', methods=['POST'])
@login_required
def ajouter_intervention(id):
    date_intervention = request.form.get('date_intervention', '').strip()
    type_contact = request.form.get('type_contact', '').strip()
    compte_rendu = request.form.get('compte_rendu', '').strip()
    nouveau_statut = request.form.get('nouveau_statut', '').strip()
    nouvelle_temperature = request.form.get('nouvelle_temperature', '').strip()
    nouvelle_relance = request.form.get('nouvelle_relance', '').strip() or None
    catalogue = 1 if 'catalogue' in request.form else 0
    grille = 1 if 'grille' in request.form else 0
    devis = 1 if 'devis' in request.form else 0
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT statut FROM prospects WHERE id = ?", (id,))
    row = c.fetchone()
    ancien_statut = row['statut'] if row else ''
    c.execute(
        "INSERT INTO interventions (prospect_id, date_intervention, type_contact, compte_rendu, username) VALUES (?, ?, ?, ?, ?)",
        (id, date_intervention, type_contact, compte_rendu, session.get('username', ''))
    )
    c.execute(
        "UPDATE prospects SET statut=?, temperature=?, date_relance=?, catalogue=?, grille=?, devis=? WHERE id=?",
        (nouveau_statut, nouvelle_temperature, nouvelle_relance, catalogue, grille, devis, id)
    )
    if nouveau_statut and nouveau_statut != ancien_statut:
        c.execute(
            "INSERT INTO historique_statut (prospect_id, ancien_statut, nouveau_statut, username) VALUES (?, ?, ?, ?)",
            (id, ancien_statut, nouveau_statut, session.get('username'))
        )
    conn.commit()
    conn.close()
    return redirect(url_for('voir_prospect', id=id))

@app.route('/prospect/<int:id>/editer', methods=['GET', 'POST'])
@login_required
def editer_prospect(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if request.method == 'POST':
        etablissement = request.form.get('etablissement', '').strip()
        if not etablissement:
            c.execute("SELECT * FROM prospects WHERE id = ?", (id,))
            prospect = c.fetchone()
            conn.close()
            return render_template('editer_prospect.html', prospect=prospect, error="L'établissement est obligatoire.")
        contact = request.form.get('contact', '').strip()
        telephone = request.form.get('telephone', '').strip()
        email = request.form.get('email', '').strip()
        adresse = request.form.get('adresse', '').strip()
        code_postal = request.form.get('code_postal', '').strip()
        ville = request.form.get('ville', '').strip()
        categorie = request.form.get('categorie', '').strip()
        statut = request.form.get('statut', '').strip()
        temperature = request.form.get('temperature', '').strip()
        date_relance = request.form.get('date_relance', '').strip() or None
        commentaire = request.form.get('commentaire', '').strip()
        catalogue = 1 if 'catalogue' in request.form else 0
        grille = 1 if 'grille' in request.form else 0
        devis = 1 if 'devis' in request.form else 0
        source = request.form.get('source', '').strip()
        base_legale = request.form.get('base_legale', '').strip()
        no_contact = 1 if 'no_contact' in request.form else 0
        c.execute("SELECT statut FROM prospects WHERE id = ?", (id,))
        row_statut = c.fetchone()
        ancien_statut = row_statut['statut'] if row_statut else ''
        c.execute(
            '''UPDATE prospects SET etablissement=?, contact=?, telephone=?, email=?, adresse=?, code_postal=?, ville=?, categorie=?, statut=?, temperature=?, date_relance=?, commentaire=?, catalogue=?, grille=?, devis=?, source=?, base_legale=?, no_contact=? WHERE id=?''',
            (etablissement, contact, telephone, email, adresse, code_postal, ville, categorie, statut, temperature, date_relance, commentaire, catalogue, grille, devis, source, base_legale, no_contact, id)
        )
        if statut and statut != ancien_statut:
            c.execute(
                "INSERT INTO historique_statut (prospect_id, ancien_statut, nouveau_statut, username) VALUES (?, ?, ?, ?)",
                (id, ancien_statut, statut, session.get('username'))
            )
        conn.commit()
        conn.close()
        return redirect(url_for('voir_prospect', id=id))
    c.execute("SELECT * FROM prospects WHERE id = ?", (id,))
    prospect = c.fetchone()
    conn.close()
    if not prospect:
        return redirect(url_for('base_donnees'))
    return render_template('editer_prospect.html', prospect=prospect)

@app.route('/prospect/<int:id>/send_mail', methods=['POST'])
@login_required
def prospect_send_mail(id):
    data = request.get_json(silent=True) or {}
    subject_tpl = data.get('subject', '').strip()
    body_tpl    = data.get('body', '').strip()
    if not subject_tpl or not body_tpl:
        return jsonify(ok=False, message="Objet et corps requis.")
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects WHERE id = ?", (id,))
    p = c.fetchone()
    if not p or not p['email']:
        conn.close()
        return jsonify(ok=False, message="Ce prospect n'a pas d'adresse email.")
    if p['no_contact']:
        conn.close()
        return jsonify(ok=False, message="Ce prospect a demandé à ne plus être contacté (opt-out RGPD).")
    cfg = get_smtp_config()
    if not cfg['smtp_host'] or not cfg['smtp_port']:
        conn.close()
        return jsonify(ok=False, message="SMTP non configuré. Allez dans Admin > Mailing.")
    cn = get_setting('company_name', '')
    subject = replace_vars(subject_tpl, p, cn)
    sig = get_user_signature()
    body    = replace_vars(body_tpl, p, cn)
    if sig:
        body += '\n\n--\n' + replace_vars(sig, p, cn)
    try:
        port = int(cfg['smtp_port'])
        if cfg['smtp_secure'] == 'ssl':
            server = smtplib.SMTP_SSL(cfg['smtp_host'], port, timeout=15)
        else:
            server = smtplib.SMTP(cfg['smtp_host'], port, timeout=15)
            if cfg['smtp_secure'] == 'tls':
                server.starttls()
        if cfg['smtp_user'] and cfg['smtp_pass']:
            server.login(cfg['smtp_user'], cfg['smtp_pass'])
        from_label = cfg['smtp_from_name'] or cn or cfg['smtp_user']
        from_addr  = cfg['smtp_from'] or cfg['smtp_user']
        msg = MIMEMultipart('alternative')
        msg['From']    = f"{from_label} <{from_addr}>"
        msg['To']      = p['email']
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html', 'utf-8'))
        server.sendmail(msg['From'], p['email'], msg.as_string())
        server.quit()
        c.execute(
            "INSERT INTO interventions (prospect_id, date_intervention, type_contact, compte_rendu) VALUES (?, ?, ?, ?)",
            (id, datetime.now().strftime('%Y-%m-%d'), 'Email', f"Objet : {subject}\n\n{body}")
        )
        conn.commit()
        conn.close()
        return jsonify(ok=True, message=f"Email envoyé à {p['email']}.")
    except Exception as e:
        conn.close()
        return jsonify(ok=False, message=f"Erreur SMTP : {str(e)}")

@app.route('/prospect/<int:id>/supprimer', methods=['POST'])
@login_required
def supprimer_prospect(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT etablissement FROM prospects WHERE id = ?", (id,))
    row = c.fetchone()
    nom = row['etablissement'] if row else f"ID#{id}"
    c.execute("DELETE FROM interventions WHERE prospect_id = ?", (id,))
    c.execute("DELETE FROM historique_statut WHERE prospect_id = ?", (id,))
    c.execute("UPDATE email_logs SET prospect_id = NULL WHERE prospect_id = ?", (id,))
    c.execute("DELETE FROM prospects WHERE id = ?", (id,))
    c.execute("INSERT INTO logs (username, action, ip) VALUES (?, ?, ?)",
              (session.get('username'), f"Suppression prospect RGPD : {nom} (ID#{id})", request.remote_addr))
    conn.commit()
    conn.close()
    return redirect(url_for('base_donnees'))

@app.route('/prospect/<int:id>/export')
@login_required
def export_prospect(id):
    import json as _json
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects WHERE id = ?", (id,))
    p = c.fetchone()
    if not p:
        conn.close()
        return "Prospect introuvable", 404
    c.execute("SELECT date_intervention, type_contact, compte_rendu FROM interventions WHERE prospect_id = ? ORDER BY date_intervention DESC", (id,))
    interventions = [dict(r) for r in c.fetchall()]
    c.execute("SELECT to_email, subject, status, sent_at FROM email_logs WHERE prospect_id = ? ORDER BY sent_at DESC", (id,))
    emails = [dict(r) for r in c.fetchall()]
    conn.close()
    data = {
        "export_date": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "prospect": dict(p),
        "interventions": interventions,
        "emails_envoyes": emails
    }
    return Response(
        _json.dumps(data, ensure_ascii=False, indent=2, default=str),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename=prospect_{id}_export.json'}
    )

# ─────────────────────────────────────────────
# ARCHIVAGE
# ─────────────────────────────────────────────

@app.route('/prospect/<int:id>/archiver', methods=['POST'])
@login_required
def archiver_prospect(id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE prospects SET archived = 1 WHERE id = ?", (id,))
    c.execute("INSERT INTO logs (username, action, ip) VALUES (?, ?, ?)",
              (session.get('username'), f"Archivage prospect ID#{id}", request.remote_addr))
    conn.commit()
    conn.close()
    return redirect(url_for('base_donnees'))

@app.route('/prospect/<int:id>/restaurer', methods=['POST'])
@login_required
def restaurer_prospect(id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE prospects SET archived = 0 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('corbeille'))

@app.route('/corbeille')
@login_required
def corbeille():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects WHERE archived = 1 ORDER BY etablissement ASC")
    prospects = c.fetchall()
    conn.close()
    return render_template('corbeille.html', prospects=prospects)

# ─────────────────────────────────────────────
# TAGS
# ─────────────────────────────────────────────

@app.route('/api/tags')
@login_required
def api_tags():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT nom FROM tags ORDER BY nom ASC")
    tags = [r[0] for r in c.fetchall()]
    conn.close()
    return jsonify(tags)

@app.route('/prospect/<int:id>/tags', methods=['POST'])
@login_required
def update_prospect_tags(id):
    data = request.get_json(silent=True) or {}
    tags_list = [t.strip() for t in data.get('tags', []) if t.strip()]
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Créer les tags manquants
    for tag in tags_list:
        c.execute("INSERT OR IGNORE INTO tags (nom) VALUES (?)", (tag,))
    # Remplacer les associations
    c.execute("DELETE FROM prospect_tags WHERE prospect_id = ?", (id,))
    for tag in tags_list:
        c.execute("SELECT id FROM tags WHERE nom = ?", (tag,))
        row = c.fetchone()
        if row:
            c.execute("INSERT OR IGNORE INTO prospect_tags (prospect_id, tag_id) VALUES (?, ?)", (id, row[0]))
    conn.commit()
    conn.close()
    return jsonify(ok=True, tags=tags_list)

# ─────────────────────────────────────────────
# PIÈCES JOINTES
# ─────────────────────────────────────────────

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'xls', 'xlsx', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/intervention/<int:id>/attachment', methods=['POST'])
@login_required
def upload_attachment(id):
    if 'file' not in request.files:
        return redirect(request.referrer or url_for('base_donnees'))
    f = request.files['file']
    if not f or not f.filename or not allowed_file(f.filename):
        return redirect(request.referrer or url_for('base_donnees'))
    original_name = f.filename
    ext = original_name.rsplit('.', 1)[1].lower()
    unique_name = f"{secrets.token_hex(12)}.{ext}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    f.save(os.path.join(UPLOAD_FOLDER, unique_name))
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("INSERT INTO intervention_attachments (intervention_id, filename, original_name) VALUES (?, ?, ?)",
              (id, unique_name, original_name))
    c.execute("SELECT prospect_id FROM interventions WHERE id = ?", (id,))
    row = c.fetchone()
    prospect_id = row['prospect_id'] if row else None
    conn.commit()
    conn.close()
    if prospect_id:
        return redirect(url_for('voir_prospect', id=prospect_id))
    return redirect(url_for('base_donnees'))

@app.route('/attachment/<int:id>')
@login_required
def download_attachment(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM intervention_attachments WHERE id = ?", (id,))
    att = c.fetchone()
    conn.close()
    if not att:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, att['filename'],
                               as_attachment=True, download_name=att['original_name'])

@app.route('/attachment/<int:id>/supprimer', methods=['POST'])
@login_required
def supprimer_attachment(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT ia.*, i.prospect_id FROM intervention_attachments ia JOIN interventions i ON i.id=ia.intervention_id WHERE ia.id=?", (id,))
    att = c.fetchone()
    if att:
        filepath = os.path.join(UPLOAD_FOLDER, att['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
        c.execute("DELETE FROM intervention_attachments WHERE id = ?", (id,))
        prospect_id = att['prospect_id']
    conn.commit()
    conn.close()
    if att and prospect_id:
        return redirect(url_for('voir_prospect', id=prospect_id))
    return redirect(url_for('base_donnees'))

# ─────────────────────────────────────────────
# EXPORT PDF
# ─────────────────────────────────────────────

@app.route('/prospect/<int:id>/pdf')
@login_required
def export_prospect_pdf(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects WHERE id = ?", (id,))
    prospect = c.fetchone()
    if not prospect:
        conn.close()
        return "Prospect introuvable", 404
    c.execute("SELECT * FROM interventions WHERE prospect_id = ? ORDER BY date_intervention DESC", (id,))
    interventions = c.fetchall()
    c.execute("SELECT * FROM historique_statut WHERE prospect_id = ? ORDER BY horodatage DESC", (id,))
    historique_statut = c.fetchall()
    conn.close()
    html_string = render_template('prospect_pdf.html', prospect=prospect,
                                  interventions=interventions, historique_statut=historique_statut,
                                  now=datetime.now().strftime('%d/%m/%Y %H:%M'))
    pdf_bytes = WeasyprintHTML(string=html_string, base_url=request.base_url).write_pdf()
    return Response(pdf_bytes, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename=prospect_{id}_{prospect["etablissement"][:30]}.pdf'})

# ─────────────────────────────────────────────
# INTERVENTIONS
# ─────────────────────────────────────────────

@app.route('/intervention/<int:id>/editer', methods=['GET', 'POST'])
@login_required
def editer_intervention(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM interventions WHERE id = ?", (id,))
    intervention = c.fetchone()
    if not intervention:
        conn.close()
        return redirect(url_for('base_donnees'))
    prospect_id = intervention['prospect_id']
    if request.method == 'POST':
        date_intervention = request.form.get('date_intervention', '').strip()
        type_contact = request.form.get('type_contact', '').strip()
        compte_rendu = request.form.get('compte_rendu', '').strip()
        nouveau_statut = request.form.get('nouveau_statut', '').strip()
        nouvelle_temperature = request.form.get('nouvelle_temperature', '').strip()
        nouvelle_relance = request.form.get('nouvelle_relance', '').strip() or None
        c.execute(
            '''UPDATE interventions SET date_intervention=?, type_contact=?, compte_rendu=? WHERE id=?''',
            (date_intervention, type_contact, compte_rendu, id)
        )
        c.execute(
            '''UPDATE prospects SET statut=?, temperature=?, date_relance=? WHERE id=?''',
            (nouveau_statut, nouvelle_temperature, nouvelle_relance, prospect_id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for('voir_prospect', id=prospect_id))
    c.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,))
    prospect = c.fetchone()
    conn.close()
    return render_template('editer_intervention.html', intervention=intervention, prospect=prospect)

@app.route('/intervention/<int:id>/supprimer', methods=['POST'])
@login_required
def supprimer_intervention(id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT prospect_id FROM interventions WHERE id = ?", (id,))
    res = c.fetchone()
    prospect_id = res['prospect_id'] if res else None
    c.execute("DELETE FROM interventions WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    if prospect_id:
        return redirect(url_for('voir_prospect', id=prospect_id))
    return redirect(url_for('index'))

# ─────────────────────────────────────────────
# ACTIONS MULTIPLES
# ─────────────────────────────────────────────

@app.route('/action_multiple', methods=['GET', 'POST'])
@login_required
def action_multiple():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if request.method == 'POST':
        prospect_ids = request.form.getlist('prospect_ids')
        date_intervention = request.form.get('date_intervention', '').strip()
        type_contact = request.form.get('type_contact', '').strip()
        compte_rendu = request.form.get('compte_rendu', '').strip()
        catalogue_check = 'catalogue' in request.form
        grille_check = 'grille' in request.form
        devis_check = 'devis' in request.form
        nouveau_statut = request.form.get('nouveau_statut', '').strip()
        nouvelle_temperature = request.form.get('nouvelle_temperature', '').strip()
        date_relance = request.form.get('date_relance', '').strip()
        nb = 0
        for pid in prospect_ids:
            if not pid.isdigit():
                continue
            nb += 1
            c.execute(
                "INSERT INTO interventions (prospect_id, date_intervention, type_contact, compte_rendu) VALUES (?, ?, ?, ?)",
                (pid, date_intervention, type_contact, compte_rendu)
            )
            c.execute("SELECT catalogue, grille, devis, statut, temperature, date_relance FROM prospects WHERE id = ?", (pid,))
            p = c.fetchone()
            if p:
                new_cat = 1 if catalogue_check else p['catalogue']
                new_gri = 1 if grille_check else p['grille']
                new_dev = 1 if devis_check else p['devis']
                statut_final = nouveau_statut if nouveau_statut else p['statut']
                temp_finale = nouvelle_temperature if nouvelle_temperature else p['temperature']
                relance_finale = date_relance if date_relance else p['date_relance']
                ancien_statut = p['statut']
                c.execute(
                    "UPDATE prospects SET catalogue=?, grille=?, devis=?, statut=?, temperature=?, date_relance=? WHERE id=?",
                    (new_cat, new_gri, new_dev, statut_final, temp_finale, relance_finale, pid)
                )
                if statut_final and statut_final != ancien_statut:
                    c.execute(
                        "INSERT INTO historique_statut (prospect_id, ancien_statut, nouveau_statut, username) VALUES (?, ?, ?, ?)",
                        (pid, ancien_statut, statut_final, session.get('username'))
                    )
        conn.commit()
        conn.close()
        success = f"Action appliquée sur {nb} prospect(s) avec succès."
        conn2 = sqlite3.connect(DB_NAME)
        conn2.row_factory = sqlite3.Row
        c2 = conn2.cursor()
        c2.execute("SELECT id, etablissement, categorie, statut, temperature FROM prospects ORDER BY etablissement ASC")
        prospects = c2.fetchall()
        c2.execute("SELECT DISTINCT categorie FROM prospects WHERE categorie IS NOT NULL AND categorie != '' ORDER BY categorie")
        categories = [r[0] for r in c2.fetchall()]
        c2.execute("SELECT DISTINCT statut FROM prospects WHERE statut IS NOT NULL AND statut != '' ORDER BY statut")
        statuts = [r[0] for r in c2.fetchall()]
        conn2.close()
        return render_template('action_multiple.html', prospects=prospects, categories=categories, statuts=statuts,
                               today=datetime.now().strftime('%Y-%m-%d'), success=success)
    c.execute("SELECT id, etablissement, categorie, statut, temperature FROM prospects ORDER BY etablissement ASC")
    prospects = c.fetchall()
    c.execute("SELECT DISTINCT categorie FROM prospects WHERE categorie IS NOT NULL AND categorie != '' ORDER BY categorie")
    categories = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT statut FROM prospects WHERE statut IS NOT NULL AND statut != '' ORDER BY statut")
    statuts = [r[0] for r in c.fetchall()]
    conn.close()
    return render_template('action_multiple.html', prospects=prospects, categories=categories, statuts=statuts,
                           today=datetime.now().strftime('%Y-%m-%d'))

# ─────────────────────────────────────────────
# CALENDRIER
# ─────────────────────────────────────────────

@app.route('/calendrier')
@login_required
def calendrier():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, etablissement, date_relance FROM prospects WHERE date_relance IS NOT NULL AND date_relance != '' AND statut != 'Client !'")
    relances = c.fetchall()
    c.execute("SELECT * FROM evenements_perso ORDER BY date_event ASC")
    evenements = c.fetchall()
    c.execute("SELECT id, etablissement FROM prospects ORDER BY etablissement ASC")
    tous_prospects = c.fetchall()
    conn.close()
    prospect_map = {p['etablissement']: p['id'] for p in tous_prospects}
    return render_template('calendrier.html', relances=relances, evenements=evenements,
                           tous_prospects=tous_prospects, prospect_map=prospect_map)

@app.route('/calendrier/ajouter', methods=['POST'])
@login_required
def ajouter_event_calendrier():
    titre = request.form.get('titre', '').strip()
    date_event = request.form.get('date_event', '').strip()
    type_event = request.form.get('type_event', '').strip()
    if not titre or not date_event or not type_event:
        return redirect(url_for('calendrier'))
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO evenements_perso (titre, date_event, type_event) VALUES (?, ?, ?)", (titre, date_event, type_event))
    conn.commit()
    conn.close()
    return redirect(url_for('calendrier'))

@app.route('/calendrier/supprimer/<int:id>', methods=['POST'])
@login_required
def supprimer_event_calendrier(id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM evenements_perso WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('calendrier'))

# ─────────────────────────────────────────────
# KANBAN / GANTT / CALCULATRICE / CARTE
# ─────────────────────────────────────────────

@app.route('/kanban')
@login_required
def kanban():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects ORDER BY date_relance ASC NULLS LAST, etablissement ASC")
    prospects = c.fetchall()
    conn.close()
    colonnes = ['À contacter', 'Premier contact établi', 'À relancer', 'Client !']
    return render_template('kanban.html', prospects=prospects, colonnes=colonnes)

@app.route('/prospect/<int:id>/statut', methods=['POST'])
@login_required
def update_statut(id):
    data = request.get_json()
    nouveau_statut = (data or {}).get('statut', '').strip()
    if not nouveau_statut:
        return {'ok': False}, 400
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT statut FROM prospects WHERE id = ?", (id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {'ok': False}, 404
    ancien_statut = row['statut']
    c.execute("UPDATE prospects SET statut = ? WHERE id = ?", (nouveau_statut, id))
    if nouveau_statut != ancien_statut:
        c.execute("INSERT INTO historique_statut (prospect_id, ancien_statut, nouveau_statut, username) VALUES (?, ?, ?, ?)",
                  (id, ancien_statut, nouveau_statut, session.get('username')))
    conn.commit()
    conn.close()
    return {'ok': True}

@app.route('/gantt')
@login_required
def gantt():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT p.etablissement, i.date_intervention, i.type_contact FROM interventions i JOIN prospects p ON i.prospect_id = p.id''')
    interventions = c.fetchall()
    c.execute('''SELECT etablissement, date_relance FROM prospects WHERE date_relance IS NOT NULL AND date_relance != '' ''')
    relances = c.fetchall()
    c.execute('''SELECT titre as etablissement, date_event, type_event as type_contact FROM evenements_perso WHERE type_event IN ('RDV Client', 'Dégustation', 'Entretien')''')
    rdvs = c.fetchall()
    conn.close()
    return render_template('gantt.html', interventions=interventions, relances=relances, rdvs=rdvs)

@app.route('/calculatrice')
@login_required
def calculatrice():
    return render_template('calculatrice.html')

@app.route('/carte')
@login_required
def carte():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, etablissement, adresse, statut, temperature FROM prospects WHERE adresse IS NOT NULL AND adresse != ''")
    prospects = c.fetchall()
    conn.close()
    return render_template('carte.html', prospects=prospects)

# ─────────────────────────────────────────────
# EXPORT / IMPORT CSV
# ─────────────────────────────────────────────

@app.route('/export')
@login_required
@admin_required
def export_csv():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects ORDER BY date_ajout DESC")
    prospects = c.fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['ID', 'Établissement', 'Contact', 'Téléphone', 'Email', 'Adresse', 'Catégorie', 'Statut', 'Température', 'Catalogue', 'Grille', 'Devis', 'Date relance', 'Commentaire', 'Date ajout'])
    for p in prospects:
        writer.writerow([
            p['id'], p['etablissement'], p['contact'], p['telephone'], p['email'],
            p['adresse'], p['categorie'], p['statut'], p['temperature'],
            'Oui' if p['catalogue'] else 'Non',
            'Oui' if p['grille'] else 'Non',
            'Oui' if p['devis'] else 'Non',
            p['date_relance'], p['commentaire'], p['date_ajout']
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=prospects_export.csv'}
    )

def _csv_find(row, *aliases):
    """Cherche une valeur dans un dict CSV en ignorant la casse et les variantes."""
    row_lower = {k.lower().strip(): v for k, v in row.items()}
    for alias in aliases:
        v = row_lower.get(alias.lower().strip())
        if v is not None:
            return v.strip() if isinstance(v, str) else v
    return ''

@app.route('/import', methods=['GET', 'POST'])
@login_required
def import_csv():
    error = None
    success = None
    detected_cols = []
    skipped = 0
    if request.method == 'POST':
        f = request.files.get('fichier')
        sep = request.form.get('separateur', ';')
        if not f or not f.filename.lower().endswith('.csv'):
            error = "Veuillez sélectionner un fichier CSV."
        else:
            try:
                content = f.read().decode('utf-8-sig')
                reader = csv.DictReader(io.StringIO(content), delimiter=sep)
                detected_cols = list(reader.fieldnames or [])
                count = 0
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                for row in reader:
                    etab = _csv_find(row, 'Établissement', 'etablissement', 'Nom', 'Société', 'Entreprise', 'Company', 'Name')
                    if not etab:
                        skipped += 1
                        continue
                    cat_val = _csv_find(row, 'Catalogue').lower()
                    gri_val = _csv_find(row, 'Grille').lower()
                    dev_val = _csv_find(row, 'Devis').lower()
                    date_rel = _csv_find(row, 'Date relance', 'date_relance', 'Relance') or None
                    c.execute(
                        "INSERT INTO prospects (etablissement, contact, telephone, email, adresse, code_postal, ville, categorie, statut, temperature, date_relance, commentaire, catalogue, grille, devis, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (etab,
                         _csv_find(row, 'Contact', 'Nom du contact', 'Prénom Nom', 'contact'),
                         _csv_find(row, 'Téléphone', 'Telephone', 'Tel', 'Phone', 'telephone'),
                         _csv_find(row, 'Email', 'email', 'Mail', 'Courriel'),
                         _csv_find(row, 'Adresse', 'adresse', 'Rue', 'Address'),
                         _csv_find(row, 'Code Postal', 'code_postal', 'CP', 'CodePostal', 'Zip'),
                         _csv_find(row, 'Ville', 'ville', 'City', 'Commune'),
                         _csv_find(row, 'Catégorie', 'categorie', 'Categorie', 'Type', 'Category'),
                         _csv_find(row, 'Statut', 'statut', 'Status'),
                         _csv_find(row, 'Température', 'Temperature', 'temperature', 'Qualification'),
                         date_rel,
                         _csv_find(row, 'Commentaire', 'commentaire', 'Notes', 'Note', 'Comment'),
                         1 if cat_val in ('oui', '1', 'true') else 0,
                         1 if gri_val in ('oui', '1', 'true') else 0,
                         1 if dev_val in ('oui', '1', 'true') else 0,
                         _csv_find(row, 'Source', 'source', 'Origine'),
                        )
                    )
                    count += 1
                conn.commit()
                conn.close()
                msg = f"{count} prospect(s) importé(s) avec succès."
                if skipped:
                    msg += f" {skipped} ligne(s) ignorée(s) (sans établissement)."
                success = msg
            except Exception as e:
                error = f"Erreur lors de l'import : {e}"
    return render_template('import.html', error=error, success=success, detected_cols=detected_cols, skipped=skipped)

# ─────────────────────────────────────────────
# ADMINISTRATION
# ─────────────────────────────────────────────

def get_admin_users():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, username, role FROM users ORDER BY id ASC")
    users = c.fetchall()
    conn.close()
    return users

def get_mail_templates():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM mail_templates ORDER BY nom ASC")
    templates = c.fetchall()
    conn.close()
    return templates

@app.route('/admin')
@login_required
@admin_required
def admin():
    tab = request.args.get('tab', 'users')
    success = request.args.get('success', '')
    error = request.args.get('error', '')
    # Données RGPD : prospects inactifs
    seuil_mois = int(get_setting('rgpd_inactivity_months', '36'))
    seuil_date = (datetime.now() - timedelta(days=seuil_mois * 30)).strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT p.id, p.etablissement, p.contact, p.date_ajout,
               MAX(i.date_intervention) as derniere_activite
        FROM prospects p
        LEFT JOIN interventions i ON i.prospect_id = p.id
        GROUP BY p.id
        HAVING (derniere_activite IS NULL AND p.date_ajout < ?)
            OR (derniere_activite IS NOT NULL AND derniere_activite < ?)
        ORDER BY derniere_activite ASC
    """, (seuil_date, seuil_date))
    prospects_inactifs = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('admin.html', users=get_admin_users(),
                           mail_templates=get_mail_templates(),
                           tab=tab, success=success, error=error,
                           prospects_inactifs=prospects_inactifs,
                           seuil_mois=seuil_mois,
                           relances_auto_enabled=get_setting('relances_auto_enabled', '0') == '1')

@app.route('/admin/toggle-mailing', methods=['POST'])
@login_required
@admin_required
def admin_toggle_mailing():
    val = request.form.get('mailing_enabled', '0')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('mailing_enabled', ?)", (val,))
    conn.commit()
    conn.close()
    state = 'activé' if val == '1' else 'désactivé'
    return redirect(url_for('admin') + f'?tab=mailing&success=Mailing+{state}')

@app.route('/admin/rgpd-config', methods=['POST'])
@login_required
@admin_required
def admin_rgpd_config():
    mois = request.form.get('rgpd_inactivity_months', '36').strip()
    if mois.isdigit() and int(mois) > 0:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('rgpd_inactivity_months', ?)", (mois,))
        conn.commit()
        conn.close()
    return redirect(url_for('admin') + '?tab=rgpd&success=Configuration+RGPD+enregistrée')

@app.route('/admin/template/ajouter', methods=['POST'])
@login_required
@admin_required
def admin_template_ajouter():
    nom   = request.form.get('nom', '').strip()
    sujet = request.form.get('sujet', '').strip()
    corps = request.form.get('corps', '').strip()
    if not nom or not sujet or not corps:
        return redirect(url_for('admin') + '?tab=mailing&error=Champs+manquants')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO mail_templates (nom, sujet, corps) VALUES (?, ?, ?)", (nom, sujet, corps))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=mailing&success=Modèle+créé')

@app.route('/admin/template/modifier/<int:id>', methods=['POST'])
@login_required
@admin_required
def admin_template_modifier(id):
    nom   = request.form.get('nom', '').strip()
    sujet = request.form.get('sujet', '').strip()
    corps = request.form.get('corps', '').strip()
    if not nom or not sujet or not corps:
        return redirect(url_for('admin') + '?tab=mailing&error=Champs+manquants')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE mail_templates SET nom=?, sujet=?, corps=? WHERE id=?", (nom, sujet, corps, id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=mailing&success=Modèle+modifié')

@app.route('/admin/template/supprimer/<int:id>', methods=['POST'])
@login_required
@admin_required
def admin_template_supprimer(id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM mail_templates WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=mailing&success=Modèle+supprimé')

@app.route('/admin/colors', methods=['POST'])
@login_required
@admin_required
def admin_colors():
    color_primary = request.form.get('color_primary', '').strip()
    color_accent  = request.form.get('color_accent', '').strip()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if color_primary:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('color_primary', ?)", (color_primary,))
    if color_accent:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('color_accent', ?)", (color_accent,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=perso&success=Couleurs+enregistrées')

@app.route('/admin/colors/reset')
@login_required
@admin_required
def admin_colors_reset():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM settings WHERE key IN ('color_primary', 'color_accent')")
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=perso&success=Couleurs+réinitialisées')

@app.route('/admin/ajouter', methods=['POST'])
@login_required
@admin_required
def admin_ajouter_utilisateur():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'commercial')
    if not username or not password:
        return redirect(url_for('admin') + '?tab=users&error=Nom+et+mot+de+passe+obligatoires')
    if len(password) < 6:
        return redirect(url_for('admin') + '?tab=users&error=Mot+de+passe+trop+court')
    if role not in ('admin', 'commercial'):
        role = 'commercial'
    hashed_pw = generate_password_hash(password)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, hashed_pw, role))
        conn.commit()
        conn.close()
        conn.close()
        return redirect(url_for('admin') + f'?tab=users&success=Utilisateur+{username}+créé')
    except sqlite3.IntegrityError:
        conn.close()
        return redirect(url_for('admin') + f'?tab=users&error=Nom+{username}+déjà+utilisé')

@app.route('/admin/supprimer/<int:id>', methods=['POST'])
@login_required
@admin_required
def admin_supprimer_utilisateur(id):
    if id == session.get('user_id'):
        return redirect(url_for('admin') + '?tab=users&error=Impossible+de+supprimer+votre+propre+compte')
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE id = ?", (id,))
    u = c.fetchone()
    c.execute("DELETE FROM users WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    msg = f"Utilisateur+supprimé"
    return redirect(url_for('admin') + f'?tab=users&success={msg}')

@app.route('/admin/reset_mdp/<int:id>', methods=['POST'])
@login_required
@admin_required
def admin_reset_mdp(id):
    nouveau_mdp = request.form.get('nouveau_mdp', '')
    confirmation_mdp = request.form.get('confirmation_mdp', '')
    if not nouveau_mdp or len(nouveau_mdp) < 6:
        return redirect(url_for('admin') + '?tab=users&error=Mot+de+passe+trop+court')
    if nouveau_mdp != confirmation_mdp:
        return redirect(url_for('admin') + '?tab=users&error=Mots+de+passe+différents')
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE id = ?", (id,))
    u = c.fetchone()
    if not u:
        conn.close()
        return redirect(url_for('admin') + '?tab=users&error=Utilisateur+introuvable')
    hashed_pw = generate_password_hash(nouveau_mdp)
    c.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=users&success=Mot+de+passe+réinitialisé')

# ─────────────────────────────────────────────
# PROFIL
# ─────────────────────────────────────────────

@app.route('/profil', methods=['GET', 'POST'])
@login_required
def profil():
    error = None
    success = None
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if request.method == 'POST':
        action = request.form.get('action', 'mdp')
        if action == 'signature':
            signature = request.form.get('signature', '')
            c.execute("UPDATE users SET signature = ? WHERE id = ?", (signature, session['user_id']))
            conn.commit()
            success = "Signature enregistrée."
        else:
            ancien_mdp = request.form.get('ancien_mdp', '')
            nouveau_mdp = request.form.get('nouveau_mdp', '')
            confirmation_mdp = request.form.get('confirmation_mdp', '')
            if not ancien_mdp or not nouveau_mdp or not confirmation_mdp:
                error = "Veuillez remplir tous les champs."
            elif nouveau_mdp != confirmation_mdp:
                error = "Les nouveaux mots de passe ne correspondent pas."
            elif len(nouveau_mdp) < 6:
                error = "Le nouveau mot de passe doit contenir au moins 6 caractères."
            else:
                c.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],))
                user = c.fetchone()
                if user and check_password_hash(user['password'], ancien_mdp):
                    hashed_pw = generate_password_hash(nouveau_mdp)
                    c.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, session['user_id']))
                    conn.commit()
                    success = "Mot de passe modifié avec succès."
                else:
                    error = "Ancien mot de passe incorrect."
    c.execute("SELECT signature FROM users WHERE id = ?", (session['user_id'],))
    row = c.fetchone()
    signature = row['signature'] if row else ''
    conn.close()
    return render_template('profil.html', error=error, success=success, current_signature=signature)

# ─────────────────────────────────────────────
# LOGS & SETTINGS
# ─────────────────────────────────────────────

def get_user_signature():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT signature FROM users WHERE id = ?", (session.get('user_id'),))
    row = c.fetchone()
    conn.close()
    return (row['signature'] or '') if row else ''

def replace_vars(text, prospect, company_name):
    mapping = {
        # Variables prospect
        '{{etablissement}}':   prospect['etablissement'] or '',
        '{{contact}}':         prospect['contact'] or '',
        '{{telephone}}':       prospect['telephone'] or '',
        '{{email}}':           prospect['email'] or '',
        '{{ville}}':           prospect['ville'] or '' if 'ville' in prospect.keys() else '',
        '{{categorie}}':       prospect['categorie'] or '',
        '{{statut}}':          prospect['statut'] or '',
        # Variables entreprise
        '{{company}}':         company_name,
        '{{company_adresse}}': get_setting('company_adresse', ''),
        '{{company_cp}}':      get_setting('company_cp', ''),
        '{{company_ville}}':   get_setting('company_ville', ''),
        '{{company_region}}':  get_setting('company_region', ''),
        '{{company_tel}}':     get_setting('company_telephone', ''),
        '{{company_email}}':   get_setting('company_email', ''),
        '{{company_site}}':    get_setting('company_site', ''),
        '{{company_siret}}':   get_setting('company_siret', ''),
        '{{company_tva}}':     get_setting('company_tva', ''),
        '{{company_forme}}':   get_setting('company_forme', ''),
    }
    for k, v in mapping.items():
        text = text.replace(k, v)
    return text

@app.route('/mailing')
@login_required
def mailing():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, etablissement, contact, email, categorie, statut, temperature, no_contact FROM prospects ORDER BY etablissement ASC")
    prospects = c.fetchall()
    c.execute("SELECT DISTINCT categorie FROM prospects WHERE categorie IS NOT NULL AND categorie != '' ORDER BY categorie")
    categories = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT statut FROM prospects WHERE statut IS NOT NULL AND statut != '' ORDER BY statut")
    statuts = [r[0] for r in c.fetchall()]
    c.execute("SELECT * FROM mail_templates ORDER BY nom ASC")
    mail_templates = [dict(r) for r in c.fetchall()]
    conn.close()
    smtp_ok = bool(get_setting('smtp_host') and get_setting('smtp_port'))
    return render_template('mailing.html', prospects=prospects, categories=categories,
                           statuts=statuts, smtp_ok=smtp_ok, mail_templates=mail_templates)

@app.route('/communications')
@login_required
def communications():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT el.*, p.etablissement as prospect_nom
        FROM email_logs el
        LEFT JOIN prospects p ON el.prospect_id = p.id
        ORDER BY el.sent_at DESC
        LIMIT 500
    """)
    logs = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('communications.html', logs=logs)

@app.route('/mailing/preview', methods=['POST'])
@login_required
def mailing_preview():
    data = request.get_json(silent=True) or {}
    pid = data.get('prospect_id')
    subject_tpl = data.get('subject', '')
    body_tpl = data.get('body', '')
    if not pid:
        return jsonify(ok=False, message="Sélectionnez un prospect pour la prévisualisation.")
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM prospects WHERE id = ?", (pid,))
    p = c.fetchone()
    conn.close()
    if not p:
        return jsonify(ok=False, message="Prospect introuvable.")
    cn = get_setting('company_name', '')
    return jsonify(ok=True, subject=replace_vars(subject_tpl, p, cn),
                   body=replace_vars(body_tpl, p, cn), prospect=p['etablissement'])

@app.route('/mailing/send', methods=['POST'])
@login_required
def mailing_send():
    data = request.get_json(silent=True) or {}
    prospect_ids    = data.get('prospect_ids', [])
    free_recipients = data.get('free_recipients', [])  # [{"email": "...", "name": "..."}]
    subject_tpl = data.get('subject', '').strip()
    body_tpl    = data.get('body', '').strip()
    if not subject_tpl or not body_tpl:
        return jsonify(ok=False, message="Objet et corps du message requis.")
    if not prospect_ids and not free_recipients:
        return jsonify(ok=False, message="Aucun destinataire sélectionné.")
    cfg = get_smtp_config()
    if not cfg['smtp_host'] or not cfg['smtp_port']:
        return jsonify(ok=False, message="SMTP non configuré. Allez dans Admin > Configuration SMTP.")
    try:
        port = int(cfg['smtp_port'])
        if cfg['smtp_secure'] == 'ssl':
            server = smtplib.SMTP_SSL(cfg['smtp_host'], port, timeout=15)
        else:
            server = smtplib.SMTP(cfg['smtp_host'], port, timeout=15)
            if cfg['smtp_secure'] == 'tls':
                server.starttls()
        if cfg['smtp_user'] and cfg['smtp_pass']:
            server.login(cfg['smtp_user'], cfg['smtp_pass'])
    except Exception as e:
        return jsonify(ok=False, message=f"Erreur connexion SMTP : {str(e)}")
    cn = get_setting('company_name', '')
    sig = get_user_signature()
    from_label = cfg['smtp_from_name'] or cn or cfg['smtp_user']
    from_addr  = cfg['smtp_from'] or cfg['smtp_user']
    sender = session.get('username', '')
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sent = 0; skipped = 0; errors = []

    # Envoi aux prospects de la BDD
    for pid in prospect_ids:
        if not str(pid).isdigit():
            continue
        c.execute("SELECT * FROM prospects WHERE id = ?", (pid,))
        p = c.fetchone()
        if not p or not p['email'] or p['no_contact']:
            skipped += 1
            continue
        try:
            subject = replace_vars(subject_tpl, p, cn)
            body    = replace_vars(body_tpl, p, cn)
            if sig:
                body += '\n\n--\n' + replace_vars(sig, p, cn)
            msg = MIMEMultipart('alternative')
            msg['From']    = f"{from_label} <{from_addr}>"
            msg['To']      = p['email']
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'html', 'utf-8'))
            server.sendmail(msg['From'], p['email'], msg.as_string())
            c.execute("INSERT INTO interventions (prospect_id, date_intervention, type_contact, compte_rendu) VALUES (?, ?, ?, ?)",
                      (pid, datetime.now().strftime('%Y-%m-%d'), 'Email', f"Campagne : {subject}"))
            c.execute("INSERT INTO email_logs (to_email, to_name, prospect_id, subject, status, sender_username) VALUES (?, ?, ?, ?, 'sent', ?)",
                      (p['email'], p['etablissement'], pid, subject, sender))
            sent += 1
        except Exception as e:
            err_msg = str(e)
            errors.append(f"{p['etablissement']} ({p['email']}) : {err_msg}")
            c.execute("INSERT INTO email_logs (to_email, to_name, prospect_id, subject, status, error_msg, sender_username) VALUES (?, ?, ?, ?, 'error', ?, ?)",
                      (p['email'], p['etablissement'], pid, replace_vars(subject_tpl, p, cn), err_msg, sender))

    # Envoi aux destinataires libres (hors BDD)
    for fr in free_recipients:
        to_email = (fr.get('email') or '').strip()
        to_name  = (fr.get('name') or '').strip()
        if not to_email:
            continue
        # Proxy prospect vide pour replace_vars
        p_free = {'etablissement': to_name or to_email, 'contact': to_name or '', 'telephone': '',
                  'email': to_email, 'ville': '', 'code_postal': '', 'categorie': '', 'statut': ''}
        try:
            subject = replace_vars(subject_tpl, p_free, cn)
            body    = replace_vars(body_tpl, p_free, cn)
            if sig:
                body += '\n\n--\n' + replace_vars(sig, p_free, cn)
            msg = MIMEMultipart('alternative')
            msg['From']    = f"{from_label} <{from_addr}>"
            msg['To']      = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'html', 'utf-8'))
            server.sendmail(msg['From'], to_email, msg.as_string())
            c.execute("INSERT INTO email_logs (to_email, to_name, prospect_id, subject, status, sender_username) VALUES (?, ?, NULL, ?, 'sent', ?)",
                      (to_email, to_name or to_email, subject, sender))
            sent += 1
        except Exception as e:
            err_msg = str(e)
            errors.append(f"{to_name or to_email} ({to_email}) : {err_msg}")
            c.execute("INSERT INTO email_logs (to_email, to_name, prospect_id, subject, status, error_msg, sender_username) VALUES (?, ?, NULL, ?, 'error', ?, ?)",
                      (to_email, to_name or to_email, replace_vars(subject_tpl, p_free, cn), err_msg, sender))

    try:
        server.quit()
    except Exception:
        pass
    conn.commit()
    conn.close()
    return jsonify(ok=True, sent=sent, skipped=skipped, errors=errors)

# ─────────────────────────────────────────────
# API REST
# ─────────────────────────────────────────────

@app.route('/api/prospects')
@login_required
def api_prospects():
    q = request.args.get('q', '').strip()
    statut = request.args.get('statut', '').strip()
    categorie = request.args.get('categorie', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1) or 1))
        per_page = min(500, max(1, int(request.args.get('per_page', 100) or 100)))
    except (ValueError, TypeError):
        page, per_page = 1, 100
    conditions = ["(archived IS NULL OR archived = 0)"]
    params = []
    if q:
        conditions.append("(etablissement LIKE ? OR contact LIKE ? OR email LIKE ?)")
        like = f'%{q}%'; params += [like, like, like]
    if statut:
        conditions.append("statut = ?"); params.append(statut)
    if categorie:
        conditions.append("categorie = ?"); params.append(categorie)
    where = "WHERE " + " AND ".join(conditions)
    offset = (page - 1) * per_page
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(f"SELECT COUNT(*) FROM prospects {where}", params)
    total = c.fetchone()[0]
    c.execute(f"SELECT id, etablissement, contact, email, telephone, ville, categorie, statut, temperature, date_relance, no_contact FROM prospects {where} ORDER BY etablissement ASC LIMIT ? OFFSET ?", params + [per_page, offset])
    data = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(data=data, total=total, page=page, per_page=per_page,
                   pages=max(1, (total + per_page - 1) // per_page))

@app.route('/api/stats')
@login_required
def api_stats():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total FROM prospects")
    total = c.fetchone()['total']
    c.execute("SELECT COUNT(*) as clients FROM prospects WHERE statut = 'Client !'")
    clients = c.fetchone()['clients']
    c.execute("SELECT COUNT(*) as relances FROM prospects WHERE date_relance <= date('now') AND statut != 'Client !'")
    relances = c.fetchone()['relances']
    c.execute("SELECT categorie, COUNT(*) as nb, SUM(CASE WHEN statut='Client !' THEN 1 ELSE 0 END) as nb_clients FROM prospects GROUP BY categorie")
    par_categorie = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(total=total, clients=clients, relances_en_retard=relances, par_categorie=par_categorie)

@app.route('/api/check-duplicate')
@login_required
def api_check_duplicate():
    nom   = request.args.get('nom', '').strip()
    email = request.args.get('email', '').strip()
    pid   = request.args.get('exclude_id', '')
    conditions, params = [], []
    if nom:
        conditions.append("etablissement LIKE ?"); params.append(f'%{nom}%')
    if email:
        conditions.append("email = ?"); params.append(email)
    if not conditions:
        return jsonify(doublons=[])
    where = "WHERE (" + " OR ".join(conditions) + ")"
    if pid and pid.isdigit():
        where += " AND id != ?"; params.append(pid)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(f"SELECT id, etablissement, email, statut FROM prospects {where} LIMIT 5", params)
    doublons = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(doublons=doublons)

# ─────────────────────────────────────────────
# NOTE RAPIDE
# ─────────────────────────────────────────────

@app.route('/prospect/<int:id>/note-rapide', methods=['POST'])
@login_required
def note_rapide(id):
    note = request.form.get('note', '').strip()
    if note:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT INTO interventions (prospect_id, date_intervention, type_contact, compte_rendu) VALUES (?, ?, 'Note', ?)",
                  (id, datetime.now().strftime('%Y-%m-%d'), note))
        conn.commit()
        conn.close()
    return redirect(request.referrer or url_for('base_donnees'))

# ─────────────────────────────────────────────
# NOTIFICATIONS RELANCES
# ─────────────────────────────────────────────

@app.route('/admin/envoyer-relances', methods=['POST'])
@login_required
@admin_required
def admin_envoyer_relances():
    cfg = get_smtp_config()
    if not cfg['smtp_host'] or not cfg['smtp_port']:
        return redirect(url_for('admin') + '?tab=mailing&error=SMTP+non+configuré')
    admin_email = get_setting('company_email', '')
    if not admin_email:
        return redirect(url_for('admin') + '?tab=mailing&error=Email+entreprise+non+configuré+dans+Admin+%3E+Personnalisation')
    aujourdhui = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, etablissement, contact, telephone, statut FROM prospects WHERE date_relance <= ? AND statut != 'Client !' ORDER BY date_relance ASC", (aujourdhui,))
    relances = c.fetchall()
    conn.close()
    if not relances:
        return redirect(url_for('admin') + '?tab=mailing&success=Aucune+relance+en+attente+à+envoyer')
    cn = get_setting('company_name', 'CRM')
    lignes = ''.join(
        f"<tr><td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'><a href='http://localhost:5000/prospect/{r['id']}'>{r['etablissement']}</a></td>"
        f"<td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'>{r['contact'] or '—'}</td>"
        f"<td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'>{r['telephone'] or '—'}</td>"
        f"<td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'>{r['statut'] or '—'}</td></tr>"
        for r in relances
    )
    body = f"""<h2 style='font-family:sans-serif;'>Résumé des relances — {datetime.now().strftime('%d/%m/%Y')}</h2>
    <p style='font-family:sans-serif; color:#64748b;'>{len(relances)} prospect(s) à relancer :</p>
    <table style='border-collapse:collapse; font-family:sans-serif; font-size:14px; width:100%;'>
    <thead><tr style='background:#f1f5f9;'>
    <th style='padding:8px 12px; text-align:left;'>Établissement</th>
    <th style='padding:8px 12px; text-align:left;'>Contact</th>
    <th style='padding:8px 12px; text-align:left;'>Téléphone</th>
    <th style='padding:8px 12px; text-align:left;'>Statut</th>
    </tr></thead><tbody>{lignes}</tbody></table>"""
    try:
        port = int(cfg['smtp_port'])
        if cfg['smtp_secure'] == 'ssl':
            server = smtplib.SMTP_SSL(cfg['smtp_host'], port, timeout=15)
        else:
            server = smtplib.SMTP(cfg['smtp_host'], port, timeout=15)
            if cfg['smtp_secure'] == 'tls':
                server.starttls()
        if cfg['smtp_user'] and cfg['smtp_pass']:
            server.login(cfg['smtp_user'], cfg['smtp_pass'])
        msg = MIMEMultipart('alternative')
        msg['From'] = cfg['smtp_from'] or cfg['smtp_user']
        msg['To'] = admin_email
        msg['Subject'] = f"[{cn}] {len(relances)} relance(s) à effectuer aujourd'hui"
        msg.attach(MIMEText(body, 'html', 'utf-8'))
        server.sendmail(msg['From'], admin_email, msg.as_string())
        server.quit()
        return redirect(url_for('admin') + f'?tab=mailing&success=Résumé+envoyé+à+{admin_email}')
    except Exception as e:
        return redirect(url_for('admin') + f'?tab=mailing&error=Erreur+SMTP+:+{str(e)[:80]}')

@app.route('/favicon.ico')
def favicon():
    logo_filename = get_setting('logo_filename', '')
    if logo_filename:
        return send_from_directory(app.static_folder, logo_filename)
    return send_from_directory(app.static_folder, 'favicon.svg')

@app.route('/admin/upload-logo', methods=['POST'])
@login_required
@admin_required
def admin_upload_logo():
    if 'logo' not in request.files or request.files['logo'].filename == '':
        return redirect(url_for('admin') + '?tab=perso&error=Aucun+fichier+sélectionné')
    file = request.files['logo']
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in {'png', 'jpg', 'jpeg', 'svg', 'ico', 'gif', 'webp'}:
        return redirect(url_for('admin') + '?tab=perso&error=Format+non+supporté')
    filename = f'logo_custom.{ext}'
    file.save(os.path.join(app.static_folder, filename))
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('logo_filename', ?)", (filename,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=perso&success=Logo+enregistré')

@app.route('/admin/smtp', methods=['POST'])
@login_required
@admin_required
def admin_smtp():
    fields = ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_from', 'smtp_from_name', 'smtp_secure']
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for field in fields:
        value = request.form.get(field, '').strip()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (field, value))
    # Ne remplace le mot de passe que s'il est fourni
    new_pass = request.form.get('smtp_pass', '').strip()
    if new_pass:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('smtp_pass', ?)", (new_pass,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=mailing&success=Configuration+SMTP+enregistrée')

@app.route('/admin/smtp-check', methods=['POST'])
@login_required
@admin_required
def admin_smtp_check():
    cfg = get_smtp_config()
    steps = []
    def ok(msg): steps.append({'status': 'ok', 'msg': msg})
    def err(msg): steps.append({'status': 'err', 'msg': msg})

    if not cfg['smtp_host'] or not cfg['smtp_port']:
        err("Hôte ou port non configuré.")
        return jsonify(steps=steps)
    try:
        port = int(cfg['smtp_port'])
        secure = cfg['smtp_secure']
        if secure == 'ssl':
            server = smtplib.SMTP_SSL(cfg['smtp_host'], port, timeout=10)
            ok(f"Connexion SSL établie sur {cfg['smtp_host']}:{port}")
        else:
            server = smtplib.SMTP(cfg['smtp_host'], port, timeout=10)
            ok(f"Connexion établie sur {cfg['smtp_host']}:{port}")
            if secure == 'tls':
                server.starttls()
                ok("STARTTLS activé (chiffrement OK)")
        if cfg['smtp_user'] and cfg['smtp_pass']:
            server.login(cfg['smtp_user'], cfg['smtp_pass'])
            ok(f"Authentification réussie ({cfg['smtp_user']})")
        else:
            ok("Aucune authentification configurée (anonyme)")
        server.quit()
    except smtplib.SMTPAuthenticationError:
        err("Échec d'authentification — vérifiez le login / mot de passe.")
    except smtplib.SMTPConnectError as e:
        err(f"Impossible de se connecter : {e}")
    except ConnectionRefusedError:
        err(f"Connexion refusée sur {cfg['smtp_host']}:{cfg['smtp_port']}")
    except TimeoutError:
        err(f"Délai dépassé — hôte injoignable ou port bloqué.")
    except Exception as e:
        err(f"Erreur : {str(e)}")
    return jsonify(steps=steps)

@app.route('/admin/smtp-test', methods=['POST'])
@login_required
@admin_required
def admin_smtp_test():
    data = request.get_json(silent=True) or {}
    to_email = data.get('to_email', '').strip()
    if not to_email:
        return jsonify(ok=False, message="Adresse email de destination manquante.")
    cfg = get_smtp_config()
    if not cfg['smtp_host'] or not cfg['smtp_port']:
        return jsonify(ok=False, message="SMTP non configuré (hôte ou port manquant).")
    try:
        msg = MIMEMultipart('alternative')
        from_label = cfg['smtp_from_name'] or cfg['smtp_from'] or cfg['smtp_user']
        msg['From'] = f"{from_label} <{cfg['smtp_from'] or cfg['smtp_user']}>"
        msg['To'] = to_email
        msg['Subject'] = f"[{get_setting('company_name', 'CRM')}] Email de test"
        body = MIMEText(
            f"<p>Bonjour,</p><p>Ceci est un email de test envoyé depuis votre CRM <strong>{get_setting('company_name','')}</strong>.</p><p>La configuration SMTP fonctionne correctement.</p>",
            'html', 'utf-8'
        )
        msg.attach(body)
        port = int(cfg['smtp_port'])
        secure = cfg['smtp_secure']
        if secure == 'ssl':
            server = smtplib.SMTP_SSL(cfg['smtp_host'], port, timeout=10)
        else:
            server = smtplib.SMTP(cfg['smtp_host'], port, timeout=10)
            if secure == 'tls':
                server.starttls()
        if cfg['smtp_user'] and cfg['smtp_pass']:
            server.login(cfg['smtp_user'], cfg['smtp_pass'])
        server.sendmail(msg['From'], to_email, msg.as_string())
        server.quit()
        return jsonify(ok=True, message=f"Email envoyé avec succès à {to_email}.")
    except Exception as e:
        return jsonify(ok=False, message=f"Erreur : {str(e)}")

@app.route('/admin/settings', methods=['POST'])
@login_required
@admin_required
def admin_settings():
    fields = ['company_name', 'company_adresse', 'company_cp', 'company_ville',
              'company_region', 'company_telephone', 'company_email', 'company_site',
              'company_siret', 'company_tva', 'company_forme', 'company_naf']
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for field in fields:
        val = request.form.get(field, '').strip()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (field, val))
    conn.commit()
    conn.close()
    return redirect(url_for('admin') + '?tab=perso&success=Informations+enregistrées')

@app.route('/admin/reset', methods=['POST'])
@login_required
@admin_required
def admin_reset():
    confirmation = request.form.get('confirmation', '').strip()
    if confirmation != 'RESET':
        return redirect(url_for('admin') + '?tab=danger&error=Confirmation+incorrecte')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM interventions")
    c.execute("DELETE FROM historique_statut")
    c.execute("DELETE FROM prospects")
    c.execute("DELETE FROM evenements_perso")
    c.execute("DELETE FROM logs")
    conn.commit()
    conn.close()
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    conn2 = sqlite3.connect(DB_NAME)
    c2 = conn2.cursor()
    c2.execute("INSERT INTO logs (username, action, ip) VALUES (?, ?, ?)", (session.get('username'), 'REMISE À ZÉRO COMPLÈTE', ip))
    conn2.commit()
    conn2.close()
    return redirect(url_for('admin') + '?tab=danger&success=Remise+à+zéro+effectuée')

@app.route('/admin/logs')
@login_required
@admin_required
def admin_logs():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM logs ORDER BY horodatage DESC LIMIT 500")
    logs = c.fetchall()
    conn.close()
    return render_template('logs.html', logs=logs)

# ─────────────────────────────────────────────
# STATS PAR COMMERCIAL
# ─────────────────────────────────────────────

@app.route('/admin/stats-commerciaux')
@login_required
@admin_required
def admin_stats_commerciaux():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT u.username, u.role,
            (SELECT COUNT(*) FROM historique_statut WHERE username = u.username) as nb_actions,
            (SELECT COUNT(*) FROM email_logs WHERE sender_username = u.username) as nb_emails,
            (SELECT COUNT(DISTINCT prospect_id) FROM historique_statut
                WHERE username = u.username AND nouveau_statut = 'Client !') as nb_conversions,
            (SELECT COUNT(*) FROM interventions WHERE username = u.username) as nb_interventions
        FROM users u
        ORDER BY nb_actions DESC
    """)
    stats = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('stats_commerciaux.html', stats=stats)

# ─────────────────────────────────────────────
# SCHEDULER (relances auto + backup auto)
# ─────────────────────────────────────────────

def _send_relances_auto():
    """Envoi automatique du résumé relances (8h chaque jour)."""
    try:
        if get_setting('relances_auto_enabled', '0') != '1':
            return
        cfg = get_smtp_config()
        admin_email = get_setting('company_email', '')
        if not cfg['smtp_host'] or not cfg['smtp_port'] or not admin_email:
            return
        aujourdhui = datetime.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, etablissement, contact, telephone, statut FROM prospects WHERE date_relance <= ? AND statut != 'Client !' AND (archived IS NULL OR archived=0) ORDER BY date_relance ASC", (aujourdhui,))
        relances = c.fetchall()
        conn.close()
        if not relances:
            return
        cn = get_setting('company_name', 'CRM')
        lignes = ''.join(
            f"<tr><td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'>{r['etablissement']}</td>"
            f"<td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'>{r['contact'] or '—'}</td>"
            f"<td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'>{r['telephone'] or '—'}</td>"
            f"<td style='padding:8px 12px; border-bottom:1px solid #e2e8f0;'>{r['statut'] or '—'}</td></tr>"
            for r in relances
        )
        body = f"""<h2 style='font-family:sans-serif;'>Résumé des relances — {datetime.now().strftime('%d/%m/%Y')}</h2>
        <p style='font-family:sans-serif; color:#64748b;'>{len(relances)} prospect(s) à relancer :</p>
        <table style='border-collapse:collapse; font-family:sans-serif; font-size:14px; width:100%;'>
        <thead><tr style='background:#f1f5f9;'>
        <th style='padding:8px 12px; text-align:left;'>Établissement</th>
        <th style='padding:8px 12px; text-align:left;'>Contact</th>
        <th style='padding:8px 12px; text-align:left;'>Téléphone</th>
        <th style='padding:8px 12px; text-align:left;'>Statut</th>
        </tr></thead><tbody>{lignes}</tbody></table>"""
        port = int(cfg['smtp_port'])
        if cfg['smtp_secure'] == 'ssl':
            server = smtplib.SMTP_SSL(cfg['smtp_host'], port, timeout=15)
        else:
            server = smtplib.SMTP(cfg['smtp_host'], port, timeout=15)
            if cfg['smtp_secure'] == 'tls':
                server.starttls()
        if cfg['smtp_user'] and cfg['smtp_pass']:
            server.login(cfg['smtp_user'], cfg['smtp_pass'])
        msg = MIMEMultipart('alternative')
        msg['From'] = cfg['smtp_from'] or cfg['smtp_user']
        msg['To'] = admin_email
        msg['Subject'] = f"[{cn}] {len(relances)} relance(s) à effectuer aujourd'hui"
        msg.attach(MIMEText(body, 'html', 'utf-8'))
        server.sendmail(msg['From'], admin_email, msg.as_string())
        server.quit()
    except Exception as e:
        print(f"[Scheduler] Erreur relances auto: {e}")

def _auto_backup():
    """Sauvegarde automatique quotidienne (2h du matin)."""
    try:
        backup_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup.py')
        subprocess.run(['python3', backup_script], timeout=60, check=True)
        print(f"[Scheduler] Backup automatique effectué à {datetime.now()}")
    except Exception as e:
        print(f"[Scheduler] Erreur backup auto: {e}")

def _start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(_send_relances_auto, 'cron', hour=8, minute=0, id='relances_auto')
    scheduler.add_job(_auto_backup, 'cron', hour=2, minute=0, id='backup_auto')
    scheduler.start()
    return scheduler

# ─────────────────────────────────────────────
# TOGGLE RELANCES AUTO (admin)
# ─────────────────────────────────────────────

@app.route('/admin/toggle-relances-auto', methods=['POST'])
@login_required
@admin_required
def admin_toggle_relances_auto():
    val = request.form.get('relances_auto_enabled', '0')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('relances_auto_enabled', ?)", (val,))
    conn.commit()
    conn.close()
    state = 'activées' if val == '1' else 'désactivées'
    return redirect(url_for('admin') + f'?tab=mailing&success=Relances+automatiques+{state}')

# ─────────────────────────────────────────────
# LANCEMENT
# ─────────────────────────────────────────────

# Initialisation au démarrage (fonctionne avec python app.py et gunicorn)
init_db()
_start_scheduler()

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=debug_mode)

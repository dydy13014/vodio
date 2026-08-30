"""Store de configuration persistant (JSON) — évite d'éditer un fichier .env
à la main pour un déploiement public. Premier démarrage sans configuration :
un code admin est généré et affiché une fois dans les logs, la page /setup
permet de tout saisir depuis un formulaire.

Les vraies variables d'environnement gardent toujours la priorité : si une
clé est déjà définie via l'environnement (ex. déploiement existant piloté par
un vodio.env), ce store ne change rien pour elle — il ne fait que préremplir
les clés absentes, une seule fois au démarrage (`apply_to_environ`), avant
que le reste de l'appli ne lise sa configuration."""
import json
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("vodio.settings")

SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "/app/data/settings.json"))

# (clé env, obligatoire, secret, libellé, description courte, recommandé)
# "recommandé" = débloque une fonctionnalité entière (pas juste un réglage
# cosmétique) — affiché avec un badge distinct de l'astérisque (réservé aux
# champs strictement obligatoires) dans /setup.
FIELDS: list[tuple[str, bool, bool, str, str, bool]] = [
    ("TMDB_API_KEY", True, True, "Clé API TMDB", "Obligatoire — gratuite sur themoviedb.org/settings/api", False),
    ("VODIO_PASSWORD", True, True, "Mot de passe watchlist", "Obligatoire — protège la page web et les API", False),
    ("VODIO_DEFAULT_NAME", False, False, "Nom du compte principal", "Laisser vide pour se connecter avec le champ Nom vide", False),
    ("ALLOCINE_PAGES", False, False, "Pages AlloCiné scrapées", "Défaut : 5", False),
    ("REFRESH_HOURS", False, False, "Intervalle de rafraîchissement (heures)", "Défaut : 24", False),
    ("C411_URL", False, False, "URL Torznab C411", "ex. https://c411.org/api", True),
    ("C411_API_KEY", False, True, "Clé API C411", "Trouvée sur votre profil/compte C411, section API", True),
    ("TR4KER_URL", False, False, "URL Torznab Tr4ker", "ex. https://tr4ker.net/api", False),
    ("TR4KER_API_KEY", False, True, "Clé API Tr4ker", "Trouvée sur votre profil/compte Tr4ker, section API", False),
    ("V3X_URL", False, False, "URL Torznab V3X", "ex. https://api.v3x.club/torznab/api", False),
    ("V3X_API_KEY", False, True, "Clé API V3X", "Trouvée sur votre profil/compte V3X, section API", False),
    ("STREAM_CHECK_URL", False, False, "URL interne de votre AIOStreams", "ex. http://aiostreams:3000", False),
    ("STREAM_CHECK_CONFIG", False, True, "Config AIOStreams", "stremio/<uuid>/<credentials-chiffrés>", False),
    ("VODIO_QUALITY_MIN", False, False, "Résolution minimale pour le badge 🧲", "Défaut : 720", False),
    ("WACUSTOM_URL", False, False, "URL Wacustom (watchlist)", "ex. http://wacustom:7000", False),
    ("WACUSTOM_CONFIG", False, True, "Config Wacustom", "", False),
    ("ALLDEBRID_API_KEY", False, True, "Clé API AllDebrid", "Générée sur alldebrid.com > Mon compte > Clé API (Apikey)", True),
    ("MEDIAFLOW_URL", False, False, "URL de votre MediaFlow-Proxy", "ex. https://votre-domaine.tld/mf", True),
    ("MEDIAFLOW_API_PASSWORD", False, True, "Mot de passe MediaFlow", "Le mot de passe API défini dans la config de votre instance MediaFlow-Proxy", True),
    ("VODIO_EXTRA_USERS", False, True, "Utilisateurs additionnels", "nom1:motdepasse1,nom2:motdepasse2", True),
    ("VODIO_NOSMS_USERS", False, False, "Utilisateurs exclus des SMS", "nom1,nom2", False),
    ("VODIO_SMS_USER", False, False, "Identifiant Free Mobile", "Votre identifiant Free Mobile (mobile.free.fr > Mes options > Notifications par SMS)", False),
    ("VODIO_SMS_PASS", False, True, "Clé API SMS Free Mobile", "Clé générée en activant l'option « Notifications par SMS » sur mobile.free.fr", False),
    ("VODIO_ADDON_ID", False, False, "Identifiant technique de l'addon", "À laisser vide dans la quasi totalité des cas. Utile seulement si vous installez plusieurs instances de VODIO (ex. une pour vous, une pour un ami) : donnez à chacune un ID différent pour éviter que Stremio les confonde. Défaut : org.selfhosted.vodio, ex. perso : org.monpseudo.vodio", False),
    ("VODIO_ADDON_NAME", False, False, "Nom affiché dans la liste des addons Stremio", "Défaut : VODIO. À changer seulement pour renommer l'addon ou distinguer plusieurs instances", False),
    ("RPDB_API_KEY", False, True, "Clé API RPDB (jaquettes avec note)", "Clé gratuite sur ratingposterdb.com", False),
    ("VODIO_BASE_URL", False, False, "URL publique de cette instance", "requis pour RPDB — ex. https://votre-domaine.tld/vodio", False),
]

FIELD_KEYS = {f[0] for f in FIELDS}

# STREAM_CHECK_URL/CONFIG et WACUSTOM_URL/CONFIG restent des champs à part
# entière (FIELDS ci-dessus, pour is_configured()/l'état "défini") mais sont
# masqués du formulaire /setup — remplacés par un champ composite unique :
# coller l'URL complète du manifest Stremio (AIOStreams/Wacustom exposent
# tous les deux le même format .../stremio/<uuid>/<config>/manifest.json),
# décomposée automatiquement en (base, config) par `parse_manifest_url`.
# Évite de demander à un utilisateur non technique de séparer lui-même
# l'URL. Édition manuelle des deux champs toujours possible via un vrai
# fichier .env (cf. .env.example) pour les cas avancés (ex. host Docker
# interne différent de l'URL publique).
# VODIO_ADDON_ID en plus : généré automatiquement (cf. apply_to_environ),
# rien à saisir dans l'immense majorité des cas.
HIDDEN_FROM_FORM = {"STREAM_CHECK_URL", "STREAM_CHECK_CONFIG", "WACUSTOM_URL", "WACUSTOM_CONFIG", "VODIO_ADDON_ID"}

# (clé pseudo, clé URL réelle, clé config réelle, libellé, indice, recommandé)
COMPOSITE_FIELDS: list[tuple[str, str, str, str, str, bool]] = [
    ("STREAM_CHECK_MANIFEST", "STREAM_CHECK_URL", "STREAM_CHECK_CONFIG",
     "Manifest AIOStreams",
     "Sur votre instance AIOStreams : ouvrez /stremio/configure, configurez vos sources/proxy, puis récupérez l'URL de manifest générée à la fin (bouton « Installer » ou lien copiable) — collez-la ici. Format : https://host/stremio/<uuid>/<config>/manifest.json",
     True),
    ("WACUSTOM_MANIFEST", "WACUSTOM_URL", "WACUSTOM_CONFIG",
     "Manifest Wacustom",
     "Sur votre instance Wacustom : ouvrez /configure, configurez vos trackers/proxy, puis collez ici l'URL de manifest générée à la fin",
     True),
]


def parse_manifest_url(url: str) -> tuple[str, str] | None:
    """Découpe une URL de manifest Stremio (.../stremio/<uuid>/<config>/
    manifest.json) en (base, config) pour STREAM_CHECK_URL/CONFIG ou
    WACUSTOM_URL/CONFIG. None si l'URL n'a pas la forme attendue."""
    from urllib.parse import urlsplit

    url = (url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.strip("/")
    if path.endswith("manifest.json"):
        path = path[: -len("manifest.json")].strip("/")
    if not path:
        return None
    return base, path


def load() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("settings.json illisible, ignoré : %s", exc)
        return {}


def save(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def apply_to_environ() -> None:
    """À appeler avant toute autre lecture de configuration (premier import
    de main.py) : préremplit os.environ avec les valeurs stockées, pour les
    clés pas déjà définies par le vrai environnement."""
    data = load()
    for key, value in data.items():
        if key in FIELD_KEYS and value and key not in os.environ:
            os.environ[key] = str(value)

    # ID d'addon : généré automatiquement une seule fois puis figé (jamais
    # régénéré), pour distinguer deux instances différentes sans demander à
    # l'utilisateur de choisir/comprendre un identifiant technique. Une vraie
    # variable d'environnement (déploiement répliqué volontairement sur
    # plusieurs machines avec le même ID, ex. bascule prod/standby) garde
    # toujours la priorité.
    if "VODIO_ADDON_ID" not in os.environ:
        addon_id = data.get("VODIO_ADDON_ID")
        if not addon_id:
            addon_id = f"org.selfhosted.vodio-{secrets.token_hex(4)}"
            data["VODIO_ADDON_ID"] = addon_id
            save(data)
        os.environ["VODIO_ADDON_ID"] = addon_id


def is_configured() -> bool:
    return bool(os.environ.get("TMDB_API_KEY")) and bool(os.environ.get("VODIO_PASSWORD"))


def get_or_create_setup_code() -> str:
    data = load()
    code = data.get("_setup_code")
    if not code:
        code = secrets.token_urlsafe(9)
        data["_setup_code"] = code
        save(data)
    return code


def save_settings(fields: dict) -> None:
    """Enregistre les champs non vides du formulaire (ignore les champs
    vides pour ne pas écraser une valeur déjà en place — un champ secret
    laissé vide dans le formulaire d'édition signifie « ne pas changer »)."""
    data = load()
    for key in FIELD_KEYS:
        value = fields.get(key)
        if value:
            data[key] = value.strip() if isinstance(value, str) else value
    save(data)

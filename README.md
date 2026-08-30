# VODIO

Addon [Stremio](https://www.stremio.com/) **self-hosté** qui apporte les **sorties VOD et torrent françaises** dans Stremio, avec une **watchlist personnelle** gérée depuis une page web.

Sources des nouveautés :
- [AlloCiné](https://www.allocine.fr/vod/films/new/) — films récemment sortis en VOD en France.
- Un tracker Torznab (ex. C411) — sorties complémentaires (documentaires, séries étrangères…) qui n'apparaissent jamais sur AlloCiné VOD. *(optionnel)*

Chaque titre est matché avec [TMDB](https://www.themoviedb.org/) pour récupérer son ID IMDb — ce qui permet aux autres addons (métadonnées, sources) de fonctionner normalement au clic.

## Fonctionnalités

- **Configuration par page web** — aucun fichier à éditer : au premier démarrage, un code admin s'affiche dans les logs, `/setup` permet de tout saisir depuis un formulaire.
- **Catalogue « Nouveautés VOD »** — les derniers films sortis en VOD en France (AlloCiné).
- **Catalogue « Nouveautés Torrent »** *(optionnel)* — films et séries récents sourcés d'un tracker Torznab, filtrés (audio français, résolution minimale), dédupliqués.
- **Watchlist perso** (films **et** séries) — page web protégée par mot de passe : cherchez un titre, ajoutez-le, il apparaît dans Stremio dans « VODIO - Ma liste ».
- **Multi-utilisateurs** *(optionnel)* — chaque personne a sa propre watchlist et son propre mot de passe, un seul formulaire de connexion pour tous.
- **Badges de disponibilité ✅/⏳** *(optionnel)* — si vous avez une instance [AIOStreams](https://github.com/Viren070/AIOStreams), VODIO indique si des sources en bonne qualité existent (seuil configurable, 720p par défaut).
- **Agenda cinéma** — sorties en salle de la semaine (AlloCiné), purement informatif, pour ajouter à la watchlist en avance.
- **Bandes-annonces** — récupérées à la demande depuis TMDB, affichées sur la fiche d'un titre.
- **Jaquettes avec note incrustée** *(optionnel)* — via [RPDB](https://ratingposterdb.com), en complément de TMDB.
- **Précache et téléchargement direct** *(optionnel, nécessite Wacustom + AllDebrid)* — préchargez un film ou une saison entière d'un clic ; téléchargez un film disponible directement depuis la page web (relayé via MediaFlow-Proxy).
- **Watchlist enrichie via Lumio** *(optionnel)* — signal complémentaire sur la watchlist perso pour confirmer plus vite qu'un titre est en cache.
- **Notifications SMS** *(optionnel, Free Mobile)* — soyez prévenu quand un titre de votre liste devient disponible.
- **PWA** — installable sur l'écran d'accueil du téléphone.
- **Changelog intégré** — cliquer sur le numéro de version dans l'appli affiche les nouveautés.

## Prérequis

- Docker + Docker Compose
- Une clé API TMDB (gratuite) : https://www.themoviedb.org/settings/api
- *(optionnel)* Une instance AIOStreams pour les badges de disponibilité
- *(optionnel)* Un compte sur un tracker Torznab pour le catalogue « Nouveautés Torrent »
- *(optionnel)* Wacustom + un compte AllDebrid (+ MediaFlow-Proxy) pour la précache/téléchargement
- *(optionnel)* Un reverse-proxy (Traefik, Nginx…) pour l'exposer en HTTPS

## Installation

```bash
git clone <ce-repo> vodio && cd vodio
cp docker-compose.example.yml docker-compose.yml   # adapter le port/volume si besoin
docker compose up -d --build
```

L'addon écoute sur le port `8000`. **Aucun fichier à éditer avant le premier lancement** : au démarrage, si `TMDB_API_KEY`/mot de passe ne sont pas définis, VODIO affiche un code admin dans ses logs et bascule sur une page de configuration.

```bash
docker logs vodio   # cherchez : "VODIO n'est pas configuré === Rendez-vous sur /setup avec ce code : XXXX"
```

Rendez-vous sur `http://<votre-ip>:8000/setup`, entrez ce code, remplissez les champs voulus (seuls `TMDB_API_KEY` et le mot de passe watchlist sont obligatoires, tout le reste est optionnel — cf. [Configuration](#configuration)), puis redémarrez le conteneur (`docker compose restart`) pour appliquer.

- **Page web (watchlist)** : `http://<votre-ip>:8000/`
- **Manifest à installer dans Stremio** : `http://<votre-ip>:8000/manifest.json`
- **Configuration / admin** : `http://<votre-ip>:8000/setup` (redevient `/admin`, protégé par le mot de passe du compte principal, une fois l'instance configurée)

> ⚠️ La page web et les endpoints `/api/*` sont protégés par mot de passe. Le manifest et les catalogues restent publics (Stremio en a besoin). Exposez de préférence derrière un reverse-proxy HTTPS.

## Configuration

Deux façons équivalentes de configurer VODIO — un déploiement peut mélanger les deux, les variables d'environnement gardent toujours la priorité :

1. **Page web `/setup` puis `/admin`** (recommandé) — décrite ci-dessus, rien à éditer à la main. Chaque champ est décrit dans l'interface.
2. **Fichier `.env`** (déploiement classique) — copier [`.env.example`](.env.example) en `vodio.env`, remplir, `chmod 600`, et référencer `env_file: vodio.env` dans votre `docker-compose.yml`. Toutes les variables y sont commentées et groupées par fonctionnalité.

Dans les deux cas, les seules valeurs obligatoires sont `TMDB_API_KEY` et le mot de passe watchlist (`VODIO_PASSWORD`). Tout le reste (nouveautés torrent, badges de dispo, watchlist renforcée, précache/DL, multi-utilisateurs, SMS, jaquettes RPDB) est optionnel et se désactive proprement si non configuré.

### Badges de disponibilité

VODIO interroge votre AIOStreams pour savoir si un titre a des sources. Depuis `/setup`, collez simplement l'URL complète du manifest de votre compte (page `/stremio/configure` d'AIOStreams) — elle est décomposée automatiquement. Via `.env`, les deux variables équivalentes :

- `STREAM_CHECK_URL` — l'URL de votre AIOStreams (interne, ex. `http://aiostreams:3000`, ou publique)
- `STREAM_CHECK_CONFIG` — le segment `stremio/<uuid>/<credentials>` visible dans l'URL de votre manifest AIOStreams

Sans ces variables, tous les films vont dans « Nouveautés VOD » sans badge de disponibilité.

### Précache et téléchargement

Ces fonctions sont spécifiques à **Wacustom + AllDebrid**. Depuis `/setup`, collez l'URL complète du manifest de votre compte Wacustom (page `/configure`) — décomposée automatiquement, comme pour AIOStreams. Via `.env`, les variables équivalentes : `WACUSTOM_URL`, `WACUSTOM_CONFIG`, `ALLDEBRID_API_KEY`, `MEDIAFLOW_URL`, `MEDIAFLOW_API_PASSWORD` — sans elles, la watchlist retombe sur le même check AIOStreams que ci-dessus et les boutons précache/téléchargement restent masqués, le reste de l'appli fonctionne normalement.

### Jaquettes RPDB

Nécessite `RPDB_API_KEY` **et** `VODIO_BASE_URL` (l'URL publique de votre instance). Cette dernière est obligatoire pour cette fonctionnalité : les posters sont servis via un relais interne (`/poster/...`) qui a besoin de connaître sa propre URL publique — sans quoi la clé RPDB se retrouverait exposée en clair dans les catalogues Stremio (publics, sans authentification).

### Watchlist enrichie (Lumio)

`LUMIO_MANIFEST_ID` — signal complémentaire (cache mutualisé [mylumio.tv](https://mylumio.tv)) pour la **watchlist perso uniquement**. Confirme plus vite qu'un titre est déjà en cache, et sert de repli pour le téléchargement direct si Wacustom n'a plus de source exploitable. Volontairement absent du catalogue principal : son quota est trop bas pour vérifier des dizaines de titres à chaque refresh (il se bloque globalement, 24h, en cas de dépassement).

## Endpoints

| Route | Description |
|-------|-------------|
| `GET /manifest.json` | Manifest Stremio (public) |
| `GET /catalog/movie/vodio-new.json` | Nouveautés VOD |
| `GET /catalog/{movie,series}/vodio-c411-new.json` | Nouveautés Torrent *(si configuré)* |
| `GET /catalog/{movie,series}/vodio-watchlist.json` | Watchlist |
| `GET /poster/{imdb,tmdb}/{id}.jpg` | Relais poster RPDB (public, ne sert jamais la clé) *(si configuré)* |
| `GET /` | Page web de gestion (ou page de configuration si instance pas encore configurée) |
| `GET /setup` / `GET /admin` | Configuration (première fois par code, ensuite par mot de passe admin) |
| `POST /api/setup` | Enregistre la configuration (public par code tant que non configuré, protégé ensuite) |
| `GET /health` | 503 si le scrape AlloCiné est cassé (monitoring) |
| `POST /api/login` / `POST /api/logout` | Session (mot de passe) |
| `GET /api/{vod,cinema,c411}` | Données brutes des catalogues, avec badges (protégés) |
| `/api/watchlist/*` | Recherche / ajout / précache / téléchargement (protégés) |

## Avertissement

Cet outil agrège des métadonnées publiques (AlloCiné, TMDB) et s'intègre à votre propre configuration Stremio/debrid/tracker. Vous êtes seul responsable de l'usage que vous en faites et du contenu auquel vous accédez via vos autres addons. Fourni tel quel, sans garantie.

## Licence

MIT — voir [LICENSE](LICENSE).

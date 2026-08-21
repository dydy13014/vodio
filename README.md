# VODIO

Addon [Stremio](https://www.stremio.com/) **self-hosté** qui apporte les **sorties VOD et torrent françaises** dans Stremio, avec une **watchlist personnelle** gérée depuis une page web.

Sources des nouveautés :
- [AlloCiné](https://www.allocine.fr/vod/films/new/) — films récemment sortis en VOD en France.
- Un tracker Torznab (ex. C411) — sorties complémentaires (documentaires, séries étrangères…) qui n'apparaissent jamais sur AlloCiné VOD. *(optionnel)*

Chaque titre est matché avec [TMDB](https://www.themoviedb.org/) pour récupérer son ID IMDb — ce qui permet aux autres addons (métadonnées, sources) de fonctionner normalement au clic.

## Fonctionnalités

- **Catalogue « Nouveautés VOD »** — les derniers films sortis en VOD en France (AlloCiné).
- **Catalogue « Nouveautés Torrent »** *(optionnel)* — films et séries récents sourcés d'un tracker Torznab, filtrés (audio français, résolution minimale), dédupliqués.
- **Watchlist perso** (films **et** séries) — page web protégée par mot de passe : cherchez un titre, ajoutez-le, il apparaît dans Stremio dans « VODIO - Ma liste ».
- **Multi-utilisateurs** *(optionnel)* — chaque personne a sa propre watchlist et son propre mot de passe, un seul formulaire de connexion pour tous.
- **Badges de disponibilité ✅/⏳** *(optionnel)* — si vous avez une instance [AIOStreams](https://github.com/Viren070/AIOStreams), VODIO indique si des sources en bonne qualité existent (seuil configurable, 720p par défaut).
- **Agenda cinéma** — sorties en salle de la semaine (AlloCiné), purement informatif, pour ajouter à la watchlist en avance.
- **Bandes-annonces** — récupérées à la demande depuis TMDB, affichées sur la fiche d'un titre.
- **Précache et téléchargement direct** *(optionnel, nécessite Wacustom + AllDebrid)* — préchargez un film ou une saison entière d'un clic ; téléchargez un film disponible directement depuis la page web (relayé via MediaFlow-Proxy).
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
cp .env.example vodio.env         # puis remplir TMDB_API_KEY + VODIO_PASSWORD
chmod 600 vodio.env
cp docker-compose.example.yml docker-compose.yml   # adapter si besoin
docker compose up -d --build
```

L'addon écoute sur le port `8000`.

- **Page web (watchlist)** : `http://<votre-ip>:8000/`
- **Manifest à installer dans Stremio** : `http://<votre-ip>:8000/manifest.json`

> ⚠️ La page web et les endpoints `/api/*` sont protégés par mot de passe. Le manifest et les catalogues restent publics (Stremio en a besoin). Exposez de préférence derrière un reverse-proxy HTTPS.

## Configuration

Toutes les variables sont dans [`.env.example`](.env.example) (commentées, groupées par fonctionnalité). Les seules obligatoires : `TMDB_API_KEY` et `VODIO_PASSWORD`. Tout le reste (nouveautés torrent, badges de dispo, watchlist renforcée, précache/DL, multi-utilisateurs, SMS) est optionnel et se désactive proprement si non configuré.

### Badges de disponibilité

VODIO interroge votre AIOStreams pour savoir si un titre a des sources. Renseignez :

- `STREAM_CHECK_URL` — l'URL interne de votre AIOStreams (ex. `http://aiostreams:3000`)
- `STREAM_CHECK_CONFIG` — le segment `stremio/<uuid>/<credentials>` visible dans l'URL de votre manifest AIOStreams

Sans ces variables, tous les films vont dans « Nouveautés VOD » sans badge de disponibilité.

### Précache et téléchargement

Ces fonctions sont spécifiques à **Wacustom + AllDebrid** (`WACUSTOM_URL`, `WACUSTOM_CONFIG`, `ALLDEBRID_API_KEY`) — sans ces variables, la watchlist retombe sur le même check AIOStreams que ci-dessus et les boutons précache/téléchargement restent masqués, le reste de l'appli fonctionne normalement.

## Endpoints

| Route | Description |
|-------|-------------|
| `GET /manifest.json` | Manifest Stremio (public) |
| `GET /catalog/movie/vodio-new.json` | Nouveautés VOD |
| `GET /catalog/{movie,series}/vodio-c411-new.json` | Nouveautés Torrent *(si configuré)* |
| `GET /catalog/{movie,series}/vodio-watchlist.json` | Watchlist |
| `GET /` | Page web de gestion |
| `GET /health` | 503 si le scrape AlloCiné est cassé (monitoring) |
| `POST /api/login` / `POST /api/logout` | Session (mot de passe) |
| `GET /api/{vod,cinema,c411}` | Données brutes des catalogues, avec badges (protégés) |
| `/api/watchlist/*` | Recherche / ajout / précache / téléchargement (protégés) |

## Avertissement

Cet outil agrège des métadonnées publiques (AlloCiné, TMDB) et s'intègre à votre propre configuration Stremio/debrid/tracker. Vous êtes seul responsable de l'usage que vous en faites et du contenu auquel vous accédez via vos autres addons. Fourni tel quel, sans garantie.

## Licence

MIT — voir [LICENSE](LICENSE).

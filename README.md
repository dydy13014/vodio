# VODIO

Addon [Stremio](https://www.stremio.com/) **self-hosté** qui apporte les **sorties VOD françaises** dans Stremio, avec une **watchlist personnelle** gérée depuis une page web.

Source des nouveautés : [AlloCiné](https://www.allocine.fr/vod/films/new/). Chaque titre est matché avec [TMDB](https://www.themoviedb.org/) pour récupérer son ID IMDb — ce qui permet aux autres addons (métadonnées, sources) de fonctionner normalement au clic.

## Fonctionnalités

- **Catalogue « Nouveautés VOD »** — les derniers films sortis en VOD en France.
- **Catalogue « Bientôt en VOD »** — les titres listés mais pas encore disponibles.
- **Watchlist perso** (films **et** séries) — page web protégée par mot de passe : cherchez un titre, ajoutez-le, il apparaît dans Stremio.
- **Badges de disponibilité ✅/⏳** *(optionnel)* — si vous avez une instance [AIOStreams](https://github.com/Viren070/AIOStreams), VODIO indique si des sources en bonne qualité existent (seuil configurable, 720p par défaut).
- **Notifications SMS** *(optionnel, Free Mobile)* — soyez prévenu quand un titre de votre liste devient disponible.
- **PWA** — installable sur l'écran d'accueil du téléphone.

## Prérequis

- Docker + Docker Compose
- Une clé API TMDB (gratuite) : https://www.themoviedb.org/settings/api
- *(optionnel)* Une instance AIOStreams pour les badges de disponibilité
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

> ⚠️ La page web et les endpoints `/api/*` sont protégés par `VODIO_PASSWORD`. Le manifest et les catalogues restent publics (Stremio en a besoin). Exposez de préférence derrière un reverse-proxy HTTPS.

## Configuration

Toutes les variables sont dans [`.env.example`](.env.example) (commentées). Les seules obligatoires : `TMDB_API_KEY` et `VODIO_PASSWORD`. Le reste (badges de dispo, SMS) est optionnel.

### Badges de disponibilité

VODIO interroge votre AIOStreams pour savoir si un titre a des sources. Renseignez :

- `STREAM_CHECK_URL` — l'URL interne de votre AIOStreams (ex. `http://aiostreams:3000`)
- `STREAM_CHECK_CONFIG` — le segment `stremio/<uuid>/<credentials>` visible dans l'URL de votre manifest AIOStreams

Sans ces variables, tous les films vont dans « Nouveautés VOD » et « Bientôt en VOD » reste vide.

## Endpoints

| Route | Description |
|-------|-------------|
| `GET /manifest.json` | Manifest Stremio (public) |
| `GET /catalog/movie/vodio-new.json` | Nouveautés VOD |
| `GET /catalog/movie/vodio-soon.json` | Bientôt en VOD |
| `GET /catalog/movie/vodio-watchlist.json` | Watchlist (films) |
| `GET /catalog/series/vodio-watchlist.json` | Watchlist (séries) |
| `GET /` | Page web de gestion |
| `GET /health` | 503 si le scrape est cassé (monitoring) |
| `/api/*` | Recherche / watchlist (protégés par mot de passe) |

## Avertissement

Cet outil agrège des métadonnées publiques (AlloCiné, TMDB) et s'intègre à votre propre configuration Stremio/debrid. Vous êtes seul responsable de l'usage que vous en faites et du contenu auquel vous accédez via vos autres addons. Fourni tel quel, sans garantie.

## Licence

MIT — voir [LICENSE](LICENSE).

"""Historique des versions de la page web VODIO — indépendant du numéro de
version du manifest Stremio (`MANIFEST["version"]` dans main.py, qui suit son
propre cycle technique). Rester sous la 1.0 tant que l'app est en
développement actif (consigne explicite du 2026-08-14). Nouvelle entrée en
tête de liste à chaque changement notable ; le changelog reprend aussi
l'historique des fonctionnalités livrées avant la création de cet onglet."""

VERSION = "0.13.0"

CHANGELOG = [
    {
        "version": "0.13.0",
        "date": "2026-08-22",
        "changes": [
            "Nouveaux badges de disponibilité : 🧲 (disponible), 🧲⚡ (disponible et confirmé par le cache mutualisé Lumio), ⏳ (indisponible) — visibles directement sur les cartes et la fiche détail",
            "Détection directe des sources CAM/télésynchro (au lieu de se fier uniquement au délai après la sortie salle) : deux blockbusters récents étaient marqués à tort disponibles via ce type de source",
            "Vérification en direct sur les trackers pour débloquer plus vite un titre ajouté manuellement à la liste, sans attendre le délai habituel",
            "Correction : la liste « Sources » d'un film pouvait afficher jusqu'à 80+ résultats quasi identiques, ou aucun lien téléchargeable — nettoyée (8 sources max, uniquement celles avec un vrai bouton téléchargement, qualité plafonnée à 1080p, tracker le plus fiable en premier)",
            "Correction : certaines sources renvoyaient un contenu sans rapport avec le titre demandé (mauvaise détection interne) — la source concernée est désormais exclue",
        ],
    },
    {
        "version": "0.12.0",
        "date": "2026-08-21",
        "changes": [
            "Nouveau catalogue « Nouveautés Torrent », qui complète les Nouveautés VOD avec les documentaires et séries étrangères absents d'AlloCiné, sur plusieurs trackers",
            "Un titre ajouté depuis ce catalogue est reconnu disponible plus rapidement (déjà vérifié à la source)",
        ],
    },
    {
        "version": "0.11.0",
        "date": "2026-08-15",
        "changes": [
            "Nouveau design « Dashboard » : barre latérale sur ordinateur, tableau de bord en tuiles (héros en carrousel, stats, liste récente), barre d'onglets étendue sur mobile (ajout d'un onglet Mises à jour/Déconnexion)",
            "Bande-annonce, ETA, reconnexion automatique et déconnexion portés dans le nouveau design",
            "Correction : un titre avec une apostrophe (très courant en français) cassait le clic sur les résultats de recherche et les sorties cinéma",
            "Correction : les titres de la section « Sorties Digitales Récentes » s'affichaient vides",
            "Correction : la liste de sources ne gérait pas les candidats Lumio (copiait un lien vide au lieu de proposer la résolution)",
        ],
    },
    {
        "version": "0.10.0",
        "date": "2026-08-15",
        "changes": [
            "Nouveau design : header flottant, thème clair/sombre (bouton dédié, mémorisé), bandeau héros en carrousel avec pastilles, barre d'onglets en bas sur mobile",
            "Reconnexion automatique si la session expire en cours d'usage, au lieu de boutons qui échouent silencieusement",
            "Confirmation avant de retirer un titre de la liste",
            "Bouton Déconnexion",
            "ETA sur les films trop récents pour être disponibles (« dispo estimée dans ~N j »)",
        ],
    },
    {
        "version": "0.9.0",
        "date": "2026-08-15",
        "changes": [
            "Connexion unifiée : un seul formulaire Nom + mot de passe pour tous les comptes, plus besoin d'une URL différente par personne, badge de version affiché dès l'écran de connexion",
            "Onglet « Mises à jour » (celui-ci) avec numéro de version à côté du nom VODIO",
            "Le bouton Télécharger liste toutes les sources trouvées pour un film (comme sur Ludio), triées de la plus légère à la plus lourde, avec téléchargement direct en un clic",
            "Repli automatique sur un lien déjà pré-résolu quand aucune source directe n'est trouvée, message d'erreur plus clair sinon",
            "Garde-fou anti-CAM renforcé sur les films (protège aussi le contenu déjà marqué « en cache », pas seulement la vérification AllDebrid active)",
            "Correctifs internes suite à une revue de code (fuite mémoire des sessions, minuteur de pré-cache mal réinitialisé, messages d'erreur plus fiables)",
        ],
    },
    {
        "version": "0.8.0",
        "date": "2026-08-02",
        "changes": ["Bande-annonce sur la fiche d'un titre"],
    },
    {
        "version": "0.7.0",
        "date": "2026-08-02",
        "changes": ["Catalogue simplifié : fusion de « Bientôt en VOD » dans « Nouveautés VOD »"],
    },
    {
        "version": "0.6.0",
        "date": "2026-07-22",
        "changes": ["Watchlist multi-utilisateurs : chaque personne a sa propre liste et son propre accès"],
    },
    {
        "version": "0.5.0",
        "date": "2026-07-21",
        "changes": ["Téléchargement direct des films disponibles"],
    },
    {
        "version": "0.4.0",
        "date": "2026-07-20",
        "changes": ["Pré-cache à la demande pour les séries (saison entière), suivi de plusieurs saisons en parallèle"],
    },
    {
        "version": "0.3.0",
        "date": "2026-07-16",
        "changes": ["Watchlist personnelle : recherche et ajout de titres depuis une page web dédiée"],
    },
    {
        "version": "0.2.0",
        "date": "2026-07-15",
        "changes": [
            "Support des séries",
            "Badges de disponibilité ✅ / ⏳",
            "Notifications SMS quand un titre devient disponible",
            "Marquer un titre comme vu",
            "Installation en application (PWA)",
        ],
    },
    {
        "version": "0.1.0",
        "date": "2026-07-14",
        "changes": ["Version initiale : catalogue des nouveautés VOD françaises"],
    },
]

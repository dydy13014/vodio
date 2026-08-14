"""Historique des versions de la page web VODIO — indépendant du numéro de
version du manifest Stremio (`MANIFEST["version"]` dans main.py, qui suit son
propre cycle technique). Rester sous la 1.0 tant que l'app est en
développement actif (consigne explicite du 2026-08-14). Nouvelle entrée en
tête de liste à chaque changement notable ; le changelog reprend aussi
l'historique des fonctionnalités livrées avant la création de cet onglet."""

VERSION = "0.14.0"

CHANGELOG = [
    {
        "version": "0.14.0",
        "date": "2026-08-15",
        "changes": [
            "Correction d'une régression : les séries récentes déjà en cache étaient marquées à tort « en cours » — le garde-fou anti-CAM ne s'applique désormais qu'aux films",
        ],
    },
    {
        "version": "0.13.0",
        "date": "2026-08-15",
        "changes": [
            "Connexion unifiée : le badge de version s'affiche maintenant dès l'écran de connexion",
            "Garde-fou anti-CAM renforcé : protège aussi les films/séries déjà marqués « en cache » (Wacustom ou Lumio), pas seulement la vérification AllDebrid active",
            "Correctifs internes suite à une revue de code (fuite mémoire des sessions, minuteur de pré-cache mal réinitialisé, messages d'erreur plus fiables)",
        ],
    },
    {
        "version": "0.12.0",
        "date": "2026-08-15",
        "changes": [
            "Le bouton Télécharger liste maintenant toutes les sources trouvées pour un film (comme sur Ludio), triées de la plus légère à la plus lourde",
            "Un clic sur une source lance directement le téléchargement, plus besoin de copier-coller un lien",
        ],
    },
    {
        "version": "0.11.0",
        "date": "2026-08-14",
        "changes": [
            "Connexion unifiée : un seul formulaire Nom + mot de passe pour tous les comptes, plus besoin d'une URL différente par personne",
        ],
    },
    {
        "version": "0.10.0",
        "date": "2026-08-14",
        "changes": [
            "Onglet « Mises à jour » (celui-ci) avec numéro de version affiché à côté du nom VODIO",
        ],
    },
    {
        "version": "0.9.0",
        "date": "2026-08-14",
        "changes": [
            "Téléchargement plus robuste : repli automatique sur un lien déjà pré-résolu quand aucune source directe n'est trouvée",
            "Message d'erreur plus clair quand aucun téléchargement n'est possible",
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

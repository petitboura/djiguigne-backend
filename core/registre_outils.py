"""
Registre des outils (bras) MCP actifs.

POUR AJOUTER UN NOUVEL OUTIL :
Ajoute une entree dans SERVEURS_MCP ci-dessous. C'est le seul fichier a
modifier. Ni mcp_tools.py (le moteur generique) ni main.py n'ont besoin
d'etre touches.

Deux modes d'authentification sont supportes, car les serveurs MCP ne
s'authentifient pas tous pareil :
- pas de cle du tout (ex: Wolfram)          -> url_builder seul
- cle glissee dans l'URL (ex: Tavily)       -> url_builder seul
- cle envoyee en header HTTP (si besoin un jour) -> url_builder + headers_builder

Chaque *_builder est une fonction qui recoit (get_secret, user_id, agent_id)
et retourne soit une URL (str), soit des headers (dict), soit None. Les
parametres user_id/agent_id sont ignores par la plupart des outils (cle
API globale, comme Tavily/Wolfram) ; ils ne sont utiles que pour un outil
"par utilisateur" (cle "necessite_utilisateur": True), ou chaque utilisateur
connecte son propre compte plutot que d'utiliser une cle partagee par
toute l'app. Pour Notion specifiquement, la connexion est scopee par
user_id seul (compte unifie, juillet 2026) : un utilisateur connecte a
Notion depuis n'importe quel agent l'est automatiquement pour tous les
autres agents de la plateforme -> voir connexions/notion.py.

POUR UN OUTIL "PAR UTILISATEUR" (ex: Notion) :
Ajoute "necessite_utilisateur": True dans son entree. Le dispatcher
(mcp_tools.py) l'ignore alors automatiquement si aucun utilisateur n'est
connecte a l'app, ou si headers_builder renvoie None (utilisateur connecte a
l'app mais pas encore a CET outil POUR CET AGENT) -> pas de bloc if/else
a ecrire ici.
"""

import os

from connexions.notion import obtenir_token_valide

def _url_generation(get_secret, user_id, agent_id):
    # Serveur MCP interne, pas un tiers externe (voir
    # core/serveur_mcp_generation.py, monté dans api/main.py). C'est
    # TOUJOURS le même process/port que celui qui répond à cette
    # requête (localhost, jamais un vrai domaine externe), donc pas
    # besoin de BACKEND_URL ici : on lit directement $PORT, la variable
    # que Railway fournit et qu'uvicorn utilise pour écouter.
    #
    # user_id/agent_id ajoutés en query params (2026-07-22) : nécessaire
    # pour planifier_rappel (notifications push), le premier outil de ce
    # serveur qui a besoin de savoir QUI l'appelle -- récupérés côté
    # serveur via ctx.request_context.request.query_params (voir
    # serveur_mcp_generation.py). Inoffensif pour les autres outils qui
    # n'en ont pas besoin.
    port = os.environ.get("PORT", "8000")
    return f"http://localhost:{port}/mcp/generation?user_id={user_id}&agent_id={agent_id}"


def _url_github(get_secret, user_id, agent_id):
    # Même logique que _url_generation ci-dessus -- serveur MCP interne
    # (core/serveur_mcp_github.py), pas un tiers externe.
    port = os.environ.get("PORT", "8000")
    return f"http://localhost:{port}/mcp/github"


def _url_programme(get_secret, user_id, agent_id):
    # Même logique que _url_generation -- serveur MCP interne (voir
    # core/serveur_mcp_programme.py). user_id transmis en query param,
    # nécessaire (identité de l'étudiant, récupérée côté serveur via
    # ctx.request_context.request.query_params -- jamais un paramètre
    # user_id laissé au modèle, voir docstring du module).
    port = os.environ.get("PORT", "8000")
    return f"http://localhost:{port}/mcp/programme?user_id={user_id}"


def _url_tavily(get_secret, user_id, agent_id):
    return f"https://mcp.tavily.com/mcp/?tavilyApiKey={get_secret('TAVILY_API_KEY')}"


def _url_wolfram(get_secret, user_id, agent_id):
    # CORRECTIF 2026-08-01 bis (Bourama : "j'ai pas mis de clé wolfram
    # hein" -> creuse). L'URL precedente (services.wolfram.com/api/mcp,
    # "Wolfram MCP Service") est une offre PAYANTE necessitant un
    # abonnement + une cle API (voir support.wolfram.com/73463) -- pas la
    # bonne offre pour ce cas d'usage. La veritable offre gratuite est un
    # produit DIFFERENT, "Wolfram Cloud MCP", confirme sans authentification
    # par DEUX pages officielles distinctes (support.wolfram.com/75237 et
    # wolfram.com/artificial-intelligence/mcp/cloud) : "free to use and
    # does not require any authentication". Limite connue et acceptee pour
    # cet usage (question -> reponse, pas de session multi-etapes) : usage
    # personnel limite, une seule requete a la fois, pas d'upload/download
    # de fichier, pas d'interaction locale.
    return "https://agenttools.wolfram.com/mcp"


def _url_notion(get_secret, user_id, agent_id):
    return "https://mcp.notion.com/mcp"


def _headers_notion(get_secret, user_id, agent_id):
    # agent_id fait partie de la signature commune a tous les *_builder
    # (voir docstring en tete de fichier) mais n'est plus utilise ici :
    # la connexion Notion est scopee par user_id seul (compte unifie).
    token = obtenir_token_valide(user_id)
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


SERVEURS_MCP = [
    {"nom": "wolfram", "url_builder": _url_wolfram},
    {
        "nom": "tavily",
        "url_builder": _url_tavily,
        # Réactivé 2026-07-23 (demande de Bourama) -- la plomberie
        # existait déjà (builder d'URL ci-dessus, libellés de statut
        # tavily_search/tavily_extract/... dans core/main.py, option
        # dans le créateur d'agent) mais l'entrée manquait ici, donc
        # injoignable même pour un agent l'ayant coché dans ses droits.
        #
        # ATTENTION connue (voir commentaire sur "notion" plus bas) :
        # Notion (20 outils) + Tavily cumulés dépassaient la limite
        # 8000 TPM du tier Groq gratuit -> 413 Payload Too Large, qui
        # faisait basculer sur le fallback Gemini SANS AUCUN outil.
        # Le nouveau système de droits par agent (filtrage par outil,
        # plus par serveur) limite le risque -- mais évite quand même
        # d'activer Notion ET Tavily ensemble pour un même agent tant
        # que ce n'est pas revérifié en conditions réelles.
    },
    {
        "nom": "generation",
        "url_builder": _url_generation,
        # Pas de "outils_autorises" fixe ici : categorie 1, filtree
        # dynamiquement par agent (agents_outils_generation croise avec
        # registre_outils_plateforme.disponible), voir mcp_tools.py ->
        # _outils_generation_actifs_pour_agent. Ce serveur est TOUJOURS
        # interroge (voir lister_tous_les_outils), contrairement a
        # wolfram/github/notion qui dependent de agents_serveurs.
    },
    {
        "nom": "github",
        "url_builder": _url_github,
        # explorer_depot_github et lire_fichier_depot_github sont sans
        # risque (lecture seule). modifier_fichier_depot_github ÉCRIT
        # réellement sur un dépôt -> dans OUTILS_SENSIBLES plus bas,
        # donc TOUJOURS interrompu pour confirmation avant exécution,
        # quel que soit le mode (direct ou branche+PR).
    },
    {
        "nom": "programme",
        "url_builder": _url_programme,
        "necessite_utilisateur": True,
        # Structure programme (classe/matière/chapitre), 2026-08-12 --
        # voir core/serveur_mcp_programme.py. Inutile sans étudiant
        # connecté (rien à consulter/écrire), donc necessite_utilisateur
        # comme Notion, plutôt que de proposer des outils qui échouent
        # systématiquement à un utilisateur anonyme.
        # ajouter_matiere/ajouter_chapitre/ajouter_exercice/ajouter_document
        # ÉCRIVENT réellement dans le programme de l'étudiant -> dans
        # OUTILS_SENSIBLES plus bas, confirmation avant exécution.
        # consulter_programme reste en lecture seule, hors de cette liste.
    },
    {
        "nom": "notion",
        "url_builder": _url_notion,
        "headers_builder": _headers_notion,
        "necessite_utilisateur": True,
        # Notion active a 100% (01/08, demande Bourama) -- plus de
        # restriction "outils_autorises" : les 20 outils Notion
        # (recherche + creation/edition de pages, bases de donnees,
        # commentaires, equipes...) sont desormais tous disponibles.
        # L'ancienne restriction a notion-search seul datait d'une
        # inquietude sur le budget 8000 TPM du tier Groq gratuit
        # (Notion + Tavily cumules -> 413 Payload Too Large) ; elle est
        # bien moins critique depuis le systeme "bouton Outils"
        # (25-26/07) qui n'envoie de toute facon plus qu'UN OU PLUSIEURS
        # outils selectionnes explicitement au LLM, jamais le catalogue
        # entier. Les outils d'ecriture (notion-create-pages,
        # notion-update-page, notion-move-pages, etc.) restent proteges :
        # ils sont tous dans OUTILS_SENSIBLES plus bas, donc TOUJOURS
        # interrompus pour confirmation utilisateur avant execution.
    },
]

# Outils qui MODIFIENT reellement quelque chose chez l'utilisateur (creation,
# edition, suppression, deplacement...). main.py interrompt le flux et
# demande une confirmation explicite avant d'executer l'un de ces outils,
# quel que soit le serveur MCP dont il provient. Pour l'instant aucun
# outil d'ecriture n'est dans `outils_autorises` ci-dessus (donc cette
# liste n'a pas encore d'effet visible) : elle sert de garde-fou pret a
# l'emploi le jour ou on active par ex. "notion-create-pages".
OUTILS_SENSIBLES = {
    "notion-create-pages",
    "notion-update-page",
    "notion-move-pages",
    "notion-duplicate-page",
    "notion-create-database",
    "notion-update-data-source",
    "notion-create-comment",
    "notion-create-view",
    "notion-update-view",
    "notion-create-attachment",
    # ÉCRIT réellement sur un dépôt GitHub (voir
    # core/serveur_mcp_github.py, modifier_fichier_depot_github) --
    # TOUJOURS interrompu pour confirmation, que ce soit en mode "direct"
    # (commit sur la branche de base) ou "branche_pr" (nouvelle branche +
    # Pull Request). Aucun des deux modes n'est silencieux.
    "modifier_fichier_depot_github",
    # ÉCRIVENT réellement dans le programme de l'étudiant (voir
    # core/serveur_mcp_programme.py) -- consulter_programme (lecture
    # seule) volontairement absent de cette liste.
    "ajouter_matiere",
    "ajouter_chapitre",
    "ajouter_exercice",
    "ajouter_document",
}

# Outils dont le RÉSULTAT n'a pas besoin de retourner au modèle (2026-07-29,
# demande Bourama, suite au bug "Le Chien" -- generer_document, appelé via
# le fallback llama-3.3-70b-versatile, avait recopié l'URL du PDF dans sa
# réponse finale malgré la consigne du prompt système, provoquant un
# doublon d'affichage avec la carte de résultat d'outil).
#
# Principe : le modèle CHOISIT lui-même ce qu'il demande à générer (titre,
# contenu, prompt...) -- il n'a donc rien à apprendre du résultat brut pour
# répondre, seulement à savoir si ça a marché ou non. Pour ces outils,
# core/main.py (_agent_groq) NE rappelle PLUS le modèle après exécution :
# le texte déjà streamé dans le même passage que l'appel d'outil (le cas
# normal depuis le fix du 26/07 sur reponse_directe) sert d'annonce, et le
# résultat s'affiche via les événements outil_resultat/fichiers_generes
# déjà émis par _traiter_appels -- indépendamment de tout texte du modèle.
# En cas d'échec, main.py ajoute lui-même un message d'erreur clair, sans
# repasser par le modèle non plus.
#
# Reste volontairement HORS de cette liste (round-trip conservé, le
# contenu réel du résultat est nécessaire pour que le modèle réponde) :
# - chercher_fichier, consulter_bibliotheque, consulter_statut_3d,
#   consulter_statut_video, consulter_statut_signature (le modèle doit
#   lire le statut/résultat pour savoir quoi dire -- "prêt" vs "encore
#   en cours", etc.)
# - tous les outils de recherche/lecture (tavily_*, wolfram, notion-search,
#   explorer_depot_github, lire_fichier_depot_github)
# - modifier_fichier_depot_github (déjà dans OUTILS_SENSIBLES -- passe par
#   la confirmation utilisateur, pas touché par ce mécanisme pour l'instant)
OUTILS_AUTONOMES = {
    "generer_document",
    "generer_document_word",
    "generer_document_excel",
    "generer_document_powerpoint",
    "generer_code",
    "generer_document_latex",
    "generer_site_zip",
    "generer_bundle",
    "exporter_donnees",
    "generer_image",
    "generer_audio",
    "deployer_site",
    "planifier_rappel",
    "lancer_generation_3d",
    "lancer_generation_video",
    "envoyer_pour_signature",
}

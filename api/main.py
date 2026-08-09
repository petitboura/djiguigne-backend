"""
Backend API du frontend Next.js (Streamlit entièrement retiré depuis le 25/07/2026).

Lancement local : uvicorn api.main:app --reload --port 8000
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional

from anyio import to_thread
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from api.agents import router as agents_router, MATIERES
from api.creators import router as creators_router
from api.profiles import router as profiles_router
from api.search import router as search_router
from api.uploads import router as uploads_router
from api.historique import router as historique_router
from api.notifications import router as notifications_router
from api.notifications_push import router as notifications_push_router
from api.agent_updates import router as agent_updates_router
from api.posts import router as posts_router
from api.chat import router as chat_router
from api.feedback import router as feedback_router
from api.generation import router as generation_router
from api.memoire import router as memoire_router
from api.connexions import router as connexions_router
from api.droits_agent import router as droits_agent_router
from api.droits_agent import router_registre as registre_outils_router
from api.bibliotheque_utilisateur import router as bibliotheque_utilisateur_router
from api.roles import router as roles_router
from api.invitations_classgpt import router as invitations_classgpt_router
from api.contenu_dynamique_matiere import router_enseignant as contenu_matiere_enseignant_router
from api.contenu_dynamique_matiere import router_etudiant as contenu_matiere_etudiant_router
from api.contenu_dynamique_matiere import router_liste_agents as contenu_matiere_liste_agents_router
from api.comportements_etudiants import router as comportements_etudiants_router
from core.serveur_mcp_generation import mcp_generation
from core.notifications_push import traiter_rappels_echus, notifications_push_disponible
from core.proactivite import verifier_relances_proactives
from core.serveur_mcp_github import mcp_github
from core.erreurs import erreur_api

logging.basicConfig(level=logging.INFO)


async def _boucle_planificateur_rappels():
    # Vérifie les rappels arrivés à échéance toutes les 60s (voir
    # core/notifications_push.py:traiter_rappels_echus). Tourne tant que
    # le process vit -- pas de garantie de service externe (cron
    # Railway, etc.), donc si le process redémarre, au pire un rappel
    # est traité quelques secondes plus tard, jamais perdu (la ligne
    # reste "envoye=false" en base tant qu'elle n'a pas été traitée).
    while True:
        try:
            traites = traiter_rappels_echus()
            if traites:
                logging.info(f"Planificateur rappels : {traites} notification(s) envoyée(s).")
        except Exception as e:
            logging.error(f"ERREUR boucle planificateur rappels : {e}")
        await asyncio.sleep(60)


async def _boucle_planificateur_proactivite():
    # Contrairement aux rappels (demande explicite, échéance à la
    # minute), la proactivité se mesure en jours d'inactivité (voir
    # core/proactivite.py) -- pas besoin d'un passage aussi fréquent.
    # Intervalle volontairement plus long que COOLDOWN_VERIFICATION
    # (6h) pour ne jamais re-scanner une paire déjà vérifiée dans le
    # même cycle.
    while True:
        try:
            envoyees = verifier_relances_proactives()
            if envoyees:
                logging.info(f"Planificateur proactivité : {envoyees} relance(s) envoyée(s).")
        except Exception as e:
            logging.error(f"ERREUR boucle planificateur proactivité : {e}")
        await asyncio.sleep(6 * 60 * 60)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Toutes les routes API sont en `def` sync (Supabase, Groq, Gemini :
    # SDKs synchrones) -- FastAPI les exécute correctement dans le
    # threadpool par défaut d'AnyIO, mais celui-ci est limité à 40
    # workers. /api/chat (SSE) retient un worker pendant toute la
    # génération (plusieurs secondes en streaming) : au-delà de ~40
    # conversations simultanées, les nouvelles requêtes attendraient un
    # worker libre. Relevé ici en attendant une éventuelle migration vers
    # des clients async (AsyncGroq, etc.) -- valeur à ajuster selon la
    # RAM disponible sur Railway (chaque thread a un coût mémoire).
    to_thread.current_default_thread_limiter().total_tokens = 100

    # Requis par FastMCP (stateless_http=True) : le session_manager du
    # serveur MCP de génération (voir core/serveur_mcp_generation.py) a
    # besoin de tourner pendant toute la durée de vie du process, sinon
    # streamable_http_app() renvoie une erreur "Task group is not
    # initialized" au premier appel d'outil.
    async with mcp_generation.session_manager.run(), mcp_github.session_manager.run():
        tache_planificateur = None
        tache_proactivite = None
        if notifications_push_disponible():
            tache_planificateur = asyncio.create_task(_boucle_planificateur_rappels())
            tache_proactivite = asyncio.create_task(_boucle_planificateur_proactivite())
        yield
        if tache_planificateur:
            tache_planificateur.cancel()
        if tache_proactivite:
            tache_proactivite.cancel()


app = FastAPI(title="Djiguigne API", version="0.1.0", lifespan=_lifespan)

# Serveur MCP interne (documents/code/images), monté en sous-application
# ASGI : voir core/serveur_mcp_generation.py pour le detail des outils, et
# registre_outils.py pour son enregistrement côté agent (nom "generation").
# CORRECTION (29/07) : mcp 2.0.0 a deplace stateless_http et
# streamable_http_path du constructeur MCPServer(...) vers
# streamable_http_app(...) -- voir les 2 fichiers serveur_mcp_*.py, qui ne
# les passent plus a la construction. streamable_http_path="/" fait que
# le point d'entree final est bien /mcp/generation, sans /mcp en trop.
app.mount("/mcp/generation", mcp_generation.streamable_http_app(stateless_http=True, streamable_http_path="/"))

# Serveur MCP interne (exploration/lecture/écriture GitHub) : voir
# core/serveur_mcp_github.py, monté de la même façon que "generation"
# ci-dessus. registre_outils.py l'enregistre sous le nom "github".
app.mount("/mcp/github", mcp_github.streamable_http_app(stateless_http=True, streamable_http_path="/"))

# Domaines autorisés à appeler cette API. "http://localhost:3000" est le
# port par defaut de `npm run dev` en Next.js, a garder tant que le
# frontend n'est pas deploye. A completer avec le vrai domaine une fois
# app.djiguigne.com cree. Sous-domaine par agent : pas encore fait.
# Domaines fixes autorisés (pas de motif possible pour ceux-la).
ORIGINES_AUTORISEES = [
    "http://localhost:3000",
    "https://app.djiguigne.com",
    "https://djiguign-ai.vercel.app",
]

# En plus des domaines fixes ci-dessus : Vercel donne une URL DIFFERENTE
# a chaque deploiement (en plus de l'alias stable djiguign-ai.vercel.app),
# donc une liste figee doit etre corrigee a la main a chaque fois. Ce
# motif autorise automatiquement toutes les URLs Vercel de CE projet
# (elles commencent toutes par "djiguign", ex. djiguign-ai.vercel.app,
# djiguign-pgwfo47je-petitbouras-projects.vercel.app), sans avoir a
# retoucher ce fichier a chaque nouveau lien.
MOTIF_ORIGINES_VERCEL = r"https://djiguign[a-z0-9\-]*\.vercel\.app"

# Idem pour Class GPT (2026-08-09) -- projet Vercel séparé
# (classgpt-frontend), URL de prévisualisation différente à chaque
# déploiement (ex. classgpt-frontend-bld5bmptn-petitbouras-projects.
# vercel.app). Correctif : l'absence de ce motif faisait échouer TOUTE
# requête depuis ce frontend avec "Failed to fetch" côté navigateur
# (bloqué par CORS avant même que la requête parte), pas une erreur
# d'API -- rien à voir avec une variable d'environnement manquante.
MOTIF_ORIGINES_CLASSGPT = r"https://classgpt-frontend[a-z0-9\-]*\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINES_AUTORISEES,
    allow_origin_regex=f"({MOTIF_ORIGINES_VERCEL}|{MOTIF_ORIGINES_CLASSGPT})",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GZipSaufChat:
    """GZip sur toute l'API SAUF /api/chat.

    Le fil, l'historique, la recherche et les listes d'agents gagnent
    beaucoup à être compressés (JSON qui peut être volumineux). Le SSE
    de /api/chat, lui, streame des chunks minuscules token par token :
    les compresser n'apporte quasi rien et ajouterait une latence de
    flush inutile, à l'encontre du réglage anti-buffering déjà en place
    (X-Accel-Buffering: no, voir api/chat.py). D'où l'exclusion
    explicite plutôt qu'un GZipMiddleware appliqué partout.
    """

    def __init__(self, app):
        self._app_brut = app
        self._app_gzip = GZipMiddleware(app, minimum_size=500)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/chat"):
            await self._app_brut(scope, receive, send)
        else:
            await self._app_gzip(scope, receive, send)


app.add_middleware(GZipSaufChat)

app.include_router(agents_router)
app.include_router(creators_router)
app.include_router(profiles_router)
app.include_router(search_router)
app.include_router(uploads_router)
app.include_router(historique_router)
app.include_router(notifications_router)
app.include_router(agent_updates_router)
app.include_router(posts_router)
app.include_router(chat_router)
app.include_router(feedback_router)
app.include_router(generation_router)
app.include_router(notifications_push_router)
app.include_router(memoire_router)
app.include_router(connexions_router)
app.include_router(droits_agent_router)
app.include_router(registre_outils_router)
app.include_router(bibliotheque_utilisateur_router)
app.include_router(roles_router)
app.include_router(invitations_classgpt_router)
app.include_router(contenu_matiere_enseignant_router)
app.include_router(contenu_matiere_etudiant_router)
app.include_router(contenu_matiere_liste_agents_router)
app.include_router(comportements_etudiants_router)


@app.get("/health")
def health():
    """Verification basique : l'API repond, sans dependance a Supabase."""
    return {"status": "ok"}


@app.get("/health/me")
def health_me(utilisateur=Depends(utilisateur_courant)):
    """
    Verification de bout en bout de l'auth : necessite un vrai token
    Supabase valide en en-tete Authorization. Sert a valider, avant de
    construire quoi que ce soit d'autre, que le frontend arrive bien a
    s'authentifier aupres de cette API. A garder meme apres l'Etape 0
    (utile pour deboguer un token en prod).
    """
    return {"id": utilisateur.id, "email": utilisateur.email}


class AgentFeedItem(BaseModel):
    id: str
    nom: str
    icone_page: str = "🤖"
    image_vitrine_url: Optional[str] = None
    # Nouveau système d'icône (2026-08-05) : voir agents.py CreerAgentPayload.icone_url
    icone_url: Optional[str] = None
    description: str = ""
    categorie_id: Optional[str] = None
    # Ajouté le 2026-07-31 (Bourama : section "Matières" de la page
    # Produit du vitrine) -- voir MATIERES dans api/agents.py. Repli sur
    # None : les agents créés avant le système matière n'ont pas cette
    # colonne renseignée.
    matiere: Optional[str] = None
    # Ajouté le 2026-07-31 (5ème bouton "Langues africaines" de la page
    # Produit du vitrine) -- voir langue_africaine dans api/agents.py.
    langue_africaine: Optional[str] = None
    # Ajouté le 2026-07-31 (compléter les boutons Métier/Filière/Domaine
    # de la page Produit du vitrine, mêmes principes qu'au-dessus).
    metier: Optional[str] = None
    filiere: Optional[str] = None
    domaine: Optional[str] = None
    # 6ème bouton "Exécution" de la page Produit du vitrine (2026-07-31),
    # mêmes principes que metier/filiere/domaine ci-dessus.
    execution: Optional[str] = None


class FeedReponse(BaseModel):
    agents: List[AgentFeedItem]
    page: int
    limite: int
    total: int


@app.get("/api/feed", response_model=FeedReponse)
def feed(
    page: int = Query(1, ge=1),
    limite: int = Query(20, ge=1, le=50),
    categorie: Optional[str] = Query(None),
    avec_matiere: Optional[bool] = Query(None),
    avec_langue_africaine: Optional[bool] = Query(None),
    avec_metier: Optional[bool] = Query(None),
    avec_filiere: Optional[bool] = Query(None),
    avec_domaine: Optional[bool] = Query(None),
    avec_execution: Optional[bool] = Query(None),
):
    """
    Liste paginée des agents publiés, pour le feed de découverte de la
    page `/`. Public, aucune auth requise.

    Un agent est considéré publié si `actif` est True OU absent/NULL
    (même convention de "True par défaut" que dans l'ancienne interface
    Streamlit, pour ne pas faire disparaître du
    feed des agents créés avant l'ajout de cette colonne).

    `categorie` (ajouté 2026-07-15, système de catégories) : filtre par
    `categorie_id` si fourni, sinon comportement inchangé (tout le feed).

    `avec_matiere` / `avec_langue_africaine` / `avec_metier` /
    `avec_filiere` / `avec_domaine` / `avec_execution` (ajoutés 2026-07-31,
    les 6 boutons de la page Produit du vitrine) : si True, ne renvoie que
    les agents ayant la colonne correspondante renseignée (voir
    api/agents.py -- `matiere` a une liste fixe, les 5 autres sont en
    texte libre). Tous indépendants les uns des autres et de `categorie` ;
    combinables si besoin même si la page Produit n'active qu'un bouton à
    la fois aujourd'hui.
    """
    debut = (page - 1) * limite
    fin = debut + limite - 1

    try:
        requete = (
            supabase.table("agents")
            .select(
                "id, nom, ui_config, image_vitrine_url, icone_url, description, categorie_id, "
                "matiere, langue_africaine, metier, filiere, domaine, execution",
                count="exact",
            )
            .or_("actif.is.null,actif.eq.true")
            .or_("publiable.is.null,publiable.eq.true")
        )
        if categorie:
            requete = requete.eq("categorie_id", categorie)
        if avec_matiere:
            requete = requete.not_.is_("matiere", "null")
        if avec_langue_africaine:
            requete = requete.not_.is_("langue_africaine", "null")
        if avec_metier:
            requete = requete.not_.is_("metier", "null")
        if avec_filiere:
            requete = requete.not_.is_("filiere", "null")
        if avec_domaine:
            requete = requete.not_.is_("domaine", "null")
        if avec_execution:
            requete = requete.not_.is_("execution", "null")
        res = requete.order("id").range(debut, fin).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture feed, page={page}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LE_FEED_POUR")

    agents = [
        AgentFeedItem(
            id=ligne["id"],
            nom=ligne["nom"],
            icone_page=(ligne.get("ui_config") or {}).get("icone_page", "🤖"),
            image_vitrine_url=ligne.get("image_vitrine_url"),
            icone_url=ligne.get("icone_url"),
            description=ligne.get("description") or "",
            categorie_id=ligne.get("categorie_id"),
            matiere=ligne.get("matiere"),
            langue_africaine=ligne.get("langue_africaine"),
            metier=ligne.get("metier"),
            filiere=ligne.get("filiere"),
            domaine=ligne.get("domaine"),
            execution=ligne.get("execution"),
        )
        for ligne in (res.data or [])
    ]

    return FeedReponse(agents=agents, page=page, limite=limite, total=res.count or 0)


class CategorieItem(BaseModel):
    id: str
    nom: str
    mots_cles: List[str] = []
    parent_id: Optional[str] = None


@app.get("/api/categories", response_model=List[CategorieItem])
def lister_categories(seulement_utilisees: bool = Query(False)):
    """
    Toutes les catégories, pour le popup de sélection sur la page
    d'accueil et les formulaires de création/modification d'agent.
    Public, aucune auth requise (même statut que /api/feed). `parent_id`
    prépare l'arrivée des sous-catégories (Bourama, 2026-07-15) : NULL
    pour toutes pour l'instant, aucune catégorie n'est encore un enfant
    d'une autre.

    `seulement_utilisees` (ajouté 2026-07-15, demande de Bourama : les
    catégories vides ne doivent pas apparaître à l'accueil) : si True, ne
    renvoie que les catégories ayant au moins un agent publié. UNIQUEMENT
    pour le popup de l'accueil -- les formulaires de création/modification
    continuent d'appeler cette route SANS ce paramètre, pour permettre de
    choisir une catégorie même si on est le premier agent dedans.
    """
    try:
        res = supabase.table("categories").select("id, nom, mots_cles, parent_id").execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture categories) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LES_CATEGORIES_POUR")

    categories = res.data or []

    if seulement_utilisees:
        try:
            res_agents = (
                supabase.table("agents")
                .select("categorie_id")
                .or_("actif.is.null,actif.eq.true")
                .not_.is_("categorie_id", "null")
                .execute()
            )
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (lecture categorie_id des agents) : {e}")
            raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LES_CATEGORIES_POUR")
        ids_utilisees = {l["categorie_id"] for l in (res_agents.data or [])}
        categories = [c for c in categories if c["id"] in ids_utilisees]

    return [CategorieItem(**ligne) for ligne in categories]


class MatiereItem(BaseModel):
    nom: str
    disponible: bool


@app.get("/api/matieres", response_model=List[MatiereItem])
def lister_matieres():
    """
    Système "matière" (2026-07-29), indépendant du système "catégorie"
    ci-dessus (aucun lien, aucune migration entre les deux). Une seule IA
    par matière (Bourama : "dès qu'une matière est prise, elle disparaît
    de la liste") -- `disponible=False` pour toute matière déjà prise par
    un agent. "Autre" est traitée comme une matière normale : elle aussi
    ne peut être prise que par une seule IA à la fois (voir la contrainte
    UNIQUE agents_matiere_unique et matiere_detail pour le texte libre
    associé). Public, aucune auth requise (même statut que /api/categories).
    """
    try:
        res = supabase.table("agents").select("matiere").not_.is_("matiere", "null").execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture matières prises) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LES_MATIERES_POUR")

    prises = {ligne["matiere"] for ligne in (res.data or [])}
    toutes = list(MATIERES) + ["Autre"]
    return [MatiereItem(nom=m, disponible=m not in prises) for m in toutes]

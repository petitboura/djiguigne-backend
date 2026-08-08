"""
POST /api/agents.

Équivalent de l'ancien formulaire Streamlit, SANS l'upload de PDF
(volontairement séparé : POST /api/agents/{id}/documents —
un fichier ne se transporte pas naturellement dans le même corps JSON
qu'un formulaire structuré, et creer_agent.py traite déjà ces deux
aspects comme deux blocs largement indépendants).

Réutilise telle quelle la logique déjà partagée avec l'ancien formulaire
Streamlit (core/creation_agent.py), pas de duplication.
"""

import os
import sys
import logging
import tempfile
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from api.auth import utilisateur_courant, utilisateur_optionnel, supabase, get_secret
from api.journal import journaliser
from api.permissions_hierarchie import peut_modifier_comportement, peut_gerer_base_connaissances

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "indexers"))
from creation_agent import generer_id_depuis_nom, extraire_id_notion, composer_system_prompt  # noqa: E402
from index_notion import parcourir_et_indexer  # noqa: E402
from index_documents import indexer_texte, indexer_document, supprimer_chunks_existants  # noqa: E402
from storage import upload_document, list_documents, delete_document, get_document_url  # noqa: E402
from bibliotheque_fichiers import enregistrer_fichier, enregistrer_lien, lister_fichiers, supprimer_fichier  # noqa: E402
from mcp_tools import lister_outils_autorises_pour_agent  # noqa: E402
from core.erreurs import erreur_api
from core.fournisseurs_llm import modeles_disponibles_pour_agent, modele_id_est_autorise

logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Liste fixe des matières (Bourama, 2026-07-29) -- miroir exact de MATIERES
# dans djiguigne-frontend/lib/matieres.ts, à garder synchronisée à la main
# si la liste change. Système INDÉPENDANT de categorie_id/table categories
# (aucun lien, aucune migration entre les deux -- deux classifications
# distinctes qui cohabitent). "Autre" n'en fait pas partie ici : c'est une
# valeur à part entière de la colonne `matiere` (texte libre associé dans
# `matiere_detail`), voir _valider_et_verifier_disponibilite_matiere.
MATIERES = [
    "Informatique",
    "Mathématiques",
    "Physique",
    "Économie",
    "Chimie",
    "Anglais",
    "SVT (Biologie)",
    "Français",
    "Gestion",
    "Arabe",
]


def _message_clair_erreur_notion(erreur_brute: str) -> str:
    """
    Traduit une erreur Notion technique (voir ErreurNotion dans
    indexers/index_notion.py) en message compréhensible pour le créateur
    d'agent, sans jargon ni code d'erreur brut. Ajouté 2026-08-03.
    """
    if "HTTP 404" in erreur_brute:
        return "La page Notion est introuvable. Vérifie que le lien est correct."
    if "HTTP 401" in erreur_brute or "HTTP 403" in erreur_brute:
        return (
            "Djiguignè n'a pas accès à cette page Notion. Vérifie qu'elle est bien "
            "partagée avec l'intégration Djiguignè dans Notion."
        )
    if "réseau injoignable" in erreur_brute:
        return "Impossible de contacter Notion pour le moment. Réessaie dans quelques minutes."
    return (
        "Le contenu de cette page Notion n'a pas pu être ajouté. Vérifie le lien et "
        "que la page est bien partagée, ou réessaie plus tard."
    )


def _indexer_notion_arriere_plan(agent_id: str, page_id: str) -> None:
    """
    Tâche de fond (voir BackgroundTasks dans creer_agent/modifier_agent,
    ajouté 2026-08-03) : jusqu'ici, coller/modifier un lien Notion
    n'indexait rien avant le prochain passage du cron GitHub Actions
    (indexer.yml, 3h du matin) -- rien dans l'UI ne le signalait. Cette
    fonction lance l'indexation tout de suite après la sauvegarde, sans
    bloquer la réponse au créateur, puis écrit un statut + message clair
    dans `agents` pour que le dashboard puisse l'afficher.
    """
    erreurs: List[str] = []
    try:
        parcourir_et_indexer(page_id, agent_id, erreurs=erreurs)
    except Exception as e:
        # Filet de sécurité : parcourir_et_indexer capture déjà ErreurNotion
        # en interne, mais une erreur totalement inattendue ici (bug,
        # quota embeddings épuisé, etc.) ne doit jamais rester invisible
        # dans les seuls logs Railway -- le créateur doit voir que ça a
        # échoué, même sans savoir pourquoi techniquement.
        logging.error(f"ERREUR inattendue indexation Notion (agent_id={agent_id}) : {e}")
        erreurs.append(str(e))

    if erreurs:
        statut = "erreur"
        message = _message_clair_erreur_notion(erreurs[0])
    else:
        statut = "ok"
        message = None

    try:
        supabase.table("agents").update(
            {
                "notion_index_statut": statut,
                "notion_index_message": message,
                "notion_index_maj_le": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", agent_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (écriture statut indexation Notion agent_id={agent_id}) : {e}")


def _valider_et_verifier_disponibilite_matiere(
    matiere: Optional[str],
    matiere_detail: Optional[str],
    agent_id_a_exclure: Optional[str] = None,
) -> None:
    """
    Une seule IA par matière (Bourama, 2026-07-29), "Autre" inclus (une
    seule IA "Autre" au total, peu importe matiere_detail). La contrainte
    UNIQUE en base (agents_matiere_unique) est le garde-fou final contre
    les cas concurrents ; cette vérification ici sert à renvoyer un
    message clair plutôt qu'une erreur SQL brute à l'appelant.
    `agent_id_a_exclure` : lors d'une modification, un agent ne doit pas
    se bloquer lui-même s'il garde la même matière.
    """
    if matiere is None:
        return
    if matiere != "Autre" and matiere not in MATIERES:
        raise erreur_api(422, "MATIERE_INCONNUE")
    if matiere == "Autre" and not (matiere_detail or "").strip():
        raise erreur_api(422, "PRECISE_MATIERE_AUTRE")
    try:
        requete = supabase.table("agents").select("id").eq("matiere", matiere)
        if agent_id_a_exclure:
            requete = requete.neq("id", agent_id_a_exclure)
        deja_prise = requete.execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification disponibilité matière={matiere}) : {e}")
        deja_prise = None
    if deja_prise and deja_prise.data:
        raise erreur_api(422, "CETTE_MATIERE_EST_DEJA_PRISE_PAR")


def _valider_et_verifier_disponibilite_langue_africaine(
    langue_africaine: Optional[str],
    agent_id_a_exclure: Optional[str] = None,
) -> None:
    """
    5ème bouton de la page Produit du vitrine, "Langues africaines"
    (Bourama, 2026-07-31). Contrairement à `matiere`, texte libre : pas
    de liste fixe, le créateur tape la langue lui-même (ex: "Bambara",
    "Wolof"...). Même règle "une seule IA par [valeur]" que le système
    matière -- même structure de vérification (message clair avant
    l'erreur SQL brute), contrainte UNIQUE en base
    (agents_langue_africaine_unique) comme garde-fou final.
    """
    _valider_et_verifier_disponibilite_categorie_libre(
        "langue_africaine", "Cette langue africaine", langue_africaine, agent_id_a_exclure
    )


# Ajouté le 2026-07-31 (Bourama : 4 champs texte libre à règle identique
# -- langue_africaine ci-dessus, puis metier/filiere/domaine juste après
# -- pour compléter les boutons de la page Produit du vitrine). Un seul
# helper générique plutôt que 4 copier-coller, chaque colonne ayant sa
# contrainte UNIQUE en base (agents_<colonne>_unique) comme garde-fou
# final.
def _valider_et_verifier_disponibilite_categorie_libre(
    colonne: str,
    libelle: str,
    valeur: Optional[str],
    agent_id_a_exclure: Optional[str] = None,
) -> None:
    if valeur is None:
        return
    valeur = valeur.strip()
    if not valeur:
        raise erreur_api(422, "PRECISE_LA_VALEUR_POUR", libelle=libelle.lower())
    try:
        requete = supabase.table("agents").select("id").eq(colonne, valeur)
        if agent_id_a_exclure:
            requete = requete.neq("id", agent_id_a_exclure)
        deja_prise = requete.execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification disponibilité {colonne}={valeur}) : {e}")
        deja_prise = None
    if deja_prise and deja_prise.data:
        raise erreur_api(422, "EST_DEJA_PRISE_PAR_UNE", libelle=libelle)


def _valider_et_verifier_disponibilite_metier(metier: Optional[str], agent_id_a_exclure: Optional[str] = None) -> None:
    _valider_et_verifier_disponibilite_categorie_libre("metier", "Ce métier", metier, agent_id_a_exclure)


def _valider_et_verifier_disponibilite_filiere(filiere: Optional[str], agent_id_a_exclure: Optional[str] = None) -> None:
    _valider_et_verifier_disponibilite_categorie_libre("filiere", "Cette filière", filiere, agent_id_a_exclure)


def _valider_et_verifier_disponibilite_domaine(domaine: Optional[str], agent_id_a_exclure: Optional[str] = None) -> None:
    _valider_et_verifier_disponibilite_categorie_libre("domaine", "Ce domaine", domaine, agent_id_a_exclure)


# Ajouté le 2026-07-31 (6ème bouton "Exécution" de la page Produit du
# vitrine, Bourama) -- même principe que metier/filiere/domaine
# ci-dessus : texte libre, une IA par valeur, contrainte UNIQUE en base
# (agents_execution_unique) comme garde-fou final.
def _valider_et_verifier_disponibilite_execution(execution: Optional[str], agent_id_a_exclure: Optional[str] = None) -> None:
    _valider_et_verifier_disponibilite_categorie_libre("execution", "Cette exécution", execution, agent_id_a_exclure)


def _agent_owner_id_ou_404(agent_id: str) -> dict:
    """Petit helper partagé par les endpoints /administrateurs (2026-08-05) :
    juste owner_id, pas besoin des colonnes complètes comme /edition."""
    try:
        res = supabase.table("agents").select("owner_id").eq("id", agent_id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture owner_id agent {agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_L_AGENT_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    return res.data


class LigneComportement(BaseModel):
    type_requete: str = ""
    comportement: str = ""


class ChampProfilUtilisateur(BaseModel):
    """
    Un champ du profil dynamique que le créateur veut suivre chez les
    personnes qui parlent à SON agent (demande Bourama, 2026-07-21) --
    pas un schéma imposé pour toute la plateforme : chaque agent a le
    sien, vide par défaut (fonctionnalité désactivée tant qu'aucun champ
    n'est défini). `description` guide le modèle lors de l'extraction
    automatique (voir _mettre_a_jour_profil_utilisateur_si_besoin dans
    core/main.py) : plus elle est précise, meilleure est l'extraction.
    """
    nom: str
    description: str = ""


class UiConfig(BaseModel):
    """
    Depuis le pivot social (2026-07-11) : le thème visuel par agent est supprimé, un seul
    thème fixe s'applique à toute la plateforme. Seul icone_page reste
    personnalisable ici — tous les anciens champs (couleurs, police,
    rayon des bulles, CSS avancé, style de titre multicolore...) sont
    retirés, pas juste ignorés, pour ne pas garder de code mort côté API.
    Cible finale de `agents.ui_config` en base (le nettoyage de la
    colonne elle-même, avec les agents déjà créés, reste une étape à
    part).
    """
    icone_page: str = "🤖"
    # Ajouté le 2026-07-14 (Bourama : le formulaire n'avait aucune section
    # pour ce champ, alors que l'ancienne interface Streamlit le lit déjà depuis
    # ui_config.placeholder_saisie -- voir UI_CONFIG_PAR_DEFAUT). Le
    # formulaire Streamlit (creer_agent.py) l'avait déjà, seul le flow
    # Next.js en manquait.
    placeholder_saisie: str = "Pose ta question..."


class CreerAgentPayload(BaseModel):
    nom: str
    ton: str  # "Tutoiement (tu)" | "Vouvoiement (vous)"
    posture_generale: str = ""
    limites_globales: str = ""
    comportements: List[LigneComportement] = Field(default_factory=list)
    outils_choisis: List[str] = Field(default_factory=list)
    # Optionnel depuis le 2026-07-12 (Bourama : champ "Nature de la
    # connaissance" retiré du formulaire Next.js -- voir docstring de
    # composer_system_prompt). Le formulaire Streamlit continue d'envoyer
    # une valeur, donc reste géré normalement quand fourni.
    type_connaissance: str = ""
    description_connaissance: str = ""
    lien_notion: Optional[str] = None
    texte_libre: str = ""
    ui_config: UiConfig = Field(default_factory=UiConfig)
    # Ajouté le 2026-07-15 (Bourama : système de catégories). Devenu
    # optionnel le 2026-07-29 : le picker manuel a été retiré des deux
    # formulaires au profit du système "matière" ci-dessous (indépendant,
    # aucun lien entre les deux) -- categorie_id n'est plus envoyé par le
    # frontend, mais reste accepté/validé s'il est fourni.
    categorie_id: Optional[str] = None
    # Système "matière" (2026-07-29), voir MATIERES et
    # _valider_et_verifier_disponibilite_matiere plus haut. `matiere` est
    # une des valeurs fixes ou "Autre" ; `matiere_detail` est le texte
    # libre associé, utilisé seulement quand matiere = "Autre".
    matiere: Optional[str] = None
    matiere_detail: Optional[str] = None
    # Système "langues africaines" (2026-07-31), indépendant du système
    # matière ci-dessus -- voir _valider_et_verifier_disponibilite_langue_africaine.
    # Texte libre, pas de liste fixe.
    langue_africaine: Optional[str] = None
    # Métier / Filière / Domaine (2026-07-31, mêmes 4ème/3ème/2ème boutons
    # de la page Produit) -- même principe : texte libre, une IA par
    # valeur. Voir _valider_et_verifier_disponibilite_categorie_libre.
    metier: Optional[str] = None
    filiere: Optional[str] = None
    domaine: Optional[str] = None
    # Exécution (2026-07-31, 6ème bouton de la page Produit) -- même
    # principe que les 3 champs ci-dessus.
    execution: Optional[str] = None
    # Nouveau flow de création (pivot social) : image de vitrine et
    # description publique de la page agent, distinctes de
    # description_connaissance qui reste un usage interne au RAG.
    image_vitrine_url: Optional[str] = None
    # Nouveau système d'icône (2026-08-05) : icône compacte (dessinée à la
    # main ou uploadée), affichée partout à la place de l'emoji
    # ui_config.icone_page et de image_vitrine_url ci-dessus -- voir
    # migrations/2026_08_05_ajout_icone_url_agents.sql. image_vitrine_url
    # reste en base pour l'instant (agents déjà créés), mais n'est plus
    # affiché nulle part une fois icone_url renseigné.
    icone_url: Optional[str] = None
    description: str = ""
    # Ajouté le 2026-07-12 (Bourama : "tu as mélangé deux choses, la
    # description publique et le sous-titre. La description publique peut
    # avoir n'importe quelle taille alors que le sous-titre n'est qu'un
    # sous-titre"). AVANT ce correctif, sous_titre_accueil était rempli
    # directement avec `description` (voir plus bas) faute de champ dédié
    # -- ça marchait "par accident" pour corriger le bug du sous-titre
    # identique à tous les agents, mais confondait deux choses de nature
    # différente : `description` = texte public de longueur libre (fiche
    # agent, SEO), `sous_titre` = courte phrase d'accueil affichée sous le
    # titre au premier écran du chat (équivalent du champ "Phrase
    # d'accueil" du formulaire Streamlit).
    # Fallback sur `description` uniquement si `sous_titre` est vide, pour
    # ne pas laisser un agent sans aucun sous-titre si le créateur ne
    # remplit pas ce nouveau champ.
    sous_titre: str = ""
    # Profil utilisateur dynamique par agent (2026-07-21) : vide par défaut,
    # voir ChampProfilUtilisateur.
    profil_utilisateur_schema: List[ChampProfilUtilisateur] = Field(default_factory=list)
    # Ajouté le 2026-07-23 (Bourama : "il doit être dans le formulaire de
    # création, comme tous les autres [champs]") : droits de l'agent
    # choisis DANS le même formulaire, envoyés avec le reste, pas
    # configurés après coup. Categorie 1 (generation, par outil) et
    # categories 2/3 (serveur entier) -- voir migration_droits_agents.sql.
    outils_generation_choisis: List[str] = Field(default_factory=list)
    serveurs_choisis: List[str] = Field(default_factory=list)
    # Ajouté le 2026-07-29 : categorie 4 (actions locales UI, ex.
    # localisation/clavier LaTeX/dessin -- pas envoyees au LLM, mais le
    # createur doit quand meme pouvoir les activer/desactiver par agent).
    actions_locales_choisies: List[str] = Field(default_factory=list)


class AgentCree(BaseModel):
    id: str
    nom: str
    lien: Optional[str] = None


@router.post("", response_model=AgentCree, status_code=201)
def creer_agent(
    payload: CreerAgentPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    utilisateur=Depends(utilisateur_courant),
):
    # Ajouté le 2026-08-05 (Bourama : "n'importe qui peut plus être
    # créateur") : gate réelle côté API, pas juste cacher le bouton côté
    # frontend -- sinon un simple POST direct contourne tout. Voir
    # profiles.est_createur (api/profiles.py), backfillé à True pour tout
    # compte possédant déjà un agent au moment de l'ajout de la colonne.
    try:
        profil = (
            supabase.table("profiles")
            .select("est_createur")
            .eq("user_id", utilisateur.id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification est_createur {utilisateur.id}) : {e}")
        profil = None
    if not profil or not profil.data or not profil.data.get("est_createur"):
        raise erreur_api(403, "COMPTE_NON_CREATEUR")

    if not payload.nom.strip():
        raise erreur_api(422, "LE_NOM_DE_L_AGENT_EST")
    if not payload.posture_generale.strip() and not payload.limites_globales.strip():
        raise erreur_api(422, "AGENT_CREATION_CHAMPS_MANQUANTS")
    # categorie_id n'est plus obligatoire (voir CreerAgentPayload) : validé
    # contre la table `categories` uniquement s'il est fourni.
    if payload.categorie_id is not None and payload.categorie_id.strip():
        try:
            categorie_existe = (
                supabase.table("categories")
                .select("id")
                .eq("id", payload.categorie_id)
                .maybe_single()
                .execute()
            )
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (vérification catégorie={payload.categorie_id}) : {e}")
            categorie_existe = None
        if not categorie_existe or not categorie_existe.data:
            raise erreur_api(422, "CATEGORIE_INCONNUE")

    _valider_et_verifier_disponibilite_matiere(payload.matiere, payload.matiere_detail)
    _valider_et_verifier_disponibilite_langue_africaine(payload.langue_africaine)
    _valider_et_verifier_disponibilite_metier(payload.metier)
    _valider_et_verifier_disponibilite_filiere(payload.filiere)
    _valider_et_verifier_disponibilite_domaine(payload.domaine)
    _valider_et_verifier_disponibilite_execution(payload.execution)

    agent_id = generer_id_depuis_nom(payload.nom)

    try:
        existe_deja = (
            supabase.table("agents").select("id").eq("id", agent_id).maybe_single().execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification unicité agent_id={agent_id}) : {e}")
        existe_deja = None

    if existe_deja and existe_deja.data:
        raise erreur_api(409, "AGENT_NOM_DEJA_PROCHE", agent_id=agent_id)

    lignes_comportement = [(l.type_requete, l.comportement) for l in payload.comportements]
    system_prompt = composer_system_prompt(
        payload.ton, payload.posture_generale, payload.limites_globales,
        lignes_comportement, payload.type_connaissance, payload.description_connaissance,
        nom=payload.nom, description_publique=payload.description,
    )
    notion_page_id = extraire_id_notion(payload.lien_notion)

    # Depuis le pivot social : plus de personnalisation de thème par agent,
    # seuls titre/icône/emoji dérivés du nom et de l'icône restent écrits
    # dans ui_config. l'ancienne interface Streamlit retombe sur UI_CONFIG_PAR_DEFAUT
    # pour tout le reste (couleurs, police, rendu_visuel, etc.), ce qui
    # est le comportement voulu : un seul thème fixe pour la plateforme.
    ui = payload.ui_config
    ui_config_dict = {
        "titre_page": payload.nom.strip(),
        "icone_page": ui.icone_page.strip() or "🤖",
        # Nouveau système d'icône (2026-08-05) : titre_accueil ne préfixe
        # plus l'emoji -- l'icône (icone_url, ou générique par défaut)
        # s'affiche séparément à côté du titre, jamais concaténée dedans
        # (sinon doublon visuel une fois l'icône ajoutée en plus du texte).
        "titre_accueil": payload.nom.strip(),
        # Bug corrigé le 2026-07-12 (Bourama : "le sous-titre est
        # identique à tous, vraiment tous") : ce champ n'était jamais
        # écrit ici, donc l'ancienne interface Streamlit retombait systématiquement
        # sur UI_CONFIG_PAR_DEFAUT["sous_titre_accueil"] (le texte de
        # l'agent maths historique) pour TOUS les agents créés via ce
        # flow, quel que soit leur sujet réel. Le formulaire Streamlit
        # (creer_agent.py) a un champ dédié pour ça ("Phrase d'accueil") ;
        # ce flow-ci en a maintenant un aussi (`sous_titre`), distinct de
        # `description` (correctif du 2026-07-12 suivant : les deux
        # avaient été confondus dans une première version de ce
        # correctif). Fallback sur description seulement si sous_titre
        # est vide, pour ne jamais laisser un agent sans sous-titre.
        "sous_titre_accueil": payload.sous_titre.strip() or payload.description.strip(),
        "emoji_reponse": ui.icone_page.strip(),
        # Point 5 (2026-07-14, Bourama) : texte de la barre de saisie du
        # chat, personnalisable par agent -- déjà lu tel quel côté
        # l'ancienne interface Streamlit (UI_CONFIG["placeholder_saisie"]), seul le
        # flow de création Next.js ne l'écrivait pas encore.
        "placeholder_saisie": ui.placeholder_saisie.strip() or "Pose ta question...",
    }

    knowledge_source = {
        "type": payload.type_connaissance,
        "description": payload.description_connaissance.strip(),
        # Conservé tel quel (pas seulement indexé), même choix que
        # creer_agent.py, pour pouvoir être réaffiché/modifié plus tard.
        "texte_libre": payload.texte_libre.strip(),
    }

    nouvelle_ligne = {
        "id": agent_id,
        "nom": payload.nom.strip(),
        "system_prompt": system_prompt,
        "ui_config": ui_config_dict,
        "knowledge_source": knowledge_source,
        # CORRECTION (2026-07-29) : payload.outils_choisis est l'ancien
        # champ, plus jamais rempli par le formulaire Next.js (toujours
        # []), donc cette colonne se figeait a vide des la creation. Le
        # moteur MCP ne la lit plus (voir mcp_tools.py), mais on la
        # resynchronise quand meme, a titre d'affichage/diagnostic, avec
        # les vraies categories choisies -- meme calcul que
        # modifier_droits_agent, pour ne jamais raconter autre chose que
        # les vraies tables agents_outils_generation / agents_serveurs /
        # agents_actions_locales.
        "tools_enabled": sorted(
            ({"generation"} if payload.outils_generation_choisis else set())
            | set(payload.serveurs_choisis)
            | ({"ui"} if payload.actions_locales_choisies else set())
        ),
        "owner_id": utilisateur.id,
        # Colonnes ajoutées par la migration pivot_social_etape_b_tables :
        # vitrine publique de l'agent,
        # distincte de knowledge_source.description (usage RAG interne).
        "image_vitrine_url": payload.image_vitrine_url,
        "icone_url": payload.icone_url,
        "description": payload.description.strip(),
        "categorie_id": payload.categorie_id,
        "matiere": payload.matiere,
        "matiere_detail": (payload.matiere_detail or "").strip() or None if payload.matiere == "Autre" else None,
        "langue_africaine": (payload.langue_africaine or "").strip() or None,
        "metier": (payload.metier or "").strip() or None,
        "filiere": (payload.filiere or "").strip() or None,
        "domaine": (payload.domaine or "").strip() or None,
        "execution": (payload.execution or "").strip() or None,
        "profil_utilisateur_schema": [c.model_dump() for c in payload.profil_utilisateur_schema],
        # Colonne ajoutée le 2026-07-12 (Bourama : le formulaire de
        # modification doit contenir tous les champs de la création).
        # composer_system_prompt() fusionne ces champs puis les jette —
        # sans cette colonne, impossible de les réafficher pour édition,
        # seul le texte composé final survivrait. Un agent créé AVANT
        # cette migration aura `config_creation IS NULL` (voir
        # obtenir_agent_pour_edition, qui gère ce cas en repli sur le
        # system_prompt brut). Inclut `sous_titre` (voir CreerAgentPayload
        # plus haut, distinct de `description`).
        "config_creation": {
            "ton": payload.ton,
            "posture_generale": payload.posture_generale,
            "limites_globales": payload.limites_globales,
            "comportements": [
                {"type_requete": l.type_requete, "comportement": l.comportement}
                for l in payload.comportements
            ],
            "type_connaissance": payload.type_connaissance,
            "description_connaissance": payload.description_connaissance,
            "sous_titre": payload.sous_titre,
        },
    }
    if notion_page_id:
        nouvelle_ligne["notion_page_id"] = notion_page_id
        nouvelle_ligne["notion_index_statut"] = "en_cours"

    try:
        supabase.table("agents").insert(nouvelle_ligne).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (insertion agent {agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CREER_L_AGENT_ERREUR")

    # Droits de l'agent (categorie 1 par outil, categories 2/3 par
    # serveur), choisis dans le meme formulaire de creation. Best-effort :
    # un souci ici ne doit pas annuler la creation de l'agent (deja
    # inseree juste au-dessus), mais on logge fort pour ne pas rater un
    # agent cree sans AUCUN droit par accident technique.
    try:
        if payload.outils_generation_choisis:
            supabase.table("agents_outils_generation").insert(
                [{"agent_id": agent_id, "nom_outil": n} for n in payload.outils_generation_choisis]
            ).execute()
        if payload.serveurs_choisis:
            supabase.table("agents_serveurs").insert(
                [{"agent_id": agent_id, "nom_serveur": n} for n in payload.serveurs_choisis]
            ).execute()
        if payload.actions_locales_choisies:
            supabase.table("agents_actions_locales").insert(
                [{"agent_id": agent_id, "nom_action": n} for n in payload.actions_locales_choisies]
            ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (insertion droits initiaux agent={agent_id}) : {e}")

    # Indexation du texte libre : best-effort, n'annule jamais la création
    # de l'agent (même choix que creer_agent.py) si elle échoue.
    if payload.texte_libre.strip():
        try:
            indexer_texte(agent_id, "texte-libre", payload.texte_libre.strip())
        except Exception as e:
            logging.error(f"ERREUR indexation texte libre (agent_id={agent_id}) : {e}")

    # Indexation Notion : même trou que texte_libre avait avant correction
    # -- jusqu'ici seul le cron quotidien (3h) l'indexait, voir
    # _indexer_notion_arriere_plan ci-dessus. En tâche de fond pour ne pas
    # retarder la réponse de création (peut prendre du temps si la page a
    # beaucoup de sous-pages).
    if notion_page_id:
        background_tasks.add_task(_indexer_notion_arriere_plan, agent_id, notion_page_id)

    url_base = get_secret("URL_RETOUR_APP")
    lien = f"{url_base.rstrip('/')}/?agent={agent_id}" if url_base else None
    if not url_base:
        logging.error("URL_RETOUR_APP absent : impossible de construire le lien complet de l'agent.")

    journaliser(
        action="agent.cree",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"nom": payload.nom.strip(), "categorie_id": payload.categorie_id},
        request=request,
    )

    return AgentCree(id=agent_id, nom=payload.nom.strip(), lien=lien)


class AgentDetailPublic(BaseModel):
    id: str
    nom: str
    icone_page: str = "🤖"
    # Ajoutés le 2026-07-16 (Bourama : reproduire visuellement l'accueil du
    # chat Streamlit -- dans le
    # chat Next.js, qui n'affichait jusqu'ici qu'un placeholder générique
    # "Pose ta question à {nomAgent}..." au lieu du titre/sous-titre propres
    # à chaque agent). Mêmes clés ui_config et mêmes valeurs de repli que
    # UI_CONFIG_PAR_DEFAUT côté Streamlit, pour un rendu identique.
    titre_accueil: str = "🎓 Votre coatch mathématique"
    sous_titre_accueil: str = "Tout comprendre sur les maths. Je te donne rien, je t'enseigne tout."
    image_vitrine_url: Optional[str] = None
    # Nouveau système d'icône (2026-08-05) : voir CreerAgentPayload.icone_url
    icone_url: Optional[str] = None
    description: str = ""
    owner_id: str
    # Ajoutés le 2026-08-01 (chantier SEO/AEO) : nécessaires au frontend
    # pour construire le JSON-LD SoftwareApplication de la page publique
    # (agent/[id]/page.tsx) — c'est ce qui dit explicitement à Google/aux
    # IA "cette IA est spécialisée dans TEL domaine précis". Mêmes champs
    # texte libre que /api/feed, voir _valider_et_verifier_disponibilite_*
    # plus haut dans ce fichier pour leur origine.
    matiere: Optional[str] = None
    matiere_detail: Optional[str] = None
    langue_africaine: Optional[str] = None
    metier: Optional[str] = None
    filiere: Optional[str] = None
    domaine: Optional[str] = None
    # Ajoutés le 02/08/2026 (modèles premium Claude/GPT/Gemini/DeepSeek) :
    # publics volontairement, contrairement à distributeur_debloque/
    # palier_debloque (bruts) qui restent réservés au créateur dans
    # AgentEditable -- un visiteur/étudiant a juste besoin de savoir QUELS
    # modèles il peut choisir, pas la mécanique d'abonnement derrière.
    modeles_disponibles: List[dict] = Field(default_factory=list)
    modele_choisi: Optional[str] = None
    # Ajouté 2026-08-06 (agent "Nitrux", contenu dynamique par matière) :
    # dit au frontend d'afficher l'entrée "Matières" (écrire du contenu /
    # entrer un code) dans le chat de cet agent précis.
    contenu_dynamique_par_matiere: bool = False
    # Ajouté 08/08/2026 : ce champ existait déjà dans ui_config (voir
    # ModifierAgentPayload.placeholder_saisie, écrit et sauvegardé
    # correctement depuis le 2026-07-14) mais n'était jamais exposé par ce
    # modèle public -- la valeur configurée par le créateur était donc
    # enregistrée en base mais jamais lue par le chat, qui retombait
    # systématiquement sur un texte générique côté frontend (voir
    # BarreDeSaisie.tsx). Corrigé ici.
    placeholder_saisie: str = "Pose ta question..."
    # Ajouté 06/08/2026 (demande Bourama) : affichage du bouton "Sans
    # enseignant" dans la barre de saisie, piloté nous-mêmes en base
    # (indépendant de contenu_dynamique_par_matiere) -- pas encore
    # automatique (dépendra plus tard d'un lien vers une IA "parents" +
    # du niveau d'étude), pour l'instant un simple interrupteur manuel.
    # Défaut TRUE pour tous les agents.
    bouton_sans_enseignant: bool = True
    # Ajouté 06/08/2026 (demande Bourama) : affichage de la section "Mes
    # comportements" (voir api/comportements_etudiants.py), piloté nous-
    # mêmes en base, même logique que bouton_sans_enseignant ci-dessus.
    # Défaut FALSE (contrairement à bouton_sans_enseignant) : Nitrux
    # uniquement pour l'instant.
    section_mes_comportements: bool = False


@router.get("/{agent_id}", response_model=AgentDetailPublic)
def obtenir_agent_public(agent_id: str):
    """
    Détail public d'un agent, pour la page `/agent/[slug]`. Public, aucune auth requise, comme
    `/api/feed`. `agent_id` sert de slug : pas de colonne `slug` dédiée
    sur `agents`.

    `owner_id` est renvoyé pour permettre au frontend de lier vers le
    portfolio créateur (`/u/[slug]`, Étape E) une fois `GET
    /api/profiles/{slug}` construit ; pas encore de résolution
    profil <-> agent ici, volontairement, pour ne pas dupliquer une
    logique qui appartient à l'endpoint profils.

    404 si l'agent n'existe pas OU s'il est désactivé (`actif` is
    False) : une page publique ne doit pas exister pour un agent
    désactivé, même en connaissant son id directement. Convention "True
    par défaut" si `actif` est absent/NULL, identique à
    `l'ancienne interface Streamlit` et à `/api/feed`.
    """
    try:
        res = (
            supabase.table("agents")
            .select(
                "id, nom, ui_config, image_vitrine_url, icone_url, description, owner_id, actif, "
                "matiere, matiere_detail, langue_africaine, metier, filiere, domaine, "
                "distributeur_debloque, palier_debloque, modele_choisi, "
                "contenu_dynamique_par_matiere, bouton_sans_enseignant, section_mes_comportements"
            )
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent public {agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_CET_AGENT_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")

    ligne = res.data
    if ligne.get("actif") is False:
        raise erreur_api(404, "AGENT_INTROUVABLE")

    _ui_config = ligne.get("ui_config") or {}
    return AgentDetailPublic(
        id=ligne["id"],
        nom=ligne["nom"],
        icone_page=_ui_config.get("icone_page", "🤖"),
        titre_accueil=_ui_config.get("titre_accueil") or AgentDetailPublic.model_fields["titre_accueil"].default,
        sous_titre_accueil=_ui_config.get("sous_titre_accueil") or AgentDetailPublic.model_fields["sous_titre_accueil"].default,
        placeholder_saisie=_ui_config.get("placeholder_saisie") or AgentDetailPublic.model_fields["placeholder_saisie"].default,
        image_vitrine_url=ligne.get("image_vitrine_url"),
        icone_url=ligne.get("icone_url"),
        description=ligne.get("description") or "",
        owner_id=ligne["owner_id"],
        matiere=ligne.get("matiere"),
        matiere_detail=ligne.get("matiere_detail"),
        langue_africaine=ligne.get("langue_africaine"),
        metier=ligne.get("metier"),
        filiere=ligne.get("filiere"),
        domaine=ligne.get("domaine"),
        modeles_disponibles=modeles_disponibles_pour_agent(
            ligne.get("distributeur_debloque"), ligne.get("palier_debloque")
        ),
        modele_choisi=ligne.get("modele_choisi"),
        contenu_dynamique_par_matiere=bool(ligne.get("contenu_dynamique_par_matiere")),
        bouton_sans_enseignant=ligne.get("bouton_sans_enseignant") if ligne.get("bouton_sans_enseignant") is not None else True,
        section_mes_comportements=bool(ligne.get("section_mes_comportements")),
    )


@router.get("/{agent_id}/outils-disponibles")
def obtenir_outils_disponibles(agent_id: str, utilisateur=Depends(utilisateur_optionnel)):
    """
    Correctif demande par Bourama le 29/07/2026 : la barre de saisie
    (BarreDeSaisie.tsx, liste OUTILS_DISPONIBLES) affichait TOUS les
    boutons d'outils pour TOUS les agents, sans jamais verifier ceux
    reellement autorises en base pour l'agent en cours (agents_outils_
    generation / agents_serveurs, voir core/registre_outils.py et
    DroitsAgentCreation.tsx pour la configuration cote createur). Un
    outil desactive par le createur restait donc cliquable dans le chat
    -- un clic dessus le faisait disparaitre SILENCIEUSEMENT de la
    requete reelle envoyee a Groq (deja corrige le 29/07 cote system
    prompt, voir chat() dans core/main.py), le modele croyant a tort
    qu'il etait disponible et pouvant halluciner un faux appel d'outil
    en texte plutot que d'utiliser le vrai mecanisme.

    Reutilise TEL QUEL lister_outils_autorises_pour_agent (meme fonction
    qui construit la vraie liste envoyee a Groq, voir core/mcp_tools.py)
    -- aucune logique dupliquee, donc aucune desynchronisation possible
    entre ce que ce endpoint annonce et ce qui est reellement autorise.

    Public (utilisateur_optionnel, comme le chat lui-meme) : un visiteur
    non connecte doit pouvoir voir les boutons d'un agent public, meme
    si certains outils "par utilisateur" (ex: Notion) resteront filtres
    en l'absence de connexion -- comportement identique a une vraie
    conversation.

    Ajout (session suivante, meme jour) : "actions_locales" -- boutons UI
    du chat qui ne sont PAS des outils LLM (localisation, clavier LaTeX,
    forcer une recherche, dessin -- prefixe "ui_"), donc invisibles pour
    lister_outils_autorises_pour_agent. Le createur les active/desactive
    quand meme par agent (agents_actions_locales, categorie 4 du
    registre), cf. DroitsAgentCreation.tsx / DroitsAgent.tsx.
    """
    user_id = utilisateur.id if utilisateur else None
    try:
        outils_pour_llm, _ = lister_outils_autorises_pour_agent(get_secret, user_id, agent_id)
        registre_res = supabase.table("registre_outils_plateforme").select("nom_outil, disponible").eq(
            "categorie", 4
        ).execute()
        locales_res = supabase.table("agents_actions_locales").select("nom_action").eq("agent_id", agent_id).execute()
    except Exception as e:
        logging.error(f"ERREUR (outils disponibles agent {agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LES_OUTILS_DISPONIBLES")

    locales_disponibles = {l["nom_outil"] for l in (registre_res.data or []) if l["disponible"]}
    locales_coches = {l["nom_action"] for l in (locales_res.data or [])} & locales_disponibles

    return {
        "outils": [o["function"]["name"] for o in outils_pour_llm],
        "actions_locales": sorted(locales_coches),
    }


class MettreAJourVitrinePayload(BaseModel):
    # Optional (pas absent = pas de valeur) volontairement, pour un PATCH
    # partiel : un champ omis (None) n'est pas touché, contrairement à une
    # chaîne vide envoyée explicitement, qui efface la valeur existante.
    image_vitrine_url: Optional[str] = None
    icone_url: Optional[str] = None
    description: Optional[str] = None


@router.patch("/{agent_id}/vitrine", response_model=AgentDetailPublic)
def mettre_a_jour_vitrine(
    agent_id: str,
    payload: MettreAJourVitrinePayload,
    request: Request,
    utilisateur=Depends(utilisateur_courant),
):
    """
    Mise à jour de la vitrine publique d'un agent (image + description),
    depuis le dashboard "Mes agents".

    Vérifie que `owner_id` du token correspond au propriétaire de l'agent
    (403 sinon) — même exigence appliquée à l'upload de
    documents, en premier
    puisque c'est le premier endpoint de modification (hors création) du
    pivot social.
    """
    try:
        res = (
            supabase.table("agents")
            .select("id, nom, ui_config, image_vitrine_url, icone_url, description, owner_id")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant mise à jour vitrine) : {e}")
        raise erreur_api(500, "VITRINE_INDISPONIBLE")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")

    ligne = res.data
    if ligne["owner_id"] != utilisateur.id:
        raise erreur_api(403, "CET_AGENT_NE_T_APPARTIENT_PAS")

    mise_a_jour = {}
    if payload.image_vitrine_url is not None:
        mise_a_jour["image_vitrine_url"] = payload.image_vitrine_url
    if payload.icone_url is not None:
        mise_a_jour["icone_url"] = payload.icone_url
    if payload.description is not None:
        mise_a_jour["description"] = payload.description.strip()

    if not mise_a_jour:
        raise erreur_api(422, "VITRINE_RIEN_A_METTRE_A_JOUR")

    try:
        supabase.table("agents").update(mise_a_jour).eq("id", agent_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (mise à jour vitrine agent {agent_id}) : {e}")
        raise erreur_api(500, "VITRINE_ERREUR_TECHNIQUE")

    ligne.update(mise_a_jour)

    journaliser(
        action="agent.vitrine.modifiee",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"champs_modifies": sorted(mise_a_jour.keys())},
        request=request,
    )

    _ui_config = ligne.get("ui_config") or {}
    return AgentDetailPublic(
        id=ligne["id"],
        nom=ligne["nom"],
        icone_page=_ui_config.get("icone_page", "🤖"),
        titre_accueil=_ui_config.get("titre_accueil") or AgentDetailPublic.model_fields["titre_accueil"].default,
        sous_titre_accueil=_ui_config.get("sous_titre_accueil") or AgentDetailPublic.model_fields["sous_titre_accueil"].default,
        placeholder_saisie=_ui_config.get("placeholder_saisie") or AgentDetailPublic.model_fields["placeholder_saisie"].default,
        image_vitrine_url=ligne.get("image_vitrine_url"),
        icone_url=ligne.get("icone_url"),
        description=ligne.get("description") or "",
        owner_id=ligne["owner_id"],
    )


class ConfigCreation(BaseModel):
    """
    Les champs discrets du formulaire de création (ton, posture,
    limites, comportements, type de connaissance, sous-titre), stockés à
    part depuis le 2026-07-12 dans `agents.config_creation` (voir
    migration `agents_config_creation`) — avant cette date,
    `composer_system_prompt` les fusionnait dans `system_prompt` puis les
    jetait, aucune trace séparée ne survivait. Un agent créé avant cette
    migration a `config_creation IS NULL` : voir
    `obtenir_agent_pour_edition`, qui retombe sur le `system_prompt` brut
    dans ce cas plutôt que d'inventer des valeurs vides qui écraseraient
    un prompt déjà soigné à l'enregistrement suivant.
    """

    ton: str = "Tutoiement (tu)"
    posture_generale: str = ""
    limites_globales: str = ""
    comportements: List[LigneComportement] = Field(default_factory=list)
    type_connaissance: str = ""
    description_connaissance: str = ""
    sous_titre: str = ""


class AgentEditable(BaseModel):
    """
    Vue complète d'un agent pour SON propriétaire (contrairement à
    AgentDetailPublic, qui est ce que voit un visiteur).

    `config_creation` est `None` pour un agent créé avant le 2026-07-12
    (voir ConfigCreation) : dans ce cas, le frontend doit retomber sur
    l'édition du `system_prompt` brut (toujours renvoyé, à jour) plutôt
    que d'afficher un formulaire structuré avec des champs vides qui
    écraseraient le prompt existant s'ils étaient enregistrés tels quels.
    """

    id: str
    nom: str
    # Ajouté 2026-08-05 (section Administrateurs) : permet au frontend de
    # distinguer le propriétaire d'un administrateur désigné, qui voit la
    # même page mais pas cet onglet (lui-même ne peut pas désigner
    # d'autres administrateurs, voir /administrateurs côté backend).
    owner_id: str = ""
    icone_page: str = "🤖"
    system_prompt: str = ""
    config_creation: Optional[ConfigCreation] = None
    tools_enabled: List[str] = Field(default_factory=list)
    notion_page_id: Optional[str] = None
    notion_index_statut: str = "jamais"
    notion_index_message: Optional[str] = None
    notion_index_maj_le: Optional[str] = None
    texte_libre: str = ""
    image_vitrine_url: Optional[str] = None
    icone_url: Optional[str] = None
    description: str = ""
    sous_titre: str = ""
    placeholder_saisie: str = "Pose ta question..."
    actif: bool = True
    categorie_id: Optional[str] = None
    matiere: Optional[str] = None
    matiere_detail: Optional[str] = None
    langue_africaine: Optional[str] = None
    metier: Optional[str] = None
    filiere: Optional[str] = None
    domaine: Optional[str] = None
    execution: Optional[str] = None
    profil_utilisateur_schema: List[ChampProfilUtilisateur] = Field(default_factory=list)
    # Proactivité (25/07) : le créateur décide QUAND (délai d'inactivité),
    # à quelle fréquence max, et POURQUOI/COMMENT (instructions libres,
    # même logique que system_prompt) -- voir core/proactivite.py.
    proactivite_active: bool = False
    proactivite_delai_jours: int = 4
    proactivite_cooldown_jours: int = 7
    proactivite_instructions: str = ""
    # Modeles premium (02/08/2026, Bourama : "on va ajouter Claude, GPT et
    # DeepSeek", voir page Notion "Pricing -- Agent Maths"). Champs en
    # LECTURE SEULE ici -- distributeur_debloque/palier_debloque ne sont
    # PAS dans ModifierAgentPayload plus bas : pas de systeme de paiement
    # pour l'instant (v1), donc uniquement modifiable par Bourama a la
    # main dans Supabase, jamais par le createur lui-meme via ce PATCH
    # (sinon n'importe quel createur se debloquerait Claude gratuitement).
    # `modeles_disponibles` est calcule (pas stocke), voir
    # core/fournisseurs_llm.py:modeles_disponibles_pour_agent -- vide si
    # rien n'est debloque, le frontend n'affiche alors aucun selecteur.
    distributeur_debloque: Optional[str] = None
    palier_debloque: Optional[str] = None
    modeles_disponibles: List[dict] = Field(default_factory=list)
    modele_choisi: Optional[str] = None


@router.get("/{agent_id}/edition", response_model=AgentEditable)
def obtenir_agent_pour_edition(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Ajouté le 2026-07-12 (Bourama : "on ne peut pas modifier ces agents
    créés", gros morceau manquant depuis le début du pivot social — la
    seule modification possible jusqu'ici était `mettre_a_jour_vitrine`,
    jamais branchée à aucune page côté Next.js). Réservé au propriétaire
    (403 sinon) : contrairement à `obtenir_agent_public`, cette vue
    contient le `system_prompt` complet, pas destiné aux visiteurs.
    """
    try:
        res = (
            supabase.table("agents")
            .select(
                "id, nom, ui_config, system_prompt, config_creation, tools_enabled, "
                "notion_page_id, knowledge_source, image_vitrine_url, icone_url, description, "
                "notion_index_statut, notion_index_message, notion_index_maj_le, "
                "actif, owner_id, categorie_id, matiere, matiere_detail, langue_africaine, "
                "metier, filiere, domaine, execution, "
                "profil_utilisateur_schema, "
                "proactivite_active, proactivite_delai_jours, proactivite_cooldown_jours, "
                "proactivite_instructions, "
                "distributeur_debloque, palier_debloque, modele_choisi"
            )
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} pour édition) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_L_AGENT_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")

    ligne = res.data
    if not peut_modifier_comportement(utilisateur.id, ligne["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    config_brut = ligne.get("config_creation")

    return AgentEditable(
        id=ligne["id"],
        nom=ligne["nom"],
        owner_id=ligne["owner_id"],
        icone_page=(ligne.get("ui_config") or {}).get("icone_page", "🤖"),
        system_prompt=ligne.get("system_prompt") or "",
        config_creation=ConfigCreation(**config_brut) if config_brut else None,
        tools_enabled=ligne.get("tools_enabled") or [],
        notion_page_id=ligne.get("notion_page_id"),
        notion_index_statut=ligne.get("notion_index_statut") or "jamais",
        notion_index_message=ligne.get("notion_index_message"),
        notion_index_maj_le=ligne.get("notion_index_maj_le"),
        texte_libre=(ligne.get("knowledge_source") or {}).get("texte_libre", ""),
        image_vitrine_url=ligne.get("image_vitrine_url"),
        icone_url=ligne.get("icone_url"),
        description=ligne.get("description") or "",
        sous_titre=(ligne.get("ui_config") or {}).get("sous_titre_accueil", ""),
        placeholder_saisie=(ligne.get("ui_config") or {}).get(
            "placeholder_saisie", "Pose ta question..."
        ),
        actif=ligne.get("actif", True),
        categorie_id=ligne.get("categorie_id"),
        matiere=ligne.get("matiere"),
        matiere_detail=ligne.get("matiere_detail"),
        langue_africaine=ligne.get("langue_africaine"),
        metier=ligne.get("metier"),
        filiere=ligne.get("filiere"),
        domaine=ligne.get("domaine"),
        execution=ligne.get("execution"),
        profil_utilisateur_schema=[
            ChampProfilUtilisateur(**c) for c in (ligne.get("profil_utilisateur_schema") or [])
        ],
        proactivite_active=ligne.get("proactivite_active", False),
        proactivite_delai_jours=ligne.get("proactivite_delai_jours", 4),
        proactivite_cooldown_jours=ligne.get("proactivite_cooldown_jours", 7),
        proactivite_instructions=ligne.get("proactivite_instructions") or "",
        distributeur_debloque=ligne.get("distributeur_debloque"),
        palier_debloque=ligne.get("palier_debloque"),
        modeles_disponibles=modeles_disponibles_pour_agent(
            ligne.get("distributeur_debloque"), ligne.get("palier_debloque")
        ),
        modele_choisi=ligne.get("modele_choisi"),
    )


class ModifierAgentPayload(BaseModel):
    # Tous optionnels : PATCH partiel, un champ omis (None) n'est pas
    # touché — même convention que MettreAJourVitrinePayload.
    nom: Optional[str] = None
    icone_page: Optional[str] = None
    # Champs discrets (formulaire structuré, voir ConfigCreation) : si
    # AU MOINS UN est fourni, le system_prompt est RECOMPOSÉ à partir
    # d'eux (fusionnés avec config_creation existant pour les champs
    # omis) et system_prompt ci-dessous est alors ignoré. À utiliser
    # quand AgentEditable.config_creation n'était pas None.
    ton: Optional[str] = None
    posture_generale: Optional[str] = None
    limites_globales: Optional[str] = None
    comportements: Optional[List[LigneComportement]] = None
    type_connaissance: Optional[str] = None
    description_connaissance: Optional[str] = None
    sous_titre: Optional[str] = None
    # Point 5 (2026-07-14, Bourama) : indépendant de nom/icone_page/
    # sous_titre, même traitement que sous_titre dans le handler plus bas
    # (sa propre condition, pas rattaché au bloc nom/icone).
    placeholder_saisie: Optional[str] = None
    # Repli brut (agents pré-migration, AgentEditable.config_creation
    # était None) : ignoré si un champ discret ci-dessus est fourni.
    system_prompt: Optional[str] = None
    lien_notion: Optional[str] = None
    texte_libre: Optional[str] = None
    image_vitrine_url: Optional[str] = None
    icone_url: Optional[str] = None
    description: Optional[str] = None
    actif: Optional[bool] = None
    categorie_id: Optional[str] = None
    matiere: Optional[str] = None
    matiere_detail: Optional[str] = None
    langue_africaine: Optional[str] = None
    metier: Optional[str] = None
    filiere: Optional[str] = None
    domaine: Optional[str] = None
    execution: Optional[str] = None
    profil_utilisateur_schema: Optional[List[ChampProfilUtilisateur]] = None
    # Proactivité (25/07) : voir AgentEditable ci-dessus.
    proactivite_active: Optional[bool] = None
    proactivite_delai_jours: Optional[int] = None
    proactivite_cooldown_jours: Optional[int] = None
    proactivite_instructions: Optional[str] = None
    # Modele par defaut parmi ceux DEJA debloques pour cet agent (voir
    # AgentEditable.modeles_disponibles) -- distributeur_debloque/
    # palier_debloque eux-memes ne sont PAS dans ce payload, voir le
    # commentaire sur AgentEditable plus haut. None accepte = pas de
    # preference explicite, l'agent retombe alors sur la cascade Groq
    # habituelle pour chaque message (aucun modele premium par defaut).
    modele_choisi: Optional[str] = None


@router.patch("/{agent_id}", response_model=AgentEditable)
def modifier_agent(
    agent_id: str,
    payload: ModifierAgentPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    utilisateur=Depends(utilisateur_courant),
):
    """
    Ajouté le 2026-07-12, voir AgentEditable/obtenir_agent_pour_edition
    ci-dessus pour le contexte. `agents.id` n'est JAMAIS modifié ici même
    si `nom` change : l'id sert de slug dans les URLs publiques
    (/agent/{id}) et de clé étrangère pour `agent_ratings`,
    `agent_comments`, `follows` — le renommer casserait tous les liens
    déjà partagés et les FK existantes. Seul `nom` (colonne d'affichage)
    et les champs dérivés dans `ui_config` (titre_page, titre_accueil,
    emoji_reponse, sous_titre_accueil) changent.
    """
    try:
        res = (
            supabase.table("agents")
            .select(
                "id, nom, ui_config, system_prompt, config_creation, tools_enabled, "
                "notion_page_id, knowledge_source, image_vitrine_url, icone_url, description, "
                "notion_index_statut, notion_index_message, notion_index_maj_le, "
                "actif, owner_id, categorie_id, matiere, matiere_detail, langue_africaine, "
                "metier, filiere, domaine, execution, "
                "profil_utilisateur_schema, "
                "proactivite_active, proactivite_delai_jours, proactivite_cooldown_jours, "
                "proactivite_instructions, "
                "distributeur_debloque, palier_debloque, modele_choisi"
            )
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant modification) : {e}")
        raise erreur_api(500, "AGENT_MODIFICATION_INDISPONIBLE")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")

    ligne = res.data
    if not peut_modifier_comportement(utilisateur.id, ligne["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    mise_a_jour = {}

    nom_final = ligne["nom"]
    if payload.nom is not None and payload.nom.strip():
        nom_final = payload.nom.strip()
        mise_a_jour["nom"] = nom_final

    ui_config = dict(ligne.get("ui_config") or {})
    icone_finale = ui_config.get("icone_page", "🤖")
    if payload.icone_page is not None and payload.icone_page.strip():
        icone_finale = payload.icone_page.strip()

    ui_config_modifie = False

    if payload.nom is not None or payload.icone_page is not None:
        ui_config.update(
            {
                "titre_page": nom_final,
                "icone_page": icone_finale,
                # Nouveau système d'icône (2026-08-05) : voir CreerAgentPayload,
                # même correction -- plus de préfixe emoji dans le titre.
                "titre_accueil": nom_final,
                "emoji_reponse": icone_finale,
            }
        )
        ui_config_modifie = True

    # sous_titre_accueil est indépendant de nom/icône, donc géré dans sa
    # propre condition plutôt que rattaché au bloc nom/icone_page
    # ci-dessus (voir la fix d'origine de ce champ) -- sinon modifier
    # UNIQUEMENT le sous-titre (sans toucher nom ni icône) n'aurait
    # jamais été écrit en base. Peut aussi arriver via les champs
    # discrets (recomposition ci-dessous) : le bloc discret prend le
    # dessus s'il est présent, voir plus bas.
    if payload.sous_titre is not None:
        ui_config["sous_titre_accueil"] = payload.sous_titre.strip()
        ui_config_modifie = True

    # Même raisonnement que sous_titre_accueil juste au-dessus (2026-07-14,
    # Bourama) : indépendant de nom/icône, sa propre condition.
    if payload.placeholder_saisie is not None:
        ui_config["placeholder_saisie"] = (
            payload.placeholder_saisie.strip() or "Pose ta question..."
        )
        ui_config_modifie = True

    if ui_config_modifie:
        mise_a_jour["ui_config"] = ui_config

    # Recomposition depuis les champs discrets si au moins un est fourni
    # (formulaire structuré, agent avec config_creation existant) ;
    # sinon repli sur system_prompt brut si fourni (agent pré-migration).
    config_actuel = dict(ligne.get("config_creation") or {})
    champs_discrets_fournis = any(
        v is not None
        for v in (
            payload.ton,
            payload.posture_generale,
            payload.limites_globales,
            payload.comportements,
            payload.type_connaissance,
            payload.description_connaissance,
        )
    )
    if champs_discrets_fournis:
        nouveau_config = {
            "ton": payload.ton if payload.ton is not None else config_actuel.get("ton", "Tutoiement (tu)"),
            "posture_generale": (
                payload.posture_generale
                if payload.posture_generale is not None
                else config_actuel.get("posture_generale", "")
            ),
            "limites_globales": (
                payload.limites_globales
                if payload.limites_globales is not None
                else config_actuel.get("limites_globales", "")
            ),
            "comportements": (
                [{"type_requete": l.type_requete, "comportement": l.comportement} for l in payload.comportements]
                if payload.comportements is not None
                else config_actuel.get("comportements", [])
            ),
            "type_connaissance": (
                payload.type_connaissance
                if payload.type_connaissance is not None
                else config_actuel.get("type_connaissance", "")
            ),
            "description_connaissance": (
                payload.description_connaissance
                if payload.description_connaissance is not None
                else config_actuel.get("description_connaissance", "")
            ),
            "sous_titre": (
                payload.sous_titre if payload.sous_titre is not None else config_actuel.get("sous_titre", "")
            ),
        }
        lignes_comportement = [
            (c["type_requete"], c["comportement"]) for c in nouveau_config["comportements"]
        ]
        mise_a_jour["system_prompt"] = composer_system_prompt(
            nouveau_config["ton"],
            nouveau_config["posture_generale"],
            nouveau_config["limites_globales"],
            lignes_comportement,
            nouveau_config["type_connaissance"],
            nouveau_config["description_connaissance"],
            nom=nom_final,
            description_publique=(
                payload.description if payload.description is not None else ligne.get("description") or ""
            ),
        )
        mise_a_jour["config_creation"] = nouveau_config
        # Le sous-titre discret prend le dessus sur celui déjà posé par
        # payload.sous_titre ci-dessus (même valeur si les deux sont
        # fournis, ce bloc est juste la source de vérité en cas de
        # formulaire structuré).
        ui_config["sous_titre_accueil"] = nouveau_config["sous_titre"] or ui_config.get(
            "sous_titre_accueil", ""
        )
        mise_a_jour["ui_config"] = ui_config
    elif payload.system_prompt is not None:
        mise_a_jour["system_prompt"] = payload.system_prompt.strip()

    if payload.lien_notion is not None:
        notion_page_id_nouveau = extraire_id_notion(payload.lien_notion)
        mise_a_jour["notion_page_id"] = notion_page_id_nouveau
        if notion_page_id_nouveau:
            # "en_cours" tout de suite (avant même l'indexation réelle, qui
            # se lance en tâche de fond juste après le .update() plus bas)
            # pour que le dashboard puisse afficher l'état sans attendre.
            mise_a_jour["notion_index_statut"] = "en_cours"
            mise_a_jour["notion_index_message"] = None
        else:
            # Lien Notion retiré du formulaire : plus rien à indexer.
            mise_a_jour["notion_index_statut"] = "jamais"
            mise_a_jour["notion_index_message"] = None
            mise_a_jour["notion_index_maj_le"] = None

    knowledge_source = dict(ligne.get("knowledge_source") or {})
    if payload.texte_libre is not None:
        knowledge_source["texte_libre"] = payload.texte_libre.strip()
        mise_a_jour["knowledge_source"] = knowledge_source

    if payload.image_vitrine_url is not None:
        mise_a_jour["image_vitrine_url"] = payload.image_vitrine_url
    if payload.icone_url is not None:
        mise_a_jour["icone_url"] = payload.icone_url
    if payload.description is not None:
        mise_a_jour["description"] = payload.description.strip()
    if payload.profil_utilisateur_schema is not None:
        mise_a_jour["profil_utilisateur_schema"] = [
            c.model_dump() for c in payload.profil_utilisateur_schema
        ]
    if payload.actif is not None:
        mise_a_jour["actif"] = payload.actif

    if payload.proactivite_active is not None:
        mise_a_jour["proactivite_active"] = payload.proactivite_active
    if payload.proactivite_delai_jours is not None:
        if payload.proactivite_delai_jours < 1:
            raise erreur_api(422, "LE_DELAI_D_INACTIVITE_DOIT_ETRE")
        mise_a_jour["proactivite_delai_jours"] = payload.proactivite_delai_jours
    if payload.proactivite_cooldown_jours is not None:
        if payload.proactivite_cooldown_jours < 1:
            raise erreur_api(422, "LE_DELAI_MINIMUM_ENTRE_DEUX_RELANCES")
        mise_a_jour["proactivite_cooldown_jours"] = payload.proactivite_cooldown_jours
    if payload.proactivite_instructions is not None:
        mise_a_jour["proactivite_instructions"] = payload.proactivite_instructions.strip()

    if payload.modele_choisi is not None:
        if payload.modele_choisi == "":
            mise_a_jour["modele_choisi"] = None
        elif not modele_id_est_autorise(
            payload.modele_choisi, ligne.get("distributeur_debloque"), ligne.get("palier_debloque")
        ):
            # Le createur ne peut choisir QUE parmi les modeles reellement
            # debloques pour son agent -- jamais de confiance aveugle
            # dans un modele_id envoye par le frontend (voir
            # core/fournisseurs_llm.py:modele_id_est_autorise).
            raise erreur_api(422, "CE_MODELE_N_EST_PAS_DEBLOQUE_POUR")
        else:
            mise_a_jour["modele_choisi"] = payload.modele_choisi

    if payload.categorie_id is not None:
        try:
            categorie_existe = (
                supabase.table("categories")
                .select("id")
                .eq("id", payload.categorie_id)
                .maybe_single()
                .execute()
            )
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (vérification catégorie={payload.categorie_id}) : {e}")
            categorie_existe = None
        if not categorie_existe or not categorie_existe.data:
            raise erreur_api(422, "CATEGORIE_INCONNUE")
        mise_a_jour["categorie_id"] = payload.categorie_id

    if payload.matiere is not None:
        _valider_et_verifier_disponibilite_matiere(
            payload.matiere, payload.matiere_detail, agent_id_a_exclure=agent_id
        )
        mise_a_jour["matiere"] = payload.matiere
        mise_a_jour["matiere_detail"] = (
            (payload.matiere_detail or "").strip() or None if payload.matiere == "Autre" else None
        )

    if payload.langue_africaine is not None:
        langue_normalisee = payload.langue_africaine.strip() or None
        _valider_et_verifier_disponibilite_langue_africaine(langue_normalisee, agent_id_a_exclure=agent_id)
        mise_a_jour["langue_africaine"] = langue_normalisee

    if payload.metier is not None:
        metier_normalise = payload.metier.strip() or None
        _valider_et_verifier_disponibilite_metier(metier_normalise, agent_id_a_exclure=agent_id)
        mise_a_jour["metier"] = metier_normalise

    if payload.filiere is not None:
        filiere_normalisee = payload.filiere.strip() or None
        _valider_et_verifier_disponibilite_filiere(filiere_normalisee, agent_id_a_exclure=agent_id)
        mise_a_jour["filiere"] = filiere_normalisee

    if payload.domaine is not None:
        domaine_normalise = payload.domaine.strip() or None
        _valider_et_verifier_disponibilite_domaine(domaine_normalise, agent_id_a_exclure=agent_id)
        mise_a_jour["domaine"] = domaine_normalise

    if payload.execution is not None:
        execution_normalisee = payload.execution.strip() or None
        _valider_et_verifier_disponibilite_execution(execution_normalisee, agent_id_a_exclure=agent_id)
        mise_a_jour["execution"] = execution_normalisee

    # Exclusivité entre les 6 catégories (08/08/2026, bouton "Changer de
    # catégorie/spécialité" du dashboard) : un agent n'appartient qu'à UNE
    # catégorie à la fois, même règle qu'à la création -- mais rien ici ne
    # l'imposait, un agent pouvait en théorie accumuler matiere ET metier
    # si les deux étaient un jour envoyés séparément. Dès que la requête
    # change l'une des 6, on vide les 5 autres (sauf celles que CETTE
    # MÊME requête fournit aussi explicitement, pour ne rien casser si un
    # futur appelant en envoie plusieurs à la fois délibérément).
    _champs_categorie = ["matiere", "langue_africaine", "metier", "filiere", "domaine", "execution"]
    _champs_categorie_fournis = [c for c in _champs_categorie if getattr(payload, c) is not None]
    if _champs_categorie_fournis:
        for _champ in _champs_categorie:
            if _champ not in _champs_categorie_fournis:
                mise_a_jour[_champ] = None
        if "matiere" not in _champs_categorie_fournis:
            mise_a_jour["matiere_detail"] = None

    if not mise_a_jour:
        raise erreur_api(422, "RIEN_A_MODIFIER")

    try:
        # .eq("owner_id", ...) en plus de .eq("id", ...) : sécurité
        # redondante avec le check ci-dessus, même précaution que
        # l'ancienne interface Streamlit (qui scope aussi son .update() par
        # owner_id, pas seulement par un if avant).
        supabase.table("agents").update(mise_a_jour).eq("id", agent_id).eq(
            "owner_id", utilisateur.id
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (modification agent {agent_id}) : {e}")
        raise erreur_api(500, "AGENT_MODIFICATION_ERREUR_TECHNIQUE")

    # Indexation Notion : jusqu'ici seul le cron quotidien (3h) indexait un
    # lien Notion nouveau/modifié -- rien dans l'UI ne le signalait (voir
    # _indexer_notion_arriere_plan). En tâche de fond pour ne pas retarder
    # la réponse de sauvegarde. Se relance même si le lien est identique à
    # l'ancien (sert aussi de "réindexer maintenant" manuel ; sans coût
    # réel car indexer_page ignore déjà les pages inchangées).
    notion_page_id_a_indexer = mise_a_jour.get("notion_page_id")
    if payload.lien_notion is not None and notion_page_id_a_indexer:
        background_tasks.add_task(_indexer_notion_arriere_plan, agent_id, notion_page_id_a_indexer)

    # Journalisé avec la LISTE des champs modifiés, pas leur contenu (le
    # system_prompt notamment peut être long/sensible) : suffisant pour
    # répondre à "qui a changé quoi sur cet agent, quand", sans dupliquer
    # tout le contenu dans le journal d'audit.
    journaliser(
        action="agent.modifie",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"champs_modifies": sorted(mise_a_jour.keys())},
        request=request,
    )

    # Réindexation du texte libre : best-effort, même choix que la
    # création (indexer_texte remplace toujours les anciens chunks pour
    # ce nom_fichier, voir supprimer_chunks_existants — pas de doublons
    # même si ce formulaire est réenregistré plusieurs fois).
    if payload.texte_libre is not None and payload.texte_libre.strip():
        try:
            indexer_texte(agent_id, "texte-libre", payload.texte_libre.strip())
        except Exception as e:
            logging.error(f"ERREUR réindexation texte libre (agent_id={agent_id}) : {e}")

    config_final = mise_a_jour.get("config_creation", ligne.get("config_creation"))

    return AgentEditable(
        id=agent_id,
        nom=nom_final,
        icone_page=icone_finale,
        system_prompt=mise_a_jour.get("system_prompt", ligne.get("system_prompt") or ""),
        config_creation=ConfigCreation(**config_final) if config_final else None,
        tools_enabled=ligne.get("tools_enabled") or [],
        notion_page_id=mise_a_jour.get("notion_page_id", ligne.get("notion_page_id")),
        texte_libre=knowledge_source.get("texte_libre", ""),
        image_vitrine_url=mise_a_jour.get("image_vitrine_url", ligne.get("image_vitrine_url")),
        icone_url=mise_a_jour.get("icone_url", ligne.get("icone_url")),
        description=mise_a_jour.get("description", ligne.get("description") or ""),
        sous_titre=ui_config.get("sous_titre_accueil", ""),
        actif=mise_a_jour.get("actif", ligne.get("actif", True)),
        categorie_id=mise_a_jour.get("categorie_id", ligne.get("categorie_id")),
        matiere=mise_a_jour.get("matiere", ligne.get("matiere")),
        matiere_detail=mise_a_jour.get("matiere_detail", ligne.get("matiere_detail")),
        langue_africaine=mise_a_jour.get("langue_africaine", ligne.get("langue_africaine")),
        metier=mise_a_jour.get("metier", ligne.get("metier")),
        filiere=mise_a_jour.get("filiere", ligne.get("filiere")),
        domaine=mise_a_jour.get("domaine", ligne.get("domaine")),
        execution=mise_a_jour.get("execution", ligne.get("execution")),
        proactivite_active=mise_a_jour.get("proactivite_active", ligne.get("proactivite_active", False)),
        proactivite_delai_jours=mise_a_jour.get("proactivite_delai_jours", ligne.get("proactivite_delai_jours", 4)),
        proactivite_cooldown_jours=mise_a_jour.get(
            "proactivite_cooldown_jours", ligne.get("proactivite_cooldown_jours", 7)
        ),
        proactivite_instructions=mise_a_jour.get(
            "proactivite_instructions", ligne.get("proactivite_instructions") or ""
        ),
        distributeur_debloque=ligne.get("distributeur_debloque"),
        palier_debloque=ligne.get("palier_debloque"),
        modeles_disponibles=modeles_disponibles_pour_agent(
            ligne.get("distributeur_debloque"), ligne.get("palier_debloque")
        ),
        modele_choisi=mise_a_jour.get("modele_choisi", ligne.get("modele_choisi")),
    )


class AdministrateurAgent(BaseModel):
    user_id: str
    email: str


class ListeAdministrateursReponse(BaseModel):
    administrateurs: List[AdministrateurAgent] = Field(default_factory=list)


class AjouterAdministrateurPayload(BaseModel):
    email: str


@router.get("/{agent_id}/administrateurs", response_model=ListeAdministrateursReponse)
def lister_administrateurs(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Section "Administrateurs" de /dashboard/agents/{id}/modifier (vitrine,
    2026-08-05, demande Bourama : "un champ, tu entres l'email, c'est
    fait, pas de confirmation"). Réservé au propriétaire, comme
    /edition -- un administrateur désigné ne peut pas lui-même en
    désigner d'autres.
    """
    ligne = _agent_owner_id_ou_404(agent_id)
    if utilisateur.id != ligne["owner_id"]:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    try:
        res = supabase.rpc("lister_administrateurs_agent", {"p_agent_id": agent_id}).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lister_administrateurs_agent {agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LES_ADMINISTRATEURS")

    return ListeAdministrateursReponse(
        administrateurs=[
            AdministrateurAgent(user_id=ligne_admin["user_id"], email=ligne_admin["email"])
            for ligne_admin in (res.data or [])
        ]
    )


@router.post("/{agent_id}/administrateurs", response_model=ListeAdministrateursReponse)
def ajouter_administrateur(
    agent_id: str, payload: AjouterAdministrateurPayload, utilisateur=Depends(utilisateur_courant)
):
    """
    Ajoute `payload.email` comme administrateur de `agent_id` (table
    `agents_administrateurs`, voir migrations/2026_08_05_*) : cette
    personne verra alors l'onglet "Administrer" dans "Mon espace" (app)
    pour cet agent. Aucune confirmation par email de son côté (décision
    Bourama, 2026-08-05) -- effectif immédiatement.
    """
    ligne = _agent_owner_id_ou_404(agent_id)
    if utilisateur.id != ligne["owner_id"]:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    try:
        res_user = supabase.rpc("email_vers_user_id", {"p_email": payload.email}).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (email_vers_user_id pour agent {agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_L_ADMINISTRATEUR")

    user_id_trouve = res_user.data if isinstance(res_user.data, str) else None
    if not user_id_trouve:
        raise erreur_api(404, "AUCUN_COMPTE_AVEC_CET_EMAIL")

    try:
        supabase.table("agents_administrateurs").upsert(
            {"agent_id": agent_id, "user_id": user_id_trouve}
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (ajout administrateur {user_id_trouve} sur agent {agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_L_ADMINISTRATEUR")

    return lister_administrateurs(agent_id, utilisateur)


class TesterProactivitePayload(BaseModel):
    # Instructions PAS FORCÉMENT encore enregistrées -- permet de tester un
    # brouillon avant de cliquer "Enregistrer" (voir ProactiviteAgent.tsx).
    proactivite_instructions: Optional[str] = None
    # Par défaut on teste sur soi-même (le créateur, en tant qu'utilisateur
    # de son propre agent) -- utile pour un agent tout juste créé, sans
    # vrai historique d'un tiers.
    user_id: Optional[str] = None


class TesterProactiviteReponse(BaseModel):
    relance: Optional[str] = None
    aucune_conversation: bool = False
    # 25/07 : distingue une vraie décision "je ne relance pas" d'un échec
    # technique (ex: quota Groq dépassé) -- les deux étaient auparavant
    # indiscernables côté interface (voir _decider_relance,
    # propager_erreurs).
    erreur: Optional[str] = None


@router.post("/{agent_id}/proactivite/tester", response_model=TesterProactiviteReponse)
def tester_proactivite(
    agent_id: str, payload: TesterProactivitePayload, utilisateur=Depends(utilisateur_courant)
):
    """
    Test EN LIVE de la décision de relance (25/07, demande Bourama : "faut
    tester") -- appelle directement _decider_relance (core/proactivite.py)
    sur une vraie conversation existante, SANS attendre les 6h du
    planificateur ni les jours d'inactivité configurés, et SANS rien
    envoyer réellement (pas de notification push, pas d'écriture dans
    l'historique) -- juste un aperçu de ce que l'agent déciderait.
    """
    try:
        agent = (
            supabase.table("agents")
            .select("owner_id, proactivite_instructions")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} pour test proactivité) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_L_AGENT_POUR")
    if not agent or not agent.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if agent.data["owner_id"] != utilisateur.id:
        raise erreur_api(403, "CET_AGENT_NE_T_APPARTIENT_PAS")

    cible_user_id = payload.user_id or utilisateur.id
    # Instructions "brouillon" envoyées par le frontend (pas encore
    # enregistrées) sinon celles déjà en base -- jamais le défaut
    # générique tant que le créateur n'a explicitement rien écrit nulle
    # part (voir _decider_relance qui, lui, applique le défaut si NULL).
    instructions = (
        payload.proactivite_instructions
        if payload.proactivite_instructions is not None
        else agent.data.get("proactivite_instructions")
    )

    from core.proactivite import _decider_relance

    try:
        a_un_historique = (
            supabase.table("historique_conversations")
            .select("user_id")
            .eq("agent_id", agent_id)
            .eq("user_id", cible_user_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérif historique test proactivité) : {e}")
        a_un_historique = None
    if not a_un_historique or not a_un_historique.data:
        return TesterProactiviteReponse(relance=None, aucune_conversation=True)

    message = None
    try:
        message = _decider_relance(agent_id, cible_user_id, instructions, propager_erreurs=True)
    except Exception as e:
        logging.error(f"ERREUR test proactivité (agent={agent_id}, user={cible_user_id}) : {e}")
        return TesterProactiviteReponse(
            relance=None,
            aucune_conversation=False,
            erreur=f"Échec technique (pas une vraie décision de l'IA) : {e}",
        )
    return TesterProactiviteReponse(relance=message, aucune_conversation=False)


@router.post("/{agent_id}/documents", status_code=201)
async def uploader_document(
    agent_id: str,
    request: Request,
    fichier: UploadFile = File(...),
    utilisateur=Depends(utilisateur_courant),
):
    """
    Ajoutée le 2026-07-12 suite à un bug remonté par Bourama : le nouveau formulaire
    de création (étape D.6 du pivot social) n'avait aucun moyen d'ajouter un
    PDF, `POST /api/agents` ne le gère pas lui-même (voir docstring en
    tête de ce fichier). Appelé APRÈS `POST /api/agents` : l'agent doit
    déjà exister, on a besoin de son id pour indexer le document dessus.

    Réutilise telle quelle la logique déjà en place côté Streamlit
    (`indexers/storage.py:upload_document` +
    `indexers/index_documents.py:indexer_document`) — pas de duplication.
    Corrigé le 26/07/2026 : `indexers/storage.py` écrivait dans un bucket
    legacy ("IA pour etudiants") au lieu de "documents-agents", donc TOUS
    les documents uploadés via cet endpoint atterrissaient au mauvais
    endroit depuis le début.

    Vérifie la propriété de l'agent (même exigence que
    `mettre_a_jour_vitrine`).
    """
    if fichier.content_type != "application/pdf":
        raise erreur_api(400, "SEULS_LES_FICHIERS_PDF_SONT_ACCEPTES")

    try:
        res = (
            supabase.table("agents")
            .select("id, owner_id")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant upload document) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_CE_DOCUMENT_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if not peut_gerer_base_connaissances(utilisateur.id, res.data["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    contenu = await fichier.read()
    if len(contenu) == 0:
        raise erreur_api(400, "FICHIER_VIDE")

    nom_original = fichier.filename or "document.pdf"
    nom_stockage = f"{agent_id}__{nom_original}"

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(contenu)
        chemin_temp = tmp.name

    try:
        upload_document(chemin_temp, nom_stockage)
        indexer_document(chemin_temp, nom_stockage, agent_id)
    except Exception as e:
        logging.error(f"ERREUR indexation PDF (agent_id={agent_id}, fichier={nom_original}) : {e}")
        raise erreur_api(500, "AGENT_CREE_MAIS_INDEXATION_ECHEC", nom=nom_original)
    finally:
        try:
            os.remove(chemin_temp)
        except OSError:
            pass

    journaliser(
        action="document.ajoute",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"nom_stockage": nom_stockage, "nom_original": nom_original},
        request=request,
    )

    return {"nom": nom_original, "statut": "indexé"}


@router.get("/{agent_id}/documents")
def lister_documents(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Ajouté le 2026-07-12, même contexte que `modifier_agent` (édition
    complète d'un agent, demandée par Bourama). Réutilise
    `indexers/storage.py:list_documents` telle quelle (liste TOUT le
    bucket, pas de filtre côté Supabase Storage par préfixe) puis filtre
    en Python sur `{agent_id}__` — même approche que
    `l'ancienne interface Streamlit` fait déjà, pas une nouvelle logique.
    """
    try:
        res = (
            supabase.table("agents")
            .select("owner_id")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant liste documents) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_LISTER_LES_DOCUMENTS_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if not peut_gerer_base_connaissances(utilisateur.id, res.data["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    try:
        tous_les_fichiers = list_documents()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE STORAGE (liste documents, agent_id={agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_LISTER_LES_DOCUMENTS_POUR")

    prefixe = f"{agent_id}__"
    fichiers_agent = [f for f in tous_les_fichiers if f.startswith(prefixe)]

    return [
        {
            "nom_stockage": f,
            "nom_affiche": f[len(prefixe):],
            "url": get_document_url(f),
        }
        for f in fichiers_agent
    ]


@router.delete("/{agent_id}/documents/{nom_stockage}", status_code=204)
def supprimer_document(agent_id: str, nom_stockage: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    """
    Ajouté le 2026-07-12, même contexte. Vérifie que `nom_stockage`
    commence bien par `{agent_id}__` (pas juste que l'agent appartient à
    l'utilisateur) : sinon un propriétaire d'un agent A pourrait passer
    le nom de stockage d'un document de l'agent B et le supprimer, tant
    que A lui appartient. Supprime aussi les chunks vectorisés associés
    (`supprimer_chunks_existants`), sinon le RAG continuerait à retrouver
    le contenu d'un PDF qui n'existe plus dans le stockage — même
    précaution que `l'ancienne interface Streamlit`.
    """
    try:
        res = (
            supabase.table("agents")
            .select("owner_id")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant suppression document) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CE_DOCUMENT_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if not peut_gerer_base_connaissances(utilisateur.id, res.data["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    if not nom_stockage.startswith(f"{agent_id}__"):
        raise erreur_api(403, "CE_DOCUMENT_N_APPARTIENT_PAS_A")

    try:
        delete_document(nom_stockage)
        supprimer_chunks_existants(agent_id, nom_stockage)
    except Exception as e:
        logging.error(f"ERREUR suppression document {nom_stockage} (agent_id={agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CE_DOCUMENT")

    journaliser(
        action="document.supprime",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"nom_stockage": nom_stockage},
        request=request,
    )


TYPES_BIBLIOTHEQUE_AUTORISES = {
    "application/pdf",
    "image/jpeg", "image/png", "image/webp",
    "audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4",
    "video/mp4", "video/webm", "video/quicktime",
}
TAILLE_MAX_BIBLIOTHEQUE_OCTETS = 50 * 1024 * 1024  # 50 Mo


@router.post("/{agent_id}/bibliotheque", status_code=201)
async def uploader_fichier_bibliotheque(
    agent_id: str,
    request: Request,
    fichier: UploadFile = File(...),
    titre: str = Form(None),
    description: str = Form(None),
    utilisateur=Depends(utilisateur_courant),
):
    """
    Bibliothèque du créateur pour CET agent (niveau="agent", voir
    core/bibliotheque_fichiers.py) : n'importe quel type de fichier
    (image/audio/vidéo/PDF...), avec une description donnée par le
    créateur pour que l'IA sache le retrouver via chercher_fichier
    -- le titre est optionnel (juste un intitulé court), c'est la
    description qui compte vraiment pour la recherche (2026-07-22,
    demande de Bourama : la description prime sur le titre).

    Cas particulier du PDF : en plus d'être stocké brut dans la
    bibliothèque (comme tout le reste), il est AUSSI vectorisé dans le
    circuit RAG existant (indexer_document), exactement comme le faisait
    déjà POST /{agent_id}/documents ci-dessus -- cette route ne remplace
    pas l'ancienne (toujours utilisée par le frontend existant), elle
    généralise le même geste à tous les types de fichiers.
    """
    if not (titre or "").strip() and not (description or "").strip():
        raise erreur_api(400, "DONNE_AU_MOINS_UNE_DESCRIPTION_OU")

    if fichier.content_type not in TYPES_BIBLIOTHEQUE_AUTORISES:
        raise erreur_api(400, "TYPE_DE_FICHIER_NON_SUPPORTE")

    try:
        res = (
            supabase.table("agents")
            .select("id, owner_id")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant upload bibliothèque) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_CE_FICHIER_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if not peut_gerer_base_connaissances(utilisateur.id, res.data["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    contenu = await fichier.read()
    if len(contenu) == 0:
        raise erreur_api(400, "FICHIER_VIDE")
    if len(contenu) > TAILLE_MAX_BIBLIOTHEQUE_OCTETS:
        raise erreur_api(400, "FICHIER_TROP_LOURD_50_MO_MAX")

    nom_original = fichier.filename or "fichier"

    if fichier.content_type == "application/pdf":
        nom_stockage_rag = f"{agent_id}__{nom_original}"
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(contenu)
            chemin_temp = tmp.name
        try:
            upload_document(chemin_temp, nom_stockage_rag)
            indexer_document(chemin_temp, nom_stockage_rag, agent_id)
        except Exception as e:
            logging.error(f"ERREUR vectorisation PDF bibliothèque (agent_id={agent_id}, fichier={nom_original}) : {e}")
            raise erreur_api(500, "FICHIER_VECTORISATION_ECHEC", nom=nom_original)
        finally:
            try:
                os.remove(chemin_temp)
            except OSError:
                pass

    description_finale = (
        f"{titre.strip()} — {description.strip()}" if (titre or "").strip() and (description or "").strip()
        else (description or titre or "").strip()
    )

    try:
        ligne = enregistrer_fichier(
            contenu=contenu,
            nom_fichier=nom_original,
            type_mime=fichier.content_type,
            niveau="agent",
            uploade_par=utilisateur.id,
            agent_id=agent_id,
            description=description_finale,
        )
    except Exception:
        raise erreur_api(500, "FICHIER_VECTORISE_MAIS_ECHEC_DU_STOCKAGE")

    journaliser(
        action="bibliotheque.ajoute",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"description": description_finale, "type_mime": fichier.content_type},
        request=request,
    )

    return ligne


class AjouterLienBibliothequePayload(BaseModel):
    url: str
    titre: str = None
    description: str = None


@router.post("/{agent_id}/bibliotheque/lien", status_code=201)
def ajouter_lien_bibliotheque(
    agent_id: str,
    payload: AjouterLienBibliothequePayload,
    request: Request,
    utilisateur=Depends(utilisateur_courant),
):
    """
    Pendant de uploader_fichier_bibliotheque ci-dessus, pour une entrée
    "lien" (Bourama 01/08 : le filtre "Lien" existait déjà sans aucun
    moyen réel d'en ajouter un). Pas de fichier : juste une URL +
    description, voir enregistrer_lien (core/bibliotheque_fichiers.py).
    """
    if not (payload.titre or "").strip() and not (payload.description or "").strip():
        raise erreur_api(400, "DONNE_AU_MOINS_UNE_DESCRIPTION_OU")
    if not (payload.url or "").strip():
        raise erreur_api(400, "URL_MANQUANTE")

    try:
        res = (
            supabase.table("agents")
            .select("id, owner_id")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant ajout lien bibliothèque) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_CE_LIEN_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if not peut_gerer_base_connaissances(utilisateur.id, res.data["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    description_finale = (
        f"{payload.titre.strip()} — {payload.description.strip()}"
        if (payload.titre or "").strip() and (payload.description or "").strip()
        else (payload.description or payload.titre or "").strip()
    )

    try:
        ligne = enregistrer_lien(
            url=payload.url.strip(),
            nom_fichier=(payload.titre or payload.url).strip(),
            niveau="agent",
            uploade_par=utilisateur.id,
            agent_id=agent_id,
            description=description_finale,
        )
    except Exception:
        raise erreur_api(500, "ECHEC_DE_L_ENREGISTREMENT_DU_LIEN")

    journaliser(
        action="bibliotheque.ajoute",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"description": description_finale, "type_mime": "text/uri-list"},
        request=request,
    )

    return ligne


@router.get("/{agent_id}/bibliotheque")
def lister_bibliotheque(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    try:
        res = supabase.table("agents").select("owner_id").eq("id", agent_id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant liste bibliothèque) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_LISTER_LA_BIBLIOTHEQUE_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if not peut_gerer_base_connaissances(utilisateur.id, res.data["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    return lister_fichiers("agent", agent_id=agent_id)


@router.delete("/{agent_id}/bibliotheque/{fichier_id}", status_code=204)
def supprimer_fichier_bibliotheque(agent_id: str, fichier_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    try:
        res = supabase.table("agents").select("owner_id").eq("id", agent_id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant suppression bibliothèque) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CE_FICHIER_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if not peut_gerer_base_connaissances(utilisateur.id, res.data["owner_id"], agent_id):
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_AGENT")

    supprimer_fichier(fichier_id)

    journaliser(
        action="bibliotheque.supprime",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"fichier_id": fichier_id},
        request=request,
    )


class NoterAgentPayload(BaseModel):
    # Classe manquante : provoquait un NameError au chargement du module,
    # qui faisait crasher TOUT le service au démarrage (pas seulement cet
    # endpoint) - Railway l'a marqué "CRASHED" juste après le déploiement
    # de main.py du 2026-07-13. La fonction noter_agent juste en dessous
    # l'utilisait déjà, seule la définition manquait.
    note: int


@router.post("/{agent_id}/rating", status_code=204)
def noter_agent(agent_id: str, payload: NoterAgentPayload, request: Request, utilisateur=Depends(utilisateur_courant)):
    """
    Note un agent de 1 à 5 (table `agent_ratings`,
    contrainte unique `(agent_id, user_id)` — un utilisateur note un agent
    une seule fois mais peut modifier sa note). Upsert plutôt qu'insert
    pour porter ce comportement directement, sans 409 + endpoint PATCH
    séparé pour le même geste côté frontend (contrairement à `/vitrine`,
    qui est une vraie modification d'un objet déjà possédé).

    Ne vérifie pas que l'agent existe avant d'insérer : la contrainte FK
    `agent_id` fera déjà échouer l'upsert proprement si l'agent n'existe
    pas, pas besoin de dupliquer cette vérification ici.
    """
    if not 1 <= payload.note <= 5:
        raise erreur_api(422, "LA_NOTE_DOIT_ETRE_COMPRISE_ENTRE")

    try:
        supabase.table("agent_ratings").upsert(
            {"agent_id": agent_id, "user_id": utilisateur.id, "note": payload.note},
            on_conflict="agent_id,user_id",
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (upsert note agent={agent_id}, user={utilisateur.id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_ENREGISTRER_LA_NOTE_POUR")

    journaliser(
        action="agent.note",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"note": payload.note},
        request=request,
    )


class NoteAgregee(BaseModel):
    moyenne: Optional[float] = None
    total: int = 0


@router.get("/{agent_id}/rating", response_model=NoteAgregee)
def obtenir_note_agent(agent_id: str):
    """
    Note moyenne publique d'un agent, pour l'affichage "note 1-5" sur
    `/agent/[slug]`.
    Public, aucune auth. `moyenne` reste `None` (pas 0) tant qu'aucune
    note n'existe, pour que le frontend distingue "pas encore noté" de
    "noté 0".
    """
    try:
        res = supabase.table("agent_ratings").select("note").eq("agent_id", agent_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture notes agent={agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LA_NOTE_POUR")

    notes = [ligne["note"] for ligne in (res.data or [])]
    if not notes:
        return NoteAgregee(moyenne=None, total=0)
    return NoteAgregee(moyenne=round(sum(notes) / len(notes), 2), total=len(notes))


class CommentaireCree(BaseModel):
    contenu: str


class Commentaire(BaseModel):
    id: str
    agent_id: str
    user_id: str
    # Nom affiché du profil de l'auteur, résolu par jointure côté serveur
    # (voir lister_commentaires / creer_commentaire). None si l'auteur n'a
    # jamais renseigné de profil (PATCH /api/profiles/me jamais appelé) —
    # le frontend décide de l'affichage de repli dans ce cas, pas ici.
    nom_affiche: Optional[str] = None
    contenu: str
    created_at: Optional[str] = None


@router.post("/{agent_id}/comments", response_model=Commentaire, status_code=201)
def creer_commentaire(agent_id: str, payload: CommentaireCree, utilisateur=Depends(utilisateur_courant)):
    """
    Ajoute un commentaire sur un agent (table `agent_comments`).
    Un commentaire par appel ; aucune limite de nombre
    par utilisateur pour l'instant, aucune modération demandée par
    Bourama à ce stade — à revoir si besoin plus tard.
    """
    contenu = payload.contenu.strip()
    if not contenu:
        raise erreur_api(422, "LE_COMMENTAIRE_NE_PEUT_PAS_ETRE")

    try:
        res = (
            supabase.table("agent_comments")
            .insert({"agent_id": agent_id, "user_id": utilisateur.id, "contenu": contenu})
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (insertion commentaire agent={agent_id}, user={utilisateur.id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_ENREGISTRER_LE_COMMENTAIRE_POUR")

    if not res.data:
        raise erreur_api(500, "LE_COMMENTAIRE_N_A_PAS_PU")

    ligne = res.data[0]

    # Best-effort : le nom affiché n'est pas critique au point de faire
    # échouer la création du commentaire si cette lecture rate.
    nom_affiche = None
    try:
        profil = (
            supabase.table("profiles")
            .select("nom_affiche")
            .eq("user_id", utilisateur.id)
            .maybe_single()
            .execute()
        )
        if profil and profil.data:
            nom_affiche = profil.data.get("nom_affiche") or None
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture nom_affiche pour commentaire, user={utilisateur.id}) : {e}")

    return Commentaire(
        id=str(ligne["id"]),
        agent_id=ligne["agent_id"],
        user_id=ligne["user_id"],
        nom_affiche=nom_affiche,
        contenu=ligne["contenu"],
        created_at=ligne.get("created_at"),
    )


@router.get("/{agent_id}/comments", response_model=List[Commentaire])
def lister_commentaires(
    agent_id: str,
    page: int = Query(1, ge=1),
    limite: int = Query(20, ge=1, le=50),
):
    """
    Liste paginée des commentaires d'un agent, plus récents d'abord.
    Public, aucune auth requise. Mêmes bornes de pagination que
    `/api/feed` (limite plafonnée à 50/page).
    """
    debut = (page - 1) * limite
    fin = debut + limite - 1
    try:
        res = (
            supabase.table("agent_comments")
            .select("id, agent_id, user_id, contenu, created_at")
            .eq("agent_id", agent_id)
            .order("created_at", desc=True)
            .range(debut, fin)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture commentaires agent={agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LES_COMMENTAIRES_POUR")

    lignes = res.data or []

    # Résolution des noms affichés en une seule requête groupée (pas une
    # par commentaire, pour ne pas multiplier les allers-retours Supabase
    # sur une page qui peut afficher jusqu'à 50 commentaires).
    noms_par_user_id = {}
    ids_uniques = list({ligne["user_id"] for ligne in lignes})
    if ids_uniques:
        try:
            profils_res = (
                supabase.table("profiles")
                .select("user_id, nom_affiche")
                .in_("user_id", ids_uniques)
                .execute()
            )
            for p in profils_res.data or []:
                if p.get("nom_affiche"):
                    noms_par_user_id[p["user_id"]] = p["nom_affiche"]
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (lecture noms affichés commentaires agent={agent_id}) : {e}")
            # best-effort : noms_par_user_id reste vide, chaque commentaire
            # retombe sur nom_affiche=None plutôt que de faire échouer
            # tout l'affichage des commentaires.

    return [
        Commentaire(
            id=str(ligne["id"]),
            agent_id=ligne["agent_id"],
            user_id=ligne["user_id"],
            nom_affiche=noms_par_user_id.get(ligne["user_id"]),
            contenu=ligne["contenu"],
            created_at=ligne.get("created_at"),
        )
        for ligne in lignes
    ]


def supprimer_agent_completement(agent_id: str):
    """
    Purge un agent et tout ce qui en dépend directement (documents PDF +
    chunks vectorisés `documents`/`prompts_chunks`, commentaires, notes ;
    `agent_updates` et ses likes/commentaires partent tout seuls via
    ON DELETE CASCADE, voir la migration
    pivot_social_mises_a_jour_agent). Ne vérifie AUCUNE propriété : c'est
    à l'appelant de l'avoir fait avant (voir supprimer_agent ci-dessous et
    api/profiles.py:supprimer_mon_compte, qui appelle ceci pour chaque
    agent du compte).

    Ne touche PAS à `historique_conversations` (journal permanent des
    échanges, jamais purgé ailleurs dans le projet) ni aux notifications
    qui référencent cet agent_id (colonne sans contrainte FK, laissée
    orpheline -- même choix de simplicité que le reste du projet, aucun
    nettoyage de notifications n'existait avant cette fonctionnalité).
    Chaque étape est best-effort (log et continue) sauf la suppression
    finale de la ligne `agents`, qui doit réussir pour que l'appelant
    sache si l'opération a globalement marché.
    """
    try:
        prefixe = f"{agent_id}__"
        for nom_stockage in [f for f in list_documents() if f.startswith(prefixe)]:
            try:
                delete_document(nom_stockage)
            except Exception as e:
                logging.error(f"ERREUR suppression fichier storage {nom_stockage} (agent={agent_id}) : {e}")
    except Exception as e:
        logging.error(f"ERREUR SUPABASE STORAGE (liste documents pour purge agent={agent_id}) : {e}")

    for table in ("documents", "prompts_chunks", "agent_comments", "agent_ratings"):
        try:
            supabase.table(table).delete().eq("agent_id", agent_id).execute()
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (purge table {table} pour agent={agent_id}) : {e}")

    supabase.table("agents").delete().eq("id", agent_id).execute()


@router.delete("/{agent_id}", status_code=204)
def supprimer_agent(agent_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    """
    "Supprimer un agent" dans la zone de danger de Mon espace (demande
    Bourama, 2026-07-15). Propriétaire uniquement -- même vérification que
    tous les autres endpoints d'écriture sur un agent.
    """
    try:
        res = supabase.table("agents").select("owner_id, nom").eq("id", agent_id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture agent {agent_id} avant suppression complète) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CET_AGENT_POUR")

    if not res or not res.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")
    if res.data["owner_id"] != utilisateur.id:
        raise erreur_api(403, "CET_AGENT_NE_T_APPARTIENT_PAS")

    try:
        supprimer_agent_completement(agent_id)
    except Exception as e:
        logging.error(f"ERREUR suppression complète agent={agent_id} : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CET_AGENT_POUR")

    # Journalisé après coup (pas avant) : on ne veut pas d'entrée
    # "agent.supprime" dans le journal si la suppression a en fait échoué.
    journaliser(
        action="agent.supprime",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"nom": res.data.get("nom")},
        request=request,
    )


class MonProfilAgent(BaseModel):
    """
    Vue du profil dynamique CÔTÉ UTILISATEUR FINAL (pas le créateur) :
    quels champs cet agent suit (`champs`, défini par le créateur, lecture
    seule ici) et ce qui a été retenu sur MOI par cet agent (`donnees`,
    modifiable). Ne pas confondre avec AgentEditable/api/profiles.py qui
    concernent le créateur -- voir clarification du 2026-07-21.
    """
    champs: List[ChampProfilUtilisateur]
    donnees: dict


class ModifierMonProfilPayload(BaseModel):
    donnees: dict


@router.get("/{agent_id}/mon-profil", response_model=MonProfilAgent)
def obtenir_mon_profil(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Ajouté le 2026-07-21 (demande Bourama : l'utilisateur final doit
    pouvoir voir/modifier ce que l'IA a retenu sur lui, pas seulement le
    créateur qui définit le schéma). Aucune vérification de propriété :
    n'importe quel utilisateur connecté peut voir SON PROPRE profil pour
    n'importe quel agent, ça ne concerne que lui.
    """
    try:
        res_agent = (
            supabase.table("agents")
            .select("profil_utilisateur_schema")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture schéma profil agent={agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LE_PROFIL_POUR")

    if not res_agent or not res_agent.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")

    champs = res_agent.data.get("profil_utilisateur_schema") or []

    try:
        res_profil = (
            supabase.table("agent_user_profiles")
            .select("donnees")
            .eq("agent_id", agent_id)
            .eq("user_id", utilisateur.id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (lecture agent_user_profiles agent={agent_id}, user={utilisateur.id}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_LE_PROFIL_POUR")

    donnees = (res_profil.data or {}).get("donnees") or {} if res_profil else {}

    return MonProfilAgent(
        champs=[ChampProfilUtilisateur(**c) for c in champs],
        donnees=donnees,
    )


@router.patch("/{agent_id}/mon-profil", status_code=204)
def modifier_mon_profil(
    agent_id: str, payload: ModifierMonProfilPayload, request: Request, utilisateur=Depends(utilisateur_courant)
):
    """
    Correction manuelle par l'utilisateur (l'IA s'est trompée, ou il veut
    préciser lui-même sans attendre l'extraction automatique). Ne garde
    que les clés présentes dans le schéma défini par le créateur -- pas
    de valeurs arbitraires en base, même écrites par l'utilisateur
    lui-même (le créateur reste seul décisionnaire de CE QUI est suivi,
    pas de CE QUE ça vaut).
    """
    try:
        res_agent = (
            supabase.table("agents")
            .select("profil_utilisateur_schema")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture schéma profil agent={agent_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_ENREGISTRER_LE_PROFIL_POUR")

    if not res_agent or not res_agent.data:
        raise erreur_api(404, "AGENT_INTROUVABLE")

    noms_valides = {c["nom"] for c in (res_agent.data.get("profil_utilisateur_schema") or [])}
    donnees_filtrees = {k: v for k, v in payload.donnees.items() if k in noms_valides}

    try:
        supabase.table("agent_user_profiles").upsert(
            {
                "agent_id": agent_id,
                "user_id": utilisateur.id,
                "donnees": donnees_filtrees,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="agent_id,user_id",
        ).execute()
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (upsert agent_user_profiles agent={agent_id}, user={utilisateur.id}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_D_ENREGISTRER_LE_PROFIL_POUR")

    journaliser(
        action="profil_utilisateur.modifie_par_user",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        request=request,
    )


@router.delete("/{agent_id}/mon-profil", status_code=204)
def effacer_mon_profil(agent_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    """
    Remet le profil à zéro pour cet agent (pas de suppression de compte,
    juste "oublie ce que tu sais de moi sur cet agent précis").
    """
    try:
        supabase.table("agent_user_profiles").delete().eq("agent_id", agent_id).eq(
            "user_id", utilisateur.id
        ).execute()
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (delete agent_user_profiles agent={agent_id}, user={utilisateur.id}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_D_EFFACER_LE_PROFIL_POUR")

    journaliser(
        action="profil_utilisateur.efface_par_user",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        request=request,
    )

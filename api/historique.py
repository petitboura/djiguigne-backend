"""
Étape ajoutée le 2026-07-13 (Bourama : "conversation récente par membre de
la plateforme qui se conserve pour chaque agent utilisée, dans le tableau
de bord, à gauche comme toute IA en fait").

Lit `historique_conversations` (voir la migration du même nom) : une table
PERMANENTE, jamais purgée, distincte de `conversations` (mémoire de
travail de l'IA, résumée puis supprimée -- voir core/main.py). Ce fichier
ne fait qu'AFFICHER l'historique ; il n'écrit jamais dedans (l'écriture se
fait uniquement depuis core/main.py, au moment de chaque échange).
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from core.erreurs import erreur_api

router = APIRouter(prefix="/api/historique", tags=["historique"])


class ConversationResume(BaseModel):
    agent_id: str
    agent_nom: str
    agent_icone: str = "🤖"
    dernier_message: str
    dernier_message_role: str
    derniere_activite: str


@router.get("", response_model=List[ConversationResume])
def lister_conversations(utilisateur=Depends(utilisateur_courant)):
    """
    Liste des agents avec qui CET utilisateur a déjà échangé, le plus
    récemment actif en premier -- pour la barre latérale façon ChatGPT
    demandée par Bourama. Un agent = une "conversation" ; le détail
    message par message est sur GET /api/historique/{agent_id}.

    Pas de pagination pour l'instant : le nombre d'agents avec qui un même
    utilisateur discute reste naturellement borné (contrairement au feed
    public), donc pas de risque de volume immédiat -- à revisiter si ça
    devient un problème réel (même remarque que pour le feed).
    """
    try:
        lignes = (
            supabase.table("historique_conversations")
            .select("agent_id, role, content, created_at")
            .eq("user_id", utilisateur.id)
            .order("created_at", desc=True)
            .execute()
        ).data or []
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lister_conversations, user_id={utilisateur.id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_L_HISTORIQUE")

    # Un seul aller-retour supabase pour tous les messages (déjà trié du
    # plus récent au plus ancien), puis on garde juste la PREMIÈRE ligne
    # rencontrée par agent_id -- c'est mécaniquement la plus récente,
    # grâce au tri ci-dessus. Évite une requête groupée par agent plus
    # coûteuse pour un gain minime ici.
    resume_par_agent = {}
    for ligne in lignes:
        aid = ligne["agent_id"]
        if aid not in resume_par_agent:
            resume_par_agent[aid] = ligne

    if not resume_par_agent:
        return []

    try:
        agents_res = (
            supabase.table("agents")
            .select("id, nom, ui_config")
            .in_("id", list(resume_par_agent.keys()))
            .execute()
        )
        agents_par_id = {a["id"]: a for a in (agents_res.data or [])}
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lister_conversations, jointure agents) : {e}")
        agents_par_id = {}

    resultat = []
    for agent_id, ligne in resume_par_agent.items():
        agent = agents_par_id.get(agent_id)
        # Agent supprimé depuis (actif=false n'est PAS filtré ici : voir
        # docstring — on veut quand même montrer l'historique d'un agent
        # désactivé, juste pas un agent qui n'existe plus du tout en base)
        # mais un id qui ne matche plus aucune ligne dans `agents` est
        # silencieusement ignoré plutôt que de planter toute la liste.
        if not agent:
            continue
        resultat.append(
            ConversationResume(
                agent_id=agent_id,
                agent_nom=agent["nom"],
                agent_icone=(agent.get("ui_config") or {}).get("icone_page", "🤖"),
                dernier_message=ligne["content"],
                dernier_message_role=ligne["role"],
                derniere_activite=ligne["created_at"],
            )
        )

    resultat.sort(key=lambda r: r.derniere_activite, reverse=True)
    return resultat


class MessageHistorique(BaseModel):
    role: str
    content: str
    created_at: str


@router.get("/{agent_id}", response_model=List[MessageHistorique])
def obtenir_historique_agent(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Historique complet (jamais purgé) des échanges entre CET utilisateur
    et CET agent, du plus ancien au plus récent -- pour rouvrir/afficher
    une conversation passée en entier depuis la barre latérale.

    Pas de vérification "cet agent existe/est actif" ici : filtrer par
    user_id suffit à garantir qu'on ne lit jamais l'historique de
    quelqu'un d'autre (aucune donnée du payload/de l'URL n'influence QUEL
    user_id est utilisé, uniquement le token vérifié par
    utilisateur_courant).
    """
    try:
        lignes = (
            supabase.table("historique_conversations")
            .select("role, content, created_at")
            .eq("user_id", utilisateur.id)
            .eq("agent_id", agent_id)
            .order("created_at")
            .execute()
        ).data or []
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (obtenir_historique_agent, user_id={utilisateur.id}, "
            f"agent_id={agent_id}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_L_HISTORIQUE")

    return [MessageHistorique(**ligne) for ligne in lignes]


# --- Fils de discussion (par conversation_id), ajouté le 2026-07-16 ------
# Bourama : reproduire dans le chat Next.js la sidebar "Historique" du chat
# Streamlit (l'ancienne interface Streamlit), qui liste les fils de discussion
# DISTINCTS avec un même agent (pas juste "un agent = une conversation",
# comme le fait lister_conversations ci-dessus pour le tableau de bord).
# Même logique de regroupement que _lister_conversations_passees côté
# Streamlit : titre = début du premier message utilisateur du fil (pas de
# titre généré par IA, décision de Bourama : trop coûteux pour ce que ça
# apporte), lignes sans conversation_id (NULL, d'avant cette fonctionnalité)
# regroupées sous un fil "legacy" plutôt qu'ignorées.
LONGUEUR_MAX_TITRE = 42


class FilConversation(BaseModel):
    conversation_id: Optional[str]
    titre: str
    derniere_activite: str


@router.get("/{agent_id}/conversations", response_model=List[FilConversation])
def lister_fils_conversation(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Liste des fils de discussion distincts entre CET utilisateur et CET
    agent, le plus récemment actif en premier -- pour la section
    "Historique" de la sidebar du chat (un agent peut avoir plusieurs
    conversations séparées, contrairement à GET /api/historique qui n'en
    garde qu'une par agent pour le tableau de bord).
    """
    try:
        lignes = (
            supabase.table("historique_conversations")
            .select("conversation_id, role, content, created_at")
            .eq("user_id", utilisateur.id)
            .eq("agent_id", agent_id)
            .order("created_at")
            .execute()
        ).data or []
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (lister_fils_conversation, user_id={utilisateur.id}, "
            f"agent_id={agent_id}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_L_HISTORIQUE")

    fils: dict = {}
    for ligne in lignes:
        cle = ligne["conversation_id"] or "legacy"
        if cle not in fils:
            fils[cle] = {
                "conversation_id": ligne["conversation_id"],
                "premier_message_user": None,
                "derniere_activite": ligne["created_at"],
            }
        if ligne["role"] == "user" and fils[cle]["premier_message_user"] is None:
            fils[cle]["premier_message_user"] = ligne["content"]
        fils[cle]["derniere_activite"] = ligne["created_at"]

    resultat = []
    for cle, fil in fils.items():
        if cle == "legacy":
            titre = "Avant l'historique par conversation"
        else:
            titre = (fil["premier_message_user"] or "Conversation sans titre").strip()
            if len(titre) > LONGUEUR_MAX_TITRE:
                titre = titre[:LONGUEUR_MAX_TITRE].rstrip() + "…"
        resultat.append(
            FilConversation(
                conversation_id=fil["conversation_id"],
                titre=titre,
                derniere_activite=fil["derniere_activite"],
            )
        )

    # Titres choisis par l'utilisateur (renommage), meme source que /fils.
    try:
        titres_perso = {
            m["cle"]: m["titre_perso"]
            for m in (
                supabase.table("historique_fils_meta")
                .select("cle, titre_perso")
                .eq("user_id", utilisateur.id)
                .eq("agent_id", agent_id)
                .not_.is_("titre_perso", "null")
                .execute()
            ).data
            or []
        }
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lister_fils_conversation, titres perso) : {e}")
        titres_perso = {}
    for fil in resultat:
        titre_perso = titres_perso.get(fil.conversation_id or "legacy")
        if titre_perso:
            fil.titre = titre_perso

    resultat.sort(key=lambda f: f.derniere_activite, reverse=True)
    return resultat


@router.get("/{agent_id}/conversations/{conversation_id}", response_model=List[MessageHistorique])
def obtenir_fil_conversation(agent_id: str, conversation_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Contenu complet d'UN fil précis (clic sur une entrée de la liste
    ci-dessus). `conversation_id` vaut littéralement "legacy" pour recharger
    le fil des lignes d'avant cette fonctionnalité (conversation_id NULL en
    base) -- convention interne à cette route, jamais stockée telle quelle.
    """
    try:
        requete = (
            supabase.table("historique_conversations")
            .select("role, content, created_at")
            .eq("user_id", utilisateur.id)
            .eq("agent_id", agent_id)
        )
        if conversation_id == "legacy":
            requete = requete.is_("conversation_id", "null")
        else:
            requete = requete.eq("conversation_id", conversation_id)
        lignes = requete.order("created_at").execute().data or []
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (obtenir_fil_conversation, user_id={utilisateur.id}, "
            f"agent_id={agent_id}, conversation_id={conversation_id}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_CETTE_CONVERSATION")

    return [MessageHistorique(**ligne) for ligne in lignes]


# ---------------------------------------------------------------------------
# Historique du chat : liste PAGINEE, epingler, renommer, supprimer
# (demande Bourama, 01/10/2026). GET /{agent_id}/conversations ci-dessus
# relit tous les messages et renvoie tous les fils d'un coup ; ces routes-ci
# s'appuient sur la fonction SQL lister_fils_historique (voir la migration
# 2026_10_01_historique_fils_epingles.sql) et ne renvoient qu'un paquet a la
# fois. Un fil est designe par sa `cle` : son conversation_id, ou "legacy"
# pour les lignes d'avant l'historique par conversation.
# ---------------------------------------------------------------------------

TAILLE_PAGE_PAR_DEFAUT = 20
TAILLE_PAGE_MAX = 50
LONGUEUR_MAX_TITRE_PERSO = 80


class FilPage(BaseModel):
    conversation_id: Optional[str]
    cle: str
    titre: str
    derniere_activite: str
    epingle: bool = False


class CurseurFils(BaseModel):
    avant_activite: str
    avant_cle: str


class PageFils(BaseModel):
    # Rempli uniquement sur la premiere page (sans curseur) : les fils
    # epingles sont peu nombreux et toujours affiches en haut.
    epingles: List[FilPage]
    fils: List[FilPage]
    # None quand il n'y a plus rien a charger.
    suivant: Optional[CurseurFils] = None


class ModificationFil(BaseModel):
    epingle: Optional[bool] = None
    titre: Optional[str] = None


def _fil_page_depuis_ligne(ligne: dict) -> FilPage:
    cle = ligne["cle"]
    titre = (ligne.get("titre_perso") or "").strip()
    if not titre:
        if cle == "legacy":
            titre = "Avant l'historique par conversation"
        else:
            titre = (ligne.get("premier_message") or "Conversation sans titre").strip()
            if len(titre) > LONGUEUR_MAX_TITRE:
                titre = titre[:LONGUEUR_MAX_TITRE].rstrip() + "…"
    return FilPage(
        conversation_id=ligne.get("conversation_id"),
        cle=cle,
        titre=titre,
        derniere_activite=ligne["derniere_activite"],
        epingle=bool(ligne.get("epingle_le")),
    )


def _verifier_cle_fil(cle: str) -> None:
    if cle == "legacy":
        return
    try:
        uuid.UUID(cle)
    except ValueError:
        raise erreur_api(404, "FIL_INTROUVABLE")


def _filtrer_fil(requete, cle: str):
    if cle == "legacy":
        return requete.is_("conversation_id", "null")
    return requete.eq("conversation_id", cle)


@router.get("/{agent_id}/fils", response_model=PageFils)
def lister_fils_pagines(
    agent_id: str,
    limite: int = Query(TAILLE_PAGE_PAR_DEFAUT, ge=1, le=TAILLE_PAGE_MAX),
    avant_activite: Optional[str] = None,
    avant_cle: Optional[str] = None,
    utilisateur=Depends(utilisateur_courant),
):
    """
    Un paquet de `limite` fils non epingles, du plus recemment actif au plus
    ancien. Sans curseur (premiere page), renvoie aussi tous les fils
    epingles. Pour la page suivante, renvoyer `suivant` tel quel
    (avant_activite + avant_cle).
    """
    if (avant_activite is None) != (avant_cle is None):
        raise erreur_api(400, "IMPOSSIBLE_DE_CHARGER_L_HISTORIQUE")
    premiere_page = avant_activite is None
    try:
        epingles = []
        if premiere_page:
            epingles = (
                supabase.rpc(
                    "lister_fils_historique",
                    {"p_user": utilisateur.id, "p_agent": agent_id, "p_epingles": True},
                ).execute()
            ).data or []
        # On demande un fil de plus que la page : s'il revient, il reste de
        # quoi charger ; il n'est jamais renvoye au client.
        parametres = {
            "p_user": utilisateur.id,
            "p_agent": agent_id,
            "p_epingles": False,
            "p_limite": limite + 1,
        }
        if not premiere_page:
            parametres["p_avant_activite"] = avant_activite
            parametres["p_avant_cle"] = avant_cle
        lignes = (supabase.rpc("lister_fils_historique", parametres).execute()).data or []
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (lister_fils_pagines, user_id={utilisateur.id}, "
            f"agent_id={agent_id}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_DE_CHARGER_L_HISTORIQUE")

    reste = len(lignes) > limite
    page = lignes[:limite]
    suivant = None
    if reste and page:
        derniere = page[-1]
        suivant = CurseurFils(avant_activite=derniere["derniere_activite"], avant_cle=derniere["cle"])
    return PageFils(
        epingles=[_fil_page_depuis_ligne(ligne) for ligne in epingles],
        fils=[_fil_page_depuis_ligne(ligne) for ligne in page],
        suivant=suivant,
    )


@router.patch("/{agent_id}/fils/{cle}", response_model=FilPage)
def modifier_fil(
    agent_id: str,
    cle: str,
    modification: ModificationFil,
    utilisateur=Depends(utilisateur_courant),
):
    """Epingler / desepingler (`epingle`) et/ou renommer (`titre`) un fil."""
    _verifier_cle_fil(cle)
    if modification.epingle is None and modification.titre is None:
        raise erreur_api(400, "TITRE_FIL_INVALIDE")
    titre = None
    if modification.titre is not None:
        titre = " ".join(modification.titre.split())
        if not titre or len(titre) > LONGUEUR_MAX_TITRE_PERSO:
            raise erreur_api(400, "TITRE_FIL_INVALIDE")

    try:
        existe = (
            _filtrer_fil(
                supabase.table("historique_conversations")
                .select("id")
                .eq("user_id", utilisateur.id)
                .eq("agent_id", agent_id),
                cle,
            )
            .limit(1)
            .execute()
        ).data
        if not existe:
            raise erreur_api(404, "FIL_INTROUVABLE")

        ligne_meta = {
            "user_id": utilisateur.id,
            "agent_id": agent_id,
            "cle": cle,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if modification.epingle is not None:
            ligne_meta["epingle_le"] = (
                datetime.now(timezone.utc).isoformat() if modification.epingle else None
            )
        if titre is not None:
            ligne_meta["titre_perso"] = titre
        # Un upsert qui ne mentionne qu'une des deux colonnes laisse l'autre
        # intacte (PostgREST ne met a jour que les colonnes envoyees).
        supabase.table("historique_fils_meta").upsert(
            ligne_meta, on_conflict="user_id,agent_id,cle"
        ).execute()

        lignes = (
            supabase.rpc(
                "lister_fils_historique",
                {"p_user": utilisateur.id, "p_agent": agent_id, "p_epingles": modification.epingle is True},
            ).execute()
        ).data or []
    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (modifier_fil, user_id={utilisateur.id}, "
            f"agent_id={agent_id}, cle={cle}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_DE_MODIFIER_LE_FIL")

    # Quand le fil vient d'etre epingle, il est dans la liste des epingles ;
    # sinon on relit simplement sa ligne de metadonnees pour repondre.
    for ligne in lignes:
        if ligne["cle"] == cle:
            return _fil_page_depuis_ligne(ligne)
    return _fil_apres_modification(utilisateur.id, agent_id, cle)


def _fil_apres_modification(user_id: str, agent_id: str, cle: str) -> FilPage:
    """Relit un fil precis (cas ou il n'est pas dans la liste des epingles)."""
    meta = (
        supabase.table("historique_fils_meta")
        .select("epingle_le, titre_perso")
        .eq("user_id", user_id)
        .eq("agent_id", agent_id)
        .eq("cle", cle)
        .limit(1)
        .execute()
    ).data or [{}]
    lignes = _filtrer_fil(
        supabase.table("historique_conversations")
        .select("role, content, created_at")
        .eq("user_id", user_id)
        .eq("agent_id", agent_id),
        cle,
    ).order("created_at").execute().data or []
    premier = next((l["content"] for l in lignes if l["role"] == "user"), None)
    return _fil_page_depuis_ligne(
        {
            "conversation_id": None if cle == "legacy" else cle,
            "cle": cle,
            "titre_perso": meta[0].get("titre_perso"),
            "premier_message": premier,
            "derniere_activite": lignes[-1]["created_at"] if lignes else datetime.now(timezone.utc).isoformat(),
            "epingle_le": meta[0].get("epingle_le"),
        }
    )


@router.delete("/{agent_id}/fils/{cle}")
def supprimer_fil(agent_id: str, cle: str, utilisateur=Depends(utilisateur_courant)):
    """
    Supprime DEFINITIVEMENT un fil : tous ses messages (les retours
    feedback_messages liés partent avec, ON DELETE CASCADE), les reponses
    de QCM de ce fil et sa ligne de metadonnees. Irreversible.
    Les signalements aux enseignants (table signalements) ne sont PAS
    touches : ils servent a la supervision pedagogique.
    """
    _verifier_cle_fil(cle)
    try:
        supprimes = (
            _filtrer_fil(
                supabase.table("historique_conversations")
                .delete()
                .eq("user_id", utilisateur.id)
                .eq("agent_id", agent_id),
                cle,
            ).execute()
        ).data or []
        if not supprimes:
            raise erreur_api(404, "FIL_INTROUVABLE")
        if cle != "legacy":
            supabase.table("historique_reponses_qcm").delete().eq(
                "user_id", utilisateur.id
            ).eq("conversation_id", cle).execute()
        supabase.table("historique_fils_meta").delete().eq("user_id", utilisateur.id).eq(
            "agent_id", agent_id
        ).eq("cle", cle).execute()
    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (supprimer_fil, user_id={utilisateur.id}, "
            f"agent_id={agent_id}, cle={cle}) : {e}"
        )
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_LE_FIL")
    return {"ok": True, "messages_supprimes": len(supprimes)}

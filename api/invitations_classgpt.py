"""
Invitations Class GPT (2026-08-08, partie 4 du brief Class GPT).

Remplace le menu déroulant de POST /api/roles/choisir (api/roles.py) par
un code à partager -- même esprit que api/contenu_dynamique_matiere.py
(génération de code, alphabet sans caractères ambigus), réutilisé ici
pour rattacher un compte à un établissement/enseignant plutôt que pour
débloquer un contenu de matière.

Écrit un profil de la même forme que choisir_role() (profiles.role/
etablissement_id/enseignant_id) : les deux chemins restent compatibles,
/api/roles/moi fonctionne à l'identique quel que soit celui utilisé.
"""

import logging
import os
import secrets
import sys
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from api.journal import journaliser
from api.permissions_hierarchie import _lire_profil_role
from api.roles import AGENT_PAR_ROLE
from core.erreurs import erreur_api

# Même sys.path que api/roles.py -- generer_id_depuis_nom vit dans
# core/creation_agent.py mais s'importe par son nom de module seul
# (core/ ajouté à sys.path, pas le repo entier). Correctif (08/08) :
# la version livrée faisait `from creation_agent import ...` sans ce
# sys.path.append -- fonctionnait par accident seulement si un autre
# fichier (api/agents.py, api/roles.py...) avait déjà fait cet append
# plus tôt dans l'ordre d'import, sinon ImportError au premier appel.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "core"))

logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/api/roles", tags=["invitations_classgpt"])

# Même alphabet que contenu_dynamique_matiere.py (pas de 0/O/1/I/L) --
# un code pensé pour être recopié à la main sans ambiguïté.
_ALPHABET_CODE = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_LONGUEUR_CODE = 6
_TENTATIVES_MAX_CODE = 10

ROLE_CIBLE_PAR_PROPRIETAIRE = {
    "etablissement": "enseignant",
    "enseignant": "etudiant",
}


def _generer_code_unique() -> str:
    for _ in range(_TENTATIVES_MAX_CODE):
        code = "".join(secrets.choice(_ALPHABET_CODE) for _ in range(_LONGUEUR_CODE))
        try:
            existe = (
                supabase.table("invitations_classgpt").select("id").eq("code", code).maybe_single().execute()
            )
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (vérification unicité code invitation) : {e}")
            continue
        if not existe or not existe.data:
            return code
    raise erreur_api(500, "ERREUR_INCONNUE")


class Invitation(BaseModel):
    code: str
    role_cible: Literal["enseignant", "etudiant"]
    utilisations: int


@router.get("/invitation", response_model=Invitation)
def lire_mon_invitation(utilisateur=Depends(utilisateur_courant)):
    """
    Mon code actif actuel, pour l'afficher dans l'écran "Inviter" sans
    en générer un nouveau à chaque ouverture de la page.
    """
    profil = _lire_profil_role(utilisateur.id)
    if not profil or profil.get("role") not in ("etablissement", "enseignant"):
        raise erreur_api(403, "ACTION_RESERVEE_A_CE_ROLE")

    try:
        res = (
            supabase.table("invitations_classgpt")
            .select("code, role_cible, utilisations")
            .eq("proprietaire_id", utilisateur.id)
            .eq("actif", True)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture invitation {utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    if not res or not res.data:
        raise erreur_api(404, "AUCUNE_INVITATION_ACTIVE")
    return Invitation(**res.data)


@router.post("/invitation", response_model=Invitation, status_code=201)
def generer_invitation(request: Request, utilisateur=Depends(utilisateur_courant)):
    """
    Génère mon code -- réservé établissement/enseignant (un étudiant
    n'invite personne en dessous de lui). Régénérer désactive l'ancien
    code (il arrête de fonctionner) plutôt que d'en garder plusieurs
    actifs en même temps -- plus simple à comprendre pour Bourama que
    "lequel de mes codes est le bon".
    """
    profil = _lire_profil_role(utilisateur.id)
    if not profil or profil.get("role") not in ("etablissement", "enseignant"):
        raise erreur_api(403, "ACTION_RESERVEE_A_CE_ROLE")

    role_cible = ROLE_CIBLE_PAR_PROPRIETAIRE[profil["role"]]
    code = _generer_code_unique()

    try:
        supabase.table("invitations_classgpt").update({"actif": False}).eq(
            "proprietaire_id", utilisateur.id
        ).eq("actif", True).execute()
        supabase.table("invitations_classgpt").insert(
            {
                "proprietaire_id": utilisateur.id,
                "role_proprietaire": profil["role"],
                "role_cible": role_cible,
                "code": code,
            }
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (génération invitation {utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    journaliser(
        action="invitation.generee",
        user_id=utilisateur.id,
        cible_type="invitation_classgpt",
        cible_id=None,
        details={"role_cible": role_cible},
        request=request,
    )
    return Invitation(code=code, role_cible=role_cible, utilisations=0)


class RejoindreParCodePayload(BaseModel):
    code: str
    nom_affiche: str


class RejoindreReponse(BaseModel):
    role: str
    agent_id: str


@router.post("/rejoindre", response_model=RejoindreReponse, status_code=201)
def rejoindre_par_code(
    payload: RejoindreParCodePayload, request: Request, utilisateur=Depends(utilisateur_courant)
):
    """
    Rattache le compte connecté au propriétaire d'un code -- remplace
    POST /api/roles/choisir pour Class GPT (plus de menu déroulant), même
    règle "une seule fois" : un compte qui a déjà un rôle ne peut pas en
    reprendre un autre par ce chemin non plus.
    """
    profil_existant = _lire_profil_role(utilisateur.id)
    if profil_existant and profil_existant.get("role"):
        raise erreur_api(409, "ROLE_DEJA_CHOISI")

    code_normalise = payload.code.strip().upper()
    if not code_normalise:
        raise erreur_api(400, "CODE_MANQUANT")
    if not payload.nom_affiche.strip():
        raise erreur_api(400, "NOM_AFFICHE_MANQUANT")

    try:
        invitation = (
            supabase.table("invitations_classgpt")
            .select("id, proprietaire_id, role_cible, utilisations")
            .eq("code", code_normalise)
            .eq("actif", True)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification code invitation {code_normalise}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    if not invitation or not invitation.data:
        # Code distinct de CODE_INVALIDE (déjà utilisé par
        # contenu_dynamique_matiere.py avec un message parlant de
        # "contenu", pas d'invitation) -- éviter de réutiliser un message
        # trompeur pour un cas différent.
        raise erreur_api(404, "CODE_INVITATION_INVALIDE")

    donnees_invitation = invitation.data
    role_cible = donnees_invitation["role_cible"]
    proprietaire_id = donnees_invitation["proprietaire_id"]

    ligne_maj = {"role": role_cible, "nom_affiche": payload.nom_affiche.strip()}
    if role_cible == "enseignant":
        ligne_maj["etablissement_id"] = proprietaire_id
    elif role_cible == "etudiant":
        ligne_maj["enseignant_id"] = proprietaire_id

    try:
        deja = supabase.table("profiles").select("slug").eq("user_id", utilisateur.id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification profil existant {utilisateur.id}) : {e}")
        deja = None

    try:
        if deja and deja.data:
            supabase.table("profiles").update(ligne_maj).eq("user_id", utilisateur.id).execute()
        else:
            from creation_agent import generer_id_depuis_nom

            ligne_maj["user_id"] = utilisateur.id
            ligne_maj["slug"] = generer_id_depuis_nom(utilisateur.id[:8]) or utilisateur.id[:8]
            supabase.table("profiles").insert(ligne_maj).execute()

        supabase.table("invitations_classgpt").update({"utilisations": donnees_invitation["utilisations"] + 1}).eq(
            "id", donnees_invitation["id"]
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (rattachement par code {utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    journaliser(
        action="invitation.utilisee",
        user_id=utilisateur.id,
        cible_type="invitation_classgpt",
        cible_id=donnees_invitation["id"],
        details={"role_cible": role_cible, "proprietaire_id": proprietaire_id},
        request=request,
    )
    return RejoindreReponse(role=role_cible, agent_id=AGENT_PAR_ROLE[role_cible])


class RootPayload(BaseModel):
    nom_affiche: str


@router.post("/etablissement-racine", response_model=RejoindreReponse, status_code=201)
def creer_etudiant_autonome(payload: RootPayload, request: Request, utilisateur=Depends(utilisateur_courant)):
    """
    Inscription libre sur Class GPT (sans code d'invitation) : devient
    "etudiant" directement, sans rattachement à un enseignant
    (enseignant_id reste NULL) -- décision Bourama du 09/08, remplace le
    comportement d'origine (rôle "etablissement" par défaut). Le nom de
    la route (/etablissement-racine) et de l'action journalisée restent
    inchangés pour ne pas casser de compatibilité ni retoucher inutilement
    lib/invitations.ts côté frontend -- seul le rôle attribué change.

    Point qui n'était pas dans le brief d'origine, posé explicitement
    plutôt que deviné (voir échange avec Bourama) : un compte "etudiant"
    sans enseignant_id est un cas nouveau, jusqu'ici un étudiant était
    toujours rattaché via /rejoindre (code reçu d'un enseignant/
    établissement). Aucune fonctionnalité existante (mon-equipe,
    diffusion...) ne suppose enseignant_id non-NULL côté étudiant --
    vérifié dans api/roles.py avant ce changement.
    """
    profil_existant = _lire_profil_role(utilisateur.id)
    if profil_existant and profil_existant.get("role"):
        raise erreur_api(409, "ROLE_DEJA_CHOISI")

    if not payload.nom_affiche.strip():
        raise erreur_api(400, "NOM_AFFICHE_MANQUANT")

    ligne_maj = {"role": "etudiant", "nom_affiche": payload.nom_affiche.strip()}

    try:
        deja = supabase.table("profiles").select("slug").eq("user_id", utilisateur.id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification profil existant {utilisateur.id}) : {e}")
        deja = None

    try:
        if deja and deja.data:
            supabase.table("profiles").update(ligne_maj).eq("user_id", utilisateur.id).execute()
        else:
            from creation_agent import generer_id_depuis_nom

            ligne_maj["user_id"] = utilisateur.id
            ligne_maj["slug"] = generer_id_depuis_nom(utilisateur.id[:8]) or utilisateur.id[:8]
            supabase.table("profiles").insert(ligne_maj).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création étudiant autonome {utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    journaliser(
        action="etudiant_autonome.cree",
        user_id=utilisateur.id,
        cible_type="profile",
        cible_id=utilisateur.id,
        details={},
        request=request,
    )
    return RejoindreReponse(role="etudiant", agent_id=AGENT_PAR_ROLE["etudiant"])

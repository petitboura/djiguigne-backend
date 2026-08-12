"""
Structure programme (classe -> matière -> chapitre), 2026-08-12, demande
Bourama -- chantier "programme adaptatif étudiant", lot 1/5.

Voir migrations/2026_08_12_programme_structure.sql pour le schéma. Chaque
route vérifie que l'appelant est bien propriétaire de la ressource
(directement pour un programme, en remontant jusqu'au programme parent
pour matière/chapitre) -- jamais de lecture/écriture croisée entre
comptes ici (la lecture "publique" d'un programme via un plugin appartient
au lot 3, pas à ce fichier).
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from core.erreurs import erreur_api

logging.basicConfig(level=logging.INFO)

router_programmes = APIRouter(prefix="/api/programmes", tags=["programmes"])
router_matieres = APIRouter(prefix="/api/matieres", tags=["programmes"])
router_chapitres = APIRouter(prefix="/api/chapitres", tags=["programmes"])


# ============================== Programmes ==============================

class ProgrammePayload(BaseModel):
    niveau: str
    nom: str | None = None


class ProgrammePatchPayload(BaseModel):
    niveau: str | None = None
    nom: str | None = None


class Programme(BaseModel):
    id: str
    niveau: str
    nom: str | None = None
    created_at: str
    updated_at: str


def _charger_programme_ou_404(programme_id: str, utilisateur_id: str) -> dict:
    """Charge un programme en vérifiant la propriété, sinon 404 (jamais de
    403 ici : on ne révèle pas qu'un programme existe chez quelqu'un d'autre)."""
    try:
        res = (
            supabase.table("programmes")
            .select("*")
            .eq("id", programme_id)
            .eq("proprietaire_id", utilisateur_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not res or not res.data:
        raise erreur_api(404, "PROGRAMME_INTROUVABLE")
    return res.data


@router_programmes.get("", response_model=list[Programme])
def lister_mes_programmes(utilisateur=Depends(utilisateur_courant)):
    try:
        res = (
            supabase.table("programmes")
            .select("*")
            .eq("proprietaire_id", utilisateur.id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste programmes {utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return [Programme(**ligne) for ligne in (res.data or [])]


@router_programmes.post("", response_model=Programme, status_code=201)
def creer_programme(payload: ProgrammePayload, utilisateur=Depends(utilisateur_courant)):
    niveau = payload.niveau.strip()
    if not niveau:
        raise erreur_api(400, "NIVEAU_REQUIS")
    try:
        res = (
            supabase.table("programmes")
            .insert({"proprietaire_id": utilisateur.id, "niveau": niveau, "nom": (payload.nom or "").strip() or None})
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création programme {utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return Programme(**res.data[0])


@router_programmes.get("/{programme_id}")
def lire_programme(programme_id: str, utilisateur=Depends(utilisateur_courant)):
    """Détail d'un programme avec ses matières imbriquées (chaque matière
    sans ses chapitres -- utiliser GET /api/matieres/{id}/chapitres à part,
    pour ne pas tout charger d'un coup sur un programme volumineux)."""
    programme = _charger_programme_ou_404(programme_id, utilisateur.id)
    try:
        matieres = (
            supabase.table("matieres")
            .select("*")
            .eq("programme_id", programme_id)
            .order("created_at")
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (matières du programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return {**programme, "matieres": matieres.data or []}


@router_programmes.patch("/{programme_id}", response_model=Programme)
def modifier_programme(programme_id: str, payload: ProgrammePatchPayload, utilisateur=Depends(utilisateur_courant)):
    _charger_programme_ou_404(programme_id, utilisateur.id)
    maj = {}
    if payload.niveau is not None:
        niveau = payload.niveau.strip()
        if not niveau:
            raise erreur_api(400, "NIVEAU_REQUIS")
        maj["niveau"] = niveau
    if payload.nom is not None:
        maj["nom"] = payload.nom.strip() or None
    if not maj:
        raise erreur_api(400, "AUCUNE_MODIFICATION_FOURNIE")
    maj["updated_at"] = "now()"
    try:
        res = supabase.table("programmes").update(maj).eq("id", programme_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (modification programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return Programme(**res.data[0])


@router_programmes.delete("/{programme_id}", status_code=204)
def supprimer_programme(programme_id: str, utilisateur=Depends(utilisateur_courant)):
    _charger_programme_ou_404(programme_id, utilisateur.id)
    try:
        supabase.table("programmes").delete().eq("id", programme_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")


# =============================== Matières ================================

class MatierePayload(BaseModel):
    nom: str
    limites: str | None = None


class MatierePatchPayload(BaseModel):
    nom: str | None = None
    limites: str | None = None


class Matiere(BaseModel):
    id: str
    nom: str
    limites: str | None = None
    created_at: str
    updated_at: str


def _charger_matiere_avec_programme_ou_404(matiere_id: str, utilisateur_id: str) -> dict:
    """Charge une matière ET vérifie que son programme parent appartient
    bien à l'appelant (jointure manuelle -- pas de RLS sur ce projet, le
    filtrage se fait ici, voir convention du reste du backend)."""
    try:
        res = (
            supabase.table("matieres")
            .select("*, programmes!inner(proprietaire_id)")
            .eq("id", matiere_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture matière {matiere_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not res or not res.data or res.data.get("programmes", {}).get("proprietaire_id") != utilisateur_id:
        raise erreur_api(404, "MATIERE_INTROUVABLE")
    ligne = dict(res.data)
    ligne.pop("programmes", None)
    return ligne


@router_programmes.get("/{programme_id}/matieres", response_model=list[Matiere])
def lister_matieres(programme_id: str, utilisateur=Depends(utilisateur_courant)):
    _charger_programme_ou_404(programme_id, utilisateur.id)
    try:
        res = (
            supabase.table("matieres")
            .select("*")
            .eq("programme_id", programme_id)
            .order("created_at")
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste matières {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return [Matiere(**ligne) for ligne in (res.data or [])]


@router_programmes.post("/{programme_id}/matieres", response_model=Matiere, status_code=201)
def creer_matiere(programme_id: str, payload: MatierePayload, utilisateur=Depends(utilisateur_courant)):
    _charger_programme_ou_404(programme_id, utilisateur.id)
    nom = payload.nom.strip()
    if not nom:
        raise erreur_api(400, "NOM_REQUIS")
    try:
        res = (
            supabase.table("matieres")
            .insert({"programme_id": programme_id, "nom": nom, "limites": (payload.limites or "").strip() or None})
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création matière {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return Matiere(**res.data[0])


@router_matieres.patch("/{matiere_id}", response_model=Matiere)
def modifier_matiere(matiere_id: str, payload: MatierePatchPayload, utilisateur=Depends(utilisateur_courant)):
    _charger_matiere_avec_programme_ou_404(matiere_id, utilisateur.id)
    maj = {}
    if payload.nom is not None:
        nom = payload.nom.strip()
        if not nom:
            raise erreur_api(400, "NOM_REQUIS")
        maj["nom"] = nom
    if payload.limites is not None:
        maj["limites"] = payload.limites.strip() or None
    if not maj:
        raise erreur_api(400, "AUCUNE_MODIFICATION_FOURNIE")
    maj["updated_at"] = "now()"
    try:
        res = supabase.table("matieres").update(maj).eq("id", matiere_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (modification matière {matiere_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return Matiere(**res.data[0])


@router_matieres.delete("/{matiere_id}", status_code=204)
def supprimer_matiere(matiere_id: str, utilisateur=Depends(utilisateur_courant)):
    _charger_matiere_avec_programme_ou_404(matiere_id, utilisateur.id)
    try:
        supabase.table("matieres").delete().eq("id", matiere_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression matière {matiere_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")


# =============================== Chapitres ================================

class ChapitrePayload(BaseModel):
    nom: str
    ordre: int | None = None
    limites: str | None = None


class ChapitrePatchPayload(BaseModel):
    nom: str | None = None
    ordre: int | None = None
    limites: str | None = None


class Chapitre(BaseModel):
    id: str
    nom: str
    ordre: int
    limites: str | None = None
    created_at: str
    updated_at: str


def _charger_chapitre_avec_programme_ou_404(chapitre_id: str, utilisateur_id: str) -> dict:
    """Même principe que _charger_matiere_avec_programme_ou_404, une jointure
    plus loin (chapitre -> matière -> programme)."""
    try:
        res = (
            supabase.table("chapitres")
            .select("*, matieres!inner(programme_id, programmes!inner(proprietaire_id))")
            .eq("id", chapitre_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not res or not res.data:
        raise erreur_api(404, "CHAPITRE_INTROUVABLE")
    proprietaire = (res.data.get("matieres") or {}).get("programmes", {}).get("proprietaire_id")
    if proprietaire != utilisateur_id:
        raise erreur_api(404, "CHAPITRE_INTROUVABLE")
    ligne = dict(res.data)
    ligne.pop("matieres", None)
    return ligne


@router_matieres.get("/{matiere_id}/chapitres", response_model=list[Chapitre])
def lister_chapitres(matiere_id: str, utilisateur=Depends(utilisateur_courant)):
    _charger_matiere_avec_programme_ou_404(matiere_id, utilisateur.id)
    try:
        res = (
            supabase.table("chapitres")
            .select("*")
            .eq("matiere_id", matiere_id)
            .order("ordre")
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste chapitres {matiere_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return [Chapitre(**ligne) for ligne in (res.data or [])]


@router_matieres.post("/{matiere_id}/chapitres", response_model=Chapitre, status_code=201)
def creer_chapitre(matiere_id: str, payload: ChapitrePayload, utilisateur=Depends(utilisateur_courant)):
    _charger_matiere_avec_programme_ou_404(matiere_id, utilisateur.id)
    nom = payload.nom.strip()
    if not nom:
        raise erreur_api(400, "NOM_REQUIS")
    try:
        res = (
            supabase.table("chapitres")
            .insert(
                {
                    "matiere_id": matiere_id,
                    "nom": nom,
                    "ordre": payload.ordre if payload.ordre is not None else 0,
                    "limites": (payload.limites or "").strip() or None,
                }
            )
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création chapitre {matiere_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return Chapitre(**res.data[0])


@router_chapitres.patch("/{chapitre_id}", response_model=Chapitre)
def modifier_chapitre(chapitre_id: str, payload: ChapitrePatchPayload, utilisateur=Depends(utilisateur_courant)):
    _charger_chapitre_avec_programme_ou_404(chapitre_id, utilisateur.id)
    maj = {}
    if payload.nom is not None:
        nom = payload.nom.strip()
        if not nom:
            raise erreur_api(400, "NOM_REQUIS")
        maj["nom"] = nom
    if payload.ordre is not None:
        maj["ordre"] = payload.ordre
    if payload.limites is not None:
        maj["limites"] = payload.limites.strip() or None
    if not maj:
        raise erreur_api(400, "AUCUNE_MODIFICATION_FOURNIE")
    maj["updated_at"] = "now()"
    try:
        res = supabase.table("chapitres").update(maj).eq("id", chapitre_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (modification chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return Chapitre(**res.data[0])


@router_chapitres.delete("/{chapitre_id}", status_code=204)
def supprimer_chapitre(chapitre_id: str, utilisateur=Depends(utilisateur_courant)):
    _charger_chapitre_avec_programme_ou_404(chapitre_id, utilisateur.id)
    try:
        supabase.table("chapitres").delete().eq("id", chapitre_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

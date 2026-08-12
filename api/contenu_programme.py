"""
Contenu pratique du programme étudiant (lot 2/5 du chantier "nouveau
produit étudiant" -- voir chantier-programme-etudiant.md, partie 1) :
documents/exercices rattachés à un seul chapitre, examens/devoirs/
problèmes composites rattachés à plusieurs chapitres (many-to-many), et
classement transversal (semestre/année/section libre) superposé au
squelette officiel classe->matière->chapitre.

Dépend des tables `programmes`/`matieres`/`chapitres` créées par le
lot 1 (NON recréées ici). Voir migrations/2026_08_12_contenu_pratique_programme.sql
pour le schéma des tables propres à ce lot.

Sécurité : même convention que le reste du projet (pas de RLS, filtrage
applicatif -- voir core/erreurs.py, api/roles.py). Chaque route remonte
jusqu'au `proprietaire_id` du programme parent avant lecture/écriture :
404 si l'entité n'existe pas, 403 si elle existe mais n'appartient pas à
l'utilisateur connecté (même distinction que api/agents.py).

`classement_transversal_items.cible_id` est une référence polymorphe
(pas de vraie foreign key SQL sur plusieurs tables à la fois -- confirmé
avec Bourama le 12/08, cohérent avec l'absence de RLS du reste du
projet). Le nettoyage des lignes orphelines quand un document/exercice/
examen est supprimé est donc fait explicitement ici, côté API.
"""

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from api.journal import journaliser
from core.erreurs import erreur_api

logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/api", tags=["contenu-programme"])

TYPES_EXAMEN = ("examen", "devoir", "probleme_composite")
TYPES_CLASSEMENT = ("semestre", "annee", "section")
TYPES_CIBLE_CLASSEMENT = ("matiere", "chapitre", "document", "exercice", "examen")


# ---------------------------------------------------------------------------
# Lecture brute + vérifications de propriété -- remontent la chaîne jusqu'au
# propriétaire du programme parent. Convention commune à toutes les routes
# ci-dessous. Style volontairement en requêtes séquentielles simples (pas
# d'embedding PostgREST), pour rester cohérent avec le reste du dépôt (voir
# _etablissement_de_etudiant dans api/roles.py).
# ---------------------------------------------------------------------------


def _lire_chapitre(chapitre_id: str) -> Optional[dict]:
    try:
        res = supabase.table("chapitres").select("id, matiere_id").eq("id", chapitre_id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return res.data if res else None


def _lire_matiere(matiere_id: str) -> Optional[dict]:
    try:
        res = supabase.table("matieres").select("id, programme_id").eq("id", matiere_id).maybe_single().execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture matière {matiere_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return res.data if res else None


def _lire_programme(programme_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("programmes")
            .select("id, proprietaire_id")
            .eq("id", programme_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return res.data if res else None


def _lire_document(document_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("documents_programme")
            .select("id, chapitre_id, titre, url_ou_contenu, created_at")
            .eq("id", document_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture document {document_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return res.data if res else None


def _lire_exercice(exercice_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("exercices_programme")
            .select("id, chapitre_id, enonce, created_at, updated_at")
            .eq("id", exercice_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture exercice {exercice_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return res.data if res else None


def _lire_examen(examen_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("examens_programme")
            .select("id, proprietaire_id, titre, type, created_at, updated_at")
            .eq("id", examen_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture examen {examen_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return res.data if res else None


def _lire_classement(classement_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("classements_transversaux")
            .select("id, proprietaire_id, type, label, created_at")
            .eq("id", classement_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture classement {classement_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return res.data if res else None


def _proprietaire_du_chapitre(chapitre_id: str) -> Optional[str]:
    """None si le chapitre (ou sa matière, ou son programme) n'existe pas."""
    chapitre = _lire_chapitre(chapitre_id)
    if not chapitre:
        return None
    matiere = _lire_matiere(chapitre["matiere_id"])
    if not matiere:
        return None
    programme = _lire_programme(matiere["programme_id"])
    if not programme:
        return None
    return programme["proprietaire_id"]


def _verifier_acces_chapitre(chapitre_id: str, user_id: str) -> None:
    proprietaire_id = _proprietaire_du_chapitre(chapitre_id)
    if proprietaire_id is None:
        raise erreur_api(404, "CHAPITRE_INTROUVABLE")
    if proprietaire_id != user_id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CE_CHAPITRE")


def _verifier_acces_programme(programme_id: str, user_id: str) -> dict:
    programme = _lire_programme(programme_id)
    if not programme:
        raise erreur_api(404, "PROGRAMME_INTROUVABLE")
    if programme["proprietaire_id"] != user_id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CE_PROGRAMME")
    return programme


def _proprietaire_de_cible(cible_type: str, cible_id: str) -> Optional[str]:
    """Résout le propriétaire réel d'une cible de classement transversal,
    quel que soit son type -- None si la cible n'existe pas."""
    if cible_type == "chapitre":
        return _proprietaire_du_chapitre(cible_id)
    if cible_type == "matiere":
        matiere = _lire_matiere(cible_id)
        if not matiere:
            return None
        programme = _lire_programme(matiere["programme_id"])
        return programme["proprietaire_id"] if programme else None
    if cible_type == "document":
        document = _lire_document(cible_id)
        if not document:
            return None
        return _proprietaire_du_chapitre(document["chapitre_id"])
    if cible_type == "exercice":
        exercice = _lire_exercice(cible_id)
        if not exercice:
            return None
        return _proprietaire_du_chapitre(exercice["chapitre_id"])
    if cible_type == "examen":
        examen = _lire_examen(cible_id)
        return examen["proprietaire_id"] if examen else None
    return None


def _nettoyer_classements_pour_cible(cible_type: str, cible_id: str) -> None:
    """Best-effort : supprime les lignes de classement transversal qui
    pointaient vers une cible qui vient d'être supprimée. Ne doit jamais
    faire échouer la suppression principale (déjà faite à ce stade)."""
    try:
        supabase.table("classement_transversal_items").delete().eq("cible_type", cible_type).eq(
            "cible_id", cible_id
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR nettoyage classements orphelins ({cible_type}={cible_id}) : {e}")


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class DocumentPayload(BaseModel):
    titre: str
    url_ou_contenu: str


class DocumentReponse(BaseModel):
    id: str
    chapitre_id: str
    titre: str
    url_ou_contenu: str
    created_at: str


@router.get("/chapitres/{chapitre_id}/documents", response_model=List[DocumentReponse])
def lister_documents(chapitre_id: str, utilisateur=Depends(utilisateur_courant)):
    _verifier_acces_chapitre(chapitre_id, utilisateur.id)
    try:
        res = (
            supabase.table("documents_programme")
            .select("id, chapitre_id, titre, url_ou_contenu, created_at")
            .eq("chapitre_id", chapitre_id)
            .order("created_at", desc=False)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste documents chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_LISTER_LES_DOCUMENTS_POUR")
    return res.data or []


@router.post("/chapitres/{chapitre_id}/documents", response_model=DocumentReponse, status_code=201)
def creer_document(
    chapitre_id: str, payload: DocumentPayload, request: Request, utilisateur=Depends(utilisateur_courant)
):
    _verifier_acces_chapitre(chapitre_id, utilisateur.id)

    if not payload.titre.strip():
        raise erreur_api(422, "TITRE_DOCUMENT_REQUIS")
    if not payload.url_ou_contenu.strip():
        raise erreur_api(422, "CONTENU_DOCUMENT_REQUIS")

    try:
        res = (
            supabase.table("documents_programme")
            .insert(
                {
                    "chapitre_id": chapitre_id,
                    "titre": payload.titre.strip(),
                    "url_ou_contenu": payload.url_ou_contenu.strip(),
                }
            )
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création document chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_CE_DOCUMENT_POUR")

    ligne = res.data[0]
    journaliser(
        action="document_programme.cree",
        user_id=utilisateur.id,
        cible_type="document_programme",
        cible_id=ligne["id"],
        details={"chapitre_id": chapitre_id, "titre": ligne["titre"]},
        request=request,
    )
    return ligne


@router.delete("/documents/{document_id}", status_code=204)
def supprimer_document(document_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    document = _lire_document(document_id)
    if not document:
        raise erreur_api(404, "DOCUMENT_PROGRAMME_INTROUVABLE")
    _verifier_acces_chapitre(document["chapitre_id"], utilisateur.id)

    try:
        supabase.table("documents_programme").delete().eq("id", document_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression document {document_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CE_DOCUMENT_POUR")

    _nettoyer_classements_pour_cible("document", document_id)

    journaliser(
        action="document_programme.supprime",
        user_id=utilisateur.id,
        cible_type="document_programme",
        cible_id=document_id,
        details={"titre": document["titre"]},
        request=request,
    )


# ---------------------------------------------------------------------------
# Exercices
# ---------------------------------------------------------------------------


class ExercicePayload(BaseModel):
    enonce: str


class ExerciceModificationPayload(BaseModel):
    enonce: str


class ExerciceReponse(BaseModel):
    id: str
    chapitre_id: str
    enonce: str
    created_at: str
    updated_at: str


@router.get("/chapitres/{chapitre_id}/exercices", response_model=List[ExerciceReponse])
def lister_exercices(chapitre_id: str, utilisateur=Depends(utilisateur_courant)):
    _verifier_acces_chapitre(chapitre_id, utilisateur.id)
    try:
        res = (
            supabase.table("exercices_programme")
            .select("id, chapitre_id, enonce, created_at, updated_at")
            .eq("chapitre_id", chapitre_id)
            .order("created_at", desc=False)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste exercices chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_LISTER_LES_EXERCICES_POUR")
    return res.data or []


@router.post("/chapitres/{chapitre_id}/exercices", response_model=ExerciceReponse, status_code=201)
def creer_exercice(
    chapitre_id: str, payload: ExercicePayload, request: Request, utilisateur=Depends(utilisateur_courant)
):
    _verifier_acces_chapitre(chapitre_id, utilisateur.id)

    if not payload.enonce.strip():
        raise erreur_api(422, "ENONCE_REQUIS")

    try:
        res = (
            supabase.table("exercices_programme")
            .insert({"chapitre_id": chapitre_id, "enonce": payload.enonce.strip()})
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création exercice chapitre {chapitre_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_CET_EXERCICE_POUR")

    ligne = res.data[0]
    journaliser(
        action="exercice_programme.cree",
        user_id=utilisateur.id,
        cible_type="exercice_programme",
        cible_id=ligne["id"],
        details={"chapitre_id": chapitre_id},
        request=request,
    )
    return ligne


@router.patch("/exercices/{exercice_id}", response_model=ExerciceReponse)
def modifier_exercice(
    exercice_id: str,
    payload: ExerciceModificationPayload,
    request: Request,
    utilisateur=Depends(utilisateur_courant),
):
    exercice = _lire_exercice(exercice_id)
    if not exercice:
        raise erreur_api(404, "EXERCICE_INTROUVABLE")
    _verifier_acces_chapitre(exercice["chapitre_id"], utilisateur.id)

    if not payload.enonce.strip():
        raise erreur_api(422, "ENONCE_REQUIS")

    try:
        res = (
            supabase.table("exercices_programme")
            .update({"enonce": payload.enonce.strip(), "updated_at": "now()"})
            .eq("id", exercice_id)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (modification exercice {exercice_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_MODIFIER_CET_EXERCICE_POUR")

    journaliser(
        action="exercice_programme.modifie",
        user_id=utilisateur.id,
        cible_type="exercice_programme",
        cible_id=exercice_id,
        details={},
        request=request,
    )
    return res.data[0]


@router.delete("/exercices/{exercice_id}", status_code=204)
def supprimer_exercice(exercice_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    exercice = _lire_exercice(exercice_id)
    if not exercice:
        raise erreur_api(404, "EXERCICE_INTROUVABLE")
    _verifier_acces_chapitre(exercice["chapitre_id"], utilisateur.id)

    try:
        supabase.table("exercices_programme").delete().eq("id", exercice_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression exercice {exercice_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CET_EXERCICE_POUR")

    _nettoyer_classements_pour_cible("exercice", exercice_id)

    journaliser(
        action="exercice_programme.supprime",
        user_id=utilisateur.id,
        cible_type="exercice_programme",
        cible_id=exercice_id,
        details={},
        request=request,
    )


# ---------------------------------------------------------------------------
# Examens / devoirs / problèmes composites
# ---------------------------------------------------------------------------


class ExamenPayload(BaseModel):
    titre: str
    type: Literal["examen", "devoir", "probleme_composite"]
    chapitre_ids: List[str]


class ExamenModificationPayload(BaseModel):
    titre: Optional[str] = None
    type: Optional[Literal["examen", "devoir", "probleme_composite"]] = None
    chapitre_ids: Optional[List[str]] = None


class ExamenReponse(BaseModel):
    id: str
    titre: str
    type: str
    chapitre_ids: List[str]
    created_at: str
    updated_at: str


def _chapitre_ids_de_examen(examen_id: str) -> List[str]:
    try:
        res = supabase.table("examen_chapitres").select("chapitre_id").eq("examen_id", examen_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture chapitres de l'examen {examen_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return [ligne["chapitre_id"] for ligne in (res.data or [])]


def _verifier_chapitres_pour_examen(chapitre_ids: List[str], user_id: str) -> None:
    if not chapitre_ids:
        raise erreur_api(422, "CHAPITRE_IDS_REQUIS")
    for chapitre_id in chapitre_ids:
        _verifier_acces_chapitre(chapitre_id, user_id)


@router.get("/programmes/{programme_id}/examens", response_model=List[ExamenReponse])
def lister_examens(programme_id: str, utilisateur=Depends(utilisateur_courant)):
    _verifier_acces_programme(programme_id, utilisateur.id)

    try:
        matieres_res = supabase.table("matieres").select("id").eq("programme_id", programme_id).execute()
        matiere_ids = [m["id"] for m in (matieres_res.data or [])]

        chapitre_ids: List[str] = []
        if matiere_ids:
            chapitres_res = supabase.table("chapitres").select("id").in_("matiere_id", matiere_ids).execute()
            chapitre_ids = [c["id"] for c in (chapitres_res.data or [])]

        examen_ids: List[str] = []
        if chapitre_ids:
            liens_res = (
                supabase.table("examen_chapitres").select("examen_id").in_("chapitre_id", chapitre_ids).execute()
            )
            examen_ids = sorted({l["examen_id"] for l in (liens_res.data or [])})

        examens: List[dict] = []
        if examen_ids:
            examens_res = (
                supabase.table("examens_programme")
                .select("id, titre, type, created_at, updated_at")
                .in_("id", examen_ids)
                .eq("proprietaire_id", utilisateur.id)
                .order("created_at", desc=False)
                .execute()
            )
            examens = examens_res.data or []
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste examens programme {programme_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_LISTER_LES_EXAMENS_POUR")

    return [{**examen, "chapitre_ids": _chapitre_ids_de_examen(examen["id"])} for examen in examens]


@router.post("/examens", response_model=ExamenReponse, status_code=201)
def creer_examen(payload: ExamenPayload, request: Request, utilisateur=Depends(utilisateur_courant)):
    if not payload.titre.strip():
        raise erreur_api(422, "TITRE_EXAMEN_REQUIS")
    _verifier_chapitres_pour_examen(payload.chapitre_ids, utilisateur.id)

    try:
        res = (
            supabase.table("examens_programme")
            .insert({"proprietaire_id": utilisateur.id, "titre": payload.titre.strip(), "type": payload.type})
            .execute()
        )
        examen = res.data[0]
        supabase.table("examen_chapitres").insert(
            [{"examen_id": examen["id"], "chapitre_id": cid} for cid in payload.chapitre_ids]
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création examen) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CREER_CET_EXAMEN_POUR")

    journaliser(
        action="examen_programme.cree",
        user_id=utilisateur.id,
        cible_type="examen_programme",
        cible_id=examen["id"],
        details={"titre": examen["titre"], "type": examen["type"], "nb_chapitres": len(payload.chapitre_ids)},
        request=request,
    )
    return {**examen, "chapitre_ids": payload.chapitre_ids}


@router.patch("/examens/{examen_id}", response_model=ExamenReponse)
def modifier_examen(
    examen_id: str, payload: ExamenModificationPayload, request: Request, utilisateur=Depends(utilisateur_courant)
):
    examen = _lire_examen(examen_id)
    if not examen:
        raise erreur_api(404, "EXAMEN_INTROUVABLE")
    if examen["proprietaire_id"] != utilisateur.id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_EXAMEN")

    if payload.titre is not None and not payload.titre.strip():
        raise erreur_api(422, "TITRE_EXAMEN_REQUIS")
    if payload.chapitre_ids is not None:
        _verifier_chapitres_pour_examen(payload.chapitre_ids, utilisateur.id)

    maj: dict = {}
    if payload.titre is not None:
        maj["titre"] = payload.titre.strip()
    if payload.type is not None:
        maj["type"] = payload.type
    if maj:
        maj["updated_at"] = "now()"

    try:
        if maj:
            supabase.table("examens_programme").update(maj).eq("id", examen_id).execute()
        if payload.chapitre_ids is not None:
            supabase.table("examen_chapitres").delete().eq("examen_id", examen_id).execute()
            supabase.table("examen_chapitres").insert(
                [{"examen_id": examen_id, "chapitre_id": cid} for cid in payload.chapitre_ids]
            ).execute()
        res = (
            supabase.table("examens_programme")
            .select("id, titre, type, created_at, updated_at")
            .eq("id", examen_id)
            .single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (modification examen {examen_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_MODIFIER_CET_EXAMEN_POUR")

    journaliser(
        action="examen_programme.modifie",
        user_id=utilisateur.id,
        cible_type="examen_programme",
        cible_id=examen_id,
        details={},
        request=request,
    )
    return {**res.data, "chapitre_ids": _chapitre_ids_de_examen(examen_id)}


@router.delete("/examens/{examen_id}", status_code=204)
def supprimer_examen(examen_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    examen = _lire_examen(examen_id)
    if not examen:
        raise erreur_api(404, "EXAMEN_INTROUVABLE")
    if examen["proprietaire_id"] != utilisateur.id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CET_EXAMEN")

    try:
        # examen_chapitres est nettoyé automatiquement (on delete cascade).
        supabase.table("examens_programme").delete().eq("id", examen_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression examen {examen_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CET_EXAMEN_POUR")

    _nettoyer_classements_pour_cible("examen", examen_id)

    journaliser(
        action="examen_programme.supprime",
        user_id=utilisateur.id,
        cible_type="examen_programme",
        cible_id=examen_id,
        details={"titre": examen["titre"]},
        request=request,
    )


# ---------------------------------------------------------------------------
# Classement transversal (semestre / année / section libre)
# ---------------------------------------------------------------------------


class ClassementPayload(BaseModel):
    type: Literal["semestre", "annee", "section"]
    label: str


class ClassementReponse(BaseModel):
    id: str
    type: str
    label: str
    created_at: str


class ClassementItemPayload(BaseModel):
    cible_type: Literal["matiere", "chapitre", "document", "exercice", "examen"]
    cible_id: str


class ClassementItemReponse(BaseModel):
    id: str
    classement_id: str
    cible_type: str
    cible_id: str
    created_at: str


@router.get("/classements", response_model=List[ClassementReponse])
def lister_classements(utilisateur=Depends(utilisateur_courant)):
    try:
        res = (
            supabase.table("classements_transversaux")
            .select("id, type, label, created_at")
            .eq("proprietaire_id", utilisateur.id)
            .order("created_at", desc=False)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste classements {utilisateur.id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_LISTER_LES_CLASSEMENTS_POUR")
    return res.data or []


@router.post("/classements", response_model=ClassementReponse, status_code=201)
def creer_classement(payload: ClassementPayload, request: Request, utilisateur=Depends(utilisateur_courant)):
    if not payload.label.strip():
        raise erreur_api(422, "LABEL_CLASSEMENT_REQUIS")

    try:
        res = (
            supabase.table("classements_transversaux")
            .insert({"proprietaire_id": utilisateur.id, "type": payload.type, "label": payload.label.strip()})
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création classement) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_CREER_CE_CLASSEMENT_POUR")

    ligne = res.data[0]
    journaliser(
        action="classement_transversal.cree",
        user_id=utilisateur.id,
        cible_type="classement_transversal",
        cible_id=ligne["id"],
        details={"type": ligne["type"], "label": ligne["label"]},
        request=request,
    )
    return ligne


@router.post("/classements/{classement_id}/items", response_model=ClassementItemReponse, status_code=201)
def ajouter_item_classement(
    classement_id: str, payload: ClassementItemPayload, request: Request, utilisateur=Depends(utilisateur_courant)
):
    classement = _lire_classement(classement_id)
    if not classement:
        raise erreur_api(404, "CLASSEMENT_INTROUVABLE")
    if classement["proprietaire_id"] != utilisateur.id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CE_CLASSEMENT")

    proprietaire_cible = _proprietaire_de_cible(payload.cible_type, payload.cible_id)
    if proprietaire_cible is None:
        raise erreur_api(404, "CIBLE_INTROUVABLE")
    if proprietaire_cible != utilisateur.id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CETTE_CIBLE")

    try:
        res = (
            supabase.table("classement_transversal_items")
            .insert(
                {"classement_id": classement_id, "cible_type": payload.cible_type, "cible_id": payload.cible_id}
            )
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (ajout item classement {classement_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_D_AJOUTER_CET_ELEMENT_AU")

    ligne = res.data[0]
    journaliser(
        action="classement_transversal.item_ajoute",
        user_id=utilisateur.id,
        cible_type="classement_transversal_item",
        cible_id=ligne["id"],
        details={"classement_id": classement_id, "cible_type": payload.cible_type, "cible_id": payload.cible_id},
        request=request,
    )
    return ligne


@router.delete("/classements/{classement_id}/items/{item_id}", status_code=204)
def supprimer_item_classement(
    classement_id: str, item_id: str, request: Request, utilisateur=Depends(utilisateur_courant)
):
    classement = _lire_classement(classement_id)
    if not classement:
        raise erreur_api(404, "CLASSEMENT_INTROUVABLE")
    if classement["proprietaire_id"] != utilisateur.id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CE_CLASSEMENT")

    try:
        item_res = (
            supabase.table("classement_transversal_items")
            .select("id, classement_id")
            .eq("id", item_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture item classement {item_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    item = item_res.data if item_res else None
    if not item or item["classement_id"] != classement_id:
        raise erreur_api(404, "CLASSEMENT_ITEM_INTROUVABLE")

    try:
        supabase.table("classement_transversal_items").delete().eq("id", item_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression item classement {item_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CET_ELEMENT_DU")

    journaliser(
        action="classement_transversal.item_supprime",
        user_id=utilisateur.id,
        cible_type="classement_transversal_item",
        cible_id=item_id,
        details={"classement_id": classement_id},
        request=request,
    )


@router.delete("/classements/{classement_id}", status_code=204)
def supprimer_classement(classement_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    classement = _lire_classement(classement_id)
    if not classement:
        raise erreur_api(404, "CLASSEMENT_INTROUVABLE")
    if classement["proprietaire_id"] != utilisateur.id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CE_CLASSEMENT")

    try:
        # classement_transversal_items est nettoyé automatiquement (on delete cascade).
        supabase.table("classements_transversaux").delete().eq("id", classement_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (suppression classement {classement_id}) : {e}")
        raise erreur_api(500, "IMPOSSIBLE_DE_SUPPRIMER_CE_CLASSEMENT_POUR")

    journaliser(
        action="classement_transversal.supprime",
        user_id=utilisateur.id,
        cible_type="classement_transversal",
        cible_id=classement_id,
        details={"label": classement["label"]},
        request=request,
    )

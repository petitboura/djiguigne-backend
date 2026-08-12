"""
Lecture des audits IA par matière (2026-08-12, chantier "connexion IA <->
structure programme"). Écriture réservée à core/audit_programme.py (boucle
planificatrice du lundi) -- ce fichier est volontairement en LECTURE SEULE,
pas de PATCH/DELETE : l'audit est réécrit en place chaque lundi par l'IA,
l'étudiant peut le consulter mais ne le modifie pas directement (voir
discussion Bourama 12/08 -- toute modification serait de toute façon
écrasée au lundi suivant).
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from core.erreurs import erreur_api

logging.basicConfig(level=logging.INFO)

router_audits_programme = APIRouter(prefix="/api/programmes", tags=["programmes"])


class AuditMatiere(BaseModel):
    matiere_id: str
    matiere_nom: str
    texte: str | None = None
    derniere_execution: str | None = None


@router_audits_programme.get("/{programme_id}/audits", response_model=list[AuditMatiere])
def lister_audits_programme(programme_id: str, utilisateur=Depends(utilisateur_courant)):
    """Un audit par matière du programme -- `texte`/`derniere_execution`
    sont None si la matière n'a encore jamais été auditée (pas encore de
    contenu à analyser, ou pas encore le premier lundi passé)."""
    try:
        programme_res = (
            supabase.table("programmes")
            .select("id")
            .eq("id", programme_id)
            .eq("proprietaire_id", utilisateur.id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification programme {programme_id} pour audits) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not programme_res or not programme_res.data:
        raise erreur_api(404, "PROGRAMME_INTROUVABLE")

    try:
        matieres = (
            supabase.table("matieres").select("id, nom").eq("programme_id", programme_id).order("created_at").execute().data
            or []
        )
        audits = (
            supabase.table("audits_matiere")
            .select("matiere_id, texte, derniere_execution")
            .eq("proprietaire_id", utilisateur.id)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture audits programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    audits_par_matiere = {a["matiere_id"]: a for a in audits}
    resultat = []
    for matiere in matieres:
        audit = audits_par_matiere.get(matiere["id"])
        resultat.append(
            AuditMatiere(
                matiere_id=matiere["id"],
                matiere_nom=matiere["nom"],
                texte=audit["texte"] if audit else None,
                derniere_execution=audit["derniere_execution"] if audit else None,
            )
        )
    return resultat

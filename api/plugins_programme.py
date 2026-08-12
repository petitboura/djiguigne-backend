"""
Système de "plugins" pour le programme étudiant (lot 3/5, 2026-08-12).
Voir chantier-programme-etudiant.md, partie 1, section "Système de
plugins" : un plugin = l'export en bloc d'un programme complet (matières,
chapitres, documents, exercices), recherchable par niveau ou par nom de
créateur, téléchargeable par un autre utilisateur qui obtient sa propre
copie modifiable -- l'original partagé n'est jamais touché.

Dépend des tables `programmes`/`matieres`/`chapitres` (lot 1) et
`documents_programme`/`exercices_programme` (lot 2), utilisées ici
uniquement en lecture pour la publication/le clone -- jamais créées ni
modifiées par ce module.

Récompense "1 an de gratuité" au plugin le plus téléchargé (voir doc
source, "Modèle économique des plugins") : ce module fournit uniquement
le compteur et le classement (GET /api/plugins/classement).
L'attribution effective de la récompense n'est PAS automatisée --
vérifié le 12/08 (grep sur abonnement/premium/facturation dans ce
dépôt) : il n'existe aucun système de facturation/abonnement réel,
seulement un déblocage premium rempli à la main en base par Bourama.
Reste donc une action manuelle de sa part à la fin de la période de
lancement, à faire via le classement retourné par cet endpoint.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from api.journal import journaliser
from core.erreurs import erreur_api

logging.basicConfig(level=logging.INFO)

router = APIRouter(prefix="/api/plugins", tags=["plugins"])
router_programmes = APIRouter(prefix="/api/programmes", tags=["plugins"])


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------

class PublierPluginPayload(BaseModel):
    nom: str


class PluginReponse(BaseModel):
    id: str
    programme_source_id: str
    auteur_id: str
    auteur_nom: Optional[str] = None
    niveau: str
    nom: str
    gratuit: bool
    telechargements_count: int
    created_at: str


class TelechargerReponse(BaseModel):
    programme_id: str


# ---------------------------------------------------------------------------
# Aide interne
# ---------------------------------------------------------------------------

def _nom_affiche_ou_repli(user_id: str) -> str:
    try:
        res = (
            supabase.table("profiles")
            .select("nom_affiche")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (nom_affiche {user_id}) : {e}")
        res = None
    return ((res.data or {}).get("nom_affiche") if res else None) or "Sans nom"


def _plugin_vers_reponse(ligne: dict, noms_par_auteur: dict) -> PluginReponse:
    return PluginReponse(
        id=ligne["id"],
        programme_source_id=ligne["programme_source_id"],
        auteur_id=ligne["auteur_id"],
        auteur_nom=noms_par_auteur.get(ligne["auteur_id"]),
        niveau=ligne["niveau"],
        nom=ligne["nom"],
        gratuit=ligne["gratuit"],
        telechargements_count=ligne["telechargements_count"],
        created_at=ligne["created_at"],
    )


def _cloner_programme(programme_source_id: str, nouveau_proprietaire_id: str, nom_copie: str) -> str:
    """
    Clone un programme complet (matières, chapitres, documents, exercices)
    en une copie indépendante appartenant à `nouveau_proprietaire_id`.
    Ne touche jamais au programme source. Retourne l'id du nouveau
    programme.

    Les tables `documents_programme`/`exercices_programme` sont du lot 2 :
    si elles n'existent pas encore côté base au moment où cette fonction
    tourne, leur lecture échoue proprement (liste vide, jamais une
    exception qui casse le clone du programme/matières/chapitres).
    """
    try:
        programme_source = (
            supabase.table("programmes")
            .select("id, niveau, nom")
            .eq("id", programme_source_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture programme source {programme_source_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    if not programme_source or not programme_source.data:
        raise erreur_api(404, "PROGRAMME_INTROUVABLE")

    niveau_source = programme_source.data["niveau"]

    try:
        nouveau_programme = (
            supabase.table("programmes")
            .insert({
                "proprietaire_id": nouveau_proprietaire_id,
                "niveau": niveau_source,
                "nom": nom_copie,
            })
            .execute()
        )
        nouveau_programme_id = nouveau_programme.data[0]["id"]

        matieres = (
            supabase.table("matieres")
            .select("id, nom, limites")
            .eq("programme_id", programme_source_id)
            .execute()
        )
        correspondance_matieres = {}
        for matiere in (matieres.data or []):
            nouvelle_matiere = (
                supabase.table("matieres")
                .insert({
                    "programme_id": nouveau_programme_id,
                    "nom": matiere["nom"],
                    "limites": matiere.get("limites"),
                })
                .execute()
            )
            correspondance_matieres[matiere["id"]] = nouvelle_matiere.data[0]["id"]

        if correspondance_matieres:
            chapitres = (
                supabase.table("chapitres")
                .select("id, matiere_id, nom, ordre, limites")
                .in_("matiere_id", list(correspondance_matieres.keys()))
                .execute()
            )
        else:
            chapitres = None

        correspondance_chapitres = {}
        for chapitre in ((chapitres.data if chapitres else None) or []):
            nouveau_chapitre = (
                supabase.table("chapitres")
                .insert({
                    "matiere_id": correspondance_matieres[chapitre["matiere_id"]],
                    "nom": chapitre["nom"],
                    "ordre": chapitre.get("ordre", 0),
                    "limites": chapitre.get("limites"),
                })
                .execute()
            )
            correspondance_chapitres[chapitre["id"]] = nouveau_chapitre.data[0]["id"]

        if correspondance_chapitres:
            # Documents et exercices (lot 2) : lecture best-effort, une
            # table encore absente ne doit jamais faire échouer le clone
            # du squelette programme/matières/chapitres ci-dessus.
            try:
                documents = (
                    supabase.table("documents_programme")
                    .select("chapitre_id, titre, url_ou_contenu")
                    .in_("chapitre_id", list(correspondance_chapitres.keys()))
                    .execute()
                )
                for doc in (documents.data or []):
                    supabase.table("documents_programme").insert({
                        "chapitre_id": correspondance_chapitres[doc["chapitre_id"]],
                        "titre": doc["titre"],
                        "url_ou_contenu": doc["url_ou_contenu"],
                    }).execute()
            except Exception as e:
                logging.error(f"ERREUR clone documents_programme (source {programme_source_id}) : {e}")

            try:
                exercices = (
                    supabase.table("exercices_programme")
                    .select("chapitre_id, enonce")
                    .in_("chapitre_id", list(correspondance_chapitres.keys()))
                    .execute()
                )
                for ex in (exercices.data or []):
                    supabase.table("exercices_programme").insert({
                        "chapitre_id": correspondance_chapitres[ex["chapitre_id"]],
                        "enonce": ex["enonce"],
                    }).execute()
            except Exception as e:
                logging.error(f"ERREUR clone exercices_programme (source {programme_source_id}) : {e}")

    except Exception as e:
        logging.error(f"ERREUR SUPABASE (clone programme {programme_source_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    return nouveau_programme_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router_programmes.post("/{programme_id}/publier-plugin", response_model=PluginReponse, status_code=201)
def publier_plugin(
    programme_id: str,
    payload: PublierPluginPayload,
    request: Request,
    utilisateur=Depends(utilisateur_courant),
):
    if not (payload.nom or "").strip():
        raise erreur_api(400, "LE_NOM_DE_L_AGENT_EST")

    try:
        programme = (
            supabase.table("programmes")
            .select("id, proprietaire_id, niveau")
            .eq("id", programme_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    if not programme or not programme.data:
        raise erreur_api(404, "PROGRAMME_INTROUVABLE")
    if programme.data["proprietaire_id"] != utilisateur.id:
        raise erreur_api(403, "PAS_LE_DROIT_SUR_CE_PROGRAMME")

    try:
        nouveau = (
            supabase.table("plugins_programme")
            .insert({
                "programme_source_id": programme_id,
                "auteur_id": utilisateur.id,
                "niveau": programme.data["niveau"],
                "nom": payload.nom.strip(),
            })
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (publication plugin, programme {programme_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    ligne = nouveau.data[0]

    journaliser(
        action="plugin.publie",
        user_id=utilisateur.id,
        cible_type="plugin_programme",
        cible_id=ligne["id"],
        details={"programme_source_id": programme_id, "nom": payload.nom.strip()},
        request=request,
    )

    return _plugin_vers_reponse(ligne, {utilisateur.id: _nom_affiche_ou_repli(utilisateur.id)})


@router.get("", response_model=List[PluginReponse])
def rechercher_plugins(
    niveau: Optional[str] = Query(default=None),
    auteur: Optional[str] = Query(default=None),
):
    """
    Recherche par niveau (correspondance exacte) et/ou par nom du
    créateur (recherche approchante sur profiles.nom_affiche). Au moins
    un des deux filtres doit être fourni.
    """
    if not (niveau or "").strip() and not (auteur or "").strip():
        raise erreur_api(400, "DONNE_AU_MOINS_UNE_DESCRIPTION_OU")

    requete = supabase.table("plugins_programme").select("*")

    if niveau and niveau.strip():
        requete = requete.eq("niveau", niveau.strip())

    if auteur and auteur.strip():
        try:
            profils = (
                supabase.table("profiles")
                .select("user_id, nom_affiche")
                .ilike("nom_affiche", f"%{auteur.strip()}%")
                .execute()
            )
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (recherche auteur '{auteur}') : {e}")
            raise erreur_api(500, "RECHERCHE_INDISPONIBLE")
        ids_auteurs = [p["user_id"] for p in (profils.data or [])]
        if not ids_auteurs:
            return []
        requete = requete.in_("auteur_id", ids_auteurs)

    try:
        res = requete.order("created_at", desc=True).limit(100).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (recherche plugins niveau={niveau} auteur={auteur}) : {e}")
        raise erreur_api(500, "RECHERCHE_INDISPONIBLE")

    lignes = res.data or []
    noms_par_auteur = {uid: _nom_affiche_ou_repli(uid) for uid in {ligne["auteur_id"] for ligne in lignes}}
    return [_plugin_vers_reponse(ligne, noms_par_auteur) for ligne in lignes]


@router.get("/classement", response_model=List[PluginReponse])
def classement_plugins():
    """
    Classement par nombre de téléchargements décroissant -- pour
    identifier le plugin gagnant de la mécanique de lancement (voir doc
    source). L'attribution de la récompense reste manuelle, voir
    docstring en tête de fichier.
    """
    try:
        res = (
            supabase.table("plugins_programme")
            .select("*")
            .order("telechargements_count", desc=True)
            .limit(100)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (classement plugins) : {e}")
        raise erreur_api(500, "RECHERCHE_INDISPONIBLE")

    lignes = res.data or []
    noms_par_auteur = {uid: _nom_affiche_ou_repli(uid) for uid in {ligne["auteur_id"] for ligne in lignes}}
    return [_plugin_vers_reponse(ligne, noms_par_auteur) for ligne in lignes]


@router.post("/{plugin_id}/telecharger", response_model=TelechargerReponse, status_code=201)
def telecharger_plugin(plugin_id: str, request: Request, utilisateur=Depends(utilisateur_courant)):
    try:
        plugin = (
            supabase.table("plugins_programme")
            .select("id, programme_source_id, nom")
            .eq("id", plugin_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture plugin {plugin_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    if not plugin or not plugin.data:
        raise erreur_api(404, "PLUGIN_INTROUVABLE")

    # Déjà téléchargé par cet utilisateur : ne recompte pas, mais ne
    # recrée pas non plus une deuxième copie -- renvoie la copie existante.
    try:
        deja = (
            supabase.table("plugin_telechargements")
            .select("programme_copie_id")
            .eq("plugin_id", plugin_id)
            .eq("telecharge_par", utilisateur.id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification téléchargement existant {plugin_id}) : {e}")
        deja = None

    if deja and deja.data and deja.data.get("programme_copie_id"):
        return TelechargerReponse(programme_id=deja.data["programme_copie_id"])

    nouveau_programme_id = _cloner_programme(
        programme_source_id=plugin.data["programme_source_id"],
        nouveau_proprietaire_id=utilisateur.id,
        nom_copie=plugin.data["nom"],
    )

    try:
        supabase.table("plugin_telechargements").insert({
            "plugin_id": plugin_id,
            "telecharge_par": utilisateur.id,
            "programme_copie_id": nouveau_programme_id,
        }).execute()

        # Incrément atomique côté base impossible sans RPC dédiée -- lu
        # puis réécrit ici, cohérent avec le reste du dépôt (pas de RPC
        # d'incrément trouvée pour un cas équivalent). Fenêtre de course
        # improbable (deux téléchargements simultanés du même
        # utilisateur), sans conséquence grave si elle se produit
        # (compteur en retard d'une unité, jamais faux positif de
        # sécurité).
        plugin_actuel = (
            supabase.table("plugins_programme")
            .select("telechargements_count")
            .eq("id", plugin_id)
            .single()
            .execute()
        )
        supabase.table("plugins_programme").update({
            "telechargements_count": plugin_actuel.data["telechargements_count"] + 1
        }).eq("id", plugin_id).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (enregistrement téléchargement plugin {plugin_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    journaliser(
        action="plugin.telecharge",
        user_id=utilisateur.id,
        cible_type="plugin_programme",
        cible_id=plugin_id,
        details={"programme_copie_id": nouveau_programme_id},
        request=request,
    )

    return TelechargerReponse(programme_id=nouveau_programme_id)

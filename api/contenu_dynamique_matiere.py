"""
Contenu dynamique par matière -- agent "Nitrux" (2026-08-06, demande
Bourama), pensé réutilisable pour d'autres agents du même genre. Voir
migrations/2026_08_06_contenu_dynamique_par_matiere.sql pour le schéma,
et core/contenu_dynamique_matiere.py pour la résolution du system_prompt
côté chat (routeur + fallback généraliste).

Indépendant de l'ancien système établissement/enseignant/étudiant
(désactivé) : ici, N'IMPORTE QUEL compte connecté peut écrire du contenu
pour une matière sur un agent marqué `contenu_dynamique_par_matiere`
(devient "enseignant" pour cette matière précise), et n'importe quel
compte peut entrer un code pour débloquer ce contenu ("étudiant"). Pas
de vérification de rôle ici, volontairement.
"""

import logging
import os
import secrets
import string
import sys
import tempfile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel

from api.auth import utilisateur_courant, supabase
from api.journal import journaliser
from core.erreurs import erreur_api

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "core"))
from bibliotheque_fichiers import enregistrer_fichier, enregistrer_lien  # noqa: E402
from bibliotheque_rag import indexer_pdf_bibliotheque  # noqa: E402

logging.basicConfig(level=logging.INFO)

router_enseignant = APIRouter(prefix="/api/agents/{agent_id}/contenus-matiere", tags=["contenu_dynamique_matiere"])
router_etudiant = APIRouter(prefix="/api/agents/{agent_id}/rattachements", tags=["contenu_dynamique_matiere"])
router_liste_agents = APIRouter(prefix="/api/agents-contenu-dynamique", tags=["contenu_dynamique_matiere"])


class AgentContenuDynamique(BaseModel):
    id: str
    nom: str


@router_liste_agents.get("", response_model=list[AgentContenuDynamique])
def lister_agents_contenu_dynamique():
    """
    Agents actifs marqués `contenu_dynamique_par_matiere` (ex: Nitrux).
    Sert à afficher dynamiquement, dans Mon espace (2026-08-06), l'entrée
    "Matières" sans coder le nom/id d'un agent précis en dur côté
    frontend -- s'il y en a plusieurs un jour, elles apparaissent toutes.
    """
    try:
        res = (
            supabase.table("agents")
            .select("id, nom")
            .eq("contenu_dynamique_par_matiere", True)
            .eq("actif", True)
            .order("nom")
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (liste agents contenu dynamique) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return [AgentContenuDynamique(**ligne) for ligne in (res.data or [])]

# Alphabet sans caractères ambigus (0/O, 1/I/L) -- code pensé pour être
# recopié à la main par un étudiant depuis un tableau/une feuille.
_ALPHABET_CODE = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_LONGUEUR_CODE = 6
_TENTATIVES_MAX_CODE = 10


def _generer_code_unique() -> str:
    for _ in range(_TENTATIVES_MAX_CODE):
        code = "".join(secrets.choice(_ALPHABET_CODE) for _ in range(_LONGUEUR_CODE))
        try:
            existe = supabase.table("contenus_par_matiere").select("id").eq("code", code).maybe_single().execute()
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (vérification unicité code) : {e}")
            continue
        if not existe or not existe.data:
            return code
    raise erreur_api(500, "ERREUR_INCONNUE")


class ContenuMatierePayload(BaseModel):
    matiere: str
    system_prompt: str


class ContenuMatiere(BaseModel):
    id: str
    matiere: str
    system_prompt: str
    code: str


@router_enseignant.get("", response_model=list[ContenuMatiere])
def lister_mes_contenus(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """Les matières que CE compte a écrites pour cet agent (mes codes à partager)."""
    try:
        res = (
            supabase.table("contenus_par_matiere")
            .select("id, matiere, system_prompt, code")
            .eq("agent_id", agent_id)
            .eq("enseignant_id", utilisateur.id)
            .order("matiere")
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture contenus_par_matiere {agent_id}/{utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return [ContenuMatiere(**ligne) for ligne in (res.data or [])]


@router_enseignant.put("", response_model=ContenuMatiere)
def ecrire_contenu_matiere(agent_id: str, payload: ContenuMatierePayload, utilisateur=Depends(utilisateur_courant)):
    """
    Crée ou met à jour (même matière, même auteur = même ligne, voir
    contrainte UNIQUE(agent_id, enseignant_id, matiere)) le contenu d'une
    matière. Le code n'est généré qu'à la création -- une mise à jour du
    texte ne change jamais le code déjà partagé aux étudiants.
    """
    matiere = payload.matiere.strip()
    system_prompt = payload.system_prompt.strip()
    if not matiere or not system_prompt:
        raise erreur_api(400, "MATIERE_ET_SYSTEM_PROMPT_REQUIS")

    try:
        existant = (
            supabase.table("contenus_par_matiere")
            .select("id, code")
            .eq("agent_id", agent_id)
            .eq("enseignant_id", utilisateur.id)
            .eq("matiere", matiere)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification contenu existant {agent_id}/{utilisateur.id}/{matiere}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    if existant and existant.data:
        try:
            supabase.table("contenus_par_matiere").update(
                {"system_prompt": system_prompt, "updated_at": "now()"}
            ).eq("id", existant.data["id"]).execute()
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (mise à jour contenu {existant.data['id']}) : {e}")
            raise erreur_api(500, "ERREUR_INCONNUE")
        code = existant.data["code"]
        contenu_id = existant.data["id"]
    else:
        code = _generer_code_unique()
        try:
            res = (
                supabase.table("contenus_par_matiere")
                .insert(
                    {
                        "agent_id": agent_id,
                        "enseignant_id": utilisateur.id,
                        "matiere": matiere,
                        "system_prompt": system_prompt,
                        "code": code,
                    }
                )
                .execute()
            )
        except Exception as e:
            logging.error(f"ERREUR SUPABASE (création contenu {agent_id}/{utilisateur.id}/{matiere}) : {e}")
            raise erreur_api(500, "ERREUR_INCONNUE")
        contenu_id = res.data[0]["id"]

        journaliser(
            action="contenu_matiere.cree",
            user_id=utilisateur.id,
            cible_type="agent",
            cible_id=agent_id,
            details={"matiere": matiere},
        )

    return ContenuMatiere(id=contenu_id, matiere=matiere, system_prompt=system_prompt, code=code)


class RattachementPayload(BaseModel):
    code: str


class Rattachement(BaseModel):
    contenu_id: str
    matiere: str
    enseignant_nom: str
    actif: bool
    surnom: str | None = None


def _nom_enseignant(enseignant_id: str) -> str:
    try:
        res = (
            supabase.table("profiles").select("nom_affiche").eq("user_id", enseignant_id).maybe_single().execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (nom enseignant {enseignant_id}) : {e}")
        return "Enseignant"
    return (res.data or {}).get("nom_affiche") or "Enseignant" if res else "Enseignant"


@router_etudiant.get("", response_model=list[Rattachement])
def lister_mes_rattachements(agent_id: str, utilisateur=Depends(utilisateur_courant)):
    """Toutes les matières débloquées par CE compte sur cet agent, actives ou non
    (les non-actives servent au bouton "changer d'enseignant" côté chat)."""
    try:
        res = (
            supabase.table("rattachements_par_matiere")
            .select("contenu_id, matiere, actif, surnom, contenus_par_matiere(enseignant_id)")
            .eq("agent_id", agent_id)
            .eq("etudiant_id", utilisateur.id)
            .order("matiere")
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture rattachements {agent_id}/{utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    resultat = []
    for ligne in res.data or []:
        enseignant_id = (ligne.get("contenus_par_matiere") or {}).get("enseignant_id")
        resultat.append(
            Rattachement(
                contenu_id=ligne["contenu_id"],
                matiere=ligne["matiere"],
                enseignant_nom=_nom_enseignant(enseignant_id) if enseignant_id else "Enseignant",
                actif=ligne["actif"],
                surnom=ligne.get("surnom"),
            )
        )
    return resultat


@router_etudiant.post("", response_model=Rattachement, status_code=201)
def entrer_code(agent_id: str, payload: RattachementPayload, utilisateur=Depends(utilisateur_courant)):
    code = payload.code.strip().upper()
    try:
        contenu = (
            supabase.table("contenus_par_matiere")
            .select("id, matiere, enseignant_id")
            .eq("agent_id", agent_id)
            .eq("code", code)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (recherche code {code} pour agent {agent_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    if not contenu or not contenu.data:
        raise erreur_api(404, "CODE_INVALIDE")

    contenu_id = contenu.data["id"]
    matiere = contenu.data["matiere"]

    try:
        deja = (
            supabase.table("rattachements_par_matiere")
            .select("id")
            .eq("etudiant_id", utilisateur.id)
            .eq("contenu_id", contenu_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification rattachement existant) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if deja and deja.data:
        raise erreur_api(400, "DEJA_RATTACHE_A_CE_CONTENU")

    # Actif par défaut UNIQUEMENT si l'étudiant n'a encore aucun
    # rattachement actif pour cette matière (voir index unique partiel
    # côté base) -- sinon ce nouveau rattachement reste inactif, l'ancien
    # gardant la main tant que l'étudiant ne bascule pas explicitement.
    try:
        actif_existant = (
            supabase.table("rattachements_par_matiere")
            .select("id")
            .eq("etudiant_id", utilisateur.id)
            .eq("agent_id", agent_id)
            .eq("matiere", matiere)
            .eq("actif", True)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification actif existant {matiere}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    actif = not (actif_existant and actif_existant.data)

    try:
        supabase.table("rattachements_par_matiere").insert(
            {
                "agent_id": agent_id,
                "etudiant_id": utilisateur.id,
                "contenu_id": contenu_id,
                "matiere": matiere,
                "actif": actif,
            }
        ).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (création rattachement {contenu_id}/{utilisateur.id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    journaliser(
        action="rattachement_matiere.cree",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"matiere": matiere, "actif": actif},
    )

    return Rattachement(
        contenu_id=contenu_id,
        matiere=matiere,
        enseignant_nom=_nom_enseignant(contenu.data["enseignant_id"]),
        actif=actif,
        surnom=None,
    )


class SurnomPayload(BaseModel):
    surnom: str


@router_etudiant.patch("/{contenu_id}/surnom", status_code=204)
def renommer_rattachement(agent_id: str, contenu_id: str, payload: SurnomPayload, utilisateur=Depends(utilisateur_courant)):
    """Label perso optionnel (06/08, demande Bourama) : l'étudiant peut
    donner un nom à un rattachement pour s'y retrouver dans sa liste (ex:
    plusieurs enseignants sur la même matière). Vide (chaîne blanche)
    remet le surnom à null plutôt que de stocker une chaîne vide."""
    surnom = payload.surnom.strip() or None
    try:
        maj = (
            supabase.table("rattachements_par_matiere")
            .update({"surnom": surnom})
            .eq("etudiant_id", utilisateur.id)
            .eq("agent_id", agent_id)
            .eq("contenu_id", contenu_id)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (renommage rattachement {contenu_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not maj.data:
        raise erreur_api(404, "RATTACHEMENT_INTROUVABLE")


@router_etudiant.patch("/{contenu_id}/activer", status_code=204)
def activer_rattachement(agent_id: str, contenu_id: str, utilisateur=Depends(utilisateur_courant)):
    """Bouton "changer d'enseignant" dans le chat : bascule quel rattachement
    est actif pour la matière du contenu visé, tous les autres rattachements
    de cet étudiant pour la même matière repassent inactifs."""
    try:
        cible = (
            supabase.table("rattachements_par_matiere")
            .select("id, matiere")
            .eq("etudiant_id", utilisateur.id)
            .eq("agent_id", agent_id)
            .eq("contenu_id", contenu_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture rattachement à activer {contenu_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not cible or not cible.data:
        raise erreur_api(404, "RATTACHEMENT_INTROUVABLE")

    matiere = cible.data["matiere"]
    try:
        # Désactive d'abord tout le monde sur cette matière (contrainte
        # unique partielle sinon violée par le passage à True ci-dessous),
        # puis active uniquement la cible.
        supabase.table("rattachements_par_matiere").update({"actif": False}).eq(
            "etudiant_id", utilisateur.id
        ).eq("agent_id", agent_id).eq("matiere", matiere).execute()
        supabase.table("rattachements_par_matiere").update({"actif": True}).eq("id", cible.data["id"]).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (activation rattachement {contenu_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")


class Receveur(BaseModel):
    user_id: str
    nom_affiche: str
    surnom: str | None = None
    actif: bool


@router_enseignant.get("/{contenu_id}/receveurs", response_model=list[Receveur])
def lister_receveurs(agent_id: str, contenu_id: str, utilisateur=Depends(utilisateur_courant)):
    """
    Qui a entré MON code pour cette matière précise -- 09/08, demande
    Bourama ("Class GPT": plus de rôle enseignant/étudiant, juste "tu
    génères un code ou tu en entres un"). N'existait pas encore : jusqu'ici
    seul l'étudiant pouvait lister SES rattachements (lister_mes_rattachements
    ci-dessus), rien côté auteur du contenu pour voir qui l'a débloqué.
    Vérifie que le contenu appartient bien à l'appelant avant de renvoyer
    quoi que ce soit (pas de fuite vers un contenu_id d'un autre compte).
    Actif ou non : quelqu'un qui a débloqué puis basculé sur un autre
    enseignant pour la même matière (voir activer_rattachement) reste
    listé ici -- il a bien "entré ce code" à un moment donné.
    """
    try:
        contenu = (
            supabase.table("contenus_par_matiere")
            .select("id")
            .eq("id", contenu_id)
            .eq("agent_id", agent_id)
            .eq("enseignant_id", utilisateur.id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification propriété contenu {contenu_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not contenu or not contenu.data:
        raise erreur_api(404, "CONTENU_INTROUVABLE")

    try:
        res = (
            supabase.table("rattachements_par_matiere")
            .select("etudiant_id, surnom, actif")
            .eq("contenu_id", contenu_id)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture receveurs {contenu_id}) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")

    lignes = res.data or []
    if not lignes:
        return []

    ids = [l["etudiant_id"] for l in lignes]
    try:
        profils = (
            supabase.table("profiles").select("user_id, nom_affiche").in_("user_id", ids).execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture profils receveurs {contenu_id}) : {e}")
        profils = None
    noms = {p["user_id"]: p.get("nom_affiche") for p in (profils.data if profils else [])}

    return [
        Receveur(
            user_id=l["etudiant_id"],
            nom_affiche=noms.get(l["etudiant_id"]) or "Sans nom",
            surnom=l.get("surnom"),
            actif=l["actif"],
        )
        for l in lignes
    ]


TYPES_DIFFUSION_AUTORISES = {
    "application/pdf",
    "image/jpeg", "image/png", "image/webp",
    "audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4",
    "video/mp4", "video/webm", "video/quicktime",
}
TAILLE_MAX_DIFFUSION_OCTETS = 50 * 1024 * 1024  # 50 Mo, même limite que la bibliothèque perso


class ResultatDiffusionMatiere(BaseModel):
    diffuse_a: int
    total_receveurs: int
    echecs: list[str]


def _receveurs_de(contenu_id: str, utilisateur_id: str, agent_id: str) -> list[str]:
    """Vérifie la propriété du contenu et renvoie les user_id de tous ses
    receveurs (actifs ou non, voir lister_receveurs ci-dessus)."""
    try:
        contenu = (
            supabase.table("contenus_par_matiere")
            .select("id")
            .eq("id", contenu_id)
            .eq("agent_id", agent_id)
            .eq("enseignant_id", utilisateur_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (vérification propriété contenu {contenu_id} avant diffusion) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    if not contenu or not contenu.data:
        raise erreur_api(404, "CONTENU_INTROUVABLE")

    try:
        res = (
            supabase.table("rattachements_par_matiere")
            .select("etudiant_id")
            .eq("contenu_id", contenu_id)
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture receveurs {contenu_id} avant diffusion) : {e}")
        raise erreur_api(500, "ERREUR_INCONNUE")
    return [l["etudiant_id"] for l in (res.data or [])]


@router_enseignant.post("/{contenu_id}/diffuser", response_model=ResultatDiffusionMatiere, status_code=201)
async def diffuser_document_matiere(
    agent_id: str,
    contenu_id: str,
    request: Request,
    fichier: UploadFile = File(...),
    titre: str = Form(None),
    description: str = Form(None),
    utilisateur=Depends(utilisateur_courant),
):
    """
    Diffuse un fichier à tous ceux qui ont entré MON code pour cette
    matière -- 09/08, demande Bourama. Contrairement à
    api/roles.py:diffuser_document (qui écrit niveau="agent", donc visible
    par TOUS les utilisateurs de l'agent), ici le fichier est ajouté
    séparément dans la BIBLIOTHÈQUE PERSONNELLE de CHAQUE receveur
    (niveau="utilisateur", voir api/bibliotheque_utilisateur.py) : privé au
    lien code-par-code, pas de fuite vers les autres receveurs de Nitrux.
    Vectorisation PDF via indexer_pdf_bibliotheque (table dédiée
    documents_bibliotheque, scopée user_id) -- pas indexer_document (RAG
    partagé par agent_id, non scopé par utilisateur), pour la même raison
    de confidentialité.
    """
    if fichier.content_type not in TYPES_DIFFUSION_AUTORISES:
        raise erreur_api(400, "TYPE_DE_FICHIER_NON_SUPPORTE")

    contenu = await fichier.read()
    if len(contenu) == 0:
        raise erreur_api(400, "FICHIER_VIDE")
    if len(contenu) > TAILLE_MAX_DIFFUSION_OCTETS:
        raise erreur_api(400, "FICHIER_TROP_LOURD_50_MO_MAX")

    receveurs = _receveurs_de(contenu_id, utilisateur.id, agent_id)
    if not receveurs:
        return ResultatDiffusionMatiere(diffuse_a=0, total_receveurs=0, echecs=[])

    nom_original = fichier.filename or "fichier"
    description_finale = (
        f"{titre.strip()} — {description.strip()}" if (titre or "").strip() and (description or "").strip()
        else (description or titre or "").strip() or nom_original
    )

    diffuse_a = 0
    echecs: list[str] = []
    for receveur_id in receveurs:
        try:
            ligne = enregistrer_fichier(
                contenu=contenu,
                nom_fichier=nom_original,
                type_mime=fichier.content_type,
                niveau="utilisateur",
                uploade_par=utilisateur.id,
                user_id=receveur_id,
                description=description_finale,
            )
        except Exception as e:
            logging.error(f"ERREUR diffusion matière (contenu_id={contenu_id}, receveur={receveur_id}) : {e}")
            echecs.append(receveur_id)
            continue

        if fichier.content_type == "application/pdf":
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(contenu)
                chemin_temp = tmp.name
            try:
                indexer_pdf_bibliotheque(chemin_temp, fichier_id=ligne["id"], user_id=receveur_id)
            except Exception as e:
                # Non bloquant, comme la bibliothèque perso (voir sa
                # docstring) : le fichier reste stocké et retrouvable même
                # si la vectorisation échoue.
                logging.error(f"ERREUR vectorisation PDF diffusion (fichier_id={ligne['id']}, receveur={receveur_id}) : {e}")
            finally:
                try:
                    os.remove(chemin_temp)
                except OSError:
                    pass

        diffuse_a += 1

    journaliser(
        action="contenu_matiere.diffuse",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"contenu_id": contenu_id, "nom_original": nom_original, "diffuse_a": diffuse_a, "total_receveurs": len(receveurs)},
        request=request,
    )

    return ResultatDiffusionMatiere(diffuse_a=diffuse_a, total_receveurs=len(receveurs), echecs=echecs)


class DiffuserLienMatierePayload(BaseModel):
    url: str
    titre: str | None = None
    description: str | None = None


@router_enseignant.post("/{contenu_id}/diffuser-lien", response_model=ResultatDiffusionMatiere, status_code=201)
def diffuser_lien_matiere(
    agent_id: str,
    contenu_id: str,
    payload: DiffuserLienMatierePayload,
    request: Request,
    utilisateur=Depends(utilisateur_courant),
):
    """Pendant de diffuser_document_matiere pour un lien (pas de fichier,
    juste une URL) -- même portée (mes receveurs pour ce contenu_id
    précis), même stockage niveau="utilisateur" par receveur."""
    if not (payload.titre or "").strip() and not (payload.description or "").strip():
        raise erreur_api(400, "DONNE_AU_MOINS_UNE_DESCRIPTION_OU")
    if not (payload.url or "").strip():
        raise erreur_api(400, "URL_MANQUANTE")

    receveurs = _receveurs_de(contenu_id, utilisateur.id, agent_id)
    if not receveurs:
        return ResultatDiffusionMatiere(diffuse_a=0, total_receveurs=0, echecs=[])

    description_finale = (
        f"{payload.titre.strip()} — {payload.description.strip()}"
        if (payload.titre or "").strip() and (payload.description or "").strip()
        else (payload.description or payload.titre or "").strip()
    )

    diffuse_a = 0
    echecs: list[str] = []
    for receveur_id in receveurs:
        try:
            enregistrer_lien(
                url=payload.url.strip(),
                nom_fichier=payload.titre.strip() if payload.titre else payload.url.strip(),
                niveau="utilisateur",
                uploade_par=utilisateur.id,
                user_id=receveur_id,
                description=description_finale,
            )
            diffuse_a += 1
        except Exception as e:
            logging.error(f"ERREUR diffusion lien matière (contenu_id={contenu_id}, receveur={receveur_id}) : {e}")
            echecs.append(receveur_id)

    journaliser(
        action="contenu_matiere.lien_diffuse",
        user_id=utilisateur.id,
        cible_type="agent",
        cible_id=agent_id,
        details={"contenu_id": contenu_id, "url": payload.url, "diffuse_a": diffuse_a, "total_receveurs": len(receveurs)},
        request=request,
    )

    return ResultatDiffusionMatiere(diffuse_a=diffuse_a, total_receveurs=len(receveurs), echecs=echecs)

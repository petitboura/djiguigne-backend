"""
Serveur MCP local pour la structure programme (classe -> matière ->
chapitre) -- monté comme core/serveur_mcp_generation.py et
core/serveur_mcp_github.py (voir api/main.py), enregistré dans
registre_outils.py au même titre.

La liste LÉGÈRE des programmes de l'étudiant (id/niveau/nom) est déjà
envoyée automatiquement dans le system prompt (voir
core/audit_programme.py:lister_mes_programmes_legers, injectée dans
core/main.py) -- l'IA a donc déjà les id de programme sans avoir besoin
d'un outil "lister". Ce fichier ne fournit que ce qui manque : consulter
la structure COMPLÈTE d'un programme choisi, et écrire dedans.

Identité de l'appelant : comme envoyer_message/planifier_rappel dans
core/serveur_mcp_generation.py, récupérée via
ctx.request_context.request.query_params (transmis dans l'URL par
_url_programme() dans registre_outils.py) -- JAMAIS un paramètre `user_id`
laissé au modèle, pour que l'IA ne puisse pas lire/écrire dans le
programme de quelqu'un d'autre en se trompant (ou en étant manipulée) sur
l'id à passer.

Scope volontairement limité pour cette première version : lecture
complète d'un programme, ajout de matière/chapitre/exercice/document.
Modification/suppression et examens/plugins pourront être ajoutés plus
tard en suivant exactement le même pattern (voir api/programmes.py,
api/contenu_programme.py, api/plugins_programme.py pour la logique déjà
construite côté REST -- ce fichier duplique volontairement les vérifications
de propriété plutôt que d'importer les routes FastAPI, même convention que
core/audit_programme.py et core/proactivite.py qui ne dépendent jamais des
fichiers api/*.py directement).
"""

import logging

from mcp.server.mcpserver import MCPServer as FastMCP, Context

from api.auth import supabase

logging.basicConfig(level=logging.INFO)

mcp_programme = FastMCP(name="programme")


def _user_id_depuis_contexte(ctx: Context) -> str | None:
    try:
        return ctx.request_context.request.query_params.get("user_id")
    except Exception as e:
        logging.error(f"ERREUR lecture user_id contexte MCP programme : {e}")
        return None


def _programme_appartient_a(programme_id: str, user_id: str) -> bool:
    try:
        res = (
            supabase.table("programmes")
            .select("id")
            .eq("id", programme_id)
            .eq("proprietaire_id", user_id)
            .maybe_single()
            .execute()
        )
        return bool(res and res.data)
    except Exception as e:
        logging.error(f"ERREUR vérification propriété programme {programme_id} : {e}")
        return False


def _matiere_appartient_a(matiere_id: str, user_id: str) -> dict | None:
    try:
        res = (
            supabase.table("matieres")
            .select("*, programmes!inner(proprietaire_id)")
            .eq("id", matiere_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR vérification propriété matière {matiere_id} : {e}")
        return None
    if not res or not res.data or res.data.get("programmes", {}).get("proprietaire_id") != user_id:
        return None
    return res.data


def _chapitre_appartient_a(chapitre_id: str, user_id: str) -> bool:
    try:
        res = (
            supabase.table("chapitres")
            .select("id, matieres!inner(programme_id, programmes!inner(proprietaire_id))")
            .eq("id", chapitre_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logging.error(f"ERREUR vérification propriété chapitre {chapitre_id} : {e}")
        return False
    if not res or not res.data:
        return False
    return (res.data.get("matieres") or {}).get("programmes", {}).get("proprietaire_id") == user_id


@mcp_programme.tool()
def consulter_programme(programme_id: str, ctx: Context) -> str:
    """
    Renvoie la structure complète d'un programme (classe/matière/chapitre)
    de l'étudiant connecté : toutes ses matières, avec leurs limites, et
    pour chaque matière tous ses chapitres avec LEUR nom, ordre, id et
    limites. `programme_id` vient de la liste des programmes déjà indiquée
    dans ton contexte. Utilise le id d'un chapitre renvoyé ici pour ensuite
    y ajouter un exercice ou un document avec les autres outils.
    """
    user_id = _user_id_depuis_contexte(ctx)
    if not user_id:
        return "Erreur : impossible d'identifier l'étudiant."
    if not _programme_appartient_a(programme_id, user_id):
        return "Erreur : programme introuvable ou n'appartenant pas à cet étudiant."

    try:
        matieres = (
            supabase.table("matieres").select("*").eq("programme_id", programme_id).order("created_at").execute().data
            or []
        )
    except Exception as e:
        logging.error(f"ERREUR consulter_programme (matières, {programme_id}) : {e}")
        return "Erreur : lecture du programme impossible, réessaie."

    lignes = []
    for matiere in matieres:
        lignes.append(f"- MATIÈRE id={matiere['id']} — {matiere['nom']}" + (f" (limites : {matiere['limites']})" if matiere.get("limites") else ""))
        try:
            chapitres = (
                supabase.table("chapitres")
                .select("*")
                .eq("matiere_id", matiere["id"])
                .order("ordre")
                .execute()
                .data
                or []
            )
        except Exception as e:
            logging.error(f"ERREUR consulter_programme (chapitres de {matiere['id']}) : {e}")
            chapitres = []
        for chapitre in chapitres:
            lignes.append(
                f"    - CHAPITRE id={chapitre['id']} (ordre {chapitre['ordre']}) — {chapitre['nom']}"
                + (f" (limites : {chapitre['limites']})" if chapitre.get("limites") else "")
            )

    if not lignes:
        return "Ce programme n'a encore aucune matière."
    return "\n".join(lignes)


@mcp_programme.tool()
def ajouter_matiere(programme_id: str, nom: str, limites: str, ctx: Context) -> str:
    """
    Ajoute une nouvelle matière à un programme de l'étudiant. `limites`
    décrit le cadre du programme officiel pour cette matière (pour rester
    dans le programme, jamais hors-programme) -- laisse vide ("") si
    l'étudiant n'a pas précisé de limites.
    """
    user_id = _user_id_depuis_contexte(ctx)
    if not user_id:
        return "Erreur : impossible d'identifier l'étudiant."
    if not _programme_appartient_a(programme_id, user_id):
        return "Erreur : programme introuvable ou n'appartenant pas à cet étudiant."
    if not nom.strip():
        return "Erreur : le nom de la matière est requis."

    try:
        res = (
            supabase.table("matieres")
            .insert({"programme_id": programme_id, "nom": nom.strip(), "limites": limites.strip() or None})
            .execute()
        )
        return f"Matière \"{nom.strip()}\" ajoutée (id={res.data[0]['id']})."
    except Exception as e:
        logging.error(f"ERREUR ajouter_matiere ({programme_id}) : {e}")
        return "Erreur : l'ajout de la matière a échoué, réessaie."


@mcp_programme.tool()
def ajouter_chapitre(matiere_id: str, nom: str, limites: str, ordre: int, ctx: Context) -> str:
    """
    Ajoute un nouveau chapitre à une matière de l'étudiant. `ordre`
    détermine sa position dans la liste (0 = premier). `limites` décrit le
    cadre officiel pour ce chapitre précis -- laisse vide ("") si non
    précisé.
    """
    user_id = _user_id_depuis_contexte(ctx)
    if not user_id:
        return "Erreur : impossible d'identifier l'étudiant."
    if not _matiere_appartient_a(matiere_id, user_id):
        return "Erreur : matière introuvable ou n'appartenant pas à cet étudiant."
    if not nom.strip():
        return "Erreur : le nom du chapitre est requis."

    try:
        res = (
            supabase.table("chapitres")
            .insert({"matiere_id": matiere_id, "nom": nom.strip(), "ordre": ordre or 0, "limites": limites.strip() or None})
            .execute()
        )
        return f"Chapitre \"{nom.strip()}\" ajouté (id={res.data[0]['id']})."
    except Exception as e:
        logging.error(f"ERREUR ajouter_chapitre ({matiere_id}) : {e}")
        return "Erreur : l'ajout du chapitre a échoué, réessaie."


@mcp_programme.tool()
def ajouter_exercice(chapitre_id: str, enonce: str, ctx: Context) -> str:
    """
    Ajoute un exercice à un chapitre précis du programme de l'étudiant.
    `enonce` est le texte complet de l'exercice (ex: dicté par l'étudiant
    dans le chat, ou rédigé par toi à sa demande).
    """
    user_id = _user_id_depuis_contexte(ctx)
    if not user_id:
        return "Erreur : impossible d'identifier l'étudiant."
    if not _chapitre_appartient_a(chapitre_id, user_id):
        return "Erreur : chapitre introuvable ou n'appartenant pas à cet étudiant."
    if not enonce.strip():
        return "Erreur : l'énoncé de l'exercice est requis."

    try:
        supabase.table("exercices_programme").insert({"chapitre_id": chapitre_id, "enonce": enonce.strip()}).execute()
        return "Exercice ajouté au chapitre."
    except Exception as e:
        logging.error(f"ERREUR ajouter_exercice ({chapitre_id}) : {e}")
        return "Erreur : l'ajout de l'exercice a échoué, réessaie."


@mcp_programme.tool()
def ajouter_document(chapitre_id: str, titre: str, contenu_ou_url: str, ctx: Context) -> str:
    """
    Ajoute un document (cours, ressource) à un chapitre précis. `titre`
    est le nom du document, `contenu_ou_url` soit une URL vers un fichier
    déjà hébergé, soit directement le texte du document.
    """
    user_id = _user_id_depuis_contexte(ctx)
    if not user_id:
        return "Erreur : impossible d'identifier l'étudiant."
    if not _chapitre_appartient_a(chapitre_id, user_id):
        return "Erreur : chapitre introuvable ou n'appartenant pas à cet étudiant."
    if not titre.strip() or not contenu_ou_url.strip():
        return "Erreur : titre et contenu du document requis."

    try:
        supabase.table("documents_programme").insert(
            {"chapitre_id": chapitre_id, "titre": titre.strip(), "url_ou_contenu": contenu_ou_url.strip()}
        ).execute()
        return f"Document \"{titre.strip()}\" ajouté au chapitre."
    except Exception as e:
        logging.error(f"ERREUR ajouter_document ({chapitre_id}) : {e}")
        return "Erreur : l'ajout du document a échoué, réessaie."

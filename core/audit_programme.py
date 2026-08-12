"""
Audit IA hebdomadaire par matière (2026-08-12, chantier "connexion IA <->
structure programme", discussion Bourama).

Chaque lundi, pour chaque matière ayant du contenu, l'IA relit
INTÉGRALEMENT la donnée source (chapitres, limites, documents/PDF
extraits, énoncés d'exercices) -- jamais son propre texte de la semaine
précédente -- et écrit un audit structuré (limites/attentes/état, organisé
par chapitre). Ce texte est réécrit en place chaque lundi (upsert, un seul
par matière), puis découpé + vectorisé pour être injecté automatiquement
en RAG pendant le chat (voir core/main.py, même mécanique que le RAG
documents existant -- scopé par étudiant ici, pas par agent).

Volontairement séparé de core/bibliotheque_rag.py et core/retriever.py :
même schéma vector(768)/gemini-embedding-001 (voir core/embeddings.py),
mais une table dédiée (audits_matiere_chunks) pour ne rien risquer sur les
deux circuits RAG existants.

Pas de suivi de performance/résultats étudiant ici (hors scope, décision
Bourama 12/08) -- uniquement le contenu du programme lui-même.
"""

import io
import os
import logging
from datetime import datetime, timezone, timedelta

import requests
import PyPDF2
from groq import Groq

from api.auth import supabase
from core.embeddings import vectoriser, decouper_texte

logging.basicConfig(level=logging.INFO)


def get_secret(key):
    return os.environ.get(key)


# Dupliqué depuis core/main.py:GROQ_PRIMARY volontairement (pas d'import
# croisé -- core/main.py importe CE module pour l'injection RAG dans le
# prompt système, un import dans l'autre sens créerait un cycle. Même
# pattern déjà utilisé partout ailleurs dans ce projet, voir
# core/retriever.py, indexers/index_documents.py).
GROQ_PRIMARY = "openai/gpt-oss-120b"

TAILLE_MAX_CHUNKS_PAR_AUDIT = 400  # même garde-fou que bibliotheque_rag.py
DELAI_TELECHARGEMENT_DOC = 15  # secondes -- best-effort, un document lent ne doit jamais bloquer tout l'audit

INSTRUCTION_AUDIT_MATIERE = (
    "Tu es chargé d'auditer le contenu d'une matière scolaire pour un "
    "étudiant. On te donne la liste de ses chapitres, les limites du "
    "programme officiel définies pour chacun, le contenu des documents "
    "qui y sont rattachés, et les énoncés d'exercices existants.\n\n"
    "Écris un audit structuré, organisé chapitre par chapitre, qui décrit "
    "pour chacun : les limites du programme officiel à respecter (jamais "
    "hors-programme), ce qui est déjà couvert par le contenu existant, et "
    "les attentes/comportements à garder pour un futur tuteur IA qui "
    "aiderait cet étudiant sur cette matière (ex: rester dans tel cadre, "
    "tel niveau de difficulté, telles notions ne sont pas encore vues).\n\n"
    "Ne parle jamais de performance, de résultats ou de niveau réel de "
    "l'étudiant : tu n'as accès à aucune donnée sur ce qu'il a réussi ou "
    "raté -- seulement au contenu du programme lui-même. Texte en "
    "français, aussi long que nécessaire, pas de préambule ni de "
    "conclusion générique."
)


def _telecharger_et_extraire(url: str) -> str:
    """Best-effort : télécharge un document distant et en extrait le texte
    (PDF via PyPDF2, sinon traité comme texte brut). Ne lève jamais
    d'exception -- renvoie une chaîne vide en cas d'échec, loggé."""
    try:
        reponse = requests.get(url, timeout=DELAI_TELECHARGEMENT_DOC)
        reponse.raise_for_status()
    except Exception as e:
        logging.error(f"ERREUR téléchargement document audit ({url}) : {e}")
        return ""

    content_type = (reponse.headers.get("content-type") or "").lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            lecteur = PyPDF2.PdfReader(io.BytesIO(reponse.content))
            texte = "".join((page.extract_text() or "") + "\n" for page in lecteur.pages)
            return texte.replace("\x00", "")
        except Exception as e:
            logging.error(f"ERREUR extraction PDF audit ({url}) : {e}")
            return ""

    try:
        return reponse.content.decode("utf-8", errors="ignore")
    except Exception as e:
        logging.error(f"ERREUR décodage document audit ({url}) : {e}")
        return ""


def _contenu_document(url_ou_contenu: str) -> str:
    """Un document_programme stocke soit une URL, soit du texte direct
    (voir api/contenu_programme.py, lot 2). Heuristique : commence par
    http(s):// -> on télécharge, sinon c'est déjà du texte."""
    if url_ou_contenu.strip().lower().startswith(("http://", "https://")):
        return _telecharger_et_extraire(url_ou_contenu.strip())
    return url_ou_contenu


def _construire_contenu_matiere(matiere: dict) -> str:
    """Assemble tout le contenu réel d'une matière (chapitres, limites,
    documents extraits, exercices) en un seul texte, prêt à être donné au
    LLM pour l'audit. Best-effort à chaque étape -- un document illisible
    ne doit jamais faire échouer tout l'audit de la matière."""
    matiere_id = matiere["id"]
    try:
        chapitres = (
            supabase.table("chapitres")
            .select("*")
            .eq("matiere_id", matiere_id)
            .order("ordre")
            .execute()
            .data
            or []
        )
    except Exception as e:
        logging.error(f"ERREUR lecture chapitres pour audit (matière {matiere_id}) : {e}")
        return ""

    blocs = [f"MATIÈRE : {matiere['nom']}"]
    if matiere.get("limites"):
        blocs.append(f"Limites globales de la matière : {matiere['limites']}")

    for chapitre in chapitres:
        chapitre_id = chapitre["id"]
        bloc = [f"\n--- CHAPITRE : {chapitre['nom']} ---"]
        if chapitre.get("limites"):
            bloc.append(f"Limites de ce chapitre : {chapitre['limites']}")

        try:
            documents = (
                supabase.table("documents_programme").select("*").eq("chapitre_id", chapitre_id).execute().data or []
            )
        except Exception as e:
            logging.error(f"ERREUR lecture documents pour audit (chapitre {chapitre_id}) : {e}")
            documents = []
        for doc in documents:
            texte_doc = _contenu_document(doc.get("url_ou_contenu", ""))
            if texte_doc.strip():
                bloc.append(f"Document \"{doc.get('titre', '')}\" :\n{texte_doc}")

        try:
            exercices = (
                supabase.table("exercices_programme").select("*").eq("chapitre_id", chapitre_id).execute().data or []
            )
        except Exception as e:
            logging.error(f"ERREUR lecture exercices pour audit (chapitre {chapitre_id}) : {e}")
            exercices = []
        for exercice in exercices:
            if exercice.get("enonce", "").strip():
                bloc.append(f"Exercice existant : {exercice['enonce']}")

        blocs.append("\n".join(bloc))

    return "\n".join(blocs)


def _generer_texte_audit(contenu_matiere: str) -> str:
    client = Groq(api_key=get_secret("GROQ_API_KEY"))
    reponse = client.chat.completions.create(
        model=GROQ_PRIMARY,
        messages=[
            {"role": "system", "content": INSTRUCTION_AUDIT_MATIERE},
            {"role": "user", "content": contenu_matiere},
        ],
    )
    return reponse.choices[0].message.content or ""


def executer_audit_matiere(matiere: dict, proprietaire_id: str) -> bool:
    """Audite UNE matière : construit son contenu réel, génère le texte,
    l'upsert dans audits_matiere, puis re-vectorise ses chunks (les
    anciens sont supprimés avant réinsertion -- jamais d'accumulation).
    Best-effort : ne lève jamais d'exception, renvoie False en cas
    d'échec (loggé), pour ne jamais interrompre la boucle sur les autres
    matières. Renvoie True si l'audit a été écrit avec succès."""
    matiere_id = matiere["id"]
    try:
        contenu = _construire_contenu_matiere(matiere)
        if not contenu.strip():
            logging.info(f"Audit matière {matiere_id} ignoré : aucun contenu à analyser.")
            return False

        texte_audit = _generer_texte_audit(contenu)
        if not texte_audit.strip():
            logging.error(f"ERREUR audit matière {matiere_id} : le LLM n'a renvoyé aucun texte.")
            return False

        res = (
            supabase.table("audits_matiere")
            .upsert(
                {
                    "matiere_id": matiere_id,
                    "proprietaire_id": proprietaire_id,
                    "texte": texte_audit,
                    "derniere_execution": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="matiere_id",
            )
            .execute()
        )
        audit_id = res.data[0]["id"]

        # Réindexation complète : on supprime les anciens chunks avant
        # d'insérer les nouveaux (jamais d'accumulation entre deux lundis).
        supabase.table("audits_matiere_chunks").delete().eq("audit_id", audit_id).execute()

        morceaux = decouper_texte(texte_audit)[:TAILLE_MAX_CHUNKS_PAR_AUDIT]
        lignes = []
        for morceau in morceaux:
            if not morceau.strip():
                continue
            embedding = vectoriser(morceau)
            lignes.append(
                {
                    "audit_id": audit_id,
                    "proprietaire_id": proprietaire_id,
                    "contenu": morceau,
                    "embedding": embedding,
                }
            )
        if lignes:
            supabase.table("audits_matiere_chunks").insert(lignes).execute()

        logging.info(f"Audit matière {matiere_id} écrit avec succès ({len(lignes)} chunks).")
        return True
    except Exception as e:
        logging.error(f"ERREUR audit matière {matiere_id} : {e}")
        return False


def executer_audits_du_lundi() -> None:
    """Point d'entrée appelé par la boucle planificatrice
    (api/main.py:_boucle_planificateur_audit_programme, toutes les 6h,
    tous les jours -- c'est CETTE fonction qui vérifie qu'on est bien
    lundi, pas la boucle). Parcourt toutes les matières qui n'ont pas
    encore été auditées cette semaine et lance leur audit, une par une,
    best-effort (une matière en échec ne bloque jamais les suivantes)."""
    if datetime.now(timezone.utc).weekday() != 0:  # 0 = lundi
        return

    try:
        matieres = supabase.table("matieres").select("*, programmes!inner(proprietaire_id)").execute().data or []
    except Exception as e:
        logging.error(f"ERREUR lecture matières pour audits du lundi : {e}")
        return

    il_y_a_6_jours = datetime.now(timezone.utc) - timedelta(days=6)
    try:
        deja_faits = (
            supabase.table("audits_matiere")
            .select("matiere_id, derniere_execution")
            .gte("derniere_execution", il_y_a_6_jours.isoformat())
            .execute()
            .data
            or []
        )
    except Exception as e:
        logging.error(f"ERREUR lecture audits déjà faits cette semaine : {e}")
        deja_faits = []
    matiere_ids_deja_faits = {d["matiere_id"] for d in deja_faits}

    for matiere in matieres:
        if matiere["id"] in matiere_ids_deja_faits:
            continue
        proprietaire_id = matiere.get("programmes", {}).get("proprietaire_id")
        if not proprietaire_id:
            continue
        ligne = dict(matiere)
        ligne.pop("programmes", None)
        executer_audit_matiere(ligne, proprietaire_id)


def chercher_audits_programme(question: str, user_id: str, match_count: int = 3) -> list:
    """Recherche sémantique dans les audits de matière de `user_id` --
    même pattern que core/retriever.py:chercher_candidats et
    core/bibliotheque_rag.py:chercher_bibliotheque, table dédiée. Renvoie
    une liste de {contenu, similarite}, triée par pertinence. Best-effort
    : liste vide en cas d'échec ou d'absence de user_id (pas de session
    connectée), jamais d'exception propagée."""
    if not user_id:
        return []
    try:
        vecteur = vectoriser(question, task_type="RETRIEVAL_QUERY")
    except Exception as e:
        logging.error(f"ERREUR VECTORISATION audits programme (Gemini) : {e}")
        return []
    try:
        return (
            supabase.rpc(
                "recherche_audits_programme",
                {"query_embedding": vecteur, "match_count": match_count, "p_proprietaire_id": user_id},
            )
            .execute()
            .data
            or []
        )
    except Exception as e:
        logging.error(f"ERREUR SUPABASE RPC recherche_audits_programme (user_id={user_id}) : {e}")
        return []


def lister_mes_programmes_legers(user_id: str) -> list:
    """Liste légère (niveau/nom uniquement, pas la structure complète) des
    programmes de l'étudiant -- injectée automatiquement dans le prompt
    système (voir core/main.py), pour que l'IA sache que la section
    existe sans avoir à tout charger d'un coup. Best-effort : liste vide
    en cas d'échec ou d'absence de user_id."""
    if not user_id:
        return []
    try:
        return (
            supabase.table("programmes")
            .select("id, niveau, nom")
            .eq("proprietaire_id", user_id)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logging.error(f"ERREUR lecture liste légère programmes (user_id={user_id}) : {e}")
        return []

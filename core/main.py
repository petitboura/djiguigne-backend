import os
import json
import logging
import base64
import re
import concurrent.futures
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from groq import Groq
from google import genai
from google.genai import types
from supabase import create_client
from configuration import get_system_prompt
from contenu_dynamique_matiere import agent_a_contenu_dynamique, resoudre_system_prompt as resoudre_system_prompt_matiere
from comportements_etudiants import lister_comportements as lister_comportements_etudiant
from retriever import chercher_candidats
from mcp_tools import lister_tous_les_outils, lister_outils_autorises_pour_agent, appeler_outil
from registre_outils import OUTILS_SENSIBLES, OUTILS_AUTONOMES
from fournisseurs_llm import generer_reponse_premium

logging.basicConfig(level=logging.INFO)


def get_secret(key):
    return os.environ.get(key)


SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_SECRET = get_secret("SUPABASE_SECRET")
supabase = create_client(SUPABASE_URL, SUPABASE_SECRET)

GROQ_PRIMARY = "openai/gpt-oss-120b"
GOOGLE_MODEL = "gemini-2.5-flash"
GROQ_FALLBACKS = [
    "llama-3.3-70b-versatile",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    # llama-4-scout-17b-16e-instruct et qwen3-32b retires par Groq le
    # 17/06/2026 (voir console.groq.com/docs/deprecations) -- 404
    # systematique, retires de la chaine le 26/07/2026. llama-3.1-8b-instant
    # les remplace en TOUT dernier recours Groq (avant Gemini) : c'est le
    # modele le plus permissif du plan gratuit (le moins susceptible d'etre
    # a court de quota quand tout le reste sature), mais nettement moins
    # capable -- voir MODELES_QUALITE_REDUITE plus bas, qui sert a prevenir
    # l'utilisateur quand CE modele precis a genere la reponse, pour ne pas
    # laisser juger la qualite de la plateforme sur lui.
    "llama-3.1-8b-instant",
]

# Modeles de secours dont la qualite de reponse est nettement en retrait par
# rapport a GROQ_PRIMARY (utilises seulement quand tout le reste a echoue) --
# quand un de ces modeles repond, on le signale explicitement a l'utilisateur
# (evenement "meta", voir _agent_groq et le frontend) plutot que de laisser
# une reponse plus faible passer pour une reponse normale de la plateforme.
MODELES_QUALITE_REDUITE = {"llama-3.1-8b-instant"}
MESSAGE_ERREUR = "Désolé, je rencontre un souci technique pour répondre. Merci de réessayer dans un instant."

# Modération d'entrée (25/07) : verifie le message BRUT de l'utilisateur avant
# tout le reste. IMPORTANT : Llama Guard 4 (meta-llama/llama-guard-4-12b)
# a ete retire par Groq (deprecation du 10/02/2026, voir
# console.groq.com/docs/deprecations) -- constate en prod le 25/07 (fail-
# open, donc rien n'etait bloque depuis le debut, gpt-oss-120b refusait
# parfois tout seul en anglais sur son propre entrainement de securite,
# d'ou la confusion initiale). Remplace par openai/gpt-oss-safeguard-20b,
# le modele recommande par Groq -- fonctionne differemment : "bring your
# own policy", on lui fournit notre propre politique de moderation
# (POLITIQUE_MODERATION plus bas) en role "system", et il repond en JSON
# structure {"violation": 0|1, "category": "...", "rationale": "..."} au
# lieu du simple "safe"/"unsafe" de Llama Guard (voir
# console.groq.com/docs/content-moderation). Demande Bourama (25/07) :
# uniquement l'entree pour l'instant (pas la sortie), pour limiter le
# surcout en tokens.
MODELE_MODERATION = "openai/gpt-oss-safeguard-20b"
POLITIQUE_MODERATION = """# Politique de modération -- messages d'étudiants vers un assistant IA éducatif

## Catégories de violation
- violence : instructions ou encouragement à la violence, fabrication d'armes ou d'explosifs.
- haine : contenu haineux ou discriminatoire visant un groupe protégé.
- sexuel : contenu sexuel explicite, ou impliquant des mineurs sous quelque forme que ce soit.
- automutilation : encouragement ou instructions de suicide/automutilation.
- illegal : instructions pour des activités clairement illégales (drogues dures, piratage malveillant, fraude...).
- harcelement : insultes graves ou harcèlement ciblé envers une personne précise.

## Ce qui N'EST PAS une violation (à laisser passer)
- Questions scolaires/académiques, même sur des sujets sensibles en soi (histoire des guerres, chimie de base, biologie, philosophie...).
- Langage familier, frustration ou grossièretés légères sans intention de nuire à quelqu'un.
- Demandes créatives ou hypothétiques clairement encadrées (devoirs, fiction, débat argumenté).
- Un vrai JSON ou du code demandé explicitement par l'étudiant.

## Format de réponse (JSON uniquement, rien d'autre)
{"violation": 0 ou 1, "category": "<une des catégories ci-dessus ou null si aucune>", "rationale": "<explication en une phrase>"}
"""
MESSAGE_CONTENU_BLOQUE = "Je ne peux pas répondre à ce message. Reformule ta question autrement, je suis là pour t'aider !"

# Valeur de repli si le secret AGENT_ID n'est pas defini pour ce deploiement
# (doit rester alignee avec AGENT_ID_PAR_DEFAUT dans retriever.py).
AGENT_ID_PAR_DEFAUT = "tutorat-maths"

# Au-dela de ce nombre de messages non resumes (table conversations), on
# redemande un resume condense au modele plutot que d'empiler indefiniment
# l'historique brut dans conversation_summaries.
SEUIL_RESUME_MESSAGES = 20
MODELE_RESUME = "llama-3.1-8b-instant"  # quota TPM separe de la cascade principale (llama-3.3-70b-versatile), evite la contention

# Profil utilisateur dynamique par agent (2026-07-21, voir
# agents.profil_utilisateur_schema et _mettre_a_jour_profil_utilisateur_si_besoin
# plus bas). Seuil plus bas que SEUIL_RESUME_MESSAGES : contrairement au
# resume memoire (qui compte TOUS les messages de l'utilisateur, tous agents
# confondus), celui-ci compte seulement les messages avec CET agent -- ils
# s'accumulent donc plus lentement, un seuil identique mettrait
# potentiellement des semaines a se declencher pour un agent utilise
# occasionnellement.
SEUIL_PROFIL_MESSAGES = 10
MODELE_PROFIL = "llama-3.1-8b-instant"  # meme raison que MODELE_RESUME : quota TPM separe

# Routeur d'outils (2026-07-28, demande Bourama) : premier appel LLM
# séparé, rapide, qui juge quels outils seraient pertinents pour la
# question -- voir _router_outils plus bas. Tâche de classification
# simple (pas besoin de raisonnement) -- llama-3.1-8b-instant plutôt que
# MODELE_PROFIL/MODELE_RESUME (llama-3.3-70b-versatile) : demande
# explicite de Bourama (28/07) : "un petit rapide et open source, le
# plus rapide possible" -- ce modèle est déjà le plus rapide de la
# cascade Groq existante (voir GROQ_FALLBACKS).
MODELE_ROUTEUR_OUTILS = "llama-3.1-8b-instant"

# D'apres la doc Groq (console.groq.com/docs/reasoning), le parametre
# reasoning_effort n'est reconnu que par certains modeles (GPT-OSS 20B/120B,
# Qwen 3). Les autres modeles de GROQ_FALLBACKS (ex: llama-3.3-70b-versatile,
# llama-3.1-8b-instant) ne sont PAS des modeles de raisonnement : leur
# envoyer ce parametre risque une erreur API plutot qu'un simple no-op. On
# ne l'active donc que pour les modeles confirmes compatibles -- ET la
# valeur qui desactive/minimise le raisonnement DIFFERE selon la famille :
# "none" pour Qwen 3 (raisonnement desactivable), mais GPT-OSS exige
# obligatoirement low/medium/high (pas de "none") -- bug reel trouve le
# 26/07/2026 : gpt-oss-20b recevait "none" et echouait a CHAQUE appel avec
# une erreur 400 "`reasoning_effort` must be one of `low`, `medium`, or
# `high`", le rendant inutilisable comme filet de secours depuis le debut.
MODELES_AVEC_REASONING_EFFORT = {
    "openai/gpt-oss-20b": "low",       # pas de "none" chez GPT-OSS -- "low" pour rester rapide
    "openai/gpt-oss-120b": "low",
    "qwen/qwen3.6-27b": "none",        # Qwen 3 peut vraiment desactiver le raisonnement
}

# Nombre maximum d'aller-retours "outil" autorisés pour une seule question,
# pour éviter qu'un modèle ne boucle indéfiniment sur le même outil.
MAX_ETAPES_OUTILS = 5

def _verifier_message_utilisateur(message: str) -> tuple[bool, str | None]:
    """
    Verifie un message via gpt-oss-safeguard-20b + POLITIQUE_MODERATION
    (voir plus haut -- remplace Llama Guard 4, retire par Groq). Retourne
    (est_sur: bool, categorie: str|None -- presente seulement si
    est_sur=False). En cas d'erreur reseau/API OU si le JSON renvoye est
    illisible, on laisse passer plutot que de bloquer tout le chat pour un
    souci technique isole sur CE modele de moderation (pas le modele
    principal) -- (True, None) avec un log d'avertissement.
    """
    try:
        client = Groq(api_key=get_secret("GROQ_API_KEY"), max_retries=0, timeout=8.0)
        completion = client.chat.completions.create(
            model=MODELE_MODERATION,
            messages=[
                {"role": "system", "content": POLITIQUE_MODERATION},
                {"role": "user", "content": message},
            ],
            reasoning_effort="low",  # priorite a la latence, c'est un feu vert/rouge avant le vrai appel
        )
        resultat = json.loads(completion.choices[0].message.content or "{}")
        if not resultat.get("violation"):
            return True, None
        return False, resultat.get("category")
    except Exception as e:
        logging.warning(f"Modération d'entrée indisponible (gpt-oss-safeguard), message laissé passer : {e}")
        return True, None


def _ressemble_a_du_json_casse(texte: str) -> bool:
    """
    Heuristique pour un bug Groq connu et non resolu sur gpt-oss-120b (voir
    community.groq.com/t/670 -- "Reasoning tokens and gibberish output
    appearing in responses despite configuration to hide reasoning") : le
    modele melange parfois des arguments d'appel d'outil (JSON brut) dans
    delta.content (le texte de reponse visible) au lieu de passer par
    delta.tool_calls comme prevu. Plus frequent avec 3+ outils actifs ou une
    conversation longue -- notre cas avec Notion/Wolfram/Tavily. Signale par
    Bourama (24/07) : "souvent il donne dans le chat le json qu'il reçoit".
    Pas un bug corrige par un parametre (arrive meme avec reasoning_format=
    "hidden" d'apres les rapports) -- on ne peut que le detecter et masquer
    le debut suspect plutot que l'afficher tel quel a l'utilisateur.

    IMPORTANT (retour Bourama, 25/07) : ne PAS juste tester "ca commence par
    { ou [" -- si l'utilisateur demande explicitement un vrai JSON ("donne-moi
    un JSON avec..."), sa reponse legitime commence pareil et serait masquee
    a tort. On exige donc en plus la signature precise d'un appel d'outil
    Groq rate (les cles "name" ET "arguments" pres du debut, la structure
    interne que Groq utilise pour le tool calling) -- un vrai JSON demande
    par l'utilisateur a quasiment jamais ces deux cles precises ensemble.
    """
    debut = texte.lstrip()
    if not (debut.startswith("{") or debut.startswith("[")):
        return False
    return '"name"' in debut and '"arguments"' in debut


def _urls_generation_manquantes(reponse_texte: str, messages_agent) -> list:
    """
    Filet de securite (31/07, demande Bourama) : quand GROQ_PRIMARY sature
    son quota TPM, la cascade bascule sur un modele de secours (ex.
    llama-3.3-70b-versatile) pour rediger la reponse finale -- avec le
    meme contexte, y compris le resultat d'un outil de generation
    (image/pdf/code...) deja execute ce tour-ci. Ce modele plus simple
    "oublie" parfois de reprendre le lien dans sa reponse alors que le
    fichier a bien ete genere avec succes (l'appel outil, lui, n'a pas
    echoue). Cette fonction repere ces URLs "orphelines" : presentes dans
    un resultat d'outil de CE TOUR mais absentes du texte final envoye a
    l'utilisateur -- pour qu'on puisse les rajouter nous-memes plutot que
    de dependre du modele.
    """
    urls_manquantes = []
    for message in reversed(messages_agent):
        if message.get("role") != "tool":
            break  # les messages "tool" d'un meme tour sont toujours groupes en fin de liste
        contenu = message.get("content")
        if not isinstance(contenu, str) or not _ressemble_a_une_simple_url(contenu):
            continue
        correspondance = re.search(r"https?://\S+", contenu)
        if not correspondance:
            continue
        url = correspondance.group(0).rstrip(").,;\"'")
        if url and url not in reponse_texte and url not in urls_manquantes:
            urls_manquantes.append(url)
    return urls_manquantes


def _completer_liens_manquants(reponse_accumulee, messages_agent):
    """
    A appeler juste apres une reponse finale reussie (voir
    _urls_generation_manquantes) : si des liens de fichiers generes ce
    tour-ci manquent dans la reponse, les rajoute a la fois dans le texte
    envoye a l'utilisateur ("reponse", pour l'affichage immediat) ET dans
    `reponse_accumulee` (pour que l'historique sauvegarde en garde trace).
    Retourne l'evenement a yielder, ou None si rien ne manquait.
    """
    texte_actuel = "".join(reponse_accumulee)
    manquantes = _urls_generation_manquantes(texte_actuel, messages_agent)
    if not manquantes:
        return None
    ajout = "".join(f"\n\n[Voir le fichier généré]({url})" for url in manquantes)
    reponse_accumulee.append(ajout)
    return {"type": "reponse", "texte": ajout}


def _ressemble_a_une_simple_url(contenu: str) -> bool:
    """
    Vrai si le resultat d'un outil n'est (essentiellement) qu'un lien nu,
    comme le renvoient generer_image/generer_document/generer_code/
    generer_site_zip/deployer_site... -- typiquement une courte phrase
    d'accompagnement suivie d'une URL, sans structure JSON. Sert a
    exclure ces resultats de _debut_provient_d_un_resultat_outil : les
    reutiliser dans la reponse est le comportement normal et voulu, pas
    une fuite a masquer.
    """
    c = contenu.strip()
    return ("http://" in c or "https://" in c) and "{" not in c and "\"name\"" not in c


def _debut_provient_d_un_resultat_outil(debut: str, messages_agent) -> bool:
    """
    Deuxieme cas signale par Bourama (25/07), distinct de
    _ressemble_a_du_json_casse : le modele recopie parfois tel quel le
    JSON BRUT renvoye par un outil (GitHub, Notion, Tavily, Wolfram...)
    comme si c'etait sa reponse, au lieu de le resumer en langage naturel.
    Contrairement au bug d'appel d'outil rate, ce JSON n'a pas forcement
    les cles "name"/"arguments" -- sa forme depend entierement de l'outil
    source, donc pas de pattern generique fiable. On compare plutot
    directement au texte des resultats d'outils recus DANS CE TOUR
    (messages_agent, role="tool", toujours groupes juste avant l'appel
    Groq courant -- voir _traiter_appels) : si le debut de la reponse est
    un extrait verbatim d'un de ces resultats, c'est une recopie brute,
    peu importe l'outil ou le format.
    """
    debut = debut.strip()
    if len(debut) < 15:
        return False
    for message in reversed(messages_agent):
        if message.get("role") != "tool":
            break  # les messages "tool" d'un meme tour sont toujours groupes en fin de liste
        contenu = message.get("content")
        # CORRECTION (31/07, signalee par Bourama -- lien image/pdf tronque
        # a l'affichage) : l'ancienne comparaison (`debut[:40] in contenu`)
        # declenchait un faux positif des qu'une URL renvoyee par un outil
        # de generation (image/pdf/code...) etait reutilisee -- normalement
        # -- par le modele dans sa reponse markdown : cette URL apparait
        # par definition dans le contenu de l'outil, meme quand le modele
        # l'integre proprement dans une phrase. Ancrer la comparaison sur
        # le DEBUT du contenu de l'outil ne suffit pas non plus : quand le
        # buffer de streaming est coupe pile au debut de l'URL (cf
        # _position_sure_pour_flush), ce debut coincide quand meme avec le
        # debut du resultat de l'outil. La vraie distinction est donc :
        # un resultat d'outil qui n'est QU'une URL nue (generer_image,
        # generer_document, generer_code...) est fait pour etre reutilise
        # tel quel -- ce n'est jamais une "fuite" -- alors qu'un resultat
        # structure (JSON de GitHub/Notion/Tavily/Wolfram...) recopie
        # verbatim, lui, est bien le bug vise ici. On ignore donc les
        # resultats d'outils qui ne sont qu'un lien.
        if not isinstance(contenu, str):
            continue
        if _ressemble_a_une_simple_url(contenu):
            continue
        if debut[:40] in contenu:
            return True
    return False


_RE_DEBUT_TOOL_CODE = re.compile(r"```\s*tool_code\b", re.IGNORECASE)


def _trouver_debut_tool_code(texte: str):
    """
    Troisieme cas de fuite signale par Bourama (28/07) : au lieu d'un JSON
    casse ou d'une recopie de resultat, le modele ecrit carrement un FAUX
    appel d'outil sous forme de bloc de code cloture ```TOOL_CODE ... ```
    (ex: print(generer_image(prompt='...'))) au lieu d'utiliser le vrai
    mecanisme de tool calling de l'API.

    CORRECTION (29/07, signalee par Bourama) : l'ancienne version
    (_ressemble_a_un_pseudo_appel_outil) ne regardait que le tout DEBUT du
    texte -- si une phrase legitime precedait le faux bloc dans la meme
    fenetre de streaming (ex: "Je lance les operations pour le reste.\n\n
    ```TOOL_CODE"), la verification voyait une phrase normale en premier et
    laissait tout passer, faux bloc inclus. Cette fonction cherche desormais
    le marqueur ```TOOL_CODE N'IMPORTE OU dans le texte et retourne sa
    position (ou None si absent), pour permettre de ne masquer QUE le bloc
    lui-meme -- pas la phrase legitime qui le precede.
    """
    m = _RE_DEBUT_TOOL_CODE.search(texte)
    return m.start() if m else None


_RE_DEBUT_CALL_OUTIL = re.compile(r"\bcall:[A-Za-z_][A-Za-z0-9_]*\{")


def _trouver_debut_call_outil(texte: str):
    """
    Quatrieme cas de fuite signale par Bourama (29/07, captures d'ecran a
    l'appui), distinct du bloc ```TOOL_CODE``` : le modele ecrit un faux
    appel d'outil directement dans le texte visible, sans backticks ni
    print(), sous la forme "call:nom_outil{...json...}", ex :
        call:generer_image{"prompt":"Un chat elegant..."}
        call:tavily_search{"query":"informations sur les chats"}
    Ni _trouver_debut_tool_code (cherche des backticks) ni
    _reponse_suspecte_generique (cherche des cles "name"/"arguments" dans
    un JSON qui commence en debut de fenetre) ne detectaient ce motif --
    confirme en le testant contre les 3 captures d'ecran recues. Cherche
    le marqueur "call:nom_outil{" n'importe ou dans le texte et retourne
    sa position (ou None si absent) ; meme logique de decoupage precis
    que _trouver_debut_tool_code (ne masque que le faux appel, jamais le
    texte legitime autour).
    """
    m = _RE_DEBUT_CALL_OUTIL.search(texte)
    return m.start() if m else None


def _position_fin_bloc_call_outil(bloc_buffer: str):
    """
    A appeler uniquement sur un bloc_buffer qui commence par un motif
    detecte via _trouver_debut_call_outil. Compte les accolades une par
    une (au lieu de s'arreter a la premiere "}" venue) pour gerer un
    argument JSON lui-meme imbrique, ex: call:generer_code{"nom_projet":
    "x","fichiers":{"main.py":"..."}} -- s'arreter a la premiere "}"
    couperait avant la vraie fin.

    Si un autre "call:nom{" enchaine juste apres (espaces/retours a la
    ligne autorises entre les deux, comme les plusieurs print() d'un
    bloc TOOL_CODE), il est absorbe dans le meme bloc a masquer plutot
    que de rouvrir un nouveau passage "reponse" au milieu.

    Retourne la position de fin (exclusive) une fois sur qu'aucun autre
    appel n'enchaine juste apres, ou None si le bloc est encore en cours
    de reception (JSON pas complet, ou fin de fragment ambigue en plein
    milieu d'espaces -- on attend alors la suite du streaming plutot que
    de risquer une coupure trop tot).
    """
    position = 0
    while True:
        m = _RE_DEBUT_CALL_OUTIL.match(bloc_buffer, position)
        if not m:
            return position if position > 0 else None
        profondeur = 0
        fin_accolade = None
        i = m.end() - 1  # position du "{" ouvrant qui vient d'etre matche
        while i < len(bloc_buffer):
            if bloc_buffer[i] == "{":
                profondeur += 1
            elif bloc_buffer[i] == "}":
                profondeur -= 1
                if profondeur == 0:
                    fin_accolade = i + 1
                    break
            i += 1
        if fin_accolade is None:
            return None  # JSON de cet appel pas encore complet
        suite = bloc_buffer[fin_accolade:]
        suite_sans_espaces = suite.lstrip(" \t\r\n")
        if not suite_sans_espaces:
            return None  # ambigu : un autre appel pourrait suivre juste apres, on attend la suite
        if _RE_DEBUT_CALL_OUTIL.match(suite_sans_espaces):
            position = fin_accolade + (len(suite) - len(suite_sans_espaces))
            continue
        return fin_accolade


def _reponse_suspecte_generique(buffer_debut: str, messages_agent) -> bool:
    """Les 2 filets de securite "tout ou rien" contre les bugs Groq connus
    (JSON casse, recopie brute d'un resultat d'outil) -- le 3e cas (faux
    bloc TOOL_CODE) est gere a part via _trouver_debut_tool_code, qui
    permet de ne masquer que le bloc precis plutot que tout le passage."""
    return (
        _ressemble_a_du_json_casse(buffer_debut)
        or _debut_provient_d_un_resultat_outil(buffer_debut, messages_agent)
    )


SEUIL_VERIF_JSON = 60
# Marge de securite (29/07, elargie le 29/07 pour couvrir aussi le motif
# "call:nom_outil{", voir _trouver_debut_call_outil). Le plus long nom
# d'outil enregistre (ex: "generer_document_powerpoint") donne un motif
# "call:generer_document_powerpoint{" d'environ 34 caracteres -- largement
# plus long que "```TOOL_CODE" (~12 caracteres). Si on flush tout le
# buffer des que SEUIL_VERIF_JSON est atteint, on risque de couper un
# motif en deux pile au mauvais moment (ex: le buffer contient juste
# "call:generer_doc" quand le seuil est atteint) -- le debut partirait en
# "reponse" normale et le reste, arrivant dans le fragment suivant, ne
# serait plus jamais reconnu comme un faux appel (le debut manquant est
# deja parti). On garde donc toujours les RESERVE_SUFFIXE derniers
# caracteres du buffer en attente, jamais flushes tant qu'on n'est pas
# sur qu'ils ne sont pas le debut d'un motif.
RESERVE_SUFFIXE = 50


_CARACTERES_FIN_URL = (" ", "\n", "\t", ")", "]", '"', "'")


def _position_sure_pour_flush(buffer: str, position_max: int) -> int:
    """
    Renvoie une position <= position_max a laquelle on peut flusher sans
    risquer de couper une URL en plein milieu (bug signale par Bourama le
    31/07 : lien d'image/pdf tronque et casse a l'affichage cote
    frontend). Cherche la derniere occurrence de "http" avant
    position_max ; si rien entre ce "http" et position_max ne ressemble a
    une fin d'URL (espace, retour a la ligne, guillemet, parenthese ou
    crochet fermant), on considere l'URL encore en cours de formation et
    on recule le point de flush jusqu'a son debut -- elle sera flushee
    d'un seul bloc une fois complete, au prochain passage.
    """
    dernier_http = buffer.rfind("http", 0, position_max)
    if dernier_http == -1:
        return position_max
    segment = buffer[dernier_http:position_max]
    if any(caractere in segment for caractere in _CARACTERES_FIN_URL):
        return position_max  # l'URL semble deja terminee avant position_max
    return dernier_http


def _nouvel_etat_filtre_texte():
    """Etat initial pour _traiter_fragment_texte / _finaliser_fragment_texte
    (voir ces fonctions). Un etat par passage de streaming Groq."""
    return {
        "phase": "avant",   # "avant" (texte normal en cours de verification) ou "dans_bloc" (faux appel en cours)
        "buffer": "",       # texte en attente de decision, phase "avant"
        "bloc_buffer": "",  # texte du faux bloc en cours, phase "dans_bloc"
        "type_bloc": None,  # "tool_code" ou "call_outil" -- decide comment reperer la fin du bloc
        "tool_code_detecte": False,  # devient True des qu'un faux bloc a ete vu (partiel ou complet)
    }


def _traiter_fragment_texte(etat, fragment, messages_agent):
    """
    Traite un nouveau fragment de texte recu du streaming Groq, en isolant
    precisement un eventuel faux appel d'outil -- soit un bloc ```TOOL_CODE
    ... ``` (voir _trouver_debut_tool_code), soit un motif call:nom{...}
    (voir _trouver_debut_call_outil) : tout ce qui est AVANT le faux appel
    est affiche normalement ("reponse"), le faux appel lui-meme est masque
    ("raisonnement"), et tout ce qui vient APRES redevient visible
    normalement -- au lieu d'un comportement "tout ou rien" ou une fois
    suspect, tout le reste du passage restait cache.

    Retourne la liste des evenements a yield. Mute `etat` en place.
    """
    evenements = []

    if etat["phase"] == "dans_bloc":
        etat["bloc_buffer"] += fragment
        if etat["type_bloc"] == "call_outil":
            fin = _position_fin_bloc_call_outil(etat["bloc_buffer"])
        else:
            fin = etat["bloc_buffer"].find("```", 3)  # cherche la fermeture APRES l'ouvrant (3 premiers caracteres)
            fin = None if fin == -1 else fin + 3
        if fin is None:
            return evenements  # bloc toujours en cours, rien a afficher pour l'instant
        evenements.append({"type": "raisonnement", "texte": etat["bloc_buffer"][:fin]})
        reste = etat["bloc_buffer"][fin:]
        etat["phase"] = "avant"
        etat["buffer"] = ""
        etat["bloc_buffer"] = ""
        etat["type_bloc"] = None
        if reste:
            evenements.extend(_traiter_fragment_texte(etat, reste, messages_agent))
        return evenements

    etat["buffer"] += fragment
    position_tool_code = _trouver_debut_tool_code(etat["buffer"])
    position_call_outil = _trouver_debut_call_outil(etat["buffer"])
    positions = [
        (p, t) for p, t in ((position_tool_code, "tool_code"), (position_call_outil, "call_outil")) if p is not None
    ]
    if positions:
        position, type_bloc = min(positions, key=lambda pt: pt[0])
        avant = etat["buffer"][:position]
        if avant:
            evenements.append({"type": "reponse", "texte": avant})
        etat["tool_code_detecte"] = True
        etat["phase"] = "dans_bloc"
        etat["type_bloc"] = type_bloc
        etat["bloc_buffer"] = etat["buffer"][position:]
        etat["buffer"] = ""
        evenements.extend(_traiter_fragment_texte(etat, "", messages_agent))
        return evenements

    if len(etat["buffer"]) >= SEUIL_VERIF_JSON + RESERVE_SUFFIXE:
        # On ne flush que le buffer MOINS la marge de securite finale, pour
        # ne jamais couper un motif en cours de formation (voir le
        # commentaire de RESERVE_SUFFIXE plus haut).
        position_max = len(etat["buffer"]) - RESERVE_SUFFIXE
        # SECURITE SUPPLEMENTAIRE (31/07, signalee par Bourama -- lien
        # image/pdf tronque a l'affichage) : ne jamais flusher en plein
        # milieu d'une URL non terminee (ex. "...supabase.co/storage/v1/
        # object" sans le reste du chemin ni la parenthese fermante du
        # markdown) -- meme si la classification reponse/raisonnement est
        # correcte, couper une URL en deux casse le rendu du lien ou de
        # l'image cote frontend. Si le dernier "http" avant position_max
        # ne semble pas encore termine (aucun espace/saut de ligne/") ]"
        # apres), on recule le point de flush jusqu'au debut de cette URL
        # et on attend la suite du streaming pour la flusher d'un bloc.
        position_flush = _position_sure_pour_flush(etat["buffer"], position_max)
        a_flusher = etat["buffer"][:position_flush]
        etat["buffer"] = etat["buffer"][position_flush:]
        if a_flusher:
            if _reponse_suspecte_generique(a_flusher, messages_agent):
                evenements.append({"type": "raisonnement", "texte": a_flusher})
            else:
                evenements.append({"type": "reponse", "texte": a_flusher})
    return evenements


def _finaliser_fragment_texte(etat, messages_agent):
    """A appeler une fois le flux Groq termine : vide le reliquat de
    `etat`, quelle que soit la phase en cours. Si le flux s'est arrete en
    plein milieu d'un faux appel (TOOL_CODE ou call:outil{...}, fermeture
    jamais recue), le reliquat est tout de meme masque et
    tool_code_detecte reste True."""
    evenements = []
    if etat["phase"] == "dans_bloc":
        if etat["bloc_buffer"]:
            evenements.append({"type": "raisonnement", "texte": etat["bloc_buffer"]})
        etat["tool_code_detecte"] = True
        etat["bloc_buffer"] = ""
        etat["type_bloc"] = None
    elif etat["buffer"]:
        if _reponse_suspecte_generique(etat["buffer"], messages_agent):
            evenements.append({"type": "raisonnement", "texte": etat["buffer"]})
        else:
            evenements.append({"type": "reponse", "texte": etat["buffer"]})
        etat["buffer"] = ""
    return evenements
# Nouvel outil = ajouter une ligne ici (optionnel, sinon le nom brut s'affiche).
NOMS_OUTILS_LISIBLES = {
    "tavily_search": "Recherche sur le web",
    "tavily_extract": "Lecture d'une page web",
    "tavily_crawl": "Exploration d'un site web",
    "tavily_map": "Cartographie d'un site web",
    "tavily_research": "Recherche approfondie",
    "notion-search": "Recherche dans ton Notion",
    "notion-fetch": "Lecture d'une page Notion",
    "notion-create-pages": "Création d'une page Notion",
    "notion-update-page": "Modification d'une page Notion",
    "explorer_depot_github": "Exploration d'un dépôt GitHub",
    "lire_fichier_depot_github": "Lecture d'un fichier GitHub",
    "modifier_fichier_depot_github": "Modification d'un fichier GitHub",
}


def _construire_parts_gemini(texte, images=None):
    """
    Construit la liste `parts` d'un message Gemini. Le texte est toujours
    présent ; `images` (si fourni) est une liste de tuples
    (bytes, mime_type), ajoutés en inline_data base64 -- format REST
    attendu par google-genai pour du contenu multimodal, voir
    https://ai.google.dev/gemini-api/docs/vision. Une image (cas simple)
    ou plusieurs (frames vidéo, voir _extraire_frames_video) sont traitées
    de la même façon.
    """
    parts = [{"text": texte}]
    for image_bytes, image_mime in (images or []):
        parts.append({
            "inline_data": {
                "mime_type": image_mime or "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            }
        })
    return parts


def _telecharger_image(image_url):
    """
    Télécharge l'image pointée par `image_url` (URL publique Supabase
    Storage, voir api/uploads.py:uploader_image_chat) pour l'envoyer en
    base64 à Gemini. On ne passe jamais l'URL telle quelle à Gemini : les
    URLs Supabase ne sont pas des URI Google Cloud Storage, `Part.from_uri`
    ne les accepterait pas.
    """
    reponse = requests.get(image_url, timeout=15)
    reponse.raise_for_status()
    return reponse.content, reponse.headers.get("content-type", "image/jpeg")


REGEX_URL = re.compile(r"https?://[^\s<>\"']+")
LONGUEUR_MAX_TEXTE_URL = 8_000  # caracteres, par lien, pour ne pas saturer le prompt


def _extraire_id_youtube(url):
    match = re.search(r"(?:youtu\.be/|youtube\.com/watch\?v=|youtube\.com/shorts/)([\w-]{11})", url)
    return match.group(1) if match else None


# Connecteur GitHub -- lien public collé dans le message par l'utilisateur
# (ou dépôt privé si connecté via OAuth, voir connexions/oauth_generique.py).
# Quatre formes de lien reconnues :
# - fichier précis : github.com/user/repo/blob/branche/chemin/fichier.py
# - dossier : github.com/user/repo/tree/branche/chemin -> liste NON
#   récursive du contenu à ce niveau (noms + type fichier/dossier), pas le
#   contenu des fichiers -- lire un dossier entier en profondeur est un
#   chantier à part (stratégie de sélection/troncature des fichiers).
# - branche seule : github.com/user/repo/tree/branche -> README de CETTE
#   branche précise (pas forcément la branche par défaut du dépôt).
# - dépôt entier : github.com/user/repo (sans /tree/ ni /blob/) -> README
#   de la branche par défaut.
REGEX_GITHUB_FICHIER = re.compile(
    r"github\.com/([\w.-]+)/([\w.-]+)/blob/([\w.\-/%]+?)/([^/\s]+\.\w+)"
)
REGEX_GITHUB_ARBORESCENCE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/tree/([\w.\-/%]+)")
REGEX_GITHUB_DEPOT = re.compile(r"github\.com/([\w.-]+)/([\w.-]+?)/?(?:\s|$)")


def _lire_github(url, user_id=None):
    """
    Récupère le contenu d'un lien GitHub collé dans le message. Deux
    formats reconnus (fichier précis ou dépôt entier), et TROIS niveaux
    d'authentification possibles pour chacun, du plus au moins privilégié :
    1. Token OAuth de LA PERSONNE connectée (voir
       connexions/oauth_generique.py, obtenir_token_valide("github", ...))
       -- seul niveau donnant accès aux dépôts PRIVÉS. Ajouté le
       2026-07-22 : nécessite que la personne ait connecté son compte
       GitHub ET qu'une GitHub OAuth App existe (GITHUB_CLIENT_ID/SECRET
       sur Railway) -- voir connexions/oauth_generique.py pour la config.
    2. GITHUB_TOKEN de la plateforme (voir plus bas) -- dépôts publics
       uniquement, mais lève la limite de 60 à 5000 requêtes/heure.
    3. Non authentifié -- dépôts publics, 60 requêtes/heure PARTAGÉES
       entre tous les utilisateurs (confirmé limitant en test réel).
    """
    token_utilisateur = None
    if user_id:
        try:
            from connexions.oauth_generique import obtenir_token_valide
            token_utilisateur = obtenir_token_valide("github", user_id)
        except Exception as e:
            logging.error(f"ERREUR LECTURE TOKEN GITHUB (user {user_id}) : {e}")

    m_fichier = REGEX_GITHUB_FICHIER.search(url)
    if m_fichier:
        utilisateur, depot, branche_et_chemin_partiel, nom_fichier = m_fichier.groups()
        chemin_complet = f"{branche_et_chemin_partiel}/{nom_fichier}"
        # Le premier segment de chemin_complet est la branche (main,
        # master...), le reste est le chemin réel dans le dépôt.
        segments = chemin_complet.split("/", 1)
        if len(segments) != 2:
            return None
        branche, chemin_fichier = segments
        raw_url = f"https://raw.githubusercontent.com/{utilisateur}/{depot}/{branche}/{chemin_fichier}"
        # raw.githubusercontent.com accepte un Authorization: Bearer pour
        # les dépôts privés (comportement GitHub, pas garanti stable dans
        # le temps -- à revalider si ce point casse un jour).
        headers = {"Authorization": f"Bearer {token_utilisateur}"} if token_utilisateur else {}
        try:
            reponse = requests.get(raw_url, timeout=10, headers=headers)
            if reponse.status_code != 200:
                logging.warning(f"LECTURE GITHUB ECHOUEE (fichier, statut {reponse.status_code}) : {raw_url}")
                return None
            return reponse.text[:LONGUEUR_MAX_TEXTE_URL]
        except Exception as e:
            logging.error(f"ERREUR LECTURE GITHUB (fichier) {raw_url} : {e}")
            return None

    m_arbo = REGEX_GITHUB_ARBORESCENCE.search(url)
    if m_arbo:
        utilisateur, depot, reste = m_arbo.groups()
        segments = reste.split("/", 1)
        branche = segments[0]
        chemin_dossier = segments[1] if len(segments) > 1 else None
        headers_auth = {"Authorization": f"Bearer {token_utilisateur}"} if token_utilisateur else {}

        if chemin_dossier:
            # Lien de dossier : liste NON récursive du contenu à ce
            # niveau (noms + type), pas le contenu des fichiers -- lire un
            # dossier entier en profondeur nécessiterait une stratégie de
            # sélection/troncature, hors de portée ici.
            api_url = f"https://api.github.com/repos/{utilisateur}/{depot}/contents/{chemin_dossier}?ref={branche}"
            try:
                reponse = requests.get(api_url, timeout=10, headers=headers_auth)
                if reponse.status_code != 200:
                    logging.warning(f"LECTURE GITHUB ECHOUEE (dossier, statut {reponse.status_code}) : {api_url}")
                    return None
                elements = reponse.json()
                if not isinstance(elements, list):
                    # L'API renvoie un objet (pas une liste) si le chemin
                    # pointe en fait vers un fichier, pas un dossier.
                    return None
                lignes = [
                    f"- {e['name']} ({'dossier' if e['type'] == 'dir' else 'fichier'})"
                    for e in elements
                ]
                return f"Contenu du dossier {chemin_dossier} (branche {branche}) :\n" + "\n".join(lignes)
            except Exception as e:
                logging.error(f"ERREUR LECTURE GITHUB (dossier) {api_url} : {e}")
                return None
        else:
            # Lien de branche seule : README de CETTE branche précise,
            # pas forcément la branche par défaut du dépôt.
            api_url = f"https://api.github.com/repos/{utilisateur}/{depot}/readme?ref={branche}"
            headers = {"Accept": "application/vnd.github.raw+json", **headers_auth}
            try:
                reponse = requests.get(api_url, timeout=10, headers=headers)
                if reponse.status_code != 200:
                    logging.warning(f"LECTURE GITHUB ECHOUEE (branche, statut {reponse.status_code}) : {api_url}")
                    return None
                return reponse.text[:LONGUEUR_MAX_TEXTE_URL]
            except Exception as e:
                logging.error(f"ERREUR LECTURE GITHUB (branche) {api_url} : {e}")
                return None

    m_depot = REGEX_GITHUB_DEPOT.search(url)
    if m_depot:
        utilisateur, depot = m_depot.groups()
        api_url = f"https://api.github.com/repos/{utilisateur}/{depot}/readme"
        headers = {"Accept": "application/vnd.github.raw+json"}
        # Priorité : token de la personne connectée (dépôts privés) >
        # GITHUB_TOKEN de la plateforme (dépôts publics, quota levé) >
        # non authentifié (dépôts publics, quota serré). Voir docstring.
        token_github = token_utilisateur or get_secret("GITHUB_TOKEN")
        if token_github:
            headers["Authorization"] = f"Bearer {token_github}"
        try:
            reponse = requests.get(api_url, timeout=10, headers=headers)
            if reponse.status_code != 200:
                logging.warning(f"LECTURE GITHUB ECHOUEE (dépôt, statut {reponse.status_code}) : {api_url}")
                return None
            return reponse.text[:LONGUEUR_MAX_TEXTE_URL]
        except Exception as e:
            logging.error(f"ERREUR LECTURE GITHUB (dépôt) {api_url} : {e}")
            return None

    return None


def _lire_url(url, user_id=None):
    """
    Récupère le contenu textuel d'un lien collé dans le message. Trois cas :
    - YouTube (vidéo) : transcript via youtube-transcript-api, pas de
      scraping HTML -- c'est notre seule "entrée vidéo" pour l'instant,
      limitée aux vidéos YouTube sous-titrées (voir note plus bas, pas de
      vrai traitement vidéo/image par frame).
    - GitHub (fichier ou dépôt) : contenu brut du fichier ou README du
      dépôt, voir _lire_github. `user_id` permet d'utiliser le token
      OAuth de la personne connectée si elle a lié son compte GitHub
      (voir connexions/oauth_generique.py) -- seul moyen de lire un dépôt
      PRIVÉ ; sans connexion, uniquement les dépôts publics (voir
      _lire_github pour le détail des 3 niveaux d'authentification).
    - Page web générique : extraction via trafilatura (garde le texte
      utile, jette nav/pubs/footer).
    Retourne None si l'extraction échoue (lien mort, page protégée, vidéo
    sans sous-titres...) -- on ne bloque jamais le message pour ça, on
    l'envoie tel quel au modèle.
    """
    id_youtube = _extraire_id_youtube(url)
    if id_youtube:
        try:
            # BUG corrigé le 2026-07-20 : même famille de bug que
            # trafilatura.fetch_url(timeout=...) -- youtube-transcript-api
            # 1.x a totalement changé son API par rapport à l'ancienne
            # version que j'avais en tête. `YouTubeTranscriptApi.get_transcript`
            # (méthode statique, résultat = liste de dicts) n'existe plus :
            # il faut instancier la classe et appeler `.fetch()` (méthode
            # d'instance), qui renvoie un objet FetchedTranscript itérable
            # de FetchedTranscriptSnippet (dataclasses avec un attribut
            # `.text`, pas une clé de dict `["text"]`). Confirmé cassé en
            # test réel le 2026-07-20 (lien YouTube collé, aucun contenu
            # récupéré, le modèle répondait qu'il ne pouvait pas voir de
            # vidéos -- comme pour trafilatura, l'exception était avalée
            # silencieusement par le except plus bas).
            from youtube_transcript_api import YouTubeTranscriptApi
            api = YouTubeTranscriptApi()
            transcript = api.fetch(id_youtube, languages=["fr", "en"])
            texte = " ".join(morceau.text for morceau in transcript)
            return texte[:LONGUEUR_MAX_TEXTE_URL]
        except Exception as e:
            logging.error(f"ERREUR TRANSCRIPT YOUTUBE ({url}): {e}")
            return None

    if "github.com" in url:
        # Avant le fallback trafilatura générique : un lien GitHub scrapé
        # comme une page HTML normale donnerait la navigation/sidebar de
        # l'interface GitHub, pas le vrai contenu du fichier/README.
        contenu_github = _lire_github(url, user_id)
        if contenu_github:
            return contenu_github
        # Si _lire_github échoue (dépôt privé, format de lien non
        # reconnu...), on retombe sur trafilatura plutôt que d'abandonner
        # -- au moins la page HTML publique GitHub reste lisible.

    try:
        import trafilatura
        # BUG corrigé le 2026-07-20 : trafilatura 2.1.0 n'a pas de paramètre
        # `timeout` sur fetch_url() (TypeError à CHAQUE appel, silencieux
        # car avalé par le except plus bas -- résultat : cette fonction ne
        # récupérait jamais aucun lien depuis le déploiement initial,
        # confirmé en testant en conditions réelles contre Wikipedia et
        # ia-info.fr, qui échouaient identiquement). Le timeout par défaut
        # de trafilatura reste raisonnable, pas besoin de le personnaliser.
        telechargement = trafilatura.fetch_url(url)
        if not telechargement:
            # Échec SILENCIEUX auparavant (aucun log) -- cas exact vécu le
            # 2026-07-20 : impossible de distinguer depuis les logs si le
            # lien a été bloqué (ex: 429, comme YouTube l'a fait à Claude
            # directement lors du diagnostic), jamais tenté, ou un autre
            # souci. trafilatura n'expose pas le code HTTP ici (fetch_url
            # avale l'erreur en interne), donc on log au moins le fait
            # qu'un téléchargement a été tenté et a échoué.
            logging.warning(f"LECTURE URL ECHOUEE (telechargement vide, ex: bloqué/429/timeout) : {url}")
            return None
        texte = trafilatura.extract(telechargement)
        if not texte:
            logging.warning(f"LECTURE URL ECHOUEE (page téléchargée mais aucun texte extrait, ex: page vide/JS-only) : {url}")
            return None
        return texte[:LONGUEUR_MAX_TEXTE_URL]
    except Exception as e:
        logging.error(f"ERREUR LECTURE URL ({url}): {e}")
        return None


def _enrichir_message_avec_urls(message, user_id=None):
    """
    Détecte les liens collés dans le message utilisateur, récupère leur
    contenu, et l'ajoute en contexte APRÈS le message original (jamais à la
    place) -- le modèle voit toujours la question telle que posée, plus le
    contenu des liens en pièce jointe textuelle. Le message ORIGINAL (sans
    enrichissement) reste ce qui est sauvegardé dans l'historique -- voir
    l'appel à _sauvegarder_echange dans chat(), qui reçoit toujours
    message_utilisateur brut, jamais message_pour_modele.

    `user_id` (2026-07-22) : transmis à _lire_url -> _lire_github pour
    utiliser le token GitHub de la personne si elle a connecté son compte
    (accès aux dépôts privés) -- voir connexions/oauth_generique.py.
    """
    urls = REGEX_URL.findall(message)
    if not urls:
        return message

    logging.info(f"LIEN(S) DETECTE(S) DANS LE MESSAGE : {urls[:3]}")

    blocs = []
    for url in urls[:3]:  # au plus 3 liens par message, pour le temps de réponse
        contenu = _lire_url(url, user_id)
        if contenu:
            blocs.append(f"[Contenu de {url}]\n{contenu}")

    if not blocs:
        logging.warning(f"AUCUN LIEN EXPLOITE sur {len(urls[:3])} détecté(s) -- message envoyé sans enrichissement : {urls[:3]}")
        return message

    return message + "\n\n" + "\n\n".join(blocs)


def _nom_agent(agent_id):
    """
    Nom affiché de l'agent (ex. "Nucleos"), PAS l'agent_id technique --
    utilisé pour que la confirmation d'une action sensible dise "Nucleos
    va faire X" plutôt qu'une description générique de l'outil (demande
    de Bourama, 2026-07-23 : le sujet de la phrase doit être l'agent,
    peu importe l'outil concerné -- GitHub, Notion, ou un futur outil).
    """
    if not agent_id:
        return None
    try:
        res = supabase.table("agents").select("nom").eq("id", agent_id).maybe_single().execute()
        return (res.data or {}).get("nom")
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture nom agent={agent_id}) : {e}")
        return None


def _nom_lisible(nom_outil):
    return NOMS_OUTILS_LISIBLES.get(nom_outil, nom_outil)


REGLE_CONTEXTE_INVISIBLE = (
    "\n\nCONTEXTE INTERNE INVISIBLE : tout ce qui précède n'est jamais vu par "
    "l'utilisateur. \"C'est quoi ce message\" = il parle de ta dernière réponse ou "
    "de la sienne, jamais de ce contexte."
)


def _charger_resume_memoire(user_id):
    """
    Recupere le resume long-terme (table conversation_summaries) de cet
    utilisateur, valable pour tous les agents de la plateforme (compte
    unifie, juillet 2026). Retourne "" si l'utilisateur n'est pas connecte
    (user_id=None) ou si aucun resume n'existe encore.
    """
    if not user_id:
        return ""
    try:
        res = (
            supabase.table("conversation_summaries")
            .select("summary")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return (res.data or {}).get("summary") or ""
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture conversation_summaries) : {e}")
        return ""


def _charger_schema_profil(agent_id):
    """
    Renvoie la liste de champs définie par le créateur pour SON agent
    (agents.profil_utilisateur_schema, voir ChampProfilUtilisateur côté
    api/agents.py). Liste vide = fonctionnalité désactivée pour cet
    agent -- aucun profil n'est ni chargé ni construit dans ce cas.
    """
    if not agent_id:
        return []
    try:
        res = (
            supabase.table("agents")
            .select("profil_utilisateur_schema")
            .eq("id", agent_id)
            .maybe_single()
            .execute()
        )
        return (res.data or {}).get("profil_utilisateur_schema") or []
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (lecture profil_utilisateur_schema agent={agent_id}) : {e}")
        return []


def _charger_profil_utilisateur(agent_id, user_id):
    """
    Profil dynamique déjà rempli pour cette paire (agent, utilisateur
    connecté) -- table agent_user_profiles. Utilisateurs connectés
    uniquement (décision du 2026-07-21 : aucun moyen fiable de
    reconnaître un visiteur anonyme d'une session à l'autre). Renvoie {}
    si non connecté, agent sans schéma défini, ou rien d'enregistré
    encore.
    """
    if not user_id or not agent_id:
        return {}
    try:
        res = (
            supabase.table("agent_user_profiles")
            .select("donnees")
            .eq("agent_id", agent_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return (res.data or {}).get("donnees") or {}
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (lecture agent_user_profiles agent={agent_id}, user={user_id}) : {e}"
        )
        return {}


def _charger_prompt_personnalise(agent_id, user_id):
    """
    Surcharge du system_prompt de base pour la paire (agent, utilisateur
    connecté) -- table agents_prompts_utilisateur (2026-08-06, agents
    "partagés" type Stirux/Lirinus où chaque utilisateur a sa propre
    version). Contrairement au profil dynamique, ne varie pas par
    message : ne change que quand l'utilisateur modifie son prompt.
    Utilisateurs connectés uniquement. Renvoie None si non connecté ou
    si rien n'est enregistré pour cette paire -- _construire_system_prompt
    retombe alors sur le system_prompt de base de l'agent.
    """
    if not user_id or not agent_id:
        return None
    try:
        res = (
            supabase.table("agents_prompts_utilisateur")
            .select("system_prompt")
            .eq("agent_id", agent_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return (res.data or {}).get("system_prompt") or None
    except Exception as e:
        logging.error(
            f"ERREUR SUPABASE (lecture agents_prompts_utilisateur agent={agent_id}, user={user_id}) : {e}"
        )
        return None


def _mettre_a_jour_profil_utilisateur_si_besoin(user_id, agent_id):
    """
    Pendant du profil dynamique à _mettre_a_jour_resume_si_besoin
    ci-dessous, mais scopé à un seul agent (pas tous agents confondus) et
    guidé par un schéma défini par le créateur plutôt que par un résumé
    libre. Ne fait rien si : utilisateur non connecté, agent sans schéma
    défini (profil_utilisateur_schema vide -- cas par défaut, aucun coût
    ajouté pour les agents qui n'utilisent pas cette fonctionnalité), ou
    pas encore assez de nouveaux messages avec CET agent.

    Contrairement à _mettre_a_jour_resume_si_besoin, ne purge PAS les
    messages bruts de `conversations` -- ce n'est pas son rôle (le résumé
    mémoire s'en charge déjà, tous agents confondus) ; lire les mêmes
    lignes deux fois pour deux mécanismes différents ne pose aucun
    problème tant qu'aucun des deux n'écrit sur les données de l'autre.
    Ne bloque jamais la réponse à l'utilisateur : toute erreur est juste
    loguée, jamais remontée à l'appelant.
    """
    if not user_id or not agent_id:
        return
    schema = _charger_schema_profil(agent_id)
    if not schema:
        return
    try:
        messages = (
            supabase.table("conversations")
            .select("role, content, created_at")
            .eq("user_id", user_id)
            .eq("agent_id", agent_id)
            .order("created_at", desc=True)
            .limit(SEUIL_PROFIL_MESSAGES)
            .execute()
        ).data or []

        if len(messages) < SEUIL_PROFIL_MESSAGES:
            return  # pas encore assez de matière avec CET agent

        profil_actuel = _charger_profil_utilisateur(agent_id, user_id)
        messages_recents = "\n".join(
            f"{'Utilisateur' if m['role'] == 'user' else 'Assistant'} : {m['content']}"
            for m in reversed(messages)
        )
        champs_desc = "\n".join(
            f"- {c['nom']} : {c.get('description') or '(pas de description)'}" for c in schema
        )

        prompt_profil = (
            "Tu extrais des informations factuelles sur un utilisateur à partir d'une "
            "conversation, selon un schéma précis défini par le créateur de cet agent. "
            "Réponds UNIQUEMENT avec un objet JSON dont les clés sont EXACTEMENT les "
            "noms de champs ci-dessous (aucune clé en plus, aucune clé en moins). Pour "
            "chaque champ, indique la valeur si elle est clairement déductible de la "
            "conversation, sinon reprends la valeur déjà connue (fournie ci-dessous), "
            "sinon mets une chaîne vide. N'invente rien, ne devine pas au-delà de ce qui "
            "est dit ou clairement impliqué.\n\n"
            f"Champs à extraire :\n{champs_desc}\n\n"
            f"Valeurs déjà connues (à conserver si rien de nouveau) :\n"
            f"{json.dumps(profil_actuel, ensure_ascii=False) if profil_actuel else '(aucune)'}\n\n"
            f"Conversation à analyser :\n{messages_recents}"
        )

        client_groq = Groq(api_key=get_secret("GROQ_API_KEY"), max_retries=0)
        completion = client_groq.chat.completions.create(
            model=MODELE_PROFIL,
            messages=[{"role": "user", "content": prompt_profil}],
            response_format={"type": "json_object"},
            max_completion_tokens=None,
            timeout=DELAI_MAX_PAR_APPEL,
        )
        brut = completion.choices[0].message.content.strip()

        try:
            extrait = json.loads(brut)
        except json.JSONDecodeError:
            logging.error(
                f"ERREUR profil utilisateur : réponse non-JSON du modèle "
                f"(agent={agent_id}, user={user_id}) : {brut[:200]!r}"
            )
            return

        if not isinstance(extrait, dict):
            return

        # Ne garde que les clés du schéma défini (le modèle peut halluciner
        # des clés en plus malgré la consigne) et jette les valeurs vides
        # pour ne pas écraser une ancienne valeur connue par du vide.
        noms_valides = {c["nom"] for c in schema}
        nouveau_profil = dict(profil_actuel)
        for cle, valeur in extrait.items():
            if cle in noms_valides and valeur:
                nouveau_profil[cle] = valeur

        supabase.table("agent_user_profiles").upsert(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "donnees": nouveau_profil,
                "updated_at": datetime.utcnow().isoformat(),
            },
            on_conflict="agent_id,user_id",
        ).execute()

        logging.info(f"Profil utilisateur mis à jour pour agent={agent_id}, user={user_id}.")
    except Exception as e:
        logging.error(f"ERREUR mise à jour profil utilisateur (agent={agent_id}, user={user_id}) : {e}")


INSTRUCTIONS_LONGUEUR_REPONSE = {
    # Sélecteur Courte/Moyenne/Longue dans la barre de saisie, modifiable
    # à chaque message. "moyenne" = comportement historique (pas
    # d'instruction ajoutée), pour ne rien changer par défaut.
    "courte": (
        "\n\nCONSIGNE DE LONGUEUR : réponds de façon brève et directe (quelques "
        "phrases maximum), sans sacrifier l'exactitude. Va à l'essentiel."
    ),
    "moyenne": "",
    "longue": (
        "\n\nCONSIGNE DE LONGUEUR : développe ta réponse en détail (explications, "
        "exemples, étapes intermédiaires si utile), sans être verbeux pour rien."
    ),
}


# Ajouté 2026-07-20 après un test réel de Bourama : demander "montre-moi
# une image d'un ordinateur portable" ou "une carte de Tunis" faisait
# INVENTER un lien markdown ![](url) vers une fausse source ("Wikimedia
# Commons", "OpenStreetMap") -- URL cassée, citation fabriquée, aucun
# outil réel derrière. Deux causes distinctes, une seule règle :
#   1. La génération d'image réelle (Together AI/Flux, voir
#      core/generation_images.py) existe mais TOGETHER_API_KEY n'est pas
#      encore configurée -> l'outil n'est pas dans outils_mcp, donc
#      injoignable. Pas de solution ici tant que la clé n'est pas ajoutée.
#   2. Carte/graphique/widget interactif N'ONT JAMAIS eu d'outil dédié --
#      le frontend (djiguigne-frontend) sait déjà rendre ces trois blocs
#      nativement (voir CarteMessage.tsx, GraphiqueDonnees.tsx,
#      WidgetSandbox.tsx), il manquait juste la convention ici.
INSTRUCTIONS_FORMATS_AFFICHAGE = (
    "\n\nFORMATS ENRICHIS : utilise ces blocs quand ils apportent une vraie valeur "
    "(jamais pour décorer) :\n"
    "- ```mermaid``` : diagramme flowchart/séquence/état. Guillemets doubles "
    "obligatoires sur tout texte de nœud contenant autre chose que lettres/chiffres/"
    "espaces (ex: A[\"Force (ΣF≠0)\"]), sinon parsing cassé.\n"
    "- ```chart``` : JSON {\"type\": \"line\"|\"bar\"|\"pie\", \"data\": [...], "
    "\"titre\"?: \"...\"}. \"data\" = tableau d'objets plats, 1ère clé = axe X, "
    "suivantes = séries.\n"
    "- ```carte``` : JSON {\"lat\": ..., \"lng\": ..., \"label\"?: \"...\"} pour "
    "localiser un lieu. Jamais de lien texte brut Maps/OSM à la place.\n"
    "- ```widget```/```html``` : mini-outil interactif autonome. Fond sombre par "
    "défaut -- si tu le changes, adapte aussi la couleur du texte.\n"
    "- ```geometrie``` : JSON {\"titre\"?, \"repere\"?: bool, \"points\": "
    "[{\"id\", \"x\", \"y\", \"label\"?}], \"elements\": [...]} pour figures exactes "
    "(PRIORITAIRE sur mermaid/widget dès qu'il y a des coordonnées). Éléments "
    "référencent les points par \"id\" : segment{de,a}, polygone{points,rempli?}, "
    "cercle{centre,rayon}, vecteur{de,a,label?}, angle{sommet,point1,point2,label?}. "
    "Bornes auto-calculées.\n"
    "\n"
    "Bloc léger (ci-dessus) = aperçu immédiat sans fichier. Outil de génération = "
    "livrable réel téléchargeable. Choisis selon le besoin réel, pas par défaut.\n"
    "LIENS : n'écris JAMAIS une URL non obtenue réellement (ni via outil, ni donnée "
    "par l'utilisateur), même plausible. N'invente jamais de source. Si demandé et "
    "aucun outil dispo, dis-le. Si un outil de génération renvoie une URL réelle, "
    "NE LA RÉÉCRIS PAS (l'interface l'affiche automatiquement en carte) -- confirme "
    "juste en langage naturel.\n"
    "OUTILS DE GÉNÉRATION/ACTION (document, image, code, site, audio, rappel...) : "
    "ton texte est affiché AVANT que l'exécution soit terminée -- tu ne sauras "
    "jamais, au moment où tu écris, si ça a réussi. ANNONCE ce que tu fais "
    "(\"Je génère ton document sur...\"), n'AFFIRME JAMAIS que c'est fait ou réussi "
    "(pas de \"Voici\", \"C'est prêt\", \"J'ai créé\"). Si ça échoue, un message "
    "d'erreur s'affichera automatiquement après coup -- tu n'as rien à faire de plus, "
    "pas de second message, pas de round-trip.\n"
    "FAITS VÉRIFIABLES : pour toute question sur un état réel (structure dépôt, "
    "contenu fichier, liste, nombre...), appelle TOUJOURS l'outil correspondant et "
    "rapporte EXACTEMENT son résultat (y compris troncatures), jamais de complément "
    "par supposition. Structure/arborescence d'un dépôt GitHub -> toujours "
    "explorer_depot_github, jamais un README (peut être obsolète).\n"
    "Ne décris JAMAIS un appel d'outil (pas de \"Appel de X avec...\", pas de JSON "
    "de requête/résultat) -- l'interface l'affiche déjà. Réponds en langage naturel "
    "comme si tu savais déjà."
)


# Ajouté 2026-08-01 (demande Bourama, suite a l'ajout de calculer_symbolique) :
# les deux outils se chevauchent en pratique -- WolframLanguageEvaluator (un
# des 3 outils fixes exposes par le serveur MCP Wolfram, voir
# registre_outils.py) sait aussi resoudre/deriver/integrer/simplifier, sa
# description (fixee par Wolfram, pas modifiable ici) ne l'exclut nulle part.
# La description de calculer_symbolique (voir serveur_mcp_generation.py) dit
# deja "utilise wolfram pour le factuel", mais rien cote Wolfram ne dit
# l'inverse -- cette regle vit donc dans le prompt systeme general (au-dessus
# des descriptions d'outils individuelles) pour trancher des que les DEUX
# sont disponibles pour un agent. Suit le meme principe que le correctif du
# 31/07 sur _router_outils : un exemple concret par cas vaut mieux qu'un
# principe abstrait pour un petit modele.
INSTRUCTIONS_ARBITRAGE_CALCUL = (
    "\n\nARBITRAGE calculer_symbolique / wolfram (si les deux sont disponibles) : "
    "calcul formel EXACT (simplifier, developper, factoriser, deriver, integrer, "
    "resoudre une equation, limite) -> TOUJOURS calculer_symbolique, jamais "
    "wolfram, meme via WolframLanguageEvaluator qui sait techniquement le faire "
    "aussi. wolfram reste reserve aux questions de connaissance factuelle du "
    "monde reel non calculables par un moteur symbolique seul (constante "
    "physique, donnee chimique, donnee geographique/demographique...). "
    "Exemples : \"derive x^2*sin(x)\" -> calculer_symbolique. \"masse du "
    "proton\" -> wolfram. \"resous 2x+3=7\" -> calculer_symbolique, pas "
    "wolfram meme si ca semble plus rapide."
)


def _router_outils(message_utilisateur, outils_disponibles, historique=None):
    """
    Bouton Outils, couche de suggestion automatique (2026-07-28, demande
    Bourama). Coexiste avec la sélection manuelle (BarreDeSaisie.tsx) sans
    la remplacer : les deux retombent sur le même mécanisme final
    (outil_force -> lister_tous_les_outils -> system prompt).

    Premier appel LLM séparé, rapide (pas le modèle qui répond à
    l'utilisateur), qui juge lesquels des outils RÉELLEMENT autorisés
    pour cet agent (outils_disponibles, déjà filtré par
    lister_tous_les_outils AVANT le filtre outil_force -- jamais le
    catalogue brut du registre) seraient pertinents pour la question. Ne
    répond jamais à la question lui-même, ne décide rien à la place de
    l'utilisateur : le résultat sert juste à proposer des boutons côté
    frontend (voir chat(), événement SSE "outils_suggeres"), que
    l'utilisateur clique ou ignore.

    historique (2026-07-31, demande Bourama, correction "routeur suggère
    mal") : quelques derniers échanges de la conversation, même format que
    messages_base (liste de {"role", "content"}), pour que le routeur juge
    avec le contexte de la discussion et pas seulement la dernière phrase
    isolée (ex: "et pour le fichier PDF ?" ne veut rien dire sans savoir de
    quel fichier on parlait). Optionnel et borné aux 4 derniers messages
    pour ne pas alourdir cet appel censé rester rapide et bon marché.

    Renvoie une liste de noms d'outils (sous-ensemble de
    outils_disponibles, éventuellement vide). Fail-safe strict : toute
    erreur ou réponse mal formée renvoie une liste vide plutôt que de
    bloquer la réponse normale -- ce routeur ne doit jamais empêcher
    l'utilisateur d'obtenir une réponse.
    """
    if not outils_disponibles or not message_utilisateur:
        return []

    noms_valides = {o["function"]["name"] for o in outils_disponibles}
    catalogue = "\n".join(
        f"- {o['function']['name']} : {o['function']['description']}"
        for o in outils_disponibles
    )

    contexte = ""
    if historique:
        derniers = historique[-4:]
        lignes = "\n".join(
            f"{m.get('role', '?')} : {m.get('content', '')}" for m in derniers
        )
        contexte = f"Derniers échanges de la conversation (contexte) :\n{lignes}\n\n"

    prompt_routeur = (
        "Tu es un routeur d'outils : tu ne réponds JAMAIS à la question "
        "toi-même, tu décides seulement quels outils (parmi la liste "
        "ci-dessous) seraient utiles pour y répondre. Si aucun outil "
        "n'est pertinent (question générale, conversation normale, "
        "salutation...), renvoie une liste vide -- ne force jamais un "
        "outil par défaut ni \"au cas où\".\n\n"
        "IMPORTANT : diagramme, graphique/chart, carte/localisation, "
        "figure géométrique et mini-outil interactif (widget) NE SONT "
        "JAMAIS des outils de cette liste -- ce sont des blocs que le "
        "modèle principal écrit lui-même directement dans sa réponse, "
        "affichés nativement par l'interface. Une demande de ce type "
        "(\"fais-moi un diagramme de...\", \"montre-moi une carte de...\", "
        "\"trace un graphique de...\") ne justifie donc JAMAIS de "
        "suggestion, même si un outil de la liste semble vaguement "
        "proche -- réponds liste vide dans ce cas.\n\n"
        # CORRECTIF 2026-07-31 (signalé par Bourama, test réel : le
        # routeur suggérait une recherche web pour "1+1") : un petit
        # modèle rapide (voir MODELE_ROUTEUR_OUTILS) a besoin d'exemples
        # concrets, pas seulement d'une règle abstraite -- "évite les
        # outils inutiles" ne suffit pas à empêcher un réflexe "au cas
        # où" sur une question triviale. Les exemples ci-dessous couvrent
        # explicitement calcul simple, connaissance générale stable et
        # salutation/conversation normale.
        "IMPORTANT : ne suggère JAMAIS un outil pour une question à "
        "laquelle le modèle principal peut répondre seul, avec certitude, "
        "par simple raisonnement ou connaissance générale stable -- même "
        "si un outil de la liste pourrait techniquement s'en servir. "
        "Exemples qui DOIVENT renvoyer une liste vide : \"1+1\", "
        "\"combien font 12 fois 15 ?\", \"quelle est la capitale de la "
        "France ?\", \"explique-moi la photosynthèse\", \"salut, ça va ?\". "
        "Réserve les outils aux cas où une information réelle et "
        "vérifiable est nécessaire : donnée qui change dans le temps "
        "(actualité, prix, météo...), contenu spécifique à récupérer "
        "(fichier, page web, dépôt...), ou calcul non trivial qu'un humain "
        "ne ferait pas de tête.\n\n"
        f"Outils disponibles :\n{catalogue}\n\n"
        f"{contexte}"
        f"Question de l'utilisateur : {message_utilisateur}\n\n"
        "Réponds UNIQUEMENT avec un objet JSON de la forme "
        '{"outils": ["nom_outil_1", "nom_outil_2"]} (noms EXACTEMENT '
        "comme listés ci-dessus, liste vide si rien n'est pertinent)."
    )

    try:
        client_groq = Groq(api_key=get_secret("GROQ_API_KEY"), max_retries=0)
        completion = client_groq.chat.completions.create(
            model=MODELE_ROUTEUR_OUTILS,
            messages=[{"role": "user", "content": prompt_routeur}],
            response_format={"type": "json_object"},
            max_completion_tokens=200,
            timeout=DELAI_MAX_PAR_APPEL,
        )
        brut = completion.choices[0].message.content.strip()
        suggestion = json.loads(brut)
        outils_suggeres = [n for n in suggestion.get("outils", []) if n in noms_valides]
        logging.info(f"Routeur d'outils -> suggérés : {outils_suggeres or '(aucun)'}")
        return outils_suggeres
    except Exception as e:
        logging.error(f"ERREUR routeur outils : {e}")
        return []


def _construire_system_prompt(message_utilisateur, agent_id, user_id=None, longueur_reponse="moyenne", fuseau_horaire=None, recherche_forcee=False, outil_force=None, sans_enseignant=False):
    # Agents à "contenu dynamique par matière" (voir
    # core/contenu_dynamique_matiere.py, 2026-08-06) : le system_prompt
    # dépend de l'étudiant et du message, jamais de get_system_prompt()
    # (qui suppose un prompt fixe et cacheable par agent).
    #
    # Sinon, agents "partagés" (Stirux, Lirinus...) : si un prompt
    # personnalisé existe pour cet utilisateur sur cet agent (table
    # agents_prompts_utilisateur), il remplace entièrement le
    # system_prompt de base -- pas de fusion des deux. Ne casse pas le
    # cache Groq pour les autres agents (aucune ligne dans la table =
    # comportement inchangé, un seul appel de plus, "best effort").
    #
    # Tous les autres agents de la plateforme passent par
    # get_system_prompt() comme avant, aucune régression.
    # Perf (10/08, demande Bourama : "l'assemblage du system prompt") :
    # system_prompt (ligne suivante) ne dépend d'AUCUNE des 4 lectures
    # ci-dessous (candidats/resume_memoire/profil_utilisateur/
    # comportements_etudiant) -- pourtant elle partait avant elles, en
    # séquence. Pour Nitrux (agent_a_contenu_dynamique=True),
    # resoudre_system_prompt_matiere peut en plus déclencher un SECOND
    # appel LLM séparé (_choisir_matiere, routeur de matière -- même
    # coût qu'un appel LLM complet, voir core/contenu_dynamique_matiere.py),
    # en plus du routeur d'outils déjà parallélisé côté chat(). Intégrée
    # ici au même ThreadPoolExecutor que les 4 autres lectures : sa
    # latence est maintenant absorbée avec elles au lieu de s'ajouter
    # avant. Même valeur retournée qu'avant, juste calculée en même
    # temps.
    with concurrent.futures.ThreadPoolExecutor() as executor:
        f_system_prompt = (
            executor.submit(resoudre_system_prompt_matiere, message_utilisateur, agent_id, user_id, sans_enseignant)
            if agent_a_contenu_dynamique(agent_id)
            else executor.submit(lambda: _charger_prompt_personnalise(agent_id, user_id) or get_system_prompt(agent_id))
        )
        f_candidats = executor.submit(chercher_candidats, message_utilisateur, agent_id=agent_id)
        f_resume = executor.submit(_charger_resume_memoire, user_id)
        f_profil = executor.submit(_charger_profil_utilisateur, agent_id, user_id)
        f_comportements = (
            executor.submit(lister_comportements_etudiant, agent_id, user_id) if user_id else None
        )
    system_prompt = f_system_prompt.result()
    candidats = f_candidats.result()
    resume_memoire = f_resume.result()
    profil_utilisateur = f_profil.result()
    comportements_etudiant = f_comportements.result() if f_comportements else []

    instructions = "".join(f"\n{c['contenu']}\n" for c in candidats.get("prompts", []))
    contexte_docs = "".join(f"\n{c['contenu']}\n" for c in candidats.get("documents", []))

    # ORDRE DU PROMPT (2026-07-29, optimisation cache Groq) : du plus stable
    # au plus volatil, pour maximiser la longueur du prefixe identique
    # entre appels successifs. Groq met en cache automatiquement le
    # prefixe commun a une requete recente (jusqu'a 2h) : cette portion
    # coute moitie prix ET NE COMPTE PLUS dans le quota TPM. Des qu'un
    # seul caractere differe plus tot dans le texte, tout ce qui suit
    # perd le benefice du cache -- d'ou l'ordre choisi ici :
    #   1. Blocs 100% fixes, identiques pour TOUTE la plateforme
    #      (FORMATS_AFFICHAGE, CONTEXTE_INVISIBLE) -> cache hit sur
    #      quasi 100% des appels, tous agents/utilisateurs confondus.
    #   2. Prompt de l'agent (base_notion) -> stable pour TOUS les
    #      utilisateurs de CET agent, change seulement si le createur
    #      l'edite.
    #   3. Memoire + profil utilisateur -> stables pour UN utilisateur
    #      sur plusieurs messages (ne changent que tous les 10-20
    #      messages, voir SEUIL_RESUME_MESSAGES/SEUIL_PROFIL_MESSAGES).
    #   4. Blocs outils (bibliotheque/outils actifs/github) + consigne
    #      de longueur -> stables tant que la selection de la barre de
    #      saisie ne change pas d'un message a l'autre (plus volatil que
    #      3, moins que le RAG).
    #   5. RAG (instructions/contexte_docs) + recherche forcee -> change
    #      quasi a chaque message (dependant du texte exact de la
    #      question).
    #   6. Date/heure -> LE plus volatil, change chaque minute : doit
    #      absolument rester en tout dernier pour ne jamais casser le
    #      prefixe cachable de tout ce qui precede.
    system_final = (
        INSTRUCTIONS_FORMATS_AFFICHAGE.lstrip("\n")
        + INSTRUCTIONS_ARBITRAGE_CALCUL
        + REGLE_CONTEXTE_INVISIBLE
    )

    # get_system_prompt peut renvoyer None (agent sans notion_page_id ni
    # system_prompt renseigné ET aucun prompt jamais mis en cache avec
    # succès) -- repli sur "" pour ne pas planter les += qui suivent,
    # certains inconditionnels (bug repéré le 2026-07-21, jamais déclenché
    # en pratique jusqu'ici mais bien réel pour un agent mal configuré).
    if system_prompt:
        system_final += f"\n\n{system_prompt}"

    # Section "Mes comportements" (06/08/2026, demande Bourama : "on peut
    # en mettre plusieurs hein, pas juste un") : instructions écrites par
    # l'étudiant lui-même, EN PLUS du system_prompt résolu ci-dessus
    # (généraliste, matière d'un enseignant, ou "sans enseignant") --
    # jamais un remplacement. Liste vide si rien d'enregistré pour cet
    # (agent, utilisateur), aucun bruit ajouté dans ce cas.
    # comportements_etudiant déjà chargé plus haut, en parallèle avec
    # candidats/resume_memoire/profil_utilisateur (voir ThreadPoolExecutor
    # ci-dessus, perf 10/08).
    if comportements_etudiant:
        liste_comportements = "\n".join(f"- {c['texte']}" for c in comportements_etudiant)
        system_final += (
            "\n\nINSTRUCTIONS PERSONNELLES ÉCRITES PAR CET ÉTUDIANT LUI-MÊME (à respecter EN PLUS de "
            f"tout ce qui précède, jamais à la place) :\n{liste_comportements}"
        )

    if resume_memoire:
        system_final += (
            "\n\nCONTEXTE DES SESSIONS PRÉCÉDENTES AVEC CETTE PERSONNE (résumé, à utiliser "
            "pour personnaliser ta réponse -- son projet, ses préférences, ce qu'elle a déjà "
            "expliqué -- MAIS ne jamais le réciter tel quel, et ne JAMAIS t'en servir comme "
            "source de vérité pour un fait que tu peux vérifier maintenant avec un outil "
            "(structure d'un dépôt, contenu d'un fichier, état actuel de quoi que ce soit). "
            "Ce résumé peut décrire une situation ancienne, déjà changée depuis -- si un outil "
            "existe pour vérifier l'état actuel d'une chose mentionnée ici, appelle-le, ne "
            f"réponds jamais depuis ce résumé seul) :\n{resume_memoire}"
        )
    if profil_utilisateur:
        system_final += (
            "\n\nPROFIL CONNU DE CET UTILISATEUR (rempli automatiquement au fil des "
            "conversations, à utiliser pour personnaliser ta réponse, ne jamais le "
            f"réciter tel quel) :\n{json.dumps(profil_utilisateur, ensure_ascii=False)}"
        )

    # Bouton Outils (2026-07-25, multi-sélection depuis le 26/07 --
    # outil_force est maintenant une LISTE, plus une simple chaîne, voir
    # demande Bourama). Ces deux blocs décrivaient ces capacités de façon
    # inconditionnelle, même quand aucun outil n'est réellement envoyé au
    # modèle -- confirmé en test réel le 25/07 (l'IA "récitait" ces
    # capacités alors que le log backend montrait `Outils envoyés au LLM
    # ce tour-ci : []`). Désormais gated sur outil_force : le bloc
    # n'apparaît que si l'outil concerné fait partie de la sélection pour
    # ce message précis.
    outils_forces = outil_force or []
    if "chercher_fichier" in outils_forces:
        system_final += (
            "\n\nBIBLIOTHÈQUE DE FICHIERS : outil chercher_fichier pour retrouver un "
            "fichier déjà uploadé (plateforme, créateur de l'agent, ou cet utilisateur "
            f"dans une conversation passée). agent_id=\"{agent_id}\", "
            f"user_id={f'"{user_id}"' if user_id else 'absent (non connectée)'}, à passer "
            "exactement tels quels. Si on te redemande un fichier envoyé plus tôt dans "
            "la conversation (tu l'as vu mais tu n'as pas son URL réelle), appelle "
            "TOUJOURS chercher_fichier au lieu d'inventer un lien."
        )
    if "consulter_bibliotheque" in outils_forces:
        # 2026-08-01, demande Bourama : bibliothèque personnelle de
        # documents PDF (voir "Mon espace" côté app), consultable par le
        # contenu (contrairement à chercher_fichier ci-dessus qui ne
        # matche que nom/description) -- outil disponible même si cet
        # agent n'a rien coché en catégorie 1, voir mcp_tools.py.
        system_final += (
            "\n\nBIBLIOTHÈQUE PERSONNELLE : outil consulter_bibliotheque pour chercher "
            "dans les documents PDF que CET utilisateur a lui-même ajoutés à sa "
            "bibliothèque personnelle (indépendante de cet agent). "
            f"user_id={f'"{user_id}"' if user_id else 'absent (non connectée)'}, à passer "
            "exactement tel quel -- sans utilisateur connecté, l'outil renverra toujours "
            "vide, inutile de l'appeler. Utilise les extraits renvoyés directement pour "
            "répondre, sans les recopier mot pour mot."
        )
    if outils_forces:
        # Confirmé en test réel 25/07 : même avec l'outil réellement
        # présent dans la liste envoyée au modèle (vérifié via le log
        # `Outils envoyés au LLM ce tour-ci : ['tavily_search']`), le
        # modèle a quand même répondu par son réflexe par défaut ("mes
        # connaissances s'arrêtent à ma coupure, je ne peux pas chercher
        # en ligne") au lieu d'appeler l'outil. Instruction explicite
        # pour forcer la bonne priorité : la présence réelle de l'outil
        # prime sur toute limitation générale apprise à l'entraînement.
        liste_lisible = ", ".join(outils_forces)
        system_final += (
            f"\n\nOUTIL(S) ACTIF(S) : {liste_lisible} "
            f"{'sont disponibles' if len(outils_forces) > 1 else 'est disponible'}, "
            "sélectionné(s) via le bouton Outils. Utilise-les si pertinents -- leur "
            "présence prime sur tes limitations par défaut, n'invente jamais un refus. "
            "Appel via le vrai mécanisme API uniquement, jamais de pseudo-syntaxe en "
            "texte (TOOL_CODE, nom_outil(...), nom_outil{...}, call:nom_outil{...})."
        )
    if not outils_forces:
        # Bouton Outils (2026-07-25, suite) : sans cette instruction, le
        # modèle invente de lui-même une liste de capacités générique
        # d'assistant IA (génération de fichiers, recherche web, etc.)
        # dès qu'on lui demande "qu'est-ce que tu sais faire" -- confirmé
        # en test réel le 25/07, aucun bloc du prompt système ne causait
        # ça (le prompt de l'agent Nucleos ne fait que 126 caractères),
        # c'est une invention pure du modèle. Instruction explicite pour
        # contrer ce réflexe par défaut.
        system_final += (
            "\n\nAUCUN OUTIL ACTIF : aucune capacité de recherche web/génération de "
            "fichier/exploration GitHub/recherche de fichier envoyé pour ce message, "
            "même si active plus tôt dans la conversation. Si on demande 'qu'est-ce "
            "que tu sais faire', ne liste pas de capacités génériques : dis que tu "
            "n'as aucun outil actif, invite à cliquer sur le bouton Outils. N'invente "
            "jamais un outil ni une fausse syntaxe d'appel (TOOL_CODE, nom_outil(...), "
            "nom_outil{...}, call:nom_outil{...}). Les blocs ```mermaid/chart/carte/"
            "widget/geometrie restent disponibles (formats d'affichage, pas des outils)."
        )
    if any(o in outils_forces for o in ("explorer_depot_github", "lire_fichier_depot_github", "modifier_fichier_depot_github")):
        system_final += (
            "\n\nEXPLORATION GITHUB : explorer_depot_github (arborescence), "
            "lire_fichier_depot_github (contenu d'un fichier), modifier_fichier_depot_github "
            "(écrit un changement -- uniquement si demandé explicitement). user_id à "
            f"passer exactement : {f'"{user_id}"' if user_id else 'une chaîne vide (non connectée)'}. "
            "Dépôts privés accessibles seulement si compte GitHub connecté."
        )
    system_final += INSTRUCTIONS_LONGUEUR_REPONSE.get(longueur_reponse, "")

    if instructions:
        system_final += f"\n\n{instructions}"
    if contexte_docs:
        system_final += f"\n\n{contexte_docs}"
    if recherche_forcee:
        # Icône de recherche dans la barre de saisie (djiguigne-frontend) --
        # forçage manuel pour CE message précis (voir docstring de
        # chat()). Le modèle peut de toute façon décider seul d'utiliser
        # Tavily sans ce flag (tool-calling normal) ; ceci garantit que
        # ça arrive quand l'étudiant veut être sûr.
        system_final += (
            "\n\nCONSIGNE DE RECHERCHE : pour ce message précis, utilise "
            "systématiquement un outil de recherche web (tavily_search) avant de "
            "répondre, même si tu penses déjà connaître la réponse -- l'étudiant a "
            "explicitement demandé une recherche fraîche."
        )

    # Contexte système "date/heure actuelle" (2026-07-20) : sans ça, le
    # modèle ne sait pas qu'on est en 2026 et peut situer les événements
    # récents n'importe où par rapport à sa coupure d'entraînement.
    #
    # Fuseau horaire (corrigé 2026-07-20) : PAS figé sur Tunis -- Djiguignè
    # est un projet panafricain (voir Maame), rien ne dit que l'utilisateur
    # est à Tunis. `fuseau_horaire` vient du navigateur
    # (Intl.DateTimeFormat().resolvedOptions().timeZone, voir
    # ChatIA.tsx:envoyerMessage), pas d'une valeur choisie côté serveur.
    # Repli sur UTC si absent ou si le navigateur envoie un nom de fuseau
    # invalide (ZoneInfo lève ZoneInfoNotFoundError) -- jamais une supposition
    # de pays. Ce bloc DOIT rester en tout dernier (voir commentaire
    # d'ordre en tête de fonction) : il change chaque minute, donc tout ce
    # qui le suivrait perdrait le benefice du cache Groq -- ici rien ne le
    # suit.
    try:
        fuseau = ZoneInfo(fuseau_horaire) if fuseau_horaire else ZoneInfo("UTC")
    except Exception:
        fuseau = ZoneInfo("UTC")
    maintenant = datetime.now(fuseau)
    jours_fr = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois_fr = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ]
    date_fr = f"{jours_fr[maintenant.weekday()]} {maintenant.day} {mois_fr[maintenant.month - 1]} {maintenant.year}, {maintenant.strftime('%H:%M')}"
    system_final += f"\n\nNous sommes le {date_fr} (fuseau : {fuseau.key if hasattr(fuseau, 'key') else 'UTC'})."

    logging.info(
        f"Prompt système construit -> base_notion:{len(system_prompt or '')} caractères, "
        f"memoire:{'oui' if resume_memoire else 'NON'}, "
        f"profil_utilisateur:{'oui' if profil_utilisateur else 'NON'}, "
        f"instructions:{'oui' if instructions else 'NON'}, "
        f"contexte_docs:{'oui' if contexte_docs else 'NON'}"
    )
    return system_final


def _est_timeout(erreur):
    return "timeout" in str(erreur).lower()


DELAI_MAX_PAR_APPEL = 10  # secondes : on bascule vite plutot que d'attendre
MAX_PASSAGES_CASCADE = 2  # on ne retente toute la cascade que si TOUT a timeout


def _sauvegarder_echange(user_id, agent_id, message_utilisateur, reponse_finale, conversation_id=None, modele=None):
    """
    Persiste l'echange (question + reponse) dans `conversations`, pour la
    memoire long-terme. Ignore silencieusement si l'utilisateur n'est pas
    connecte (user_id=None) ou si la reponse est vide (ex: message
    d'erreur technique, qu'on ne veut pas polluer la memoire avec).

    `modele` (optionnel, 02/08/2026) : modele_id qui a genere
    `reponse_finale`, ecrit UNIQUEMENT sur la ligne "assistant" de
    `historique_conversations` (pas sur `conversations`, table de memoire
    court terme sans vocation d'affichage) -- None si la cascade Groq/
    Gemini par defaut a repondu (comportement historique inchange, colonne
    nullable), sinon le modele_id premium (voir core/fournisseurs_llm.py).
    Permet au frontend d'afficher quel modele a repondu sous chaque
    message (voir AgentEditable.modeles_disponibles cote api/agents.py).
    """
    ids_historique = None  # renvoyé à l'appelant pour l'indexation du feedback

    if not user_id or not (reponse_finale or "").strip():
        return ids_historique
    try:
        supabase.table("conversations").insert([
            {"user_id": user_id, "agent_id": agent_id, "role": "user", "content": message_utilisateur},
            {"user_id": user_id, "agent_id": agent_id, "role": "assistant", "content": reponse_finale},
        ]).execute()
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (sauvegarde conversations) : {e}")

    # Ajouté le 2026-07-13 (Bourama : historique de conversation visible,
    # conservée par agent, dans le tableau de bord). Table SÉPARÉE de
    # `conversations` ci-dessus, jamais purgée -- voir le commentaire de
    # migration (historique_conversations) pour le detail de la
    # distinction. Volontairement dans un bloc try/except À PART : si cette
    # écriture échoue, ça ne doit jamais faire échouer la mémoire de l'IA
    # ci-dessus, qui est la partie critique pour la qualité des réponses.
    #
    # `conversation_id` (2026-07-13, Bourama : liste de conversations
    # distinctes et cliquables dans la sidebar de chat.py, façon Claude.ai)
    # regroupe les messages d'un même fil de discussion, généré côté
    # chat.py (une valeur par conversation affichée, PAS par message) et
    # simplement transmis ici tel quel. None accepté (colonne nullable) :
    # un appelant qui ne gère pas encore les fils continue de fonctionner
    # sans erreur, ses messages sont juste groupés sous "historique ancien"
    # côté affichage plutôt que dans un fil précis.
    try:
        res = (
            supabase.table("historique_conversations")
            .insert([
                {"user_id": user_id, "agent_id": agent_id, "role": "user", "content": message_utilisateur, "conversation_id": conversation_id},
                {"user_id": user_id, "agent_id": agent_id, "role": "assistant", "content": reponse_finale, "conversation_id": conversation_id, "modele": modele},
            ])
            .execute()
        )
        lignes = res.data or []
        ligne_user = next((l for l in lignes if l["role"] == "user"), None)
        ligne_assistant = next((l for l in lignes if l["role"] == "assistant"), None)
        if ligne_user and ligne_assistant:
            ids_historique = {
                "message_id_user": ligne_user["id"],
                "message_id_assistant": ligne_assistant["id"],
                "created_at_assistant": ligne_assistant.get("created_at"),
                # Propage automatiquement dans tous les evenements SSE
                # "meta" (voir chaque site d'appel plus haut, tous font
                # **ids_historique) -- evite de dupliquer ce champ a la
                # main partout, voir ChatIA.tsx cote frontend pour l'usage.
                "modele": modele,
            }
    except Exception as e:
        logging.error(f"ERREUR SUPABASE (sauvegarde historique_conversations) : {e}")

    return ids_historique


def _mettre_a_jour_resume_si_besoin(user_id):
    """
    Si assez de nouveaux messages bruts se sont accumules (>= SEUIL_RESUME_MESSAGES)
    depuis le dernier resume, en regenere un condense (ancien resume + messages
    recents) via un modele Groq rapide, l'ecrit dans conversation_summaries, puis
    purge les messages bruts desormais condenses. Ne bloque jamais la reponse a
    la personne : toute erreur est juste loguee, jamais remontee a l'appelant.

    Compte unifie (juillet 2026) : scope par user_id seul, tous agents
    confondus. `agent_id` reste present dans `conversations` en tant que
    simple metadonnee de tracabilite (colonne non retiree par la
    migration), mais ne filtre plus rien ici -> les messages de tous les
    agents de la plateforme alimentent le meme resume.
    """
    if not user_id:
        return
    try:
        messages = (
            supabase.table("conversations")
            .select("id, role, content, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(SEUIL_RESUME_MESSAGES)
            .execute()
        ).data or []

        if len(messages) < SEUIL_RESUME_MESSAGES:
            return  # pas encore assez de matiere pour justifier un resume

        ancien_resume = _charger_resume_memoire(user_id)
        messages_recents = "\n".join(
            f"{'Utilisateur' if m['role'] == 'user' else 'Assistant'} : {m['content']}"
            for m in reversed(messages)
        )

        # Neutralisé le 2026-07-22 (Bourama : la plateforme n'est pas
        # réservée aux étudiants, ce n'était que le point de départ du
        # projet -- un ancien prompt ici forçait "niveau apparent" et
        # "sujets de difficulté d'étudiant" sur N'IMPORTE QUELLE
        # conversation, y compris des sessions de test technique sans
        # aucun rapport avec l'école, produisant des résumés inventés/hors
        # sujet). Ne présuppose plus rien sur qui est cette personne ni
        # sur la nature de l'agent avec qui elle parle.
        prompt_resume = (
            "Condense ce qui suit en un résumé factuel et concis (5-8 lignes maximum) "
            "de cette personne, utile pour personnaliser une future session avec elle : "
            "ses centres d'intérêt ou sujets récurrents, ses préférences, le contexte "
            "réellement présent dans les échanges. N'invente rien qui ne soit pas "
            "clairement indiqué -- ne présuppose ni niveau scolaire, ni statut "
            "d'étudiant, ni progression pédagogique si rien dans la conversation ne "
            "l'indique explicitement. Pas de politesse, pas de méta-commentaire, "
            "juste les faits utiles.\n\n"
        )
        if ancien_resume:
            prompt_resume += f"Résumé précédent :\n{ancien_resume}\n\n"
        prompt_resume += f"Nouveaux échanges à intégrer :\n{messages_recents}"

        client_groq = Groq(api_key=get_secret("GROQ_API_KEY"), max_retries=0)
        completion = client_groq.chat.completions.create(
            model=MODELE_RESUME,
            messages=[{"role": "user", "content": prompt_resume}],
            max_completion_tokens=None,
            timeout=DELAI_MAX_PAR_APPEL,
        )
        nouveau_resume = completion.choices[0].message.content.strip()

        supabase.table("conversation_summaries").upsert({
            "user_id": user_id,
            "summary": nouveau_resume,
        }).execute()

        # Purge les messages bruts maintenant condenses, pour ne pas
        # reconstruire indefiniment le meme resume a chaque appel suivant.
        ids_a_purger = [m["id"] for m in messages if m.get("id") is not None]
        if ids_a_purger:
            supabase.table("conversations").delete().in_("id", ids_a_purger).execute()

        logging.info(f"Résumé mémoire mis à jour pour user={user_id}.")
    except Exception as e:
        logging.error(f"ERREUR mise à jour résumé mémoire : {e}")


class _AttenteConfirmation(Exception):
    """
    Levee des qu'un outil sensible (ecriture) est rencontre, AVANT de
    l'executer. `appel` est l'appel en question ; `appels_restants` sont
    les appels du meme lot qui n'ont pas encore ete traites (ils seront
    rejoues a la reprise, dans l'ordre, apres que celui-ci ait ete
    confirme ou annule).
    """
    def __init__(self, appel, appels_restants):
        self.appel = appel
        self.appels_restants = appels_restants


def _executer_un_appel(appel, table_routage):
    try:
        arguments = json.loads(appel["arguments"] or "{}")
    except Exception:
        arguments = {}
    return appeler_outil(appel["name"], arguments, table_routage)


def _sources_depuis_json_generique(resultat_brut):
    """
    Detection GENERIQUE, independante du nom de l'outil : tout outil qui
    renvoie un JSON de la forme {"results": [{"title"/"titre", "url"}, ...]}
    voit ses sources extraites automatiquement -- couvre tavily_* et
    notion-search aujourd'hui (verifie par appel reel, pas suppose), et
    n'importe quel outil FUTUR qui renverrait la meme forme, sans toucher
    a ce fichier (demande explicite de Bourama, session du 2026-07-26 :
    preparer les citations pour n'importe quelle action/outil a venir,
    pas seulement ceux d'aujourd'hui).

    Best-effort : si le JSON ne correspond pas au format attendu (ou
    n'est pas du JSON), renvoie une liste vide plutot que de faire
    planter la reponse -- les sources sont un bonus, jamais un
    prerequis pour repondre.
    """
    try:
        donnees = json.loads(resultat_brut)
    except (json.JSONDecodeError, TypeError):
        return []

    resultats = donnees.get("results") if isinstance(donnees, dict) else None
    if not isinstance(resultats, list):
        return []

    sources = []
    for r in resultats:
        if isinstance(r, dict) and r.get("url"):
            sources.append({"titre": r.get("title") or r["url"], "url": r["url"]})
    return sources


def _sources_github_depuis_arguments(appel):
    """
    Cas particulier : nos outils GitHub locaux (core/serveur_mcp_github.py)
    renvoient du TEXTE brut (arborescence ou contenu de fichier), jamais du
    JSON -- la detection generique ci-dessus ne peut donc rien y trouver.
    La source se deduit plutot des ARGUMENTS de l'appel (repo/chemin),
    exactement comme le fait l'outil lui-meme pour construire ses requetes
    API. Si `branche` n'a pas ete precisee par le modele, on refait le
    meme appel `default_branch` que l'outil (voir explorer_depot_github/
    lire_fichier_depot_github) plutot que de deviner "main" -- un mauvais
    lien casse (ex: depot dont la branche par defaut est "master") serait
    pire qu'une source absente.
    """
    try:
        arguments = json.loads(appel["arguments"] or "{}")
    except Exception:
        return []

    repo = (arguments.get("repo") or "").strip()
    if not repo:
        return []

    branche = (arguments.get("branche") or "").strip()
    if not branche:
        try:
            info = requests.get(f"https://api.github.com/repos/{repo}", timeout=5)
            branche = info.json().get("default_branch", "main") if info.status_code == 200 else "main"
        except Exception:
            branche = "main"

    if appel["name"] == "lire_fichier_depot_github":
        chemin = (arguments.get("chemin") or "").strip()
        if not chemin:
            return []
        return [{"titre": chemin.split("/")[-1], "url": f"https://github.com/{repo}/blob/{branche}/{chemin}"}]

    # explorer_depot_github
    chemin_depart = (arguments.get("chemin_depart") or "").strip()
    url = f"https://github.com/{repo}/tree/{branche}/{chemin_depart}".rstrip("/")
    return [{"titre": repo, "url": url}]


def _resultat_pour_affichage(resultat_brut, max_chars=3000):
    """
    Tronque le resultat brut d'un outil pour l'evenement SSE
    "outil_resultat" (2026-07-26, demande Bourama : afficher ce qui a ete
    execute/le resultat pour CHAQUE outil, dans une section dediee avec
    l'icone de l'outil, distincte du raisonnement libre du modele -- voir
    OutilResultatBulle.tsx). Purement un affront de securite d'affichage
    (un depot GitHub explore ou un JSON de recherche peuvent faire
    plusieurs dizaines de Ko) : le contenu COMPLET reste envoye au modele
    via messages_agent, cette troncature ne concerne QUE ce qui est
    montre a la personne.
    """
    if not isinstance(resultat_brut, str):
        resultat_brut = str(resultat_brut)
    if len(resultat_brut) <= max_chars:
        return resultat_brut
    return resultat_brut[:max_chars] + f"\n... (tronqué, {len(resultat_brut)} caractères au total)"


def _extraire_sources(appel, resultat_brut):
    """
    Construit les sources ({"titre", "url"}) d'un appel d'outil pour
    l'evenement SSE "sources" (citations affichees sous la reponse, voir
    ChatIA.tsx/SourcesBulle.tsx). Deux strategies, dans l'ordre :

    1. Generique par forme de JSON (_sources_depuis_json_generique) --
       future-proof, aucune liste d'outils a maintenir.
    2. Cas particuliers a resultat texte brut, ou la source se deduit des
       arguments de l'appel plutot que du resultat (GitHub aujourd'hui ;
       tout futur outil du meme genre s'ajoute ici au besoin).

    Best-effort partout : jamais d'exception qui remonte jusqu'a la
    reponse -- les sources sont un bonus.
    """
    sources = _sources_depuis_json_generique(resultat_brut)
    if sources:
        return sources

    if appel["name"] in ("explorer_depot_github", "lire_fichier_depot_github"):
        return _sources_github_depuis_arguments(appel)

    return []


# Mêmes extensions que EXTENSIONS_FICHIER dans FichierChip.tsx (frontend)
# -- si une extension est ajoutée d'un côté, l'ajouter aussi de l'autre.
EXTENSIONS_FICHIER_GENERE = (
    "pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt", "zip", "json",
    "xml", "png", "jpg", "jpeg", "webp", "glb", "tex",
)
REGEX_FICHIER_GENERE = re.compile(
    r"https?://[^\s<>\"'\)\]]+\.(?:" + "|".join(EXTENSIONS_FICHIER_GENERE) + r")\b",
    re.IGNORECASE,
)


def _extraire_fichiers_generes(resultat_brut):
    """
    Detecte, dans le resultat brut d'un outil, tout lien vers un fichier
    genere -- generique par FORME d'URL (extension connue), pas par nom
    d'outil : aucune liste d'outils de generation a maintenir ici, un
    futur outil de generation est couvert automatiquement du moment que
    son URL se termine par une extension listee ci-dessus (2026-07-28,
    demande Bourama : le lien ne doit plus dependre de la fidelite du
    modele a le recopier correctement dans sa propre reponse -- voir
    evenement SSE "fichiers_generes" emis par _traiter_appels ci-dessous,
    et son rendu cote frontend qui reutilise FichierChip.tsx tel quel).
    Best-effort : jamais d'exception, renvoie [] si rien trouve.
    """
    if not isinstance(resultat_brut, str):
        return []
    vus = set()
    fichiers = []
    for match in REGEX_FICHIER_GENERE.finditer(resultat_brut):
        url = match.group(0)
        if url in vus:
            continue
        vus.add(url)
        fichiers.append({"url": url, "nom": url.rsplit("/", 1)[-1]})
    return fichiers


def _traiter_appels(appels, messages_agent, table_routage):
    """
    Execute une liste d'appels d'outils, en ajoutant le resultat de chacun
    a messages_agent au fur et a mesure. Des qu'un outil sensible
    (OUTILS_SENSIBLES) est rencontre, s'arrete AVANT de l'executer et leve
    _AttenteConfirmation avec les appels restants (lui inclus).

    Les appels "surs" qui precedent ce premier outil sensible (le cas le
    plus frequent : aucun outil sensible du tout dans le lot) sont
    executes EN PARALLELE plutot qu'un par un, pour ne pas payer en
    latence la somme des temps de reponse de chaque outil alors qu'ils
    sont independants les uns des autres (ex: deux recherches web
    simultanees). On ne parallelise jamais un outil sensible ni ce qui le
    suit : la garantie "on s'arrete avant de l'executer" doit rester
    valable meme dans le lot.
    """
    index_sensible = next(
        (i for i, appel in enumerate(appels) if appel["name"] in OUTILS_SENSIBLES),
        None,
    )
    appels_surs = appels if index_sensible is None else appels[:index_sensible]

    if appels_surs:
        for appel in appels_surs:
            yield {"type": "statut", "texte": f"{_nom_lisible(appel['name'])}..."}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(appels_surs)) as executor:
            futures = {
                executor.submit(_executer_un_appel, appel, table_routage): appel
                for appel in appels_surs
            }
            for future in concurrent.futures.as_completed(futures):
                appel = futures[future]
                try:
                    resultat = future.result()
                except Exception as e:
                    # CORRECTIF 2026-07-30 (audit UX) : avant, une exception
                    # levee par un outil (ex: generation_video.py/generation_3d.py
                    # si fal.ai change son format de reponse, envoyer_pour_signature
                    # si un signataire est mal forme, etc.) remontait telle
                    # quelle jusqu'a la cascade de secours dans chat(), qui la
                    # traitait comme une panne du modele Groq lui-meme -> bascule
                    # sur un modele de secours SANS AUCUN outil, sans jamais dire
                    # a la personne que sa generation avait echoue. En plus,
                    # messages_agent se retrouvait avec un tool_call sans reponse
                    # correspondante (puisqu'on n'atteignait jamais l'append plus
                    # bas), ce qui faisait aussi echouer les modeles de secours
                    # suivants (API tool-calling stricte sur ce point).
                    #
                    # Desormais : un outil qui echoue est un RESULTAT normal (visible
                    # dans le fil, explique au modele dans le meme tour), jamais une
                    # exception qui remonte. Le modele peut donc reagir dans sa
                    # propre reponse ("la generation a echoue, veux-tu reessayer ?")
                    # au lieu de changer de personnalite en silence.
                    logging.error(f"ERREUR OUTIL ({appel['name']}) : {e}")
                    resultat = f"Erreur : {_nom_lisible(appel['name'])} a échoué ({e})."
                    yield {"type": "statut_termine", "texte": f"{_nom_lisible(appel['name'])} a échoué"}
                    yield {
                        "type": "outil_resultat",
                        "nom_outil": appel["name"],
                        "nom_lisible": _nom_lisible(appel["name"]),
                        "resultat": resultat,
                    }
                    messages_agent.append({
                        "role": "tool",
                        "tool_call_id": appel["id"],
                        "content": resultat,
                    })
                    continue
                yield {"type": "statut_termine", "texte": f"{_nom_lisible(appel['name'])} effectuée"}
                # Généralisé (26/07, demande Bourama) : pour N'IMPORTE QUEL
                # outil, présent ou futur -- pas de liste à maintenir, voir
                # docstring de _resultat_pour_affichage.
                yield {
                    "type": "outil_resultat",
                    "nom_outil": appel["name"],
                    "nom_lisible": _nom_lisible(appel["name"]),
                    "resultat": _resultat_pour_affichage(resultat),
                }
                # Garanti indépendamment de ce que le modèle écrira ensuite
                # dans sa propre réponse -- voir _extraire_fichiers_generes
                # et INSTRUCTIONS_FORMATS_AFFICHAGE (le modèle est instruit
                # de ne plus réécrire ce lien lui-même, pour éviter le
                # doublon).
                fichiers_generes = _extraire_fichiers_generes(resultat)
                if fichiers_generes:
                    yield {
                        "type": "fichiers_generes",
                        "nom_outil": appel["name"],
                        "fichiers": fichiers_generes,
                    }
                sources = _extraire_sources(appel, resultat)
                if sources:
                    yield {"type": "sources", "sources": sources}
                messages_agent.append({
                    "role": "tool",
                    "tool_call_id": appel["id"],
                    "content": resultat,
                })

    if index_sensible is not None:
        raise _AttenteConfirmation(appels[index_sensible], appels[index_sensible + 1:])


def _evenement_confirmation(attente, messages_agent, outils_mcp, table_routage, modele=GROQ_PRIMARY, reasoning_effort=None, agent_nom=None):
    appel = attente.appel
    try:
        arguments_dict = json.loads(appel["arguments"] or "{}")
    except Exception:
        arguments_dict = {}
    return {
        "type": "confirmation_requise",
        "nom_outil": appel["name"],
        "nom_lisible": _nom_lisible(appel["name"]),
        # Message centré sur l'AGENT (2026-07-23) : "Nucleos va faire X",
        # pas une description technique de l'outil -- valable pour
        # n'importe quelle action sensible, pas seulement GitHub. Le
        # frontend peut afficher ce message directement, ou continuer à
        # composer le sien à partir de nom_lisible s'il préfère.
        "message": f"{agent_nom or 'Cet agent'} veut faire ceci : {_nom_lisible(appel['name'])}.",
        "agent_nom": agent_nom,
        "arguments": arguments_dict,
        "etat_reprise": {
            "messages_agent": messages_agent,
            "outils_mcp": outils_mcp,
            "table_routage": table_routage,
            "appel": appel,
            "appels_restants": attente.appels_restants,
            "modele": modele,
            "reasoning_effort": reasoning_effort,
            "agent_nom": agent_nom,
        },
    }


def _agent_groq(client_groq, messages_agent, outils_mcp, table_routage,
                 appels_en_cours_a_finir=None, modele=GROQ_PRIMARY, reasoning_effort=None, agent_nom=None,
                 rattrapage_tool_code_restant=1):
    """
    Boucle d'agent generique sur le modele Groq utilise (par defaut
    GROQ_PRIMARY, mais peut recevoir n'importe quel modele Groq qui sait
    faire du tool calling -> permet de reutiliser cette meme boucle pour
    les modeles de secours de GROQ_FALLBACKS, avec les outils MCP branches
    dessus aussi, plutot que de les perdre des que GROQ_PRIMARY sature son
    quota TPM.

    `reasoning_effort`, si fourni (ex: "none"), est transmis tel quel a
    l'appel Groq : certains modeles de secours (ex: qwen3) font du
    raisonnement par defaut, ce qui peut etre desactive pour rester rapide.

    Genere des evenements "statut"/"reponse"/"confirmation_requise".
    S'arrete (sans exception) des qu'une reponse finale a ete produite OU
    qu'une confirmation est necessaire.

    `appels_en_cours_a_finir`, si fourni, est traite AVANT le prochain
    appel a Groq : c'est le cas lors d'une reprise apres confirmation, ou
    il faut d'abord finir le lot d'outils du tour precedent (executer les
    appels restants, ou re-demander confirmation si l'un d'eux est aussi
    sensible) avant de redemander une reponse au modele.

    `rattrapage_tool_code_restant` (29/07, demande Bourama) : quand le
    modele ecrit un faux appel d'outil (bloc ```TOOL_CODE, voir
    _trouver_debut_tool_code) au lieu d'utiliser le vrai mecanisme de tool
    calling, l'ancien comportement se contentait de masquer le texte a
    l'utilisateur SANS jamais executer l'action demandee -- meme quand la
    detection fonctionnait, le vrai probleme (rien n'est execute) restait
    entier. Desormais, une detection de ce cas declenche UNE tentative de
    rattrapage automatique : un message correctif est injecte et le modele
    est relance avec ce budget decremente a 0, pour eviter toute boucle. Si
    le meme bug se reproduit malgre le rattrapage, un message d'erreur clair
    est affiche a l'utilisateur plutot que de le laisser sans reponse.
    """
    kwargs_reasoning = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    # reasoning_format="parsed" separe le raisonnement (delta.reasoning) du
    # texte de reponse final (delta.content). IMPORTANT : la doc Groq
    # (console.groq.com/docs/reasoning) precise que ce parametre n'est PAS
    # supporte par gpt-oss-20b/120b (eux exposent deja le raisonnement par
    # defaut dans le champ "reasoning") -- on ne l'envoie donc qu'aux
    # modeles qui le supportent reellement (qwen3), pour eviter un
    # comportement indefini cote API sur GROQ_PRIMARY (gpt-oss-120b).
    if modele in MODELES_AVEC_REASONING_EFFORT and "gpt-oss" not in modele and reasoning_effort != "none":
        kwargs_reasoning["reasoning_format"] = "parsed"

    if appels_en_cours_a_finir:
        try:
            for event in _traiter_appels(appels_en_cours_a_finir, messages_agent, table_routage):
                yield event
        except _AttenteConfirmation as attente:
            yield _evenement_confirmation(attente, messages_agent, outils_mcp, table_routage, modele, reasoning_effort, agent_nom)
            return

    for _ in range(MAX_ETAPES_OUTILS):
        # Forçage réel de l'appel d'outil (2026-07-28, correction demandée
        # par Bourama) : jusqu'ici, un outil sélectionné (bouton Outils ou
        # clic sur une suggestion du routeur) n'était qu'"encouragé" via une
        # instruction texte dans le system prompt (voir plus haut, "OUTIL(S)
        # ACTIF(S) POUR CE MESSAGE") -- rien n'empêchait le modèle de
        # répondre sans l'appeler malgré tout (confirmé en test réel :
        # aucun appel d'outil dans certains tours pourtant sélectionnés).
        # tool_choice="required" oblige réellement l'API à renvoyer un
        # appel d'outil plutôt qu'une réponse texte -- mais UNIQUEMENT tant
        # qu'aucun outil n'a encore été appelé ce tour-ci (sinon la 2e
        # itération de cette même boucle, censée rédiger la réponse finale
        # après coup, serait forcée de rappeler un outil en boucle, sans
        # jamais pouvoir conclure). On revérifie l'état RÉEL de
        # messages_agent à chaque itération plutôt qu'un simple booléen
        # "premier passage" : ça couvre aussi bien le cas normal que la
        # reprise après confirmation (appels_en_cours_a_finir, traité juste
        # au-dessus) où un message "tool" existe déjà avant même la
        # première itération de cette boucle.
        outil_deja_appele = any(m.get("role") == "tool" for m in messages_agent)
        kwargs_tool_choice = {"tool_choice": "required"} if (outils_mcp and not outil_deja_appele) else {}

        # Reserve de tokens de sortie (2026-07-30, correction demandee par
        # Bourama) : jusqu'ici 8192 fixe pour TOUS les appels, principal
        # comme fallbacks, meme quand un outil est force. Or Groq compare
        # cette reserve demandee (pas l'usage reel) a la limite TPM du
        # modele AVANT meme de generer quoi que ce soit -- plusieurs
        # modeles de la cascade plafonnent autour de 8000 TPM cote gratuit,
        # donc demander 8192 fait echouer l'appel des le depart, meme sur
        # un premier message tout simple (ex: "genere-moi une image"),
        # constate par Bourama le 30/07. Trois cas distincts desormais :
        #   1. Outil force, pas encore appele ce tour-ci (tool_choice=
        #      "required" ci-dessus) : le modele ne fait qu'emettre un
        #      appel structure, pas de prose -- reserve minimale.
        #   2. Reponse texte normale sur GROQ_PRIMARY (gpt-oss-120b) :
        #      garder 8192, c'est le fix d'origine (27/07) contre les
        #      reponses coupees en plein milieu (raisonnement + texte
        #      partagent le meme budget sur ce modele).
        #   3. Reponse texte normale sur un modele de secours : reduit a
        #      4096 -- ces modeles servent justement a economiser du
        #      debit quand le principal sature, pas de raison de leur
        #      reserver autant que lui.
        if kwargs_tool_choice:
            reserve_tokens = 512
        elif modele == GROQ_PRIMARY:
            reserve_tokens = 8192
        else:
            reserve_tokens = 4096

        completion = client_groq.chat.completions.create(
            model=modele,
            messages=messages_agent,
            max_completion_tokens=reserve_tokens,
            tools=outils_mcp if outils_mcp else None,
            stream=True,
            timeout=DELAI_MAX_PAR_APPEL,
            **kwargs_reasoning,
            **kwargs_tool_choice,
        )

        reponse_directe = False
        appels_en_cours = {}  # index -> {"id", "name", "arguments"}
        # Filet de securite contre les bugs Groq connus (JSON casse, recopie
        # brute d'un resultat d'outil, faux bloc ```TOOL_CODE) : voir
        # _traiter_fragment_texte / _finaliser_fragment_texte plus haut.
        #
        # IMPORTANT (bug trouve le 26/07/2026, signale par Bourama) : cette
        # verification portait AVANT seulement sur les 60 tout premiers
        # caracteres du flux, une seule fois -- une fois la reponse jugee
        # "normale" au debut, plus RIEN ne revenait verifier le reste du
        # flux. Desormais on re-verifie en continu pendant TOUTE la duree
        # du flux, pas juste au debut.
        #
        # CORRECTION (29/07, signalee par Bourama) : pour le cas specifique
        # du faux bloc TOOL_CODE, on ne masque plus que le bloc lui-meme
        # (bornes precises, voir _traiter_fragment_texte) -- le texte avant
        # ET apres le bloc reste visible, au lieu de tout basculer en
        # "raisonnement" des la detection et jusqu'a la fin du passage.
        etat_filtre = _nouvel_etat_filtre_texte()
        dernier_finish_reason = None
        dernier_usage = None

        for chunk in completion:
            # Diagnostic du bug de troncature (27/07) : le dernier chunk
            # du flux porte finish_reason ("stop" = fin normale, "length" =
            # coupé faute de budget de tokens) et parfois x_groq.usage
            # (tokens consommés) -- on les garde pour les logger une fois
            # le flux terminé, plutôt que de deviner la cause à l'aveugle.
            if chunk.choices and chunk.choices[0].finish_reason:
                dernier_finish_reason = chunk.choices[0].finish_reason
            if getattr(chunk, "x_groq", None) and getattr(chunk.x_groq, "usage", None):
                dernier_usage = chunk.x_groq.usage

            delta = chunk.choices[0].delta

            raisonnement = getattr(delta, "reasoning", None)
            if raisonnement:
                yield {"type": "raisonnement", "texte": raisonnement}

            if delta.content:
                reponse_directe = True
                for evenement in _traiter_fragment_texte(etat_filtre, delta.content, messages_agent):
                    yield evenement

            if delta.tool_calls:
                for fragment in delta.tool_calls:
                    etat = appels_en_cours.setdefault(
                        fragment.index, {"id": None, "name": "", "arguments": ""}
                    )
                    if fragment.id:
                        etat["id"] = fragment.id
                    if fragment.function:
                        if fragment.function.name:
                            etat["name"] += fragment.function.name
                        if fragment.function.arguments:
                            etat["arguments"] += fragment.function.arguments

        if dernier_finish_reason == "length":
            logging.error(
                f"TRONCATURE (finish_reason=length) sur {modele} -- usage : {dernier_usage}"
            )
        elif dernier_finish_reason and dernier_finish_reason != "stop":
            logging.info(f"Fin de flux {modele} avec finish_reason={dernier_finish_reason} (usage : {dernier_usage})")

        for evenement in _finaliser_fragment_texte(etat_filtre, messages_agent):
            yield evenement

        if etat_filtre["tool_code_detecte"] and not appels_en_cours:
            # Faux appel d'outil détecté (bloc ```TOOL_CODE) et aucun vrai
            # tool_calls reçu ce tour-ci : rien n'a été réellement exécuté.
            # Voir la docstring de _agent_groq pour le mécanisme de
            # rattrapage.
            if rattrapage_tool_code_restant > 0 and outils_mcp:
                logging.warning(
                    f"Faux appel d'outil (bloc TOOL_CODE) détecté sur {modele} -- rattrapage automatique déclenché."
                )
                messages_agent.append({
                    "role": "system",
                    "content": (
                        "Tu viens d'écrire un faux appel d'outil sous forme de bloc de "
                        "code (```TOOL_CODE...```) au lieu d'utiliser le vrai mécanisme "
                        "d'appel d'outil de l'API. Cela n'exécute rien du tout. Si tu "
                        "veux exécuter un outil, utilise IMPÉRATIVEMENT le vrai "
                        "mécanisme d'appel d'outil (tool_calls), jamais de bloc de "
                        "code. N'écris plus jamais de bloc ```TOOL_CODE```."
                    ),
                })
                yield from _agent_groq(
                    client_groq, messages_agent, outils_mcp, table_routage,
                    modele=modele, reasoning_effort=reasoning_effort, agent_nom=agent_nom,
                    rattrapage_tool_code_restant=rattrapage_tool_code_restant - 1,
                )
                return
            else:
                logging.error(
                    f"Faux appel d'outil (bloc TOOL_CODE) détecté à nouveau sur {modele} après rattrapage (ou sans outil dispo) -- abandon."
                )
                yield {
                    "type": "reponse",
                    "texte": "Désolé, je n'ai pas réussi à exécuter l'action demandée. Peux-tu réessayer ou reformuler ta demande ?",
                }
                return

        if reponse_directe and not appels_en_cours:
            # Cas normal : reponse texte pure, aucun outil appele -- on
            # peut s'arreter la.
            logging.info(f"Réponse via GROQ (sans outil, streaming): {modele}")
            return

        if not appels_en_cours:
            return  # ni contenu ni outil (rare) : rien a faire de plus

        # BUG CORRIGE (2026-07-26, trouve par Bourama) : avant ce fix, la
        # simple presence de texte (reponse_directe=True) faisait sortir
        # la fonction ICI, AVANT d'atteindre le code qui execute
        # appels_en_cours plus bas -- un appel d'outil recu dans le meme
        # passage qu'un peu de texte (ex: un modele qui dit "D'accord, je
        # m'en occupe..." en meme temps qu'il appelle l'outil) etait donc
        # silencieusement perdu : jamais execute, aucune erreur visible,
        # l'IA repondait juste comme si de rien n'etait. Desormais, si
        # appels_en_cours n'est pas vide, on continue vers le traitement
        # des appels ci-dessous, meme si du texte a deja ete stream et
        # affiche.

        appels = [appels_en_cours[i] for i in sorted(appels_en_cours)]

        messages_agent.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": appel["id"],
                    "type": "function",
                    "function": {"name": appel["name"], "arguments": appel["arguments"]},
                }
                for appel in appels
            ],
        })

        try:
            for event in _traiter_appels(appels, messages_agent, table_routage):
                yield event
        except _AttenteConfirmation as attente:
            yield _evenement_confirmation(attente, messages_agent, outils_mcp, table_routage, modele, reasoning_effort, agent_nom)
            return

        # (2026-07-30, demande Bourama : retour au round-trip standard
        # apres execution des outils -- meme pour les outils de
        # generation/OUTILS_AUTONOMES. On ne s'arrete plus ici : la
        # boucle `for` continue naturellement vers un nouvel appel Groq
        # avec les resultats d'outils ajoutes a messages_agent, pour que
        # le modele formule sa reponse finale a partir d'eux -- le
        # fonctionnement standard du tool calling.
        #
        # La reponse du modele n'est PLUS filtree/masquee cote backend
        # (choix explicite de Bourama, 2026-07-30) : si le modele
        # recopie/casse un lien, tant pis, sa reponse s'affiche telle
        # quelle. Le vrai resultat fiable de l'outil (URL correcte
        # comprise) reste disponible independamment, via les evenements
        # outil_resultat/fichiers_generes deja emis par _traiter_appels
        # -- voir OutilResultatBulle.tsx/FichierChip.tsx cote frontend
        # pour leur affichage en menu repliable en bas de la reponse.


    # MAX_ETAPES_OUTILS epuise sans reponse directe : on force une reponse
    # finale (sans autoriser de nouvel appel d'outil).
    completion = client_groq.chat.completions.create(
        model=modele,
        messages=messages_agent,
        max_completion_tokens=None,
        tools=outils_mcp if outils_mcp else None,
        stream=True,
        timeout=DELAI_MAX_PAR_APPEL,
        **kwargs_reasoning,
    )
    # Meme filtre que dans la boucle principale plus haut (voir
    # _traiter_fragment_texte / _finaliser_fragment_texte) -- corrige le
    # meme bug ici aussi puisque ce chemin duplique la meme logique de
    # streaming. Pas de rattrapage ici : le budget MAX_ETAPES_OUTILS est
    # deja epuise, on affiche directement un message d'erreur si un faux
    # bloc TOOL_CODE est quand meme detecte, plutot que de relancer encore.
    etat_filtre = _nouvel_etat_filtre_texte()
    for chunk in completion:
        delta = chunk.choices[0].delta
        raisonnement = getattr(delta, "reasoning", None)
        if raisonnement:
            yield {"type": "raisonnement", "texte": raisonnement}
        token = delta.content or ""
        if token:
            for evenement in _traiter_fragment_texte(etat_filtre, token, messages_agent):
                yield evenement
    for evenement in _finaliser_fragment_texte(etat_filtre, messages_agent):
        yield evenement
    if etat_filtre["tool_code_detecte"]:
        logging.error(f"Faux appel d'outil (bloc TOOL_CODE) détecté sur {modele} (budget d'étapes épuisé) -- abandon.")
        yield {
            "type": "reponse",
            "texte": "Désolé, je n'ai pas réussi à exécuter l'action demandée. Peux-tu réessayer ou reformuler ta demande ?",
        }
    logging.info(f"Réponse via GROQ (avec outil): {modele}")


def _capturer_reponse(generateur, accumulateur):
    """
    Relaie tous les evenements d'un generateur tel quel, en accumulant au
    passage le texte des evenements "reponse" dans `accumulateur` (une
    liste, mutee en place). Permet de reconstruire la reponse finale
    complete une fois le generateur epuise, pour la persister en memoire,
    sans dupliquer cette logique a chaque point de sortie de chat().
    """
    for event in generateur:
        if event["type"] == "reponse":
            accumulateur.append(event["texte"])
        yield event


def chat(message_utilisateur=None, historique=None, user_id=None, reprise=None, agent_id=None, conversation_id=None, longueur_reponse="moyenne", image_url=None, localisation=None, fuseau_horaire=None, images_base64=None, recherche_forcee=False, outil_force=None, ignorer_suggestion_outils=False, modele_force=None, sans_enseignant=False):
    """
    Generateur d'evenements. Chaque element produit est un dictionnaire :
    - {"type": "statut", "texte": "..."}         -> un outil MCP est en cours d'utilisation
    - {"type": "statut_termine", "texte": "..."} -> cet outil a fini (ou a ete annule)
    - {"type": "outil_resultat", "nom_outil": "...", "nom_lisible": "...", "resultat": "..."}
      -> ce que CET outil a concretement execute/retourne (tronque a 3000 caracteres pour
      l'affichage, voir _resultat_pour_affichage -- le contenu complet reste envoye au
      modele separement). Generalise a tout outil, present ou futur (26/07) : distinct du
      raisonnement libre du modele, qui lui peut paraphraser/melanger ce contenu avec
      d'autres reflexions dans son propre texte -- voir OutilResultatBulle.tsx cote frontend.
    - {"type": "raisonnement", "texte": "..."}   -> fragment de raisonnement interne du modele, avant la reponse finale (modeles de MODELES_AVEC_REASONING_EFFORT uniquement)
    - {"type": "sources", "sources": [{"titre": "...", "url": "..."}]} -> resultats d'une
      recherche web (Tavily) utilisee pour repondre. Peut etre emis plusieurs fois dans le
      meme echange (plusieurs recherches) -- l'appelant accumule/fusionne, ne remplace pas.
    - {"type": "reponse", "texte": "..."}        -> morceau de la reponse finale (streaming)
    - {"type": "fichiers_generes", "nom_outil": "...", "fichiers": [{"url": "...", "nom": "..."}]}
      -> (28/07) emis des qu'un outil produit un fichier telechargeable (detecte par
      extension d'URL, voir _extraire_fichiers_generes) -- INDEPENDANT de ce que le
      modele ecrit dans sa reponse texte, garanti a chaque fois. Le frontend l'affiche
      en carte fichier (FichierChip.tsx) a la fin du message assistant.
    - {"type": "outils_suggeres", "outils": ["nom_outil", ...]} -> routeur d'outils (28/07,
      _router_outils) : DERNIER evenement de l'echange (rien n'est sauvegarde, aucune
      reponse n'est generee ce tour-ci). Emis a la place d'une reponse quand aucun
      outil n'est deja force (ni menu manuel, ni suggestion precedente) ET que le
      routeur juge au moins un outil pertinent. Le frontend affiche un bouton par
      outil ; un clic renvoie la MEME question avec ce nom dans outil_force, exactement
      comme une selection manuelle -- voir BarreDeSaisie.tsx / ChatIA.tsx.
    - {"type": "confirmation_requise", ...}      -> un outil qui MODIFIE les donnees de
      l'utilisateur (ex: creer une page Notion) attend une confirmation avant de s'executer.
      Contient "nom_lisible", "arguments" (a afficher a l'utilisateur), et "etat_reprise"
      (a repasser tel quel a chat(reprise=...) une fois la decision prise).
    - {"type": "meta", "message_id_user": ..., "message_id_assistant": ...,
      "created_at_assistant": ...}                -> DERNIER evenement emis, une fois
      l'echange persiste dans historique_conversations (voir _sauvegarder_echange).
      Ids necessaires cote appelant (API du frontend Next.js) pour indexer un
      feedback like/dislike sur CE message precis. Absent si l'utilisateur n'est pas
      connecte (user_id=None) : dans ce cas aucun feedback n'est possible non plus.

    Le frontend doit distinguer ces types pour savoir quoi afficher, et ne
    garder que "reponse" dans l'historique de conversation.

    `longueur_reponse` (optionnel, "courte" | "moyenne" | "longue", defaut
    "moyenne" = comportement historique inchange) pilote la longueur de la
    reponse generee via une consigne ajoutee au prompt systeme (voir
    INSTRUCTIONS_LONGUEUR_REPONSE). Migration Next.js, section 3.3 :
    modifiable a chaque message par l'utilisateur.

    `user_id` (session.user.id de Supabase Auth, ou None si l'utilisateur n'est
    pas connecte) est transmis au registre d'outils pour que les outils "par
    utilisateur" (ex: Notion) sachent pour qui aller chercher un token. Il sert
    aussi a scoper la memoire long-terme (conversation_summaries, scope par
    user_id seul depuis le compte unifie de juillet 2026 -> le resume suit
    l'utilisateur d'un agent a l'autre, pas cloisonne par agent) : sans user_id
    (utilisateur non connecte), rien n'est lu ni ecrit en memoire.

    `agent_id` (optionnel) determine quel prompt systeme et quelles donnees
    RAG utiliser (voir configuration.py / retriever.py). Si non fourni, on
    utilise le secret AGENT_ID du deploiement, puis AGENT_ID_PAR_DEFAUT.

    `conversation_id` (optionnel, 2026-07-13) identifie le fil de
    discussion affiche dans la sidebar de chat.py (liste de conversations
    distinctes et cliquables, façon Claude.ai) -- genere cote chat.py, une
    valeur par conversation, pas par message. Simplement transmis a
    _sauvegarder_echange(). None accepte : un appelant qui ne gere pas
    encore les fils continue de fonctionner normalement.

    Pour reprendre apres une confirmation_requise, appeler :
        chat(reprise={"etat_reprise": evenement["etat_reprise"], "approuve": True|False})
    (message_utilisateur/historique/user_id sont alors ignores.)
    LIMITE CONNUE : la memoire long-terme n'est PAS persistee sur ce chemin de
    reprise (etat_reprise ne transporte ni agent_id, ni user_id, ni le message
    utilisateur d'origine, ni conversation_id). A etendre si besoin en les
    ajoutant a etat_reprise dans _evenement_confirmation.

    `image_url` (optionnel, 2026-07-20) : URL publique d'une image jointe au
    message (voir api/uploads.py:uploader_image_chat). Si presente, on ne
    passe PAS par le cascade Groq habituel (aucun des modeles Groq de
    GROQ_PRIMARY/GROQ_FALLBACKS n'est multimodal) : on route directement et
    uniquement vers Gemini, seul modele vision de la cascade. Consequence
    connue : pas d'outils MCP (Notion, Wolfram, recherche web) sur un
    message avec image, comme pour le fallback Gemini texte plus bas. Si
    Gemini echoue sur ce chemin, on renvoie MESSAGE_ERREUR direct (pas de
    retry cascade complet comme pour le texte : un seul modele disponible).

    `localisation` (optionnel, 2026-07-20) : dict {"latitude":..., "longitude":...}
    transmis explicitement par l'utilisateur (bouton dedie, jamais automatique).
    Injecte en fin de prompt systeme, jamais traite comme un fait dit par
    l'utilisateur. N'affecte ni le cascade ni le choix de modele.

    `fuseau_horaire` (optionnel, 2026-07-20) : nom de fuseau IANA lu depuis
    le navigateur (Intl.DateTimeFormat().resolvedOptions().timeZone, voir
    ChatIA.tsx:envoyerMessage). PAS de fuseau fixe côté serveur -- Djiguignè
    est panafricain, aucune hypothèse de pays. Repli sur UTC si absent ou
    invalide.

    `images_base64` (optionnel, 2026-07-20) : liste de frames JPEG en
    base64, extraites d'une vidéo uploadée (voir
    api/uploads.py:uploader_video_chat et core/video.py:_extraire_frames_video).
    Combinable avec image_url (rare en pratique) -- toutes les images sont
    envoyées à Gemini dans le MÊME message. Le son de la vidéo n'est PAS
    envoyé ici : il est transcrit à part (Whisper) et injecté comme texte
    dans message_utilisateur par le frontend, avant l'appel à chat().

    `recherche_forcee` (optionnel, 2026-07-23, defaut False) : icône de
    recherche dans la barre de saisie (djiguigne-frontend) -- force une
    consigne de recherche web systematique pour CE message. Le modele
    peut de toute facon decider seul d'utiliser Tavily sans ce flag
    (tool-calling normal, des lors que le serveur "tavily" est active
    pour l'agent) ; ce parametre garantit juste que ca arrive.

    Liens colles dans message_utilisateur (page web ou video YouTube) :
    recuperes automatiquement (_enrichir_message_avec_urls) et ajoutes en
    contexte APRES le message original avant envoi au modele. Le message
    BRUT (sans ce contenu) reste ce qui est sauvegarde dans l'historique.

    Si TOUS les maillons de la cascade (Groq principal, Gemini, fallbacks
    Groq) echouent uniquement a cause d'un timeout, on retente une seconde
    fois toute la cascade. Si au moins une erreur n'est pas un timeout (ex:
    429, cle invalide...), on ne retente pas et on part direct sur le
    message d'erreur.

    `modele_force` (optionnel, 02/08/2026, voir core/fournisseurs_llm.py) :
    modele_id premium (Claude/GPT/Gemini/DeepSeek) choisi par l'utilisateur
    pour CE message, deja revalide cote appelant (api/chat.py) contre les
    modeles reellement debloques de l'agent -- ce module ne refait PAS
    cette verification, il fait confiance a l'appelant. Ignore si un
    image_url/images_base64 est present (la vision reste reservee au
    chemin Gemini existant plus bas). LIMITE CONNUE : ce chemin ne passe
    PAS par le cascade Groq ni par les outils MCP (pas de RAG, Wolfram,
    Notion, recherche web...) -- reponse texte seule, comme le chemin
    vision Gemini juste en dessous. A etendre en v2 si le tool-calling
    multi-fournisseurs est prioritaire.
    """
    if reprise is not None:
        etat = reprise["etat_reprise"]
        approuve = reprise["approuve"]
        messages_agent = etat["messages_agent"]
        outils_mcp = etat["outils_mcp"]
        table_routage = etat["table_routage"]
        appel = etat["appel"]
        modele_reprise = etat.get("modele", GROQ_PRIMARY)
        reasoning_effort_reprise = etat.get("reasoning_effort")

        client_groq = Groq(api_key=get_secret("GROQ_API_KEY"), max_retries=0)

        if approuve:
            yield {"type": "statut", "texte": f"{_nom_lisible(appel['name'])}..."}
            try:
                arguments = json.loads(appel["arguments"] or "{}")
            except Exception:
                arguments = {}
            # CORRECTIF 2026-07-30 (audit UX, même principe que
            # _traiter_appels ci-dessus) : cet appel n'était protégé par
            # AUCUN try/except -- une exception ici (ex: token GitHub
            # invalide/expiré, panne réseau) faisait planter tout le flux
            # sans passer ni par la cascade de secours, ni par MESSAGE_ERREUR :
            # rien n'était renvoyé à la personne, pas même une erreur générique.
            deja_ajoute_a_messages_agent = False
            try:
                resultat = appeler_outil(appel["name"], arguments, table_routage)
            except Exception as e:
                logging.error(f"ERREUR OUTIL APPROUVÉ ({appel['name']}) : {e}")
                resultat = f"Erreur : {_nom_lisible(appel['name'])} a échoué ({e})."
                yield {"type": "statut_termine", "texte": f"{_nom_lisible(appel['name'])} a échoué"}
                yield {
                    "type": "outil_resultat",
                    "nom_outil": appel["name"],
                    "nom_lisible": _nom_lisible(appel["name"]),
                    "resultat": resultat,
                }
                messages_agent.append({
                    "role": "tool",
                    "tool_call_id": appel["id"],
                    "content": resultat,
                })
                deja_ajoute_a_messages_agent = True
            if not deja_ajoute_a_messages_agent:
                yield {"type": "statut_termine", "texte": f"{_nom_lisible(appel['name'])} effectuée"}
                yield {
                    "type": "outil_resultat",
                    "nom_outil": appel["name"],
                    "nom_lisible": _nom_lisible(appel["name"]),
                    "resultat": _resultat_pour_affichage(resultat),
                }
        else:
            resultat = "Action annulée par l'utilisateur : cet outil n'a pas été exécuté."
            yield {"type": "statut_termine", "texte": f"{_nom_lisible(appel['name'])} annulée"}
            deja_ajoute_a_messages_agent = False

        if not deja_ajoute_a_messages_agent:
            messages_agent.append({
                "role": "tool",
                "tool_call_id": appel["id"],
                "content": resultat,
            })

        try:
            yield from _agent_groq(
                client_groq, messages_agent, outils_mcp, table_routage,
                appels_en_cours_a_finir=etat.get("appels_restants") or None,
                modele=modele_reprise, reasoning_effort=reasoning_effort_reprise,
                agent_nom=etat.get("agent_nom"),
            )
        except Exception as e:
            logging.error(f"ERREUR GROQ (reprise apres confirmation) {modele_reprise}: {e}")
            yield {"type": "reponse", "texte": MESSAGE_ERREUR}
        return

    # --- Chemin normal : nouvelle question --------------------------------
    if historique is None:
        historique = []

    # Modération d'entrée (Llama Guard, 25/07) : vérifie le message BRUT de
    # l'étudiant avant tout le reste (prompt système, RAG, appel du modèle
    # principal...) -- fail-fast, pas la peine de construire quoi que ce
    # soit si le message est bloqué. Demande Bourama (25/07) : uniquement
    # l'entrée pour l'instant, pas de vérification sur la sortie de l'agent
    # (pour limiter le surcoût en tokens -- l'agent garde ses garde-fous via
    # son prompt système + le filet JSON cassé déjà en place).
    if message_utilisateur:
        est_sur, categorie = _verifier_message_utilisateur(message_utilisateur)
        if not est_sur:
            logging.warning(f"Message bloqué par la modération d'entrée (Llama Guard, {categorie}).")
            yield {"type": "reponse", "texte": MESSAGE_CONTENU_BLOQUE}
            return

    if agent_id is None:
        agent_id = get_secret("AGENT_ID") or AGENT_ID_PAR_DEFAUT

    # Routeur d'outils (2026-07-28, demande Bourama) : voir _router_outils
    # plus haut pour la doc complète. Ne se déclenche QUE si rien n'est
    # déjà forcé (ni sélection manuelle via BarreDeSaisie.tsx, ni clic sur
    # une suggestion précédente qui a renvoyé outil_force lui-même) --
    # sinon on tournerait en boucle. Pas de chemin image/vidéo (Gemini,
    # aucun outil MCP dans cette branche de toute façon, voir plus bas) ni
    # de reprise (déjà retournée avant ce point).
    #
    # ignorer_suggestion_outils (31/07, demande Bourama) : bouton "Aucun"
    # à côté des suggestions -- le routeur se trompe souvent (suggère un
    # outil sans rapport avec la question), l'utilisateur doit pouvoir
    # relancer sa question SANS repasser par le routeur (sinon, comme
    # outil_force serait vide/falsy, la condition ci-dessous re-déclencherait
    # le routeur et redonnerait potentiellement la même suggestion à côté --
    # boucle silencieuse pour l'utilisateur). Distinct de outil_force=None
    # normal : ici on VEUT explicitement zéro outil, pas "laisse le routeur
    # décider".
    # Perf (10/08, demande Bourama : "avant c'était quasi instantané") :
    # le routeur d'outils est un appel LLM séparé et complet (voir
    # _router_outils), payé en SÉQUENCE avant même de commencer à
    # construire le prompt de la vraie réponse -- gros ajout de latence
    # sur quasi tous les messages texte normaux. Dans l'écrasante
    # majorité des cas, le routeur ne suggère RIEN (voir ses instructions :
    # "liste vide si rien n'est pertinent", conçu pour rester silencieux
    # sauf besoin réel), auquel cas le prompt qu'on aurait construit sans
    # lui est de toute façon exactement le bon. On lance donc le routeur
    # ET la construction "optimiste" du prompt (comme si aucun outil
    # n'était suggéré, cas normal AVANT ce correctif) EN PARALLÈLE :
    # - routeur muet (cas normal) -> on utilise directement ce qui a déjà
    #   été calculé en parallèle, latence du routeur totalement absorbée.
    # - routeur_outils_auto=false (comportement bouton) -> retour
    #   immédiat avec l'événement outils_suggeres comme avant, le travail
    #   optimiste est simplement jeté (rien de cassé, coût négligeable).
    # - routeur_outils_auto=true ET des outils sont suggérés (seul cas où
    #   outil_force change réellement) -> le prompt optimiste ne convient
    #   plus, on le recalcule avec le bon outil_force, exactement comme
    #   avant ce correctif (donc jamais plus lent que l'ancien
    #   comportement dans ce cas rare, seulement dans les cas fréquents
    #   où ça ne change rien).
    outils_suggeres = []
    routeur_auto = False
    if not outil_force and not ignorer_suggestion_outils and message_utilisateur and not image_url and not images_base64:
        def _tache_routeur():
            outils_disponibles_agent, _ = lister_outils_autorises_pour_agent(get_secret, user_id, agent_id)
            return _router_outils(message_utilisateur, outils_disponibles_agent, historique)

        def _tache_prompt_optimiste():
            outils_mcp, table_routage = lister_tous_les_outils(get_secret, user_id, agent_id, None)
            system_final = _construire_system_prompt(message_utilisateur, agent_id, user_id, longueur_reponse, fuseau_horaire, recherche_forcee, None, sans_enseignant)
            return outils_mcp, table_routage, system_final

        with concurrent.futures.ThreadPoolExecutor() as executor:
            f_routeur = executor.submit(_tache_routeur)
            f_optimiste = executor.submit(_tache_prompt_optimiste)
        outils_suggeres = f_routeur.result()
        outils_mcp, table_routage, system_final = f_optimiste.result()
        outil_force_verifie = None

        if outils_suggeres:
            # routeur_outils_auto (03/08, demande Bourama, agent par agent) :
            # colonne sur `agents`, false par defaut. Si true pour CET agent,
            # on saute l'etape bouton cliquable (evenement "outils_suggeres")
            # et on envoie directement la suggestion du routeur au modele,
            # comme si l'utilisateur avait force ces outils lui-meme --
            # aucune confirmation cliquee. Les autres agents gardent le
            # comportement bouton normal (return ci-dessous inchange).
            try:
                agent_ligne = (
                    supabase.table("agents").select("routeur_outils_auto").eq("id", agent_id).maybe_single().execute()
                )
                routeur_auto = bool((agent_ligne.data or {}).get("routeur_outils_auto"))
            except Exception as e:
                logging.error(f"ERREUR lecture routeur_outils_auto agent={agent_id} : {e}")
                routeur_auto = False

            if routeur_auto:
                # Prompt optimiste invalide (calculé avec outil_force=None) :
                # DOIT être explicitement jeté, sinon le bloc plus bas
                # (`if system_final is None`) le laisserait passer tel
                # quel malgré outil_force mis à jour juste en dessous --
                # seul cas où on repaie le coût séquentiel, exactement
                # comme avant ce correctif.
                outil_force = outils_suggeres
                outils_mcp = table_routage = system_final = None
            else:
                yield {"type": "outils_suggeres", "outils": outils_suggeres}
                return
    else:
        outils_mcp = table_routage = system_final = None  # recalculés ci-dessous dans tous les autres cas

    # CORRECTION (29/07, Bourama) : la liste réelle d'outils (celle qui
    # part dans tools=... vers Groq, filtrée par autorisation agent en
    # base) doit être calculée AVANT le system prompt, et c'est ELLE qui
    # doit servir à annoncer "OUTIL(S) ACTIF(S)" -- jamais outil_force brut
    # (sélection frontend non vérifiée). Avant ce fix : si un outil
    # sélectionné (ex: generer_code) n'était pas autorisé en base pour cet
    # agent, il disparaissait silencieusement de outils_mcp mais le system
    # prompt continuait d'affirmer au modèle qu'il était "disponible et
    # prêt à être appelé" -- contradiction qui pouvait pousser le modèle à
    # halluciner un faux appel (bloc TOOL_CODE) plutôt que d'utiliser un
    # vrai outil absent de son schéma technique réel.
    if system_final is None:
        if image_url or images_base64:
            # Chemin image = Gemini, aucun outil MCP jamais utilisé ici (voir
            # plus bas) -- inutile d'interroger les serveurs MCP pour rien.
            outils_mcp, table_routage = [], {}
            outil_force_verifie = outil_force
        else:
            outils_mcp, table_routage = lister_tous_les_outils(get_secret, user_id, agent_id, outil_force)
            outil_force_verifie = [o["function"]["name"] for o in outils_mcp] if outil_force else outil_force
        system_final = _construire_system_prompt(message_utilisateur, agent_id, user_id, longueur_reponse, fuseau_horaire, recherche_forcee, outil_force_verifie, sans_enseignant)

    if localisation and localisation.get("latitude") is not None and localisation.get("longitude") is not None:
        # Contexte "système/environnement" (2026-07-20) : position GPS
        # transmise explicitement par l'utilisateur (bouton dédié côté
        # frontend, jamais automatique/silencieux -- voir BarreDeSaisie.tsx
        # et la permission navigateur navigator.geolocation). Ajoutée en
        # fin de prompt système, jamais comme un fait affirmé par
        # l'utilisateur lui-même.
        system_final += (
            "\n\nContexte de localisation (fourni par le navigateur de "
            "l'utilisateur, à utiliser seulement si pertinent pour la "
            f"question) : latitude {localisation['latitude']}, "
            f"longitude {localisation['longitude']}."
        )

    # Liens collés dans le message (page web ou vidéo YouTube) : récupérés
    # ICI, sur le message pour le modèle uniquement -- message_utilisateur
    # (brut, sans le contenu des liens) reste ce qui est sauvegardé dans
    # l'historique via _sauvegarder_echange plus bas.
    message_pour_modele = _enrichir_message_avec_urls(message_utilisateur, user_id)

    messages_base = [{"role": "system", "content": system_final}]
    messages_base += historique
    messages_base.append({"role": "user", "content": message_pour_modele})

    if image_url or images_base64:
        # Chemin dédié image(s) : voir docstring ci-dessus. Pas de cascade
        # multi-modeles ici, Gemini est le seul maillon capable de traiter
        # de la vision -- s'il echoue, il n'y a pas de second recours
        # multimodal. `images_base64` (2026-07-20) : frames extraites d'une
        # vidéo par _extraire_frames_video, voir la branche vidéo dédiée
        # dans api/uploads.py:uploader_video_chat -- même mécanique que
        # l'image simple, juste plusieurs inline_data au lieu d'un seul.
        images = []
        if image_url:
            try:
                images.append(_telecharger_image(image_url))
            except Exception as e:
                logging.error(f"ERREUR TELECHARGEMENT IMAGE ({image_url}): {e}")
                yield {"type": "reponse", "texte": "Désolé, je n'ai pas pu récupérer l'image envoyée. Réessaie."}
                return
        if images_base64:
            for image_b64 in images_base64:
                images.append((base64.b64decode(image_b64), "image/jpeg"))

        gemini_messages = [
            {"role": "user" if m["role"] != "assistant" else "model", "parts": [{"text": m["content"]}]}
            for m in messages_base[:-1] if m["role"] != "system"
        ]
        gemini_messages.append({
            "role": "user",
            "parts": _construire_parts_gemini(message_pour_modele, images),
        })

        reponse_accumulee = []
        try:
            client_google = genai.Client(api_key=get_secret("GOOGLE_API_KEY"))
            response = client_google.models.generate_content_stream(
                model=GOOGLE_MODEL,
                contents=gemini_messages,
                config=types.GenerateContentConfig(
                    system_instruction=system_final
                )
            )
            for chunk in response:
                if chunk.text:
                    reponse_accumulee.append(chunk.text)
                    yield {"type": "reponse", "texte": chunk.text}
            logging.info("Réponse via GEMINI (image)")
            ids_historique = _sauvegarder_echange(user_id, agent_id, message_utilisateur, "".join(reponse_accumulee), conversation_id, modele=GOOGLE_MODEL)
            if ids_historique:
                yield {"type": "meta", **ids_historique}
            _mettre_a_jour_resume_si_besoin(user_id)
            _mettre_a_jour_profil_utilisateur_si_besoin(user_id, agent_id)
        except Exception as e:
            logging.error(f"ERREUR GEMINI (image): {e}")
            if not reponse_accumulee:
                yield {"type": "reponse", "texte": MESSAGE_ERREUR}
        return

    if modele_force:
        # Modele premium (Claude/GPT/Gemini/DeepSeek), voir docstring de
        # chat() -- meme structure que le bloc image juste au-dessus :
        # pas d'outils MCP, pas de cascade de secours multi-modeles, un
        # seul appel, on retombe sur MESSAGE_ERREUR s'il echoue.
        messages_premium = [
            {"role": m["role"], "content": m["content"]}
            for m in messages_base if m["role"] != "system"
        ]
        reponse_accumulee = []
        try:
            for morceau in generer_reponse_premium(modele_force, system_final, messages_premium):
                reponse_accumulee.append(morceau)
                yield {"type": "reponse", "texte": morceau}
            logging.info(f"Réponse via MODELE PREMIUM : {modele_force}")
            ids_historique = _sauvegarder_echange(
                user_id, agent_id, message_utilisateur, "".join(reponse_accumulee), conversation_id, modele=modele_force
            )
            if ids_historique:
                yield {"type": "meta", **ids_historique}
            _mettre_a_jour_resume_si_besoin(user_id)
            _mettre_a_jour_profil_utilisateur_si_besoin(user_id, agent_id)
        except Exception as e:
            logging.error(f"ERREUR MODELE PREMIUM ({modele_force}) : {e}")
            if not reponse_accumulee:
                yield {"type": "reponse", "texte": MESSAGE_ERREUR}
        return

    client_groq = Groq(api_key=get_secret("GROQ_API_KEY"), max_retries=0)
    # Nom affiché de l'agent (ex. "Nucleos"), calculé UNE fois ici -- voir
    # _nom_agent, utilisé pour que la confirmation d'une action sensible
    # dise "Nucleos veut faire X" plutôt qu'une description générique.
    agent_nom = _nom_agent(agent_id)

    for _passage in range(MAX_PASSAGES_CASCADE):
        tout_est_timeout = True

        # Une SEULE liste de messages pour tout ce passage de la cascade
        # Groq (modele principal + fallbacks), au lieu d'en recreer une a
        # chaque modele. Raison : si un modele a deja appele un outil (ex:
        # notion-search) et obtenu un resultat AVANT d'echouer sur l'appel
        # Groq suivant (429/413 en essayant de rediger la reponse finale),
        # le resultat de cet outil est deja present dans messages_agent
        # (ajoute par _agent_groq/_traiter_appels). Si on repartait de
        # messages_base a chaque modele, ce resultat serait perdu et le
        # modele de secours suivant redemarrerait a zero, sans le contexte
        # deja recupere (cause du bug ou la page Notion trouvee n'arrivait
        # jamais dans la reponse finale).
        messages_agent = list(messages_base)
        reponse_accumulee = []

        # 1. GPT-OSS 120B, avec cycle d'outils MCP dynamique
        try:
            yield from _capturer_reponse(
                _agent_groq(client_groq, messages_agent, outils_mcp, table_routage, agent_nom=agent_nom),
                reponse_accumulee,
            )
            evenement_lien_manquant = _completer_liens_manquants(reponse_accumulee, messages_agent)
            if evenement_lien_manquant:
                yield evenement_lien_manquant
            ids_historique = _sauvegarder_echange(user_id, agent_id, message_utilisateur, "".join(reponse_accumulee), conversation_id, modele=GROQ_PRIMARY)
            if ids_historique:
                yield {"type": "meta", **ids_historique}
            _mettre_a_jour_resume_si_besoin(user_id)
            _mettre_a_jour_profil_utilisateur_si_besoin(user_id, agent_id)
            return
        except Exception as e:
            if not _est_timeout(e):
                tout_est_timeout = False
                logging.error(f"ERREUR GROQ {GROQ_PRIMARY}: {e}")

        # 2. Fallbacks Groq — AVEC les memes outils MCP (via _agent_groq),
        # pour que Notion/Wolfram restent utilisables meme quand
        # GROQ_PRIMARY sature son quota TPM (ce qui est le cas le plus
        # frequent de bascule ici, pas une vraie panne du modele).
        # reasoning_pour_ce_modele vient de MODELES_AVEC_REASONING_EFFORT.get(model) :
        # chaque modele recoit sa propre valeur valide ("none" pour Qwen 3,
        # "low" pour GPT-OSS -- jamais "none" pour ce dernier, invalide cote
        # API, voir la definition du dict plus haut), None (donc rien envoye)
        # pour les modeles non-raisonnement comme llama-3.3-70b-versatile et
        # llama-3.1-8b-instant.
        # IMPORTANT : on reutilise messages_agent tel quel (meme instance,
        # mutee en place par _agent_groq) d'un modele a l'autre — on ne le
        # reinitialise PAS a messages_base a chaque tour de boucle (voir
        # commentaire ci-dessus).
        for model in GROQ_FALLBACKS:
            try:
                reasoning_pour_ce_modele = MODELES_AVEC_REASONING_EFFORT.get(model)
                yield from _capturer_reponse(
                    _agent_groq(
                        client_groq, messages_agent, outils_mcp, table_routage,
                        modele=model, reasoning_effort=reasoning_pour_ce_modele, agent_nom=agent_nom,
                    ),
                    reponse_accumulee,
                )
                evenement_lien_manquant = _completer_liens_manquants(reponse_accumulee, messages_agent)
                if evenement_lien_manquant:
                    yield evenement_lien_manquant
                ids_historique = _sauvegarder_echange(user_id, agent_id, message_utilisateur, "".join(reponse_accumulee), conversation_id, modele=model)
                # Signale au frontend quand la reponse vient d'un modele de
                # qualite reduite (demande Bourama, 26/07) : evite que
                # l'utilisateur juge la plateforme sur une reponse plus
                # faible que la normale sans le savoir -- voir
                # MODELES_QUALITE_REDUITE plus haut et StatutOutil.tsx /
                # ChatIA.tsx cote frontend pour l'affichage.
                meta_a_envoyer = dict(ids_historique) if ids_historique else {}
                if model in MODELES_QUALITE_REDUITE:
                    meta_a_envoyer["modele_qualite_reduite"] = True
                if meta_a_envoyer:
                    yield {"type": "meta", **meta_a_envoyer}
                _mettre_a_jour_resume_si_besoin(user_id)
                _mettre_a_jour_profil_utilisateur_si_besoin(user_id, agent_id)
                return
            except Exception as e:
                if not _est_timeout(e):
                    tout_est_timeout = False
                    logging.error(f"ERREUR GROQ {model}: {e}")
                continue

        # 3. Gemini 2.5 Flash — tout dernier recours, sans outils MCP a lui,
        # mais REND COMPTE de ce qu'un outil Groq a deja execute avant lui
        # dans cette meme cascade (2026-07-31, corrige suite a un cas reel
        # observe par Bourama en logs : tavily_search execute avec succes,
        # 7000+ caracteres de resultat obtenus, mais tous les modeles Groq
        # tombent en rate limit sur l'appel de REDACTION finale -- Gemini
        # prend le relais et repond quand meme "je ne peux pas verifier en
        # direct", car il partait de messages_base (jamais mute), qui ne
        # contient jamais les resultats d'outils -- le resultat deja obtenu
        # etait donc silencieusement jete. Utilise messages_agent (comme la
        # cascade Groq juste au-dessus, meme logique/meme commentaire) : SI
        # un outil a deja tourne, son resultat y est present et Gemini doit
        # s'en servir ; SINON (aucun outil execute avant d'arriver ici),
        # Gemini doit dire que l'outil n'est pas disponible MAINTENANT
        # (indisponibilite technique temporaire) plutot que de se presenter
        # comme une IA incapable de faire des recherches par nature.
        try:
            client_google = genai.Client(api_key=get_secret("GOOGLE_API_KEY"))
            outil_deja_execute = any(m.get("role") == "tool" for m in messages_agent)
            gemini_messages = []
            for m in messages_agent:
                if m["role"] == "system":
                    continue
                if m["role"] == "tool":
                    # Pas de role "tool" natif dans ce format simplifie de
                    # contenus Gemini (contents/parts) -- integre comme
                    # contexte texte explicite, marque clairement pour que
                    # l'instruction ci-dessous puisse s'y referer sans
                    # ambiguite.
                    gemini_messages.append(
                        {"role": "user", "parts": [{"text": f"[Résultat de l'outil déjà exécuté] {m['content']}"}]}
                    )
                elif m["role"] == "assistant" and not m.get("content"):
                    # Message "assistant" qui ne fait QUE declarer un appel
                    # d'outil (content=None, voir messages_agent.append
                    # plus haut dans _agent_groq) -- rien a montrer a
                    # Gemini, le resultat juste apres (role "tool"
                    # ci-dessus) suffit.
                    continue
                else:
                    gemini_messages.append(
                        {"role": "user" if m["role"] != "assistant" else "model", "parts": [{"text": m["content"]}]}
                    )
            # Instruction ciblee (2026-07-24, trouve par Bourama en test
            # reel, PERDUE le 25/07 par le commit de57439 -- modification
            # concurrente partie d'une base sans ce fix, qui a ecrase la
            # ligne system_instruction=system_gemini_sans_outils par
            # system_instruction=system_final -- reappliquee le 25/07
            # apres reapparition confirmee du bug) -- la regle generale
            # anti-hallucination du prompt (voir INSTRUCTIONS_FORMATS_AFFICHAGE)
            # n'a PAS suffi ici : Gemini a quand meme invente un faux appel
            # d'outil (default_api.get_exchange_rate(...),
            # default_api.search_news(...), noms qui n'existent nulle part
            # dans le code reel -- "default_api" est un nom generique que
            # Gemini associe au function-calling dans ses propres exemples
            # d'entrainement). Ce chemin precis n'a REELEMENT aucun outil
            # branche (pas de parametre tools= sur cet appel), donc
            # l'instruction est directe et sans ambiguite plutot que de
            # compter sur la regle generale noyee dans un long prompt
            # systeme.
            if outil_deja_execute:
                system_gemini_sans_outils = (
                    system_final
                    + "\n\nIMPORTANT : un outil a DÉJÀ été exécuté plus tôt dans cet échange et son "
                    "résultat est présent ci-dessus, marqué \"[Résultat de l'outil déjà exécuté]\" -- "
                    "base ta réponse DESSUS. Ne dis JAMAIS que tu ne peux pas faire de recherche ou "
                    "vérifier l'information : le résultat est déjà là, utilise-le. Tu n'as par "
                    "contre AUCUN outil à appeler toi-même dans cette réponse précise (pas de "
                    "nouvel appel) -- n'écris jamais de code, de pseudo-code, ou de texte qui "
                    "ressemble à un appel d'outil/API."
                )
            else:
                system_gemini_sans_outils = (
                    system_final
                    + "\n\nIMPORTANT : tu n'as accès à AUCUN outil réel dans cette réponse précise "
                    "(pas de recherche web, pas d'API externe, pas de Notion, rien). Si la question "
                    "porte sur une information qui change (prix, taux de change, actualité, données "
                    "en temps réel...) et nécessiterait normalement un outil, dis clairement que cet "
                    "outil n'est PAS DISPONIBLE POUR L'INSTANT (indisponibilité technique "
                    "temporaire) plutôt que de deviner OU de te présenter comme une IA qui ne peut "
                    "pas faire de recherches par nature -- ce n'est pas une limite permanente, "
                    "juste indisponible sur ce message précis. N'écris JAMAIS de code, de "
                    "pseudo-code, ou de texte qui ressemble à un appel d'outil/API (même dans un "
                    "bloc de code) -- tu n'as aucun outil à appeler, l'écrire ne fait qu'inventer "
                    "un résultat qui n'existe pas."
                )
            response = client_google.models.generate_content_stream(
                model=GOOGLE_MODEL,
                contents=gemini_messages,
                config=types.GenerateContentConfig(
                    system_instruction=system_gemini_sans_outils
                )
            )
            for chunk in response:
                if chunk.text:
                    reponse_accumulee.append(chunk.text)
                    yield {"type": "reponse", "texte": chunk.text}
            logging.info("Réponse via GEMINI")
            ids_historique = _sauvegarder_echange(user_id, agent_id, message_utilisateur, "".join(reponse_accumulee), conversation_id, modele=GOOGLE_MODEL)
            if ids_historique:
                yield {"type": "meta", **ids_historique}
            _mettre_a_jour_resume_si_besoin(user_id)
            _mettre_a_jour_profil_utilisateur_si_besoin(user_id, agent_id)
            return
        except Exception as e:
            if not _est_timeout(e):
                tout_est_timeout = False
            logging.error(f"ERREUR GEMINI: {e}")

        if not tout_est_timeout:
            break  # au moins une vraie erreur (pas juste lent) : inutile de retenter

        logging.info("Toute la cascade a timeout, on retente un passage complet.")

    # Echec complet : on ne persiste jamais un message d'erreur technique
    # en memoire (polluerait le resume avec du bruit sans valeur).
    yield {"type": "reponse", "texte": MESSAGE_ERREUR}


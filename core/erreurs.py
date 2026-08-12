"""
Messages d'erreur centralisés, orientés utilisateur.

Chaque erreur API a un CODE stable (utilisé par le front pour la
traduction, voir djiguigne-frontend/lib/erreurs.ts) et un message français
par défaut (utilisé tel quel tant que le front ne connaît pas encore le
code, ou si une locale n'a pas encore de traduction).

Utilisation :
    from core.erreurs import erreur_api
    raise erreur_api(404, "AGENT_INTROUVABLE")
    raise erreur_api(500, "FICHIER_VECTORISATION_ECHEC", nom=nom_original)
"""
from fastapi import HTTPException

MESSAGES_FR: dict[str, str] = {
    "AGENT_INTROUVABLE": "Agent introuvable.",
    "AGENT_MODIFICATION_ERREUR_TECHNIQUE": "Impossible de modifier l'agent (erreur technique). Réessaie dans un instant.",
    "AGENT_MODIFICATION_INDISPONIBLE": "Impossible de modifier l'agent pour le moment.",
    "ARTICLE_SANS_PHOTOS_SUPP": "Un article n'a pas de photos supplémentaires.",
    "ARTICLE_SANS_TITRE": "Un article doit avoir un titre.",
    "AUCUNE_FORMULE_DETECTEE_DANS_CETTE_IMAGE": "Aucune formule détectée dans cette image.",
    "AUCUN_COMPTE_AVEC_CET_EMAIL": "Aucun compte Djiguignè n'utilise cet email.",
    "AUCUN_TEXTE_TROUVE_DOCUMENT_SCANNE_IMAGE": "Aucun texte trouvé (document scanné/image sans OCR ?).",
    "AUDIO_TROP_LONG_20_MO_MAX": "Audio trop long (20 Mo max).",
    "CATEGORIE_INCONNUE": "Catégorie inconnue.",
    "COMPTE_NON_CREATEUR": "Ce compte n'a pas (encore) le statut créateur.",
    "CETTE_MATIERE_EST_DEJA_PRISE_PAR": "Cette matière est déjà prise par une autre IA.",
    "CETTE_PUBLICATION_NE_T_APPARTIENT_PAS": "Cette publication ne t'appartient pas.",
    "CET_AGENT_NE_T_APPARTIENT_PAS": "Cet agent ne t'appartient pas.",
    "CE_DOCUMENT_N_APPARTIENT_PAS_A": "Ce document n'appartient pas à cet agent.",
    "DOCUMENT_TROP_LOURD_15_MO_MAX": "Document trop lourd (15 Mo max).",
    "DONNE_AU_MOINS_UNE_DESCRIPTION_OU": "Donne au moins une description ou un titre.",
    "ECHEC_DE_LA_GENERATION_AUDIO_REESSAIE": "Échec de la génération audio, réessaie.",
    "ECHEC_DE_LA_GENERATION_DU_DOCUMENT": "Échec de la génération du document, réessaie.",
    "ECHEC_DE_LA_LECTURE_DU_DOCUMENT": "Échec de la lecture du document.",
    "ECHEC_DE_LA_TRANSCRIPTION_REESSAIE": "Échec de la transcription, réessaie.",
    "ECHEC_DE_L_ENREGISTREMENT_DE_L": "Échec de l'enregistrement de l'abonnement.",
    "ECHEC_DE_L_ENVOI_POUR_SIGNATURE": "Échec de l'envoi pour signature, réessaie.",
    "ECHEC_DE_L_EXPORT_REESSAIE": "Échec de l'export, réessaie.",
    "ECHEC_DE_L_EXTRACTION_REESSAIE": "Échec de l'extraction, réessaie.",
    "ECHEC_DE_L_UPLOAD_REESSAIE": "Échec de l'upload, réessaie.",
    "ECHEC_DU_DESABONNEMENT": "Échec du désabonnement.",
    "ECHEC_GENERATION_ARCHIVE": "Échec de la génération de l'archive, réessaie.",
    "ECHEC_GENERATION_IMAGE": "Échec de la génération de l'image, réessaie.",
    "ECHEC_LANCEMENT_GENERATION_3D": "Échec du lancement de la génération 3D.",
    "ECHEC_LANCEMENT_GENERATION_VIDEO": "Échec du lancement de la génération vidéo.",
    "FICHIER_AUDIO_VIDE": "Fichier audio vide.",
    "FICHIER_TROP_LOURD_50_MO_MAX": "Fichier trop lourd (50 Mo max).",
    "FICHIER_VECTORISE_MAIS_ECHEC_DU_STOCKAGE": "Fichier vectorisé mais échec du stockage en bibliothèque.",
    "FICHIER_VIDE": "Fichier vide.",
    "FORMAT_NON_SUPPORTE_JPEG_PNG_OU": "Format non supporté (jpeg, png ou webp uniquement).",
    "FORMAT_NON_SUPPORTE_MP4_WEBM_OU": "Format non supporté (mp4, webm ou mov uniquement).",
    "FORMAT_NON_SUPPORTE_PDF_WORD_DOCX": "Format non supporté (PDF, Word .docx ou Excel .xlsx uniquement).",
    "GENERATION_3D_INDISPONIBLE": "La génération 3D n'est pas encore activée.",
    "GENERATION_AUDIO_INDISPONIBLE": "La génération audio n'est pas encore activée sur cette plateforme.",
    "GENERATION_VIDEO_INDISPONIBLE": "La génération vidéo n'est pas encore activée.",
    "HISTOIRE_SANS_COUVERTURE": "Une histoire doit avoir une photo de couverture.",
    "HISTOIRE_SANS_TITRE": "Une histoire doit avoir un titre.",
    "IMAGE_TROP_LOURDE_5_MO_MAX": "Image trop lourde (5 Mo max).",
    "IMPOSSIBLE_DE_CHARGER_CETTE_CONVERSATION": "Impossible de charger cette conversation.",
    "IMPOSSIBLE_DE_CHARGER_CET_AGENT_POUR": "Impossible de charger cet agent pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_CE_PROFIL_POUR": "Impossible de charger ce profil pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LA_MEMOIRE_POUR": "Impossible de charger la mémoire pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LA_NOTE_POUR": "Impossible de charger la note pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_ABONNES_POUR": "Impossible de charger les abonnés pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_ADMINISTRATEURS": "Impossible de charger les administrateurs pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_CATEGORIES_POUR": "Impossible de charger les catégories pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_COMMENTAIRES_POUR": "Impossible de charger les commentaires pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_CREATEURS_POUR": "Impossible de charger les créateurs pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_DROITS_POUR": "Impossible de charger les droits pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_MATIERES_POUR": "Impossible de charger les matières pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_MISES_A": "Impossible de charger les mises à jour pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_NOTIFICATIONS_POUR": "Impossible de charger les notifications pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_OUTILS_DISPONIBLES": "Impossible de charger les outils disponibles pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LES_PUBLICATIONS_POUR": "Impossible de charger les publications pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LE_FEED_POUR": "Impossible de charger le feed pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LE_PROFIL_POUR": "Impossible de charger le profil pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_LE_REGISTRE_POUR": "Impossible de charger le registre pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_L_AGENT_POUR": "Impossible de charger l'agent pour le moment.",
    "IMPOSSIBLE_DE_CHARGER_L_HISTORIQUE": "Impossible de charger l'historique.",
    "IMPOSSIBLE_DE_CREER_L_AGENT_ERREUR": "Impossible de créer l'agent (erreur technique). Réessaie dans un instant.",
    "IMPOSSIBLE_DE_LISTER_LA_BIBLIOTHEQUE_POUR": "Impossible de lister la bibliothèque pour le moment.",
    "IMPOSSIBLE_DE_LISTER_LES_DOCUMENTS_POUR": "Impossible de lister les documents pour le moment.",
    "IMPOSSIBLE_DE_MARQUER_CETTE_NOTIFICATION_COMME": "Impossible de marquer cette notification comme lue.",
    "IMPOSSIBLE_DE_MARQUER_LES_NOTIFICATIONS_COMME": "Impossible de marquer les notifications comme lues.",
    "IMPOSSIBLE_DE_METTRE_A_JOUR_LE": "Impossible de mettre à jour le profil pour le moment.",
    "IMPOSSIBLE_DE_MODIFIER_LES_DROITS_POUR": "Impossible de modifier les droits pour le moment.",
    "IMPOSSIBLE_DE_PUBLIER_LA_MISE_A": "Impossible de publier la mise à jour pour le moment.",
    "IMPOSSIBLE_DE_PUBLIER_POUR_LE_MOMENT": "Impossible de publier pour le moment.",
    "IMPOSSIBLE_DE_RECUPERER_LE_STATUT": "Impossible de récupérer le statut.",
    "IMPOSSIBLE_DE_RETIRER_CE_FOLLOW_POUR": "Impossible de retirer ce follow pour le moment.",
    "IMPOSSIBLE_DE_SE_SUIVRE_SOI_MEME": "Impossible de se suivre soi-même.",
    "IMPOSSIBLE_DE_SUIVRE_CE_CREATEUR_POUR": "Impossible de suivre ce créateur pour le moment.",
    "IMPOSSIBLE_DE_SUPPRIMER_CETTE_PUBLICATION_POUR": "Impossible de supprimer cette publication pour le moment.",
    "IMPOSSIBLE_DE_SUPPRIMER_CET_AGENT_POUR": "Impossible de supprimer cet agent pour le moment.",
    "IMPOSSIBLE_DE_SUPPRIMER_CE_DOCUMENT": "Impossible de supprimer ce document.",
    "IMPOSSIBLE_DE_SUPPRIMER_CE_DOCUMENT_POUR": "Impossible de supprimer ce document pour le moment.",
    "IMPOSSIBLE_DE_SUPPRIMER_CE_FICHIER_POUR": "Impossible de supprimer ce fichier pour le moment.",
    "IMPOSSIBLE_DE_VERIFIER_CET_AGENT_POUR": "Impossible de vérifier cet agent pour le moment.",
    "IMPOSSIBLE_D_AJOUTER_CE_DOCUMENT_POUR": "Impossible d'ajouter ce document pour le moment.",
    "IMPOSSIBLE_D_AJOUTER_CE_FICHIER_POUR": "Impossible d'ajouter ce fichier pour le moment.",
    "IMPOSSIBLE_D_AJOUTER_L_ADMINISTRATEUR": "Impossible d'ajouter cet administrateur pour le moment.",
    "IMPOSSIBLE_D_ANALYSER_CETTE_VIDEO_REESSAIE": "Impossible d'analyser cette vidéo, réessaie.",
    "IMPOSSIBLE_D_EFFACER_LA_MEMOIRE_POUR": "Impossible d'effacer la mémoire pour le moment.",
    "IMPOSSIBLE_D_EFFACER_LE_PROFIL_POUR": "Impossible d'effacer le profil pour le moment.",
    "IMPOSSIBLE_D_ENREGISTRER_LA_MEMOIRE_POUR": "Impossible d'enregistrer la mémoire pour le moment.",
    "IMPOSSIBLE_D_ENREGISTRER_LA_NOTE_POUR": "Impossible d'enregistrer la note pour le moment.",
    "IMPOSSIBLE_D_ENREGISTRER_LE_COMMENTAIRE_POUR": "Impossible d'enregistrer le commentaire pour le moment.",
    "IMPOSSIBLE_D_ENREGISTRER_LE_LIKE_POUR": "Impossible d'enregistrer le like pour le moment.",
    "IMPOSSIBLE_D_ENREGISTRER_LE_PROFIL_POUR": "Impossible d'enregistrer le profil pour le moment.",
    "IMPOSSIBLE_D_ENVOYER_CE_RETOUR_POUR": "Impossible d'envoyer ce retour pour le moment.",
    "LA_MISE_A_JOUR_N_A": "La mise à jour n'a pas pu être créée (erreur technique).",
    "LA_NOTE_DOIT_ETRE_COMPRISE_ENTRE": "La note doit être comprise entre 1 et 5.",
    "LA_PUBLICATION_N_A_PAS_PU": "La publication n'a pas pu être créée (erreur technique).",
    "LE_COMMENTAIRE_NE_PEUT_PAS_ETRE": "Le commentaire ne peut pas être vide.",
    "LE_COMMENTAIRE_N_A_PAS_PU": "Le commentaire n'a pas pu être créé (erreur technique).",
    "LE_CONTENU_A_ETE_SUPPRIME_MAIS": "Le contenu a été supprimé mais le compte lui-même n'a pas pu être fermé, réessaie.",
    "LE_CONTENU_NE_PEUT_PAS_ETRE": "Le contenu ne peut pas être vide.",
    "LE_DELAI_D_INACTIVITE_DOIT_ETRE": "Le délai d'inactivité doit être d'au moins 1 jour.",
    "LE_DELAI_MINIMUM_ENTRE_DEUX_RELANCES": "Le délai minimum entre deux relances doit être d'au moins 1 jour.",
    "LE_NOM_DE_L_AGENT_EST": "Le nom de l'agent est obligatoire.",
    "LE_TITRE_NE_PEUT_PAS_ETRE": "Le titre ne peut pas être vide.",
    "MATIERE_INCONNUE": "Matière inconnue.",
    "NOTIFICATIONS_PUSH_INDISPONIBLE": "Les notifications push ne sont pas encore activées.",
    "PROFIL_INTROUVABLE": "Profil introuvable.",
    "PROFIL_MIS_A_JOUR_MAIS_IMPOSSIBLE": "Profil mis à jour mais impossible de le relire pour confirmation.",
    "PUBLICATION_INTROUVABLE": "Publication introuvable.",
    "REFLEXION_SANS_PHOTO": "Une réflexion ne contient pas de photo.",
    "REMPLIS_AU_MOINS_LA_POSTURE_GENERALE": "Remplis au moins la posture générale ou les limites globales.",
    "RIEN_A_METTRE_A_JOUR_IMAGE": "Rien à mettre à jour (image_vitrine_url et description sont absents).",
    "RIEN_A_MODIFIER": "Rien à modifier.",
    "RIEN_N_A_ETE_COMPRIS_REESSAIE": "Rien n'a été compris, réessaie plus près du micro.",
    "SEULS_LES_FICHIERS_PDF_SONT_ACCEPTES": "Seuls les fichiers PDF sont acceptés.",
    "SIGNATURE_INDISPONIBLE": "La signature électronique n'est pas encore activée.",
    "TOKEN_INVALIDE": "Token invalide ou expiré",
    "TOKEN_MANQUANT": "Token d'authentification manquant",
    "TYPE_DE_FICHIER_NON_SUPPORTE": "Type de fichier non supporté.",
    "TYPE_DE_PUBLICATION_INVALIDE": "Type de publication invalide.",
    "VIDEO_ILLISIBLE_REESSAIE_AVEC_UN_AUTRE": "Vidéo illisible, réessaie avec un autre fichier.",
    "VIDEO_TROP_LOURDE_40_MO_MAX": "Vidéo trop lourde (40 Mo max).",
    "VITRINE_ERREUR_TECHNIQUE": "Impossible de mettre à jour la vitrine (erreur technique). Réessaie dans un instant.",
    "VITRINE_INDISPONIBLE": "Impossible de mettre à jour la vitrine pour le moment.",
    "SERVICE_INCONNU": "Service « {service} » inconnu.",
    "CONNEXION_INDISPONIBLE": "Connexion à {service} indisponible pour le moment.",
    "GITHUB_NON_CONNECTE": "Compte GitHub non connecté.",
    "GITHUB_DEPOTS_INDISPONIBLE": "Impossible de récupérer la liste des dépôts.",
    "RECHERCHE_INDISPONIBLE": "La recherche est indisponible pour le moment.",
    "SESSION_EXPIREE": "Ta session a expiré, reconnecte-toi.",
    "REQUETE_INVALIDE": "La requête envoyée est invalide.",
    "ERREUR_INCONNUE": "Une erreur est survenue, réessaie dans un instant.",
    # Contenu dynamique par matière (2026-08-06)
    "MATIERE_ET_SYSTEM_PROMPT_REQUIS": "La matière et le contenu sont obligatoires.",
    "CONTENU_MATIERE_INTROUVABLE": "Contenu introuvable.",
    "CODE_INVALIDE": "Ce code ne correspond à aucun contenu.",
    "DEJA_RATTACHE_A_CE_CONTENU": "Tu as déjà débloqué ce contenu.",
    "RATTACHEMENT_INTROUVABLE": "Rattachement introuvable.",
    "FICHIER_VECTORISATION_ECHEC": "« {nom} » n'a pas pu être vectorisé.",
    "AGENT_NOM_DEJA_PROCHE": "Un agent existe déjà avec un nom trop proche (id généré: {agent_id}). Choisis un nom légèrement différent.",
    "VITRINE_RIEN_A_METTRE_A_JOUR": "Rien à mettre à jour (image_vitrine_url et description sont absents).",
    "PHOTOS_SUPP_MAXIMUM": "Maximum {maximum} photos supplémentaires en plus de la couverture.",
    "VIDEO_TROP_LONGUE": "Vidéo trop longue ({duree}s, {maximum}s max).",
    "AGENT_CREATION_CHAMPS_MANQUANTS": "Remplis au moins la posture générale ou les limites globales.",
    "PROFIL_FERMETURE_PARTIELLE": "Le contenu a été supprimé mais le compte lui-même n'a pas pu être fermé, réessaie.",
    "PROFIL_RELECTURE_ECHEC": "Profil mis à jour mais impossible de le relire pour confirmation.",
    "AGENT_CREE_MAIS_INDEXATION_ECHEC": "L'agent est créé, mais « {nom} » n'a pas pu être indexé. Réessaie depuis « Mes agents ».",
    "PRECISE_MATIERE_AUTRE": "Précise la matière dans \"Autre\".",
    "PRECISE_LA_VALEUR_POUR": "Précise la valeur pour {libelle}.",
    "EST_DEJA_PRISE_PAR_UNE": "{libelle} est déjà prise par une autre IA.",
    "ROLE_DEJA_CHOISI": "Ton rôle a déjà été choisi, il ne peut plus être modifié ici.",
    "ROLE_INVALIDE": "Rôle invalide.",
    "ETABLISSEMENT_INTROUVABLE": "Établissement introuvable.",
    "ENSEIGNANT_INTROUVABLE": "Enseignant introuvable.",
    "ENSEIGNANT_ID_REQUIS_POUR_ETUDIANT": "Choisis ton enseignant pour continuer.",
    "ETABLISSEMENT_ID_REQUIS_POUR_ENSEIGNANT": "Choisis ton établissement pour continuer.",
    "ACTION_RESERVEE_A_CE_ROLE": "Cette action n'est pas disponible pour ton rôle.",
    # Invitations Clovis (2026-08-08, partie 4)
    "AUCUNE_INVITATION_ACTIVE": "Aucun code actif pour l'instant, génère-en un.",
    "CODE_MANQUANT": "Entre un code pour continuer.",
    "NOM_AFFICHE_MANQUANT": "Entre ton nom pour continuer.",
    "CODE_INVITATION_INVALIDE": "Ce code d'invitation n'est pas valide.",
    "PAS_LE_DROIT_SUR_CET_AGENT": "Tu n'as pas le droit de modifier cet agent.",
    "DESTINATAIRE_INTROUVABLE": "Destinataire introuvable.",
    "MESSAGE_VIDE": "Le message ne peut pas être vide.",
    "ANNONCE_VIDE": "L'annonce ne peut pas être vide.",
    "TEXTE_REQUIS": "Le texte ne peut pas être vide.",
    "COMPORTEMENT_INTROUVABLE": "Comportement introuvable.",
    "IMPOSSIBLE_DE_SUPPRIMER_CET_ELEMENT_DU": "Impossible de retirer cet élément du classement pour le moment.",
    # Plugins programme (2026-08-12, lot 3/5)
    # (PROGRAMME_INTROUVABLE et PAS_LE_DROIT_SUR_CE_PROGRAMME déjà définis par le lot 1/2 ci-dessus, réutilisés tels quels)
    "PLUGIN_INTROUVABLE": "Plugin introuvable.",
}


def erreur_api(status_code: int, code: str, message: str | None = None, **kwargs) -> HTTPException:
    """
    Construit une HTTPException avec un corps standard 
    {"detail": {"code": ..., "message": ...}}, exploitable par le front
    (djiguigne-frontend/lib/api.ts) pour la traduction et l'affichage.

    - `code` : identifiant stable (ex: "AGENT_INTROUVABLE"), utilisé par le
      front pour choisir la traduction dans la langue active.
    - `message` : surcharge ponctuelle du message par défaut (rare, garde
      la clé i18n sur `code` autant que possible).
    - `**kwargs` : valeurs pour les messages paramétrés (ex: nom, agent_id).
    """
    modele = message or MESSAGES_FR.get(code, MESSAGES_FR["ERREUR_INCONNUE"])
    try:
        texte = modele.format(**kwargs) if kwargs else modele
    except (KeyError, IndexError):
        texte = modele
    corps = {"code": code, "message": texte}
    if kwargs:
        # Valeurs brutes en plus du texte déjà interpolé en français : le
        # front (lib/erreurs.ts) s'en sert pour reconstruire le message dans
        # la langue active si ce n'est pas le français, sans quoi un message
        # paramétré traduit afficherait littéralement "{nom}" au lieu de la
        # vraie valeur.
        corps["params"] = kwargs
    return HTTPException(status_code=status_code, detail=corps)

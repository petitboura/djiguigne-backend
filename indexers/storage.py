"""
Utilitaires de gestion des documents dans le stockage Supabase
(bucket "documents-agents").
"""

import os
from supabase import create_client


def get_secret(key):
    return os.environ.get(key)


SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_SECRET = get_secret("SUPABASE_SECRET")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET)

BUCKET = "documents-agents"


def upload_document(file_path, file_name):
    # Corrigé le 09/08/2026 (Bourama : un PDF uploadé s'ouvrait comme du
    # texte brut -- "%PDF-1.5 ... obj ... endobj" -- au lieu de s'afficher
    # dans la visionneuse PDF du navigateur). Cause : sans file_options,
    # le client Python Supabase envoie Content-Type: text/plain par
    # défaut (confirmé dans la doc officielle storage-py), quel que soit
    # le contenu réel du fichier -- le PDF était bien stocké intact,
    # juste servi avec le mauvais en-tête. upsert=true nécessaire en plus
    # : réindexer un document déjà présent (même nom_stockage) provoquait
    # sinon une erreur "Asset Already Exists" au lieu de remplacer.
    with open(file_path, "rb") as f:
        supabase.storage.from_(BUCKET).upload(
            file_name, f, file_options={"content-type": "application/pdf", "upsert": "true"}
        )
    print(f"{file_name} uploadé avec succès")


def list_documents():
    files = supabase.storage.from_(BUCKET).list()
    return [f["name"] for f in files]


def delete_document(file_name):
    supabase.storage.from_(BUCKET).remove([file_name])
    print(f"{file_name} supprimé")


def get_document_url(file_name):
    return supabase.storage.from_(BUCKET).get_public_url(file_name)

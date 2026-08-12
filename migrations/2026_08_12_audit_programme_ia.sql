-- Audit IA hebdomadaire par matière (12/08, chantier "connexion IA <->
-- structure programme", discussion Bourama). Chaque lundi, l'IA relit
-- tout le contenu réel d'une matière (chapitres, limites, documents/PDF
-- extraits) et écrit un texte structuré (jamais à partir de son propre
-- texte de la semaine précédente -- toujours depuis la donnée source).
-- Ce texte est ensuite découpé + vectorisé pour être injecté
-- automatiquement en RAG pendant le chat (comme le RAG documents
-- existant, scopé par agent_id -- ici scopé par étudiant à la place).
--
-- Nouvelle section dédiée (PAS "Mes comportements" existant, qui reste
-- un texte libre écrit par l'étudiant lui-même).

create table if not exists audits_matiere (
  id uuid primary key default gen_random_uuid(),
  matiere_id uuid not null references matieres(id) on delete cascade,
  proprietaire_id uuid not null references auth.users(id) on delete cascade,  -- dénormalisé depuis programmes.proprietaire_id, pour filtrer sans jointure au moment du chat
  texte text not null,
  derniere_execution timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (matiere_id)  -- un seul audit par matière, RÉÉCRIT chaque lundi (upsert), jamais accumulé
);
create index if not exists idx_audits_matiere_proprietaire on audits_matiere(proprietaire_id);

create table if not exists audits_matiere_chunks (
  id uuid primary key default gen_random_uuid(),
  audit_id uuid not null references audits_matiere(id) on delete cascade,
  proprietaire_id uuid not null references auth.users(id) on delete cascade,  -- dénormalisé, filtre direct sans jointure dans la RPC
  contenu text not null,
  embedding vector(768) not null,  -- même dimension que public.documents (gemini-embedding-001, voir core/embeddings.py)
  created_at timestamptz not null default now()
);
create index if not exists idx_audits_chunks_proprietaire on audits_matiere_chunks(proprietaire_id);

create or replace function recherche_audits_programme(query_embedding vector, match_count integer, p_proprietaire_id uuid)
returns table(contenu text, similarite double precision)
language sql
as $$
  select
    contenu,
    1 - (embedding <=> query_embedding) as similarite
  from public.audits_matiere_chunks
  where proprietaire_id = p_proprietaire_id
  order by embedding <=> query_embedding
  limit match_count;
$$;

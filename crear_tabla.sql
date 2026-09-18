-- Ejecutar esto UNA sola vez en Supabase: Dashboard -> SQL Editor -> New query -> pegar y RUN.

create table if not exists public.tareas (
    id bigint generated always as identity primary key,
    titulo text not null,
    categoria text not null,
    responsable text,
    fecha date not null,
    hora time not null,
    estado text not null default 'Pendiente',
    avance integer not null default 0,
    notas text,
    alertado boolean not null default false,
    creado_en timestamptz not null default now()
);

-- Si vas a usar la app vos solo (sin login de usuarios), esto habilita el
-- acceso con la clave "anon" que pusiste en secrets.toml.
alter table public.tareas enable row level security;

create policy "Permitir todo con anon key"
on public.tareas
for all
using (true)
with check (true);

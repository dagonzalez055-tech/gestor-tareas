-- Ejecutar UNA sola vez en Supabase (Dashboard -> SQL Editor -> New query -> pegar y RUN)
-- sobre la tabla "tareas" que ya tenés creada. Agrega la columna que la
-- app necesita para no repetir la alerta de "faltan 15 minutos" una y
-- otra vez en cada actualización de la página.

alter table public.tareas
    add column if not exists alertado boolean not null default false;

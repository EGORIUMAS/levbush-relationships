-- Функции API (контракт — docs/api.md). Вызывают Edge Function (service_role), бот и `levbush web`.
-- Проверка доступа — снаружи; сами функции доступны только service_role и владельцу.

-- имя из одних невидимых символов (селекторы вариантов, заполнители хангыля, пробелы) → ник, как в render.visible
create or replace function person_name(p people) returns text
language sql immutable as $$
    select case when regexp_replace(n, '[[:space:]\u00AD\u034F\u115F\u1160\u180B-\u180F\u200B-\u200F'
                                     '\u202A-\u202E\u2060-\u206F\u2800\u3164\uFE00-\uFE0F\uFEFF\uFFA0]', '', 'g') <> ''
                then n else coalesce(p.username, 'id' || p.id::text) end
    from (select trim(coalesce(p.first_name, '') || ' ' || coalesce(p.last_name, '')) as n) x
$$;

create or replace function api_group() returns jsonb
language sql stable as $$
    select coalesce((select jsonb_build_object(
        'title', g.title, 'username', g.username, 'members', g.members, 'messages', g.messages,
        'first_date', g.first_date, 'map_updated_at', g.map_updated_at,
        'stats_updated_at', g.stats_updated_at, 'pass', g.pass, 'tz', g.tz)
      from group_info g where g.id = 1), '{}'::jsonb)
$$;

create or replace function api_graph() returns jsonb
language sql stable as $$
    select jsonb_build_object(
        'group', api_group(),
        'nodes', coalesce((
            select jsonb_agg(jsonb_build_object(
                'id', p.id, 'name', person_name(p), 'username', p.username, 'avatar', p.avatar,
                'is_member', p.is_member, 'msgs', coalesce((s.c ->> 'msgs')::int, 0),
                'summary', d.summary) order by p.id)
            from people p
            left join stats_total s on s.user_id = p.id
            left join dossiers d on d.user_id = p.id
            where not p.hidden and not p.is_bot), '[]'::jsonb),
        'links', coalesce((
            select jsonb_agg(jsonb_build_object(
                'a', r.a, 'b', r.b, 'strength', round(r.strength::numeric, 3), 'kind', r.kind,
                'tone', r.tone, 'summary', r.summary))
            from relations r
            join people pa on pa.id = r.a and not pa.hidden and not pa.is_bot
            join people pb on pb.id = r.b and not pb.hidden and not pb.is_bot
            where r.strength > 0), '[]'::jsonb))
$$;

-- Последние n периодов с нулями там, где активности не было
create or replace function api_periods(uid bigint, pt char, n int) returns jsonb
language sql stable as $$
    with g as (select coalesce((select tz from group_info where id = 1), 'Europe/Moscow') as tz),
    today as (select (now() at time zone g.tz)::date as d from g),
    starts as (
        select case pt
                 when 'd' then (select d from today) - i
                 when 'w' then date_trunc('week', (select d from today))::date - 7 * i
                 else (date_trunc('month', (select d from today)) - make_interval(months => i))::date
               end as s
        from generate_series(0, n - 1) i)
    select coalesce(jsonb_agg(jsonb_build_object('start', starts.s, 'c', coalesce(sp.c, '{}'::jsonb))
                              order by starts.s desc), '[]'::jsonb)
    from starts
    left join stats_period sp on sp.user_id = uid and sp.ptype = pt and sp.pstart = starts.s
$$;

create or replace function api_person(uid bigint) returns jsonb
language plpgsql stable as $$
declare
    p people;
    days numeric;
    tot stats_total;
    avg jsonb := '{}'::jsonb;
    k text;
    v jsonb;
begin
    select * into p from people where id = uid;
    if not found then
        return null;
    end if;
    select * into tot from stats_total where user_id = uid;
    days := greatest(p.time_in_group_sec / 86400.0, 1);
    if tot.c is not null then
        for k, v in select * from jsonb_each(tot.c) loop
            avg := avg || jsonb_build_object(k, round((v::text)::numeric / days, 2));
        end loop;
    end if;
    return jsonb_build_object(
        'person', jsonb_build_object(
            'id', p.id, 'name', person_name(p), 'first_name', p.first_name, 'last_name', p.last_name,
            'username', p.username, 'avatar', p.avatar, 'is_bot', p.is_bot, 'is_premium', p.is_premium,
            'is_member', p.is_member, 'kind', p.kind, 'first_join', p.first_join,
            'first_join_exact', p.first_join_exact, 'last_join', p.last_join, 'left_at', p.left_at,
            'time_in_group_sec', p.time_in_group_sec, 'first_msg_at', p.first_msg_at,
            'last_msg_at', p.last_msg_at),
        'total', coalesce(tot.extra, '{}'::jsonb) || jsonb_build_object('c', coalesce(tot.c, '{}'::jsonb)),
        'avg_per_day', avg,
        'days_in_group', round(days, 1),
        'periods', jsonb_build_object('d', api_periods(uid, 'd', 30), 'w', api_periods(uid, 'w', 12),
                                      'm', api_periods(uid, 'm', 12)),
        'dossier', (select jsonb_build_object('as_of', d.as_of, 'summary', d.summary, 'content', d.content,
                                              'updated_at', d.updated_at)
                    from dossiers d where d.user_id = uid and d.content is not null),
        'relations', coalesce((
            select jsonb_agg(jsonb_build_object(
                'id', o.id, 'name', person_name(o), 'avatar', o.avatar, 'strength', round(r.strength::numeric, 3),
                'kind', r.kind, 'tone', r.tone, 'summary', r.summary) order by r.strength desc)
            from relations r
            join people o on o.id = case when r.a = uid then r.b else r.a end
            where (r.a = uid or r.b = uid) and r.strength > 0 and not o.hidden), '[]'::jsonb),
        'pairs', coalesce((
            select jsonb_agg(jsonb_build_object('id', o.id, 'name', person_name(o),
                                                'out', coalesce(po.c, '{}'::jsonb), 'in', coalesce(pi.c, '{}'::jsonb))
                             order by x.total desc)
            from (select other, sum(n) as total from (
                      select dst as other, (select coalesce(sum((value)::int), 0) from jsonb_each_text(c)) as n
                      from pair_stats where src = uid
                      union all
                      select src, (select coalesce(sum((value)::int), 0) from jsonb_each_text(c))
                      from pair_stats where dst = uid) u
                  where other <> uid group by other) x
            join people o on o.id = x.other
            left join pair_stats po on po.src = uid and po.dst = x.other
            left join pair_stats pi on pi.src = x.other and pi.dst = uid), '[]'::jsonb)
    );
end
$$;

create or replace function api_relation(x bigint, y bigint) returns jsonb
language sql stable as $$
    select jsonb_build_object(
        'a', (select jsonb_build_object('id', id, 'name', person_name(p), 'avatar', avatar) from people p where id = x),
        'b', (select jsonb_build_object('id', id, 'name', person_name(p), 'avatar', avatar) from people p where id = y),
        'relation', (select jsonb_build_object(
                        'strength', round(strength::numeric, 3), 'quant', round(quant::numeric, 3),
                        'llm_score', round(llm_score::numeric, 3), 'co_episodes', co_episodes, 'kind', kind,
                        'tone', tone, 'summary', summary, 'description', description, 'updated_at', updated_at,
                        'as_of', as_of)
                     from relations where a = least(x, y) and b = greatest(x, y)),
        'ab', coalesce((select c from pair_stats where src = x and dst = y), '{}'::jsonb),
        'ba', coalesce((select c from pair_stats where src = y and dst = x), '{}'::jsonb))
$$;

-- Таблица лидеров для бота: metric — ключ счётчика, pt — 'd' | 'w' | 'm' | 'a' (всё время)
create or replace function api_top(metric text, pt char, lim int default 15) returns jsonb
language sql stable as $$
    with g as (select coalesce((select tz from group_info where id = 1), 'Europe/Moscow') as tz),
    today as (select (now() at time zone g.tz)::date as d from g),
    cur as (select case pt when 'd' then (select d from today)
                           when 'w' then date_trunc('week', (select d from today))::date
                           else date_trunc('month', (select d from today))::date end as s),
    src as (
        select user_id, coalesce((c ->> metric)::bigint, 0) as n from stats_total where pt = 'a'
        union all
        select user_id, coalesce((c ->> metric)::bigint, 0) from stats_period
        where pt <> 'a' and ptype = pt and pstart = (select s from cur))
    select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'name', person_name(p), 'username', p.username,
                                                 'n', src.n) order by src.n desc), '[]'::jsonb)
    from (select * from src where n > 0 order by n desc limit lim) src
    join people p on p.id = src.user_id
$$;

-- В Supabase закрываем функции от anon/authenticated; в локальном Postgres этих ролей может не быть.
do $$
begin
    revoke all on function person_name(people), api_group(), api_graph(), api_periods(bigint, char, int),
        api_person(bigint), api_relation(bigint, bigint),
        api_top(text, char, int) from public;
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function person_name(people), api_group(), api_graph(), api_periods(bigint, char, int),
        api_person(bigint), api_relation(bigint, bigint),
        api_top(text, char, int) from anon, authenticated;
        grant execute on function person_name(people), api_group(), api_graph(), api_periods(bigint, char, int),
        api_person(bigint), api_relation(bigint, bigint),
        api_top(text, char, int) to service_role;
    end if;
end
$$;

-- фиксированный search_path (советник Supabase: function_search_path_mutable)
alter function person_name(people) set search_path = public;
alter function api_group() set search_path = public;
alter function api_graph() set search_path = public;
alter function api_periods(bigint, char, int) set search_path = public;
alter function api_person(bigint) set search_path = public;
alter function api_relation(bigint, bigint) set search_path = public;
alter function api_top(text, char, int) set search_path = public;

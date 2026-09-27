-- Levbush Relationships — схема базы (Supabase на этапе 1, локальный Postgres на этапе 2).
-- Кэш переписки группы сюда НЕ попадает: он лежит локально (SQLite, см. levbush/cache.py).
-- Всё закрыто RLS без политик: читать можно только сервисной ролью (Edge Function) и владельцем БД (бот).

create table if not exists people (
    id                bigint primary key,
    kind              text not null default 'user',       -- user | channel
    first_name        text,
    last_name         text,
    username          text,
    is_bot            boolean not null default false,
    is_premium        boolean not null default false,
    avatar            text,                                -- data:image/jpeg;base64,… (маленькая)
    is_member         boolean not null default false,
    first_join        timestamptz,
    first_join_exact  boolean not null default true,       -- false — вход выведен из первого сообщения
    last_join         timestamptz,
    left_at           timestamptz,
    time_in_group_sec bigint not null default 0,
    first_msg_at      timestamptz,
    last_msg_at       timestamptz,
    hidden            boolean not null default false,      -- не показывать на карте (привязанный канал и т.п.)
    updated_at        timestamptz not null default now()
);
create index if not exists people_username on people (lower(username));

-- Счётчики за всё время; extra — часы, дни недели, сессии и прочие вычисляемые показатели
create table if not exists stats_total (
    user_id    bigint primary key references people on delete cascade,
    c          jsonb not null default '{}',
    extra      jsonb not null default '{}',
    updated_at timestamptz not null default now()
);

-- Последние периоды: 30 дней / 12 недель / 12 месяцев (лишнее удаляет бот при выгрузке)
create table if not exists stats_period (
    user_id bigint not null references people on delete cascade,
    ptype   char(1) not null check (ptype in ('d', 'w', 'm')),
    pstart  date not null,
    c       jsonb not null,
    primary key (user_id, ptype, pstart)
);

-- Кто кого: src → dst (replies, quotes, mentions, reactions, forwards)
create table if not exists pair_stats (
    src bigint not null references people on delete cascade,
    dst bigint not null references people on delete cascade,
    c   jsonb not null,
    primary key (src, dst)
);
create index if not exists pair_stats_dst on pair_stats (dst);

-- Связь (неориентированная, a < b): сила = количественная (quant) + оценка нейросети (llm_score)
create table if not exists relations (
    a           bigint not null references people on delete cascade,
    b           bigint not null references people on delete cascade,
    strength    real not null default 0,
    quant       real not null default 0,
    llm_score   real,
    co_episodes integer not null default 0,
    kind        text,
    tone        text,
    summary     text,
    description text,                           -- Markdown, собирается из data
    data        jsonb not null default '{}',    -- заметки и история связи (levbush/dossier.py)
    as_of       timestamptz,
    updated_at  timestamptz not null default now(),
    primary key (a, b),
    check (a < b)
);
create index if not exists relations_b on relations (b);

-- Досье: записи с датами появления/изменения (data), content — Markdown для сайта и бота. Версий нет.
create table if not exists dossiers (
    user_id    bigint primary key references people on delete cascade,
    as_of      timestamptz not null,
    summary    text,
    content    text,
    data       jsonb not null default '{}',
    updated_at timestamptz not null default now()
);

-- Разбор эпизодов переписки нейросетью (сырьё для досье и связей)
create table if not exists episodes (
    id           bigint primary key,        -- id первого сообщения
    last_msg_id  bigint not null,
    started_at   timestamptz not null,
    ended_at     timestamptz not null,
    n_msgs       integer not null,
    participants bigint[] not null,
    summary      text,
    topics       text[],
    mood         text,
    result       jsonb not null,
    analyzed_at  timestamptz not null default now()
);
create index if not exists episodes_ended on episodes (ended_at);
create index if not exists episodes_participants on episodes using gin (participants);

create table if not exists group_info (
    id               integer primary key default 1 check (id = 1),
    chat_id          bigint,
    title            text,
    username         text,
    channel_id       bigint,
    channel_title    text,
    members          integer,
    messages         bigint,
    first_date       timestamptz,
    tz               text not null default 'Europe/Moscow',   -- для дней/недель/месяцев
    stats_updated_at timestamptz,
    map_updated_at   timestamptz,
    pass             jsonb not null default '{"state": "idle"}',
    updated_at       timestamptz not null default now()
);

create table if not exists group_daily (
    day    date primary key,
    msgs   integer not null,
    active integer not null
);

-- Производные от токена бота ключи для проверки подписи входа (пишет бот; читает только Edge Function)
create table if not exists app_secrets (
    name  text primary key,
    value text not null
);

alter table people            enable row level security;
alter table stats_total       enable row level security;
alter table stats_period      enable row level security;
alter table pair_stats        enable row level security;
alter table relations         enable row level security;
alter table dossiers          enable row level security;
alter table episodes          enable row level security;
alter table group_info        enable row level security;
alter table group_daily       enable row level security;
alter table app_secrets       enable row level security;

/* Демо-данные для ?demo=1: выдуманная группа, без обращения к API.
 * Формат ответов — как в docs/api.md. */
(() => {
  'use strict';

  function rng(seed) {
    let a = seed >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const R = rng(20260927);
  const pick = (arr, r = R) => arr[Math.floor(r() * arr.length)];
  const int = (lo, hi, r = R) => Math.floor(lo + r() * (hi - lo + 1));
  const round2 = (x) => Math.round(x * 100) / 100;

  const NOW = Date.now();
  const DAY = 86400000;
  const iso = (ms) => new Date(ms).toISOString();
  const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

  const PEOPLE = [
    ['Лев', 'Бушуев', 'levbush', 'm'], ['Мария', 'Иванова', 'masha_iv', 'f'], ['Дмитрий', 'Кузнецов', 'dimakuz', 'm'],
    ['Анна', 'Попова', null, 'f'], ['Сергей', 'Соколов', 'sokol_s', 'm'], ['Екатерина', 'Лебедева', 'katya_leb', 'f'],
    ['Иван', 'Козлов', 'ivan_kozlov', 'm'], ['Ольга', 'Новикова', 'olganov', 'f'], ['Никита', 'Морозов', 'nikmoroz', 'm'],
    ['Татьяна', 'Петрова', null, 'f'], ['Михаил', 'Волков', 'volkov_m', 'm'], ['Юлия', 'Соловьёва', 'yulia_sol', 'f'],
    ['Артём', 'Васильев', 'artvas', 'm'], ['Наталья', 'Зайцева', 'nzaytseva', 'f'], ['Павел', 'Павлов', 'pavel2', 'm'],
    ['Елена', 'Семёнова', 'lena_sem', 'f'], ['Андрей', 'Голубев', null, 'm'], ['Дарья', 'Виноградова', 'dasha_vino', 'f'],
    ['Кирилл', 'Богданов', 'kirbog', 'm'], ['Алина', 'Воробьёва', 'alinavor', 'f'], ['Егор', 'Фёдоров', 'egorf', 'm'],
    ['Полина', 'Михайлова', 'polymih', 'f'], ['Роман', 'Беляев', 'rombel', 'm'], ['Ксения', 'Тарасова', 'ksu_tar', 'f'],
    ['Глеб', 'Орлов', null, 'm'],
  ];

  const INTERESTS = ['настолки', 'походы', 'фотография', 'бег', 'аниме', 'программирование', 'кулинария', 'кино', 'музыка',
    'велосипед', 'психология', 'книги', 'путешествия', 'игры', 'йога', 'история', 'мемы', 'котики', 'кофе', 'дизайн'];
  const TRAITS = ['остроумный', 'заботливый', 'спорщик', 'душа компании', 'тихий наблюдатель', 'организатор встреч',
    'генератор мемов', 'эрудит', 'миротворец', 'критик', 'полуночник', 'ранняя пташка'];
  const KINDS = ['дружба', 'приятели', 'рабочие', 'флирт', 'соперничество', 'наставничество', 'близкие друзья', 'знакомые', 'конфликт'];
  const TONES = ['тёплый', 'дружеский', 'нейтральный', 'ироничный', 'напряжённый', 'игривый', 'уважительный', 'колкий'];

  function avatarSvg(i, name) {
    const h1 = (i * 47) % 360, h2 = (h1 + 50) % 360;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">` +
      `<stop offset="0" stop-color="hsl(${h1},65%,62%)"/><stop offset="1" stop-color="hsl(${h2},60%,42%)"/></linearGradient></defs>` +
      `<rect width="64" height="64" fill="url(#g)"/><circle cx="32" cy="26" r="11" fill="rgba(255,255,255,0.85)"/>` +
      `<path d="M12 60c2-12 10-18 20-18s18 6 20 18z" fill="rgba(255,255,255,0.85)"/></svg>`;
    return 'data:image/svg+xml;base64,' + btoa(svg);
  }

  // ---- люди
  const clusters = [0, 0, 1, 2, 0, 1, 2, 1, 0, 2, 1, 0, 2, 1, 0, 2, 1, 0, 2, 1, 0, 2, 1, 0, 2];
  const people = PEOPLE.map(([first, last, username, g], i) => {
    const id = 1000 + i * 37;
    const msgs = Math.round(40 + 18000 * Math.pow(R(), 2.6));
    const joinedDaysAgo = int(60, 900);
    const isMember = ![7, 16, 24].includes(i);
    const leftDaysAgo = isMember ? null : int(5, Math.min(120, joinedDaysAgo - 10));
    return {
      i, id, first, last, name: `${first} ${last}`, username, g, cluster: clusters[i], msgs,
      avatar: i % 3 === 0 ? avatarSvg(i) : null,
      is_member: isMember, is_premium: R() < 0.3, is_bot: false,
      joined: NOW - joinedDaysAgo * DAY, left: leftDaysAgo != null ? NOW - leftDaysAgo * DAY : null,
      interests: [pick(INTERESTS), pick(INTERESTS), pick(INTERESTS)].filter((v, k, a) => a.indexOf(v) === k),
      trait: pick(TRAITS),
      peak: int(0, 23) < 6 ? int(0, 2) : int(18, 23),
    };
  });
  const byId = new Map(people.map((p) => [p.id, p]));
  const ended = (p, f, m) => (p.g === 'f' ? f : m);

  // ---- связи
  const links = [];
  const linkSet = new Set();
  const key = (a, b) => (a < b ? `${a}-${b}` : `${b}-${a}`);
  let guard = 0;
  while (links.length < 80 && guard++ < 5000) {
    const A = pick(people), B = pick(people);
    if (A === B) continue;
    const k = key(A.id, B.id);
    if (linkSet.has(k)) continue;
    const same = A.cluster === B.cluster;
    const act = Math.log1p(A.msgs) * Math.log1p(B.msgs) / 90;
    if (R() > (same ? 0.75 : 0.22) * Math.min(1, act + 0.2)) continue;
    linkSet.add(k);
    const base = same ? 0.35 + 0.65 * R() : 0.05 + 0.5 * R();
    const strength = round2(Math.min(1, Math.pow(base, 1.3)));
    const described = R() > 0.08;
    const kind = strength > 0.75 ? pick(['близкие друзья', 'дружба', 'флирт', 'наставничество']) : strength > 0.45 ? pick(['дружба', 'приятели', 'рабочие', 'флирт', 'соперничество', 'наставничество']) : pick(['знакомые', 'приятели', 'рабочие', 'соперничество', 'конфликт']);
    const tone = kind === 'конфликт' || kind === 'соперничество' ? pick(['напряжённый', 'колкий', 'ироничный']) : pick(TONES.filter((t) => t !== 'напряжённый'));
    const [a, b] = A.id < B.id ? [A, B] : [B, A];
    const topic = pick([...new Set([...a.interests, ...b.interests])]);
    const summaries = {
      'близкие друзья': `Давно и близко общаются, постоянно поддерживают друг друга; общая тема — ${topic}.`,
      'дружба': `Дружат: переписываются почти каждый день, чаще всего про ${topic}.`,
      'флирт': `Заметный взаимный интерес: комплименты, шутки, ночные переписки.`,
      'наставничество': `${a.first} часто даёт советы, ${b.first} прислушивается и благодарит.`,
      'приятели': `Приятельские отношения, пересекаются в общих обсуждениях про ${topic}.`,
      'рабочие': `Общаются по делу: договариваются о встречах и помогают с задачами.`,
      'соперничество': `Регулярно спорят и соревнуются в остроумии, но без злобы.`,
      'знакомые': `Знакомы, изредка отвечают друг другу в общих ветках.`,
      'конфликт': `Было несколько резких споров; сейчас общаются сдержанно.`,
    };
    links.push({ a: a.id, b: b.id, strength, kind: described ? kind : null, tone: described ? tone : null, summary: described ? summaries[kind] : null, topic, described });
  }
  const linkByKey = new Map(links.map((l) => [key(l.a, l.b), l]));

  const totalMsgs = people.reduce((s, p) => s + p.msgs, 0);

  // ---- статистика человека
  function counters(r, msgs) {
    const media = Math.round(msgs * (0.05 + 0.15 * r()));
    const c = {
      msgs,
      replies: Math.round(msgs * (0.2 + 0.3 * r())),
      quotes: Math.round(msgs * 0.04 * r()),
      forwards: Math.round(msgs * 0.03 * r()),
      mentions: Math.round(msgs * 0.05 * r()),
      reactions: Math.round(msgs * (0.1 + 0.6 * r())),
      media,
      photos: Math.round(media * 0.5),
      videos: Math.round(media * 0.12),
      documents: Math.round(media * 0.05 * r()),
      audios: r() < 0.3 ? Math.round(media * 0.02) : 0,
      gifs: Math.round(media * 0.18),
      video_notes: r() < 0.5 ? Math.round(msgs * 0.01 * r()) : 0,
      voices: r() < 0.6 ? Math.round(msgs * 0.03 * r()) : 0,
      stickers: Math.round(msgs * 0.08 * r()),
      polls: r() < 0.2 ? Math.round(msgs * 0.002) : 0,
      links: Math.round(msgs * 0.04 * r()),
      words: Math.round(msgs * (4 + 8 * r())),
      edits: Math.round(msgs * 0.05 * r()),
      replies_recv: Math.round(msgs * (0.15 + 0.3 * r())),
      quotes_recv: Math.round(msgs * 0.03 * r()),
      mentions_recv: Math.round(msgs * 0.05 * r()),
      reactions_recv: Math.round(msgs * (0.2 + 0.8 * r())),
    };
    c.chars = Math.round(c.words * (5.5 + r()));
    c.voice_sec = c.voices * int(8, 40, r);
    c.video_note_sec = c.video_notes * int(10, 45, r);
    for (const k of Object.keys(c)) if (!c[k]) delete c[k];
    return c;
  }

  function scaleCounters(c, f, r) {
    const o = {};
    for (const [k, v] of Object.entries(c)) {
      const x = Math.round(v * f * (0.5 + r()));
      if (x) o[k] = x;
    }
    return o;
  }

  function dossierMd(p, version) {
    const rels = links.filter((l) => l.a === p.id || l.b === p.id).sort((x, y) => y.strength - x.strength).slice(0, 3);
    const other = (l) => byId.get(l.a === p.id ? l.b : l.a);
    const shy = ended(p, 'а', '');
    let s = `## Кто это\n\n**${p.name}** — ${p.trait}. В группе с ${new Date(p.joined).toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' })}; ` +
      `пишет${p.peak < 6 ? ' в основном глубокой ночью' : ' чаще всего вечером'}.\n\n` +
      `## Интересы\n\n${p.interests.map((x) => `- ${x}`).join('\n')}\n\n` +
      `## Манера общения\n\nОтвечает быстро, любит длинные сообщения с подробностями. Часто ставит реакции вместо ответа; ` +
      `в спорах ${version > 1 ? 'стал' + shy + ' заметно мягче, чем раньше' : 'бывает резк' + ended(p, 'ой', 'им')}.\n\n` +
      `> «Давайте сначала разберёмся, а потом уже спорить» — типичная фраза.\n\n`;
    if (rels.length) {
      s += `## Ближний круг\n\n${rels.map((l) => `- **${other(l).name}** — ${l.kind || 'связь без описания'} (сила ${l.strength.toFixed(2)})`).join('\n')}\n\n`;
    }
    if (version > 1) s += `## Что нового\n\nЗа последний месяц стал${shy} активнее в обсуждениях про *${p.interests[0]}*, организовал${shy} встречу в выходные.\n`;
    if (version > 2) s += `\n| Период | Сообщений |\n|---|---|\n| весна | ${Math.round(p.msgs * 0.2)} |\n| лето | ${Math.round(p.msgs * 0.35)} |\n`;
    return s;
  }

  function personResponse(id) {
    const p = byId.get(Number(id));
    if (!p) return null;
    const r = rng(p.id * 7919);
    const c = counters(r, p.msgs);
    const leftOrNow = p.left || NOW;
    const days = Math.max(1, (leftOrNow - p.joined) / DAY);
    const avg = {};
    for (const [k, v] of Object.entries(c)) avg[k] = Math.round((v / days) * 10) / 10;

    const hours = Array.from({ length: 24 }, (_, h) => {
      const d = Math.min(Math.abs(h - p.peak), 24 - Math.abs(h - p.peak));
      return Math.round((p.msgs / 60) * Math.exp(-(d * d) / 18) * (0.6 + 0.8 * r()));
    });
    const weekdays = Array.from({ length: 7 }, (_, i) => Math.round((p.msgs / 7) * (i >= 5 ? 1.2 : 0.9) * (0.7 + 0.6 * r())));
    const today = new Date(NOW); today.setHours(0, 0, 0, 0);
    const d = Array.from({ length: 30 }, (_, i) => { const x = new Date(today); x.setDate(x.getDate() - i); return { start: ymd(x), c: scaleCounters(c, 1 / days, r) }; });
    const monday = new Date(today); monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
    const w = Array.from({ length: 12 }, (_, i) => { const x = new Date(monday); x.setDate(x.getDate() - 7 * i); return { start: ymd(x), c: scaleCounters(c, 7 / days, r) }; });
    const m = Array.from({ length: 12 }, (_, i) => { const x = new Date(today.getFullYear(), today.getMonth() - i, 1); return { start: ymd(x), c: scaleCounters(c, 30 / days, r) }; });
    if (p.left) {
      const cut = (arr) => arr.forEach((e) => { if (new Date(e.start).getTime() > p.left) e.c = {}; });
      cut(d); cut(w); cut(m);
    }

    const hasDossier = p.i % 5 !== 4;
    const dossier = hasDossier ? {
      as_of: iso(NOW - (p.i % 7) * DAY),
      summary: `${p.trait[0].toUpperCase() + p.trait.slice(1)}, увлекается: ${p.interests.join(', ')}.`,
      content: dossierMd(p, 3),
      updated_at: iso(NOW - (p.i % 7) * DAY),
    } : null;

    const mine = links.filter((l) => l.a === p.id || l.b === p.id);
    const relations = mine.filter((l) => l.described).sort((x, y) => y.strength - x.strength).map((l) => {
      const o = byId.get(l.a === p.id ? l.b : l.a);
      return { id: o.id, name: o.name, avatar: o.avatar, strength: l.strength, kind: l.kind, tone: l.tone, summary: l.summary };
    });
    const pairs = mine.map((l) => {
      const o = byId.get(l.a === p.id ? l.b : l.a);
      return { id: o.id, name: o.name, out: pairCounts(p.id, o.id), in: pairCounts(o.id, p.id) };
    });
    // пара человек, с кем нет связи на карте, но есть пара реакций
    for (const o of people) {
      if (pairs.length >= mine.length + 2) break;
      if (o.id !== p.id && !linkByKey.has(key(o.id, p.id))) pairs.push({ id: o.id, name: o.name, out: { reactions: int(0, 3, r) }, in: { replies: int(0, 2, r), reactions: int(0, 4, r) } });
    }
    const tot = (x) => Object.values(x.out).reduce((s, v) => s + v, 0) + Object.values(x.in).reduce((s, v) => s + v, 0);
    pairs.sort((x, y) => tot(y) - tot(x));

    return {
      person: {
        id: p.id, name: p.name, first_name: p.first, last_name: p.last, username: p.username, avatar: p.avatar,
        is_bot: p.is_bot, is_premium: p.is_premium, is_member: p.is_member,
        first_join: iso(p.joined), last_join: p.i === 5 ? iso(p.joined + 40 * DAY) : iso(p.joined), left_at: p.left ? iso(p.left) : null,
        time_in_group_sec: Math.round((leftOrNow - p.joined) / 1000),
        first_msg_at: iso(p.joined + 3600000 * int(1, 48, r)), last_msg_at: iso(leftOrNow - 60000 * int(3, 3000, r)),
      },
      total: {
        c, hours, weekdays,
        mean_hour: Math.round((p.peak - 1.3 + 24) % 24 * 10) / 10, peak_hour: p.peak,
        median_reply_sec: int(20, 900, r), avg_session_min: Math.round((4 + 30 * r()) * 10) / 10,
        sessions: Math.round(p.msgs / 12), active_days: Math.min(Math.round(days), Math.round(p.msgs / 25) + 3),
        longest_streak: int(2, 40, r), active_min_per_day: Math.round((2 + 40 * r()) * 10) / 10,
        conversations_started: Math.round(p.msgs / 60),
        top_reactions: [['❤', int(50, 400, r)], ['😂', int(30, 300, r)], ['🔥', int(10, 150, r)], ['👍', int(5, 120, r)], ['🤔', int(1, 40, r)]].sort((x, y) => y[1] - x[1]),
      },
      avg_per_day: avg,
      days_in_group: Math.round(days * 10) / 10,
      periods: { d, w, m },
      dossier,
      relations,
      pairs,
    };
  }

  function pairCounts(from, to) {
    const l = linkByKey.get(key(from, to));
    const r = rng(from * 31 + to * 17);
    const s = l ? l.strength : 0.02;
    const o = {
      replies: Math.round(120 * s * r()), quotes: Math.round(15 * s * r()), mentions: Math.round(30 * s * r()),
      reactions: Math.round(260 * s * r()), forwards: Math.round(5 * s * r()),
    };
    return o;
  }

  function relationDescription(A, B, l, version) {
    const since = new Date(Math.max(A.joined, B.joined) + 20 * DAY).toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' });
    let s = `## Коротко\n\n${l.summary}\n\n## История\n\n` +
      `- **${since}** — первые ответы друг другу в обсуждении про *${l.topic}*.\n` +
      `- Через пару месяцев ${A.first} и ${B.first} начали регулярно отмечать друг друга в сообщениях.\n` +
      (version > 1 ? `- Недавно — ${l.kind === 'конфликт' ? 'крупный спор из-за мелочи, после которого общение стало сдержанным' : 'совместная встреча, после которой общение стало заметно теплее'}.\n` : '') +
      `\n## Как общаются\n\n${A.first} чаще начинает разговор, ${B.first} чаще отвечает реакциями. ` +
      `Тон — **${l.tone}**; ${l.strength > 0.6 ? 'много личных шуток и отсылок, понятных только им двоим' : 'в основном обмен репликами в общих ветках'}.\n`;
    if (version > 2) s += `\n> ${B.first}: «Без тебя тут было бы скучно».\n`;
    return s;
  }

  function relationResponse(a, b) {
    const A = byId.get(Number(a)), B = byId.get(Number(b));
    if (!A || !B) return null;
    const l = linkByKey.get(key(A.id, B.id));
    const described = !!(l && l.described);
    return {
      a: { id: A.id, name: A.name, avatar: A.avatar },
      b: { id: B.id, name: B.name, avatar: B.avatar },
      relation: described ? {
        strength: l.strength, quant: round2(Math.min(1, l.strength * 0.9 + 0.05)), llm_score: round2(Math.min(1, l.strength * 1.05)),
        kind: l.kind, tone: l.tone, summary: l.summary, description: relationDescription(A, B, l, 3),
        updated_at: iso(NOW - ((A.id + B.id) % 9) * DAY), as_of: iso(NOW - ((A.id + B.id) % 9) * DAY),
      } : null,
      ab: pairCounts(A.id, B.id),
      ba: pairCounts(B.id, A.id),
    };
  }

  const graph = {
    group: {
      title: 'Левбуш и друзья', username: 'levbush_demo', members: people.filter((p) => p.is_member).length, messages: totalMsgs,
      first_date: iso(NOW - 900 * DAY), map_updated_at: iso(NOW - 2.4 * 3600000), stats_updated_at: iso(NOW - 25 * 60000),
      pass: { state: 'running', done: 57, total: 80, stage: 'шаг 58/80: переписка за 2026-08-14' },
    },
    me: people[0].id,
    nodes: people.map((p) => ({ id: p.id, name: p.name, username: p.username, avatar: p.avatar, is_member: p.is_member, msgs: p.msgs, summary: `${p.trait}; ${p.interests.join(', ')}` })),
    links: links.map((l) => ({ a: l.a, b: l.b, strength: l.strength, kind: l.kind, tone: l.tone, summary: l.summary })),
  };

  function handle(q, params) {
    switch (q) {
      case 'graph': return graph;
      case 'person': return personResponse(params.id);
      case 'relation': return relationResponse(params.a, params.b);
      default: return null;
    }
  }

  window.LevbushDemo = {
    api(q, params = {}) {
      return new Promise((resolve, reject) => {
        setTimeout(() => {
          const res = handle(q, params);
          // глубокая копия, чтобы клиент не мутировал «серверные» данные
          if (res) resolve(JSON.parse(JSON.stringify(res)));
          else reject(Object.assign(new Error('Не найдено'), { status: 404, code: 'not_found' }));
        }, 150 + Math.random() * 250);
      });
    },
  };
})();

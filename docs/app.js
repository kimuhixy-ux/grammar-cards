/* ============================================================
   GrammarCards - Leitner式間隔反復フラッシュカード
   ============================================================ */

const STORAGE_PROGRESS = 'grammarcards_progress_v1';
const STORAGE_SETTINGS = 'grammarcards_settings_v1';
const STORAGE_NEWCOUNT = 'grammarcards_newcount_v1';

// 5箱: 1日/3日/7日/14日/30日（box 1〜5 に対応。box 0 = 未着手）
const BOX_INTERVALS = [1, 3, 7, 14, 30];
const MASTERED_BOX = 5;

const TYPE_LABELS = {
  fill_blank: '空所補充',
  multiple_choice: '4択',
  reorder: '整序英作文',
  translate_ja_to_en: '和文英訳',
};

const BOOK_LABELS = { forest: 'Forest', chigasaki: '茅ヶ崎方式' };

const DEFAULT_SETTINGS = {
  sessionSize: 30,
  newPerDay: 15,
  bookFilter: 'all',
  categoryFilter: '',
};

let allCards = [];
let cardsById = {};
let settings = loadSettings();

let sessionQueue = [];
let sessionIndex = 0;
let sessionStats = { correct: 0, total: 0 };
let sessionNewIds = new Set(); // このセッション開始時点で未着手だったカードID
let currentAutoResult = null; // 現在のカードの自動判定結果（true/false/null）

/* ---------------- ユーティリティ ---------------- */

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function addDays(dateStr, n) {
  const d = new Date(dateStr + 'T00:00:00');
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function normalizeAnswerText(s) {
  return (s || '')
    .trim()
    .toLowerCase()
    .replace(/[.。!！?？、,]+$/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function normalizeCompact(s) {
  return (s || '').toLowerCase().replace(/\s+/g, '');
}

/* ---------------- localStorage ---------------- */

function loadProgress() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_PROGRESS)) || {};
  } catch (e) {
    console.warn('progress読み込み失敗', e);
    return {};
  }
}

function saveProgress(progress) {
  localStorage.setItem(STORAGE_PROGRESS, JSON.stringify(progress));
}

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_SETTINGS));
    return { ...DEFAULT_SETTINGS, ...(saved || {}) };
  } catch (e) {
    return { ...DEFAULT_SETTINGS };
  }
}

function saveSettings() {
  localStorage.setItem(STORAGE_SETTINGS, JSON.stringify(settings));
}

function loadNewCounter() {
  try {
    const c = JSON.parse(localStorage.getItem(STORAGE_NEWCOUNT));
    if (c && c.date === today()) return c;
  } catch (e) { /* fallthrough */ }
  return { date: today(), count: 0 };
}

function saveNewCounter(counter) {
  localStorage.setItem(STORAGE_NEWCOUNT, JSON.stringify(counter));
}

/* ---------------- カードデータ ---------------- */

async function loadCards() {
  const res = await fetch('data/cards.json');
  allCards = await res.json();
  cardsById = {};
  for (const c of allCards) cardsById[c.id] = c;
}

function cardStatus(card, progress) {
  const entry = progress[card.id];
  if (!entry || entry.box <= 0) return 'new';
  return entry.box >= MASTERED_BOX ? 'mastered' : 'learning';
}

function filterCards(cards) {
  const cat = settings.categoryFilter.trim().toLowerCase();
  return cards.filter((c) => {
    if (settings.bookFilter !== 'all' && c.book !== settings.bookFilter) return false;
    if (cat && !c.grammar_category.toLowerCase().includes(cat)) return false;
    return true;
  });
}

/* ---------------- 統計・ホーム画面 ---------------- */

function computeStats() {
  const progress = loadProgress();
  const filtered = filterCards(allCards);
  const t = today();

  let due = 0, mastered = 0, learning = 0, newCount = 0;
  for (const c of filtered) {
    const status = cardStatus(c, progress);
    if (status === 'new') {
      newCount++;
    } else if (status === 'mastered') {
      mastered++;
      if (progress[c.id].due <= t) due++;
    } else {
      learning++;
      if (progress[c.id].due <= t) due++;
    }
  }
  return { total: filtered.length, due, mastered, learning, newCount };
}

function renderHome() {
  const stats = computeStats();
  document.getElementById('stat-due').textContent = stats.due;
  document.getElementById('stat-new').textContent = stats.newCount;
  document.getElementById('stat-total').textContent = stats.total;
  document.getElementById('legend-mastered-count').textContent = stats.mastered;
  document.getElementById('legend-learning-count').textContent = stats.learning;
  document.getElementById('legend-new-count').textContent = stats.newCount;

  const total = stats.total || 1;
  document.getElementById('bar-mastered').style.width = `${(stats.mastered / total) * 100}%`;
  document.getElementById('bar-learning').style.width = `${(stats.learning / total) * 100}%`;

  const canStudy = stats.due > 0 || stats.newCount > 0;
  document.getElementById('btn-start-study').disabled = !canStudy;
  document.getElementById('no-cards-msg').classList.toggle('hidden', canStudy);

  document.getElementById('home-loading').classList.add('hidden');
  document.getElementById('home-content').classList.remove('hidden');
}

/* ---------------- セッション構築 ---------------- */

function buildSessionQueue() {
  const progress = loadProgress();
  const filtered = filterCards(allCards);
  const t = today();

  const dueList = shuffle(filtered.filter((c) => {
    const e = progress[c.id];
    return e && e.box > 0 && e.due <= t;
  }));
  const newList = shuffle(filtered.filter((c) => cardStatus(c, progress) === 'new'));

  const counter = loadNewCounter();
  const newAllowed = Math.max(0, settings.newPerDay - counter.count);

  const queue = dueList.slice(0, settings.sessionSize);
  const remaining = Math.max(0, settings.sessionSize - queue.length);
  const newToAdd = newList.slice(0, Math.min(remaining, newAllowed));

  sessionNewIds = new Set(newToAdd.map((c) => c.id));
  return shuffle(queue.concat(newToAdd));
}

/* ---------------- 採点（Leitner） ---------------- */

function grade(cardId, isCorrect) {
  const progress = loadProgress();
  const entry = progress[cardId] || { box: 0, due: today(), correct: 0, wrong: 0, last: null };

  if (isCorrect) {
    entry.box = Math.min(entry.box + 1, MASTERED_BOX);
    entry.correct++;
  } else {
    entry.box = 1;
    entry.wrong++;
  }
  entry.due = addDays(today(), BOX_INTERVALS[entry.box - 1]);
  entry.last = new Date().toISOString();

  progress[cardId] = entry;
  saveProgress(progress);

  if (sessionNewIds.has(cardId)) {
    const counter = loadNewCounter();
    counter.count++;
    saveNewCounter(counter);
  }
}

/* ---------------- 学習画面: カード描画 ---------------- */

function currentCard() {
  return sessionQueue[sessionIndex];
}

function renderStudyProgress() {
  const label = document.getElementById('study-progress-label');
  const fill = document.getElementById('study-progress-fill');
  label.textContent = `${sessionIndex} / ${sessionQueue.length}`;
  fill.style.width = `${(sessionIndex / sessionQueue.length) * 100}%`;
}

function renderCard() {
  const card = currentCard();
  currentAutoResult = null;

  document.getElementById('card-type-badge').textContent = TYPE_LABELS[card.type] || card.type;
  document.getElementById('card-category').textContent = card.grammar_category;
  document.getElementById('question-text').textContent = card.question;

  document.getElementById('answer-reveal').classList.add('hidden');
  document.getElementById('grade-buttons').classList.add('hidden');
  document.getElementById('submit-row').classList.remove('hidden');

  const area = document.getElementById('answer-area');
  area.innerHTML = '';

  if (card.type === 'multiple_choice') {
    document.getElementById('submit-row').classList.add('hidden');
    const list = document.createElement('div');
    list.className = 'choice-list';
    card.choices.forEach((choice) => {
      const btn = document.createElement('button');
      btn.className = 'choice-btn';
      btn.textContent = choice;
      btn.addEventListener('click', () => handleChoiceAnswer(choice));
      list.appendChild(btn);
    });
    area.appendChild(list);
  } else if (card.type === 'reorder') {
    const bank = document.createElement('div');
    bank.className = 'reorder-bank';
    const assembled = document.createElement('div');
    assembled.className = 'reorder-assembled';

    const tokens = shuffle(card.choices).map((text, i) => ({ text, id: i, used: false }));
    const picked = []; // クリック順を保持する配列

    function renderTokens() {
      bank.innerHTML = '';
      assembled.innerHTML = '';
      tokens.forEach((tok) => {
        const chip = document.createElement('button');
        chip.className = 'token-chip' + (tok.used ? ' used' : '');
        chip.textContent = tok.text;
        chip.disabled = tok.used;
        chip.addEventListener('click', () => {
          tok.used = true;
          picked.push(tok);
          renderTokens();
        });
        bank.appendChild(chip);
      });
      picked.forEach((tok) => {
        const chip = document.createElement('button');
        chip.className = 'token-chip';
        chip.textContent = tok.text;
        chip.addEventListener('click', () => {
          tok.used = false;
          picked.splice(picked.indexOf(tok), 1);
          renderTokens();
        });
        assembled.appendChild(chip);
      });
    }
    renderTokens();

    area.appendChild(bank);
    area.appendChild(assembled);
    area._getAssembled = () => picked.map((t) => t.text).join(' ');
  } else {
    // fill_blank / translate_ja_to_en
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'answer-input';
    input.placeholder = card.type === 'translate_ja_to_en' ? '英文を入力' : '空所に入る語句を入力';
    input.id = 'text-answer-input';
    area.appendChild(input);
  }

  renderStudyProgress();
}

function revealAnswer(card, isCorrect) {
  currentAutoResult = isCorrect;

  document.getElementById('submit-row').classList.add('hidden');
  const reveal = document.getElementById('answer-reveal');
  reveal.classList.remove('hidden');

  const banner = document.getElementById('result-banner');
  banner.textContent = isCorrect ? '⭕ 正解' : '✕ 不正解';
  banner.className = 'result-banner ' + (isCorrect ? 'is-correct' : 'is-wrong');

  document.getElementById('correct-answer-text').textContent = `正解: ${card.answer}`;
  document.getElementById('explanation-text').textContent = card.explanation || '';
  const source = card.book ? `${BOOK_LABELS[card.book] || card.book} p.${card.page}` : '';
  document.getElementById('card-meta').textContent = source;

  const gradeButtons = document.getElementById('grade-buttons');
  gradeButtons.classList.remove('hidden');
  document.getElementById('btn-grade-wrong').classList.toggle('suggested', !isCorrect);
  document.getElementById('btn-grade-correct').classList.toggle('suggested', isCorrect);
}

function handleChoiceAnswer(choice) {
  const card = currentCard();
  const buttons = document.querySelectorAll('#answer-area .choice-btn');
  buttons.forEach((btn) => {
    btn.disabled = true;
    if (btn.textContent === card.answer) btn.classList.add('correct-choice');
    if (btn.textContent === choice && choice !== card.answer) btn.classList.add('wrong-choice');
    if (btn.textContent === choice) btn.classList.add('selected');
  });
  revealAnswer(card, choice === card.answer);
}

function handleTextAnswer() {
  const card = currentCard();
  const input = document.getElementById('text-answer-input');
  const value = input ? input.value : '';
  const isCorrect = normalizeAnswerText(value) === normalizeAnswerText(card.answer);
  revealAnswer(card, isCorrect);
}

function handleReorderAnswer() {
  const card = currentCard();
  const area = document.getElementById('answer-area');
  const assembledText = area._getAssembled ? area._getAssembled() : '';
  const isCorrect = normalizeCompact(assembledText) === normalizeCompact(card.answer);
  revealAnswer(card, isCorrect);
}

function handleSubmitAnswer() {
  const card = currentCard();
  if (card.type === 'reorder') {
    handleReorderAnswer();
  } else {
    handleTextAnswer();
  }
}

function handleGrade(isCorrect) {
  const card = currentCard();
  grade(card.id, isCorrect);
  sessionStats.total++;
  if (isCorrect) sessionStats.correct++;

  sessionIndex++;
  if (sessionIndex >= sessionQueue.length) {
    showResultScreen();
  } else {
    renderCard();
  }
}

/* ---------------- 画面遷移 ---------------- */

function showScreen(id) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

function startStudySession() {
  sessionQueue = buildSessionQueue();
  if (sessionQueue.length === 0) return;
  sessionIndex = 0;
  sessionStats = { correct: 0, total: 0 };
  showScreen('screen-study');
  renderCard();
}

function showResultScreen() {
  document.getElementById('result-correct').textContent = sessionStats.correct;
  document.getElementById('result-total').textContent = sessionStats.total;
  document.getElementById('result-studied').textContent = sessionStats.total;
  const rate = sessionStats.total ? Math.round((sessionStats.correct / sessionStats.total) * 100) : 0;
  document.getElementById('result-rate').textContent = rate;
  showScreen('screen-result');
}

function returnHome() {
  renderHome();
  showScreen('screen-home');
}

/* ---------------- 設定画面 ---------------- */

function renderSettingsForm() {
  document.getElementById('setting-session-size').value = settings.sessionSize;
  document.getElementById('setting-new-per-day').value = settings.newPerDay;
}

function saveSettingsFromForm() {
  const sessionSize = parseInt(document.getElementById('setting-session-size').value, 10);
  const newPerDay = parseInt(document.getElementById('setting-new-per-day').value, 10);
  settings.sessionSize = Number.isFinite(sessionSize) && sessionSize > 0 ? sessionSize : DEFAULT_SETTINGS.sessionSize;
  settings.newPerDay = Number.isFinite(newPerDay) && newPerDay >= 0 ? newPerDay : DEFAULT_SETTINGS.newPerDay;
  saveSettings();
  const msg = document.getElementById('settings-save-msg');
  msg.classList.remove('hidden');
  setTimeout(() => msg.classList.add('hidden'), 1500);
}

function exportProgress() {
  const data = {
    progress: loadProgress(),
    settings,
    exportedAt: new Date().toISOString(),
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `grammarcards-progress-${today()}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

function importProgress(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(reader.result);
      if (data.progress) saveProgress(data.progress);
      if (data.settings) {
        settings = { ...DEFAULT_SETTINGS, ...data.settings };
        saveSettings();
        renderSettingsForm();
      }
      renderHome();
      alert('学習履歴を読み込みました。');
    } catch (e) {
      alert('ファイルの読み込みに失敗しました。');
    }
  };
  reader.readAsText(file);
}

function resetProgress() {
  if (!confirm('全ての学習履歴を削除します。よろしいですか？')) return;
  localStorage.removeItem(STORAGE_PROGRESS);
  localStorage.removeItem(STORAGE_NEWCOUNT);
  renderHome();
}

/* ---------------- 初期化 ---------------- */

function wireEvents() {
  document.getElementById('btn-open-settings').addEventListener('click', () => {
    renderSettingsForm();
    showScreen('screen-settings');
  });
  document.getElementById('btn-close-settings').addEventListener('click', () => {
    saveSettingsFromForm();
    returnHome();
  });

  document.getElementById('btn-start-study').addEventListener('click', startStudySession);
  document.getElementById('btn-quit-study').addEventListener('click', () => {
    if (sessionIndex > 0 && confirm('学習を中断してホームに戻りますか？')) {
      returnHome();
    } else if (sessionIndex === 0) {
      returnHome();
    }
  });

  document.getElementById('btn-submit-answer').addEventListener('click', handleSubmitAnswer);
  document.getElementById('btn-grade-wrong').addEventListener('click', () => handleGrade(false));
  document.getElementById('btn-grade-correct').addEventListener('click', () => handleGrade(true));

  document.getElementById('btn-result-home').addEventListener('click', returnHome);
  document.getElementById('btn-result-again').addEventListener('click', startStudySession);

  document.getElementById('setting-session-size').addEventListener('change', saveSettingsFromForm);
  document.getElementById('setting-new-per-day').addEventListener('change', saveSettingsFromForm);

  document.getElementById('btn-export').addEventListener('click', exportProgress);
  document.getElementById('import-file').addEventListener('change', (e) => {
    if (e.target.files[0]) importProgress(e.target.files[0]);
  });
  document.getElementById('btn-reset').addEventListener('click', resetProgress);

  document.getElementById('book-filter-row').addEventListener('click', (e) => {
    const btn = e.target.closest('.filter-chip');
    if (!btn) return;
    settings.bookFilter = btn.dataset.book;
    saveSettings();
    document.querySelectorAll('#book-filter-row .filter-chip').forEach((b) => b.classList.remove('selected'));
    btn.classList.add('selected');
    renderHome();
  });

  const categorySearch = document.getElementById('category-search');
  let searchDebounce;
  categorySearch.addEventListener('input', () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      settings.categoryFilter = categorySearch.value;
      saveSettings();
      renderHome();
    }, 250);
  });
}

async function init() {
  wireEvents();
  document.getElementById('category-search').value = settings.categoryFilter;
  document.querySelectorAll('#book-filter-row .filter-chip').forEach((b) => {
    b.classList.toggle('selected', b.dataset.book === settings.bookFilter);
  });

  await loadCards();
  renderHome();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch((e) => console.warn('SW登録失敗', e));
  }
}

init();

/**
 * RDM Agent Chat — Frontend logic.
 * Communicates with Flask backend /api/chat which calls
 * Databricks Foundation Model API (replaces Azure OpenAI).
 */

let rdmOpen = false;
let _rdmHistory = [];
let _rdmBusy = false;

function toggleRDM() {
  rdmOpen = !rdmOpen;
  document.getElementById('rdmPanel').classList.toggle('open', rdmOpen);
  const btn = document.getElementById('rdmBtn');
  if (btn) btn.classList.toggle('active', rdmOpen);
  if (rdmOpen) renderWelcomeChips(); // Refresh chips on open
}

function rdmQuickAsk(prompt) {
  const input = document.getElementById('rdmInput');
  input.value = prompt;
  sendRDM();
}

function rdmAppendMsg(role, content, isHtml) {
  const body = document.getElementById('rdmBody');
  const welcome = document.getElementById('rdmWelcome');
  if (welcome) welcome.style.display = 'none';

  const div = document.createElement('div');
  div.className = 'rdm-msg ' + role;
  const sender = role === 'user' ? 'You' : '\uD83E\uDD16 RDM Agent';
  var copyBtn = role === 'agent' ? '<button class="rdm-copy-btn" onclick="copyBubble(this)" title="Copy">\u2398 Copy</button>' : '';
  div.innerHTML = '<div class="rdm-sender">' + sender + '</div>'
    + '<div class="rdm-bubble">' + copyBtn + (isHtml ? content : escHtml(content)) + '</div>';
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function copyBubble(btn) {
  var bubble = btn.parentElement;
  // Get text content (strip HTML tags for clean copy)
  var clone = bubble.cloneNode(true);
  // Remove the copy button itself from clone
  var copyEl = clone.querySelector('.rdm-copy-btn');
  if (copyEl) copyEl.remove();
  var text = clone.innerText || clone.textContent;
  navigator.clipboard.writeText(text.trim()).then(function() {
    btn.textContent = '\u2713 Copied';
    btn.classList.add('copied');
    setTimeout(function() { btn.textContent = '\u2398 Copy'; btn.classList.remove('copied'); }, 2000);
  });
}

function rdmAppendTyping() {
  const body = document.getElementById('rdmBody');
  const div = document.createElement('div');
  div.className = 'rdm-msg agent';
  div.id = 'rdm-typing';
  div.innerHTML = '<div class="rdm-sender">\uD83E\uDD16 RDM Agent</div>'
    + '<div class="rdm-bubble thinking"><div class="rdm-typing">'
    + '<div class="rdm-dot"></div><div class="rdm-dot"></div><div class="rdm-dot"></div>'
    + '</div></div>';
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function removeTyping() {
  const el = document.getElementById('rdm-typing');
  if (el) el.remove();
}

function formatReply(text) {
  // Step 1: Extract markdown tables into placeholders (before HTML escaping)
  var tables = [];
  text = text.replace(/(?:^|\n)(\|[^\n]+\|\n\|[\s:\-|]+\|\n(?:\|[^\n]+\|(?:\n|$))+)/gm, function(match) {
    var lines = match.trim().split('\n');
    if (lines.length < 3) return match;
    var headers = lines[0].split('|').filter(function(c) { return c.trim() !== ''; });
    // Detect alignment from separator
    var aligns = lines[1].split('|').filter(function(c) { return c.trim() !== ''; }).map(function(sep) {
      sep = sep.trim();
      if (sep.startsWith(':') && sep.endsWith(':')) return 'center';
      if (sep.endsWith(':')) return 'right';
      return 'left';
    });
    var rows = [];
    for (var i = 2; i < lines.length; i++) {
      var cells = lines[i].split('|').filter(function(c) { return c.trim() !== ''; });
      if (cells.length > 0) rows.push(cells);
    }
    // Build styled HTML table
    var html = '<div class="rdm-table-wrap"><table class="rdm-reply-table"><thead><tr>';
    headers.forEach(function(h, idx) {
      var align = aligns[idx] || 'left';
      html += '<th style="text-align:' + align + '">' + h.trim().replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>') + '</th>';
    });
    html += '</tr></thead><tbody>';
    rows.forEach(function(row, ri) {
      html += '<tr>';
      row.forEach(function(cell, ci) {
        var align = aligns[ci] || 'left';
        var val = cell.trim().replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
        // Make key values in first column clickable (6-10 digit numbers)
        if (ci === 0 && /^\d{6,10}$/.test(val)) {
          val = '<a class="rdm-key-link" href="#" onclick="filterByKey(\'' + val + '\');return false;" title="Show in results">' + val + '</a>';
        }
        html += '<td style="text-align:' + align + '">' + val + '</td>';
      });
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    var placeholder = '\x00TABLE_' + tables.length + '\x00';
    tables.push(html);
    return '\n' + placeholder + '\n';
  });

  // Step 2: HTML-escape the remaining text
  text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // Step 3: Apply markdown formatting
  text = text
    .replace(/^### (.+)$/gm, '<div class="rdm-h3">$1</div>')
    .replace(/^## (.+)$/gm, '<div class="rdm-h2">$1</div>')
    .replace(/^# (.+)$/gm, '<div class="rdm-h1">$1</div>')
    .replace(/^---$/gm, '<hr class="rdm-hr">')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^[\-\*\u2022] (.+)$/gm, '<div class="rdm-li">\u2022 $1</div>')
    .replace(/\n{2,}/g, '<br><br>')
    .replace(/\n/g, '<br>');

  // Step 4: Re-insert tables in place of placeholders
  tables.forEach(function(tableHtml, idx) {
    text = text.replace('\x00TABLE_' + idx + '\x00', tableHtml);
  });

  // Step 5: Make key references clickable (filter results when clicked)
  // Match patterns like key values in backticks or quoted numbers (e.g. `0001234567`, "0001234567")
  text = text.replace(/(?:<code>)(\d{6,10})(?:<\/code>)/g, function(match, key) {
    return '<a class="rdm-key-link" href="#" onclick="filterByKey(\'' + key + '\');return false;" title="Filter results for key ' + key + '">' + key + '</a>';
  });

  return text;
}

function renderSQLResult(result) {
  if (!result || result.error) {
    return result ? '<div class="sql-error">\u26A0 ' + escHtml(result.error) + '</div>' : '';
  }
  if (!result.rows || !result.rows.length) {
    return '<div class="sql-empty">No rows returned.</div>';
  }
  let html = '<div class="sql-result">';
  html += '<details class="sql-query-details"><summary>\u25B6 Query used</summary><pre class="sql-pre">' + escHtml(result.sql) + '</pre></details>';
  html += '<div class="sql-table-wrap"><table class="sql-table"><thead><tr>';
  result.columns.forEach(c => { html += '<th>' + escHtml(c) + '</th>'; });
  html += '</tr></thead><tbody>';
  result.rows.forEach(row => {
    html += '<tr>' + row.map(v => '<td>' + (v != null ? escHtml(String(v)) : '<span class="sql-null">null</span>') + '</td>').join('') + '</tr>';
  });
  html += '</tbody></table></div>';
  html += '<div class="sql-rowcount">' + result.rows.length + ' row(s)</div></div>';
  return html;
}

async function sendRDM() {
  const input = document.getElementById('rdmInput');
  const text = input.value.trim();
  if (!text || _rdmBusy) return;

  rdmAppendMsg('user', text);
  input.value = '';
  _rdmHistory.push({ role: 'user', content: text });
  trackQuestion(text); // Adaptive learning

  rdmAppendTyping();
  _rdmBusy = true;
  document.getElementById('rdmSendBtn').disabled = true;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: _rdmHistory.slice(-8) })
    });
    const data = await res.json();
    removeTyping();

    if (data.error) {
      rdmAppendMsg('agent', '\u26A0\uFE0F **Error:** ' + data.error, true);
    } else {
      let replyHtml = formatReply(data.reply.replace(/```sql[\s\S]*?```/gi, '').trim());

      // If LLM returned SQL, execute it and show results
      if (data.sql) {
        const sqlRes = await fetch('/api/chat/sql', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sql: data.sql })
        });
        const sqlData = await sqlRes.json();
        replyHtml += renderSQLResult(sqlData);
      }

      rdmAppendMsg('agent', replyHtml, true);
      _rdmHistory.push({ role: 'assistant', content: data.reply });

      // Show follow-up suggestions after agent reply
      var suggestions = data.suggestions || generateSuggestions(text);
      if (suggestions && suggestions.length) {
        rdmAppendSuggestions(suggestions);
      }
    }
  } catch (e) {
    removeTyping();
    rdmAppendMsg('agent', '\u26A0\uFE0F **Error:** ' + e.message, true);
  }

  _rdmBusy = false;
  document.getElementById('rdmSendBtn').disabled = false;
}

function escHtml(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ==================== Smart Welcome Chips & Adaptive Suggestions ====================

// Question categories for adaptive learning
var _questionPatterns = {
  overview: { keywords: ['summary', 'overview', 'stats', 'loaded', 'status'], count: 0, label: 'overview' },
  conflicts: { keywords: ['conflict', 'differ', 'mismatch', 'disagree', 'wrong'], count: 0, label: 'conflicts' },
  fields: { keywords: ['field', 'column', 'attribute', 'which field'], count: 0, label: 'field analysis' },
  samples: { keywords: ['sample', 'example', 'show me', 'values', 'look like'], count: 0, label: 'samples' },
  transforms: { keywords: ['transform', 'normalize', 'fix', 'rule', 'clean'], count: 0, label: 'transforms' },
  export: { keywords: ['export', 'download', 'excel', 'csv'], count: 0, label: 'export' },
  quality: { keywords: ['quality', 'rate', 'percent', 'coverage', 'completeness'], count: 0, label: 'data quality' },
};

// Load saved patterns from localStorage
function loadQuestionPatterns() {
  try {
    var saved = localStorage.getItem('rdm_question_patterns');
    if (saved) {
      var parsed = JSON.parse(saved);
      for (var cat in parsed) {
        if (_questionPatterns[cat]) _questionPatterns[cat].count = parsed[cat];
      }
    }
  } catch (e) {}
}

// Track a user question
function trackQuestion(text) {
  var q = text.toLowerCase();
  for (var cat in _questionPatterns) {
    var p = _questionPatterns[cat];
    for (var i = 0; i < p.keywords.length; i++) {
      if (q.indexOf(p.keywords[i]) >= 0) {
        p.count++;
        break;
      }
    }
  }
  // Save to localStorage
  var counts = {};
  for (var c in _questionPatterns) counts[c] = _questionPatterns[c].count;
  try { localStorage.setItem('rdm_question_patterns', JSON.stringify(counts)); } catch (e) {}
}

// Get session state for context-aware chips
function getSessionState() {
  var hasSources = uploadedSources && Object.keys(uploadedSources).length > 0;
  var hasMapping = !!currentMapping;
  var hasResults = allDiffRows && allDiffRows.length > 0;
  return { hasSources: hasSources, hasMapping: hasMapping, hasResults: hasResults };
}

// Render contextual welcome chips based on session state
function renderWelcomeChips() {
  var el = document.getElementById('rdmWelcomeChips');
  if (!el) return;

  var state = getSessionState();
  var chips = [];

  if (!state.hasSources) {
    // No data loaded yet
    chips = [
      { icon: '\uD83D\uDE80', text: 'How to get started', prompt: 'How do I get started with a 3-way comparison?' },
      { icon: '\uD83D\uDCC2', text: 'What files do I need?', prompt: 'What file formats and sources do I need to upload?' },
      { icon: '\u2699\uFE0F', text: 'What can you do?', prompt: 'What are your capabilities as RDM Agent?' },
    ];
  } else if (!state.hasResults) {
    // Files loaded but no comparison yet
    chips = [
      { icon: '\uD83D\uDCC4', text: "What's loaded?", prompt: 'What data sources are loaded?' },
      { icon: '\uD83D\uDD04', text: 'Run comparison', prompt: 'How do I run a comparison?' },
      { icon: '\uD83D\uDDFA\uFE0F', text: 'Explain field mapping', prompt: 'Explain how fields are mapped across sources' },
    ];
  } else {
    // Comparison results available — show smart suggestions
    chips = getAdaptiveChips();
  }

  var html = '';
  chips.forEach(function(c) {
    html += '<button class="rdm-qp" onclick="rdmQuickAsk(\'' + c.prompt.replace(/'/g, "\\'") + '\')">' + c.icon + ' ' + c.text + '</button>';
  });
  el.innerHTML = html;
}

// Generate adaptive chips based on what user asks most + session state
function getAdaptiveChips() {
  var state = getSessionState();
  
  // Base suggestions for post-comparison state
  var allChips = [
    { icon: '\u26A0\uFE0F', text: 'Top conflicts', prompt: 'Which fields have the most conflicts?', category: 'conflicts', priority: 10 },
    { icon: '\uD83D\uDCCA', text: 'Match rate', prompt: 'What is the overall match rate?', category: 'quality', priority: 9 },
    { icon: '\uD83D\uDD0D', text: 'Show sample conflicts', prompt: 'Show me sample conflicts for the most problematic field', category: 'samples', priority: 8 },
    { icon: '\uD83D\uDCC8', text: 'Field breakdown', prompt: 'Give me a breakdown of conflicts by field', category: 'fields', priority: 7 },
    { icon: '\u2728', text: 'Suggest transforms', prompt: 'Which fields could benefit from normalization transforms?', category: 'transforms', priority: 6 },
    { icon: '\uD83D\uDCE5', text: 'Export results', prompt: 'How do I export the conflict results?', category: 'export', priority: 5 },
    { icon: '\uD83D\uDCA1', text: 'Insights', prompt: 'What are the key insights from this comparison?', category: 'overview', priority: 7 },
    { icon: '\uD83C\uDFAF', text: 'Root cause', prompt: 'What might be causing the most common conflicts?', category: 'conflicts', priority: 6 },
  ];

  // Boost priority based on user's question history
  allChips.forEach(function(chip) {
    var cat = _questionPatterns[chip.category];
    if (cat) {
      // Reduce priority for categories already asked about (avoid repetition)
      chip.priority -= Math.min(cat.count * 2, 6);
      // But boost categories the user frequently explores
      if (cat.count >= 3) chip.priority += 1; // They clearly care about this
    }
  });

  // Sort by priority and take top 4
  allChips.sort(function(a, b) { return b.priority - a.priority; });
  return allChips.slice(0, 4);
}

// Refresh welcome chips when session state changes
function refreshWelcomeChips() {
  var welcome = document.getElementById('rdmWelcome');
  if (welcome && welcome.style.display !== 'none') {
    renderWelcomeChips();
  }
}

// Initialize on load
loadQuestionPatterns();

// ==================== User Profile & Identity ====================

var _currentUser = { email: '', name: '' };

// Fetch user identity and populate both header avatar + chat badge
(function fetchCurrentUser() {
  fetch('/api/whoami')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data.email) return;
      _currentUser.email = data.email;

      // Derive display name from email prefix
      var raw = data.email.split('@')[0].replace(/[._-]/g, ' ');
      var name = raw.replace(/\b\w/g, function(c) { return c.toUpperCase(); });
      _currentUser.name = name;

      // --- Main header avatar ---
      var initials = name.split(' ').map(function(w) { return w[0]; }).join('').slice(0, 2).toUpperCase();
      var avatarEl = document.getElementById('userInitials');
      if (avatarEl) avatarEl.textContent = initials;
      var avatarWrap = document.getElementById('userAvatar');
      if (avatarWrap) avatarWrap.title = data.email;

      // Populate user menu
      var menuName = document.getElementById('userMenuName');
      if (menuName) menuName.textContent = name;
      var menuEmail = document.getElementById('userMenuEmail');
      if (menuEmail) menuEmail.textContent = data.email;

      // --- Chat panel user badge ---
      var badge = document.getElementById('rdmUserBadge');
      if (badge) {
        badge.textContent = name.split(' ')[0]; // first name only
        badge.title = data.email;
        badge.style.display = 'inline-block';
      }
    })
    .catch(function() {});
})();

function toggleUserMenu() {
  var menu = document.getElementById('userMenu');
  if (menu) menu.classList.toggle('open');
}

// Close user menu on outside click
document.addEventListener('click', function(e) {
  var profile = document.getElementById('userProfile');
  if (profile && !profile.contains(e.target)) {
    var menu = document.getElementById('userMenu');
    if (menu) menu.classList.remove('open');
  }
});

// ==================== Agent Panel Width & Fullscreen ====================

var _rdmFullscreen = false;
var _rdmWideMode = false;

function setRdmWidth(mode) {
  var panel = document.getElementById('rdmPanel');
  var btn = document.getElementById('rdmExpandBtn');
  if (!panel) return;
  if (mode === 'wide') {
    _rdmWideMode = !_rdmWideMode;
    panel.classList.toggle('wide', _rdmWideMode);
    if (btn) {
      btn.classList.toggle('active', _rdmWideMode);
      btn.title = _rdmWideMode ? 'Restore width' : 'Expand panel';
    }
    // Exit fullscreen if active
    if (_rdmFullscreen) toggleRdmFullscreen();
  }
}

function toggleRdmFullscreen() {
  var panel = document.getElementById('rdmPanel');
  var btn = document.getElementById('rdmFullscreenBtn');
  if (!panel) return;
  _rdmFullscreen = !_rdmFullscreen;
  panel.classList.toggle('fullscreen', _rdmFullscreen);
  if (btn) {
    btn.innerHTML = _rdmFullscreen ? '&#9633;' : '&#9974;'; // restore vs fullscreen icon
    btn.classList.toggle('active', _rdmFullscreen);
    btn.title = _rdmFullscreen ? 'Exit fullscreen' : 'Fullscreen agent';
  }
  // In fullscreen, also exit wide mode styling (width:100vw takes over)
  if (_rdmFullscreen && _rdmWideMode) {
    _rdmWideMode = false;
    panel.classList.remove('wide');
    var expBtn = document.getElementById('rdmExpandBtn');
    if (expBtn) expBtn.classList.remove('active');
  }
  // Scroll chat to bottom after layout shift
  setTimeout(function() {
    var body = document.getElementById('rdmBody');
    if (body) body.scrollTop = body.scrollHeight;
  }, 50);
}

// ==================== Drag-to-Resize Handle ====================

(function initResizeHandle() {
  var handle = document.getElementById('rdmResizeHandle');
  var panel = document.getElementById('rdmPanel');
  if (!handle || !panel) return;

  var dragging = false;
  var startX = 0;
  var startWidth = 0;
  var MIN_WIDTH = 300;
  var MAX_WIDTH = Math.round(window.innerWidth * 0.75);

  handle.addEventListener('mousedown', function(e) {
    if (_rdmFullscreen) return;
    dragging = true;
    startX = e.clientX;
    startWidth = panel.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor = 'ew-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    MAX_WIDTH = Math.round(window.innerWidth * 0.75);
    var dx = startX - e.clientX; // dragging left = wider
    var newWidth = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth + dx));
    panel.style.width = newWidth + 'px';
    // Remove preset classes since we're in custom-width mode
    panel.classList.remove('wide');
    _rdmWideMode = false;
  });

  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });

  // Double-click handle: reset to default width
  handle.addEventListener('dblclick', function() {
    panel.style.width = '';
    panel.classList.remove('wide');
    _rdmWideMode = false;
    var btn = document.getElementById('rdmExpandBtn');
    if (btn) { btn.classList.remove('active'); btn.title = 'Expand panel'; }
  });
})();

// ==================== Follow-up Suggestions ====================

function rdmAppendSuggestions(suggestions) {
  var body = document.getElementById('rdmBody');
  var div = document.createElement('div');
  div.className = 'rdm-suggestions';
  var html = '';
  suggestions.forEach(function(s) {
    html += '<button class="rdm-suggest-btn" onclick="rdmQuickAsk(this.textContent)">' + escHtml(s) + '</button>';
  });
  div.innerHTML = html;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function generateSuggestions(lastQuestion) {
  var q = lastQuestion.toLowerCase();
  // Context-aware suggestions based on what was asked + adaptive boosting
  var suggestions = [];

  if (q.indexOf('loaded') >= 0 || q.indexOf('source') >= 0 || q.indexOf('file') >= 0) {
    suggestions = ['Show mapping summary', 'Run comparison', 'Which fields are mapped?'];
  } else if (q.indexOf('conflict') >= 0 || q.indexOf('differ') >= 0 || q.indexOf('mismatch') >= 0) {
    suggestions = ['Show sample conflicts', 'Suggest transforms to fix', 'Export conflicts to Excel'];
  } else if (q.indexOf('match') >= 0 || q.indexOf('same') >= 0 || q.indexOf('agree') >= 0) {
    suggestions = ['What is the overall match rate?', 'Which fields always match?', 'Show conflict breakdown'];
  } else if (q.indexOf('transform') >= 0 || q.indexOf('normalize') >= 0) {
    suggestions = ['Show active transforms', 'Which fields need transforms?', 'Re-run comparison'];
  } else if (q.indexOf('export') >= 0 || q.indexOf('download') >= 0) {
    suggestions = ['Export all rows', 'Export conflicts only', 'Show summary stats'];
  } else if (q.indexOf('field') >= 0 || q.indexOf('column') >= 0) {
    suggestions = ['Show sample values', 'Which fields conflict most?', 'Suggest normalization'];
  } else {
    // Use adaptive: suggest from least-explored categories
    var cats = Object.keys(_questionPatterns).map(function(k) {
      return { cat: k, count: _questionPatterns[k].count };
    }).sort(function(a, b) { return a.count - b.count; });

    var prompts = {
      overview: 'Give me a summary overview',
      conflicts: 'Which fields have most conflicts?',
      fields: 'Break down conflicts by field',
      samples: 'Show sample conflict values',
      transforms: 'Suggest transforms to reduce conflicts',
      export: 'Export results to Excel',
      quality: 'What is the data quality score?',
    };
    for (var i = 0; i < Math.min(3, cats.length); i++) {
      if (prompts[cats[i].cat]) suggestions.push(prompts[cats[i].cat]);
    }
  }

  return suggestions.slice(0, 3);
}

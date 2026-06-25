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
  // First, extract and convert markdown tables to HTML
  text = text.replace(/(?:^|\n)(\|[^\n]+\|\n\|[\s:\-|]+\|\n(?:\|[^\n]+\|(?:\n|$))+)/gm, function(match) {
    var lines = match.trim().split('\n');
    if (lines.length < 3) return match;
    // Parse header
    var headers = lines[0].split('|').filter(function(c) { return c.trim() !== ''; });
    // Skip separator line (lines[1])
    // Parse rows
    var rows = [];
    for (var i = 2; i < lines.length; i++) {
      var cells = lines[i].split('|').filter(function(c) { return c.trim() !== ''; });
      if (cells.length > 0) rows.push(cells);
    }
    // Build HTML table
    var html = '<table class="rdm-reply-table"><thead><tr>';
    headers.forEach(function(h) { html += '<th>' + h.trim() + '</th>'; });
    html += '</tr></thead><tbody>';
    rows.forEach(function(row) {
      html += '<tr>';
      row.forEach(function(cell) { html += '<td>' + cell.trim() + '</td>'; });
      html += '</tr>';
    });
    html += '</tbody></table>';
    return '\n' + html + '\n';
  });

  // Then format the rest of markdown
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    // Restore HTML tables that were escaped
    .replace(/&lt;table class=&quot;rdm-reply-table&quot;&gt;/g, '<table class="rdm-reply-table">')
    .replace(/&lt;\/table&gt;/g, '</table>')
    .replace(/&lt;thead&gt;/g, '<thead>').replace(/&lt;\/thead&gt;/g, '</thead>')
    .replace(/&lt;tbody&gt;/g, '<tbody>').replace(/&lt;\/tbody&gt;/g, '</tbody>')
    .replace(/&lt;tr&gt;/g, '<tr>').replace(/&lt;\/tr&gt;/g, '</tr>')
    .replace(/&lt;th&gt;/g, '<th>').replace(/&lt;\/th&gt;/g, '</th>')
    .replace(/&lt;td&gt;/g, '<td>').replace(/&lt;\/td&gt;/g, '</td>')
    // Headings
    .replace(/^### (.+)$/gm, '<div class="rdm-h3">$1</div>')
    .replace(/^## (.+)$/gm, '<div class="rdm-h2">$1</div>')
    .replace(/^# (.+)$/gm, '<div class="rdm-h1">$1</div>')
    // Horizontal rules
    .replace(/^---$/gm, '<hr class="rdm-hr">')
    // Bold, code, bullets
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^[\-\*\u2022] (.+)$/gm, '<div class="rdm-li">\u2022 $1</div>')
    .replace(/\n{2,}/g, '<br><br>')
    .replace(/\n/g, '<br>');
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
  // Context-aware suggestions based on what was asked
  if (q.indexOf('loaded') >= 0 || q.indexOf('source') >= 0 || q.indexOf('file') >= 0) {
    return ['Show mapping summary', 'Run comparison', 'Which fields are mapped?'];
  }
  if (q.indexOf('conflict') >= 0 || q.indexOf('differ') >= 0 || q.indexOf('mismatch') >= 0) {
    return ['Which fields have most conflicts?', 'Show sample conflicts', 'Export conflicts to Excel'];
  }
  if (q.indexOf('match') >= 0 || q.indexOf('same') >= 0 || q.indexOf('agree') >= 0) {
    return ['What is the overall match rate?', 'Which fields always match?', 'Show only conflicts'];
  }
  if (q.indexOf('transform') >= 0 || q.indexOf('normalize') >= 0) {
    return ['Show active transforms', 'Which fields need transforms?', 'Reset all transforms'];
  }
  if (q.indexOf('export') >= 0 || q.indexOf('download') >= 0) {
    return ['Export all rows', 'Export conflicts only', 'Show summary stats'];
  }
  // Default follow-ups
  return ['Show top conflicts', 'What is the match rate?', 'Which fields differ most?'];
}

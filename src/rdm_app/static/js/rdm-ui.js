/**
 * RDM 3-Way Diff — UI logic (tabs, upload, mapping, compare).
 * Handles frontend interactions and communicates with Flask backend.
 */

let activeMode = 'SKB';
let uploadedSources = {};

// ==================== Tab Navigation ====================

function showTab(n) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.remove('active');
    b.classList.toggle('enabled', parseInt(b.id.split('-')[2]) <= n);
  });
  document.getElementById('tab-panel-' + n).classList.add('active');
  document.getElementById('tab-btn-' + n).classList.add('active');
  // Compare tab uses full screen width (like the original HTML)
  var canvas = document.querySelector('.canvas');
  if (canvas) canvas.classList.toggle('full-width', n === 3);
}

// ==================== Mode Toggle ====================

function setMode(mode) {
  activeMode = mode;
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('mode-btn-active'));
  document.getElementById('modeBtn-' + mode).classList.add('mode-btn-active');
}

// ==================== File Upload ====================

function handleDrop(e, source) {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(source, file);
}

function handleFile(input, source) {
  const file = input.files[0];
  if (file) uploadFile(source, file);
}

function showFileProgress(source, filename) {
  var overlay = document.getElementById('fileLoadOverlay');
  document.getElementById('fileLoadSrc').textContent = source;
  document.getElementById('fileLoadTitle').textContent = 'Uploading file\u2026';
  document.getElementById('fileLoadSub').textContent = filename;
  document.getElementById('fileLoadBar').style.width = '0%';
  document.getElementById('fileLoadHint').textContent = 'Preparing upload\u2026';
  overlay.classList.remove('hidden');
}

function updateFileProgress(pct, phase) {
  document.getElementById('fileLoadBar').style.width = pct + '%';
  if (phase) document.getElementById('fileLoadHint').textContent = phase;
  if (pct >= 95) {
    document.getElementById('fileLoadTitle').textContent = 'Parsing file\u2026';
    document.getElementById('fileLoadHint').textContent = 'Server is processing. This may take a moment for large files.';
  }
}

function hideFileProgress() {
  document.getElementById('fileLoadOverlay').classList.add('hidden');
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

function uploadFile(source, file) {
  var zone = document.getElementById('drop-' + source);
  zone.classList.add('uploading');
  showFileProgress(source, file.name);

  var formData = new FormData();
  formData.append('file', file);
  formData.append('source', source);

  var xhr = new XMLHttpRequest();

  // Track upload progress (bytes sent to server)
  xhr.upload.addEventListener('progress', function(e) {
    if (e.lengthComputable) {
      var pct = Math.round((e.loaded / e.total) * 90); // 0-90% for upload phase
      updateFileProgress(pct, 'Uploading: ' + formatBytes(e.loaded) + ' / ' + formatBytes(e.total));
    }
  });

  xhr.upload.addEventListener('load', function() {
    updateFileProgress(95, 'Upload complete. Server is parsing the file\u2026');
  });

  xhr.addEventListener('load', function() {
    hideFileProgress();
    zone.classList.remove('uploading');

    if (xhr.status === 200) {
      try {
        var data = JSON.parse(xhr.responseText);
        if (data.error) {
          zone.classList.add('error');
          zone.querySelector('.upload-hint').textContent = data.error;
        } else {
          zone.classList.add('success');
          zone.querySelector('.upload-label').textContent = file.name;
          zone.querySelector('.upload-hint').textContent = data.row_count + ' rows \u00b7 ' + data.headers.length + ' columns';
          uploadedSources[source] = data;
          cacheSessionData(); // Auto-save to IndexedDB for quick restore
          if (typeof refreshWelcomeChips === 'function') refreshWelcomeChips();
          checkReady();
        }
      } catch (e) {
        zone.classList.add('error');
        zone.querySelector('.upload-hint').textContent = 'Invalid response from server';
      }
    } else {
      zone.classList.add('error');
      var msg = 'Upload failed (HTTP ' + xhr.status + ')';
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch(ex) {}
      zone.querySelector('.upload-hint').textContent = msg;
    }
  });

  xhr.addEventListener('error', function() {
    hideFileProgress();
    zone.classList.remove('uploading');
    zone.classList.add('error');
    zone.querySelector('.upload-hint').textContent = 'Connection error \u2014 file may be too large. Try a smaller file or CSV export.';
  });

  xhr.addEventListener('timeout', function() {
    hideFileProgress();
    zone.classList.remove('uploading');
    zone.classList.add('error');
    zone.querySelector('.upload-hint').textContent = 'Upload timed out. The file may be too large for processing.';
  });

  xhr.open('POST', '/api/upload');
  xhr.timeout = 120000; // 2 minute timeout for large files
  xhr.send(formData);
}

function checkReady() {
  const count = Object.keys(uploadedSources).length;
  document.getElementById('btnMapping').disabled = count < 2;
}

// ==================== Mapping ====================

var currentMapping = null;

async function goToMapping() {
  showTab(2);
  var res = await fetch('/api/mapping', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: activeMode })
  });
  var data = await res.json();
  if (data.error) {
    document.getElementById('mappingTable').innerHTML = '<p class="error">' + data.error + '</p>';
  } else {
    currentMapping = data;
    renderMapping(data);
    renderKeySelects(data);
  }
}

function renderMapping(mapping) {
  var fields = mapping.comparable_fields || [];
  var html = '<table class="mapping-table"><thead><tr><th></th><th>Field</th><th>COA</th><th>FAQ</th><th>DataPool</th><th>Match</th></tr></thead><tbody>';
  fields.forEach(function(f, idx) {
    var matchCount = Object.keys(f.sources).length;
    var matchClass = matchCount >= 3 ? 'chip-ok' : matchCount === 2 ? '' : 'chip-warn';
    html += '<tr>';
    html += '<td><input type="checkbox" checked data-field-idx="' + idx + '" onchange="toggleField(' + idx + ',this.checked)"></td>';
    html += '<td><strong>' + f.label + '</strong></td>';
    html += '<td>' + (f.sources.COA || '<span style="color:var(--text3)">—</span>') + '</td>';
    html += '<td>' + (f.sources.FAQ || '<span style="color:var(--text3)">—</span>') + '</td>';
    html += '<td>' + (f.sources.DataPool || '<span style="color:var(--text3)">—</span>') + '</td>';
    html += '<td><span class="chip ' + matchClass + '">' + matchCount + '/3</span></td>';
    html += '</tr>';
  });
  html += '</tbody></table>';
  document.getElementById('mappingTable').innerHTML = html;
  document.getElementById('mapMeta').textContent = fields.length + ' fields mapped';
}

function renderKeySelects(mapping) {
  var sources = Object.keys(uploadedSources);
  var html = '';
  sources.forEach(function(src) {
    var headers = uploadedSources[src].headers || [];
    html += '<div class="key-select-group">';
    html += '<label>' + src + ':</label>';
    html += '<select id="key-' + src + '">';
    headers.forEach(function(h) {
      html += '<option value="' + h + '">' + h + '</option>';
    });
    html += '</select></div>';
  });
  document.getElementById('keySelects').innerHTML = html;
}

function toggleField(idx, checked) {
  // Mark field as excluded/included for comparison
  if (currentMapping && currentMapping.comparable_fields[idx]) {
    currentMapping.comparable_fields[idx]._excluded = !checked;
  }
}

// ==================== Compare ====================

var allDiffRows = [];
var currentFilter = 'all';
var currentPage = 0;
var pageSize = 250;
var searchTerm = '';

async function runCompare() {
  showTab(3);
  showCompareProgress('Running 3-way comparison\u2026', 'Analyzing field values across all sources');

  // Gather options from the UI
  var options = {
    trim_whitespace: document.getElementById('optTrim').checked,
    case_sensitive: !document.getElementById('optCaseInsensitive').checked,
    skip_yellow: document.getElementById('optSkipYellow').checked,
    skip_ten_digit: document.getElementById('optSkipTenDigit').checked,
    skip_strike: document.getElementById('optSkipStrike').checked
  };

  // Gather active sources
  var activeSources = [];
  if (document.getElementById('srcCOA').checked) activeSources.push('COA');
  if (document.getElementById('srcFAQ').checked) activeSources.push('FAQ');
  if (document.getElementById('srcDP').checked) activeSources.push('DataPool');

  // Gather key columns
  var keyColumns = {};
  ['COA', 'FAQ', 'DataPool'].forEach(function(src) {
    var sel = document.getElementById('key-' + src);
    if (sel) keyColumns[src] = sel.value;
  });

  // Gather excluded fields
  var excludedFields = [];
  if (currentMapping && currentMapping.comparable_fields) {
    currentMapping.comparable_fields.forEach(function(f, idx) {
      if (f._excluded) excludedFields.push(idx);
    });
  }

  try {
    var res = await fetch('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: activeMode,
        options: options,
        active_sources: activeSources,
        key_columns: keyColumns,
        excluded_fields: excludedFields
      })
    });
    var data = await res.json();
    hideCompareProgress();

    if (data.error) {
      document.getElementById('diffTable').innerHTML = '<p class="error">' + data.error + '</p>';
    } else {
      allDiffRows = data.rows || [];
      comparableFields = data.comparable_fields || [];
      currentFilter = 'all';
      currentPage = 0;
      if (typeof refreshWelcomeChips === 'function') refreshWelcomeChips();
      renderSummary(data.summary);
      renderTransforms(data.active_transforms || []);
      renderFilteredTable();
    }
  } catch(e) {
    hideCompareProgress();
    document.getElementById('diffTable').innerHTML = '<p class="error">Comparison failed: ' + e.message + '</p>';
  }
}

function renderSummary(summary) {
  var el = document.getElementById('summaryBar');
  var matchPct = summary.total > 0 ? ((summary.same / summary.total) * 100).toFixed(1) : '0.0';
  var fieldCount = comparableFields.length;
  el.innerHTML = [
    '<span class="sum-chip clickable" onclick="applyFilter(\'all\')"><strong>' + summary.total.toLocaleString() + '</strong> Total</span>',
    '<span class="sum-chip match clickable" onclick="applyFilter(\'same\')">' + summary.same.toLocaleString() + ' \u2713 Match</span>',
    '<span class="sum-chip conflict clickable" onclick="applyFilter(\'conflict\')">' + summary.conflict.toLocaleString() + ' \u26a0 Conflicts</span>',
    '<span class="sum-chip clickable" onclick="applyFilter(\'onlyCOA\')">' + (summary.onlyCOA || 0) + ' COA only</span>',
    '<span class="sum-chip clickable" onclick="applyFilter(\'onlyFAQ\')">' + (summary.onlyFAQ || 0) + ' FAQ only</span>',
    '<span class="sum-chip clickable" onclick="applyFilter(\'onlyDP\')">' + (summary.onlyDP || 0) + ' DP only</span>',
    '<span class="sum-chip pct">' + matchPct + '% Match rate</span>',
    '<span class="sum-chip">' + fieldCount + ' Fields</span>',
  ].join('');
  // Update sub-tab badge
  document.getElementById('resultsBadge').textContent = summary.total.toLocaleString();
}

function applyFilter(filter) {
  currentFilter = filter;
  currentPage = 0;
  // Map all possible filter names to the actual button IDs in index.html
  var filterMap = {
    'all': 'fAll',
    'conflict': 'fConflict', 'conflicts': 'fConflict',
    'same': 'fSame', 'identical': 'fSame',
    'onlyCOA': 'fOnlyCOA', 'only_COA': 'fOnlyCOA',
    'onlyFAQ': 'fOnlyFAQ', 'only_FAQ': 'fOnlyFAQ',
    'onlyDP': 'fOnlyDP', 'only_DataPool': 'fOnlyDP'
  };
  document.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  var btnId = filterMap[filter] || 'fAll';
  var btn = document.getElementById(btnId);
  if (btn) btn.classList.add('active');
  else {
    var fallback = document.getElementById('fAll');
    if (fallback) fallback.classList.add('active');
  }
  renderFilteredTable();
  // Switch to results sub-tab if not already there
  showSubTab('results');
}

function onRowSearch(val) {
  searchTerm = val.toLowerCase().trim();
  currentPage = 0;
  renderFilteredTable();
}

function filterByKey(key) {
  // Called from agent chat when user clicks a key link
  // 1. Set the search term to the key value
  var searchInput = document.getElementById('rowSearch');
  if (searchInput) searchInput.value = key;
  searchTerm = key.toLowerCase().trim();
  currentPage = 0;

  // 2. Reset filter to show all row types (so the key is visible regardless of type)
  currentFilter = 'all';
  document.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  var allBtn = document.getElementById('fAll');
  if (allBtn) allBtn.classList.add('active');

  // 3. Switch to results sub-tab and render
  showSubTab('results');
  renderFilteredTable();

  // 4. Scroll to the results area
  var diffTable = document.getElementById('diffTable');
  if (diffTable) diffTable.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function getFilteredRows() {
  var rows = allDiffRows;
  if (currentFilter !== 'all') {
    rows = rows.filter(function(r) {
      if (currentFilter === 'conflict' || currentFilter === 'conflicts') return r.dtype === 'conflict';
      if (currentFilter === 'same' || currentFilter === 'identical') return r.dtype === 'same';
      if (currentFilter === 'only_COA' || currentFilter === 'onlyCOA') return r.dtype === 'only_COA' || r.dtype === 'onlyCOA';
      if (currentFilter === 'only_FAQ' || currentFilter === 'onlyFAQ') return r.dtype === 'only_FAQ' || r.dtype === 'onlyFAQ';
      if (currentFilter === 'only_DataPool' || currentFilter === 'onlyDP') return r.dtype === 'only_DataPool' || r.dtype === 'onlyDP';
      return r.dtype === currentFilter;
    });
  }
  if (searchTerm) {
    rows = rows.filter(function(r) {
      // Search by key
      if ((r.key || '').toLowerCase().indexOf(searchTerm) >= 0) return true;
      // Search by value across all fields
      var vals = r.vals || {};
      for (var fk in vals) {
        var fv = vals[fk];
        for (var src in fv) {
          if ((fv[src] || '').toLowerCase().indexOf(searchTerm) >= 0) return true;
        }
      }
      return false;
    });
  }
  return rows;
}

function renderFilteredTable(keepPage) {
  var rows = getFilteredRows();
  // Progressive loading: show up to (currentPage+1)*pageSize rows
  if (!keepPage) {
    currentPage = 0;
  }
  displayedRows = Math.min((currentPage + 1) * pageSize, rows.length);
  renderDiffTable(rows.slice(0, displayedRows));
  renderPagination(rows.length);
  // Update diff title and column info
  var visCount = comparableFields.filter(function(f) { return !f._hidden; }).length;
  var totalCount = comparableFields.length;
  document.getElementById('diffTitle').textContent = 'Diff Results \u2014 ' + rows.length.toLocaleString() + ' rows (' + totalCount + ' fields)';
  document.getElementById('colInfo').innerHTML = '<span class="col-picker"><button class="col-picker-btn" onclick="toggleColPicker()">Columns ' + visCount + '/' + totalCount + ' \u25be</button><div class="col-picker-menu" id="colPickerMenu"></div></span> \u2022 ' + rows.length.toLocaleString() + ' rows - ' + totalCount + ' cols';
}

function showSubTab(name) {
  ['settings','transforms','results'].forEach(function(t) {
    var panel = document.getElementById('panel-' + t);
    var btn = document.getElementById('subtab-' + t);
    if (panel) panel.style.display = (t === name) ? '' : 'none';
    if (btn) btn.classList.toggle('active', t === name);
  });
}

var comparableFields = []; // populated from API response

function renderDiffTable(rows) {
  if (!rows || !rows.length) {
    document.getElementById('diffTable').innerHTML = '<p style="padding:20px;color:var(--text3);">No rows match the current filter.</p>';
    return;
  }

  // Build header: Source | Key | Field1 | Field2 | ...
  var fields = comparableFields.filter(function(f) { return !f._hidden; });
  var html = '<div class="diff-scroll"><table class="diff-table"><thead><tr>';
  html += '<th class="th-src">Source</th><th class="th-key">Key</th>';
  fields.forEach(function(f, idx) {
    html += '<th class="th-field" title="' + f.label + '">';
    html += '<span>' + f.label + '</span>';
    html += '<span class="th-actions">';
    html += '<button class="th-btn th-btn-tx" onclick="event.stopPropagation();openTransformForCol(' + idx + ')" title="Transform">&#9889;</button>';
    html += '<button class="th-btn th-btn-rm" onclick="event.stopPropagation();hideColumn(' + idx + ')" title="Remove column">&times;</button>';
    html += '</span></th>';
  });
  html += '</tr></thead><tbody>';

  rows.forEach(function(r) {
    var vals = r.vals || {};
    var fieldConflicts = r.field_conflicts || [];

    if (r.dtype === 'same') {
      // Single row showing the matching value from any source
      html += '<tr class="r-same">';
      html += '<td class="td-src src-ok">\u2713 Match</td>';
      html += '<td class="td-key">' + esc(r.key || '') + '</td>';
      fields.forEach(function(f) {
        var fv = vals[f.canonical] || {};
        var v = fv.COA || fv.FAQ || fv.DataPool || '';
        html += '<td title="' + esc(v) + '">' + esc(v) + '</td>';
      });
      html += '</tr>';

    } else if (r.dtype === 'conflict') {
      // Multiple rows — one per active source (like the original HTML app)
      var sources = ['COA', 'FAQ', 'DataPool'];
      var activeSrcs = sources.filter(function(s) {
        return Object.keys(vals).some(function(fk) { return vals[fk] && vals[fk][s] !== undefined && vals[fk][s] !== null; });
      });
      if (activeSrcs.length === 0) activeSrcs = sources;

      activeSrcs.forEach(function(src, si) {
        html += '<tr class="r-conflict r-' + src.toLowerCase() + '">';
        html += '<td class="td-src src-' + src.toLowerCase() + '">' + srcLabel(src) + '</td>';
        if (si === 0) {
          html += '<td class="td-key" rowspan="' + activeSrcs.length + '">' + esc(r.key || '') + '</td>';
        }
        fields.forEach(function(f, fi) {
          var fv = vals[f.canonical] || {};
          var v = fv[src];
          var cellCls = '';
          if (v === undefined || v === null) {
            cellCls = 'c-na';
            v = '\u2014';
          } else if (fieldConflicts[fi]) {
            // Smart coloring: find majority value for this field
            var allVals = activeSrcs.map(function(s2) { return (fv[s2] || '').toString().toLowerCase().trim(); });
            var valCounts = {};
            allVals.forEach(function(av) { if (av) valCounts[av] = (valCounts[av] || 0) + 1; });
            var myVal = (v || '').toString().toLowerCase().trim();
            var maxCount = Math.max.apply(null, Object.values(valCounts));
            if (valCounts[myVal] === maxCount && maxCount > 1) {
              cellCls = 'c-agree'; // consensus — green
            } else {
              cellCls = 'c-diff'; // outlier — salmon
            }
          }
          html += '<td class="' + cellCls + '" title="' + esc(v) + '">' + esc(v) + '</td>';
        });
        html += '</tr>';
      });
      // Separator row after conflict group
      html += '<tr class="r-sep"><td colspan="' + (fields.length + 2) + '"></td></tr>';

    } else {
      // only_COA, only_FAQ, only_DataPool
      var onlySrc = r.dtype.replace('only_', '');
      html += '<tr class="r-only r-' + onlySrc.toLowerCase() + '">';
      html += '<td class="td-src src-' + onlySrc.toLowerCase() + '">' + srcLabel(onlySrc) + ' Only</td>';
      html += '<td class="td-key">' + esc(r.key || '') + '</td>';
      fields.forEach(function(f) {
        var fv = vals[f.canonical] || {};
        var v = fv[onlySrc] || '';
        html += '<td title="' + esc(v) + '">' + esc(v) + '</td>';
      });
      html += '</tr>';
    }
  });

  html += '</tbody></table></div>';
  document.getElementById('diffTable').innerHTML = html;
}

function srcLabel(src) {
  var labels = {COA: 'COA Side', FAQ: 'FAQ (SAP)', DataPool: 'DataPool'};
  return labels[src] || src;
}

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

var displayedRows = 0; // Track how many rows are currently shown

function renderPagination(totalRows) {
  var el = document.getElementById('pagination');
  if (displayedRows >= totalRows) {
    el.innerHTML = '<div class="load-info">Showing all ' + totalRows.toLocaleString() + ' rows</div>';
    return;
  }
  var html = '<div class="load-info">Showing ' + displayedRows.toLocaleString() + ' of ' + totalRows.toLocaleString() + ' rows</div>';
  html += '<button class="load-more-btn" onclick="loadMoreRows()">Load next 250 rows</button>';
  el.innerHTML = html;
}

function loadMoreRows() {
  currentPage++;
  renderFilteredTable(true); // keep current page
}

// ==================== Export ====================

async function exportCSV() {
  const res = await fetch('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format: 'csv', conflicts_only: false })
  });
  const data = await res.json();
  if (data.data) {
    const blob = new Blob([JSON.stringify(data.data)], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'rdm_diff_results.csv';
    a.click();
  }
}

async function exportExcel() {
  // TODO: Implement server-side Excel export
  alert('Excel export coming soon \u2014 use CSV for now.');
}

// ==================== Transforms ====================

var activeTransforms = [];

function renderTransforms(transforms) {
  activeTransforms = transforms || [];
  var el = document.getElementById('transformsTable');
  var badge = document.getElementById('txBadge');
  var countBadge = document.getElementById('txCountBadge');
  badge.textContent = transforms.length;
  if (countBadge) countBadge.textContent = '(' + transforms.length + ')';
  if (!transforms.length) {
    el.innerHTML = '<p style="padding:16px;color:var(--text3);font-size:12px;">No active transforms.</p>';
    return;
  }
  var html = '<table class="tx-table"><thead><tr><th>Field</th><th>Source</th><th>Instruction</th><th>Actions</th></tr></thead><tbody>';
  transforms.forEach(function(tx, idx) {
    var srcCls = 'tx-src-' + tx.source.toLowerCase();
    var isCustom = tx.is_custom ? ' (custom)' : '';
    html += '<tr>';
    html += '<td class="tx-field">' + esc(tx.field) + '</td>';
    html += '<td><span class="tx-src ' + srcCls + '">' + tx.source + '</span></td>';
    html += '<td class="tx-instr">' + esc(tx.instruction) + isCustom + '</td>';
    html += '<td class="tx-actions">';
    html += '<button class="tx-btn-edit" onclick="editTransform(' + idx + ')">Edit</button>';
    html += '<button class="tx-btn-remove" onclick="removeTransform(' + idx + ')">&times; Remove</button>';
    html += '</td>';
    html += '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

function editTransform(idx) {
  var tx = activeTransforms[idx];
  if (!tx) return;

  // Find the field in comparableFields
  var field = comparableFields.find(function(f) { return f.canonical === tx.field; });
  if (!field) {
    // Create a temporary field reference for the panel
    field = { canonical: tx.field, label: tx.field };
  }

  txPanelField = field;
  txPanelSource = tx.source;
  txPanelFunction = '';

  // Populate the panel with existing transform data
  document.getElementById('txPanelTitle').textContent = 'Transform: ' + field.label;
  document.getElementById('txPanelSub').textContent = 'Transform active \u2014 modify instruction and Preview to update';
  document.getElementById('txInstruction').value = tx.instruction || '';
  document.getElementById('txSamples').innerHTML = '<p style="color:var(--text3);font-size:11px;padding:12px;">Click Preview to see updated transformed values</p>';
  document.getElementById('txFunction').style.display = 'none';

  // Set source button
  setTxSource(tx.source);

  // Show overlay
  document.getElementById('txPanelOverlay').classList.remove('hidden');
}

function removeTransform(idx) {
  var tx = activeTransforms[idx];
  if (!tx) return;

  // Call backend to remove this transform
  fetch('/api/transform/remove', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      field: tx.field,
      source: tx.source,
      is_custom: !!tx.is_custom
    })
  }).then(function(res) { return res.json(); })
    .then(function(data) {
      if (data.error) { alert(data.error); return; }
      // Remove from local list and re-render
      activeTransforms.splice(idx, 1);
      renderTransforms(activeTransforms);
    })
    .catch(function(e) { alert('Remove failed: ' + e.message); });
}

// ==================== Export, Fullscreen, Column Picker ====================

function toggleExportMenu() {
  var menu = document.getElementById('exportMenu');
  menu.classList.toggle('show');
  // Close on outside click
  setTimeout(function() {
    document.addEventListener('click', function close(e) {
      if (!e.target.closest('#exportDropdown')) {
        menu.classList.remove('show');
        document.removeEventListener('click', close);
      }
    });
  }, 10);
}

function exportData(type) {
  document.getElementById('exportMenu').classList.remove('show');
  if (type === 'csv_all') {
    exportAsFile('csv', false);
  } else if (type === 'excel_all') {
    exportAsFile('excel', false);
  } else if (type === 'excel_conflicts') {
    exportAsFile('excel', true);
  }
}

function toggleFullscreen() {
  var card = document.getElementById('tab-panel-3').querySelector('.card');
  var isFs = card.classList.toggle('fullscreen');
  // Show/hide exit button
  var existing = card.querySelector('.exit-fs-btn');
  if (isFs) {
    if (!existing) {
      var btn = document.createElement('button');
      btn.className = 'exit-fs-btn';
      btn.textContent = '\u25a3 Exit Fullscreen';
      btn.onclick = toggleFullscreen;
      card.appendChild(btn);
    }
    // Make sure results panel is visible
    showSubTab('results');
  } else {
    if (existing) existing.remove();
  }
}

function clearResults() {
  allDiffRows = [];
  comparableFields = [];
  document.getElementById('diffTable').innerHTML = '<p style="padding:20px;color:var(--text3);">No comparison yet. Upload files and run comparison.</p>';
  document.getElementById('summaryBar').innerHTML = '';
  document.getElementById('diffTitle').textContent = 'No comparison yet';
  document.getElementById('pagination').innerHTML = '';
  document.getElementById('colInfo').textContent = '';
}

// ==================== Column Hide/Show ====================

function hideColumn(idx) {
  var visibleFields = comparableFields.filter(function(f) { return !f._hidden; });
  if (visibleFields[idx]) {
    visibleFields[idx]._hidden = true;
    renderFilteredTable();
  }
}

function showAllColumns() {
  comparableFields.forEach(function(f) { f._hidden = false; });
  renderFilteredTable();
  var menu = document.getElementById('colPickerMenu');
  if (menu) menu.classList.remove('show');
}

function toggleColPicker() {
  var menu = document.getElementById('colPickerMenu');
  if (!menu) return;
  menu.classList.toggle('show');
  if (menu.classList.contains('show')) {
    renderColPickerItems();
    setTimeout(function() {
      document.addEventListener('click', closeColPicker);
    }, 10);
  }
}

function closeColPicker(e) {
  if (!e.target.closest('.col-picker')) {
    var menu = document.getElementById('colPickerMenu');
    if (menu) menu.classList.remove('show');
    document.removeEventListener('click', closeColPicker);
  }
}

function renderColPickerItems() {
  var menu = document.getElementById('colPickerMenu');
  if (!menu) return;
  var html = '<label style="padding:6px 12px;border-bottom:1px solid var(--border);font-weight:600;"><a onclick="showAllColumns()" style="font-size:10px;color:var(--blue);cursor:pointer;">Show all</a></label>';
  comparableFields.forEach(function(f, idx) {
    var checked = f._hidden ? '' : ' checked';
    html += '<label><input type="checkbox"' + checked + ' onchange="toggleColVisibility(' + idx + ', this.checked)"> ' + esc(f.label) + '</label>';
  });
  menu.innerHTML = html;
}

function toggleColVisibility(idx, visible) {
  if (comparableFields[idx]) {
    comparableFields[idx]._hidden = !visible;
    renderFilteredTable();
  }
}

// ==================== Transform Panel ====================

var txPanelField = null; // current field being transformed
var txPanelSource = 'FAQ'; // selected source
var txPanelFunction = ''; // generated function code

function openTransformForCol(idx) {
  var visibleFields = comparableFields.filter(function(f) { return !f._hidden; });
  var f = visibleFields[idx];
  if (!f) return;

  txPanelField = f;
  txPanelSource = 'FAQ';
  txPanelFunction = '';

  document.getElementById('txPanelTitle').textContent = 'Transform: ' + f.label;
  document.getElementById('txPanelSub').textContent = 'Enter an instruction and Preview to generate';
  document.getElementById('txInstruction').value = '';
  document.getElementById('txSamples').innerHTML = '<p style="color:var(--text3);font-size:11px;padding:12px;">Click Preview to see transformed values</p>';
  document.getElementById('txFunction').style.display = 'none';

  // Set source buttons
  setTxSource('FAQ');

  // Show overlay
  document.getElementById('txPanelOverlay').classList.remove('hidden');
}

function closeTxPanel() {
  document.getElementById('txPanelOverlay').classList.add('hidden');
  txPanelField = null;
}

function setTxSource(src) {
  txPanelSource = src;
  document.querySelectorAll('.tx-src-btn').forEach(function(b) {
    b.classList.toggle('active', b.textContent.trim() === src || (src === 'All' && b.textContent.trim() === 'All Sources'));
  });
}

function setTxPill(instruction) {
  document.getElementById('txInstruction').value = instruction;
}

async function previewTransform() {
  if (!txPanelField) return;
  var instruction = document.getElementById('txInstruction').value.trim();
  if (!instruction) { alert('Enter an instruction first'); return; }

  document.getElementById('txSamples').innerHTML = '<p style="color:var(--text3);font-size:11px;padding:12px;">Generating preview...</p>';

  try {
    var res = await fetch('/api/transform/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        field: txPanelField.canonical,
        source: txPanelSource,
        instruction: instruction
      })
    });
    var data = await res.json();
    if (data.error) {
      document.getElementById('txSamples').innerHTML = '<p style="color:#dc2626;font-size:11px;padding:12px;">' + data.error + '</p>';
      return;
    }

    // Render sample values table
    var samples = data.samples || [];
    var html = '<table><thead><tr><th>Original</th><th class="tx-arrow"></th><th>Transformed</th></tr></thead><tbody>';
    samples.forEach(function(s) {
      html += '<tr><td>' + esc(s.original) + '</td><td class="tx-arrow">&rarr;</td><td class="tx-val-new">' + esc(s.transformed) + '</td></tr>';
    });
    html += '</tbody></table>';
    document.getElementById('txSamples').innerHTML = html;

    // Show generated function
    txPanelFunction = data.function_code || '';
    if (txPanelFunction) {
      document.getElementById('txFunction').textContent = txPanelFunction;
      document.getElementById('txFunction').style.display = 'block';
    }

    document.getElementById('txPanelSub').textContent = 'Transform active \u2014 modify instruction and Preview to update';
  } catch(e) {
    document.getElementById('txSamples').innerHTML = '<p style="color:#dc2626;font-size:11px;padding:12px;">Error: ' + e.message + '</p>';
  }
}

async function applyTransform() {
  if (!txPanelField || !txPanelFunction) {
    alert('Preview a transform first');
    return;
  }
  var instruction = document.getElementById('txInstruction').value.trim();

  try {
    var res = await fetch('/api/transform/apply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        field: txPanelField.canonical,
        source: txPanelSource,
        instruction: instruction,
        function_code: txPanelFunction
      })
    });
    var data = await res.json();
    if (data.error) { alert(data.error); return; }

    closeTxPanel();
    // Re-run comparison to apply new transform
    runCompare();
  } catch(e) {
    alert('Apply failed: ' + e.message);
  }
}

function clearTransform() {
  if (!txPanelField) return;
  fetch('/api/transform/clear', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ field: txPanelField.canonical, source: txPanelSource })
  }).then(function() {
    document.getElementById('txInstruction').value = '';
    document.getElementById('txSamples').innerHTML = '<p style="color:var(--text3);font-size:11px;padding:12px;">Transform cleared</p>';
    document.getElementById('txFunction').style.display = 'none';
    document.getElementById('txPanelSub').textContent = 'Enter an instruction and Preview to generate';
    txPanelFunction = '';
  });
}

// ==================== Export (Excel / CSV) ====================

async function exportAsFile(format, conflictsOnly) {
  try {
    var res = await fetch('/api/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({format: format, conflicts_only: !!conflictsOnly})
    });
    if (res.ok) {
      var blob = await res.blob();
      var ext = format === 'excel' ? '.xlsx' : '.csv';
      var name = conflictsOnly ? 'rdm_conflicts' + ext : 'rdm_diff_results' + ext;
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
    } else {
      var err = await res.json();
      alert('Export error: ' + (err.error || 'Unknown'));
    }
  } catch(e) {
    alert('Export failed: ' + e.message);
  }
}

// ==================== Compare Progress ====================

function showCompareProgress(title, sub) {
  var overlay = document.getElementById('fileLoadOverlay');
  document.getElementById('fileLoadSrc').textContent = 'COMPARE';
  document.getElementById('fileLoadTitle').textContent = title || 'Running comparison\u2026';
  document.getElementById('fileLoadSub').textContent = sub || '';
  document.getElementById('fileLoadBar').style.width = '0%';
  document.getElementById('fileLoadHint').textContent = 'This may take a moment for large datasets.';
  overlay.classList.remove('hidden');
  // Animate the progress bar indeterminate-style
  var bar = document.getElementById('fileLoadBar');
  bar.style.transition = 'width 30s ease-out';
  bar.style.width = '85%';
}

function hideCompareProgress() {
  var bar = document.getElementById('fileLoadBar');
  bar.style.transition = 'width 0.2s';
  bar.style.width = '100%';
  setTimeout(function() {
    document.getElementById('fileLoadOverlay').classList.add('hidden');
    bar.style.transition = 'width 0.2s ease';
  }, 300);
}

// ==================== Session Cache (IndexedDB) ====================
// Caches uploaded source data in browser so you don't re-upload after redeploy

var _cacheDB = null;
var _CACHE_DB_NAME = 'rdm_session_cache';
var _CACHE_STORE = 'sources';

function openCacheDB() {
  return new Promise(function(resolve, reject) {
    if (_cacheDB) { resolve(_cacheDB); return; }
    var req = indexedDB.open(_CACHE_DB_NAME, 1);
    req.onupgradeneeded = function(e) { e.target.result.createObjectStore(_CACHE_STORE); };
    req.onsuccess = function(e) { _cacheDB = e.target.result; resolve(_cacheDB); };
    req.onerror = function() { reject('IndexedDB error'); };
  });
}

function cacheSessionData() {
  // Save all uploaded source data to IndexedDB after successful upload
  if (!uploadedSources || Object.keys(uploadedSources).length === 0) return;
  openCacheDB().then(function(db) {
    var tx = db.transaction(_CACHE_STORE, 'readwrite');
    var store = tx.objectStore(_CACHE_STORE);
    store.put(JSON.parse(JSON.stringify(uploadedSources)), 'latest');
    store.put(new Date().toISOString(), 'timestamp');
    store.put(activeMode, 'mode');
    console.log('[RDM] Session cached to IndexedDB');
  }).catch(function(e) { console.warn('[RDM] Cache save failed:', e); });
}

function checkCachedSession() {
  // On page load, check if we have cached data and show restore button
  openCacheDB().then(function(db) {
    var tx = db.transaction(_CACHE_STORE, 'readonly');
    var store = tx.objectStore(_CACHE_STORE);
    var req = store.get('latest');
    req.onsuccess = function() {
      if (req.result && Object.keys(req.result).length > 0) {
        var tsReq = store.get('timestamp');
        tsReq.onsuccess = function() {
          showRestoreBanner(req.result, tsReq.result);
        };
      }
    };
  }).catch(function() {});
}

function showRestoreBanner(cachedSources, timestamp) {
  var names = Object.keys(cachedSources).map(function(k) {
    return k + ' (' + (cachedSources[k].filename || 'unknown') + ')';
  }).join(', ');
  var timeStr = timestamp ? new Date(timestamp).toLocaleString() : 'unknown';
  var banner = document.createElement('div');
  banner.id = 'restoreBanner';
  banner.className = 'restore-banner';
  banner.innerHTML = '<span>\uD83D\uDCBE Previous session found: ' + names + ' (' + timeStr + ')</span>'
    + '<button class="restore-btn" onclick="restoreCachedSession()">Restore Session</button>'
    + '<button class="restore-dismiss" onclick="this.parentElement.remove()">\u2715</button>';
  var canvas = document.querySelector('.canvas');
  if (canvas) canvas.insertBefore(banner, canvas.firstChild);
}

function restoreCachedSession() {
  openCacheDB().then(function(db) {
    var tx = db.transaction(_CACHE_STORE, 'readonly');
    var store = tx.objectStore(_CACHE_STORE);
    var req = store.get('latest');
    req.onsuccess = function() {
      if (!req.result) return;
      var cached = req.result;
      // Send to backend /api/restore
      fetch('/api/restore', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sources: cached})
      }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.error) { alert('Restore failed: ' + data.error); return; }
        // Update local state
        uploadedSources = cached;
        // Mark upload zones as done
        Object.keys(cached).forEach(function(src) {
          var zone = document.getElementById('zone-' + src);
          if (zone) {
            zone.classList.add('uploaded');
            zone.innerHTML = '<div class="upload-done">\u2705 ' + (cached[src].filename || src) + '</div>'
              + '<div class="upload-meta">' + (cached[src].row_count || cached[src].rows.length) + ' rows restored from cache</div>';
          }
        });
        // Remove banner
        var banner = document.getElementById('restoreBanner');
        if (banner) banner.remove();
        // Enable next step
        showTab(2);
        goToMapping();
      }).catch(function(e) { alert('Restore error: ' + e); });
    };
  });
}

// ==================== Init ====================

// Check for cached session on page load
checkCachedSession();

// Make upload zones clickable
document.querySelectorAll('.upload-zone').forEach(zone => {
  zone.addEventListener('click', function() {
    const input = this.querySelector('input[type=file]');
    if (input) input.click();
  });
});

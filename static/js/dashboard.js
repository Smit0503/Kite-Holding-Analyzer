const currentSortState = [];

function formatSigned(value) {
  const number = Number(value || 0);
  const sign = number > 0 ? '+' : number < 0 ? '-' : '';
  return `${sign}${Math.abs(number).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatMoney(value, digits = 0) {
  const number = Number(value || 0);
  return `₹${number.toLocaleString('en-IN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function formatSignedMoney(value, digits = 0) {
  const number = Number(value || 0);
  const sign = number > 0 ? '+' : number < 0 ? '-' : '';
  const absoluteValue = Math.abs(number);
  return `₹${sign}${absoluteValue.toLocaleString('en-IN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function safeAssetType(assetType) {
  const normalized = String(assetType || '').trim().toLowerCase();
  if (!normalized || normalized === 'n/a' || normalized === 'na') return 'equity';
  if (normalized.includes('mf') || normalized.includes('mutual')) return 'mf';
  if (normalized.includes('debt') || normalized.includes('bond')) return 'debt';
  if (normalized.includes('metal') || normalized.includes('gold')) return 'metal';
  if (normalized.includes('equity') || normalized.includes('stock')) return 'equity';
  return normalized;
}

function setTextIfPresent(id, value) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = value;
  }
}

function setRefreshStatus(message, type = 'info') {
  const el = document.getElementById('refresh-status');
  if (!el) return;
  el.textContent = message;
  el.classList.remove('is-success', 'is-error');
  if (type === 'success') el.classList.add('is-success');
  if (type === 'error') el.classList.add('is-error');
}

function getSortEntry(key) {
  return currentSortState.find((entry) => entry.key === key) || null;
}

function toggleSort(key) {
  const existingIndex = currentSortState.findIndex((entry) => entry.key === key);
  if (existingIndex === -1) {
    currentSortState.unshift({ key, direction: 'asc' });
    return;
  }

  const existing = currentSortState[existingIndex];
  if (existing.direction === 'asc') {
    existing.direction = 'desc';
  } else if (existing.direction === 'desc') {
    currentSortState.splice(existingIndex, 1);
  } else {
    existing.direction = 'asc';
  }
}

function getCellSortValue(row, key) {
  const rawValue = row.getAttribute(`data-${key.replace(/_/g, '-')}`) ?? '';

  if (key === 'symbol' || key === 'asset') {
    return String(rawValue || '').trim().toUpperCase();
  }

  return Number(rawValue || 0);
}

function compareSortValues(leftValue, rightValue) {
  if (leftValue === rightValue) return 0;
  if (leftValue == null || Number.isNaN(leftValue)) return 1;
  if (rightValue == null || Number.isNaN(rightValue)) return -1;

  if (typeof leftValue === 'string' && typeof rightValue === 'string') {
    return leftValue.localeCompare(rightValue, 'en', { numeric: true, sensitivity: 'base' });
  }

  return Number(leftValue) > Number(rightValue) ? 1 : -1;
}

function sortTableRows() {
  const tbody = document.querySelector('.table-wrap tbody');
  if (!tbody) return;

  const rows = Array.from(tbody.querySelectorAll('tr[data-sort-row="true"]'));
  if (!rows.length) return;

  rows.sort((rowA, rowB) => {
    for (const entry of currentSortState) {
      const comparison = compareSortValues(
        getCellSortValue(rowA, entry.key),
        getCellSortValue(rowB, entry.key)
      );

      if (comparison !== 0) {
        return comparison * (entry.direction === 'asc' ? 1 : -1);
      }
    }

    return 0;
  });

  rows.forEach((row) => tbody.appendChild(row));
}

function bindSymbolActions() {
  document.querySelectorAll('.symbol-options-button').forEach((button) => {
    button.onclick = async (event) => {
      event.stopPropagation();
      const symbol = button.dataset.symbol;
      if (!symbol) return;
      const existingMenu = button.querySelector('.symbol-menu');
      if (existingMenu) {
        existingMenu.classList.toggle('is-open');
        return;
      }
      const menu = createSymbolMenu(symbol, button);
      menu.classList.add('is-open');
      document.querySelectorAll('.symbol-menu.is-open').forEach((openMenu) => {
        if (openMenu !== menu) openMenu.classList.remove('is-open');
      });
      try {
        const discrepancy = await getDiscrepancyStatus(symbol);
        if (discrepancy && discrepancy.has_discrepancy) {
          button.classList.add('discrepancy-alert');
          const item = document.createElement('button');
          item.type = 'button';
          item.dataset.action = 'discrepancy';
          item.dataset.symbol = symbol;
          item.classList.add('discrepancy-option');
          item.textContent = `Discrepancy (${Number(discrepancy.quantity || 0).toLocaleString('en-IN')})`;
          item.addEventListener('click', () => {
            menu.classList.remove('is-open');
            openDiscrepancy(symbol, discrepancy.quantity || 0);
          });
          menu.appendChild(item);
        }
      } catch (error) {
        console.warn('Discrepancy check failed:', error);
      }
    };
  });

  document.addEventListener('click', (event) => {
    if (!event.target.closest('.symbol-options-button')) {
      document.querySelectorAll('.symbol-menu.is-open').forEach((menu) => menu.classList.remove('is-open'));
    }
  });
}

async function getDiscrepancyStatus(symbol) {
  try {
    const response = await fetch(`/api/symbol-discrepancy/${encodeURIComponent(symbol)}`);
    if (!response.ok) return null;
    const data = await response.json();
    return data && data.has_discrepancy ? data : null;
  } catch (error) {
    return null;
  }
}

async function syncDiscrepancyAlerts() {
  const buttons = document.querySelectorAll('.symbol-options-button');
  for (const button of buttons) {
    const symbol = button.dataset.symbol;
    if (!symbol) continue;
    try {
      const discrepancy = await getDiscrepancyStatus(symbol);
      button.classList.toggle('discrepancy-alert', Boolean(discrepancy));
    } catch (error) {
      button.classList.remove('discrepancy-alert');
    }
  }
}

function createSymbolMenu(symbol, button) {
  const menu = document.createElement('div');
  menu.className = 'symbol-menu';
  const viewItem = document.createElement('button');
  viewItem.type = 'button';
  viewItem.dataset.action = 'breakdown';
  viewItem.dataset.symbol = symbol;
  viewItem.textContent = 'View breakdown';
  viewItem.addEventListener('click', () => {
    menu.classList.remove('is-open');
    openBreakdown(symbol);
  });
  menu.appendChild(viewItem);
  button.appendChild(menu);
  return menu;
}

function renderSortIndicators() {
  const sortButtons = document.querySelectorAll('.sort-button');
  sortButtons.forEach((button) => {
    const key = button.dataset.sortKey;
    const entry = getSortEntry(key);
    const direction = entry ? entry.direction : null;
    const th = button.closest('th');

    button.classList.toggle('is-active', Boolean(entry));
    if (th) {
      th.setAttribute('aria-sort', direction === 'asc' ? 'ascending' : direction === 'desc' ? 'descending' : 'none');
    }

    const upArrow = button.querySelector('.sort-arrow-up');
    const downArrow = button.querySelector('.sort-arrow-down');
    if (upArrow) upArrow.classList.toggle('is-active', direction === 'asc');
    if (downArrow) downArrow.classList.toggle('is-active', direction === 'desc');
  });
}

function updateHoldings(data) {
  const payload = data || {};
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  const aggregate = payload.aggregate_by_asset || {};

  setTextIfPresent('kpi-invested', formatMoney(payload.invested_value, 0));
  setTextIfPresent('kpi-portfolio-value', formatMoney(payload.portfolio_value, 0));

  const pnlEl = document.getElementById('kpi-total-pnl');
  if (pnlEl) {
    const totalPnl = Number(payload.total_pnl || 0);
    pnlEl.textContent = formatSignedMoney(totalPnl, 0);
    pnlEl.classList.toggle('positive', totalPnl >= 0);
    pnlEl.classList.toggle('negative', totalPnl < 0);
  }

  const pctEl = document.getElementById('kpi-total-profit-pct');
  if (pctEl) {
    const dayPnl = Number(payload.day_pnl || 0);
    pctEl.textContent = formatSignedMoney(dayPnl, 0);
    pctEl.classList.toggle('positive', dayPnl >= 0);
    pctEl.classList.toggle('negative', dayPnl < 0);
  }

  const totalMeta = document.querySelector('.kpi-card-total .meta-lines');
  if (totalMeta) {
    const pct = Number(payload.total_profit_pct || 0);
    const xirr = Number(payload.portfolio_xirr || 0);
    const pctNode = totalMeta.querySelector('.kpi-subvalue');
    const xirrNode = totalMeta.querySelector('.xirr-value');
    if (pctNode) {
      pctNode.textContent = `Chg %: ${formatSigned(pct)}%`;
      pctNode.classList.toggle('positive', pct >= 0);
      pctNode.classList.toggle('negative', pct < 0);
    }
    if (xirrNode) {
      xirrNode.textContent = `XIRR: ${formatSigned(xirr)}%`;
      xirrNode.classList.toggle('positive', xirr >= 0);
      xirrNode.classList.toggle('negative', xirr < 0);
    }
  }

  const assetGrid = document.querySelector('.asset-grid');
  if (assetGrid) {
    assetGrid.innerHTML = '';
    const valid = ['equity', 'debt', 'mf', 'metal'];
    const xirrMap = payload.asset_xirr || {};

    valid.forEach((assetKey) => {
      const agg = aggregate[assetKey] || { invested: 0, current: 0, pnl: 0 };
      const invested = Number(agg.invested || 0);
      const current = Number(agg.current || 0);
      const pnl = Number(agg.pnl || 0);
      const pct = invested ? ((current - invested) / invested) * 100 : 0;
      const xirr = Number(xirrMap[assetKey] || 0);

      assetGrid.insertAdjacentHTML('beforeend', `
        <article class="kpi-card compact">
          <span class="label">${assetKey.toUpperCase()}</span>
          <strong>${formatMoney(current, 0)}</strong>
          <div class="meta-lines">
            <span>Invested: ${formatMoney(invested, 0)}</span>
            <span>P&amp;L: <span class="${pnl >= 0 ? 'positive' : 'negative'}">${formatMoney(pnl, 0)}</span></span>
            <span>Chg %: <span class="${pct >= 0 ? 'positive' : 'negative'}">${formatSigned(pct)}%</span></span>
            <span>XIRR: <span class="${xirr >= 0 ? 'positive' : 'negative'}">${formatSigned(xirr)}%</span></span>
          </div>
        </article>
      `);
    });
  }

  const tbody = document.querySelector('.table-wrap tbody');
  if (tbody) {
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty">No holdings found.</td></tr>';
      renderSortIndicators();
      return;
    }

    tbody.innerHTML = rows.map((row) => {
      const quantity = Number(row.quantity || 0);
      const averagePrice = Number(row.average_price || 0);
      const ltp = Number(row.ltp || 0);
      const invested = quantity * averagePrice;
      const currentValue = quantity * ltp;
      const dayChange = Number(row.day_change || 0);
      const pnlValue = Number(row.pnl || 0);
      const pnlPctValue = invested ? (pnlValue / invested) * 100 : 0;
      const xirrValue = Number(row.xirr || 0);
      const symbol = row.tradingsymbol || row.symbol || 'N/A';
      const assetType = safeAssetType(row.asset_type || 'equity');

      return `
        <tr
          data-sort-row="true"
          data-symbol="${escapeHtml(symbol)}"
          data-asset="${escapeHtml(assetType.toUpperCase())}"
          data-qty="${quantity}"
          data-invested="${invested}"
          data-current-value="${currentValue}"
          data-average-price="${averagePrice}"
          data-ltp="${ltp}"
          data-day-change="${dayChange}"
          data-pnl="${pnlValue}"
          data-pnl-pct="${pnlPctValue}"
          data-xirr="${xirrValue}"
        >
          <td>${escapeHtml(symbol)}</td>
          <td>${escapeHtml(assetType.toUpperCase())}</td>
          <td>${quantity.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</td>
          <td>${formatMoney(invested, 0)}</td>
          <td>${formatMoney(currentValue, 0)}</td>
          <td>${formatMoney(averagePrice, 2)}</td>
          <td>${formatMoney(ltp, 2)}</td>
          <td class="${dayChange > 0 ? 'positive' : dayChange < 0 ? 'negative' : ''}">${formatSigned(dayChange)}</td>
          <td class="${pnlValue > 0 ? 'positive' : pnlValue < 0 ? 'negative' : ''}">
            ${formatSigned(pnlValue)}
            <small class="pnl-change">(${formatSigned(pnlPctValue)}%)</small>
          </td>
          <td class="${xirrValue > 0 ? 'positive' : xirrValue < 0 ? 'negative' : ''}">${formatSigned(xirrValue)}%</td>
          <td><button type="button" class="symbol-options-button" data-symbol="${escapeHtml(symbol)}" aria-label="Open symbol options for ${escapeHtml(symbol)}">⋯</button></td>
        </tr>
      `;
    }).join('');

    sortTableRows();
    bindSymbolActions();
    syncDiscrepancyAlerts();
  }

  const chip = document.querySelector('.chip');
  if (chip) chip.textContent = payload.user_name || 'User';
}

async function refreshData() {
  const refreshButton = document.getElementById('refresh-button');
  if (refreshButton) {
    refreshButton.disabled = true;
    refreshButton.title = 'Refreshing...';
  }

  setRefreshStatus('Refreshing portfolio data…');

  try {
    const activeFilter = window.location.search || '';
    const response = await fetch(`/api/refresh${activeFilter}`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Refresh failed');
    }
    updateHoldings(data);
    setRefreshStatus('Portfolio refreshed successfully.', 'success');
  } catch (error) {
    console.error('Refresh error:', error);
    setRefreshStatus(`Refresh failed: ${error.message}`, 'error');
  } finally {
    if (refreshButton) {
      refreshButton.disabled = false;
      refreshButton.title = 'Refresh holdings';
    }
  }
}

async function refreshTradebookData() {
  const tradebookButton = document.getElementById('refresh-tradebook-button');
  if (tradebookButton) {
    tradebookButton.disabled = true;
    tradebookButton.title = 'Refreshing tradebook...';
  }

  setRefreshStatus('Refreshing tradebook data…');

  try {
    const response = await fetch('/api/refresh-tradebook', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || 'Tradebook refresh failed');
    }
    setRefreshStatus(`Tradebook refreshed successfully (${data.total_rows || 0} rows).`, 'success');
    await refreshData();
  } catch (error) {
    console.error('Tradebook refresh error:', error);
    setRefreshStatus(`Tradebook refresh failed: ${error.message}`, 'error');
  } finally {
    if (tradebookButton) {
      tradebookButton.disabled = false;
      tradebookButton.title = 'Refresh tradebook';
    }
  }
}

async function openBreakdown(symbol) {
  const modal = document.getElementById('breakdown-modal');
  const content = document.getElementById('breakdown-content');
  if (!modal || !content) return;

  content.innerHTML = 'Loading…';
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');

  try {
    const response = await fetch(`/api/symbol-breakdown/${encodeURIComponent(symbol)}`);
    const rawText = await response.text();
    if (!rawText || !rawText.trim()) {
      throw new Error('No breakdown data returned');
    }
    if (!response.ok) {
      throw new Error(rawText.includes('<!doctype') ? 'Unable to load breakdown for this symbol.' : rawText.slice(0, 160));
    }
    const data = JSON.parse(rawText);
    if (!data || !Array.isArray(data.lots)) {
      throw new Error(data?.error || 'Unable to load breakdown');
    }

    const rows = data.lots.map((lot) => {
      const tradeType = lot.type || lot.trade_type || 'BUY';
      const cncStatus = (lot.cnc_status || 'CNC').toUpperCase();
      const tradeTypeDisplay = lot.notes ? `${tradeType} - ${lot.notes}` : tradeType;
      return `
      <tr>
        <td>${escapeHtml(lot.date || 'N/A')}</td>
        <td>${escapeHtml(cncStatus)}</td>
        <td>${escapeHtml(tradeTypeDisplay)}</td>
        <td>${Number(lot.quantity || 0).toLocaleString('en-IN')}</td>
        <td>${formatMoney(lot.price || 0, 2)}</td>
        <td>${formatMoney(lot.ltp || 0, 2)}</td>
        <td class="${(lot.pnl || 0) >= 0 ? 'positive' : 'negative'}">${formatSignedMoney(lot.pnl || 0, 0)}</td>
        <td class="${(lot.chg_pct || 0) >= 0 ? 'positive' : 'negative'}">${formatSigned(Number(lot.chg_pct || 0))}%</td>
        <td class="${(lot.xirr || 0) >= 0 ? 'positive' : 'negative'}">${formatSigned(Number(lot.xirr || 0))}%</td>
      </tr>
    `;
    }).join('');

    content.innerHTML = `
      <div class="summary-row"><strong>${escapeHtml(symbol)}</strong></div>
      <table class="breakdown-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>CNC/MIS</th>
            <th>BUY/SELL</th>
            <th>Qty</th>
            <th>Price</th>
            <th>LTP</th>
            <th>P&amp;L</th>
            <th>Chg %</th>
            <th>XIRR</th>
          </tr>
        </thead>
        <tbody>${rows || '<tr><td colspan="9">No purchase data available.</td></tr>'}</tbody>
      </table>
    `;
  } catch (error) {
    content.innerHTML = `<p class="empty">${escapeHtml(error.message || 'Failed to load breakdown')}</p>`;
  }
}

function openDiscrepancy(symbol, qty = 0) {
  const modal = document.getElementById('discrepancy-modal');
  const symbolField = document.getElementById('discrepancy-symbol');
  if (!modal || !symbolField) return;
  symbolField.value = symbol || '';
  const qtyField = document.querySelector('#discrepancy-form input[name="quantity"]');
  if (qtyField) qtyField.value = Number(qty || 0).toString();
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');
}

function closeBreakdown() {
  const modal = document.getElementById('breakdown-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
}

function closeDiscrepancy() {
  const modal = document.getElementById('discrepancy-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
}

document.addEventListener('DOMContentLoaded', function () {
  const filterForm = document.getElementById('asset-filter-form');
  const filterInputs = filterForm ? Array.from(filterForm.querySelectorAll('input[name="filter"]')) : [];

  filterInputs.forEach((input) => {
    input.addEventListener('change', () => {
      if (input.value === 'all' && input.checked) {
        filterInputs.forEach((other) => {
          if (other !== input) other.checked = false;
        });
      } else if (input.checked) {
        const allInput = filterInputs.find((other) => other.value === 'all');
        if (allInput) allInput.checked = false;
      }
    });
  });

  if (filterForm) {
    filterForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const selected = filterInputs.filter((input) => input.checked).map((input) => input.value);
      const params = new URLSearchParams();
      (selected.length ? selected : ['all']).forEach((value) => params.append('filter', value));
      const query = params.toString();
      window.history.replaceState({}, '', `/holdings?${query}`);
      filterForm.classList.add('is-applying');
      await refreshData();
      filterForm.classList.remove('is-applying');
    });
  }

  document.querySelectorAll('.sort-button').forEach((button) => {
    button.addEventListener('click', () => {
      toggleSort(button.dataset.sortKey);
      renderSortIndicators();
      sortTableRows();
    });
  });

  syncDiscrepancyAlerts();

  document.querySelectorAll('[data-close-modal="true"]').forEach((control) => {
    control.addEventListener('click', () => {
      closeBreakdown();
      closeDiscrepancy();
    });
  });

  const discrepancyForm = document.getElementById('discrepancy-form');
  if (discrepancyForm) {
    discrepancyForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(discrepancyForm);
      const payload = Object.fromEntries(formData.entries());

      try {
        const response = await fetch('/api/discrepancy', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' },
          body: new URLSearchParams(payload).toString(),
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
          throw new Error(data?.error || 'Unable to save discrepancy');
        }
        closeDiscrepancy();
        discrepancyForm.reset();
        setRefreshStatus('Discrepancy saved.', 'success');
      } catch (error) {
        setRefreshStatus(error.message || 'Discrepancy failed to save.', 'error');
      }
    });
  }

  const refreshButton = document.getElementById('refresh-button');
  if (refreshButton) {
    refreshButton.addEventListener('click', refreshData);
  }

  const refreshTradebookButton = document.getElementById('refresh-tradebook-button');
  if (refreshTradebookButton) {
    refreshTradebookButton.addEventListener('click', refreshTradebookData);
  }

  bindSymbolActions();
  renderSortIndicators();
  setRefreshStatus('Loading portfolio data…');
  refreshData();
});
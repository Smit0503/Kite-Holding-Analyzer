function formatSigned(value) {
  const number = Number(value || 0);
  const sign = number > 0 ? '+' : '';
  return `${sign}${Math.abs(number).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatMoney(value, digits = 0) {
  const number = Number(value || 0);
  return `₹${number.toLocaleString('en-IN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
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

function updateHoldings(data) {
  const payload = data || {};
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  const aggregate = payload.aggregate_by_asset || {};

  setTextIfPresent('kpi-invested', formatMoney(payload.invested_value, 0));
  setTextIfPresent('kpi-portfolio-value', formatMoney(payload.portfolio_value, 0));

  const pnlEl = document.getElementById('kpi-total-pnl');
  if (pnlEl) {
    pnlEl.textContent = formatMoney(payload.total_pnl, 0);
    pnlEl.classList.toggle('positive', Number(payload.total_pnl || 0) >= 0);
    pnlEl.classList.toggle('negative', Number(payload.total_pnl || 0) < 0);
  }

  const pctEl = document.getElementById('kpi-total-profit-pct');
  if (pctEl) {
    const pct = Number(payload.total_profit_pct || 0);
    pctEl.textContent = `${formatSigned(pct)}%`;
    pctEl.classList.toggle('positive', pct >= 0);
    pctEl.classList.toggle('negative', pct < 0);
  }

  const assetGrid = document.querySelector('.asset-grid');
  if (assetGrid) {
    assetGrid.innerHTML = '';
    const valid = ['equity', 'debt', 'mf', 'metal'];

    valid.forEach((assetKey) => {
      const agg = aggregate[assetKey] || { invested: 0, current: 0, pnl: 0 };
      const invested = Number(agg.invested || 0);
      const current = Number(agg.current || 0);
      const pnl = Number(agg.pnl || 0);
      const pct = invested ? ((current - invested) / invested) * 100 : 0;

      assetGrid.insertAdjacentHTML('beforeend', `
        <article class="kpi-card compact">
          <span class="label">${assetKey.toUpperCase()}</span>
          <strong>${formatMoney(current, 0)}</strong>
          <div class="meta-lines">
            <span>Invested: ${formatMoney(invested, 0)}</span>
            <span>P&amp;L: <span class="${pnl >= 0 ? 'positive' : 'negative'}">${formatMoney(pnl, 0)}</span></span>
            <span>Profit %: <span class="${pct >= 0 ? 'positive' : 'negative'}">${formatSigned(pct)}%</span></span>
          </div>
        </article>
      `);
    });
  }

  const tbody = document.querySelector('.table-wrap tbody');
  if (tbody) {
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">No holdings found.</td></tr>';
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
      const symbol = row.tradingsymbol || row.symbol || 'N/A';
      const assetType = safeAssetType(row.asset_type || 'equity');

      return `
        <tr>
          <td>${symbol}</td>
          <td>${assetType.toUpperCase()}</td>
          <td>${quantity.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</td>
          <td>${formatMoney(invested, 0)}</td>
          <td>${formatMoney(currentValue, 0)}</td>
          <td>${row.purchase_date || 'N/A'}</td>
          <td>${formatMoney(averagePrice, 2)}</td>
          <td>${formatMoney(ltp, 2)}</td>
          <td class="${dayChange > 0 ? 'positive' : dayChange < 0 ? 'negative' : ''}">${formatSigned(dayChange)}</td>
          <td class="${pnlValue > 0 ? 'positive' : pnlValue < 0 ? 'negative' : ''}">${formatSigned(pnlValue)}</td>
        </tr>
      `;
    }).join('');
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
    const response = await fetch('/api/refresh', { cache: 'no-store' });
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

document.addEventListener('DOMContentLoaded', function () {
  const refreshButton = document.getElementById('refresh-button');
  if (refreshButton) {
    refreshButton.addEventListener('click', refreshData);
  }
  setRefreshStatus('Loading portfolio data…');
  refreshData();
});
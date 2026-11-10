function formatCurrency(value, compact = false) {
  const number = Number(value || 0);
  if (compact && Math.abs(number) >= 10000000) {
    return `₹${(number / 10000000).toFixed(1)}Cr`;
  }
  if (compact && Math.abs(number) >= 100000) {
    return `₹${(number / 100000).toFixed(1)}L`;
  }
  return `₹${number.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function formatSignedCurrency(value) {
  const number = Number(value || 0);
  const sign = number > 0 ? '+' : number < 0 ? '−' : '';
  return `${sign}${formatCurrency(Math.abs(number))}`;
}

function formatSignedPercent(value) {
  const number = Number(value || 0);
  const sign = number > 0 ? '+' : number < 0 ? '−' : '';
  return `${sign}${Math.abs(number).toFixed(2)}%`;
}

function showChartTooltip(event, chartState) {
  const { values, width, left, plotWidth, yPnl, x, includeYear } = chartState;
  const svg = document.getElementById('portfolio-history-chart');
  const wrapper = svg.parentElement;
  const tooltip = document.getElementById('history-chart-tooltip');
  const bounds = svg.getBoundingClientRect();
  const pointerX = ((event.clientX - bounds.left) / bounds.width) * width;
  const index = values.length === 1
    ? 0
    : Math.max(0, Math.min(values.length - 1, Math.round(((pointerX - left) / plotWidth) * (values.length - 1))));
  const point = values[index];
  if (!point || pointerX < left - 12 || pointerX > left + plotWidth + 12) {
    hideChartTooltip();
    return;
  }

  const marker = svg.querySelector('.chart-hover-marker');
  marker.setAttribute('cx', x(index));
  marker.setAttribute('cy', yPnl(point.pnl));
  tooltip.textContent = `${formatDate(point.date, includeYear)} · ${formatSignedCurrency(point.pnl)} · ${formatSignedPercent(point.pct)}`;
  tooltip.hidden = false;

  const wrapperBounds = wrapper.getBoundingClientRect();
  const tooltipLeft = Math.min(
    Math.max(event.clientX - wrapperBounds.left + 12, 0),
    wrapper.clientWidth - tooltip.offsetWidth,
  );
  const tooltipTop = Math.max(event.clientY - wrapperBounds.top - tooltip.offsetHeight - 12, 0);
  tooltip.style.left = `${tooltipLeft}px`;
  tooltip.style.top = `${tooltipTop}px`;
}

function hideChartTooltip() {
  const tooltip = document.getElementById('history-chart-tooltip');
  if (tooltip) tooltip.hidden = true;
}

function svgElement(name, attributes = {}, text = '') {
  const node = document.createElementNS('http://www.w3.org/2000/svg', name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text) node.textContent = text;
  return node;
}

function setStatus(message, isError = false) {
  const status = document.getElementById('history-data-status');
  status.textContent = message;
  status.classList.toggle('is-error', isError);
}

function clearChart(svg) {
  svg.replaceChildren();
}

function renderWarnings(warnings) {
  const container = document.getElementById('history-warnings');
  const list = document.getElementById('history-warning-list');
  list.replaceChildren();
  if (!warnings.length) {
    container.hidden = true;
    return;
  }
  warnings.forEach((warning) => {
    const item = document.createElement('li');
    item.textContent = warning;
    list.appendChild(item);
  });
  container.hidden = false;
}

function formatDate(value, includeYear = false) {
  const options = includeYear
    ? { month: 'short', year: 'numeric' }
    : { day: '2-digit', month: 'short' };
  return new Date(`${value}T00:00:00`).toLocaleDateString('en-IN', options);
}

function renderChart(points) {
  const svg = document.getElementById('portfolio-history-chart');
  hideChartTooltip();
  clearChart(svg);
  const title = svgElement('title', { id: 'history-chart-title' }, 'Portfolio profit and loss history');
  const desc = svgElement('desc', { id: 'history-chart-description' });
  svg.append(title, desc);

  const empty = document.getElementById('history-empty');
  if (!points.length) {
    empty.hidden = false;
    desc.textContent = 'No historical profit and loss values are available for this selection.';
    document.getElementById('history-latest-pnl').textContent = 'Not available';
    document.getElementById('history-latest-pct').textContent = 'Not available';
    document.getElementById('history-date-range').textContent = '';
    return;
  }
  empty.hidden = true;

  const values = points.map((point) => ({
    date: point.date,
    pnl: Number(point.pnl),
    pct: Number(point.pnl_pct),
  }));
  const latest = values[values.length - 1];
  const first = values[0];
  const includeYear = new Date(`${latest.date}T00:00:00`) - new Date(`${first.date}T00:00:00`) > 365 * 24 * 60 * 60 * 1000;
  document.getElementById('history-latest-pnl').textContent = formatSignedCurrency(latest.pnl);
  document.getElementById('history-latest-pct').textContent = formatSignedPercent(latest.pct);
  document.getElementById('history-date-range').textContent = `${formatDate(first.date, includeYear)} to ${formatDate(latest.date, includeYear)}`;
  desc.textContent = `Daily portfolio P&L and return from ${formatDate(first.date, includeYear)} to ${formatDate(latest.date, includeYear)}. Rupee amounts use the left axis and percentages use the right axis.`;

  const width = Math.max(360, Math.round(svg.clientWidth));
  const height = Math.max(300, Math.min(430, Math.round(width * 0.43)));
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const left = width < 520 ? 66 : 96;
  const right = width < 520 ? 64 : 88;
  const top = 28;
  const bottom = 48;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const pnlValues = values.map((point) => point.pnl);
  const pnlMin = Math.min(0, ...pnlValues);
  const pnlMax = Math.max(0, ...pnlValues);
  const pnlSpan = pnlMax - pnlMin || 1;
  const pnlPadding = pnlSpan * 0.08;
  const pnlDomain = [pnlMin - pnlPadding, pnlMax + pnlPadding];
  const pctMin = Math.min(0, ...values.map((point) => point.pct));
  const pctMax = Math.max(0, ...values.map((point) => point.pct));
  const pctSpan = pctMax - pctMin || 1;
  const pctPadding = pctSpan * 0.08;
  const pctDomain = [pctMin - pctPadding, pctMax + pctPadding];
  const x = (index) => left + (values.length === 1 ? plotWidth / 2 : (index / (values.length - 1)) * plotWidth);
  const yPnl = (value) => top + ((pnlDomain[1] - value) / (pnlDomain[1] - pnlDomain[0])) * plotHeight;

  for (let tick = 0; tick <= 4; tick += 1) {
    const fraction = tick / 4;
    const y = top + fraction * plotHeight;
    const pnlTick = pnlDomain[1] - fraction * (pnlDomain[1] - pnlDomain[0]);
    const pctTick = pctDomain[1] - fraction * (pctDomain[1] - pctDomain[0]);
    svg.appendChild(svgElement('line', { x1: left, y1: y, x2: width - right, y2: y, class: 'chart-grid' }));
    svg.appendChild(svgElement('text', { x: left - 8, y: y + 4, 'text-anchor': 'end', class: 'chart-axis-text' }, formatCurrency(pnlTick, true)));
    svg.appendChild(svgElement('text', { x: width - right + 8, y: y + 4, 'text-anchor': 'start', class: 'chart-axis-text' }, `${pctTick.toFixed(1)}%`));
  }

  const zeroPnlY = yPnl(0);
  if (zeroPnlY >= top && zeroPnlY <= top + plotHeight) {
    svg.appendChild(svgElement('line', { x1: left, y1: zeroPnlY, x2: width - right, y2: zeroPnlY, class: 'chart-zero' }));
  }
  const pnlPath = values.map((point, index) => `${index ? 'L' : 'M'} ${x(index).toFixed(2)} ${yPnl(point.pnl).toFixed(2)}`).join(' ');
  svg.appendChild(svgElement('path', { d: pnlPath, class: 'chart-line-pnl' }));
  const hitArea = svgElement('path', { d: pnlPath, class: 'chart-line-hit-area', 'aria-label': 'Hover for profit and return' });
  const hoverMarker = svgElement('circle', { r: 5, class: 'chart-hover-marker', visibility: 'hidden' });
  svg.append(hitArea, hoverMarker);
  const chartState = { values, width, left, plotWidth, yPnl, x, includeYear };
  hitArea.addEventListener('pointermove', (event) => {
    hoverMarker.setAttribute('visibility', 'visible');
    showChartTooltip(event, chartState);
  });
  hitArea.addEventListener('pointerleave', () => {
    hoverMarker.setAttribute('visibility', 'hidden');
    hideChartTooltip();
  });

  const tickIndexes = new Set([0, values.length - 1]);
  const xTickCount = width < 520 ? 3 : 5;
  for (let tick = 1; tick < xTickCount; tick += 1) {
    tickIndexes.add(Math.round((tick / xTickCount) * (values.length - 1)));
  }
  Array.from(tickIndexes).sort((a, b) => a - b).forEach((index) => {
    const point = values[index];
    svg.appendChild(svgElement('line', { x1: x(index), y1: top + plotHeight, x2: x(index), y2: top + plotHeight + 5, class: 'chart-grid' }));
    svg.appendChild(svgElement('text', { x: x(index), y: height - 15, 'text-anchor': 'middle', class: 'chart-axis-text' }, formatDate(point.date, includeYear)));
  });

  svg.appendChild(svgElement('text', {
    x: 18,
    y: top + plotHeight / 2,
    transform: `rotate(-90 18 ${top + plotHeight / 2})`,
    'text-anchor': 'middle',
    class: 'chart-axis-title',
  }, 'P&L amount (₹)'));
  svg.appendChild(svgElement('text', {
    x: width - 18,
    y: top + plotHeight / 2,
    transform: `rotate(90 ${width - 18} ${top + plotHeight / 2})`,
    'text-anchor': 'middle',
    class: 'chart-axis-title',
  }, 'Return (%)'));
}

function selectedAssets() {
  const checked = Array.from(document.querySelectorAll('.history-asset-filters input[name="filter"]:checked'));
  const values = checked.map((input) => input.value);
  return values.includes('all') || values.length === 0 ? ['all'] : values;
}

let activeRequest = null;
let currentPoints = [];

async function loadHistory(period) {
  const params = new URLSearchParams();
  params.set('period', period);
  selectedAssets().forEach((asset) => params.append('filter', asset));
  if (activeRequest) activeRequest.abort();
  activeRequest = new AbortController();
  document.getElementById('history-period-input').value = period;
  document.querySelectorAll('[data-history-period]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.historyPeriod === period));
  });
  setStatus('Loading historical prices');
  try {
    const response = await fetch(`/api/portfolio-history?${params.toString()}`, {
      cache: 'no-store',
      signal: activeRequest.signal,
    });
    const result = await response.json();
    if (!response.ok || result.error) {
      throw new Error(result.error || `History request failed (${response.status}).`);
    }
    currentPoints = result.points || [];
    renderChart(currentPoints);
    renderWarnings(result.warnings || []);
    const sourceText = (result.sources || []).join(' and ');
    if (result.points && result.points.length) {
      setStatus(`${sourceText ? `Sources: ${sourceText}. ` : ''}${result.warnings?.length ? 'Some holdings are missing; see data notes.' : 'History is up to date.'}`, Boolean(result.warnings?.length));
    } else if (result.warnings?.length) {
      setStatus('No complete history could be built. Review the data notes.', true);
    } else {
      setStatus('No current holdings match these asset filters.');
    }
  } catch (error) {
    if (error.name === 'AbortError') return;
    setStatus(error.message, true);
    currentPoints = [];
    renderChart(currentPoints);
    renderWarnings([error.message]);
  }
}

document.querySelectorAll('[data-history-period]').forEach((button) => {
  button.addEventListener('click', () => loadHistory(button.dataset.historyPeriod));
});

document.querySelectorAll('.history-asset-filters input[name="filter"]').forEach((input) => {
  input.addEventListener('change', () => {
    const allInput = document.querySelector('.history-asset-filters input[value="all"]');
    const individualInputs = Array.from(document.querySelectorAll('.history-asset-filters input:not([value="all"])'));
    if (input.value === 'all' && input.checked) {
      individualInputs.forEach((individual) => { individual.checked = false; });
    } else if (input.value !== 'all' && input.checked) {
      allInput.checked = false;
    } else if (input.value !== 'all' && !individualInputs.some((individual) => individual.checked)) {
      allInput.checked = true;
    }
  });
});

const initialPeriod = document.getElementById('history-period-input').value || '1M';
loadHistory(initialPeriod);

window.addEventListener('resize', () => {
  if (currentPoints.length) renderChart(currentPoints);
});

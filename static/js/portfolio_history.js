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

function chartIndexAtPointer(event, chartState) {
  const { values, width, left, plotWidth } = chartState;
  const bounds = document.getElementById('portfolio-history-chart').getBoundingClientRect();
  const pointerX = ((event.clientX - bounds.left) / bounds.width) * width;
  if (pointerX < left - 12 || pointerX > left + plotWidth + 12) return -1;
  return values.length === 1
    ? 0
    : Math.max(0, Math.min(values.length - 1, Math.round(((pointerX - left) / plotWidth) * (values.length - 1))));
}

function showChartTooltip(event, chartState) {
  const { values, yPnl, x } = chartState;
  const svg = document.getElementById('portfolio-history-chart');
  const wrapper = svg.parentElement;
  const tooltip = document.getElementById('history-chart-tooltip');
  const index = chartIndexAtPointer(event, chartState);
  if (index < 0) {
    hideChartTooltip();
    return;
  }
  const point = values[index];
  if (!point) {
    hideChartTooltip();
    return;
  }

  const marker = svg.querySelector('.chart-hover-marker');
  marker.setAttribute('cx', x(index));
  marker.setAttribute('cy', yPnl(point.pnl));
  marker.classList.toggle('is-profit', point.pnl >= 0);
  marker.classList.toggle('is-loss', point.pnl < 0);
  tooltip.textContent = `${formatDate(point.date)} · ${formatSignedCurrency(point.pnl)} · ${formatSignedPercent(point.pct)}`;
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

function setHistoryLoading(isLoading, message = 'Loading historical prices') {
  const loading = document.getElementById('history-loading');
  const chartWrap = document.getElementById('history-chart-wrap');
  const status = document.getElementById('history-data-status');
  loading.hidden = !isLoading;
  chartWrap.classList.toggle('is-loading', isLoading);
  chartWrap.setAttribute('aria-busy', String(isLoading));
  status.textContent = message;
  status.classList.remove('is-error');
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

function formatDate(value) {
  return new Date(`${value}T00:00:00`).toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: '2-digit',
  });
}

function formatFullDate(value) {
  return new Date(`${value}T00:00:00`).toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

function renderChart(points, periodReturnPct = null, periodXirrPct = null) {
  const svg = document.getElementById('portfolio-history-chart');
  const zoomControls = document.getElementById('history-chart-zoom');
  hideChartTooltip();
  clearChart(svg);
  const title = svgElement('title', { id: 'history-chart-title' }, 'Portfolio profit and loss history');
  const desc = svgElement('desc', { id: 'history-chart-description' });
  svg.append(title, desc);

  const empty = document.getElementById('history-empty');
  if (!points.length) {
    empty.hidden = false;
    zoomControls.hidden = true;
    desc.textContent = 'No historical profit and loss values are available for this selection.';
    document.getElementById('history-latest-pnl').textContent = 'Not available';
    document.getElementById('history-latest-pct').textContent = 'Not available';
    document.getElementById('history-period-return').textContent = 'Not available';
    document.getElementById('history-date-range').textContent = '';
    return;
  }
  empty.hidden = true;
  zoomControls.hidden = points.length < 2;

  const values = points.map((point) => ({
    date: point.date,
    pnl: Number(point.pnl),
    pct: Number(point.pnl_pct),
  }));
  const latest = values[values.length - 1];
  chartVisibleCount = Math.max(1, Math.min(chartVisibleCount || values.length, values.length));
  chartStartIndex = Math.max(0, Math.min(chartStartIndex, values.length - chartVisibleCount));
  const visibleValues = values.slice(chartStartIndex, chartStartIndex + chartVisibleCount);
  const firstVisible = visibleValues[0];
  const lastVisible = visibleValues[visibleValues.length - 1];
  document.getElementById('history-latest-pnl').textContent = formatSignedCurrency(latest.pnl);
  document.getElementById('history-latest-pct').textContent = formatSignedPercent(latest.pct);
  const periodReturnLabel = document.getElementById('history-period-return');
  periodReturnLabel.replaceChildren();
  if (periodReturnPct == null) {
    periodReturnLabel.textContent = 'Not available';
  } else {
    const periodReturn = document.createElement('span');
    periodReturn.textContent = formatSignedPercent(periodReturnPct);
    periodReturnLabel.appendChild(periodReturn);
    if (periodXirrPct != null) {
      const xirr = document.createElement('small');
      xirr.className = 'history-period-xirr';
      xirr.textContent = `(XIRR ${formatSignedPercent(periodXirrPct)})`;
      periodReturnLabel.appendChild(xirr);
    }
  }
  document.getElementById('history-date-range').textContent = `${formatDate(firstVisible.date)} to ${formatDate(lastVisible.date)}`;
  desc.textContent = `Daily portfolio P&L from ${formatDate(firstVisible.date)} to ${formatDate(lastVisible.date)}. Rupee amounts use the right axis; return percentages are available on hover.`;

  const width = Math.max(360, Math.round(svg.clientWidth));
  const height = Math.max(300, Math.min(430, Math.round(width * 0.43)));
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.classList.toggle('is-zoomed', chartVisibleCount < values.length);
  const left = width < 520 ? 66 : 96;
  const right = width < 520 ? 64 : 88;
  const top = 28;
  const bottom = 48;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const pnlValues = visibleValues.map((point) => point.pnl);
  const pnlMin = Math.min(0, ...pnlValues);
  const pnlMax = Math.max(0, ...pnlValues);
  const pnlSpan = pnlMax - pnlMin || 1;
  const pnlPadding = pnlSpan * 0.08;
  const pnlDomain = [pnlMin - pnlPadding, pnlMax + pnlPadding];
  const x = (index) => left + (visibleValues.length === 1 ? plotWidth / 2 : (index / (visibleValues.length - 1)) * plotWidth);
  const yPnl = (value) => top + ((pnlDomain[1] - value) / (pnlDomain[1] - pnlDomain[0])) * plotHeight;

  for (let tick = 0; tick <= 4; tick += 1) {
    const fraction = tick / 4;
    const y = top + fraction * plotHeight;
    const pnlTick = pnlDomain[1] - fraction * (pnlDomain[1] - pnlDomain[0]);
    svg.appendChild(svgElement('line', { x1: left, y1: y, x2: width - right, y2: y, class: 'chart-grid' }));
    svg.appendChild(svgElement('text', { x: width - right + 8, y: y + 4, 'text-anchor': 'start', class: 'chart-axis-text' }, formatCurrency(pnlTick, true)));
  }

  const zeroPnlY = yPnl(0);
  if (zeroPnlY >= top && zeroPnlY <= top + plotHeight) {
    svg.appendChild(svgElement('line', { x1: left, y1: zeroPnlY, x2: width - right, y2: zeroPnlY, class: 'chart-zero' }));
  }
  let pnlPath = '';
  for (let index = 0; index < visibleValues.length; index += 1) {
    pnlPath += `${index ? 'L' : 'M'} ${x(index).toFixed(2)} ${yPnl(visibleValues[index].pnl).toFixed(2)} `;
  }
  for (let index = 0; index < visibleValues.length - 1; index += 1) {
    const startPoint = visibleValues[index];
    const endPoint = visibleValues[index + 1];
    const startX = x(index);
    const endX = x(index + 1);
    const startY = yPnl(startPoint.pnl);
    const endY = yPnl(endPoint.pnl);
    if ((startPoint.pnl < 0 && endPoint.pnl > 0) || (startPoint.pnl > 0 && endPoint.pnl < 0)) {
      const fraction = -startPoint.pnl / (endPoint.pnl - startPoint.pnl);
      const zeroX = startX + fraction * (endX - startX);
      const zeroY = yPnl(0);
      svg.appendChild(svgElement('path', {
        d: `M ${startX.toFixed(2)} ${startY.toFixed(2)} L ${zeroX.toFixed(2)} ${zeroY.toFixed(2)}`,
        class: `chart-line-pnl ${startPoint.pnl < 0 ? 'is-loss' : 'is-profit'}`,
      }));
      svg.appendChild(svgElement('path', {
        d: `M ${zeroX.toFixed(2)} ${zeroY.toFixed(2)} L ${endX.toFixed(2)} ${endY.toFixed(2)}`,
        class: `chart-line-pnl ${endPoint.pnl < 0 ? 'is-loss' : 'is-profit'}`,
      }));
    } else {
      const segmentPnl = (startPoint.pnl + endPoint.pnl) / 2;
      svg.appendChild(svgElement('path', {
        d: `M ${startX.toFixed(2)} ${startY.toFixed(2)} L ${endX.toFixed(2)} ${endY.toFixed(2)}`,
        class: `chart-line-pnl ${segmentPnl < 0 ? 'is-loss' : 'is-profit'}`,
      }));
    }
  }
  const hitArea = svgElement('path', { d: pnlPath.trim(), class: 'chart-line-hit-area', 'aria-label': 'Hover for profit and return' });
  const hoverMarker = svgElement('circle', { r: 5, class: 'chart-hover-marker', visibility: 'hidden' });
  svg.append(hitArea, hoverMarker);
  const chartState = { values: visibleValues, width, left, plotWidth, top, plotHeight, yPnl, x };
  hitArea.addEventListener('pointermove', (event) => {
    hoverMarker.setAttribute('visibility', 'visible');
    showChartTooltip(event, chartState);
  });
  hitArea.setAttribute('role', 'button');
  hitArea.setAttribute('tabindex', '0');
  hitArea.setAttribute('aria-label', 'Select a date to view portfolio details');
  hitArea.addEventListener('click', (event) => {
    if (suppressChartClick) return;
    const index = chartIndexAtPointer(event, chartState);
    if (index >= 0) loadBreakdown(visibleValues[index].date);
  });
  hitArea.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      loadBreakdown(visibleValues[visibleValues.length - 1].date);
    }
  });
  hitArea.addEventListener('pointerleave', () => {
    hoverMarker.setAttribute('visibility', 'hidden');
    hideChartTooltip();
  });

  const tickIndexes = new Set([0, visibleValues.length - 1]);
  const xTickCount = width < 520 ? 3 : 5;
  for (let tick = 1; tick < xTickCount; tick += 1) {
    tickIndexes.add(Math.round((tick / xTickCount) * (visibleValues.length - 1)));
  }
  Array.from(tickIndexes).sort((a, b) => a - b).forEach((index) => {
    const point = visibleValues[index];
    svg.appendChild(svgElement('line', { x1: x(index), y1: top + plotHeight, x2: x(index), y2: top + plotHeight + 5, class: 'chart-grid' }));
    svg.appendChild(svgElement('text', { x: x(index), y: height - 15, 'text-anchor': 'middle', class: 'chart-axis-text' }, formatDate(point.date)));
  });

  svg.appendChild(svgElement('text', {
    x: width - 18,
    y: top + plotHeight / 2,
    transform: `rotate(90 ${width - 18} ${top + plotHeight / 2})`,
    'text-anchor': 'middle',
    class: 'chart-axis-title',
  }, 'P&L amount (₹)'));
  updateChartZoomControls();
  const chartWrap = document.getElementById('history-chart-wrap');
  chartWrap.onwheel = (event) => {
    if (event.deltaX || event.shiftKey) {
      event.preventDefault();
      panChart(event.deltaX || event.deltaY);
      return;
    }
    event.preventDefault();
    const bounds = svg.getBoundingClientRect();
    const pointerX = ((event.clientX - bounds.left) / bounds.width) * width;
    const anchorFraction = Math.max(0, Math.min(1, (pointerX - left) / plotWidth));
    zoomChart(event.deltaY < 0 ? 1.25 : 0.8, anchorFraction);
  };
  svg.addEventListener('pointerdown', (event) => {
    if (chartVisibleCount >= values.length || event.button !== 0) return;
    const bounds = svg.getBoundingClientRect();
    const pointerX = ((event.clientX - bounds.left) / bounds.width) * width;
    const pointerY = ((event.clientY - bounds.top) / bounds.height) * height;
    if (
      pointerX < left
      || pointerX > left + plotWidth
      || pointerY < top
      || pointerY > top + plotHeight
    ) return;
    event.preventDefault();
    chartPan = { clientX: event.clientX, startIndex: chartStartIndex };
  });
}

function updateChartZoomControls() {
  const zoomLabel = document.getElementById('history-chart-zoom-level');
  const zoomIn = document.getElementById('history-chart-zoom-in');
  const zoomOut = document.getElementById('history-chart-zoom-out');
  if (!zoomLabel || !zoomIn || !zoomOut) return;
  const pointCount = currentPoints.length;
  const scale = pointCount && chartVisibleCount ? pointCount / chartVisibleCount : 1;
  zoomLabel.textContent = `${scale.toFixed(1)}×`;
  zoomIn.disabled = pointCount < 2 || chartVisibleCount <= 2;
  zoomOut.disabled = pointCount < 2 || chartVisibleCount >= pointCount;
}

function zoomChart(factor, anchorFraction = 0.5) {
  const pointCount = currentPoints.length;
  if (pointCount < 2) return;
  const oldCount = Math.max(2, Math.min(chartVisibleCount || pointCount, pointCount));
  const nextCount = Math.max(2, Math.min(pointCount, Math.round(oldCount / factor)));
  if (nextCount === oldCount) return;
  const anchorIndex = chartStartIndex + anchorFraction * (oldCount - 1);
  chartVisibleCount = nextCount;
  chartStartIndex = Math.max(0, Math.min(
    pointCount - nextCount,
    Math.round(anchorIndex - anchorFraction * (nextCount - 1)),
  ));
  renderChart(currentPoints, currentPeriodReturnPct, currentPeriodXirrPct);
}

function chartPlotMetrics() {
  const svg = document.getElementById('portfolio-history-chart');
  const width = svg.viewBox.baseVal.width;
  const left = width < 520 ? 66 : 96;
  const right = width < 520 ? 64 : 88;
  return { svg, width, plotWidth: width - left - right };
}

function panChart(deltaX) {
  if (currentPoints.length <= chartVisibleCount) return;
  const { svg, width, plotWidth } = chartPlotMetrics();
  const bounds = svg.getBoundingClientRect();
  const shift = Math.round((deltaX / bounds.width) * width / plotWidth * (chartVisibleCount - 1));
  if (!shift) return;
  chartStartIndex = Math.max(0, Math.min(currentPoints.length - chartVisibleCount, chartStartIndex + shift));
  renderChart(currentPoints, currentPeriodReturnPct, currentPeriodXirrPct);
}

function selectedAssets() {
  const checked = Array.from(document.querySelectorAll('.history-asset-filters input[name="filter"]:checked'));
  const values = checked.map((input) => input.value);
  return values.includes('all') || values.length === 0 ? ['all'] : values;
}

let activeRequest = null;
let activeBreakdownRequest = null;
let currentPoints = [];
let currentPeriodReturnPct = null;
let currentPeriodXirrPct = null;
let chartVisibleCount = 0;
let chartStartIndex = 0;
let chartPan = null;
let suppressChartClick = false;
const breakdownSortState = [];

function resetBreakdown() {
  if (activeBreakdownRequest) activeBreakdownRequest.abort();
  activeBreakdownRequest = null;
  document.getElementById('history-breakdown').hidden = true;
  document.getElementById('history-breakdown-rows').replaceChildren();
  document.getElementById('history-breakdown-total').replaceChildren();
  document.getElementById('history-breakdown-total').hidden = true;
}

function breakdownSortEntry(key) {
  return breakdownSortState.find((entry) => entry.key === key) || null;
}

function toggleBreakdownSort(key) {
  const existingIndex = breakdownSortState.findIndex((entry) => entry.key === key);
  if (existingIndex === -1) {
    breakdownSortState.unshift({ key, direction: 'asc' });
    return;
  }
  const existing = breakdownSortState[existingIndex];
  if (existing.direction === 'asc') {
    existing.direction = 'desc';
  } else {
    breakdownSortState.splice(existingIndex, 1);
  }
}

function breakdownSortValue(row, key) {
  const value = row.getAttribute(`data-${key.replace(/_/g, '-')}`) ?? '';
  if (key === 'symbol' || key === 'asset') {
    return value.trim().toUpperCase();
  }
  return Number(value || 0);
}

function sortBreakdownRows() {
  const tbody = document.getElementById('history-breakdown-rows');
  const rows = Array.from(tbody.querySelectorAll('tr[data-sort-row="true"]'));
  rows.sort((rowA, rowB) => {
    for (const entry of breakdownSortState) {
      const left = breakdownSortValue(rowA, entry.key);
      const right = breakdownSortValue(rowB, entry.key);
      let comparison = 0;
      if (typeof left === 'string' && typeof right === 'string') {
        comparison = left.localeCompare(right, 'en', { numeric: true, sensitivity: 'base' });
      } else if (left !== right) {
        comparison = left > right ? 1 : -1;
      }
      if (comparison) return comparison * (entry.direction === 'asc' ? 1 : -1);
    }
    return 0;
  });
  rows.forEach((row) => tbody.appendChild(row));
}

function renderBreakdownSortIndicators() {
  document.querySelectorAll('.history-breakdown-table .sort-button').forEach((button) => {
    const entry = breakdownSortEntry(button.dataset.sortKey);
    const direction = entry?.direction;
    const header = button.closest('th');
    button.classList.toggle('is-active', Boolean(entry));
    if (header) {
      header.setAttribute('aria-sort', direction === 'asc' ? 'ascending' : direction === 'desc' ? 'descending' : 'none');
    }
    button.querySelector('.sort-arrow-up')?.classList.toggle('is-active', direction === 'asc');
    button.querySelector('.sort-arrow-down')?.classList.toggle('is-active', direction === 'desc');
  });
}

function renderBreakdown(result) {
  const section = document.getElementById('history-breakdown');
  const tbody = document.getElementById('history-breakdown-rows');
  const footer = document.getElementById('history-breakdown-total');
  document.getElementById('history-breakdown-date').textContent = formatFullDate(result.date);
  document.getElementById('history-breakdown-status').textContent = result.rows.length
    ? `${result.rows.length} holdings`
    : 'No priced holdings for this date.';
  tbody.replaceChildren();
  footer.replaceChildren();

  const totals = { invested: 0, current_value: 0, pnl: 0 };
  result.rows.forEach((holding) => {
    totals.invested += Number(holding.invested);
    totals.current_value += Number(holding.current_value);
    totals.pnl += Number(holding.pnl);
    const row = document.createElement('tr');
    row.dataset.sortRow = 'true';
    row.dataset.symbol = holding.symbol;
    row.dataset.asset = holding.asset_class;
    row.dataset.quantity = holding.quantity;
    row.dataset.price = holding.price;
    row.dataset.invested = holding.invested;
    row.dataset.currentValue = holding.current_value;
    row.dataset.pnl = holding.pnl;

    const values = [
      [holding.symbol, 'text'],
      [holding.asset_class.toUpperCase(), 'text'],
      [Number(holding.quantity).toLocaleString('en-IN', { maximumFractionDigits: 3 }), 'numeric'],
      [formatCurrency(holding.price), 'numeric'],
      [formatCurrency(holding.invested), 'numeric'],
      [formatCurrency(holding.current_value), 'numeric'],
      [formatSignedCurrency(holding.pnl), 'numeric'],
    ];
    values.forEach(([value, className], index) => {
      const cell = document.createElement('td');
      cell.classList.toggle('numeric', className === 'numeric');
      cell.textContent = value;
      if (index === 0 && holding.name && holding.name !== holding.symbol) cell.title = holding.name;
      if (index === 6) cell.classList.add(holding.pnl >= 0 ? 'positive' : 'negative');
      row.appendChild(cell);
    });
    tbody.appendChild(row);
  });

  if (result.rows.length) {
    const totalRow = document.createElement('tr');
    const label = document.createElement('td');
    label.colSpan = 4;
    label.textContent = 'Total';
    totalRow.appendChild(label);
    ['invested', 'current_value', 'pnl'].forEach((key) => {
      const cell = document.createElement('td');
      cell.className = 'numeric';
      cell.textContent = key === 'pnl' ? formatSignedCurrency(totals[key]) : formatCurrency(totals[key]);
      if (key === 'pnl') cell.classList.add(totals.pnl >= 0 ? 'positive' : 'negative');
      totalRow.appendChild(cell);
    });
    footer.appendChild(totalRow);
    footer.hidden = false;
  } else {
    footer.hidden = true;
  }

  sortBreakdownRows();
  renderBreakdownSortIndicators();
  section.hidden = false;
  section.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadBreakdown(selectedDate) {
  if (activeBreakdownRequest) activeBreakdownRequest.abort();
  activeBreakdownRequest = new AbortController();
  const section = document.getElementById('history-breakdown');
  section.hidden = false;
  document.getElementById('history-breakdown-date').textContent = formatFullDate(selectedDate);
  document.getElementById('history-breakdown-status').textContent = 'Loading details…';
  document.getElementById('history-breakdown-rows').replaceChildren();
  document.getElementById('history-breakdown-total').hidden = true;

  const params = new URLSearchParams({ date: selectedDate });
  selectedAssets().forEach((asset) => params.append('filter', asset));
  try {
    const response = await fetch(`/api/portfolio-history/details?${params.toString()}`, {
      cache: 'no-store',
      signal: activeBreakdownRequest.signal,
    });
    const result = await response.json();
    if (!response.ok || result.error) {
      throw new Error(result.error || `History detail request failed (${response.status}).`);
    }
    renderBreakdown(result);
  } catch (error) {
    if (error.name === 'AbortError') return;
    document.getElementById('history-breakdown-status').textContent = error.message;
  }
}

async function loadHistory(period) {
  const params = new URLSearchParams();
  params.set('period', period);
  selectedAssets().forEach((asset) => params.append('filter', asset));
  if (activeRequest) activeRequest.abort();
  activeRequest = new AbortController();
  chartPan = null;
  suppressChartClick = false;
  document.getElementById('history-period-input').value = period;
  document.querySelectorAll('[data-history-period]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.historyPeriod === period));
  });
  resetBreakdown();
  setHistoryLoading(true, 'Loading historical price history');
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
    currentPeriodReturnPct = result.period_return_pct ?? null;
    currentPeriodXirrPct = result.period_xirr_pct ?? null;
    chartVisibleCount = currentPoints.length;
    chartStartIndex = 0;
    renderChart(currentPoints, currentPeriodReturnPct, currentPeriodXirrPct);
    renderWarnings(result.warnings || []);
    setHistoryLoading(false);
  } catch (error) {
    if (error.name === 'AbortError') return;
    setHistoryLoading(false);
    currentPoints = [];
    currentPeriodReturnPct = null;
    currentPeriodXirrPct = null;
    chartVisibleCount = 0;
    chartStartIndex = 0;
    renderChart(currentPoints, currentPeriodReturnPct, currentPeriodXirrPct);
    renderWarnings([error.message]);
  }
}

document.querySelectorAll('[data-history-period]').forEach((button) => {
  button.addEventListener('click', () => loadHistory(button.dataset.historyPeriod));
});

document.getElementById('history-chart-zoom-in').addEventListener('click', () => zoomChart(1.5));
document.getElementById('history-chart-zoom-out').addEventListener('click', () => zoomChart(1 / 1.5));

window.addEventListener('pointermove', (event) => {
  if (!chartPan) return;
  const deltaX = event.clientX - chartPan.clientX;
  if (Math.abs(deltaX) < 4) return;
  const { svg, width, plotWidth } = chartPlotMetrics();
  const bounds = svg.getBoundingClientRect();
  const shift = Math.round((-deltaX / bounds.width) * width / plotWidth * (chartVisibleCount - 1));
  chartStartIndex = Math.max(0, Math.min(
    currentPoints.length - chartVisibleCount,
    chartPan.startIndex + shift,
  ));
  suppressChartClick = true;
  svg.classList.add('is-panning');
  renderChart(currentPoints, currentPeriodReturnPct, currentPeriodXirrPct);
  document.getElementById('portfolio-history-chart').classList.add('is-panning');
});

function finishChartPan() {
  if (!chartPan) return;
  chartPan = null;
  document.getElementById('portfolio-history-chart').classList.remove('is-panning');
  if (suppressChartClick) {
    window.setTimeout(() => { suppressChartClick = false; }, 0);
  }
}

window.addEventListener('pointerup', finishChartPan);
window.addEventListener('pointercancel', finishChartPan);

document.querySelectorAll('.history-breakdown-table .sort-button').forEach((button) => {
  button.addEventListener('click', () => {
    toggleBreakdownSort(button.dataset.sortKey);
    sortBreakdownRows();
    renderBreakdownSortIndicators();
  });
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
  if (currentPoints.length) renderChart(currentPoints, currentPeriodReturnPct, currentPeriodXirrPct);
});

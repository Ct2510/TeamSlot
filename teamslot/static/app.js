const element = (id) => document.getElementById(id);
const cancellationTokens = new Map();
let viewRevision = 0;

function message(text, isError = false) {
  element('message').textContent = text;
  element('message').classList.toggle('error', isError);
  element('message').hidden = false;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || '無法完成操作，請稍後再試。');
  return result;
}

function post(path, payload, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  return api(path, { method: 'POST', headers, body: JSON.stringify(payload) });
}

function localInput(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function readableTime(value) {
  return new Date(value).toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', hour12: false });
}

function timeRange(start, end, windowEnd) {
  const endLabel = windowEnd && new Date(end).getTime() === windowEnd.getTime() ? '24:00' : readableTime(end);
  return `${readableTime(start)} – ${endLabel}`;
}

async function loadResources(selectedId) {
  const result = await api('/api/resources');
  const selector = element('resource');
  const previous = selectedId || selector.value;
  selector.replaceChildren();
  if (!result.resources.length) selector.append(new Option('請先新增一個資源', ''));
  result.resources.forEach((resource) => selector.append(new Option(resource.name, resource.id)));
  if (result.resources.some((resource) => resource.id === previous)) selector.value = previous;
  await refreshSchedule();
}

async function cancelBooking(bookingId, token) {
  await post(`/api/bookings/${encodeURIComponent(bookingId)}/cancel`, {}, token);
  cancellationTokens.delete(bookingId);
  if (element('receipt-id').value === bookingId) {
    element('receipt-id').value = '';
    element('receipt-token').value = '';
    element('receipt').hidden = true;
  }
  message('預約已取消，時段已釋出。');
  await refreshSchedule();
}

async function refreshSchedule() {
  const revision = ++viewRevision;
  const resourceId = element('resource').value;
  const container = element('bookings');
  element('availability').replaceChildren();
  container.replaceChildren();
  element('count').textContent = '0';
  if (!resourceId) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '新增資源後，開始安排第一個預約。';
    container.append(empty);
    return;
  }
  const start = new Date(`${element('day').value}T00:00:00`);
  if (Number.isNaN(start.getTime())) throw new Error('請選擇有效日期。');
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  const query = new URLSearchParams({ resource_id: resourceId, start: start.toISOString(), end: end.toISOString() });
  const [bookingsResult, availabilityResult] = await Promise.all([
    api(`/api/bookings?${query}`), api(`/api/availability?${query}`),
  ]);
  if (revision !== viewRevision) return;
  element('count').textContent = String(bookingsResult.bookings.length);
  if (!bookingsResult.bookings.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '今天尚無預約，安排一段團隊時間吧。';
    container.append(empty);
  }
  bookingsResult.bookings.forEach((booking) => {
    const card = document.createElement('article');
    card.className = 'booking';
    const content = document.createElement('div');
    const title = document.createElement('h3');
    title.textContent = booking.title;
    const time = document.createElement('p');
    const clippedStart = new Date(booking.start) < start ? start.toISOString() : booking.start;
    const clippedEnd = new Date(booking.end) > end ? end.toISOString() : booking.end;
    time.textContent = timeRange(clippedStart, clippedEnd, end);
    const organizer = document.createElement('p');
    organizer.textContent = `預約人：${booking.organizer}`;
    const identifier = document.createElement('p');
    identifier.textContent = `編號：${booking.id}`;
    content.append(title, time, organizer, identifier);
    card.append(content);
    if (cancellationTokens.has(booking.id)) {
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'cancel-button';
      cancel.textContent = '取消預約';
      cancel.addEventListener('click', () => withBusy(cancel, () => cancelBooking(booking.id, cancellationTokens.get(booking.id))));
      card.append(cancel);
    }
    container.append(card);
  });
  availabilityResult.intervals.forEach((interval) => {
    const slot = document.createElement('span');
    slot.className = 'slot';
    slot.textContent = timeRange(interval.start, interval.end, end);
    element('availability').append(slot);
  });
  if (!availabilityResult.intervals.length) element('availability').textContent = '這一天沒有至少 30 分鐘的空檔。';
}

async function withBusy(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
}

element('resource-form').addEventListener('submit', (event) => {
  event.preventDefault();
  withBusy(event.submitter, async () => {
    const resource = await post('/api/resources', { name: element('resource-name').value });
    element('resource-name').value = '';
    await loadResources(resource.id);
    message('資源已新增，可以開始預約。');
  });
});

element('booking-form').addEventListener('submit', (event) => {
  event.preventDefault();
  withBusy(event.submitter, async () => {
    if (!element('resource').value) throw new Error('請先新增或選擇資源。');
    const start = new Date(element('start').value);
    const end = new Date(element('end').value);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end <= start) throw new Error('結束時間必須晚於開始時間。');
    const booking = await post('/api/bookings', {
      resource_id: element('resource').value, title: element('title').value, organizer: element('organizer').value,
      start: start.toISOString(), end: end.toISOString(),
    });
    cancellationTokens.set(booking.id, booking.cancel_token);
    element('receipt-id').value = booking.id;
    element('receipt-token').value = booking.cancel_token;
    element('receipt').hidden = false;
    element('day').value = localInput(start).slice(0, 10);
    message('預約成功！請保存下方的取消憑證。');
    await refreshSchedule();
  });
});

element('cancel-form').addEventListener('submit', (event) => {
  event.preventDefault();
  withBusy(event.submitter, async () => {
    await cancelBooking(element('cancel-id').value.trim(), element('cancel-token').value.trim());
    element('cancel-token').value = '';
  });
});

for (const id of ['resource', 'day']) element(id).addEventListener('change', () => refreshSchedule().catch((error) => message(error.message, true)));
const defaultStart = new Date();
defaultStart.setMinutes(0, 0, 0);
defaultStart.setHours(defaultStart.getHours() + 1);
element('start').value = localInput(defaultStart);
element('end').value = localInput(new Date(defaultStart.getTime() + 3_600_000));
element('day').value = localInput(defaultStart).slice(0, 10);
element('timezone').textContent = `顯示時區：${Intl.DateTimeFormat().resolvedOptions().timeZone} · 資料統一以 UTC 儲存`;
loadResources().catch((error) => message(error.message, true));

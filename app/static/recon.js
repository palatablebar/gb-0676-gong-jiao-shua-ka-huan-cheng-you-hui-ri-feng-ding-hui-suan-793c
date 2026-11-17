const fb = document.querySelector('#recon-feedback');
let caseId = null;
let seq = 0;

function say(msg, bad) {
  fb.textContent = msg;
  fb.style.color = bad ? '#c0392b' : '';
}
async function call(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.message || '请求失败');
  return body;
}
function statusTag(s) {
  return { PROVISIONAL: '候选·预占', CONFIRMED: '已并账', EXPIRED: '已到期' }[s] || s;
}
function render(data) {
  caseId = data.case_id;
  document.querySelector('#recon-state').textContent =
    `候选 ${data.current.pending_candidates.length} · 已并账 ${data.current.confirmed_count} · 累计退差 ${data.current.total_refund} 元`;

  const rows = data.current.rows;
  document.querySelector('#rows-count').textContent = `${rows.length} 行`;
  document.querySelector('#recon-rows').innerHTML = rows.map(r =>
    `<tr><td>${r.event_id}</td><td>${r.card}</td><td>${r.route}</td><td>${r.business_day}</td><td>${r.base_fare}</td><td>${r.transfer_anchor}</td><td>${r.transfer_discount}</td><td>${r.cap_discount}</td><td>${r.expected}</td><td>${r.day_paid}</td><td>${(r._sources || []).join(',')}</td></tr>`
  ).join('') || '<tr><td colspan="11" class="empty">无行程</td></tr>';

  document.querySelector('#link-count').textContent = data.links.length;
  document.querySelector('#recon-links').innerHTML = data.links.map(l =>
    `<tr><td>${l.link_id}</td><td>${l.business_day}</td><td>${l.old_card} → ${l.new_card}</td><td>${l.gap_minutes ?? '—'}</td><td>${statusTag(l.status)}</td></tr>`
  ).join('') || '<tr><td colspan="5" class="empty">暂无</td></tr>';

  const latest = data.versions[data.versions.length - 1];
  document.querySelector('#version-count').textContent = `V${data.versions.length}`;
  document.querySelector('#recon-versions').innerHTML = data.versions.map(v =>
    `<tr><td>V${v.version}</td><td>${v.command_id}</td><td>${v.before_paid} → ${v.after_paid}</td><td>${v.refund}</td><td>${v.changes.length} 条</td></tr>`
  ).join('') || '<tr><td colspan="5" class="empty">尚未补证</td></tr>';

  if (latest) {
    const led = latest.quota_ledger;
    document.querySelector('#recon-ledger').innerHTML =
      '<div class="balance"><span>释放分卡预占</span><b>' + led.released.map(x => x.key).join('；') + '</b></div>' +
      '<div class="balance"><span>同人共享占用</span><b>' + led.held.map(x => `${x.key}（已用 ${x.used} / 余量 ${x.remaining}）`).join('；') + '</b></div>' +
      '<div class="bar"></div>';
    const labels = { transfer_anchor: '换乘锚点', transfer_discount: '换乘优惠', cap_discount: '封顶减免', expected: '应扣', day_paid: '日累计' };
    const lines = [];
    for (const ch of latest.changes)
      for (const [f, v] of Object.entries(ch.changed))
        lines.push(`<tr><td>${ch.event_id}</td><td>${ch.card}</td><td>${labels[f] || f}</td><td>${v.from ?? '—'}</td><td>${v.to ?? '—'}</td></tr>`);
    document.querySelector('#recon-diff').innerHTML = lines.join('') || '<tr><td colspan="5" class="empty">无字段变化</td></tr>';
  }
}

async function refresh() {
  if (!caseId) return;
  render(await call(`/api/recon/cases/${caseId}`));
}

document.querySelector('#recon-load').addEventListener('click', async () => {
  try { say('载入中…'); render(await call('/api/recon/cases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })); say('案卷已建立：候选权益已预占，当天按分卡结果交付。'); }
  catch (e) { say(e.message, true); }
});

document.querySelector('#recon-claim').addEventListener('click', async () => {
  if (!caseId) return say('请先载入案卷。', true);
  seq += 1;
  try {
    const data = await call(`/api/recon/cases/${caseId}/claim`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command_id: `CMD-${Date.now()}-${seq}`, claim: { business_day: '2026-11-16', old_card: 'C-OLD', new_card: 'C-NEW', replaced_at: '2026-11-16T08:50:00+08:00', evidence: '网点坏卡换卡回执#R-20261116-07' } })
    });
    render(data); say(data.applied ? '补证已转正：生成唯一同人权益结果 V' + data.version + '。' : '该指令已处理（幂等）。');
  } catch (e) { say(e.message, true); }
});

document.querySelector('#recon-retry').addEventListener('click', async () => {
  if (!caseId) return say('请先载入案卷。', true);
  const fixed = 'CMD-RETRY-0001';
  const body = JSON.stringify({ command_id: fixed, claim: { business_day: '2026-11-16', old_card: 'C-OLD', new_card: 'C-NEW' } });
  try {
    const first = await call(`/api/recon/cases/${caseId}/claim`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    render(first);
    const second = await call(`/api/recon/cases/${caseId}/claim`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    render(second);
    say(`同一补证重试两次：applied=${first.applied}/${second.applied}，退差仅记一次（${second.current.total_refund} 元）。`, !second.applied ? false : true);
  } catch (e) { say(e.message, true); }
});

document.querySelector('#recon-expire').addEventListener('click', async () => {
  if (!caseId) return say('请先载入案卷。', true);
  const pending = (await call(`/api/recon/cases/${caseId}`)).links.find(l => l.status === 'PROVISIONAL');
  if (!pending) return say('没有待裁决候选。', true);
  try { render(await call(`/api/recon/cases/${caseId}/expire/${pending.link_id}`, { method: 'POST' })); say('候选到期：预占额度已释放，维持分卡结果。'); }
  catch (e) { say(e.message, true); }
});

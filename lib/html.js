import { RECEIPT_TYPE_LABELS } from './domain.js';

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function layout(title, body) {
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${esc(title)}</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #17202a; }
    header { background: #102a43; color: white; padding: 24px 32px; }
    header a { color: #bdd7ff; }
    main { max-width: 980px; margin: 24px auto; padding: 0 20px 64px; }
    .card { background: white; border: 1px solid #d9e2ec; border-radius: 14px; padding: 20px; margin: 18px 0; box-shadow: 0 1px 2px rgba(16,42,67,.06); }
    .badge { display: inline-block; border-radius: 999px; padding: 3px 10px; font-size: 12px; font-weight: 700; margin-right: 6px; }
    .verified { background: #e3fcef; color: #006644; border: 1px solid #abf5d1; }
    .held { background: #e6f6ff; color: #075985; border: 1px solid #bae6fd; }
    .ended { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }
    .cross { background: #fff7ed; color: #9a3412; border: 1px solid #fed7aa; }
    .void { background: #fff1f2; color: #9f1239; border: 1px solid #fecdd3; }
    .muted { color: #64748b; font-size: 13px; }
    .receipt { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
    .receipt td { border-top: 1px solid #eef2f7; padding: 6px 8px; vertical-align: top; }
    .receipt td:first-child { color: #64748b; width: 130px; white-space: nowrap; }
    .answer, .question { border-left: 4px solid #2563eb; background: #f8fafc; padding: 12px 14px; border-radius: 8px; margin: 12px 0; }
    .answer { border-left-color: #059669; background: #ecfdf5; }
    .addendum { border-left: 3px solid #f59e0b; padding-left: 14px; margin: 16px 0; }
    .follow-index a { display: inline-block; margin: 4px 8px 4px 0; }
    .rating { color: #b45309; font-weight: 800; }
    code { background: #eef2f7; padding: 2px 5px; border-radius: 5px; }
    a { color: #1d4ed8; }
    .notice { border: 1px dashed #fb7185; background: #fff1f2; border-radius: 10px; padding: 12px 14px; margin: 12px 0; }
  </style>
</head>
<body>
  <header>
    <div class="muted" style="color:#bdd7ff">已实测 · 收货凭证核对评价册</div>
    <h1 style="margin:6px 0 0">${esc(title)}</h1>
  </header>
  <main>${body}</main>
</body>
</html>`;
}

function receiptTable(v) {
  if (!v) return '';
  const interval = v.interval;
  const hold = interval
    ? interval.end
      ? `<span class="badge ended">该件持有至 ${esc(interval.end)}</span>`
      : '<span class="badge held">发文后仍在持有区间</span>'
    : '';
  return `
    <table class="receipt">
      <tr><td>核对状态</td><td><span class="badge verified">${esc(v.message || v.status)}</span>${hold}</td></tr>
      <tr><td>实际收用款号</td><td><strong>${esc(v.actualModelCode)}</strong></td></tr>
      <tr><td>收货凭证</td><td><code>${esc(v.receiptId)}</code>${v.voucherNo ? ` / 凭证号 <code>${esc(v.voucherNo)}</code>` : ''} · ${esc(v.typeLabel || RECEIPT_TYPE_LABELS[v.receiptType] || v.receiptType || '')} · <a href="/api/receipts/${esc(v.receiptId)}" target="_blank" rel="noreferrer">核对 JSON</a></td></tr>
      ${v.unitId ? `<tr><td>具体物件编号</td><td><code>${esc(v.unitId)}</code></td></tr>` : ''}
      ${interval ? `<tr><td>可试用区间</td><td>${esc(interval.start)} ～ ${esc(interval.end || '仍持有')}${interval.returnReceiptId ? `；收回凭证 <code>${esc(interval.returnReceiptId)}</code>` : ''}</td></tr>` : ''}
      ${v.correctedFromReceiptId ? `<tr><td>更正前凭证</td><td><code>${esc(v.correctedFromReceiptId)}</code>，以现行有效凭证为准</td></tr>` : ''}
    </table>`;
}

function renderAddendum(a, modelCode) {
  if (a.status === 'WITHHELD') {
    return `<section class="addendum" id="${esc(a.anchor)}">
      <div class="notice">追记 <code>${esc(a.id)}</code> 的补交/收货依据已失效：${esc(a.verification?.message || a.withholdReason || '')}。不会改挂其他款。</div>
    </section>`;
  }
  const cross = a.verification?.actualModelCode && a.verification.actualModelCode !== modelCode
    ? `<span class="badge cross">换后实际为 ${esc(a.verification.actualModelCode)}，非原款 ${esc(modelCode)}</span>`
    : '';
  return `<section class="addendum" id="${esc(a.anchor)}">
    <h3 style="margin:0 0 6px">📝 有时间的追记 ${esc(a.title || '')} ${cross}</h3>
    <div class="muted">${esc(a.createdAt)} · 类型 ${esc(a.kind || 'FOLLOW_UP')}${a.side ? ` · 部位 ${esc(a.side)}` : ''}</div>
    <p style="white-space:pre-wrap">${esc(a.body)}</p>
    ${receiptTable(a.verification)}
  </section>`;
}

function renderReview(r, modelCode) {
  const fu = r.followUp;
  return `<article class="card" id="${esc(r.anchor)}">
    <div>
      <span class="badge verified">已实测首测</span>
      <span class="badge held">说的是 ${esc(r.verification.actualModelCode)}</span>
      <span class="rating">${'★'.repeat(r.rating || 0)}${'☆'.repeat(5 - (r.rating || 0))}</span>
    </div>
    <h2 style="margin:10px 0 4px">${esc(r.title || '无标题体验')}</h2>
    <div class="muted">作者 ${esc(r.author.displayName)}（<code>${esc(r.author.buyerId)}</code>） · 首发于 ${esc(r.publishedAt)}${r.experiencedAt && r.experiencedAt !== r.publishedAt ? ` · 标注实测时间 ${esc(r.experiencedAt)}` : ''} · 评价编号 <code>${esc(r.id)}</code></div>
    <p style="white-space:pre-wrap;font-size:16px;line-height:1.7">${esc(r.body)}</p>
    ${receiptTable(r.verification)}
    ${(r.addenda || []).map((a) => renderAddendum(a, modelCode)).join('')}
    ${fu?.answer ? `
      <section class="question" id="${esc(fu.id ? `followup-${fu.id}` : '')}">
        <div class="muted">商家针对本条具体体验 <code>${esc(fu.id)}</code> 发起的一次补充询问 · ${esc(fu.createdAt)}</div>
        <p><strong>商家问：</strong>${esc(fu.merchantBody)}</p>
        <div class="answer" id="${esc(fu.answer.anchor)}">
          <div class="muted">买家从本人正常账号作答 · ${esc(fu.answer.createdAt)}</div>
          <p style="white-space:pre-wrap;margin-bottom:0"><strong>后续答：</strong>${esc(fu.answer.body)}</p>
          <div class="muted">读者定位：<a href="${esc(fu.urlSuffix)}">${esc(fu.urlSuffix)}</a></div>
        </div>
      </section>` : ''}
  </article>`;
}

export function renderIndex(store) {
  const products = Object.values(store.products);
  const body = `
    <section class="card">
      <h2>按款号打开公开评价册</h2>
      <p>每条首测只挂在买家凭有效收货凭证实际收用的款号下；换件、拒收、退件和补交各有凭证，售后退款金额不作为实测资格依据。</p>
      <ul>${products.map((p) => `<li><a href="/p/${encodeURIComponent(p.modelCode)}"><code>${esc(p.modelCode)}</code> ${esc(p.name)}</a> · <a href="/api/products/${encodeURIComponent(p.modelCode)}/page">JSON</a></li>`).join('')}</ul>
      <p class="muted">接口说明见 <a href="/api">/api</a>。</p>
    </section>`;
  return layout('已实测评价服务', body);
}

export function renderProductPage(data) {
  const p = data.product;
  const followIndex = data.followUpIndex.length ? `
    <section class="card follow-index">
      <h2>读者后续入口</h2>
      <p class="muted">作答后，产品页生成可定位到原测下答复的永久锚点。</p>
      ${data.followUpIndex.map((f) => `<a href="${esc(f.url)}">${esc(f.answeredAt)} 的商家问答后续 ↗</a>`).join('')}
    </section>` : '';

  const notices = (data.correctionNotices || []).map((n) => `
    <div class="notice" id="${esc(n.anchor)}">
      <strong>一篇旧刊已冻结/撤下：</strong><code>${esc(n.id)}</code> 原挂款号 <code>${esc(n.modelCode)}</code>。<br>
      原因：${esc(n.withholdReason || '收货依据失效')}。系统保留审计记录，<strong>不会静默搬到另一款</strong>。
      ${n.question?.id ? `<br>关联询问 <code>${esc(n.question.id)}</code> 状态：<span class="badge void">${esc(n.question.status)}</span>` : ''}
    </div>`).join('');

  const cross = (data.crossModelAddenda || []).length ? `
    <section class="card">
      <h2>涉及本款的跨款换样追记</h2>
      ${data.crossModelAddenda.map((a) => `<p><a href="${esc(a.sourceUrl)}">${esc(a.createdAt)} ${esc(a.title || a.addendumId)} → 查看原评价册锚点</a></p>`).join('')}
    </section>` : '';

  const reviews = data.reviews.length
    ? data.reviews.map((r) => renderReview(r, data.modelCode)).join('')
    : '<section class="card"><p>当前没有凭有效收货依据刊出的首测。</p></section>';

  const body = `
    <section class="card">
      <div class="muted">款号 / SKU</div>
      <h2 style="margin:4px 0">${esc(data.modelCode)} · ${esc(p.name || '')}</h2>
      <p class="muted">本页公开首测 ${data.reviewCount} 篇；每篇均标明对应的是哪一次到件、拒收、退件或换件补交。</p>
      <p><a href="/api/products/${encodeURIComponent(data.modelCode)}/page">查看本页 JSON</a></p>
    </section>
    ${followIndex}
    ${notices}
    ${cross}
    ${reviews}
  `;
  return layout(`${data.modelCode} · ${p.name || '产品页'}`, body);
}
